"""登录平台登记表 + 账号密码登录链路。

三块东西值得单独锁住，因为它们出错时都是"静默"的：

1. **取值路径**（`pick`）。平台返回的形状千差万别，路径写错不会报错，只会
   拿到 None，然后表现成"平台未返回用户标识"——一个看不出真正原因的 502；
2. **配置校验**（`normalize_config`）。`login_path` 与点路径的规则完全不同，
   放宽任何一条都会让拼接出的 URL 跑到根地址之外；
3. **失败分类**（`sign_in`）。401/403/429/502 的区分是前端唯一能给的提示，
   把"密码错"和"平台挂了"混成一个，用户会一直重试密码。

不连库：库里那张表的行为由 `platforms` 的读写方法自带事务保证，而这里要测的是
"给定配置，登录链路的判定对不对"。
"""
import asyncio
import unittest

import httpx

from webpage_agent.api import platform_login
from webpage_agent.store.pg_platforms import DEFAULT_CONFIG, normalize_config


def _platform(**overrides):
    item = {
        'platform_id': 'demo',
        'display_name': 'Demo',
        'login_mode': 'password_http',
        'enabled': True,
        'tenant_id': 'tenant-demo',
        'identity_provider_id': 'idp-demo',
        'base_url': 'http://127.0.0.1:8099/api',
        'config': dict(DEFAULT_CONFIG),
    }
    item.update(overrides)
    return item


class FakeStore:
    """只实现 `resolve` 用到的两个方法。"""

    def __init__(self, platform=None):
        self.platform = platform

    async def platform_login_config(self, platform_id=''):
        return self.platform

    async def platform_public_list(self):
        return [] if self.platform is None else [
            {'platform_id': self.platform['platform_id'],
             'display_name': self.platform['display_name']}]


def _run(store, **kwargs):
    return asyncio.run(platform_login.sign_in(store, **kwargs))


def _run_with(handler, store, **kwargs):
    """带 MockTransport 跑一次登录：AsyncClient 必须 `async with` 打开。"""
    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await platform_login.sign_in(store, client=client, **kwargs)
    return asyncio.run(go())


class PickTests(unittest.TestCase):
    def test_nested_value(self):
        payload = {'data': {'user': {'id': 'u-1'}}}
        self.assertEqual(platform_login.pick(payload, 'data.user.id'), 'u-1')

    def test_missing_segment_is_none(self):
        self.assertIsNone(platform_login.pick({'data': {}}, 'data.user.id'))
        self.assertIsNone(platform_login.pick({'data': {'user': None}}, 'data.user.id'))

    def test_non_dict_midway_returns_none(self):
        """`data` 是个字符串时继续往下走会 TypeError；这里必须安静地返回 None。"""
        self.assertIsNone(platform_login.pick({'data': 'oops'}, 'data.user.id'))

    def test_empty_path_returns_payload(self):
        self.assertEqual(platform_login.pick({'a': 1}, ''), {'a': 1})


class ConfigTests(unittest.TestCase):
    def test_defaults_fill_in_beeftv_shape(self):
        config = normalize_config({})
        self.assertEqual(config['login_path'], '/auth/login')
        self.assertEqual(config['subject_path'], 'data.user.id')
        self.assertEqual(config['timeout_seconds'], 10)

    def test_login_path_must_be_absolute(self):
        with self.assertRaises(ValueError):
            normalize_config({'login_path': 'auth/login'})

    def test_login_path_rejects_parent_escape(self):
        with self.assertRaises(ValueError):
            normalize_config({'login_path': '/auth/../../admin'})

    def test_dotted_path_rejects_slash(self):
        with self.assertRaises(ValueError):
            normalize_config({'subject_path': '/data/user/id'})

    def test_timeout_is_clamped(self):
        self.assertEqual(normalize_config({'timeout_seconds': 0})['timeout_seconds'], 1)
        self.assertEqual(normalize_config({'timeout_seconds': 900})['timeout_seconds'], 30)

    def test_unknown_keys_are_dropped(self):
        """未知键最终会被拼进 HTTP 请求的语义里，宁可不存。"""
        config = normalize_config({'headers': {'X-Token': 'secret'}})
        self.assertNotIn('headers', config)


class BaseUrlTests(unittest.TestCase):
    def test_accepts_plain_base(self):
        self.assertEqual(platform_login.validate_base_url('http://127.0.0.1:9/api/'),
                         'http://127.0.0.1:9/api')

    def test_rejects_query_and_fragment(self):
        for raw in ('http://127.0.0.1:9/api?x=1', 'http://127.0.0.1:9/api#f'):
            with self.assertRaises(platform_login.PlatformLoginError):
                platform_login.validate_base_url(raw)

    def test_rejects_non_http_scheme(self):
        with self.assertRaises(platform_login.PlatformLoginError):
            platform_login.validate_base_url('file:///etc/passwd')

    def test_rejects_empty(self):
        with self.assertRaises(platform_login.PlatformLoginError) as error:
            platform_login.validate_base_url('')
        self.assertEqual(error.exception.status, 404)


class SignInTests(unittest.TestCase):
    def test_beeftv_shaped_success(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen['url'] = str(request.url)
            seen['body'] = request.content
            return httpx.Response(200, json={'data': {'user': {
                'id': 'u-9', 'username': '张三', 'status': 'active',
                'role': 'admin'}}})

        store = FakeStore(_platform())
        result = _run_with(handler, store, platform_id='demo', username='alice',
                           password='pw')
        self.assertEqual(seen['url'], 'http://127.0.0.1:8099/api/auth/login')
        self.assertIn(b'alice', seen['body'])
        self.assertEqual(result['subject'], 'u-9')
        self.assertEqual(result['display_name'], '张三')
        # admin/owner/operator 都映射成 operator（本企业运维），不是超管
        self.assertEqual(result['role'], 'operator')
        self.assertEqual(result['tenant_id'], 'tenant-demo')
        self.assertEqual(result['identity_provider_id'], 'idp-demo')

    def test_custom_field_mapping(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={'user': {'uid': 'x-1', 'name': 'Bob'}})

        config = dict(DEFAULT_CONFIG, subject_path='user.uid',
                      display_name_path='user.name')
        store = FakeStore(_platform(config=config))
        result = _run_with(handler, store, platform_id='demo', username='bob',
                           password='pw')
        self.assertEqual(result['subject'], 'x-1')
        self.assertEqual(result['display_name'], 'Bob')

    def test_wrong_password_is_401(self):
        def handler(request):
            return httpx.Response(401, json={'detail': 'bad'})

        with self.assertRaises(platform_login.PlatformLoginError) as error:
            _run_with(handler, FakeStore(_platform()), platform_id='demo',
                      username='alice', password='x')
        self.assertEqual(error.exception.status, 401)

    def test_rate_limited_is_429(self):
        def handler(request):
            return httpx.Response(429)

        with self.assertRaises(platform_login.PlatformLoginError) as error:
            _run_with(handler, FakeStore(_platform()), platform_id='demo',
                      username='a', password='b')
        self.assertEqual(error.exception.status, 429)

    def test_missing_subject_is_502(self):
        """路径写错就走到这里。它不该变成 401，否则用户会去改密码。"""
        def handler(request):
            return httpx.Response(200, json={'data': {'user': {'username': 'x'}}})

        with self.assertRaises(platform_login.PlatformLoginError) as error:
            _run_with(handler, FakeStore(_platform()), platform_id='demo',
                      username='a', password='b')
        self.assertEqual(error.exception.status, 502)
        self.assertIn('用户标识', error.exception.detail)

    def test_disabled_platform_account_is_403(self):
        def handler(request):
            return httpx.Response(200, json={'data': {'user': {
                'id': 'u-1', 'status': 'disabled'}}})

        with self.assertRaises(platform_login.PlatformLoginError) as error:
            _run_with(handler, FakeStore(_platform()), platform_id='demo',
                      username='a', password='b')
        self.assertEqual(error.exception.status, 403)

    def test_no_platform_is_404(self):
        """库和环境变量都没有平台时，这条入口就是关着的。

        本机 `.env` 里通常有 `BEEFTV_API_BASE`，不清掉的话会走兜底分支、
        真的去连 8004。这里只测"两边都没有"的情形。
        """
        import os
        from unittest.mock import patch

        with patch.dict(os.environ):
            os.environ.pop('BEEFTV_API_BASE', None)
            with self.assertRaises(platform_login.PlatformLoginError) as error:
                _run(FakeStore(None), platform_id='', username='a', password='b')
        self.assertEqual(error.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
