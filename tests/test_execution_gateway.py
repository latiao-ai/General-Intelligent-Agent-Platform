"""execution_gateway 与 pi_host_tools 的单元测试（第五阶段→第六阶段）。

第六阶段起，`governed_call` 收紧准入：**正式执行必须有账本**，无账本（CLI 直调、
单元测试直连）一律拒绝（`kind='no_run'`），不再走"无账本直接真实调用"的兼容
路径——假工具 / 无治理 smoke 走独立的显式测试路径，不进这个网关。
"""
import unittest
from unittest.mock import patch

from webpage_agent.capabilities.registry import Tool
from webpage_agent.governance import execution_control
from webpage_agent.store import ledger as ledger_module
from webpage_agent.tools import execution_gateway


def _fake_tool(name='web_search', tool_key='local:web:search', effect='read',
               calls=None):
    calls = calls if calls is not None else []

    async def run(args, state):
        calls.append((name, args))
        return {'documents': [{'source_id': 's1', 'task_id': 't',
                               'url': 'https://example.com', 'title': '标题',
                               'snippet': '摘要', 'chars': 10, 'status': 'success'}],
                'status': 'searched'}

    return Tool(name=name, label=name, description='d', args={
        'type': 'object', 'properties': {'query': {'type': 'string'}},
        'required': ['query']}, run=run, tool_key=tool_key, effect=effect,
                capability='web'), calls


class _FakeStore:
    """假仓储：只实现 governed_call 治理链用到的方法，可注入 admit 结果。"""

    def __init__(self, admit_status='admitted', *, control=None):
        self.admit_status = admit_status
        self.control = control or {}
        self.admit_result = {'status': admit_status}
        self.settled = []
        self.failed = []
        self.budget_consumed = 0
        self.replay = None

    async def consume_budget(self, context, *, run_id, key, limit):
        self.budget_consumed += 1
        return 1

    async def control_state(self, run_id):
        return dict(self.control)

    async def run(self, run_id):
        return {'run_id': run_id, 'tenant_id': 'acme', 'owner_user_id': 'alice',
                'intent_version': 1, 'owner_role': 'member'}

    async def admit_action(self, context, **kwargs):
        result = dict(self.admit_result)
        if result.get('status') == 'admitted':
            result['action_id'] = 'act_test'
            result['attempt_id'] = 'att_test'
        return result

    async def settle_action(self, context, **kwargs):
        self.settled.append(kwargs)
        return {'observation_seq': 1}

    async def release_approval_reservation(self, **kwargs):
        return True

    async def consume_approval_reservation(self, **kwargs):
        return True


def _bind_ledger(store, context=None):
    book = ledger_module.ActionLedger(store, 'run1', 1, context=context)
    return ledger_module.bind(book)


class _NoContextFakeStore(_FakeStore):
    """control_state 抛出：模拟控制状态读不到（check_boundary 放行但留痕）。"""
    async def control_state(self, run_id):
        raise RuntimeError('db down')


class GovernedCallTests(unittest.IsolatedAsyncioTestCase):
    """governed_call 无账本路径：第六阶段起拒绝（不碰真实调用）。"""

    async def test_no_ledger_refuses(self):
        # 无账本 = 拒绝（kind='no_run'），远端调用计数为零
        tool, calls = _fake_tool()
        result = await execution_gateway.governed_call(
            tool=tool, args={'query': 'x'}, step_id='s1', state={})
        self.assertEqual(result.kind, 'no_run')
        self.assertFalse(result.settled)
        self.assertEqual(calls, [])

    async def test_check_boundary_no_context_passes(self):
        # 无执行上下文时边界检查放行（返回空串，不抛）
        out = await execution_gateway.check_boundary({})
        self.assertEqual(out, '')


class GovernedCallGateTests(unittest.IsolatedAsyncioTestCase):
    """第六阶段：准入门禁——只有明确 admitted 才真实调用，其余全部零调用。

    这些用例用假仓储 + 真实 ActionLedger，验证「无 run / blocked / 未知状态 /
    重放」时真实工具调用次数为零，不依赖 PostgreSQL。
    """

    def _ctx(self):
        return execution_control.ExecutionContext(
            run_id='run1', lease_owner='w1', lease_epoch=1)

    async def test_admitted_calls_tool(self):
        store = _FakeStore(admit_status='admitted')
        token = _bind_ledger(store, self._ctx())
        tool, calls = _fake_tool()
        try:
            with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                       return_value=False):
                result = await execution_gateway.governed_call(
                    tool=tool, args={'query': 'x'}, step_id='s1', state={},
                    ctx=execution_gateway.tool_context_module.from_run(
                        {'run_id': 'run1', 'tenant_id': 'acme'}))
        finally:
            ledger_module.unbind(token)
        self.assertEqual(result.kind, 'succeeded')
        self.assertEqual(len(calls), 1)

    async def test_no_run_status_blocks_call(self):
        # admit 返回 no_run：真实调用次数为零
        store = _FakeStore(admit_status='no_run')
        token = _bind_ledger(store, self._ctx())
        tool, calls = _fake_tool()
        try:
            result = await execution_gateway.governed_call(
                tool=tool, args={'query': 'x'}, step_id='s1', state={},
                ctx=None)
        finally:
            ledger_module.unbind(token)
        self.assertEqual(result.kind, 'no_run')
        self.assertEqual(calls, [])

    async def test_blocked_status_blocks_call(self):
        store = _FakeStore(admit_status='in_flight')
        store.admit_result = {'status': 'in_flight', 'blocked': 'in_flight'}
        token = _bind_ledger(store, self._ctx())
        tool, calls = _fake_tool()
        try:
            result = await execution_gateway.governed_call(
                tool=tool, args={'query': 'x'}, step_id='s1', state={},
                ctx=None)
        finally:
            ledger_module.unbind(token)
        self.assertEqual(result.kind, 'blocked')
        self.assertEqual(calls, [])

    async def test_unknown_status_blocks_call(self):
        store = _FakeStore(admit_status='bogus')
        store.admit_result = {'status': 'bogus'}
        token = _bind_ledger(store, self._ctx())
        tool, calls = _fake_tool()
        try:
            result = await execution_gateway.governed_call(
                tool=tool, args={'query': 'x'}, step_id='s1', state={},
                ctx=None)
        finally:
            ledger_module.unbind(token)
        self.assertEqual(result.kind, 'blocked')
        self.assertEqual(calls, [])

    async def test_replay_does_not_recall(self):
        store = _FakeStore(admit_status='succeeded')
        store.admit_result = {
            'status': 'succeeded',
            'replay': {'documents': [{'source_id': 's1', 'title': 't',
                                      'url': 'https://x', 'chars': 10}]}}
        token = _bind_ledger(store, self._ctx())
        tool, calls = _fake_tool()
        try:
            result = await execution_gateway.governed_call(
                tool=tool, args={'query': 'x'}, step_id='s1', state={},
                ctx=None)
        finally:
            ledger_module.unbind(token)
        self.assertEqual(result.kind, 'replay')
        self.assertEqual(calls, [])

    async def test_build_snapshot_receives_tool_result(self):
        """第七阶段：build_snapshot 收到 (updates, tool_result)，快照在结算前构造。"""
        store = _FakeStore(admit_status='admitted')
        token = _bind_ledger(store, self._ctx())
        tool, calls = _fake_tool()
        captured = {}

        def build_snapshot(updates, tool_result=None):
            captured['updates'] = updates
            captured['tool_result'] = tool_result
            return {'step_outputs': {'s1': tool_result}}

        try:
            with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                       return_value=False):
                result = await execution_gateway.governed_call(
                    tool=tool, args={'query': 'x'}, step_id='s1', state={},
                    ctx=execution_gateway.tool_context_module.from_run(
                        {'run_id': 'run1', 'tenant_id': 'acme'}),
                    build_snapshot=build_snapshot)
        finally:
            ledger_module.unbind(token)
        self.assertEqual(result.kind, 'succeeded')
        self.assertIn('tool_result', captured)
        self.assertIsNotNone(captured['tool_result'])
        # 快照随结算同事务提交（settle_action 收到含本步输出的 state）
        self.assertTrue(store.settled)
        self.assertIn('step_outputs', store.settled[0].get('state') or {})


class PiHostToolCallerTests(unittest.IsolatedAsyncioTestCase):
    """pi_host_tools：只开放 web_search，结果归一为模型内容。"""

    async def test_search_receipt_extracts_sources(self):
        from webpage_agent.agent import pi_host_tools
        updates = {
            'documents': [{'source_id': 's1', 'task_id': 't',
                           'url': 'https://x.com', 'title': '标题',
                           'snippet': '摘要', 'chars': 0, 'status': 'pending'}],
            'errors': [], 'usage': [], 'status': 'searched'}
        receipt = pi_host_tools._search_receipt(updates)
        self.assertEqual(receipt['tool'], 'web_search')
        self.assertEqual(receipt['status'], 'searched')
        self.assertEqual(len(receipt['sources']), 1)
        self.assertEqual(receipt['sources'][0]['url'], 'https://x.com')
        self.assertEqual(receipt['sources'][0]['title'], '标题')
        self.assertIn('source_id', receipt['sources'][0])
        self.assertIn('excerpt', receipt['sources'][0])

    async def test_resolve_web_search(self):
        from webpage_agent.agent import pi_host_tools
        tool = pi_host_tools._resolve_web_search()
        self.assertIsNotNone(tool)
        self.assertEqual(tool.tool_key, 'local:web:search')
        self.assertEqual(tool.name, 'web_search')

    async def test_stable_step_id_ignores_call_id(self):
        from webpage_agent.agent import pi_host_tools
        # 稳定执行身份由 tool_key + args 决定，与桥接 call_id 无关（旧语义，
        # 保留回归：同参数同身份）。第七阶段起正式路径改用 PiCallRegistry，
        # 这里只锁旧函数的确定性行为。
        a = pi_host_tools._stable_step_id('local:web:search', {'query': 'x'})
        b = pi_host_tools._stable_step_id('local:web:search', {'query': 'x'})
        c = pi_host_tools._stable_step_id('local:web:search', {'query': 'y'})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class PiCallRegistryTests(unittest.TestCase):
    """第七阶段：逻辑调用身份与参数哈希分离。"""

    def test_same_args_new_calls_get_distinct_ids(self):
        from webpage_agent.agent.pi_host_tools import PiCallRegistry
        reg = PiCallRegistry()
        # 两个不同的 Node 调用（不同 call_id），参数完全相同 → 不同逻辑 ID
        id1 = reg.logical_id('web_search:c1', tool_key='local:web:search',
                             args={'query': 'x'})
        id2 = reg.logical_id('web_search:c2', tool_key='local:web:search',
                             args={'query': 'x'})
        self.assertNotEqual(id1, id2)

    def test_same_node_call_reuses_id(self):
        from webpage_agent.agent.pi_host_tools import PiCallRegistry
        reg = PiCallRegistry()
        first = reg.logical_id('web_search:c1', tool_key='local:web:search',
                               args={'query': 'x'})
        again = reg.logical_id('web_search:c1', tool_key='local:web:search',
                               args={'query': 'x'})
        self.assertEqual(first, again)

    def test_same_id_different_args_rejected(self):
        from webpage_agent.agent.pi_host_tools import PiCallRegistry
        reg = PiCallRegistry()
        reg.logical_id('web_search:c1', tool_key='local:web:search',
                       args={'query': 'x'})
        with self.assertRaises(RuntimeError):
            reg.logical_id('web_search:c1', tool_key='local:web:search',
                           args={'query': 'y'})

    def test_restore_reuses_original_ids(self):
        from webpage_agent.agent.pi_host_tools import PiCallRegistry
        from webpage_agent.store import ledger as ledger_module
        args = {'query': 'x'}
        snapshot = {
            'logical_call_seq': 2,
            'pending_calls': [
                {'node_call_key': 'web_search:c1',
                 'logical_call_id': 'pi:call:1',
                 'tool_key': 'local:web:search',
                 'args_hash': ledger_module.args_hash(args)},
            ],
        }
        reg = PiCallRegistry(snapshot)
        # 恢复后同一次调用（参数一致）复用原 ID
        restored = reg.logical_id('web_search:c1', tool_key='local:web:search',
                                  args=args)
        self.assertEqual(restored, 'pi:call:1')
        # 新调用拿到新序号（从 2 继续）
        new_id = reg.logical_id('web_search:c2', tool_key='local:web:search',
                                args=args)
        self.assertEqual(new_id, 'pi:call:3')


class SchemaValidationTests(unittest.TestCase):
    """pi_protocol 的完整 JSON Schema 参数校验。"""

    def test_missing_required_rejected(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_args_against_schema({}, {
                'type': 'object',
                'properties': {'query': {'type': 'string'}},
                'required': ['query']})

    def test_wrong_type_rejected(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_args_against_schema({'query': 123}, {
                'type': 'object',
                'properties': {'query': {'type': 'string'}},
                'required': ['query']})

    def test_unknown_field_rejected(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_args_against_schema({'query': 'x', 'bogus': 1}, {
                'type': 'object',
                'properties': {'query': {'type': 'string'}},
                'required': ['query']})

    def test_valid_passes(self):
        from webpage_agent.agent import pi_protocol as proto
        proto.validate_args_against_schema({'query': '天气'}, {
            'type': 'object',
            'properties': {'query': {'type': 'string'}},
            'required': ['query']})

    def test_tool_catalog_empty_ok(self):
        from webpage_agent.agent import pi_protocol as proto
        proto.validate_tool_catalog([])  # 空目录合法（本轮无工具）

    def test_tool_decl_rejects_simplified_schema(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_tool_decl({
                'name': 'x', 'description': 'd',
                'parameters': {'text': 'string'}})  # 简化格式被拒


if __name__ == '__main__':
    unittest.main()
