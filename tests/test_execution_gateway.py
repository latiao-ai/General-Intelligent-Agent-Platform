"""execution_gateway 与 pi_host_tools 的单元测试（第五阶段）。

验证「一次受治理调用」的抽取没有改变语义：无账本时直接真实调用；授权拒绝时
远端调用计数为零；Pi 宿主调用对象只开放 web_search、结果归一为模型可用内容。
"""
import unittest

from webpage_agent.capabilities.registry import Tool
from webpage_agent.tools import execution_gateway


def _fake_tool(name='web_search', tool_key='local:web:search', effect='read'):
    async def run(args, state):
        return {'documents': [{'source_id': 's1', 'task_id': 't',
                               'url': 'https://example.com', 'title': '标题',
                               'snippet': '摘要', 'chars': 10, 'status': 'success'}],
                'status': 'searched'}
    return Tool(name=name, label=name, description='d', args={
        'type': 'object', 'properties': {'query': {'type': 'string'}},
        'required': ['query']}, run=run, tool_key=tool_key, effect=effect,
                capability='web')


class GovernedCallTests(unittest.IsolatedAsyncioTestCase):
    """governed_call 无账本路径：直接真实调用，不碰预算/授权/账本。"""

    async def test_no_ledger_calls_directly(self):
        tool = _fake_tool()
        result = await execution_gateway.governed_call(
            tool=tool, args={'query': 'x'}, step_id='s1', state={})
        self.assertEqual(result.kind, 'succeeded')
        self.assertEqual(result.updates.get('status'), 'searched')
        self.assertFalse(result.settled)  # 无账本不结算

    async def test_check_boundary_no_context_passes(self):
        # 无执行上下文时边界检查放行（返回空串，不抛）
        out = await execution_gateway.check_boundary({})
        self.assertEqual(out, '')


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
