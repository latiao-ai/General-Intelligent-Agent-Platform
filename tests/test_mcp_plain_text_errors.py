"""明文 isError 的错误归因：参数写错 ≠ 服务端故障。

真实事故（2026-11-12）：LibTV 画布写「嘉兴」节点，同一轮 4 个接口
（create_node ×2 / read_project / read_project_context）全部返回 isError，
根因是四次调用共用了一个错误的 projectId（用了数字 ID，且抄错一位；
服务端要的是画布 UUID）。当时 MCP 网关只回一行纯文本：

    Error executing tool read_project_context: INVALID_PROJECT_ID: Use the
    canvas UUID, not its numeric ID

旧版 result_payload() 只解析 JSON 结构化错误，明文错误被压成
「MCP 工具返回 isError，执行结果待核对，请查看服务端日志」：

  - 错误码和「UUID」这个修复线索全丢；
  - certainty 错标 unknown → 账本禁止自动重发、要求人工核实；
  - 连读接口也一起被拒，助手无法自检，最终把「共享参数错误」误判成
    「服务端那一层故障」，还让用户去查 500/403 日志。

这组测试锁住修复：明文错误也必须带出原因码，参数/前置条件拒绝必须归为
failed_confirmed 并显式说明「不是服务端故障」，其余错误仍保持 unknown、
且不泄漏服务端原文。
"""
import unittest
from types import SimpleNamespace as NS

from webpage_agent.integrations import mcp_adapter
from webpage_agent.integrations.mcp_servers import Server


LIBTV_REJECTION = ('Error executing tool read_project_context: '
                   'INVALID_PROJECT_ID: Use the canvas UUID, not its numeric ID')


def plain_is_error(text: str):
    """MCP 网关的 isError：只有一行 text block，没有 structuredContent。"""
    return NS(isError=True, structuredContent=None,
              content=[NS(type='text', text=text)])


class PlainTextIsErrorTests(unittest.TestCase):
    def test_libtv_invalid_project_id_is_a_confirmed_parameter_rejection(self):
        """四个接口共用的错误 projectId：必须能一眼看出是参数问题。"""
        with self.assertRaises(mcp_adapter.MCPToolFailure) as caught:
            mcp_adapter.result_payload(plain_is_error(LIBTV_REJECTION))
        exc = caught.exception
        self.assertEqual(exc.code, 'invalid_project_id')
        self.assertEqual(mcp_adapter.certainty_of_error(exc), 'failed_confirmed')
        # 修复线索与纠偏提示都要到模型/用户眼前
        self.assertIn('UUID', str(exc))
        self.assertIn('invalid_project_id', str(exc))
        self.assertIn('不是服务端故障', str(exc))
        self.assertIn('共用的字段', str(exc))
        self.assertNotIn('请查看服务端日志', str(exc))

    def test_every_shared_parameter_call_fails_the_same_way(self):
        """写接口与两个读接口：同一原因码，判定必须一致。

        回归点：以前读接口也一起挂，助手就说「画布无法读取核对、状态未知」。
        现在每个接口都会明确回「参数被拒、请求未落地」。
        """
        calls = (
            ('create_node', 'Error executing tool create_node: INVALID_PROJECT_ID: '
                            'Use the canvas UUID, not its numeric ID'),
            ('read_project', 'Error executing tool read_project: INVALID_PROJECT_ID: '
                             'Use the canvas UUID, not its numeric ID'),
            ('read_project_context', LIBTV_REJECTION),
        )
        for name, text in calls:
            with self.subTest(tool=name):
                with self.assertRaises(mcp_adapter.MCPToolFailure) as caught:
                    mcp_adapter.result_payload(plain_is_error(text))
                self.assertEqual(caught.exception.code, 'invalid_project_id')
                self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                                 'failed_confirmed')

    def test_other_plain_rejections_are_also_confirmed(self):
        for code in ('MISSING_NODE_ID', 'UNSUPPORTED_FIELD', 'STALE_REVISION',
                     'INVALID_ARGUMENT', 'CLIENT_FORBIDDEN', 'USER_UNAUTHORIZED'):
            with self.subTest(code=code):
                with self.assertRaises(mcp_adapter.MCPToolFailure) as caught:
                    mcp_adapter.result_payload(
                        plain_is_error(f'Error executing tool t: {code}: rejected'))
                self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                                 'failed_confirmed')

    def test_ambiguous_plain_errors_stay_unknown_but_keep_the_reason_code(self):
        """超时/服务端错误仍不能自动重发，但原因码必须留下供排障。"""
        for code in ('UPSTREAM_TIMEOUT', 'DATABASE_BUSY', 'NOT_FOUND'):
            with self.subTest(code=code):
                with self.assertRaises(RuntimeError) as caught:
                    mcp_adapter.result_payload(plain_is_error(
                        f'Error executing tool t: {code}: '
                        'Traceback: /srv/private/path'))
                exc = caught.exception
                self.assertNotIsInstance(exc, mcp_adapter.MCPToolFailure)
                self.assertEqual(mcp_adapter.certainty_of_error(exc), 'unknown')
                self.assertIn(code.lower(), str(exc))
                # 不能泄漏 traceback / 内部路径，也不能再误导去查服务端日志
                self.assertNotIn('/srv/private/path', str(exc))
                self.assertNotIn('请查看服务端日志', str(exc))

    def test_plain_traceback_without_a_code_leaks_nothing(self):
        with self.assertRaises(RuntimeError) as caught:
            mcp_adapter.result_payload(plain_is_error(
                'Traceback (most recent call last):\n  File "/srv/private/a.py"'))
        self.assertNotIn('/srv/private/a.py', str(caught.exception))
        self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                         'unknown')

    def test_plain_error_code_is_not_confused_with_uppercase_words(self):
        code, body = mcp_adapter.plain_error_details(
            'Error executing tool t: Use the UUID, HTTP 500 happened')
        self.assertEqual(code, '')
        self.assertIn('Use the UUID', body)

    def test_structured_json_branch_is_unchanged(self):
        rejected = NS(isError=True, structuredContent=None, content=[NS(
            type='text', text='{"reason":"stale_revision",'
                              '"message":"云端画布已有更新，已停止覆盖"}')])
        with self.assertRaises(mcp_adapter.MCPToolFailure) as caught:
            mcp_adapter.result_payload(rejected)
        self.assertEqual(caught.exception.code, 'stale_revision')
        self.assertIn('云端画布已有更新', str(caught.exception))

    def test_empty_is_error_does_not_crash(self):
        result = NS(isError=True, structuredContent=None, content=[])
        with self.assertRaises(RuntimeError) as caught:
            mcp_adapter.result_payload(result)
        self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                         'unknown')

    def test_server_can_declare_a_plain_reason_as_deterministic(self):
        """厂商专有词仍只能靠 MCP_SERVERS.deterministic_reasons 打开。"""
        text = 'Error executing tool t: QUOTA_EXHAUSTED: no credits'
        server = Server(id='vendorx', deterministic_reasons=('quota_exhausted',))
        with self.assertRaises(mcp_adapter.MCPToolFailure):
            mcp_adapter.result_payload(plain_is_error(text), server)
        with self.assertRaises(RuntimeError) as bare:
            mcp_adapter.result_payload(plain_is_error(text))
        self.assertEqual(mcp_adapter.certainty_of_error(bare.exception), 'unknown')
