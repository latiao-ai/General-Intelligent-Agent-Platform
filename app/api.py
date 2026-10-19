"""FastAPI + SSE 服务：把智能体的执行过程以事件流推给前端。

与 Streamlit 版的区别：那边是"后台线程 + 0.5 秒轮询"的准实时；这里是事件驱动，
节点一发出事件就立刻推送，延迟只在网络往返量级，且天然支持多客户端同时订阅同
一个任务。

启动：
    uvicorn app.api:app --reload --port 8000
或：
    python -m scripts.serve_api
"""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import memory as memory_module, profile
from app.store import now
from app.api_hub import CLOSE, Hub
from app.config import settings
from app.main import deploy_task

WEB_DIR = Path(__file__).resolve().parent / 'web'

# 整理报告阶段可能在上百秒内没有任何事件，必须定期发心跳，
# 否则连接会被中间的代理/网关按空闲超时掐断。
KEEPALIVE_SECONDS = 15

app = FastAPI(title='通用 Agent 执行中台 API', version='2.0')
hub = Hub()
# 启动即对账：进程重启后遗留的运行要按"已发出未回执=unknown"处理，
# 不能简单当成中断（文档 §8.3）。
RECOVERY = hub.reconcile()


class ProfileRequest(BaseModel):
    # 画像就是一份 Markdown，直接收原文：手改、粘贴、整段重写都走同一条路。
    text: str = Field(default='', max_length=20000)


class MemoryRequest(BaseModel):
    summary: str = Field(default='', max_length=4000)
    # 关键事实用「键：值」多行文本编辑，与画像保持同一种手感
    facts: str = Field(default='', max_length=4000)


class TaskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    session_id: str | None = None


def sse(event: dict) -> str:
    """SSE 帧。事件类型放在 JSON 里，前端统一用 onmessage 处理。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get('/', response_class=HTMLResponse)
async def index():
    # 页面是内联 JS 的单文件，一旦被浏览器缓存就会出现"代码改了但界面还是旧的"，
    # 排查时极易被误导。这里明确不缓存。
    return HTMLResponse((WEB_DIR / 'index.html').read_text(encoding='utf-8'),
                        headers={'Cache-Control': 'no-store, must-revalidate'})


@app.post('/api/tasks')
async def create_task(payload: TaskRequest):
    """开一轮任务。带上 session_id 即可实现多轮（追加要求）。"""
    record = hub.start(payload.message, payload.session_id)
    return {'task_id': record.task_id, 'session_id': record.session_id}


@app.get('/api/tasks/{task_id}')
async def get_task(task_id: str):
    record = hub.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    return record.summary()


@app.post('/api/tasks/{task_id}/cancel')
async def cancel_task(task_id: str):
    if hub.get(task_id) is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    return {'cancelled': hub.cancel(task_id)}


@app.get('/api/tasks/{task_id}/events')
async def task_events(task_id: str):
    record = hub.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail='任务不存在')

    async def stream():
        queue, history = record.subscribe()
        last_seq = 0
        try:
            # 先补发库里已有的事件：晚连上来、甚至服务重启后再连，都能看到完整过程。
            for seq, event in history:
                last_seq = seq
                yield sse(event)
            if record.done:
                yield sse({'type': 'end', 'status': record.status})
                return
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(),
                                                  timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # 用真实事件而不是 SSE 注释（": keepalive"）做心跳：
                    # 注释不会触发浏览器的 onmessage，前端就没法判断连接是否还活着，
                    # 后端一死那一行就会永远停在"进行中"、耗时一路涨。
                    yield sse({'type': 'ping'})
                    continue
                if item is CLOSE:
                    yield sse({'type': 'end', 'status': record.status})
                    return
                seq, event = item
                if seq is not None:
                    if seq <= last_seq:
                        # 订阅与读快照之间重复投递的事件，按序号丢弃。
                        continue
                    last_seq = seq
                # seq 为 None 的是不落库的过程事件（如 draft），始终透传
                yield sse(event)
        finally:
            record.unsubscribe(queue)

    return StreamingResponse(stream(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        # 关掉 nginx 一类反向代理的响应缓冲，否则事件会被攒着一起发。
        'X-Accel-Buffering': 'no',
    })


@app.post('/api/tasks/{task_id}/deploy')
async def deploy(task_id: str):
    record = hub.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    if not record.artifact_id:
        raise HTTPException(status_code=409, detail='该任务还没有生成产物')
    try:
        result = await deploy_task(record.artifact_id)
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail=f'部署失败（{type(exc).__name__}）') from None
    if record.state is not None:
        record.state['deployment'] = result
        record.state['status'] = 'deployed'
        record.persist(state=record.state, status='deployed')
    record.publish({'type': 'result', **result})
    return result


@app.get('/api/tasks/{task_id}/artifact')
async def artifact(task_id: str):
    """下载/预览任务产物。

    文件路径以任务 state 里记的 `artifact.html_path` 为准——报告是 index.html，
    "资料转 HTML" 产出的是 sources.html；早先这里写死 index.html，导致资料页 404。
    """
    record = hub.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail='产物不存在')
    recorded = ((record.state or {}).get('artifact') or {}).get('html_path')
    if recorded:
        path = Path(recorded)
    elif record.artifact_id:
        # 旧任务没有记录路径，按约定回退
        path = settings.artifact_dir / record.artifact_id / 'index.html'
    else:
        raise HTTPException(status_code=404, detail='产物不存在')
    if not path.exists():
        raise HTTPException(status_code=404, detail='产物不存在')
    return FileResponse(path, media_type='text/html', filename=path.name)


@app.get('/api/runs/{run_id}')
async def read_run(run_id: str):
    """run 的状态、计划与步骤（文档 §13）。"""
    run = hub.store.run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='运行不存在')
    plan = hub.store.plan(run_id) or {}
    return {'run': run, 'plan': plan,
            'steps': hub.store.steps_of(run_id),
            'actions': [a for a in (hub.store.actions_of(run_id))]}


@app.post('/api/runs/{run_id}/pause')
async def pause_run(run_id: str):
    if not hub.pause(run_id):
        raise HTTPException(status_code=409, detail='该运行不在可暂停状态')
    return {'paused': True}


@app.post('/api/runs/{run_id}/resume')
async def resume_run(run_id: str):
    record = hub.resume(run_id)
    if record is None:
        raise HTTPException(status_code=409, detail='没有可续跑的计划')
    return {'resumed': True, 'task_id': record.task_id}


@app.post('/api/runs/{run_id}/cancel')
async def cancel_run(run_id: str):
    """取消：停止后续步骤，回执说明在途动作状态（文档 §8.4）。"""
    run = hub.store.run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='运行不存在')
    hub.cancel(run['task_id'])
    hub.store.update_run(run_id, status='cancelled',
                         cancel_requested_at=now())
    return {'cancelled': True}


@app.get('/api/recovery')
async def recovery_status():
    return RECOVERY


@app.get('/api/profile')
async def read_profile():
    """当前的用户画像：原文 + 解析后的事实 + 版本号。"""
    return {'text': profile.raw(), 'facts': profile.load(),
            'version': profile.version()}


@app.put('/api/profile')
async def write_profile(payload: ProfileRequest):
    """整体覆盖画像。只解析"键：值"行，头部元信息由服务端重写。"""
    facts = profile.parse(payload.text)
    version = profile.save(facts)
    return {'version': version, 'facts': facts, 'count': len(facts)}


@app.delete('/api/profile')
async def delete_profile():
    profile.clear()
    return {'cleared': True}


@app.get('/api/sessions/{session_id}/memory')
async def read_memory(session_id: str):
    """会话记忆：更早对话的压缩结果，可读可改。"""
    state = hub.store.memory(session_id)
    return {'summary': state['summary'], 'facts': state['facts'],
            'facts_text': memory_module.render_facts(state['facts']),
            'upto': state['upto']}


@app.put('/api/sessions/{session_id}/memory')
async def write_memory(session_id: str, payload: MemoryRequest):
    """手动改写会话记忆（摘要 + 关键事实）。"""
    facts = memory_module.parse_facts(payload.facts)
    upto = hub.store.memory(session_id)['upto']
    hub.store.set_memory(session_id, summary=payload.summary, facts=facts,
                         upto=upto)
    return {'summary': payload.summary, 'facts': facts, 'upto': upto}


@app.delete('/api/sessions/{session_id}/memory')
async def delete_memory(session_id: str):
    """清空会话记忆，下轮会从原文重新开始累积。"""
    hub.store.clear_memory(session_id)
    return {'cleared': True}


@app.get('/api/sessions')
async def list_sessions():
    """会话列表（侧边栏用）：按最近活跃排序，含标题与消息数。"""
    return {'sessions': hub.list_sessions()}


@app.get('/api/sessions/{session_id}/messages')
async def session_messages(session_id: str):
    return {'session_id': session_id, 'messages': hub.history(session_id)}


@app.get('/api/sessions/{session_id}/timeline')
async def session_timeline(session_id: str):
    """会话回放：消息气泡 + 每轮执行过程事件，供切换会话时重建步骤面板。"""
    return hub.session_timeline(session_id)
