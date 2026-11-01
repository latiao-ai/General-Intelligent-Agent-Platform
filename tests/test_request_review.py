import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.agent import nodes
from webpage_agent.governance import request_review
from webpage_agent.core.schemas import ExecutionPlan, RequestReview


class RequestReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_segment_is_reviewed_without_demanding_unapproved_execution(self):
        call = AsyncMock(return_value=(RequestReview(complete=True), {}))
        issues, _ = await request_review.review_plan(call,
            question='生成一张图片', scene='当前画布',
            steps=[{'tool': 'prepare'}, {'tool': 'read'}, {'tool': 'propose'}],
            catalog=[], after_segment='observe',
            observation_goal='核对方案已登记，等待用户在平台确认后继续生成',
            completion_criteria=[{'kind': 'tool_succeeded', 'target': 'propose'}])
        self.assertEqual(issues, [])
        payload = json.loads(call.call_args.args[2])
        self.assertEqual(payload['after_segment'], 'observe')
        self.assertIn('等待用户', payload['observation_goal'])
        self.assertEqual(payload['完成条件'][0]['target'], 'propose')
        self.assertIn('不能因为本片段尚未执行最终付费动作而拒绝', call.call_args.args[1])

    async def test_observation_segment_without_a_concrete_goal_is_blocked(self):
        call = AsyncMock()
        issues, _ = await request_review.review_plan(call,
            question='生成图片', scene='', steps=[{'tool': 'prepare'}],
            catalog=[], after_segment='observe')
        self.assertEqual(issues[0]['code'], 'missing_observation_goal')
        call.assert_not_awaited()

    async def test_final_plan_still_requires_full_request_coverage(self):
        call = AsyncMock(return_value=(RequestReview(complete=False,
            unmet_requirements=['只有方案，没有最终图片']), {}))
        issues, _ = await request_review.review_plan(call,
            question='生成图片', scene='', steps=[{'tool': 'propose'}], catalog=[])
        self.assertEqual(issues[0]['code'], 'request_not_covered')
        self.assertEqual(json.loads(call.call_args.args[2])['after_segment'], 'verify')

    @staticmethod
    def canvas_catalog(output_schema=True):
        """一张最小画布工具表：create 的 expectedRevision 是必填，回执带 revision。"""
        from webpage_agent.tools import core as tools
        run = AsyncMock()
        create = tools.Tool(name='mcp__studio__canvas.nodes.create', label='建节点',
                            description='带 revision CAS 批量建节点', run=run,
                            args={'type': 'object', 'required': ['canvasId', 'nodes', 'expectedRevision'],
                                  'properties': {'canvasId': {'type': 'string'},
                                                 'expectedRevision': {'type': 'integer'},
                                                 'nodes': {'type': 'array'}}},
                            tool_key='mcp:studio:canvas.nodes.create',
                            server_id='studio', effect='write',
                            output_schema={'type': 'object',
                                           'properties': {'revision': {'type': 'integer'},
                                                          'result': {'type': 'object',
                                                                     'properties': {'revision': {'type': 'integer'}}}}} if output_schema else None)
        read = tools.Tool(name='mcp__studio__canvas.get', label='读画布',
                          description='读取画布', run=run,
                          args={'type': 'object', 'properties': {'canvasId': {'type': 'string'}}},
                          tool_key='mcp:studio:canvas.get', server_id='studio', effect='read',
                          output_schema={'type': 'object',
                                         'properties': {'result': {'type': 'object',
                                                                   'properties': {'canvas': {'type': 'object',
                                                                                             'properties': {'revision': {'type': 'integer'}}}}}}} if output_schema else None)
        return [read, create]

    async def test_reviewer_cannot_call_a_declared_field_fabricated(self):
        """真踩过：expectedRevision 是必填参数，审查器却判它编造，两轮否掉正确计划后静默停下。"""
        catalog = self.canvas_catalog()
        steps = [{'tool': 'mcp__studio__canvas.get', 'step_id': 's1'},
                 {'tool': 'mcp__studio__canvas.nodes.create', 'step_id': 's2',
                  'args': {'expectedRevision': {'ref': {'step_id': 's1',
                                                        'pointer': '/result/canvas/revision'}}}}]
        call = AsyncMock(return_value=(RequestReview(complete=False, unmet_requirements=[
            '计划依赖的 revision 字段在 canvas.get 回执中不存在：现场 JSON 无 revision 字段，'
            's2 用 /result/canvas/revision 作为 expectedRevision，属编造字段，会导致写步骤失败']), {}))
        issues, _ = await request_review.review_plan(call, question='生成视频', scene='',
                                                     steps=steps, catalog=catalog)
        self.assertEqual([i['code'] for i in issues], ['review_claim_contradicts_contract'])
        self.assertEqual(issues[0]['severity'], 'warning')
        payload = json.loads(call.call_args.args[2])
        self.assertEqual(payload['候选工具'][1]['inputSchema']['properties']['expectedRevision'],
                         {'type': 'integer'})

    async def test_rejection_of_an_undeclared_field_still_blocks(self):
        catalog = self.canvas_catalog()
        steps = [{'tool': 'mcp__studio__canvas.nodes.create', 'step_id': 's1'}]
        call = AsyncMock(return_value=(RequestReview(complete=False, unmet_requirements=[
            '计划用了 ghostBias 字段，该字段在工具合同里不存在，属编造参数']), {}))
        issues, _ = await request_review.review_plan(call, question='建节点', scene='',
                                                     steps=steps, catalog=catalog)
        self.assertEqual(issues[0]['code'], 'request_not_covered')
        self.assertEqual(issues[0]['severity'], 'error')

    async def test_rejection_without_declared_output_schema_stays(self):
        """没有声明输出结构时不能替审查器说话：驳回保留，交给真实读取去证。"""
        catalog = self.canvas_catalog(output_schema=False)
        steps = [{'tool': 'mcp__studio__canvas.nodes.create', 'step_id': 's1'}]
        call = AsyncMock(return_value=(RequestReview(complete=False, unmet_requirements=[
            'revision 字段不存在，写步骤的 expectedRevision 属编造']), {}))
        issues, _ = await request_review.review_plan(call, question='建节点', scene='',
                                                     steps=steps, catalog=catalog)
        self.assertIn('request_not_covered', [i['code'] for i in issues])

    async def test_planner_preserves_stage_metadata_for_review_and_execution(self):
        from webpage_agent.tools import core as tools
        run = AsyncMock()
        catalog = [tools.Tool(name='propose', label='准备方案', description='登记方案并等待用户确认',
                    args={}, run=run, tool_key='mcp:studio:propose',
                    server_id='studio', effect='read')]
        captured = {}

        async def model(schema, system, user, **kwargs):
            if schema is ExecutionPlan:
                return ExecutionPlan(steps=[{'tool': 'propose', 'step_id': 's1'}],
                    result_locus='platform_change', after_segment='observe',
                    observation_goal='等待平台用户确认，再继续生成'), {}
            captured.update(json.loads(user))
            return RequestReview(complete=True), {}

        with patch.object(tools, 'catalog', AsyncMock(return_value=catalog)), \
                patch.object(nodes, 'structured_call', model):
            result = await nodes.plan({'user_input': '生成图片', 'usage': [],
                'scene': {'verified': True, 'context': {'id': 'test'}}})
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(captured['after_segment'], 'observe')
        self.assertEqual(result['plan']['observation_goal'], captured['observation_goal'])
        run.assert_not_awaited()

    def test_readonly_preparation_cannot_be_used_as_final_platform_change(self):
        tool = SimpleNamespace(name='read', tool_key='mcp:studio:read',
                               effect='read', server_id='studio', scope='')
        steps = [{'tool': 'read', 'step_id': 's1'}]
        self.assertFalse(nodes.check_result_locus('platform_change', steps, [tool],
                                                 after_segment='observe'))
        issues = nodes.check_result_locus('platform_change', steps, [tool])
        self.assertEqual(issues[0]['code'], 'platform_change_needs_write')
        self.assertFalse(nodes.check_result_locus('platform_change', steps, [tool],
                                                  completed_write_servers={'studio'}))
        self.assertTrue(nodes.check_result_locus('platform_change', steps, [tool],
                                                 completed_write_servers={'other'}))

    async def test_external_confirmation_input_never_consumes_local_approval(self):
        from webpage_agent.api.api_hub import Hub
        hub = Hub.__new__(Hub)
        hub.store = SimpleNamespace(approvals_for=AsyncMock(return_value=[]))
        run = {'run_id': 'external', 'tenant_id': 'tenant', 'status': 'waiting_approval'}
        self.assertTrue(await hub._accepts_input_continuation(run))
        hub.store.approvals_for.return_value = [{'run_id': 'external', 'status': 'pending'}]
        self.assertFalse(await hub._accepts_input_continuation(run))
        self.assertFalse(await hub._accepts_input_continuation({**run, 'status': 'paused'}))

    async def test_confirmation_replan_sends_only_selected_schemas_and_keeps_prior_effects(self):
        from webpage_agent.tools import core as tools
        from webpage_agent.core.schemas import RevisedPlan
        read = tools.Tool(name='read_task', label='读取任务', description='读取真实任务状态',
                          args={}, run=AsyncMock(), tool_key='mcp:studio:read',
                          server_id='studio', effect='read')
        unrelated = tools.Tool(name='unrelated', label='其他', description='无关说明' * 30000,
                               args={}, run=AsyncMock(), tool_key='mcp:other:read',
                               server_id='other', effect='read')
        prior = {'step_id': 's1', 'tool_key': 'mcp:studio:prepare',
                 'status': 'succeeded', 'effect': 'write'}
        store = SimpleNamespace(run_intent=AsyncMock(return_value={'intent_version': 2}),
            pending_run_inputs=AsyncMock(return_value=[{'message': '已确认且平台已提交，请只查询 task-1'}]),
            actions_of=AsyncMock(return_value=[prior]),
            plan=AsyncMock(return_value={'version': 1, 'steps': []}))
        captured = {}

        async def model(schema, system, user, **kwargs):
            captured.update(json.loads(user))
            return RevisedPlan(steps=[{'tool': 'read_task', 'step_id': 'p2_s1'}],
                               result_locus='platform_change', after_segment='verify'), {}

        with patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'run1'))), \
                patch.object(tools, 'catalog', AsyncMock(return_value=[read, unrelated])), \
                patch.object(nodes, '_planning_catalog', AsyncMock(return_value=([read], {}))), \
                patch.object(nodes, 'structured_call', model):
            result = await nodes.revise_plan({'user_input': '生成图片', 'usage': [],
                'plan': {'result_locus': 'platform_change'}, 'actions_snapshot': [prior]})
        self.assertEqual(result['status'], 'planned')
        self.assertNotIn('无关说明', json.dumps(captured, ensure_ascii=False))
        self.assertEqual(result['plan']['steps'][0]['tool'], 'read_task')
        read.run.assert_not_awaited()
        unrelated.run.assert_not_awaited()

    async def test_final_verification_keeps_successful_receipts_from_previous_segments(self):
        state = {'plan': {'completion_criteria': [{'kind': 'tool_succeeded', 'target': 's1'}]},
                 'tool_results': [], 'actions_snapshot': [{'action_id': 'prior',
                     'step_id': 's1', 'tool_key': 'mcp:studio:prepare', 'status': 'succeeded'}]}
        self.assertEqual((await nodes.verify_completion(state))['status'], 'succeeded')
        state['actions_snapshot'][0]['status'] = 'unknown'
        self.assertEqual((await nodes.verify_completion(state))['status'], 'failed')

    async def test_plan_review_detects_omitted_part_of_compound_request(self):
        call = AsyncMock(return_value=(RequestReview(complete=False,
            unmet_requirements=['缺少修改当前对象名称的步骤']), {}))
        tools = [SimpleNamespace(name='add_items', effect='write', description='添加条目')]
        issues, _ = await request_review.review_plan(call,
            question='添加两个条目，并把当前对象名称改成福州', scene='当前对象 id=existing',
            steps=[{'tool': 'add_items', 'step_id': 's1'}], catalog=tools)
        self.assertEqual(issues[0]['code'], 'request_not_covered')
        payload = json.loads(call.call_args.args[2])
        self.assertIn('改成福州', payload['用户原始要求'])
        self.assertNotIn('BeefTV', call.call_args.args[1])

    def test_readback_must_follow_the_last_write_on_each_service(self):
        tools = [SimpleNamespace(name='read', server_id='crm', effect='read'),
                 SimpleNamespace(name='edit', server_id='crm', effect='write')]
        self.assertTrue(request_review.readback_issues([{'tool': 'read'}, {'tool': 'edit'}], tools))
        self.assertFalse(request_review.readback_issues([{'tool': 'edit'}, {'tool': 'read'}], tools))

    async def test_successful_tool_or_claimed_answer_cannot_hide_unmet_request(self):
        state = {'user_input': '添加两个条目并修改名称',
                 'plan': {'request_review_required': True},
                 'answer': '所有修改都已完成',
                 'mcp_calls': [{'tool': 'edit', 'payload': {'status': 'success'}}]}
        with patch.object(nodes, 'structured_call', AsyncMock(return_value=(
                RequestReview(complete=False, unmet_requirements=['名称仍未修改']), {}))):
            result = await nodes.verify_completion(state)
        self.assertEqual(result['status'], 'partially_succeeded')
        self.assertIn('名称仍未修改', result['answer'])
        self.assertNotIn('所有修改都已完成', result['answer'])

    async def test_unverifiable_results_do_not_silently_pass(self):
        with patch.object(nodes, 'structured_call', AsyncMock(side_effect=RuntimeError('offline'))):
            result = await nodes.verify_completion({'plan': {'request_review_required': True}})
        self.assertEqual(result['status'], 'partially_succeeded')
        self.assertIn('不能确认全部完成', result['answer'])

    async def test_unsafe_create_instead_of_update_is_a_blocking_plan_issue(self):
        call = AsyncMock(return_value=(RequestReview(complete=False,
            unsafe_substitutions=['用新建空对象代替修改已有对象']), {}))
        issues, _ = await request_review.review_plan(call, question='修改当前对象名称',
            scene='对象 existing', steps=[{'tool': 'create'}], catalog=[])
        self.assertEqual(issues[0]['severity'], 'error')

    async def test_plan_review_receives_tool_contracts_for_future_readbacks(self):
        from webpage_agent.capabilities.registry import Tool
        description = '读取完整状态。' + '字段说明。' * 70 + '版本字段位于 result.document.version'
        schema = {'type': 'object', 'properties': {'result': {
            'type': 'object', 'properties': {'document': {'type': 'object',
                'properties': {'version': {'type': 'integer'}}}}}}}
        tool = Tool('read', '读取', description,
                    {'type': 'object', 'properties': {'id': {'type': 'string'}}},
                    None, effect='read', server_id='studio', output_schema=schema)
        call = AsyncMock(return_value=(RequestReview(complete=True), {}))
        with patch.object(request_review, 'server_rules', return_value='上游节点作为下游参考输入'):
            issues, _ = await request_review.review_plan(call,
                question='连接两个素材', scene='摘要只有对象 id，没有 version',
                steps=[{'step_id': 's1', 'tool': 'read', 'args': {'id': 'existing'}}],
                catalog=[tool])
        self.assertEqual(issues, [])
        payload = json.loads(call.call_args.args[2])
        contract = payload['候选工具'][0]
        self.assertEqual(contract['outputSchema'], schema)
        self.assertEqual(contract['inputSchema'], tool.args)
        self.assertIn('result.document.version', contract['description'])
        self.assertIn('上游节点', payload['服务端字段规则'])
        self.assertIn('操作前现场只是摘要', call.call_args.args[1])
        self.assertIn('不能因为现场摘要尚无该字段', call.call_args.args[1])

    async def test_verified_result_preserves_answer(self):
        state = {'plan': {'request_review_required': True}, 'answer': '已完成'}
        with patch.object(nodes, 'structured_call', AsyncMock(return_value=(RequestReview(complete=True), {}))):
            result = await nodes.verify_completion(state)
        self.assertEqual(result['status'], 'succeeded')
        # 判定要落库：只留 stage 名的话，事后无法复核这次成功凭什么
        self.assertEqual(result['review'], {'complete': True,
                                            'unmet_requirements': [],
                                            'unsafe_substitutions': []})

    async def test_failed_review_records_its_verdict(self):
        state = {'plan': {'request_review_required': True}, 'answer': '已完成'}
        review = RequestReview(complete=False, unmet_requirements=['草稿未更新'])
        with patch.object(nodes, 'structured_call', AsyncMock(return_value=(review, {}))):
            result = await nodes.verify_completion(state)
        self.assertEqual(result['status'], 'partially_succeeded')
        self.assertEqual(result['review']['complete'], False)
        self.assertEqual(result['review']['unmet_requirements'], ['草稿未更新'])

    async def test_review_crash_is_recorded_as_such(self):
        state = {'plan': {'request_review_required': True}, 'answer': '已完成'}
        with patch.object(nodes, 'structured_call', AsyncMock(side_effect=RuntimeError('offline'))):
            result = await nodes.verify_completion(state)
        self.assertEqual(result['status'], 'partially_succeeded')
        self.assertEqual(result['review']['error'], 'review_failed')

    def test_write_receipt_criterion_can_reference_exact_step_id(self):
        tool = SimpleNamespace(name='update_record', tool_key='mcp:crm:update',
                               effect='write', server_id='crm', capability='mcp', scope='')
        issues = nodes.check_result_locus('platform_change',
            [{'tool': 'update_record', 'step_id': 's2'}], [tool],
            [{'kind': 'tool_succeeded', 'target': 's2'}])
        self.assertFalse(issues)

    def test_revisions_cannot_drop_full_request_review(self):
        metadata, _ = nodes.revision_metadata({'request_review_required': False},
            {'request_review_required': True, 'result_locus': 'platform_change'})
        self.assertTrue(metadata['request_review_required'])

    async def test_result_review_gets_the_server_field_rules(self):
        """审查器必须拿到服务端的字段语义，否则"草稿写在 composerContent"会被判成没改。"""
        from webpage_agent.integrations.mcp_servers import Server
        server = Server(id='canvasx', prompt_rules='提示词草稿写在 metadata.composerContent。')
        call = AsyncMock(return_value=(RequestReview(complete=True), {}))
        state = {'user_input': '改提示词',
                 'mcp_calls': [{'server': 'canvasx', 'tool': 'canvas.node.prompt.update'}]}
        with patch('webpage_agent.integrations.mcp_servers.servers', return_value=[server]):
            await request_review.review_result(call, state)
        self.assertIn('composerContent', call.call_args.args[1])
        self.assertIn('[服务端字段规则]', call.call_args.args[1])

    async def test_result_review_without_rules_stays_unchanged(self):
        """没有规则块时提示词不加料：不再出现指向不存在段落的引用。"""
        call = AsyncMock(return_value=(RequestReview(complete=True), {}))
        with patch('webpage_agent.integrations.mcp_servers.servers', return_value=[]):
            await request_review.review_result(call, {'user_input': 'x', 'mcp_calls': []})
        system = call.call_args.args[1]
        self.assertNotIn('\n[服务端字段规则]\n', system)      # 没有追加规则块
        self.assertNotIn('以[服务端字段规则]为准', system)    # 也不引用不存在的段落
