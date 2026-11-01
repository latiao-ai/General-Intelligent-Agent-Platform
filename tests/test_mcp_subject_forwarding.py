"""跨机器（HTTP/SSE）把调用主体交给远端服务端：发**签名断言**，不发明文头。

为什么单独一组用例：

- 明文 `X-MCP-Subject` 对端无法验真，一旦它拿来做身份判断，任何持有租户 token
  的人都能冒充任意主体——所以本端只发 HMAC 断言，且**没配密钥就拒绝**，不退化；
- 主体只能来自 `runs.platform_subject`（服务端推导），模型参数/配置都改不了；
- stdio 与 HTTP 共用同一个主体来源，但传输方式不同（环境变量 vs 签名头），
  两条都要钉住，避免以后改一条坏一条。
"""
import time
import unittest
from contextlib import asynccontextmanager, ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from webpage_agent.core import credentials
from webpage_agent.governance import execution_control
from webpage_agent.store import ledger
from webpage_agent.integrations import mcp_adapter, mcp_subject
from webpage_agent.integrations.mcp_servers import Server

SECRET = 'unit-test-secret'


class _Stop(Exception):
    """用来在"已经开始建传输"的那一刻截断，避免真连远端。"""


class _RunStore:
    """只回答 `run()` 的仓储：主体由运行行给出，与真实链路一致。"""

    def __init__(self, run: dict) -> None:
        self._run = dict(run)

    async def run(self, run_id: str) -> dict:
        return dict(self._run, run_id=run_id)


def _session(subject: str = 'user-42', platform: str = 'vendorx',
             tenant: str = 'studio-demo', run_id: str = 'run-1'):
    store = _RunStore({'tenant_id': tenant, 'owner_user_id': 'owner',
                       'platform_id': platform, 'platform_subject': subject})
    context = execution_control.ExecutionContext(run_id, lease_epoch=1)
    book = ledger.ActionLedger(store, run_id, 1, context=context)
    stack = ExitStack()
    stack.enter_context(execution_control.use(context))
    stack.enter_context(ledger.use_ledger(book))
    return stack


def _server(**extra) -> Server:
    base: dict = dict(id='vendorx', transport='streamable_http',
                url='http://127.0.0.1:9/mcp', token='tenant-token',
                forward_subject=True, subject_secret_ref=f'literal:{SECRET}')
    base.update(extra)
    return Server(**base)


def _header(headers, name: str) -> str:
    """大小写不敏感地取一个请求头。

    httpx 会把头名规范成小写（`dict(client.headers)` 里看到的是 `x-mcp-subject-...`），
    断言里写死大小写会得到一条假的"头没带上"。
    """
    value = headers.get(name) if hasattr(headers, 'get') else None
    if value:
        return value
    return {str(k).lower(): v for k, v in dict(headers or {}).items()}.get(name.lower(), '')


class AssertionTests(unittest.TestCase):
    def test_round_trip_carries_subject_and_context(self):
        token = mcp_subject.build('user-42', secret=SECRET, tenant_id='studio-demo',
                                  platform_id='vendorx', run_id='run-1')
        payload = mcp_subject.verify(token, SECRET)
        self.assertEqual(payload['sub'], 'user-42')
        self.assertEqual(payload['tenant'], 'studio-demo')
        self.assertEqual(payload['platform'], 'vendorx')
        self.assertEqual(payload['run'], 'run-1')
        self.assertEqual(payload['exp'] - payload['iat'],
                         mcp_subject.DEFAULT_TTL_SECONDS)

    def test_payload_tampering_is_rejected(self):
        token = mcp_subject.build('user-42', secret=SECRET)
        version, body, signature = token.split('.')
        forged = dict(mcp_subject.verify(token, SECRET))
        forged['sub'] = 'admin'
        import base64, json
        raw = base64.urlsafe_b64encode(
            json.dumps(forged, separators=(',', ':'), sort_keys=True)
            .encode('utf-8')).decode('ascii').rstrip('=')
        with self.assertRaises(mcp_subject.SubjectAssertionError) as caught:
            mcp_subject.verify(f'v1.{raw}.{signature}', SECRET)
        self.assertIn('签名不匹配', str(caught.exception))

    def test_wrong_secret_is_rejected(self):
        token = mcp_subject.build('user-42', secret=SECRET)
        with self.assertRaises(mcp_subject.SubjectAssertionError):
            mcp_subject.verify(token, 'another-secret')

    def test_plain_text_subject_header_cannot_pass(self):
        """有人直接发明文主体值：对端必须判格式不合法，而不是当身份用。"""
        with self.assertRaises(mcp_subject.SubjectAssertionError) as caught:
            mcp_subject.verify('user-42', SECRET)
        self.assertIn('格式不正确', str(caught.exception))

    def test_expired_assertion_is_rejected_without_leeway(self):
        now = 1_000_000.0
        token = mcp_subject.build('user-42', secret=SECRET, ttl_seconds=60, now=now)
        # 容忍时钟偏差：过期 10 秒、允许 30 秒偏差 → 仍可用
        self.assertEqual(mcp_subject.verify(token, SECRET, now=now + 70)['sub'], 'user-42')
        with self.assertRaises(mcp_subject.SubjectAssertionError) as caught:
            mcp_subject.verify(token, SECRET, now=now + 120)
        self.assertIn('已过期', str(caught.exception))

    def test_ttl_is_clamped(self):
        token = mcp_subject.build('user-42', secret=SECRET, ttl_seconds=10 ** 6)
        payload = mcp_subject.verify(token, SECRET)
        self.assertLessEqual(payload['exp'] - payload['iat'],
                            mcp_subject.MAX_TTL_SECONDS)

    def test_empty_subject_or_secret_is_refused(self):
        with self.assertRaises(mcp_subject.SubjectAssertionError):
            mcp_subject.build('', secret=SECRET)
        with self.assertRaises(mcp_subject.SubjectAssertionError):
            mcp_subject.build('user-42', secret='')


class HttpForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        credentials.invalidate_cache()

    async def test_streamable_http_carries_a_verifiable_assertion(self):
        captured = {}

        @asynccontextmanager
        async def fake_streamable(url, http_client):
            captured['url'] = url
            captured['headers'] = http_client.headers
            raise _Stop
            yield  # pragma: no cover

        with _session(), patch.object(mcp_adapter, 'streamable_http_client',
                                      fake_streamable):
            with self.assertRaises(_Stop):
                async with mcp_adapter.connect_session(_server()):
                    pass                            # pragma: no cover

        headers = captured['headers']
        self.assertEqual(_header(headers, 'Authorization'), 'Bearer tenant-token')
        token = _header(headers, mcp_subject.DEFAULT_HEADER)
        payload = mcp_subject.verify(token, SECRET)
        self.assertEqual(payload['sub'], 'user-42')
        self.assertEqual(payload['platform'], 'vendorx')
        self.assertEqual(payload['tenant'], 'studio-demo')
        self.assertEqual(payload['run'], 'run-1')

    async def test_sse_carries_the_same_assertion(self):
        captured = {}

        @asynccontextmanager
        async def fake_sse(url, headers=None, httpx_client_factory=None):
            captured['url'] = url
            captured['headers'] = headers or {}
            raise _Stop
            yield  # pragma: no cover

        with _session(), patch.object(mcp_adapter, 'sse_client', fake_sse):
            with self.assertRaises(_Stop):
                async with mcp_adapter.connect_session(_server(transport='sse')):
                    pass                            # pragma: no cover
        payload = mcp_subject.verify(
            _header(captured['headers'], mcp_subject.DEFAULT_HEADER), SECRET)
        self.assertEqual(payload['sub'], 'user-42')

    async def test_custom_header_name_is_honoured(self):
        captured = {}

        @asynccontextmanager
        async def fake_streamable(url, http_client):
            captured['headers'] = http_client.headers
            raise _Stop
            yield  # pragma: no cover

        server = _server(subject_header='X-Vendor-Subject')
        with _session(), patch.object(mcp_adapter, 'streamable_http_client',
                                      fake_streamable):
            with self.assertRaises(_Stop):
                async with mcp_adapter.connect_session(server):
                    pass                            # pragma: no cover
        self.assertTrue(_header(captured['headers'], 'X-Vendor-Subject'))
        self.assertEqual(_header(captured['headers'], mcp_subject.DEFAULT_HEADER), '')

    async def test_missing_secret_ref_refuses_before_any_transport(self):
        """没配密钥就拒绝，不退化成发明文主体头。"""
        opened = {'called': False}

        @asynccontextmanager
        async def fake_streamable(url, http_client):
            opened['called'] = True
            raise _Stop
            yield  # pragma: no cover

        server = _server(subject_secret_ref='')
        with _session(), patch.object(mcp_adapter, 'streamable_http_client',
                                      fake_streamable):
            with self.assertRaises(credentials.CredentialMissing) as caught:
                async with mcp_adapter.connect_session(server):
                    pass                            # pragma: no cover
        self.assertFalse(opened['called'], '配置不全时不该先建传输')
        self.assertIn('subject_secret_ref', str(caught.exception))
        self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                         'not_executed')

    async def test_stdio_keeps_using_the_environment_variable(self):
        """stdio 不受影响：主体仍以环境变量交给子进程，不走签名头。"""
        captured = {}

        @asynccontextmanager
        async def fake_stdio(params):
            captured['env'] = dict(params.env or {})
            raise _Stop
            yield  # pragma: no cover

        # 故意不配 subject_secret_ref：stdio 不该因此被拒
        server = _server(transport='stdio', stdio_command='/bin/true',
                         subject_secret_ref='')
        with _session(), patch.object(mcp_adapter, 'stdio_client', fake_stdio):
            with self.assertRaises(_Stop):
                # 真实链路里 token 由 tools._subject_token 解析后显式传入
                async with mcp_adapter.connect_session(server, token='tenant-token'):
                    pass                            # pragma: no cover
        self.assertEqual(captured['env']['MCP_CALLER_SUBJECT'], 'user-42')
        self.assertEqual(captured['env']['MCP_BEARER_TOKEN'], 'tenant-token')

    async def test_no_forward_subject_means_no_identity_header(self):
        captured = {}

        @asynccontextmanager
        async def fake_streamable(url, http_client):
            captured['headers'] = http_client.headers
            raise _Stop
            yield  # pragma: no cover

        with _session(), patch.object(mcp_adapter, 'streamable_http_client',
                                      fake_streamable):
            with self.assertRaises(_Stop):
                async with mcp_adapter.connect_session(
                        _server(forward_subject=False)):
                    pass                            # pragma: no cover
        self.assertEqual(_header(captured['headers'], mcp_subject.DEFAULT_HEADER), '')
        self.assertEqual(_header(captured['headers'], 'Authorization'),
                         'Bearer tenant-token')

    async def test_empty_platform_subject_is_refused(self):
        with _session(subject=''):
            with self.assertRaises(RuntimeError) as caught:
                await mcp_adapter.subject_assertion_headers(_server())
        self.assertIn('已验证的登录来源主体', str(caught.exception))


class McpStatusTests(unittest.TestCase):
    """健康检查必须看得到按运行隔离的发现结果（跨机器掉线时唯一的线索）。"""

    def setUp(self):
        from webpage_agent.tools import core as tools
        self.tools = tools
        self.saved_global = dict(tools._MCP_CACHE)
        self.saved_scoped = dict(tools._SCOPED_MCP_CACHES)
        tools._MCP_CACHE.update(at=0.0, tools=[], errors={}, signature=(),
                                capabilities={})
        tools._SCOPED_MCP_CACHES.clear()

    def tearDown(self):
        self.tools._MCP_CACHE.update(self.saved_global)
        self.tools._SCOPED_MCP_CACHES.clear()
        self.tools._SCOPED_MCP_CACHES.update(self.saved_scoped)

    def test_errors_from_a_scoped_cache_are_visible(self):
        self.tools._SCOPED_MCP_CACHES[('store', 'run-1', 1)] = {
            'at': 10.0, 'tools': [{'name': 'mcp__vendorx__ping'}],
            'errors': {'vendorx': 'McpError: Connection closed'},
            'signature': (), 'capabilities': {}}
        status = self.tools.mcp_status()
        self.assertEqual(status['errors'].get('vendorx'),
                         'McpError: Connection closed')
        self.assertEqual(status['tools'], 1)
        self.assertEqual(status['scopes'], 1)

    def test_newest_scope_wins_per_server(self):
        self.tools._SCOPED_MCP_CACHES[('s', 'old', 1)] = {
            'at': 1.0, 'tools': [], 'errors': {'vendorx': '旧失败'},
            'signature': (), 'capabilities': {}}
        self.tools._SCOPED_MCP_CACHES[('s', 'new', 1)] = {
            'at': 2.0, 'tools': [SimpleNamespace(server_id='vendorx')],
            'errors': {}, 'signature': (), 'capabilities': {}}
        self.assertEqual(self.tools.mcp_status()['errors'], {})


if __name__ == '__main__':
    unittest.main()
