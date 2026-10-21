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
import mimetypes
import logging
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, HTMLResponse, Response,
                               StreamingResponse)
from pydantic import BaseModel, Field

from app import idempotency, memory as memory_module, outcome as outcome_module
from app import auth, policies, profile
from app import tools as tool_module
from app.api_hub import CLOSE, Hub
from app.config import settings
from app.logging_setup import setup_logging

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / 'web'

# 整理报告阶段可能在上百秒内没有任何事件，必须定期发心跳，
# 否则连接会被中间的代理/网关按空闲超时掐断。
KEEPALIVE_SECONDS = 15

hub = Hub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程生命周期：启动时对账、退出时收尾。

    对账**不能放在模块导入处**（原来是 `RECOVERY = await hub.reconcile()`）：那样
    `uvicorn --reload` 一次、或者起第二个副本，就会把别的进程正在跑的任务判成
    "结果未知"并清掉租约，于是立刻又被领走 —— 正好制造它想避免的重复执行。
    现在只处理**自己名下的或租约已过期**的运行。
    """
    setup_logging('api')
    # 先确保仓储就绪（PG 会在这里打开连接池并建表）：连不上库应当在启动日志里
    # 直接失败，而不是等第一个请求到了才炸
    ready = getattr(hub.store, 'ready', None)
    if callable(ready):
        await ready()
    # 认证依赖要在请求里拿仓储（token → 平台 userinfo → 本地 user_id），
    # 但 auth 不能 import api（会成环），所以挂在 app.state 上按请求取。
    app.state.store = hub.store
    stats = await hub.reconcile(owner=hub.owner_id)
    app.state.recovery = stats
    purged = await hub.store.purge_request_keys()
    logger.info('API 启动完成，对账结果 %s，清理过期幂等键 %s 条', stats, purged)
    try:
        yield
    finally:
        await hub.close()
        await auth.aclose()
        # 连接池也要显式关：否则退出时池里的连接靠 GC 收，日志里看不到
        # "到底关没关"，测速/压测反复起停还会在服务端堆出空闲连接。
        close_store = getattr(hub.store, 'close', None)
        if callable(close_store):
            close_store()
        logger.info('API 已关闭，数据库连接池已收尾')


app = FastAPI(title='通用 Agent 执行中台 API', version='2.0', lifespan=lifespan)


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
    # 也可以放在 Idempotency-Key 头里；两处都给时以头为准。
    idempotency_key: str | None = Field(default=None, max_length=200)


def sse(event: dict) -> str:
    """SSE 帧。事件类型放在 JSON 里，前端统一用 onmessage 处理。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class TokenRequest(BaseModel):
    # 平台发的凭证。服务端会拿它去 userinfo 端点验证，**不信任其中的任何字段**。
    token: str = Field(min_length=1, max_length=8000)


# ---------- 身份 ----------
# 浏览器侧必须有 cookie：EventSource 与 <a download> 都发不出自定义请求头，
# 只支持 Authorization 的话 SSE 事件流和产物下载会全部 401。


@app.get('/api/me')
async def whoami(principal=Depends(policies.current_principal)):
    """当前主体。配好 AUTH_MODE 后用它确认「token → 身份」这条链路是通的。"""
    return {'tenant_id': principal.tenant_id,
            'user_id': principal.user_id,
            'role': principal.role,
            'anonymous': principal.anonymous,
            'auth_mode': settings.auth_mode}


@app.post('/api/auth/session')
async def create_session(payload: TokenRequest, request: Request,
                         response: Response):
    """用平台 token 换一个 HttpOnly cookie。

    过渡实现：cookie 里放的就是平台 token。正式版应换成自己签发的短期票据，
    否则平台 token 一过期用户就掉线，且撤销只能等平台。
    """
    if settings.auth_mode == 'off':
        raise HTTPException(status_code=400,
                            detail='AUTH_MODE=off，未启用认证')
    store = getattr(request.app.state, 'store', None)
    if store is None:
        raise HTTPException(status_code=503, detail='身份仓储未就绪')
    try:
        info = await auth.fetch_userinfo(payload.token)
    except auth.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    row = await store.bind_identity(info.provider, info.subject,
                                    email=info.email,
                                    display_name=info.display_name)
    response.set_cookie(
        settings.auth_cookie_name, payload.token,
        max_age=settings.auth_cookie_max_age,
        httponly=True, samesite='lax', secure=settings.auth_cookie_secure,
        path='/')
    return {'user_id': row['user_id'], 'tenant_id': row['tenant_id']}


@app.delete('/api/auth/session')
async def drop_session(response: Response):
    response.delete_cookie(settings.auth_cookie_name, path='/')
    return {'ok': True}


# ---------- 对象权限 ----------
# 文档 §10 要求"订阅 SSE、下载产物、读取画像全部执行对象权限"。
# 以前只有 /api/runs 与 /api/artifacts 做了检查，其余接口知道 ID 就能读别人的
# 会话——而 session_id 是客户端传入的 12 位十六进制，猜中即接管上下文。


async def _owned_task(task_id: str, principal):
    record = await hub.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    policies.ensure_owner({'tenant_id': record.tenant_id,
                           'owner_user_id': record.owner_user_id},
                          principal, '任务')
    return record


async def _owned_session(session_id: str, principal) -> dict:
    row = await hub.store.session(session_id)
    if row is None:
        # 有些旧数据只有 messages 没有 sessions 行；按"没有"处理更安全
        raise HTTPException(status_code=404, detail='会话不存在')
    return policies.ensure_owner(row, principal, '会话')


async def _owned_run(run_id: str, principal) -> dict:
    run = await hub.store.run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='运行不存在')
    meta = await hub.store.task_meta(run['task_id']) or {}
    resource = dict(run)
    resource['owner_user_id'] = meta.get('owner_user_id') or 'default'
    resource['tenant_id'] = run.get('tenant_id') or 'default'
    policies.ensure_owner(resource, principal, '运行')
    return run


@contextmanager
def _profile_scope(principal):
    """把画像读写限定在当前主体下（profile/<user>.md）。"""
    token = profile.use(principal.user_id)
    try:
        yield
    finally:
        profile.reset(token)


@app.get('/', response_class=HTMLResponse)
async def index():
    # 页面是内联 JS 的单文件，一旦被浏览器缓存就会出现"代码改了但界面还是旧的"，
    # 排查时极易被误导。这里明确不缓存。
    return HTMLResponse((WEB_DIR / 'index.html').read_text(encoding='utf-8'),
                        headers={'Cache-Control': 'no-store, must-revalidate'})


@app.post('/api/tasks')
async def create_task(payload: TaskRequest, request: Request,
                      principal=Depends(policies.current_principal)):
    """开一轮任务。带上 session_id 即可实现多轮（追加要求）。

    幂等：客户端用 `Idempotency-Key` 头（或请求体 `idempotency_key`）表达
    "这是我那个请求的重试"。同一个键 + 同样的内容只会产生一个任务；
    键相同但内容不同返回 409，而不是悄悄建第二个任务。
    """
    client_key = (request.headers.get('idempotency-key')
                  or payload.idempotency_key or '').strip()
    if not client_key:
        return await _start_task(payload, principal)

    try:
        reservation = await idempotency.reserve(
            hub.store, principal=principal, endpoint='tasks.create',
            client_key=client_key,
            payload={'message': payload.message,
                     'session_id': payload.session_id},
            ttl_seconds=settings.idempotency_ttl_seconds)
    except idempotency.IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except idempotency.IdempotencyInFlight as exc:
        raise HTTPException(status_code=409, detail=str(exc),
                            headers={'Retry-After': '1'}) from None
    except idempotency.IdempotencyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    if reservation.replayed:
        body = dict(await reservation.response or {})
        body['idempotent_replay'] = True
        logger.info('幂等重放 task=%s', body.get('task_id'))
        return body

    try:
        body = await _start_task(payload, principal)
    except BaseException:
        # 占位了却没建成任务：撤销占位，否则这个键会永远返回一个不存在的任务
        await reservation.release()
        raise
    await reservation.complete(body, task_id=body['task_id'],
                              session_id=body['session_id'], run_id=body['run_id'])
    return body


async def _start_task(payload: TaskRequest, principal) -> dict:
    record = await hub.start(payload.message, payload.session_id, principal)
    # status 是展示标签（旧字段名不变），run_status 才是状态机里的通用状态
    return {'task_id': record.task_id, 'session_id': record.session_id,
            'run_id': record.run_id,
            **outcome_module.view(record.status, record.state or {})}


@app.get('/api/tasks/{task_id}')
async def get_task(task_id: str,
                   principal=Depends(policies.current_principal)):
    return (await _owned_task(task_id, principal)).summary()


@app.post('/api/tasks/{task_id}/cancel')
async def cancel_task(task_id: str,
                      principal=Depends(policies.current_principal)):
    record = await _owned_task(task_id, principal)
    if not await hub.cancel(record.task_id):
        # 老实说"没停成"，而不是把 run 标成 cancelled 让界面以为停了
        raise HTTPException(status_code=409, detail='该任务已经结束，无法取消')
    return {'cancelled': True}


@app.get('/api/tasks/{task_id}/events')
async def task_events(task_id: str, request: Request,
                      after: str | None = Query(default=None),
                      principal=Depends(policies.current_principal)):
    record = await _owned_task(task_id, principal)

    # 游标：优先用 Last-Event-ID 头，其次 ?after=，从最后确认的 seq 之后补发
    cursor = request.headers.get('last-event-id') or after or '0'

    async def stream():
        queue, history = await record.subscribe()
        try:
            last_seq = int(cursor)
        except (TypeError, ValueError):
            last_seq = 0
        try:
            # 先补发库里已有的事件：晚连上来、甚至服务重启后再连，都能看到完整过程。
            for seq, event in history:
                if seq is not None and seq <= last_seq:
                    continue          # 断线前已收到过的，不重复推
                if seq is not None:
                    last_seq = seq
                # 带 id: 让标准 EventSource 记住 Last-Event-ID
                yield (f'id: {last_seq}\n' if seq is not None else '') + sse(event)
            if record.done:
                yield sse({'type': 'end',
                           **outcome_module.view(record.status, record.state or {})})
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
                    yield sse({'type': 'end', 'status': outcome_module.label(
                        record.status, record.state or {}),
                        'run_status': outcome_module.normalize_status(record.status)})
                    return
                seq, event = item
                if seq is not None:
                    if seq <= last_seq:
                        # 订阅与读快照之间重复投递的事件，按序号丢弃。
                        continue
                    last_seq = seq
                # seq 为 None 的是不落库的过程事件（如 draft），始终透传
                yield (f'id: {last_seq}\n' if seq is not None else '') + sse(event)
        finally:
            record.unsubscribe(queue)

    return StreamingResponse(stream(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        # 关掉 nginx 一类反向代理的响应缓冲，否则事件会被攒着一起发。
        'X-Accel-Buffering': 'no',
    })


@app.post('/api/tasks/{task_id}/deploy')
async def deploy(task_id: str,
                 principal=Depends(policies.current_principal)):
    """部署按钮：**走执行器**，不再直接调 deploy_task。

    直接调等于开了一个后门：没有账本 action（崩在中途不知道发没发出去）、
    没有事件序号（界面看不到过程）、不经过策略检查，暂停/取消也管不到它。
    现在它是一次正常执行：建 run → 一步计划 → 账本 + 事件 + 取消语义全都复用。
    """
    record = await _owned_task(task_id, principal)
    artifact = (record.state or {}).get('artifact') or {}
    if not artifact.get('html_path'):
        raise HTTPException(status_code=409, detail='该任务还没有生成产物')
    if not settings.deploy_enabled:
        raise HTTPException(status_code=409,
                            detail='部署已关闭（DEPLOY_ENABLED=false）')
    try:
        result = await hub.deploy_artifact(record, artifact, principal)
    except Exception as exc:                     # noqa: BLE001
        logger.exception('部署失败 task=%s', task_id)
        raise HTTPException(status_code=502,
                            detail=f'部署失败（{type(exc).__name__}）') from None
    return result


@app.get('/api/tasks/{task_id}/artifact')
async def artifact(task_id: str,
                   principal=Depends(policies.current_principal)):
    """下载/预览任务产物。

    文件路径以任务 state 里记的 `artifact.html_path` 为准——报告是 index.html，
    "资料转 HTML" 产出的是 sources.html；早先这里写死 index.html，导致资料页 404。
    """
    record = await _owned_task(task_id, principal)
    meta = (record.state or {}).get('artifact') or {}
    recorded = meta.get('path') or meta.get('html_path')
    if recorded:
        path = Path(recorded)
    elif record.artifact_id:
        # 旧任务没有记录路径，按约定回退
        path = settings.artifact_dir / record.artifact_id / 'index.html'
    else:
        raise HTTPException(status_code=404, detail='产物不存在')
    if not path.exists():
        raise HTTPException(status_code=404, detail='产物不存在')
    # 媒体类型别写死 text/html：视频产物那样会被浏览器当成网页而放不出来。
    # 优先级：产物自己声明的 mime → 按扩展名推断 → 兜底二进制流。
    media_type = (meta.get('mime')
                  or mimetypes.guess_type(path.name)[0]
                  or 'application/octet-stream')
    inline = meta.get('kind') in {'video', 'audio'} or media_type.startswith(
        ('video/', 'audio/', 'text/html'))
    return FileResponse(path, media_type=media_type, filename=path.name,
                        content_disposition_type='inline' if inline else 'attachment')


@app.get('/api/runs/{run_id}')
async def read_run(run_id: str,
                   principal=Depends(policies.current_principal)):
    """run 的状态、计划与步骤（文档 §13）。"""
    run = await _owned_run(run_id, principal)
    plan = await hub.store.plan(run_id) or {}
    return {'run': run, 'plan': plan,
            'steps': await hub.store.steps_of(run_id),
            'actions': [a for a in (await hub.store.actions_of(run_id))]}


@app.post('/api/runs/{run_id}/pause')
async def pause_run(run_id: str,
                    principal=Depends(policies.current_principal)):
    await _owned_run(run_id, principal)
    if not await hub.pause(run_id):
        raise HTTPException(status_code=409, detail='该运行不在可暂停状态')
    return {'paused': True}


@app.post('/api/runs/{run_id}/resume')
async def resume_run(run_id: str,
                     principal=Depends(policies.current_principal)):
    await _owned_run(run_id, principal)
    record = await hub.resume(run_id)
    if record is None:
        raise HTTPException(status_code=409, detail='没有可续跑的计划')
    return {'resumed': True, 'task_id': record.task_id, 'run_id': record.run_id}


@app.post('/api/runs/{run_id}/compensate')
async def compensate_run(run_id: str,
                         principal=Depends(policies.current_principal)):
    """撤销这次运行**已经完成**的有副作用步骤（文档 §8.4）。

    撤销是一次独立执行：新建 run、走账本、发事件，原 run 只记 `compensated_at`。
    取消不会撤销已经发出去的写操作，要收回副作用就走这里。
    """
    await _owned_run(run_id, principal)
    try:
        result = await hub.compensate(run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if result is None:
        raise HTTPException(status_code=409, detail='没有可补偿的步骤')
    return {'compensated': True, **result}


@app.post('/api/runs/{run_id}/cancel')
async def cancel_run(run_id: str,
                     principal=Depends(policies.current_principal)):
    """取消：停止后续步骤，回执说明在途动作状态（文档 §8.4）。

    跨进程取消靠库里的 cancel_requested_at，执行者轮询到才会真的停。所以这里
    只承诺"请求已送达"，不再无条件把 run 标成 cancelled 假装成功。
    """
    await _owned_run(run_id, principal)
    if not await hub.cancel_run(run_id):
        raise HTTPException(status_code=409, detail='该运行已经结束，无法取消')
    return {'cancelled': True,
            'detail': '取消请求已发出；在途动作的回执仍会记录，不会重复发送'}


@app.get('/api/artifacts/{artifact_id}')
async def read_artifact(artifact_id: str,
                        principal=Depends(policies.current_principal)):
    """按引用读取产物元信息（文档 §13）。

    文件内容仍走任务产物下载接口；这里给的是"这个东西是什么、多大、
    哈希多少"的权威记录，便于界面与审计引用。
    """
    meta = await hub.store.artifact(artifact_id)
    if meta is None:
        raise HTTPException(status_code=404, detail='产物不存在')
    run = await hub.store.run(meta['run_id']) or {}
    policies.ensure_owner(
        {'tenant_id': run.get('tenant_id', 'default'),
         'owner_user_id': run.get('owner_user_id', 'default')}, principal, '产物')
    return meta


@app.get('/api/recovery')
async def recovery_status():
    return getattr(app.state, 'recovery', {})


@app.get('/api/health')
async def health():
    """存活与态势：队列积压、待核实动作、失败作业都能看到。

    没有这个口子，上面那些状态只能手工查库——排障成本高到没人会去查。
    """
    try:
        stats = await hub.store.stats()
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(status_code=503,
                            detail=f'数据库不可用（{type(exc).__name__}）') from None
    body = {'ok': True, 'owner': hub.owner_id,
            'recovery': getattr(app.state, 'recovery', {}),
            'mcp': tool_module.mcp_status(), **stats}
    pool_stats = getattr(hub.store, 'pool_stats', None)
    if callable(pool_stats):
        # 连接池态势：只有 PostgreSQL 实现有池，SQLite 没有这个口子
        try:
            body['db_pool'] = pool_stats()
        except Exception:                        # noqa: BLE001
            body['db_pool'] = {'error': '取连接池状态失败'}
    return body


@app.get('/api/profile')
async def read_profile(principal=Depends(policies.current_principal)):
    """当前的用户画像：原文 + 解析后的事实 + 版本号。"""
    with _profile_scope(principal):
        return {'text': profile.raw(), 'facts': profile.load(),
                'version': profile.version()}


@app.put('/api/profile')
async def write_profile(payload: ProfileRequest,
                        principal=Depends(policies.current_principal)):
    """整体覆盖画像。只解析"键：值"行，头部元信息由服务端重写。"""
    with _profile_scope(principal):
        facts = profile.parse(payload.text)
        version = profile.save(facts)
    return {'version': version, 'facts': facts, 'count': len(facts)}


@app.delete('/api/profile')
async def delete_profile(principal=Depends(policies.current_principal)):
    with _profile_scope(principal):
        profile.clear()
    return {'cleared': True}


@app.get('/api/sessions/{session_id}/memory')
async def read_memory(session_id: str,
                      principal=Depends(policies.current_principal)):
    """会话记忆：更早对话的压缩结果，可读可改。"""
    await _owned_session(session_id, principal)
    state = await hub.store.memory(session_id)
    return {'summary': state['summary'], 'facts': state['facts'],
            'facts_text': memory_module.render_facts(state['facts']),
            'upto': state['upto']}


@app.put('/api/sessions/{session_id}/memory')
async def write_memory(session_id: str, payload: MemoryRequest,
                       principal=Depends(policies.current_principal)):
    """手动改写会话记忆（摘要 + 关键事实）。"""
    await _owned_session(session_id, principal)
    facts = memory_module.parse_facts(payload.facts)
    current = await hub.store.memory(session_id)
    upto = current['upto']
    await hub.store.set_memory(session_id, summary=payload.summary, facts=facts,
                               upto=upto)
    return {'summary': payload.summary, 'facts': facts, 'upto': upto}


@app.delete('/api/sessions/{session_id}/memory')
async def delete_memory(session_id: str,
                        principal=Depends(policies.current_principal)):
    """清空会话记忆，下轮会从原文重新开始累积。"""
    await _owned_session(session_id, principal)
    await hub.store.clear_memory(session_id)
    return {'cleared': True}


@app.get('/api/sessions')
async def list_sessions(principal=Depends(policies.current_principal)):
    """会话列表（侧边栏用）：按最近活跃排序，含标题与消息数。"""
    return {'sessions': await hub.list_sessions(principal=principal)}


@app.get('/api/sessions/{session_id}/messages')
async def session_messages(session_id: str,
                           principal=Depends(policies.current_principal)):
    await _owned_session(session_id, principal)
    return {'session_id': session_id, 'messages': await hub.history(session_id)}


@app.get('/api/sessions/{session_id}/timeline')
async def session_timeline(session_id: str,
                           principal=Depends(policies.current_principal)):
    """会话回放：消息气泡 + 每轮执行过程事件，供切换会话时重建步骤面板。"""
    await _owned_session(session_id, principal)
    return await hub.session_timeline(session_id)
