"""正式入口 Pi 进程恢复辅助：经 Hub.execute_claimed_run 跑 Pi（受控模型 + 受控工具），
在工具结算后、Node 吸收结果前自我终止（模拟崩溃）。

用法：
    python -m tests.hub_pi_crash_worker <dsn> <run_id> <task_id> <session_id>

受控模型（PI_MOCK_SCRIPT）产出「一个工具调用 + 一个答复」；受控工具
（PI_MOCK_TOOL）走完整受治理结算链路；工具结算后由 PI_CRASH_AFTER_SETTLE
钩子触发 os._exit(0)，模拟进程在「工具已结算、Node 尚未吸收、run 未收尾」时
被杀。父进程随后用新执行者经正式入口接管。
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
    # task / run 已由父进程创建（runtime_kind='pi'），这里只 claim + 执行。

    os.environ['PI_MOCK_SCRIPT'] = json.dumps([
        {'type': 'tool', 'calls': [
            {'id': 'c1', 'name': 'web_search', 'arguments': {'query': '天气'}}]},
        {'type': 'answer', 'text': '结果已拿到'},
    ])
    os.environ['PI_MOCK_TOOL'] = '1'
    os.environ['PI_CRASH_AFTER_SETTLE'] = '1'

    hub = Hub(store, owner_id='crash-pi-proc')
    claimed = await store.claim_run('crash-pi-proc', 120, run_id=run_id)
    assert claimed is not None, 'claim_run 失败'

    try:
        await hub.execute_claimed_run(claimed)
    except SystemExit:
        raise
    except Exception:
        # 崩溃点由 PI_CRASH_AFTER_SETTLE 的 os._exit 触发；这里兜底不掩盖崩溃语义
        pass

    await store.close()
    os._exit(0)


if __name__ == '__main__':
    dsn, run_id, task_id, session_id = sys.argv[1:5]
    asyncio.run(main(dsn, run_id, task_id, session_id))
