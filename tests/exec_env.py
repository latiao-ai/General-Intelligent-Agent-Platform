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
import sys
import tempfile

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from webpage_agent.core.config import settings

PG_DSN = os.getenv('PG_TEST_DSN', '')

# 测试默认不往仓库真实的 artifacts/ 和 profile/ 目录里写：整轮测试共用一个临时目录。
# 跑真实执行链的用例（seed_run + run_worker / api_hub）会落 state.json、
# results/ 和产出文件；走 HTTP 的用例（如 test_auth_isolation）会落用户画像。
# 不隔离就会在仓库里越攒越多。需要自己目录的用例仍然可以用
# override_settings(...) 或 object.__setattr__ 覆盖。
_SESSION_DATA = tempfile.TemporaryDirectory(prefix='agent-test-data-')
object.__setattr__(settings, 'artifact_dir', Path(_SESSION_DATA.name) / 'artifacts')
object.__setattr__(settings, 'profile_path', Path(_SESSION_DATA.name) / 'profile' / 'USER.md')

TEST_TABLES = ('scheduled_task_runs', 'scheduled_tasks',
               'memory_tombstones', 'memories', 'background_jobs', 'artifacts',
               'action_attempts', 'actions', 'steps', 'plans', 'run_snapshots',
               'run_inputs', 'run_input_processing', 'run_decisions',
               'runs', 'tasks_meta', 'events', 'tasks', 'session_memory',
               'session_summaries', 'messages', 'sessions', 'request_keys',
               'external_tasks', 'tool_result_manifests', 'tool_result_parts',
               'tool_authorizations', 'approvals', 'approval_reservations',
               'run_decisions', 'provider_charges', 'session_turns', 'experts')


def valid_test_dsn(dsn):
    from psycopg.conninfo import conninfo_to_dict
    try:
        return 'test' in conninfo_to_dict(dsn).get('dbname', '').lower()
    except Exception:
        return False


def new_store():
    """测试用仓储：真实 PostgreSQL（库名必须含 test，用例会 TRUNCATE）。"""
    import unittest
    if not PG_DSN:
        raise unittest.SkipTest('需要 PG_TEST_DSN（项目已不再支持 SQLite）')
    if not valid_test_dsn(PG_DSN):
        raise unittest.SkipTest('PG_TEST_DSN 必须指向一次性测试库（库名含 test）')
    from webpage_agent.store.pg_store import PostgresStore
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
        from webpage_agent.tools.core import Tool

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
    """把工具查找换成受控实现。

    连 `catalog()` 一起替换：修订计划时会用它判断"工具是否存在"，
    只换 `find_tool` 会让修订器以为工具清单是空的。
    """
    from webpage_agent.tools import core as tool_module

    async def fake_find(name, context=None):
        return tools_by_name.get(name)

    async def fake_catalog(context=None):
        return list(tools_by_name.values())

    stack = ExitStack()
    stack.enter_context(patch.object(tool_module, 'find_tool', fake_find))
    stack.enter_context(patch.object(tool_module, 'catalog', fake_catalog))
    return stack


class _InstalledPlan:
    """同时换掉规划节点，并把这次执行的运行时指到进程内节点循环。

    生产 `get_runtime` 只返回 Pi。这些用例要的是「计划提交、暂停、租约」而不是
    Node 里的模型，所以只在这个 with 块里把 `run_task` 用的工厂换成测试驱动。
    """

    def __init__(self, fake, calls):
        self._fake = fake
        self.calls = calls
        self._stack = None

    def __enter__(self):
        from webpage_agent.agent import nodes
        from webpage_agent.api import main as main_module
        from tests.host_loop import HostLoopRuntime
        self._stack = ExitStack()
        self._stack.enter_context(patch.object(nodes, 'plan', self._fake))
        self._stack.enter_context(patch.object(
            main_module, 'get_runtime', lambda kind='': HostLoopRuntime()))
        return self

    def __exit__(self, *exc):
        if self._stack is not None:
            return self._stack.__exit__(*exc)
        return False


def install_host_loop():
    """让 `run_task` 走进程内规划节点，而不是启动 Pi。"""
    from webpage_agent.api import main as main_module
    from tests.host_loop import HostLoopRuntime
    return patch.object(main_module, 'get_runtime', lambda kind='': HostLoopRuntime())


def install_plan(steps: list[dict], summary: str = '测试计划'):
    """替换规划节点：计划内容由用例决定，不调模型。

    返回 (patch, 记录表)。记录表里每次调用追加一条执行前的输入，用来断言
    "续跑不该重新规划"。with 块内 `run_task` 走进程内节点循环，不启动 Pi。
    """
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
        # 计划必须带上"依据的要求版本"：准入与计划提交都靠它做 CAS
        return {'plan': {'steps': steps, 'summary': summary, 'version': 1,
                         'intent_version': int(state.get('intent_version') or 0)},
                'step_index': 0, 'status': 'planned'}

    return _InstalledPlan(fake_plan, calls), calls


def install_main(artifact_dir):
    """让**真实** `run_task` 能在测试里跑：不需要真 key，产物写临时目录。

    这里不伪造 run_task 本身——图、节点、账本、租约都是真的，只有模型与工具
    被换掉。否则测出来的只是"我的假函数能被调用"。
    """
    from webpage_agent.api import main as main_module
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


def override_settings(**changes):
    """临时改配置，**对所有直接 import 了 settings 的模块都生效**。

    踩过的坑：`from webpage_agent.core.config import settings` 在模块里是持有引用的，只
    `patch('webpage_agent.core.config.settings')` 对它们完全没用——测试会"改了个寂寞"，
    然后拿着默认值跑出个看似合理的错误结论。
    """
    from dataclasses import replace
    from unittest.mock import patch
    from webpage_agent.core.config import settings as real_settings

    fake = replace(real_settings, **changes)
    modules = ('webpage_agent.api.routes', 'webpage_agent.api.api_hub', 'webpage_agent.api.main', 'webpage_agent.agent.nodes', 'webpage_agent.agent.decide', 'webpage_agent.agent.plan_validate', 'webpage_agent.agent.refs', 'webpage_agent.agent.pi_runtime', 'webpage_agent.core.services',
               'webpage_agent.tools.core', 'webpage_agent.agent.context', 'webpage_agent.core.memory', 'webpage_agent.capabilities.base',
               'webpage_agent.capabilities.web', 'webpage_agent.capabilities.export',
               'webpage_agent.scheduler.tick', 'webpage_agent.scheduler.delivery',
               'webpage_agent.api.schedules_api', 'webpage_agent.integrations.webhooks',
               'webpage_agent.capabilities.answers', 'webpage_agent.capabilities.publishing',
               'webpage_agent.core.sources', 'webpage_agent.api.auth', 'webpage_agent.governance.policies', 'webpage_agent.core.files',
               'webpage_agent.observe.observation', 'webpage_agent.agent.run_inputs', 'webpage_agent.integrations.mcp_servers',
               'webpage_agent.integrations.mcp_adapter', 'webpage_agent.core.profile', 'webpage_agent.store.idempotency')
    stack = ExitStack()
    stack.enter_context(patch('webpage_agent.core.config.settings', fake))
    for name in modules:
        module = sys.modules.get(name)
        if module is not None and hasattr(module, 'settings'):
            stack.enter_context(patch.object(module, 'settings', fake))
    return stack
