"""执行可靠性测试的公共夹具（文档 §8）。

原则：

- **真实 PostgreSQL**：租约、代次、动作准入、快照这些东西只有真的跑在 PG 上
  才有意义（`FOR UPDATE SKIP LOCKED`、条件更新、事务边界都没法假）；
- **假模型、假工具**：规划节点与工具调用全部替换成受控实现，不碰网络、不花钱；
- **受控时点**：用 `asyncio.Event` 固定"已进入工具/已准入"的瞬间，不靠 sleep
  猜时序。

`PG_TEST_DSN` 必须指向一次性测试库（库名含 test），因为 `reset_db()` 会 TRUNCATE。
"""
import asyncio
import os

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PG_DSN = os.getenv('PG_TEST_DSN', '')

TEST_TABLES = ('memory_tombstones', 'memories', 'background_jobs', 'artifacts',
               'action_attempts', 'actions', 'steps', 'plans', 'run_snapshots',
               'runs', 'tasks_meta', 'events', 'tasks', 'session_memory',
               'session_summaries', 'messages', 'sessions', 'request_keys')


def new_store():
    """测试用仓储：真实 PostgreSQL（库名必须含 test，用例会 TRUNCATE）。"""
    import unittest
    if not PG_DSN:
        raise unittest.SkipTest('需要 PG_TEST_DSN（项目已不再支持 SQLite）')
    if 'test' not in PG_DSN:
        raise unittest.SkipTest('PG_TEST_DSN 必须指向一次性测试库（库名含 test）')
    from app.pg_store import PostgresStore
    return PostgresStore(PG_DSN)


async def reset_db() -> None:
    """清空全部业务表，让每个用例互不影响。"""
    store = new_store()
    try:
        async with store._lease() as conn:
            for table in TEST_TABLES:
                await conn.execute(f'TRUNCATE TABLE {table} CASCADE')
    finally:
        await store.close()


async def seed_run(store, *, task_id: str = 'task1', run_id: str = 'run1',
                   session_id: str = 'sess1', status: str = 'queued',
                   message: str = '做个页面') -> None:
    """建任务 + 运行行：执行入口会去库里读它们，不能只建一半。"""
    await store.create_task(task_id, session_id, message)
    await store.ensure_task_meta(task_id, session_id=session_id, goal=message)
    await store.create_run(run_id, task_id, status=status)


class Remote:
    """假远端：真实记录**调用次数**（恢复类断言的唯一可信来源）。

    `gate` 用来固定时点：用例可以在工具"已进入但还没返回"的瞬间做别的动作
    （提交暂停、抢租约、杀进程），而不是 sleep 一会儿赌时序。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.entered: dict[str, asyncio.Event] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.failures: dict[str, BaseException] = {}
        self.outputs: dict[str, dict] = {}

    def name_count(self, name: str) -> int:
        return len([c for c in self.calls if c[0] == name])

    def enter(self, name: str) -> asyncio.Event:
        """返回"该工具已进入"的事件。"""
        return self.entered.setdefault(name, asyncio.Event())

    def gate(self, name: str) -> asyncio.Event:
        """返回"允许该工具继续"的事件；用例负责 set 它。"""
        return self.gates.setdefault(name, asyncio.Event())

    def tool(self, name: str, *, effect: str = 'write',
             tool_key: str = '', output: dict | None = None,
             label: str = ''):
        from app.tools import Tool

        async def run(args, state):
            self.calls.append((name, dict(args or {})))
            self.enter(name).set()
            gate = self.gates.get(name)
            if gate is not None:
                await gate.wait()
            failure = self.failures.get(name)
            if failure is not None:
                raise failure
            return dict(output or self.outputs.get(name) or {'answer': '好了'})

        return Tool(name, label or name, '测试工具', {'type': 'object'}, run,
                    tool_key=tool_key or f'local:test:{name}', effect=effect)


def install_tools(tools_by_name: dict):
    """把工具查找换成受控实现。返回一个 ExitStack 上下文。"""
    from app import tools as tool_module

    async def fake_find(name):
        return tools_by_name.get(name)

    return patch.object(tool_module, 'find_tool', fake_find)


def install_plan(steps: list[dict], summary: str = '测试计划'):
    """替换规划节点：计划内容由用例决定，不调模型。

    返回 (patch, 记录表)。记录表里每次调用追加一条执行前的输入，用来断言
    "续跑不该重新规划"。
    """
    from app import nodes
    calls: list[str] = []

    async def fake_plan(state):
        resumed = state.get('resume_plan') or {}
        if isinstance(resumed, dict) and resumed.get('steps'):
            # 与真实 plan 节点一致：有已提交计划就沿用，绝不再规划一次
            calls.append('沿用已保存的计划')
            return {'plan': {'steps': resumed['steps'],
                             'summary': resumed.get('summary') or '续跑'},
                    'step_index': int(resumed.get('step_index') or 0),
                    'status': 'planned'}
        calls.append(state.get('user_input') or '')
        return {'plan': {'steps': steps, 'summary': summary},
                'step_index': 0, 'status': 'planned'}

    return patch.object(nodes, 'plan', fake_plan), calls


def install_main(artifact_dir):
    """让**真实** `run_task` 能在测试里跑：不需要真 key，产物写临时目录。

    这里不伪造 run_task 本身——图、节点、账本、租约都是真的，只有模型与工具
    被换掉。否则测出来的只是"我的假函数能被调用"。
    """
    from app import main as main_module
    fake_settings = SimpleNamespace(
        artifact_dir=Path(artifact_dir), task_timeout=30.0,
        graph_recursion_limit=60, deploy_enabled=False)
    stack = ExitStack()
    stack.enter_context(patch.object(main_module, 'check_runtime_keys',
                                     lambda: None))
    stack.enter_context(patch.object(main_module, 'settings', fake_settings))
    return stack


def one_step_plan(tool: str = 'deploy', tool_key: str = '',
                  effect: str = 'write', step_id: str = 's1',
                  args: dict | None = None) -> list[dict]:
    step = {'tool': tool, 'step_id': step_id, 'args': args or {},
            'depends_on': []}
    if tool_key:
        step['tool_key'] = tool_key
    if effect:
        step['effect'] = effect
    return [step]
