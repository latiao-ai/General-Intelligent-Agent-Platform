"""MCP 错误的执行确定性：明确拒绝 ≠ 结果未定。

踩过的真实场景：BeefTV 画布写入用旧 revision，服务端回
`{"reason":"stale_revision","message":"云端画布已有更新…"}`——请求**没有被应用**。
旧实现只认 `invalid_/missing_/unsupported_/revision_conflict/node_kind_mismatch`，
于是把它归成"结果待核对"，账本按未知处理：既不能自动重试，用户也看不到真实原因。
"""
import json
import unittest

from webpage_agent.integrations import mcp_adapter
from webpage_agent.integrations.mcp_servers import Server


def is_error(reason: str, message: str = '云端画布已有更新，已停止覆盖'):
    class Block:
        type = 'text'
        text = json.dumps({'code': 4, 'message': message, 'reason': reason})

    class Result:
        isError = True
        structuredContent = None
        content = [Block()]

    return Result()


class RejectedReasonTests(unittest.TestCase):
    def test_stale_revision_is_a_confirmed_rejection(self):
        with self.assertRaises(mcp_adapter.MCPToolFailure) as caught:
            mcp_adapter.result_payload(is_error('stale_revision'))
        self.assertEqual(caught.exception.code, 'stale_revision')
        self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                         'failed_confirmed')
        # 用户/模型看得到的那一句必须带原因，否则只能猜
        self.assertIn('云端画布已有更新', str(caught.exception))

    def test_conflict_shaped_reasons_are_rejections(self):
        for reason in ('revision_conflict', 'node_kind_mismatch', 'invalid_argument',
                       'precondition_failed', 'missing_node_id', 'unsupported_field',
                       'stale_revision'):
            with self.subTest(reason=reason):
                self.assertTrue(mcp_adapter.rejected_without_apply(reason))

    def test_ambiguous_reasons_stay_unknown(self):
        """可能已经写进去的原因不能当确定性失败，否则会盲目重发。"""
        for reason in ('upstream_timeout', 'database_busy', 'stream_closed', ''):
            with self.subTest(reason=reason):
                self.assertFalse(mcp_adapter.rejected_without_apply(reason))

    def test_named_bare_conflict_and_not_found_stay_unknown(self):
        """裸 `conflict`/`not_found` 默认不算"确认未执行"。

        反例是真实风险：写请求在服务端改了前几个对象后，才发现后一个目标不存在，
        回一个 `not_found`。把它当"确认没做"就会整步重发，前面已经落地的写入
        被再来一遍——正是"意外重复写入"这一类事故。
        """
        for reason in ('conflict', 'not_found'):
            with self.subTest(reason=reason):
                self.assertFalse(mcp_adapter.rejected_without_apply(reason))
                with self.assertRaises(RuntimeError) as caught:
                    mcp_adapter.result_payload(is_error(reason, '目标不存在'))
                self.assertNotIsInstance(caught.exception, mcp_adapter.MCPToolFailure)
                self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                                 'unknown')

    def test_auth_rejections_are_confirmed_not_executed(self):
        """入口鉴权类拒绝：服务端没动手就否掉了，重发也拿不到权。"""
        for reason in ('forbidden', 'unauthorized', 'external_business_forbidden',
                       'invalid_external_identity', 'client_unauthorized',
                       'user_unauthorized'):
            with self.subTest(reason=reason):
                with self.assertRaises(mcp_adapter.MCPToolFailure) as caught:
                    mcp_adapter.result_payload(is_error(reason, '该连接仅支持读取'))
                self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                                 'failed_confirmed')

    def test_named_suffix_is_more_trusted_than_bare_word(self):
        """这是**有意的保守偏差**：带领域词的具名原因更具体，裸单词留给配置声明。

        `write_conflict` 会命中 `_conflict` 后缀 → 确认拒绝；裸 `conflict` → 未知。
        同一类语义因命名不同得到不同重试决策，是有意为之，不是漏判。
        """
        self.assertTrue(mcp_adapter.rejected_without_apply('write_conflict'))
        self.assertFalse(mcp_adapter.rejected_without_apply('conflict'))

    def test_a_server_can_opt_in_to_its_own_ambiguous_reasons(self):
        server = Server(id='legacy', deterministic_reasons=('not_found',))
        with self.assertRaises(mcp_adapter.MCPToolFailure):
            mcp_adapter.result_payload(is_error('not_found', '记录不存在'), server)

    def test_vendor_reason_declared_in_config_is_honoured(self):
        """厂商专有词只能靠 `MCP_SERVERS.deterministic_reasons` 打开。

        回归点：BeefTV 的 `read_only_client` 一旦从配置里掉出去（换机器、
        重新生成 `.env`、同事本地没配），就会静默退回 unknown——所以这条
        同时验证"配置能被解析"和"解析后真的路由到 failed_confirmed"。
        """
        import json as jsonlib
        import os
        from unittest.mock import patch
        from webpage_agent.integrations import mcp_servers

        entry = [{'id': 'beeftv-configured', 'transport': 'stdio',
                  'stdio_command': '/bin/true',
                  'deterministic_reasons': ['read_only_client']}]
        with patch.dict(os.environ, {'MCP_SERVERS': jsonlib.dumps(entry)}):
            server = mcp_servers.by_id('beeftv-configured')
            self.assertIsNotNone(server)
            self.assertEqual(server.deterministic_reasons, ('read_only_client',))
            with self.assertRaises(mcp_adapter.MCPToolFailure) as caught:
                mcp_adapter.result_payload(is_error('read_only_client'), server)
        self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                         'failed_confirmed')
        # 没配置时同一个词必须退回 unknown：配置才是唯一的开关。
        with self.assertRaises(RuntimeError) as bare:
            mcp_adapter.result_payload(is_error('read_only_client'))
        self.assertEqual(mcp_adapter.certainty_of_error(bare.exception), 'unknown')

    def test_unknown_reason_keeps_result_uncertain_but_names_the_reason(self):
        with self.assertRaises(RuntimeError) as caught:
            mcp_adapter.result_payload(is_error('upstream_timeout'))
        self.assertNotIsInstance(caught.exception, mcp_adapter.MCPToolFailure)
        self.assertEqual(mcp_adapter.certainty_of_error(caught.exception), 'unknown')
        # 排障需要原因；但不能因此变成"已确认失败"
        self.assertIn('upstream_timeout', str(caught.exception))

    def test_server_can_extend_the_vocabulary_without_touching_the_kernel(self):
        server = Server(id='vendorx', deterministic_reasons=('quota_exhausted',))
        with self.assertRaises(mcp_adapter.MCPToolFailure):
            mcp_adapter.result_payload(is_error('quota_exhausted'), server)
        with self.assertRaises(RuntimeError):
            mcp_adapter.result_payload(is_error('quota_exhausted'))   # 没配就是未知
