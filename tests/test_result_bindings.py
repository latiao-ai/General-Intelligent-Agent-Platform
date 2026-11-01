import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.agent import nodes
from webpage_agent.observe import observation
from webpage_agent.tools import core as tools
from webpage_agent.capabilities.base import ToolError
from webpage_agent.capabilities.registry import Tool
from webpage_agent.core.schemas import ObservationDecision


OUTPUT = {'type': 'object', 'additionalProperties': False, 'properties': {
    'result': {'type': 'object', 'additionalProperties': False, 'properties': {
        'created': {'type': 'array', 'items': {'type': 'object',
            'additionalProperties': False, 'properties': {'id': {'type': 'string'}}}},
        'revision': {'type': 'integer'}}}}}


class ResultBindingTests(unittest.IsolatedAsyncioTestCase):
    def test_declared_output_rejects_wrong_field_before_execution(self):
        create = Tool('create', 'create', '', {}, AsyncMock(), output_schema=OUTPUT)
        update = Tool('update', 'update', '', {}, AsyncMock())
        steps = [{'step_id': 's1', 'tool': 'create'}, {'step_id': 's2', 'tool': 'update',
            'depends_on': ['s1'], 'args': {'nodeId': {'ref': {'step_id': 's1', 'pointer': '/result/nodes/0/id'}}}}]
        self.assertIn('created', tools.describe_tool(create))
        self.assertEqual(nodes.validate_plan(steps, [create, update])[1][0]['code'], 'bad_output_reference')
        steps[1]['args']['nodeId']['ref']['pointer'] = '/result/created/0/id'
        self.assertEqual(nodes.validate_plan(steps, [create, update])[1], [])
        self.assertEqual(nodes.validate_plan(steps[1:], [create, update], completed_steps=[steps[0]])[1], [])
        # No declared contract is not evidence that a path is absent.
        steps[1]['args']['nodeId']['ref']['pointer'] = '/result/vendor_field/0/id'
        self.assertEqual(nodes.validate_plan(steps, [replace(create, output_schema=None), update])[1], [])

    async def test_real_created_receipt_repairs_remaining_step_without_recreating(self):
        calls = []
        async def create(args, state):
            calls.append(('create', args))
            return {'payload': {'result': {'created': [{'id': 'existing-node'}], 'revision': 5}}}
        async def update(args, state):
            calls.append(('update', args))
            return {'payload': {'ok': True}}
        async def reply(args, state):
            return {'answer': '已写入提示词'}
        catalog = [Tool('create', '创建', '', {}, create, tool_key='mcp:a:create', effect='write', server_id='a'),
                   Tool('update', '提示词', '', {}, update, tool_key='mcp:a:update', effect='write', server_id='a'),
                   Tool('reply', '回答', '', {}, reply, tool_key='local:answers:direct', effect='read')]
        steps = [{'step_id': 's1', 'tool': 'create', 'args': {}, 'depends_on': []},
                 {'step_id': 's2', 'tool': 'update', 'depends_on': ['s1'], 'args': {
                     'nodeId': {'ref': {'step_id': 's1', 'pointer': '/result/nodes/0/id'}},
                     'expectedRevision': {'ref': {'step_id': 's1', 'pointer': '/result/revision'}}, 'prompt': '故宫提示词'}},
                 {'step_id': 's3', 'tool': 'reply', 'args': {}, 'depends_on': ['s2']}]
        store = SimpleNamespace(pending_run_inputs=AsyncMock(return_value=[]),
            run_intent=AsyncMock(return_value={'intent_version': 1}),
            observation_state=AsyncMock(return_value={'observation_seq': 1}),
            reserve_decision=AsyncMock(return_value={'decision_id': 'decision-1'}), finish_decision=AsyncMock())
        repaired = copy.deepcopy(steps[1:])
        repaired[0]['args']['nodeId']['ref']['pointer'] = '/result/created/0/id'
        decision = ObservationDecision(kind='revise', steps=repaired)
        with tempfile.TemporaryDirectory() as folder:
            state = {'artifact_dir': folder, 'run_id': 'run-1', 'step_index': 0, 'step_outputs': {},
                'intent_version': 1, 'status': 'planned', 'plan_version': 1,
                'plan': {'version': 1, 'steps': steps, 'result_locus': 'platform_change',
                    'after_segment': 'verify', 'completion_criteria': [{'kind': 'tool_succeeded', 'target': 's2'}]}}
            lookup = {tool.name: tool for tool in catalog}
            with patch.object(nodes, '_tool_by_name', AsyncMock(side_effect=lambda name: lookup[name])), \
                 patch.object(nodes, '_check_boundary', AsyncMock()), \
                 patch.object(nodes, '_tool_context', AsyncMock(return_value=None)), \
                 patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'run-1'))), \
                 patch.object(tools, 'catalog', AsyncMock(return_value=catalog)), \
                 patch.object(nodes, 'structured_call', AsyncMock(return_value=(decision, {}))) as model:
                state.update(await nodes.run_step(state))
                state.update(await nodes.run_step(state))
                self.assertEqual([name for name, _ in calls], ['create'])
                self.assertEqual(state['errors'][0]['certainty'], 'not_executed')
                repair_context = nodes._reference_repair_context(state)
                self.assertIn('/result/created/0/id', repair_context['真实结果可用路径']['s1'])
                signals = observation.collect_observation(state=state, plan=state['plan'], step_index=state['step_index'])
                self.assertEqual(observation.route_after_step(signals)[0], observation.ROUTE_DECIDE)
                for mutate in ('recreate', 'change_prompt', 'change_source', 'missing_path', 'add_compensation', 'change_valid_ref'):
                    bad = copy.deepcopy(repaired)
                    if mutate == 'recreate': bad.insert(0, {**steps[0], 'step_id': 'new-create'})
                    if mutate == 'change_prompt': bad[0]['args']['prompt'] = 'different'
                    if mutate == 'change_source': bad[0]['args']['nodeId']['ref']['step_id'] = 'other'
                    if mutate == 'missing_path': bad[0]['args']['nodeId']['ref']['pointer'] = '/missing'
                    if mutate == 'add_compensation': bad[0]['compensate'] = {'tool': 'create', 'args': {}}
                    if mutate == 'change_valid_ref': bad[0]['args']['expectedRevision']['ref']['pointer'] = '/result/created/0/id'
                    self.assertTrue(nodes._reference_revision_issues(state, bad), mutate)
                with patch.object(nodes, 'settings', replace(nodes.settings, run_max_repairs=0)):
                    self.assertEqual((await nodes.decide_next(state))['status'], 'waiting_input')
                    model.assert_not_awaited()
                candidate = await nodes.decide_next(state)
                self.assertEqual(candidate['next_route'], 'persist_plan')
                self.assertEqual(candidate['plan_version'], 2)
                self.assertTrue(candidate['errors'][0]['resolved'])
                state.update(candidate)
                while state['step_index'] < len(state['plan']['steps']):
                    state.update(await nodes.run_step(state))
                self.assertEqual(calls, [('create', {}), ('update', {'nodeId': 'existing-node', 'expectedRevision': 5, 'prompt': '故宫提示词'})])
                self.assertEqual(state['answer'], '已写入提示词')
                self.assertFalse(observation.collect_observation(state=state, plan=state['plan'], step_index=state['step_index'])['repairable_errors'])

    def test_missing_or_ambiguous_results_do_not_get_guessed(self):
        with self.assertRaises(ToolError) as raised:
            nodes.resolve_inputs({'step_id': 's2', 'args': {'nodeId': {'ref': {'step_id': 's1', 'pointer': '/nodes/0/id'}}}}, {})
        self.assertEqual(raised.exception.error_code, 'E_INPUT_REFERENCE')
        self.assertEqual(raised.exception.certainty, 'not_executed')
        with tempfile.TemporaryDirectory() as folder:
            receipt = Path(folder) / 'receipt.json'
            receipt.write_text(json.dumps({'result': {'a': {'id': 'first'}, 'b': {'id': 'second'}}}))
            outputs = {'s1': {'tool_result': {'data_ref': 'receipt.json'}}}
            step = {'step_id': 's2', 'args': {'nodeId': {'ref': {'step_id': 's1', 'pointer': '/id'}}}}
            with self.assertRaisesRegex(ToolError, '歧义'):
                nodes.resolve_inputs(step, outputs, folder)
            step['args']['nodeId']['ref']['pointer'] = '/result/a/id'
            self.assertEqual(nodes.resolve_inputs(step, outputs, folder)['nodeId'], 'first')

    async def test_repaired_plan_updates_live_ledger_version_only_after_commit(self):
        store = SimpleNamespace(commit_plan=AsyncMock(return_value={'intent_version': 1}))
        book = SimpleNamespace(store=store, plan_version=1, context=None, run_id='r')
        state = {'plan': {'version': 2, 'base_plan_version': 1, 'intent_version': 1,
                          'steps': [{'step_id': 's2', 'tool': 'update'}]}}
        with patch.object(nodes.ledger_module, 'current_ledger', return_value=book):
            self.assertEqual((await nodes.persist_plan(state))['plan_version'], 2)
            self.assertEqual(book.plan_version, 2)
            book.plan_version = 1
            store.commit_plan.side_effect = nodes.execution_control.IntentChanged('r', 1, 2)
            self.assertTrue((await nodes.persist_plan(state))['intent_changed'])
            self.assertEqual(book.plan_version, 1)

