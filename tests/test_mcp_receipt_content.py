"""MCP 工具的返回要原样到模型手里，不能按"检索来源"归一掉。

真踩过（2026-11-11，beeftv）：`canvas.search` 明明拿回 29 个画布、`actions.receipt`
里躺着完整 projects 列表，但回执归一函数只认 `documents`，于是模型收到的永远是
`{"status": "searched", "sources": [], "total_sources": 0}`，据此回答
「当前账号下查询到 0 个画布」。工具结果丢在这一层，比工具被拒更隐蔽——
调用看起来全成功，答案却是反的。
"""
import unittest

from webpage_agent.agent.pi_host_tools import (
    MAX_MCP_RESULT_CHARS, _receipt_content)


def canvas_call(projects):
    return {'mcp_calls': [{'tool': 'canvas.search', 'server': 'beeftv',
                           'args': {'pageSize': 50},
                           'payload': {'result': {'projects': projects,
                                                  'hasMore': False}}}]}


class ReceiptContentTests(unittest.TestCase):
    def test_mcp_payload_reaches_the_model(self):
        content = _receipt_content(
            canvas_call([{'id': 'c1', 'title': '浙江'},
                         {'id': 'c2', 'title': '苍山'}]), 'canvas.search')
        self.assertEqual(content['status'], 'ok')
        self.assertEqual(len(content['mcp_results']), 1)
        item = content['mcp_results'][0]
        self.assertEqual(item['tool'], 'canvas.search')
        self.assertEqual(item['server'], 'beeftv')
        self.assertEqual([p['title'] for p in
                          item['result']['result']['projects']], ['浙江', '苍山'])
        self.assertNotIn('truncated', item)

    def test_web_search_shape_is_unchanged(self):
        content = _receipt_content(
            {'documents': [{'source_id': 's1', 'title': 't', 'url': 'u'}],
             'status': 'searched'}, 'web_search')
        self.assertEqual(content['total_sources'], 1)
        self.assertEqual(content['status'], 'searched')
        self.assertNotIn('mcp_results', content)

    def test_huge_payload_is_truncated_and_says_so(self):
        projects = [{'id': f'c{i}', 'title': '画布' * 200} for i in range(60)]
        content = _receipt_content(canvas_call(projects), 'canvas.search')
        item = content['mcp_results'][0]
        self.assertTrue(item.get('truncated'))
        self.assertGreater(item['total_chars'], MAX_MCP_RESULT_CHARS)
        self.assertEqual(len(item['result']), MAX_MCP_RESULT_CHARS)
        self.assertIn('截断', item['hint'])

    def test_only_first_few_calls_are_returned(self):
        calls = [{'tool': f't{i}', 'server': 'beeftv', 'payload': {'i': i}}
                 for i in range(10)]
        content = _receipt_content({'mcp_calls': calls}, 'x')
        self.assertEqual(len(content['mcp_results']), 4)


if __name__ == '__main__':
    unittest.main()
