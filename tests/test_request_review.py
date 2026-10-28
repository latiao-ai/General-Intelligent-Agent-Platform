import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import nodes, request_review
from app.schemas import RequestReview


class RequestReviewTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_verified_result_preserves_answer(self):
        state = {'plan': {'request_review_required': True}, 'answer': '已完成'}
        with patch.object(nodes, 'structured_call', AsyncMock(return_value=(RequestReview(complete=True), {}))):
            result = await nodes.verify_completion(state)
        self.assertEqual(result['status'], 'succeeded')

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
