"""故障注入辅助：真实子进程结算一个动作后，在收尾前自我终止（模拟崩溃）。

用法：`python -m tests.crash_worker <dsn> <run_id> <task_id> <session_id> <step_id>`
子进程：claim 租约 → 准入并结算一个动作 → 写快照 → 不释放租约、不收尾，
直接 `os._exit(0)` 退出（模拟进程被杀/崩溃）。父进程随后用新执行者恢复。
"""
import asyncio
import os
import sys


async def main(dsn: str, run_id: str, task_id: str, session_id: str,
               step_id: str) -> None:
    from webpage_agent.store.pg_store import PostgresStore
    from webpage_agent.governance import execution_control
    from webpage_agent.store import ledger as ledger_module

    store = PostgresStore(dsn)
    await store.ready()
    await store.create_task(task_id, session_id, '测试')
    await store.ensure_task_meta(task_id, session_id=session_id, goal='测试')
    await store.create_run(run_id, task_id, status='queued')

    claimed = await store.claim_run('crash-proc', 120, run_id=run_id)
    ctx = execution_control.ExecutionContext.from_claim(claimed)
    book = ledger_module.ActionLedger(store, run_id, 1, context=ctx)
    token = ledger_module.bind(book)
    ctoken = execution_control.bind(ctx)
    try:
        admitted = await book.admit(
            step_id=step_id, tool_key='local:web:search', effect='read',
            args={'query': 'x'})
        assert admitted['status'] == 'admitted', admitted
        await book.settle(
            step_id=step_id, action_id=admitted['action_id'],
            attempt_id=admitted['attempt_id'], status='succeeded',
            receipt={'documents': [{'source_id': 's1', 'url': 'https://x',
                                    'title': 't', 'chars': 5}]},
            step_status='succeeded')
    finally:
        ledger_module.unbind(token)
        execution_control.unbind(ctoken)

    # 崩溃：不释放租约、不收尾，直接退出（模拟进程被杀）
    await store.close()
    os._exit(0)


if __name__ == '__main__':
    dsn, run_id, task_id, session_id, step_id = sys.argv[1:6]
    asyncio.run(main(dsn, run_id, task_id, session_id, step_id))
