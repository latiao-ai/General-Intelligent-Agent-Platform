"""第八阶段收口第 10 点：正式入口异步 accepted 后自我终止（模拟崩溃）。

受控异步工具（PI_MOCK_ASYNC=1）让 web_search 走「异步受理」回执；Pi 在
accepted 落库后、Node 尚未退出前由 PI_CRASH_AFTER_ACCEPTED 钩子触发 os._exit(0)，
模拟进程在「accepted 已提交、外部任务已登记、run 未收尾」时被杀。

用法：
    python -m tests.hub_async_crash_worker <dsn> <run_id> <task_id> <session_id>
"""
import asyncio
import json
import os
import sys


async def main(dsn: str, run_id: str, task_id: str, session_id: str) -> None:
    from webpage_agent.store.pg_store import PostgresStore
    from webpage_agent.api.api_hub import Hub

    store = PostgresStore(dsn)
    await store.ready()

    os.environ['PI_MOCK_SCRIPT'] = json.dumps([
        {'type': 'tool', 'calls': [
            {'id': 'c1', 'name': 'web_search', 'arguments': {'query': '天气'}}]},
        {'type': 'answer', 'text': '结果已拿到'},
    ])
    os.environ['PI_MOCK_ASYNC'] = '1'
    os.environ['PI_MOCK_REMOTE_ID'] = 'job-9'
    os.environ['PI_CRASH_AFTER_ACCEPTED'] = '1'

    hub = Hub(store, owner_id='crash-async-proc')
    claimed = await store.claim_run('crash-async-proc', 120, run_id=run_id)
    assert claimed is not None, 'claim_run 失败'

    try:
        await hub.execute_claimed_run(claimed)
    except SystemExit:
        raise
    except Exception:
        pass

    await store.close()
    os._exit(0)


if __name__ == '__main__':
    dsn, run_id, task_id, session_id = sys.argv[1:5]
    asyncio.run(main(dsn, run_id, task_id, session_id))
