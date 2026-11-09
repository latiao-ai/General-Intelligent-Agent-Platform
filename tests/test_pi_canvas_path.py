"""画布主路径：坏 schema 不拖垮目录、写白名单有门禁、缺参进入 waiting_input。

不依赖 PostgreSQL。引擎身份落库见 test_runtime_ownership。
"""
import os
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SchemaAssemblyTests(unittest.TestCase):
    def test_config_source_defaults_new_runs_to_pi(self):
        text = (ROOT / 'src/webpage_agent/core/config.py').read_text(encoding='utf-8')
        self.assertIn("os.getenv('DEFAULT_RUNTIME_KIND', 'pi')", text)

    def test_bad_root_is_dropped_and_object_tool_stays(self):
        from webpage_agent.agent.pi_tool_schema import prepare_declarations
        kept, dropped = prepare_declarations([
            {'name': 'mcp__libtv__inspect_media', 'description': '坏根',
             'parameters': {'anyOf': [{'type': 'string'}, {'type': 'integer'}]}},
            {'name': 'get_object', 'description': '读对象',
             'parameters': {'type': 'object',
                            'properties': {'object_id': {'type': 'string'}},
                            'required': ['object_id']}},
        ])
        self.assertEqual([item['name'] for item in kept], ['get_object'])
        self.assertEqual(dropped[0]['name'], 'mcp__libtv__inspect_media')
        self.assertIn('anyOf', dropped[0]['reason'])

    def test_object_variant_union_is_normalized_to_object(self):
        from webpage_agent.agent.pi_tool_schema import prepare_declarations
        kept, dropped = prepare_declarations([{
            'name': 'inspect_media',
            'description': '互斥必填',
            'parameters': {'anyOf': [
                {'type': 'object',
                 'properties': {'fileRef': {'type': 'string'}},
                 'required': ['fileRef']},
                {'type': 'object',
                 'properties': {'projectId': {'type': 'string'}},
                 'required': ['projectId']},
            ]},
        }])
        self.assertEqual(dropped, [])
        params = kept[0]['parameters']
        self.assertEqual(params['type'], 'object')
        self.assertEqual(set(params['properties']), {'fileRef', 'projectId'})
        self.assertEqual(params['required'], [])

    def test_object_with_oneof_is_kept_for_the_node_compiler(self):
        from webpage_agent.agent.pi_tool_schema import prepare_declarations
        kept, dropped = prepare_declarations([{
            'name': 'inspect_media',
            'description': '已有 object 外壳',
            'parameters': {
                'type': 'object',
                'properties': {'fileRef': {'type': 'string'}},
                'oneOf': [{'required': ['fileRef']}],
            },
        }])
        self.assertEqual(dropped, [])
        self.assertEqual(kept[0]['parameters']['type'], 'object')
        self.assertIn('oneOf', kept[0]['parameters'])


class WriteGateTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in ('PI_WRITE_TOOL_KEYS', 'PI_WRITE_TOOLS_ENABLED')}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_keys_without_gate_do_not_expand_and_are_logged(self):
        from webpage_agent.agent import pi_host_tools
        os.environ['PI_WRITE_TOOL_KEYS'] = '["mcp:platform:update_object"]'
        os.environ['PI_WRITE_TOOLS_ENABLED'] = 'false'
        with self.assertLogs('webpage_agent.agent.pi_host_tools', level='WARNING') as logs:
            keys = pi_host_tools._effective_write_keys()
        self.assertEqual(keys, set())
        self.assertTrue(any('拒绝静默扩权' in line for line in logs.output))

    def test_gate_and_keys_expand_with_an_audit_log(self):
        from webpage_agent.agent import pi_host_tools
        os.environ['PI_WRITE_TOOL_KEYS'] = '["mcp:platform:update_object"]'
        os.environ['PI_WRITE_TOOLS_ENABLED'] = 'true'
        with self.assertLogs('webpage_agent.agent.pi_host_tools', level='INFO') as logs:
            keys = pi_host_tools._effective_write_keys()
        self.assertEqual(keys, {'mcp:platform:update_object'})
        self.assertTrue(any('审计' in line for line in logs.output))


class SystemPromptTests(unittest.TestCase):
    def test_get_runtime_pi_uses_settings_prompt(self):
        from webpage_agent.agent.pi_runtime import PiRuntime
        from webpage_agent.agent.runtime import get_runtime
        from tests.exec_env import override_settings
        with override_settings(pi_system_prompt='自定义停手'):
            runtime = get_runtime('pi')
        self.assertIsInstance(runtime, PiRuntime)
        self.assertEqual(runtime._system_prompt, '自定义停手')

    def test_empty_prompt_disables_injection(self):
        from webpage_agent.agent.runtime import get_runtime
        from tests.exec_env import override_settings
        with override_settings(pi_system_prompt=''):
            runtime = get_runtime('pi')
        self.assertIsNone(runtime._system_prompt)


class _Bridge:
    execution_id = 'exec'

    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    def stderr_tail(self):
        return ''


class WaitingInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_input_sets_waiting_input(self):
        from webpage_agent.agent import pi_protocol as proto
        from webpage_agent.agent.pi_host_tools import request_input_decl
        from webpage_agent.agent.pi_runtime import PiRuntime
        runtime = PiRuntime(tools=[request_input_decl()])
        runtime._snapshot = {'host_result_state': {}, 'pending_calls': [
            {'call_id': 'c1', 'tool_name': 'request_input'}]}
        bridge = _Bridge()
        state = {'task_id': 't', 'user_input': '改标题', 'answer': '旧答复'}
        msg = proto.make_message(
            'exec', 'tool_call', call_id='c1', tool_name='request_input',
            arguments={'question': '要改哪一个对象？'})
        await runtime._handle_tool_call(bridge, msg, state)
        self.assertEqual(state['status'], 'waiting_input')
        self.assertIn('对象', state['failure'])
        self.assertNotIn('answer', state)
        self.assertEqual(runtime._snapshot['phase'], 'awaiting_input')
        self.assertTrue(bridge.sent[-1]['needs_input'])
        self.assertEqual(
            runtime._snapshot['host_result_state']['awaiting_input']['call_id'], 'c1')

    async def test_repeated_missing_args_stop_instead_of_looping(self):
        from webpage_agent.agent import pi_protocol as proto
        from webpage_agent.agent.pi_runtime import PiRuntime
        runtime = PiRuntime(tools=[{
            'name': 'update_object', 'description': '写',
            'parameters': {'type': 'object',
                           'properties': {'object_id': {'type': 'string'}},
                           'required': ['object_id']},
        }])
        runtime._snapshot = {'host_result_state': {}, 'pending_calls': []}
        bridge = _Bridge()
        state = {'task_id': 't'}
        for call_id in ('a', 'b'):
            msg = proto.make_message(
                'exec', 'tool_call', call_id=call_id, tool_name='update_object',
                arguments={})
            await runtime._handle_tool_call(bridge, msg, state)
        self.assertEqual(state['status'], 'waiting_input')
        self.assertIn('object_id', state['failure'])
        self.assertFalse(bridge.sent[0].get('needs_input'))
        self.assertTrue(bridge.sent[-1]['needs_input'])

    async def test_finished_input_reason_is_waiting_input(self):
        from webpage_agent.agent import pi_protocol as proto
        from webpage_agent.agent.pi_runtime import PiRuntime
        runtime = PiRuntime(tools=[])
        runtime._snapshot = {'host_result_state': {'awaiting_input': {
            'call_id': 'c1', 'tool_name': 'request_input', 'question': '选中哪个？'}}}
        state = {'answer': '不该成功'}
        runtime._apply_finished(state, proto.make_message(
            'exec', 'finished', outcome='waiting', wait_reason='input'), 'exec')
        self.assertEqual(state['status'], 'waiting_input')
        self.assertEqual(state['failure'], '选中哪个？')
        self.assertNotIn('answer', state)


class InputRecoveryTests(unittest.TestCase):
    def _snapshot(self):
        from webpage_agent.agent import pi_snapshot
        snap = pi_snapshot.new_snapshot(user_input='改标题')
        snap['phase'] = 'awaiting_input'
        snap['host_result_state'] = {'awaiting_input': {
            'call_id': 'c1', 'tool_name': 'request_input', 'question': '哪个对象？'}}
        snap['pending_calls'] = [{'call_id': 'c1', 'tool_name': 'request_input'}]
        return snap

    def test_without_supplement_stays_waiting(self):
        from webpage_agent.agent import pi_recovery
        decision = pi_recovery.decide(self._snapshot(), [], 'running')
        self.assertEqual(decision.kind, pi_recovery.RESUME_WAIT_INPUT)
        again, _snap = pi_recovery.apply_pending_input(decision, self._snapshot(), [])
        self.assertEqual(again.kind, pi_recovery.RESUME_WAIT_INPUT)

    def test_supplement_delivers_the_parked_call(self):
        from webpage_agent.agent import pi_recovery
        snap = self._snapshot()
        decision = pi_recovery.decide(snap, [], 'waiting_input')
        nxt, updated = pi_recovery.apply_pending_input(
            decision, snap, [{'message': '左边那个'}])
        self.assertEqual(nxt.kind, pi_recovery.RESUME_DELIVER)
        self.assertEqual(nxt.deliver_results[0]['call_id'], 'c1')
        self.assertEqual(nxt.deliver_results[0]['result']['messages'], ['左边那个'])
        self.assertEqual(updated['phase'], 'running')
        self.assertNotIn('awaiting_input', updated['host_result_state'])
        self.assertEqual(snap['phase'], 'awaiting_input')


class _EngineStore:
    """只实现恢复分叉要碰到的仓储方法。"""

    def __init__(self, kind: str):
        self.kind = kind
        self.updated = None

    async def run(self, run_id):
        version = 1
        schema = 2 if self.kind == 'pi' else 1
        return {
            'run_id': run_id, 'task_id': 't1', 'status': 'paused',
            'runtime_kind': self.kind, 'runtime_version': version,
            'runtime_state_schema': schema, 'lease_expires_at': None,
            'intent_version': 1, 'processed_intent_version': 0,
            'plan_version': 1, 'goal': '改标题',
        }

    async def runtime_identity(self, run_id):
        row = await self.run(run_id)
        return {'kind': row['runtime_kind'], 'version': row['runtime_version'],
                'state_schema': row['runtime_state_schema']}

    async def plan(self, run_id):
        return {}

    async def actions_of(self, run_id):
        return [{'action_id': 'a1', 'status': 'succeeded', 'effect': 'read'}]

    async def run_snapshot(self, run_id):
        return None

    async def run_intent(self, run_id):
        return {'intent_version': 1, 'processed_intent_version': 0,
                'pending_input_count': 0}

    async def pending_run_inputs(self, run_id):
        return []

    async def update_run(self, run_id, **fields):
        self.updated = fields

    async def events(self, task_id):
        return []

    async def append_event(self, task_id, event):
        return 1


class DualEngineRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def _hub(self, kind: str):
        from webpage_agent.api.api_hub import Hub
        store = _EngineStore(kind)
        hub = Hub(store)
        hub._has_uncertain_actions = mock.AsyncMock(return_value=False)
        hub._claim_and_arm = mock.AsyncMock(return_value='started')
        return hub, store

    async def test_pi_resume_with_actions_and_no_graph_plan_continues(self):
        from webpage_agent.api.task_record import TaskRecord
        hub, store = await self._hub('pi')
        record = TaskRecord(task_id='t1', session_id='s', message='改标题', store=store)
        hub.get = mock.AsyncMock(return_value=record)
        result = await hub.resume('r1')
        self.assertIs(result, record)
        hub._claim_and_arm.assert_awaited()
        self.assertEqual(store.updated['status'], 'queued')

    async def test_langgraph_resume_with_actions_and_no_plan_refuses(self):
        from webpage_agent.api.task_record import TaskRecord
        hub, store = await self._hub('langgraph')
        record = TaskRecord(task_id='t1', session_id='s', message='改标题', store=store)
        hub.get = mock.AsyncMock(return_value=record)
        result = await hub.resume('r1')
        self.assertIsNone(result)
        hub._claim_and_arm.assert_not_awaited()

    async def test_second_round_keeps_pi_snapshot_instead_of_graph_steps(self):
        hub, _store = await self._hub('pi')
        plan = await hub._resume_for_engine('r1')
        self.assertIn('pi_resume', plan)
        self.assertNotIn('steps', plan)
        self.assertEqual(plan['pi_resume']['recovery']['kind'], 'resume_reconcile')

    async def test_second_round_langgraph_stays_on_the_graph_shape(self):
        hub, _store = await self._hub('langgraph')
        plan = await hub._resume_for_engine('r1')
        self.assertNotIn('pi_resume', plan or {})
        self.assertNotIn('steps', plan or {})
        self.assertIn('intent_version', plan)

    def test_protocol_version_matches_node(self):
        from webpage_agent.agent.pi_protocol import PROTOCOL_VERSION
        text = (ROOT / 'pi-runtime/src/protocol.ts').read_text(encoding='utf-8')
        self.assertIn(f'export const PROTOCOL_VERSION = {PROTOCOL_VERSION};', text)


if __name__ == '__main__':
    unittest.main()
