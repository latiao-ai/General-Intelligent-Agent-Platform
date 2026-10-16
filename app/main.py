import argparse
import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path
from app.config import settings, check_runtime_keys
from app.events import STAGE_LABELS  # noqa: F401  (对外仍从 app.main 可用)
from app.graph import build_graph
from app.mcp_adapter import deploy_html
from app.rendering import save_json


async def deploy_task(task_id: str) -> dict:
    """部署已经生成好的 HTML：不重新搜索、不调用模型。

    供 Streamlit 部署按钮与 scripts.retry_deploy 共用。
    """
    if not settings.deploy_enabled:
        raise RuntimeError('DEPLOY_ENABLED=false，部署已关闭；请在 .env 打开后重启进程')
    if not task_id.isalnum():
        raise ValueError('task_id 只能包含字母数字')
    folder = settings.artifact_dir / task_id
    artifact = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    actual = hashlib.sha256(Path(artifact['html_path']).read_bytes()).hexdigest()
    if actual != artifact['sha256']:
        raise ValueError('HTML 已修改，请重新生成 manifest 后再部署')
    result = await deploy_html(artifact)
    save_json(folder / 'deployment.json', result)
    return result


async def run_task(text: str, progress=None, on_event=None, history=None, on_start=None):
    """跑一次任务。

    progress(node, status, elapsed)  节点级进度
    on_event(event)                  节点内部的细粒度事件（搜索了什么、命中多少）
    history                          多轮对话历史，供解析阶段还原指代型追加要求
    on_start(task_id)                任务创建后立刻回调，便于外部记录任务 ID
    """
    check_runtime_keys()
    if not text.strip():
        raise ValueError('请输入主题或 URL')
    if len(text) > 10000:
        raise ValueError('首版输入限制为 10000 字符')
    task_id = uuid.uuid4().hex
    folder = settings.artifact_dir / task_id
    folder.mkdir(parents=True, exist_ok=True)
    if on_start:
        # 早点把 task_id 交出去，外部才能在任务被取消后定位已生成的产物。
        on_start(task_id)
    state = {
        'task_id': task_id, 'user_input': text,
        'documents': [], 'errors': [], 'usage': [],
        'research_round': 0, 'searched': False, 'status': 'created',
        'timings': [], 'history': list(history or []),
    }
    started = time.perf_counter()
    marked = started
    try:
        async with asyncio.timeout(300):
            async for mode, chunk in build_graph().astream(
                state, config={'recursion_limit': 30},
                stream_mode=['updates', 'custom'],
            ):
                if mode == 'custom':
                    # 节点内部发出的动作事件，直接透传给调用方。
                    if on_event:
                        on_event(chunk)
                    continue
                for name, update in chunk.items():
                    now = time.perf_counter()
                    if isinstance(update, dict):
                        state.update(update)
                    # stream_mode='updates' 在该节点跑完后才产出事件，
                    # 所以相邻事件的时间差≈该节点耗时（含少量调度开销）。
                    state['timings'].append({
                        'stage': name,
                        'seconds': round(now - marked, 2),
                        'status': state.get('status'),
                    })
                    marked = now
                    if progress:
                        progress(name, state.get('status'), round(now - started, 2))
                    save_json(folder / 'state.json', state)
    except Exception as exc:
        state['status'] = 'failed'
        state['errors'].append({'stage': 'runtime', 'type': type(exc).__name__})
        # 不打印原始 SDK 异常字符串；使用任务 ID 与错误类别定位。
        raise RuntimeError(f'任务 {task_id} 失败：{type(exc).__name__}') from None
    finally:
        state['elapsed_seconds'] = round(time.perf_counter() - started, 2)
        save_json(folder / 'state.json', state)
    return state


def format_timings(timings) -> list[str]:
    """把计时记录格式化成人类可读的文本行（CLI 与前端共用标签）。"""
    lines = []
    for item in timings or []:
        label = STAGE_LABELS.get(item.get('stage'), item.get('stage'))
        lines.append(
            f"  {str(label):<8} {float(item.get('seconds', 0)):>7.2f}s  {item.get('status')}"
        )
    return lines


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='研究主题或包含 URL 的描述')
    args = parser.parse_args()
    state = await run_task(
        args.input,
        lambda node, status, elapsed: print(
            f'[{elapsed:7.2f}s] {STAGE_LABELS.get(node, node)}: {status}'),
    )
    print('任务 ID:', state['task_id'])
    print('最终状态:', state['status'])
    if state.get('artifact'):
        print('HTML:', state['artifact']['html_path'])
    if state.get('deployment', {}).get('public_url'):
        print('访问地址:', state['deployment']['public_url'])
    print('各阶段耗时：')
    for line in format_timings(state.get('timings')):
        print(line)
    print('总耗时:', state.get('elapsed_seconds'), '秒')
    if state['status'] in {'failed', 'deploy_needs_check'}:
        print('请查看对应任务的 state.json 和服务端日志。')
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
