"""第十阶段：写后回读核验与完成条件的单元测试（不依赖 PG）。

验证：
1. 写后回读：revision/字段匹配判定、回读失败不冒充匹配；
2. 对象级隔离（scope_satisfied 值约束）；
3. 完成条件：写任务必须有回读匹配证据，answer 不能作为写任务成功依据。
"""
import unittest

from webpage_agent.core import platform_verify
from webpage_agent.core import outcome
from webpage_agent.tools import tool_policy


class ScopeValueConstraintTests(unittest.TestCase):
    """第十阶段第 3 点：可信对象约束与实际参数值比较，非「字段非空」。"""

    def test_value_constraint_matches(self):
        self.assertTrue(tool_policy.scope_satisfied(
            'object_id=obj-a', {'object_id': 'obj-a'}))

    def test_value_constraint_rejects_other_object(self):
        self.assertFalse(tool_policy.scope_satisfied(
            'object_id=obj-a', {'object_id': 'obj-b'}))

    def test_value_constraint_rejects_missing(self):
        self.assertFalse(tool_policy.scope_satisfied(
            'object_id=obj-a', {}))

    def test_multi_constraint(self):
        self.assertTrue(tool_policy.scope_satisfied(
            'project_id=p1,object_id=obj-a',
            {'project_id': 'p1', 'object_id': 'obj-a'}))
        self.assertFalse(tool_policy.scope_satisfied(
            'project_id=p1,object_id=obj-a',
            {'project_id': 'p1', 'object_id': 'obj-b'}))


class PlatformReadbackTests(unittest.IsolatedAsyncioTestCase):
    """第十阶段第 7 点：写后独立回读核验。"""

    async def test_readback_matches_revision_and_fields(self):
        async def get_object(object_id):
            return {'status': 'ok', 'object_id': object_id,
                    'owner': 'subject-A', 'revision': 2,
                    'fields': {'title': '新'}}
        result = await platform_verify.verify_platform_write(
            {'object_id': 'obj-a', 'new_revision': 2,
             'modified_fields': {'title': '新'}},
            server=None, get_object_call=get_object)
        self.assertTrue(result['readback_match'])

    async def test_readback_revision_mismatch(self):
        async def get_object(object_id):
            return {'status': 'ok', 'revision': 3, 'fields': {'title': '新'}}
        result = await platform_verify.verify_platform_write(
            {'object_id': 'obj-a', 'new_revision': 2,
             'modified_fields': {'title': '新'}},
            server=None, get_object_call=get_object)
        self.assertFalse(result['readback_match'])

    async def test_readback_field_mismatch(self):
        async def get_object(object_id):
            return {'status': 'ok', 'revision': 2, 'fields': {'title': '旧'}}
        result = await platform_verify.verify_platform_write(
            {'object_id': 'obj-a', 'new_revision': 2,
             'modified_fields': {'title': '新'}},
            server=None, get_object_call=get_object)
        self.assertFalse(result['readback_match'])

    async def test_readback_failure_is_not_a_match(self):
        async def get_object(object_id):
            raise RuntimeError('回读失败')
        result = await platform_verify.verify_platform_write(
            {'object_id': 'obj-a', 'new_revision': 2,
             'modified_fields': {'title': '新'}},
            server=None, get_object_call=get_object)
        self.assertFalse(result['readback_match'])

    def test_needs_readback(self):
        self.assertTrue(platform_verify.needs_readback({'object_id': 'x'}))
        self.assertFalse(platform_verify.needs_readback({'deployment': {}}))
        self.assertFalse(platform_verify.needs_readback(None))

    async def test_verify_operation_applied(self):
        async def get_operation(op_id):
            return {'status': 'ok', 'operation': {'operation_id': op_id,
                                                  'object_id': 'obj-a'}}
        result = await platform_verify.verify_operation(
            'op-1', server=None, get_operation_call=get_operation)
        self.assertTrue(result['applied'])
        self.assertEqual(result['error'], '')

    async def test_verify_operation_not_applied(self):
        async def get_operation(op_id):
            return {'status': 'error', 'code': 'E_NOT_FOUND',
                    'message': '操作不存在'}
        result = await platform_verify.verify_operation(
            'op-1', server=None, get_operation_call=get_operation)
        self.assertFalse(result['applied'])

    async def test_verify_operation_query_failure_is_unknown(self):
        async def get_operation(op_id):
            raise RuntimeError('网络错误')
        result = await platform_verify.verify_operation(
            'op-1', server=None, get_operation_call=get_operation)
        self.assertFalse(result['applied'])
        self.assertNotEqual(result['error'], '')


class WriteCompletionConditionTests(unittest.TestCase):
    """第十阶段第 9 点：写任务成功必须有回读匹配证据，answer 不够。"""

    def test_verified_requires_readback_match_for_platform_write(self):
        # 平台写：无回读匹配 → 未核实
        self.assertFalse(outcome._verified(
            {'object_id': 'obj-a', 'new_revision': 2}))
        # 平台写：回读匹配 → 已核实
        self.assertTrue(outcome._verified(
            {'object_id': 'obj-a', 'new_revision': 2,
             'readback_match': True}))
        # 平台写：回读不匹配 → 未核实
        self.assertFalse(outcome._verified(
            {'object_id': 'obj-a', 'new_revision': 2,
             'readback_match': False}))

    def test_verified_passthrough_for_non_platform(self):
        # 非平台写（无 object_id）：维持旧语义（成功即核实）
        self.assertTrue(outcome._verified({'foo': 'bar'}))


class FinalStatusWriteOrderingTests(unittest.TestCase):
    """第十阶段第 3 点：存在平台写动作时，先查写证据集合再判成功，answer 仅展示。"""

    def test_unverified_write_plus_answer_is_not_success(self):
        # 有 answer + 平台写但无回读匹配 → 不得成功
        status = outcome.final_status({
            'answer': '已完成修改',
            'write_evidences': [
                {'object_id': 'obj-a', 'operation_id': 'op-1',
                 'readback_match': False}]})
        self.assertEqual(status, 'needs_reconcile')

    def test_readback_wrong_object_is_not_success(self):
        # 回读核验对象不一致 → 不得成功
        status = outcome.final_status({
            'write_evidences': [
                {'object_id': 'obj-a', 'operation_id': 'op-1',
                 'verified': False,
                 'checks': {'object_id': False}}]})
        self.assertEqual(status, 'needs_reconcile')

    def test_missing_revision_is_not_success(self):
        # 缺 revision 核验 → 不得成功
        status = outcome.final_status({
            'write_evidences': [
                {'object_id': 'obj-a', 'operation_id': 'op-1',
                 'verified': False, 'checks': {'revision': False}}]})
        self.assertEqual(status, 'needs_reconcile')

    def test_first_write_unverified_last_verified_is_not_success(self):
        # 第一次写未核实、最后一次已核实 → 整体不得成功（逐项核验）
        status = outcome.final_status({
            'answer': '改好了',
            'write_evidences': [
                {'object_id': 'obj-a', 'operation_id': 'op-1',
                 'readback_match': False},
                {'object_id': 'obj-b', 'operation_id': 'op-2',
                 'readback_match': True}]})
        self.assertEqual(status, 'needs_reconcile')

    def test_all_writes_verified_is_success(self):
        status = outcome.final_status({
            'answer': '改好了',
            'write_evidences': [
                {'object_id': 'obj-a', 'operation_id': 'op-1',
                 'readback_match': True},
                {'object_id': 'obj-b', 'operation_id': 'op-2',
                 'readback_match': True}]})
        self.assertEqual(status, 'succeeded')

    def test_unknown_write_evidence_is_not_success(self):
        status = outcome.final_status({
            'write_evidences': [
                {'object_id': 'obj-a', 'operation_id': 'op-1',
                 'status': 'unknown'}]})
        self.assertEqual(status, 'needs_reconcile')

    def test_pure_chat_keeps_answer_semantics(self):
        # 纯闲聊/只读任务（无写证据）：保留「有答复即成功」
        status = outcome.final_status({'answer': '今天天气不错'})
        self.assertEqual(status, 'succeeded')


if __name__ == '__main__':
    unittest.main()
