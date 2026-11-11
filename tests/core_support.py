"""从 `tests/test_core.py` 拆出来的共用夹具。

`unittest` 按 `test*.py` 发现用例，这个模块不会被当成测试文件加载。
不要放进 `conftest.py`：本仓库用的是 `unittest`，不是 pytest。
"""
import os
import unittest

from contextlib import ExitStack
from unittest.mock import patch

from tests.exec_env import valid_test_dsn

# 项目只支持 PostgreSQL，测试也跟着只跑 PG：没有 PG_TEST_DSN 就逐条跳过。
PG_DSN = os.getenv('PG_TEST_DSN', '')
TEST_TABLES = ('scheduled_task_runs', 'scheduled_tasks',
               'memory_tombstones', 'memories', 'background_jobs', 'artifacts',
               'action_attempts', 'actions', 'steps', 'plans', 'run_snapshots',
               'run_inputs', 'run_input_processing', 'run_decisions',
               'runs', 'tasks_meta', 'events', 'tasks', 'session_memory',
               'session_summaries', 'messages', 'sessions', 'request_keys',
               'experts')


def new_store():
    """测试用仓储：真实 PostgreSQL（库名必须含 test，用例会 TRUNCATE）。"""
    if not PG_DSN:
        raise unittest.SkipTest('需要 PG_TEST_DSN（项目已不再支持 SQLite）')
    if not valid_test_dsn(PG_DSN):
        raise unittest.SkipTest('PG_TEST_DSN 必须指向一次性测试库（库名含 test）')
    from webpage_agent.store.pg_store import PostgresStore
    return PostgresStore(PG_DSN)


async def reset_db() -> None:
    """清空全部业务表，让每个用例互不影响（以前靠 SQLite :memory: 隔离）。"""
    store = new_store()
    try:
        async with store._lease() as conn:
            for table in TEST_TABLES:
                await conn.execute(f'TRUNCATE TABLE {table} CASCADE')
    finally:
        await store.close()


def patch_all_emits(sink):
    """能力包各自持有 emit 引用，测试里需要一起打桩。"""
    from webpage_agent.capabilities import answers, export, publishing, web
    stack = ExitStack()
    for module in (answers, export, publishing, web):
        stack.enter_context(patch.object(module, 'emit', sink))
    return stack
