"""连接器 OAuth：参数拼得对不对、state 能不能重放、刷新会不会串主体。

这些用例不联网：`oauth_client._request` 是唯一的网络出口，替换掉它就能覆盖
授权流程里真正容易出错的地方（PKCE、resource、redirect_uri、refresh 轮换）。
"""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webpage_agent.core import oauth_store
from webpage_agent.integrations import mcp_servers, oauth_client


class _Resp:
    def __init__(self, status=200, body=None, text=''):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body


META = {
    'issuer': 'https://mcp.example.test',
    'authorization_endpoint': 'https://mcp.example.test/oauth/authorize',
    'token_endpoint': 'https://mcp.example.test/oauth/token',
    'registration_endpoint': 'https://mcp.example.test/oauth/register',
}


class _FakeRequest:
    """记录调用参数，按顺序返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, method, url, **kwargs):
        self.calls.append({'method': method, 'url': url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class PkceAndUrlTests(unittest.TestCase):
    def test_pkce_is_s256(self):
        import base64
        import hashlib
        verifier, challenge = oauth_client.make_pkce()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode('ascii')).digest()).decode().rstrip('=')
        self.assertEqual(challenge, expected)
        self.assertGreaterEqual(len(verifier), 43)
        self.assertNotIn('=', verifier)

    def test_authorize_url_carries_every_required_param(self):
        url = oauth_client.build_authorize_url(
            META, client_id='cid', redirect_uri='http://127.0.0.1:8002/cb',
            scopes=('a:read', 'b:write'), state='st', challenge='ch',
            resource='https://mcp.example.test/mcp')
        self.assertTrue(url.startswith(META['authorization_endpoint'] + '?'))
        for fragment in ('response_type=code', 'client_id=cid', 'state=st',
                         'code_challenge=ch', 'code_challenge_method=S256',
                         'scope=a%3Aread+b%3Awrite',
                         'redirect_uri=http%3A%2F%2F127.0.0.1%3A8002%2Fcb',
                         'resource=https%3A%2F%2Fmcp.example.test%2Fmcp'):
            self.assertIn(fragment, url, fragment)

    def test_redirect_uri_only_loopback_or_https(self):
        self.assertEqual(oauth_client.validate_redirect_uri('http://127.0.0.1:8002/cb'),
                         'http://127.0.0.1:8002/cb')
        self.assertEqual(oauth_client.validate_redirect_uri('https://a.test/cb'),
                         'https://a.test/cb')
        # 局域网明文回调 = 把授权码交给同网段任何人
        for bad in ('http://192.168.1.5/cb', 'http://example.test/cb', 'ftp://a/cb', ''):
            with self.assertRaises(oauth_client.OAuthError):
                oauth_client.validate_redirect_uri(bad)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        oauth_client.clear_cache()

    async def test_discover_falls_back_to_oidc(self):
        fake = _FakeRequest([_Resp(404, {}), _Resp(200, META)])
        with patch.object(oauth_client, '_request', fake):
            meta = await oauth_client.discover('https://mcp.example.test')
        self.assertEqual(meta['token_endpoint'], META['token_endpoint'])
        self.assertIn('openid-configuration', fake.calls[1]['url'])

    async def test_discover_rejects_metadata_without_endpoints(self):
        fake = _FakeRequest([_Resp(200, {'issuer': 'x'}),
                             _Resp(200, {'issuer': 'x'})])
        with patch.object(oauth_client, '_request', fake):
            with self.assertRaises(oauth_client.OAuthError):
                await oauth_client.discover('https://mcp.example.test')

    async def test_register_client_is_public_and_pkce_only(self):
        fake = _FakeRequest([_Resp(201, {'client_id': 'new-id'})])
        with patch.object(oauth_client, '_request', fake):
            creds = await oauth_client.register_client(
                META, redirect_uri='http://127.0.0.1:8002/cb',
                client_name='测试中台', scopes=('a:read',))
        body = fake.calls[0]['json']
        self.assertEqual(body['token_endpoint_auth_method'], 'none')
        self.assertEqual(body['redirect_uris'], ['http://127.0.0.1:8002/cb'])
        self.assertEqual(body['grant_types'], ['authorization_code', 'refresh_token'])
        self.assertEqual(creds['client_id'], 'new-id')

    async def test_register_client_without_endpoint_raises(self):
        meta = {k: v for k, v in META.items() if k != 'registration_endpoint'}
        with self.assertRaises(oauth_client.OAuthError) as ctx:
            await oauth_client.register_client(
                meta, redirect_uri='http://127.0.0.1:8002/cb',
                client_name='x', scopes=())
        self.assertIn('registration_endpoint', str(ctx.exception))


class TokenFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        oauth_client.clear_cache()

    async def test_exchange_sends_verifier_and_resource(self):
        fake = _FakeRequest([_Resp(200, {'access_token': 'at', 'expires_in': 3600,
                                         'refresh_token': 'rt', 'scope': 'a:read'})])
        with patch.object(oauth_client, '_request', fake):
            tokens = await oauth_client.exchange_code(
                META, code='c0de', code_verifier='v', client_id='cid',
                redirect_uri='http://127.0.0.1:8002/cb', resource='https://r/mcp')
        form = fake.calls[0]['data']
        self.assertEqual(form['grant_type'], 'authorization_code')
        self.assertEqual(form['code_verifier'], 'v')
        self.assertEqual(form['resource'], 'https://r/mcp')
        self.assertGreater(tokens['expires_at'], int(time.time() * 1000))

    async def test_exchange_error_does_not_leak_code(self):
        fake = _FakeRequest([_Resp(400, {'error': 'invalid_grant'})])
        with patch.object(oauth_client, '_request', fake):
            with self.assertRaises(oauth_client.OAuthError) as ctx:
                await oauth_client.exchange_code(
                    META, code='SECRET-CODE', code_verifier='v', client_id='cid',
                    redirect_uri='http://127.0.0.1:8002/cb')
        self.assertNotIn('SECRET-CODE', str(ctx.exception))

    async def test_refresh_keeps_old_refresh_token_when_absent(self):
        fake = _FakeRequest([_Resp(200, {'access_token': 'at2', 'expires_in': 60})])
        with patch.object(oauth_client, '_request', fake):
            tokens = await oauth_client.refresh(
                META, refresh_token='rt-old', client_id='cid')
        self.assertEqual(tokens['refresh_token'], 'rt-old')
        self.assertEqual(fake.calls[0]['data']['grant_type'], 'refresh_token')


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        oauth_client.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(oauth_store, 'storage_dir',
                               return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, *, access='at', expires_at=0, refresh='rt'):
        oauth_store.save(
            tenant_id='t1', user_id='u1', server_id='libtv',
            data={'client_id': 'cid', 'issuer': META['issuer'],
                  'service': 'https://mcp.example.test/mcp',
                  'tokens': {'access_token': access, 'refresh_token': refresh,
                             'expires_at': expires_at}})

    def test_file_is_owner_only(self):
        self._write()
        path = oauth_store.credentials_path(tenant_id='t1', user_id='u1',
                                            server_id='libtv')
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_credentials_are_per_subject(self):
        self._write()
        self.assertTrue(oauth_store.has_token(tenant_id='t1', user_id='u1',
                                              server_id='libtv'))
        self.assertFalse(oauth_store.has_token(tenant_id='t1', user_id='u2',
                                               server_id='libtv'))

    async def test_expired_token_is_refreshed_and_written_back(self):
        self._write(expires_at=int(time.time() * 1000) - 1000)
        # 第一个响应给元数据（刷新前要先发现 token_endpoint），第二个才是刷新结果。
        fake = _FakeRequest([
            _Resp(200, META),
            _Resp(200, {'access_token': 'at-new', 'expires_in': 3600,
                        'refresh_token': 'rt-new'}),
        ])
        server = mcp_servers.Server(id='libtv', transport='streamable_http',
                                    url='https://mcp.example.test/mcp',
                                    auth='oauth', oauth_issuer=META['issuer'])
        with patch.object(oauth_client, '_request', fake):
            token = await oauth_store.ensure_access_token(
                server=server, tenant_id='t1', user_id='u1', server_id='libtv')
        self.assertEqual(token, 'at-new')
        data = oauth_store.load(tenant_id='t1', user_id='u1', server_id='libtv')
        self.assertEqual(data['tokens']['access_token'], 'at-new')
        self.assertEqual(data['tokens']['refresh_token'], 'rt-new')

    async def test_revoked_refresh_clears_credentials(self):
        self._write(expires_at=int(time.time() * 1000) - 1000)
        fake = _FakeRequest([_Resp(200, META), _Resp(400, {'error': 'invalid_grant'})])
        server = mcp_servers.Server(id='libtv', transport='streamable_http',
                                    url='https://mcp.example.test/mcp',
                                    auth='oauth', oauth_issuer=META['issuer'])
        with patch.object(oauth_client, '_request', fake):
            with self.assertRaises(oauth_store.OAuthAuthorizationRequired):
                await oauth_store.ensure_access_token(
                    server=server, tenant_id='t1', user_id='u1', server_id='libtv')
        # 留着一份必然失败的凭据只会让每次调用都多打一次网络
        self.assertFalse(oauth_store.has_token(tenant_id='t1', user_id='u1',
                                               server_id='libtv'))

    async def test_missing_credentials_asks_for_authorization(self):
        server = mcp_servers.Server(id='libtv', transport='streamable_http',
                                    url='https://mcp.example.test/mcp',
                                    auth='oauth', oauth_issuer=META['issuer'])
        with self.assertRaises(oauth_store.OAuthAuthorizationRequired):
            await oauth_store.ensure_access_token(
                server=server, tenant_id='t1', user_id='u1', server_id='libtv')


class ConfigParsingTests(unittest.TestCase):
    def _parse(self, entry):
        with patch.dict(os.environ, {'MCP_SERVERS': json.dumps([entry])}, clear=False):
            servers = mcp_servers.servers()
        return servers[0] if servers else None

    def test_oauth_entry_is_parsed(self):
        server = self._parse({
            'id': 'libtv', 'transport': 'streamable_http',
            'url': 'https://mcp.example.test/mcp', 'auth': 'oauth',
            'oauth': {'issuer': 'https://mcp.example.test',
                      'scopes': ['a:read'], 'client_name': '中台'},
        })
        self.assertIsNotNone(server)
        self.assertEqual(server.auth, 'oauth')
        self.assertEqual(server.oauth_scopes, ('a:read',))
        self.assertEqual(server.oauth_client_name, '中台')

    def test_oauth_on_stdio_is_rejected(self):
        """stdio 没有 Authorization 头，配了 oauth 只会得到一个永远连不上的条目。"""
        server = self._parse({
            'id': 'bad', 'transport': 'stdio', 'stdio_command': 'node',
            'auth': 'oauth', 'oauth': {'issuer': 'https://x.test'},
        })
        self.assertIsNone(server)

    def test_oauth_without_issuer_is_rejected(self):
        server = self._parse({
            'id': 'bad', 'transport': 'streamable_http', 'url': 'https://x.test/mcp',
            'auth': 'oauth',
        })
        self.assertIsNone(server)


class StateTests(unittest.TestCase):
    """state 是一次性凭证：重放必须失败。"""

    def test_state_is_single_use(self):
        from webpage_agent.api import connectors_api
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(connectors_api, '_OAUTH_STATE_DIR', Path(tmp.name)):
            connectors_api._save_state('abc123', {'state': 'abc123', 'user_id': 'u'})
            self.assertEqual(connectors_api._take_state('abc123')['user_id'], 'u')
            self.assertIsNone(connectors_api._take_state('abc123'))

    def test_state_path_traversal_is_neutralised(self):
        from webpage_agent.api import connectors_api
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(connectors_api, '_OAUTH_STATE_DIR', Path(tmp.name)):
            connectors_api._save_state('../../etc/passwd', {'state': 'x'})
            files = list(Path(tmp.name).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].parent, Path(tmp.name))


if __name__ == '__main__':
    unittest.main()
