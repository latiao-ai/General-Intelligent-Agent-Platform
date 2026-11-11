"""标准 MCP Tasks 原语的接入测试。

覆盖三件事：

1. **纯映射**：`taskSupport` 识别、TaskStatus → 中台状态、受理增量形状；
2. **通道切换**：声明 `required` 的工具走 `call_tool_as_task`，其余**仍然**
   走 `call_tool`（这是"不产生新 bug"的核心断言——默认服务端一个都没声明）；
3. **轮询回路**：协议任务用 `tasks/get` 查询，状态推进与落盘和合同路线一致。
"""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from webpage_agent.core import async_contracts
from webpage_agent.core import external_tasks
from webpage_agent.core import mcp_connections
from webpage_agent.tools import core as tools
from webpage_agent.core.config import settings
from webpage_agent.integrations import mcp_adapter, mcp_tasks


@contextmanager
def assigned(target, **fields):
    """临时改写冻结 dataclass 的字段（测试专用）。"""
    saved = {name: getattr(target, name) for name in fields}
    for name, value in fields.items():
        object.__setattr__(target, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            object.__setattr__(target, name, value)


class TaskSupportParsingTests(unittest.TestCase):
    def test_support_of_reads_discovery_shapes(self):
        self.assertEqual(mcp_tasks.support_of({'task_support': 'required'}), 'required')
        self.assertEqual(
            mcp_tasks.support_of({'execution': {'taskSupport': 'optional'}}), 'optional')
        # 缺省与非法值都必须是 forbidden —— 协议默认值，绝不猜成"支持任务"
        self.assertEqual(mcp_tasks.support_of({}), 'forbidden')
        self.assertEqual(mcp_tasks.support_of({'task_support': 'maybe'}), 'forbidden')
        self.assertEqual(mcp_tasks.support_of(None), 'forbidden')

    def test_adapter_reads_sdk_tool_execution(self):
        class Execution:
            taskSupport = 'required'

        class Tool:
            execution = Execution()

        class Bare:
            execution = None

        self.assertEqual(mcp_adapter.task_support_of(Tool()), 'required')
        self.assertEqual(mcp_adapter.task_support_of(Bare()), 'forbidden')
        self.assertEqual(mcp_adapter.task_support_of(object()), 'forbidden')


class WantsTaskTests(unittest.TestCase):
    def test_required_always_uses_task_channel(self):
        with assigned(settings, mcp_tasks_enabled=True, mcp_tasks_optional_async=False):
            self.assertTrue(mcp_tasks.wants_task('required'))

    def test_optional_is_off_by_default(self):
        with assigned(settings, mcp_tasks_enabled=True, mcp_tasks_optional_async=False):
            self.assertFalse(mcp_tasks.wants_task('optional'))

    def test_optional_can_be_opted_in(self):
        with assigned(settings, mcp_tasks_enabled=True, mcp_tasks_optional_async=True):
            self.assertTrue(mcp_tasks.wants_task('optional'))

    def test_master_switch_and_forbidden(self):
        with assigned(settings, mcp_tasks_enabled=False, mcp_tasks_optional_async=True):
            self.assertFalse(mcp_tasks.wants_task('required'))
            self.assertFalse(mcp_tasks.wants_task('optional'))
        with assigned(settings, mcp_tasks_enabled=True):
            self.assertFalse(mcp_tasks.wants_task('forbidden'))
            self.assertFalse(mcp_tasks.wants_task(''))


class StatusMappingTests(unittest.TestCase):
    def test_protocol_statuses_fold_to_platform_vocabulary(self):
        expected = {
            'working': 'running',
            'input_required': 'accepted',   # 我们没有交互通道，留在"未结束"
            'completed': 'succeeded',
            'failed': 'failed',
            'cancelled': 'cancelled',
        }
        for raw, want in expected.items():
            with self.subTest(status=raw):
                self.assertEqual(mcp_tasks.map_status(raw), want)

    def test_unknown_status_never_becomes_success(self):
        for raw in ('mystery', '', None, 42):
            with self.subTest(status=raw):
                self.assertEqual(mcp_tasks.map_status(raw), 'unknown')


class AcceptanceTests(unittest.TestCase):
    def test_accepted_task_is_marked_not_succeeded(self):
        with assigned(settings, async_tool_runtime_enabled=True):
            got = mcp_tasks.acceptance(
                {'taskId': 't-1', 'status': 'working', 'pollInterval': 5})
        self.assertTrue(got['async_accepted'])
        self.assertEqual(got['remote_task_id'], 't-1')
        self.assertEqual(got['contract_id'], mcp_tasks.PROTOCOL_CONTRACT_ID)
        # 关键：不是 succeeded。提交即"已受理"，等 tasks/get 再定论。
        self.assertEqual(got['execution_status'], 'accepted')

    def test_terminal_or_missing_task_is_not_registered_as_waiting(self):
        with assigned(settings, async_tool_runtime_enabled=True):
            self.assertEqual(mcp_tasks.acceptance({'taskId': 't', 'status': 'completed'}), {})
            self.assertEqual(mcp_tasks.acceptance({'taskId': 't', 'status': 'failed'}), {})
            self.assertEqual(mcp_tasks.acceptance({'status': 'working'}), {})
            self.assertEqual(mcp_tasks.acceptance(None), {})

    def test_disabled_async_runtime_returns_nothing(self):
        with assigned(settings, async_tool_runtime_enabled=False):
            self.assertEqual(mcp_tasks.acceptance({'taskId': 't', 'status': 'working'}), {})


class ProtocolContractTests(unittest.TestCase):
    def test_reserved_identity_resolves_without_a_file(self):
        contract = async_contracts.match_id(mcp_tasks.PROTOCOL_CONTRACT_ID,
                                            mcp_tasks.PROTOCOL_CONTRACT_VERSION)
        self.assertIsNotNone(contract)
        assert contract is not None          # 让类型检查和运行时都窄化
        self.assertTrue(contract.recoverable)
        # 协议任务的"查询"是 tasks/get 请求，不是工具 → 这里必须是空的
        self.assertEqual(contract.status_tool_key, '')
        self.assertEqual(contract.status_pointer, '/status')
        self.assertEqual(async_contracts.map_status('working', contract), 'running')

    def test_version_mismatch_does_not_match(self):
        self.assertIsNone(async_contracts.match_id(mcp_tasks.PROTOCOL_CONTRACT_ID, 99))

    def test_real_contracts_still_load(self):
        """协议保留身份不能混入出厂合同清单。"""
        loaded = async_contracts.load()
        ids = {row.contract_id for row in loaded}
        self.assertNotIn(mcp_tasks.PROTOCOL_CONTRACT_ID, ids)


class _AdapterSpy:
    """记录走了哪条通道，并回一个固定回执。"""

    def __init__(self, task_payload=None):
        self.calls = []
        self.task_payload = task_payload or {
            'taskId': 'task-1', 'status': 'working'}

    async def call_tool(self, tool_name, arguments, artifact=None, server=None, **kw):
        self.calls.append(('sync', tool_name, dict(arguments)))
        return {'status': 'ok', 'data': 'inline'}

    async def call_tool_as_task(self, tool_name, arguments, artifact=None,
                                server=None, **kw):
        self.calls.append(('task', tool_name, dict(arguments)))
        return dict(self.task_payload)


class RunnerChannelSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, task_support, spy):
        original_tool, original_task = mcp_adapter.call_tool, mcp_adapter.call_tool_as_task
        original_subject = tools._subject_token
        mcp_adapter.call_tool = spy.call_tool
        mcp_adapter.call_tool_as_task = spy.call_tool_as_task

        async def no_subject(server, tenant_id='', user_id=''):
            return None

        tools._subject_token = no_subject
        try:
            runner = tools._make_mcp_runner('long_job', None, task_support=task_support)
            return await runner({}, {'artifact': None})
        finally:
            mcp_adapter.call_tool, mcp_adapter.call_tool_as_task = original_tool, original_task
            tools._subject_token = original_subject

    async def test_required_goes_through_task_channel(self):
        spy = _AdapterSpy()
        with assigned(settings, mcp_tasks_enabled=True, mcp_tasks_optional_async=False,
                      async_tool_runtime_enabled=True):
            updates = await self._run('required', spy)
        self.assertEqual([c[0] for c in spy.calls], ['task'])
        self.assertEqual(updates['execution_status'], 'accepted')
        self.assertEqual(updates['remote_task_id'], 'task-1')
        self.assertEqual(updates['contract_id'], mcp_tasks.PROTOCOL_CONTRACT_ID)

    async def test_forbidden_keeps_the_old_synchronous_path(self):
        """回归保护：没声明任务能力的工具行为必须和以前一模一样。"""
        spy = _AdapterSpy()
        with assigned(settings, mcp_tasks_enabled=True, mcp_tasks_optional_async=True,
                      async_tool_runtime_enabled=True):
            updates = await self._run('forbidden', spy)
        self.assertEqual([c[0] for c in spy.calls], ['sync'])
        # 回归要点：没有受理增量、没有协议任务身份，回执原样记进 mcp_calls
        self.assertNotIn('execution_status', updates)
        self.assertNotIn('contract_id', updates)
        self.assertNotIn('remote_task_id', updates)
        self.assertEqual(updates['mcp_calls'][-1]['payload'],
                         {'status': 'ok', 'data': 'inline'})

    async def test_disabled_switch_keeps_synchronous_path_for_required(self):
        spy = _AdapterSpy()
        with assigned(settings, mcp_tasks_enabled=False, async_tool_runtime_enabled=True):
            await self._run('required', spy)
        self.assertEqual([c[0] for c in spy.calls], ['sync'])


class DocumentedDefaultsTests(unittest.TestCase):
    """README §10 写的是"默认全关"。默认值改动必须与文档同步，否则这条会红。"""

    def test_task_switches_default_to_false(self):
        import os
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        code = ('from webpage_agent.core.config import settings;'
                'print(settings.mcp_tasks_enabled, settings.mcp_tasks_optional_async)')
        env = {'APP_ENV': 'test', 'PATH': os.environ.get('PATH', ''),
               'PYTHONPATH': str(root / 'src')}
        completed = subprocess.run([sys.executable, '-c', code], cwd=root, env=env,
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # 两个都不该被默认打开：这是"弃用通道不会被静默启用"的底线
        self.assertEqual(completed.stdout.strip(), 'False False')


class _PollStore:
    def __init__(self, contract_id, tool_key='mcp:probe:long_job'):
        self.task = {
            'external_task_id': 'et1', 'remote_task_id': 'task-1',
            'tenant_id': 'default', 'owner_user_id': 'default',
            'tool_key': tool_key,
            'contract_id': contract_id, 'contract_version': 1,
            'run_id': 'run1', 'action_id': 'a1', 'state': 'accepted',
            'version': 1, 'deadline_at': 0, 'poll_epoch': 0,
        }
        self.run_row = {'status': 'waiting_input', 'run_id': 'run1'}
        self.snapshot = {'plan_version': 1, 'next_step_id': '',
                         'state': {'tool_results': [{
                             'action_id': 'a1', 'phase': 'accepted',
                             'remote_task_id': 'task-1',
                             'execution_status': 'accepted'}]}}

    async def due_external_tasks(self, now_ts=None, limit=10):
        return [dict(self.task)]

    async def claim_external_task_poll(self, external_task_id, owner, lease_seconds=60):
        self.task['poll_epoch'] += 1
        return dict(self.task)

    async def finish_external_poll(self, external_task_id, epoch, owner='',
                                   **fields):
        self.task.update(fields)
        return dict(self.task)

    async def complete_external_task_and_wake(self, external_task_id, epoch, *,
                                              owner='', state,
                                              output_manifest_ref='',
                                              last_result_id='', error_code='',
                                              staged=None, envelope=None):
        # 第八阶段：poller 不直接改写快照，只做「终态 + 唤醒 run」同事务。
        self.task['state'] = state
        self.task['error_code'] = error_code
        if output_manifest_ref:
            self.task['output_manifest_ref'] = output_manifest_ref
        if last_result_id:
            self.task['last_result_id'] = last_result_id
        if state == 'completed':
            self.run_row['status'] = 'queued'
        elif state in ('failed', 'cancelled', 'needs_reconcile'):
            self.run_row['status'] = 'needs_reconcile'
        return dict(self.task)

    async def run(self, run_id):
        return dict(self.run_row)

    async def run_snapshot(self, run_id):
        return {'plan_version': 1, 'next_step_id': '',
                'state': self.snapshot['state']}

    async def save_snapshot(self, run_id, *, plan_version, state, next_step_id=''):
        self.snapshot['state'] = state

    async def external_tasks_of_run(self, run_id):
        return [dict(self.task)]

    async def update_run(self, run_id, **fields):
        self.run_row.update(fields)


class ProtocolPollTests(unittest.IsolatedAsyncioTestCase):
    async def _poll(self, store, status):
        seen = {}

        async def fake_task_status(task_id, server=None, token=None):
            seen['task_id'] = task_id
            return {'taskId': task_id, 'status': status}

        async def bearer_for(*a, **kw):
            return ''

        originals = (mcp_adapter.task_status, mcp_connections.bearer_for)
        mcp_adapter.task_status = fake_task_status
        mcp_connections.bearer_for = bearer_for
        try:
            with tempfile.TemporaryDirectory() as folder, \
                    assigned(settings, async_tool_runtime_enabled=True,
                             artifact_dir=Path(folder)):
                done = await external_tasks.poll_due(store, worker_id='w1')
        finally:
            mcp_adapter.task_status, mcp_connections.bearer_for = originals
        return done, seen

    async def test_working_keeps_polling_without_waking_the_run(self):
        store = _PollStore(mcp_tasks.PROTOCOL_CONTRACT_ID)
        done, seen = await self._poll(store, 'working')
        self.assertEqual(done, 1)
        self.assertEqual(seen['task_id'], 'task-1')   # 走的是 tasks/get，不是工具
        self.assertEqual(store.task['state'], 'running')
        self.assertEqual(store.run_row['status'], 'waiting_input')

    async def test_completed_settles_and_patches_the_snapshot(self):
        store = _PollStore(mcp_tasks.PROTOCOL_CONTRACT_ID)
        await self._poll(store, 'completed')
        self.assertEqual(store.task['state'], 'completed')
        # 第八阶段：poller 不再直接改快照，改为「完成 + 唤醒 run」。
        self.assertIn('output_manifest_ref', store.task)
        self.assertEqual(store.run_row['status'], 'queued')

    async def test_failed_marks_failed_not_success(self):
        store = _PollStore(mcp_tasks.PROTOCOL_CONTRACT_ID)
        await self._poll(store, 'failed')
        self.assertEqual(store.task['state'], 'failed')
        self.assertEqual(store.run_row['status'], 'needs_reconcile')

    async def test_contract_route_is_untouched(self):
        """回归保护：合同路线仍然走状态查询工具，且不会被协议分支顶掉。

        出厂合同文件当前为空，这里用一份临时合同验证合同路由机制本身。
        """
        store = _PollStore('probe-video-v1', tool_key='mcp:probe:render_video')
        called = {}

        async def fake_get_task(*a, **kw):
            called['task_status'] = True
            return {'status': 'working'}

        async def fake_call_tool(tool_name, arguments, artifact=None, server=None, **kw):
            called['tool'] = (tool_name, dict(arguments))
            return {'status': 'running'}

        async def bearer_for(*a, **kw):
            return ''

        originals = (mcp_adapter.task_status, mcp_adapter.call_tool,
                     mcp_connections.bearer_for)
        mcp_adapter.task_status = fake_get_task
        mcp_adapter.call_tool = fake_call_tool
        mcp_connections.bearer_for = bearer_for
        try:
            with tempfile.TemporaryDirectory() as folder, \
                    assigned(settings, async_tool_runtime_enabled=True,
                             artifact_dir=Path(folder)):
                contract_file = Path(folder) / 'async_tools.json'
                contract_file.write_text(
                    '{"contracts": [{"contract_id": "probe-video-v1", '
                    '"version": 1, "submit_tool_key": "mcp:probe:render_video", '
                    '"remote_id_pointer": "/job_id", '
                    '"status_tool_key": "mcp:probe:get_job_status", '
                    '"status_id_argument": "job_id", '
                    '"status_pointer": "/status", '
                    '"status_map": {"completed": "succeeded"}, '
                    '"recoverable": true}]}', encoding='utf-8')
                with assigned(settings, async_contracts_file=str(contract_file)):
                    await external_tasks.poll_due(store, worker_id='w1')
        finally:
            (mcp_adapter.task_status, mcp_adapter.call_tool,
             mcp_connections.bearer_for) = originals
        self.assertNotIn('task_status', called)
        self.assertEqual(called['tool'][0], 'get_job_status')
        self.assertEqual(called['tool'][1], {'job_id': 'task-1'})
