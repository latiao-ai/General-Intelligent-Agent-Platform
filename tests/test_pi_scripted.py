"""补偿与部署走 Pi 脚本，不走模型，也不再钉死 LangGraph。

覆盖四件事：

- 部署 / 补偿新建的 run 是 pi，并且工具真的被调用（受控工具，不连外网）；
- 只把 `resume_plan.steps` 交给 Pi、不标 `scripted`，步骤不会执行（防空转）；
- 脚本中断后的恢复：已成功的不重做，等待批准的不重发，不走图的 `_resume_plan`；
- Pi 原 run 没有图计划时，撤销步骤来自账本回执里冻住的 compensate。
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from webpage_agent.agent.pi_host_tools import ToolReceipt
from webpage_agent.agent.pi_protocol import PROTOCOL_VERSION
from webpage_agent.agent.pi_runtime import PiRuntime
from webpage_agent.agent.pi_script import progress
from webpage_agent.capabilities.registry import Tool
from webpage_agent.governance import execution_control
from webpage_agent.store import ledger as ledger_module
from webpage_agent.tools.compensation import freeze_compensate
from webpage_agent.tools.execution_gateway import governed_call

from tests.exec_env import new_store, override_settings, reset_db


def _tool(name, effect='write', compensate=None, calls=None, output=None):
    async def run(args, state):
        if calls is not None:
            calls.append((name, dict(args or {}), dict(state.get('prior_artifact') or {})))
        return dict(output or {'ok': True})

    return Tool(name, name, '测试工具', {'type': 'object', 'properties': {}}, run,
                tool_key=f'local:test:{name}', effect=effect,
                compensate=compensate)


class _Caller:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    async def call_declared(self, *, step_id, tool, args, state, build_snapshot=None):
        self.calls.append((step_id, tool.name, dict(args or {})))
        receipt = (self._results.pop(0) if self._results
                   else ToolReceipt(ok=True, updates={}))
        if receipt.ok and build_snapshot is not None:
            build_snapshot(dict(receipt.updates or {}),
                           tool_result={'execution_status': 'succeeded',
                                        'step_id': step_id})
        return receipt


class _Bridge:
    def __init__(self):
        self.execution_id = 'exec-1'
        self.started = False

    async def start(self):
        self.started = True

    async def send(self, msg):
        self.sent = msg

    async def next_message(self):
        return {'protocol_version': PROTOCOL_VERSION,
                'execution_id': self.execution_id,
                'type': 'finished', 'outcome': 'cancelled'}

    async def close(self):
        return None

    def stderr_tail(self):
        return ''


class _ExplodingBridge:
    execution_id = 'exec-1'

    async def start(self):
        raise AssertionError('脚本化运行启动了模型桥接')


def _steps():
    return [
        {'tool': 'undo_b', 'step_id': 'undo:b', 'args': {'site_id': 'b'},
         'compensates': 'b'},
        {'tool': 'undo_a', 'step_id': 'undo:a', 'args': {'site_id': 'a'},
         'compensates': 'a'},
    ]


class ScriptedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_script_executes_steps_in_order_without_bridge(self):
        caller = _Caller()
        runtime = PiRuntime(bridge=_ExplodingBridge(), tools=[], host_caller=caller)
        state = {
            'user_input': '补偿已完成步骤', 'run_id': 'run-script',
            'resume_plan': {'scripted': True, 'kind': 'compensation',
                            'summary': '补偿已完成步骤', 'steps': _steps(),
                            'intent_version': 1},
        }
        tools = {'undo_a': _tool('undo_a'), 'undo_b': _tool('undo_b')}

        async def fake_find(name, context=None):
            return tools.get(name)

        with patch('webpage_agent.tools.core.find_tool', fake_find):
            await runtime.run(state)

        self.assertEqual([item[0] for item in caller.calls], ['undo:b', 'undo:a'])
        self.assertEqual([item[2]['site_id'] for item in caller.calls], ['b', 'a'])
        self.assertEqual(state['plan']['kind'], 'compensation')
        self.assertEqual(state['status'], 'succeeded')
        self.assertEqual(state['outcome']['kind'], 'compensation')
        self.assertEqual([item['step_id'] for item in state['compensations']],
                         ['b', 'a'])
        script = state['_pi_snapshot']['host_result_state']['script']
        self.assertTrue(script['scripted'])
        self.assertEqual(script['cursor'], 2)

    async def test_unscripted_steps_do_not_run(self):
        """防空转：只改 runtime_kind、不走脚本时，Pi 不会执行 resume_plan.steps。"""
        caller = _Caller()
        bridge = _Bridge()
        runtime = PiRuntime(bridge=bridge, tools=[], host_caller=caller)
        state = {
            'user_input': '补偿已完成步骤',
            'resume_plan': {'kind': 'compensation', 'steps': _steps(),
                            'summary': '补偿已完成步骤'},
        }
        await runtime.run(state)
        self.assertEqual(caller.calls, [])
        self.assertTrue(bridge.started)
        self.assertNotIn('compensations', state)

    async def test_waiting_approval_resume_does_not_recall_until_admitted(self):
        waiting = ToolReceipt(ok=False, needs_approval=True, approval_id='ap1',
                              error='需要批准')
        first = _Caller([waiting])
        runtime = PiRuntime(bridge=_ExplodingBridge(), tools=[], host_caller=first)
        step = {'tool': 'deploy_site', 'step_id': 's1', 'args': {}}
        state = {
            'user_input': '部署',
            'resume_plan': {'scripted': True, 'kind': 'deployment',
                            'summary': '部署上一轮产物', 'steps': [step]},
        }
        tools = {'deploy_site': _tool('deploy_site')}

        async def fake_find(name, context=None):
            return tools.get(name)

        with patch('webpage_agent.tools.core.find_tool', fake_find):
            await runtime.run(state)
        self.assertEqual(state['status'], 'waiting_approval')
        self.assertEqual(len(first.calls), 1)
        snapshot = state['_pi_snapshot']

        second = _Caller()
        again = PiRuntime(bridge=_ExplodingBridge(), tools=[], host_caller=second)
        resumed = {
            'user_input': '部署',
            'pi_resume': {'snapshot': snapshot,
                          'recovery': {'kind': 'resume_wait'}},
        }
        with patch('webpage_agent.tools.core.find_tool', fake_find):
            await again.run(resumed)
        self.assertEqual(second.calls, [])
        self.assertEqual(resumed['status'], 'waiting_approval')

        third = _Caller([ToolReceipt(
            ok=True, updates={'deployment': {'status': 'success',
                                             'public_url': 'https://example.test'}})])
        admitted = PiRuntime(bridge=_ExplodingBridge(), tools=[], host_caller=third)
        # 新一轮执行的 state 是 run_task 重新装配的，不会带着上一轮的 waiting_approval。
        admitted_state = {
            'user_input': '部署',
            'pi_resume': {'snapshot': snapshot,
                          'recovery': {'kind': 'resume_admit'}},
        }
        with patch('webpage_agent.tools.core.find_tool', fake_find):
            await admitted.run(admitted_state)
        self.assertEqual([item[0] for item in third.calls], ['s1'])
        self.assertEqual(admitted_state['status'], 'succeeded')
        self.assertEqual(admitted_state['deployment']['status'], 'success')
        self.assertEqual(admitted_state['outcome']['kind'], 'deployment')

    async def test_ledger_succeeded_step_is_not_called_again(self):
        caller = _Caller([ToolReceipt(ok=True, updates={})])
        runtime = PiRuntime(bridge=_ExplodingBridge(), tools=[], host_caller=caller)
        rows = [{
            'step_id': 's1', 'status': 'succeeded',
            'receipt': {'deployment': {'status': 'success', 'public_url': 'https://kept.test'}},
        }]

        class Store:
            async def actions_of(self, run_id):
                return rows

        book = SimpleNamespace(store=Store(), run_id='run-1', context=None,
                               plan_version=1)
        state = {
            'user_input': '部署', 'run_id': 'run-1',
            'resume_plan': {'scripted': True, 'kind': 'deployment',
                            'summary': '部署上一轮产物',
                            'steps': [
                                {'tool': 'deploy_site', 'step_id': 's1', 'args': {}},
                                {'tool': 'notify', 'step_id': 's2', 'args': {}},
                            ]},
        }
        tools = {'deploy_site': _tool('deploy_site'), 'notify': _tool('notify', effect='read')}

        async def fake_find(name, context=None):
            return tools.get(name)

        token = ledger_module.bind(book)
        try:
            with patch('webpage_agent.tools.core.find_tool', fake_find):
                await runtime.run(state)
        finally:
            ledger_module.unbind(token)
        self.assertEqual([item[0] for item in caller.calls], ['s2'])
        self.assertEqual(state['deployment']['public_url'], 'https://kept.test')

    async def test_uncertain_action_blocks_resume(self):
        index, reason = progress(
            [{'step_id': 's1'}, {'step_id': 's2'}],
            [{'step_id': 's1', 'status': 'in_flight'}])
        self.assertEqual((index, reason), (0, 'uncertain'))
        index, reason = progress(
            [{'step_id': 'undo:a'}],
            [{'step_id': 'undo:a', 'status': 'failed'}])
        self.assertEqual((index, reason), (0, 'failed'))
        index, reason = progress(
            [{'step_id': 's1'}, {'step_id': 's2'}],
            [{'step_id': 's1', 'status': 'succeeded'}])
        self.assertEqual((index, reason), (1, ''))

    def test_freeze_compensate_resolves_own_output(self):
        tool = _tool('deploy_site', compensate={
            'tool': 'delete_site',
            'args': {'site_id': {'ref': {'pointer': '/site_id'}}},
            'tool_key': 'local:test:delete_site',
            'effect': 'destructive',
        })
        frozen = freeze_compensate(tool, {'site_id': 'site-9', 'status': 'deployed'})
        self.assertEqual(frozen['tool'], 'delete_site')
        self.assertEqual(frozen['args'], {'site_id': 'site-9'})
        self.assertIsNone(freeze_compensate(tool, {'status': 'deployed'}))
        self.assertIsNone(freeze_compensate(
            _tool('deploy_site', compensate={'tool': 'deploy_site', 'args': {}}),
            {'site_id': 'x'}))


class GovernedCompensateStampTests(unittest.IsolatedAsyncioTestCase):
    async def test_governed_call_freezes_compensate_into_receipt(self):
        seen = {}

        class Book:
            run_id = 'r'
            plan_version = 1
            context = execution_control.ExecutionContext('r', 1, 'owner', 1)
            store = None

            def __init__(self):
                self.store = self

            def key_of(self, step_id, tool_key, args):
                return f'{step_id}:{tool_key}'

            async def admit(self, **kwargs):
                return {'status': 'admitted', 'action_id': 'act1', 'attempt_id': 'att1'}

            async def settle(self, **kwargs):
                seen['receipt'] = kwargs.get('receipt')
                return {'observation_seq': 0}

            async def control_state(self, run_id):
                return {'intent_version': 1}

            async def consume_budget(self, *args, **kwargs):
                return None

            async def record_authorization(self, **kwargs):
                return ''

        book = Book()
        tool = _tool('deploy_site', compensate={
            'tool': 'delete_site',
            'args': {'site_id': {'ref': {'pointer': '/site_id'}}},
            'effect': 'destructive',
        })

        async def run(args, state):
            return {'site_id': 'site-9'}

        tool = Tool('deploy_site', '部署', '部署', {'type': 'object', 'properties': {}},
                    run, tool_key='local:test:deploy_site', effect='write',
                    compensate=tool.compensate)
        ledger_token = ledger_module.bind(book)
        context_token = execution_control.bind(book.context)
        try:
            with override_settings(tool_policy_enforced=False):
                result = await governed_call(
                    tool=tool, args={}, step_id='s1', state={'intent_version': 1},
                    ctx=SimpleNamespace(tenant_id='default', user_id='default',
                                        role='member', platform_id=''))
        finally:
            execution_control.unbind(context_token)
            ledger_module.unbind(ledger_token)
        self.assertEqual(result.kind, 'succeeded')
        self.assertEqual(seen['receipt']['compensate']['args'], {'site_id': 'site-9'})
        self.assertEqual(seen['receipt']['compensate']['tool'], 'delete_site')


class HubScriptedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_db()

    async def test_deploy_artifact_is_pi_and_calls_tool(self):
        from webpage_agent.api.api_hub import Hub
        from webpage_agent.api import main as main_module

        calls = []
        tool = _tool('deploy_site', calls=calls, output={
            'deployment': {'status': 'success', 'public_url': 'https://example.test'},
        })
        hub = Hub(new_store())
        await hub.store.create_task('t1', 's1', '发布页面')
        await hub.store.ensure_task_meta('t1', session_id='s1', goal='发布页面')
        await hub.store.create_run('r-old', 't1', status='succeeded',
                                    runtime_kind='langgraph')
        record = await hub.get('t1')
        prior = {'html_path': '/tmp/report.html', 'sha256': 'abc', 'artifact_id': 'art1'}
        record.state = {'artifact': prior}
        record.artifact_id = 'art1'

        async def fake_find(name, context=None):
            return tool if name == 'deploy_site' else None

        with patch('webpage_agent.tools.core.find_tool', fake_find), \
                patch.object(main_module, 'check_runtime_keys', lambda: None), \
                override_settings(tool_policy_enforced=False):
            deployment = await hub.deploy_artifact(record, prior)

        self.assertEqual(deployment['status'], 'success')
        self.assertEqual([item[0] for item in calls], ['deploy_site'])
        deploy_run = await hub.store.run(record.run_id)
        self.assertEqual(deploy_run['runtime_kind'], 'pi')
        self.assertNotEqual(record.run_id, 'r-old')
        original = await hub.store.run('r-old')
        self.assertEqual(original['runtime_kind'], 'langgraph')
        actions = await hub.store.actions_of(record.run_id)
        self.assertEqual(actions[0]['step_id'], 's1')
        self.assertEqual(actions[0]['status'], 'succeeded')
        self.assertEqual(record.state['artifact']['html_path'], '/tmp/report.html')
        self.assertEqual(record.state['deployment']['public_url'], 'https://example.test')
        script = record.state['_pi_snapshot']['host_result_state']['script']
        self.assertTrue(script['scripted'])
        self.assertEqual(script['kind'], 'deployment')

    async def test_compensate_graph_plan_creates_pi_run_and_calls_undo(self):
        from webpage_agent.api.api_hub import Hub
        from webpage_agent.api import main as main_module

        calls = []
        undo = _tool('undo_site', effect='destructive', calls=calls,
                     output={'removed': True})
        hub = Hub(new_store())
        await hub.store.create_task('t1', 's1', '发布页面')
        await hub.store.ensure_task_meta('t1', session_id='s1', goal='发布页面')
        await hub.store.create_run('r1', 't1', status='succeeded',
                                    runtime_kind='langgraph')
        step = {'tool': 'deploy_site', 'step_id': 's1', 'args': {},
                'tool_key': 'local:publishing:deploy', 'effect': 'write',
                'compensate': {'tool': 'undo_site', 'args': {'site_id': 'x'},
                               'tool_key': 'local:test:undo_site',
                               'effect': 'destructive'}}
        await hub.store.save_plan('r1', 1, goal='发布页面', steps=[step])
        await hub.store.save_steps('r1', 1, [step])
        await hub.store.update_step('r1', 1, 's1', status='succeeded')

        async def fake_find(name, context=None):
            return undo if name == 'undo_site' else None

        with patch('webpage_agent.tools.core.find_tool', fake_find), \
                patch.object(main_module, 'check_runtime_keys', lambda: None), \
                override_settings(tool_policy_enforced=False):
            result = await hub.compensate('r1')

        self.assertEqual(result['status'], 'compensated')
        self.assertEqual(calls[0][0], 'undo_site')
        self.assertEqual(calls[0][1], {'site_id': 'x'})
        self.assertNotEqual(result['run_id'], 'r1')
        comp_run = await hub.store.run(result['run_id'])
        self.assertEqual(comp_run['runtime_kind'], 'pi')
        self.assertEqual((await hub.store.run('r1'))['runtime_kind'], 'langgraph')
        self.assertIsNotNone((await hub.store.run('r1'))['compensated_at'])
        actions = await hub.store.actions_of(result['run_id'])
        self.assertEqual(actions[0]['step_id'], 'undo:s1')
        self.assertEqual(actions[0]['status'], 'succeeded')
        self.assertEqual(result['steps'][0]['status'], 'succeeded')

        async def boom(run_id):
            raise AssertionError('脚本化 Pi run 误走了图的 _resume_plan')

        with patch.object(hub, '_resume_plan', boom):
            resumed = await hub._resume_for_engine(result['run_id'])
        self.assertIn('pi_resume', resumed)
        saved = resumed['pi_resume']['snapshot']['host_result_state']['script']
        self.assertTrue(saved['scripted'])
        self.assertEqual(saved['kind'], 'compensation')
        self.assertNotIn('step_index', resumed)

    async def test_compensate_pi_run_uses_frozen_action_receipt(self):
        from webpage_agent.api.api_hub import Hub
        from webpage_agent.api import main as main_module
        calls = []
        undo = _tool('delete_site', effect='destructive', calls=calls, output={'removed': True})
        hub = Hub(new_store())
        await hub.store.create_task('t1', 's1', '发布页面')
        await hub.store.ensure_task_meta('t1', session_id='s1', goal='发布页面')
        await hub.store.create_run('r1', 't1', status='succeeded', runtime_kind='pi')
        receipt = {
            'site_id': 'site-9',
            'compensate': {'tool': 'delete_site', 'args': {'site_id': 'site-9'},
                           'tool_key': 'local:test:delete_site',
                           'effect': 'destructive'},
        }
        async with hub.store._lease() as conn:
            await conn.execute(
                'INSERT INTO actions (action_id, run_id, plan_version, step_id, '
                'tool_key, args_hash, idempotency_key, effect, status, receipt_json, '
                'authorized_by, created_at, updated_at) VALUES '
                "(%s,%s,1,%s,%s,%s,%s,'write','succeeded',%s,'',%s,%s)",
                ('act-ok', 'r1', 'pi:call:1', 'local:test:deploy_site', 'h1',
                 'idem-ok', json.dumps(receipt), '2020-01-02T00:00:00+00:00',
                 '2020-01-02T00:00:00+00:00'))
            await conn.execute(
                'INSERT INTO actions (action_id, run_id, plan_version, step_id, '
                'tool_key, args_hash, idempotency_key, effect, status, receipt_json, '
                'authorized_by, created_at, updated_at) VALUES '
                "(%s,%s,1,%s,%s,%s,%s,'write','unknown',%s,'',%s,%s)",
                ('act-unknown', 'r1', 'pi:call:2', 'local:test:other', 'h2',
                 'idem-unknown', json.dumps(receipt), '2020-01-03T00:00:00+00:00',
                 '2020-01-03T00:00:00+00:00'))
            await conn.commit()

        undo_steps = await hub.compensation_steps('r1')
        self.assertEqual([step['step_id'] for step in undo_steps], ['undo:pi:call:1'])
        self.assertEqual(undo_steps[0]['args'], {'site_id': 'site-9'})

        async def fake_find(name, context=None):
            return undo if name == 'delete_site' else None

        with patch('webpage_agent.tools.core.find_tool', fake_find), \
                patch.object(main_module, 'check_runtime_keys', lambda: None), \
                override_settings(tool_policy_enforced=False):
            result = await hub.compensate('r1')
        self.assertEqual([item[0] for item in calls], ['delete_site'])
        self.assertEqual(calls[0][1], {'site_id': 'site-9'})
        self.assertEqual((await hub.store.run(result['run_id']))['runtime_kind'], 'pi')
        self.assertEqual((await hub.store.run('r1'))['runtime_kind'], 'pi')
        self.assertIsNotNone((await hub.store.run('r1'))['compensated_at'])
