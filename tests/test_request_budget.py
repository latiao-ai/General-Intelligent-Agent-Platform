"""完整模型请求预算与规划故障恢复；真实 PG、受控模型，不发外部请求。"""
import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIConnectionError, AuthenticationError, RateLimitError
from pydantic import BaseModel

from app import context, graph, nodes, services
from app.api_hub import Hub
from app.capabilities.registry import Tool
from app.config import settings
from app.schemas import ExecutionPlan, RevisedPlan
from tests.exec_env import (Remote, install_main, install_tools, new_store,
                            reset_db, seed_run)


class Tiny(BaseModel):
    value: str


class StubModel:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def bind_tools(self, *args, **kwargs):
        return self

    async def astream(self, messages):
        self.calls.append(copy.deepcopy(messages))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for item in outcome:
            if isinstance(item, Exception):
                raise item
            yield item


def chunk(args):
    return SimpleNamespace(tool_calls=[{'name': 'Tiny', 'args': args}],
                           tool_call_chunks=[], usage_metadata={'total_tokens': 3})


class CompleteBudgetTests(unittest.TestCase):
    def limits(self, **values):
        return replace(settings, llm_context_window=50000, llm_input_budget=2000,
                       llm_output_tokens=1000, llm_context_margin=100, **values)

    def test_entire_request_and_schema_are_counted_with_output_reserve(self):
        conversation, report = context.prepare_request('完整规则', [('user', '当前要求')],
            schema=Tiny, purpose='plan', settings=self.limits())
        self.assertGreater(report['sections']['tool_schema'], 0)
        self.assertGreater(report['input_tokens'], len('当前要求'.encode()))
        self.assertEqual(report['reserved_output'], 1000)
        self.assertFalse(report['over_budget'])
        self.assertEqual(conversation[-1], ('user', '当前要求'))
        limited = replace(settings, llm_context_window=1500, llm_input_budget=5000,
                          llm_output_tokens=1000, llm_context_margin=100)
        _, report = context.prepare_request('规则', [('user', '问题')], settings=limited)
        self.assertEqual(report['input_limit'], 400)

    def test_critical_request_is_never_silently_truncated(self):
        with self.assertRaises(context.ContextBudgetExceeded) as caught:
            context.prepare_request('权限规则', [('user', '必须保留' * 1000)],
                                    settings=self.limits())
        self.assertTrue(caught.exception.report['over_budget'])
        self.assertNotIn('必须保留', str(caught.exception.report))

    def test_old_history_can_be_removed_but_current_request_and_rules_remain(self):
        messages = [{'role': 'user', 'content': '旧历史' * 1000},
                    {'role': 'developer', 'content': '必须保留开发者规则'},
                    {'role': 'assistant', 'content': '较早回答'},
                    {'role': 'user', 'content': '修改选中对象'}]
        original = copy.deepcopy(messages)
        prepared, report = context.prepare_request('权限和系统规则', messages,
                                                   settings=self.limits())
        self.assertEqual(messages, original)
        self.assertEqual(prepared[-1][1], '修改选中对象')
        self.assertIn(('developer', '必须保留开发者规则'), prepared)
        self.assertIn('history_message', report['truncated'])
        self.assertIn('已省略', prepared[0][1])

    def test_embedded_history_summary_and_referenced_evidence_are_budgeted(self):
        payload = {'用户要求': '修改对象', '对话历史': [{'content': '旧话' * 2000}],
                   '会话记忆': '[会话摘要]' + '旧摘要' * 2000 + '[会话关键事实]\n必须竖屏',
                   '可用工具': '完整工具参数定义',
                   '已完成动作与结果': [{'step_id': 's1', 'execution_status': 'succeeded',
                       'evidence': ['result:abc'], 'summary': '长正文' * 2000}],
                   '完成条件现状': '必须有实际修改回执'}
        original = copy.deepcopy(payload)
        prepared, report = context.prepare_request('权限规则',
            [('user', json.dumps(payload, ensure_ascii=False))], schema=Tiny,
            purpose='observe', settings=self.limits())
        result = json.loads(prepared[-1][1])
        self.assertEqual(payload, original)
        self.assertIn('必须竖屏', result['会话记忆'])
        self.assertEqual(result['完成条件现状'], payload['完成条件现状'])
        self.assertEqual(result['已完成动作与结果'][0]['evidence'], ['result:abc'])
        self.assertTrue(result['已完成动作与结果'][0]['summary']['omitted'])
        self.assertIn('referenced_result_summary', report['truncated'])
        self.assertIn('tools', report['components'])
        self.assertIn('上下文预算说明', result)

    def test_evidence_without_full_reference_cannot_be_discarded(self):
        payload = {'用户要求': '修改对象', '已完成动作与结果': [{'summary': '证据' * 5000}]}
        with self.assertRaises(context.ContextBudgetExceeded):
            context.prepare_request('权限', [('user', json.dumps(payload, ensure_ascii=False))],
                                    schema=Tiny, settings=self.limits())

    def test_large_tool_directory_is_not_silently_cut(self):
        payload = {'用户要求': '查询', '可用工具': '必须完整的工具定义' * 1000}
        with self.assertRaises(context.ContextBudgetExceeded) as caught:
            context.prepare_request('规则', [('user', json.dumps(payload, ensure_ascii=False))],
                                    schema=Tiny, settings=self.limits())
        self.assertGreater(caught.exception.report['components']['tools'], 0)

    def test_legacy_section_builder_reports_total_overflow(self):
        self.assertTrue(context.build(purpose='plan', request='要求' * 5000)['report']['over_budget'])


class ModelBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_run_budget_never_sends_and_is_known_not_executed(self):
        from app.pg_store import BudgetExceeded
        from app.errors import as_outcome
        model = StubModel([[chunk({'value': 'unused'})]])
        token = services.bind_budget(AsyncMock(side_effect=BudgetExceeded()))
        try:
            with patch.object(services, 'new_llm', return_value=model):
                with self.assertRaises(BudgetExceeded) as caught:
                    await services.structured_call(Tiny, '规则', '要求')
            self.assertEqual(model.calls, [])
            self.assertEqual(as_outcome(caught.exception).certainty, 'not_executed')
        finally:
            services.unbind_budget(token)

    async def test_observation_preflight_rejection_does_not_consume_provider_budget(self):
        old = {'version': 1, 'steps': [], 'result_locus': 'platform_change'}
        store = SimpleNamespace(
            pending_run_inputs=AsyncMock(return_value=[]),
            run_intent=AsyncMock(return_value={'intent_version': 1}),
            observation_state=AsyncMock(return_value={'observation_seq': 1}),
            reserve_decision=AsyncMock(return_value={'decision_id': 'd1'}),
            consume_budget=AsyncMock(), finish_decision=AsyncMock())
        budget = AsyncMock()
        token = services.bind_budget(budget)
        try:
            with patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'r1'))), \
                    patch.object(nodes.execution_control, 'require', return_value=None), \
                    patch.object(services, 'settings', replace(settings, llm_input_budget=1)), \
                    patch.object(services, 'new_llm') as factory:
                result = await nodes.decide_next({'plan': old, 'user_input': '修改对象'})
            self.assertEqual(result['status'], 'waiting_input')
            self.assertEqual(result['plan'], old)
            self.assertEqual(result['planning_failure']['code'], 'E_CONTEXT_LIMIT')
            budget.assert_not_awaited()
            store.consume_budget.assert_not_awaited()
            factory.assert_not_called()
        finally:
            services.unbind_budget(token)

    async def test_structured_overflow_makes_zero_provider_requests(self):
        budget = AsyncMock()
        token = services.bind_budget(budget)
        try:
            with patch.object(services, 'settings', replace(settings, llm_input_budget=50)), \
                    patch.object(services, 'new_llm') as factory, \
                    patch.object(services, 'emit') as emit:
                with self.assertRaises(context.ContextBudgetExceeded):
                    await services.structured_call(Tiny, '完整规则' * 100, '当前要求')
            factory.assert_not_called()
            budget.assert_not_awaited()
            self.assertTrue(emit.call_args.args[0]['report']['over_budget'])
        finally:
            services.unbind_budget(token)

    async def test_chat_overflow_is_also_blocked_before_provider(self):
        with patch.object(services, 'settings', replace(settings, llm_input_budget=50)), \
                patch.object(services, 'new_llm') as factory:
            with self.assertRaises(context.ContextBudgetExceeded):
                await services.chat_reply('完整规则', [{'role': 'user', 'content': '资料' * 1000}])
        factory.assert_not_called()

    async def test_schema_repair_preserves_original_json_and_is_budgeted_each_time(self):
        model = StubModel([[chunk({})], [chunk({'value': 'ok'})]])
        budget, reports = AsyncMock(), []
        token = services.bind_budget(budget)
        original = {'用户要求': '修改对象', '按序补充': ['改成红色'], '可用工具': '唯一工具'}
        try:
            with patch.object(services, 'new_llm', return_value=model), \
                    patch.object(services, 'emit', side_effect=lambda event: reports.append(event)):
                value, usage = await services.structured_call(Tiny, '不能扩大权限',
                    json.dumps(original, ensure_ascii=False))
            self.assertEqual(value.value, 'ok')
            repaired = json.loads(model.calls[1][-1][1])
            for key, value in original.items():
                self.assertEqual(repaired[key], value)
            self.assertIn('结构化修复要求', repaired)
            self.assertEqual(budget.await_count, 2)
            self.assertEqual(len(reports), 2)
            self.assertEqual(usage['total_tokens'], 6)
        finally:
            services.unbind_budget(token)

    async def test_retry_attempt_is_rechecked_and_can_be_blocked(self):
        model = StubModel([[chunk({})]])
        original = context.prepare_request
        calls = []
        def prepare(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                kwargs['settings'] = replace(settings, llm_input_budget=1)
            return original(*args, **kwargs)
        with patch.object(services, 'new_llm', return_value=model), \
                patch.object(context, 'prepare_request', side_effect=prepare):
            with self.assertRaises(context.ContextBudgetExceeded):
                await services.structured_call(Tiny, '规则', '用户要求')
        self.assertEqual(len(model.calls), 1)

    async def test_transient_connection_error_retries_with_individual_accounting(self):
        model = StubModel([APIConnectionError(request=httpx.Request('POST', 'https://invalid')),
                           [chunk({'value': 'ok'})]])
        budget = AsyncMock()
        token = services.bind_budget(budget)
        try:
            with patch.object(services, 'new_llm', return_value=model), \
                    patch.object(services.asyncio, 'sleep', AsyncMock()):
                result, _ = await services.structured_call(Tiny, '规则', '要求')
            self.assertEqual(result.value, 'ok')
            self.assertEqual(budget.await_count, 2)
        finally:
            services.unbind_budget(token)

    async def test_retry_after_is_respected_or_request_is_deferred(self):
        for retry_after, expected_calls in [('0', 2), ('120', 1)]:
            error = RateLimitError('secret-provider-details', body={}, response=httpx.Response(
                429, headers={'Retry-After': retry_after},
                request=httpx.Request('POST', 'https://invalid')))
            model = StubModel([error, [chunk({'value': 'ok'})]])
            with self.subTest(retry_after=retry_after), \
                    patch.object(services, 'new_llm', return_value=model), \
                    patch.object(services.asyncio, 'sleep', AsyncMock()):
                if expected_calls == 1:
                    with self.assertRaises(services.ModelRequestError) as caught:
                        await services.structured_call(Tiny, '规则', '要求')
                    self.assertNotIn('secret-provider-details', str(caught.exception))
                else:
                    await services.structured_call(Tiny, '规则', '要求')
                self.assertEqual(len(model.calls), expected_calls)

    async def test_authentication_failure_is_not_retried_or_exposed(self):
        error = AuthenticationError('SECRET_TOKEN', body={}, response=httpx.Response(
            401, request=httpx.Request('POST', 'https://invalid')))
        model = StubModel([error])
        with patch.object(services, 'new_llm', return_value=model):
            with self.assertRaises(services.ModelConfigurationError) as caught:
                await services.structured_call(Tiny, '规则', '要求')
        self.assertNotIn('SECRET_TOKEN', str(caught.exception))
        self.assertEqual(len(model.calls), 1)

    async def test_partial_chat_is_not_transparently_restarted(self):
        error = APIConnectionError(request=httpx.Request('POST', 'https://invalid'))
        model = StubModel([[SimpleNamespace(content='一半回答'), error]])
        deltas, budget = [], AsyncMock()
        token = services.bind_budget(budget)
        try:
            with patch.object(services, 'new_llm', return_value=model):
                with self.assertRaises(services.ModelRequestError):
                    await services.chat_reply('规则', [{'role': 'user', 'content': '要求'}],
                                              on_delta=deltas.append)
            self.assertEqual(deltas, ['一半回答'])
            self.assertEqual(len(model.calls), 1)
            self.assertEqual(budget.await_count, 1)
        finally:
            services.unbind_budget(token)

    def test_sdk_retry_is_disabled_and_actual_output_limit_matches_reserve(self):
        with patch.object(services, 'ChatOpenAI') as constructor:
            services.new_llm()
        self.assertEqual(constructor.call_args.kwargs['max_retries'], 0)
        self.assertEqual(constructor.call_args.kwargs['max_tokens'], settings.llm_output_tokens)


class PlanningRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        self.addAsyncCleanup(self.store.close)
        await seed_run(self.store, message='修改平台对象')

    async def test_initial_model_failure_stops_without_tools_and_resumes_same_run(self):
        from scripts.run_worker import run_once
        remote = Remote()
        available = {'answer_directly': remote.tool('answer_directly', effect='read',
                                                     output={'answer': '已完成'})}
        with tempfile.TemporaryDirectory() as folder, install_main(folder), install_tools(available):
            hub = Hub(self.store)
            with patch.object(nodes, 'structured_call', AsyncMock(side_effect=services.ModelRequestError())):
                await run_once(hub, 'first', run_id='run1')
            self.assertEqual((await self.store.run('run1'))['status'], 'interrupted')
            self.assertEqual(await self.store.actions_of('run1'), [])
            snapshot = await self.store.run_snapshot('run1')
            self.assertEqual(snapshot['state']['planning_failure']['code'], 'E_MODEL_UNAVAILABLE')
            self.assertIn('稍后恢复', (await self.store.task('task1'))['state']['failure'])
            ready = ExecutionPlan(steps=[{'tool': 'answer_directly', 'step_id': 's1'}], result_locus='chat')
            with patch.object(nodes, 'structured_call', AsyncMock(return_value=(ready, {}))):
                resumed = await Hub(self.store).resume('run1')
                await asyncio.wait_for(resumed._task, timeout=20)
            self.assertEqual((await self.store.run('run1'))['status'], 'succeeded')
            self.assertEqual(remote.name_count('answer_directly'), 1)

    async def test_oversized_initial_plan_never_sends_provider_or_tools(self):
        from scripts.run_worker import run_once
        remote = Remote()
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'read': remote.tool('read', effect='read')}), \
                patch.object(services, 'settings', replace(settings, llm_input_budget=100)), \
                patch.object(services, 'new_llm') as factory:
            await run_once(Hub(self.store), 'worker', run_id='run1')
        run = await self.store.run('run1')
        self.assertEqual(run['status'], 'waiting_input')
        self.assertEqual(run['consumed'].get('provider_requests', 0), 0)
        self.assertEqual(await self.store.actions_of('run1'), [])
        factory.assert_not_called()
        events = [e for _, e in await self.store.events('task1')]
        self.assertTrue(any(e.get('type') == 'context_budget' for e in events))
        self.assertFalse(any('默认计划' in str(e) for e in events))

    async def test_answer_budget_failure_preserves_successful_write_without_compensation(self):
        from scripts.run_worker import run_once
        remote = Remote()
        async def answer(args, state):
            return {'answer': await services.chat_reply('完整回答规则',
                    [{'role': 'user', 'content': '长资料' * 1000}])}
        available = {'write': remote.tool('write', effect='write', output={'object_id': 'obj1'}),
                     'undo': remote.tool('undo', effect='write'),
                     'answer': Tool('answer', '回答', '测试', {'type': 'object'}, answer,
                                    tool_key='local:test:answer', effect='read')}
        plan = ExecutionPlan(steps=[{'tool': 'write', 'step_id': 's1',
                                     'compensate': {'tool': 'undo', 'args': {}}},
                                    {'tool': 'answer', 'step_id': 's2'}])
        with tempfile.TemporaryDirectory() as folder, install_main(folder), install_tools(available), \
                patch.object(nodes, 'structured_call', AsyncMock(return_value=(plan, {}))), \
                patch.object(services, 'settings', replace(settings, llm_input_budget=100)), \
                patch.object(services, 'new_llm') as factory:
            await run_once(Hub(self.store), 'worker', run_id='run1')
        self.assertEqual((await self.store.run('run1'))['status'], 'waiting_input')
        self.assertEqual(remote.name_count('write'), 1)
        self.assertEqual(remote.name_count('undo'), 0)
        factory.assert_not_called()
        snapshot = (await self.store.run_snapshot('run1'))['state']
        self.assertEqual(snapshot['step_outputs']['s1']['__status__'], 'succeeded')
        self.assertEqual(snapshot['planning_failure']['code'], 'E_CONTEXT_LIMIT')

    async def test_revision_model_failure_retains_plan_and_exits_before_execution(self):
        old = {'steps': [{'tool': 'write', 'step_id': 's1'}], 'result_locus': 'platform_change'}
        store = SimpleNamespace(run_intent=AsyncMock(return_value={'intent_version': 2}),
            pending_run_inputs=AsyncMock(return_value=[{'input_seq': 1, 'message': '改红色'}]),
            actions_of=AsyncMock(return_value=[]), plan=AsyncMock(return_value=old),
            run_snapshot=AsyncMock(return_value={'state': {'plan': old}}))
        with patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'run1'))), \
                patch.object(nodes, '_tool_context', AsyncMock(return_value=None)), \
                patch.object(nodes.tool_module, 'catalog', AsyncMock(return_value=[])), \
                patch.object(nodes, 'structured_call', AsyncMock(side_effect=services.ModelRequestError())):
            result = await nodes.revise_plan({'plan': old, 'user_input': '修改对象'})
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['plan'], old)
        self.assertEqual(graph._next_after_revise(result), 'end')
        self.assertTrue(result['pending_inputs'])

    async def test_revision_catalog_does_not_silently_drop_tools_after_24(self):
        available = [Tool('tool_' + str(i), '工具', '描述', {'type': 'object'}, None,
                          effect='read') for i in range(30)]
        store = SimpleNamespace(run_intent=AsyncMock(return_value={'intent_version': 2}),
            pending_run_inputs=AsyncMock(return_value=[]), actions_of=AsyncMock(return_value=[]),
            plan=AsyncMock(return_value={}), run_snapshot=AsyncMock(return_value={}))
        captured = []
        async def call(schema, system, user, **kwargs):
            captured.append(json.loads(user))
            return RevisedPlan(steps=[{'tool': 'tool_29', 'step_id': 'p2_s1'}]), {}
        with patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'run1'))), \
                patch.object(nodes, '_tool_context', AsyncMock(return_value=None)), \
                patch.object(nodes.tool_module, 'catalog', AsyncMock(return_value=available)), \
                patch.object(nodes, 'structured_call', call):
            result = await nodes.revise_plan({'user_input': '查询'})
        self.assertEqual(len(captured[0]['可用工具']), 30)
        self.assertEqual(result['plan']['steps'][0]['tool'], 'tool_29')
