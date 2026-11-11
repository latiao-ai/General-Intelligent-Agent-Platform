"""管理控制台的准入门槛。

这里只测**判断逻辑**，不连库：控制台的越权风险几乎全集中在"这次请求算哪一个档"
和"写操作认不认这个来源"两件事上，而这两件事都是纯函数。真正的数据面在
`pg_admin` 里，由 `tests/test_admin_users.py` 那套按租户隔离的用例覆盖。
"""
import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from pydantic import ValidationError
from starlette.requests import Request

from webpage_agent.api import console_api
from webpage_agent.governance.policies import Principal


def _request(origin: str = '', host: str = '127.0.0.1:8000') -> Request:
    headers = [(b'host', host.encode())]
    if origin:
        headers.append((b'origin', origin.encode()))
    return Request({'type': 'http', 'method': 'PATCH', 'headers': headers})


class AccessScopeTests(unittest.TestCase):
    def setUp(self):
        self.settings = replace(console_api.settings, app_env='development',
                                auth_mode='ticket', auth_allowed_origins=())

    def scope(self, principal, **overrides):
        with patch.object(console_api, 'settings', replace(self.settings, **overrides)):
            return console_api.console_access(principal)

    def test_role_maps_to_scope(self):
        self.assertEqual(self.scope(Principal(authenticated=True, role='platform_admin')),
                         console_api.ACCESS_ALL)
        self.assertEqual(self.scope(Principal(authenticated=True, role='operator')),
                         console_api.ACCESS_TENANT)
        self.assertEqual(self.scope(Principal(authenticated=True, role='member')), '')

    def test_operator_is_not_platform_admin(self):
        """operator 只能进本企业视图。这条挂掉等于跨租户泄露。"""
        principal = Principal(authenticated=True, role='operator', tenant_id='tenant-a')
        self.assertEqual(self.scope(principal), console_api.ACCESS_TENANT)
        with self.assertRaises(Exception) as error:
            with patch.object(console_api, 'settings', self.settings):
                console_api.require_all(principal)
        self.assertEqual(error.exception.status_code, 403)

    def test_dev_off_mode_opens_console(self):
        """本地开发要进得去：AUTH_MODE=off 时没有任何登录态，否则页面永远 403。"""
        anonymous = Principal()
        self.assertEqual(
            self.scope(anonymous, auth_mode='off', app_env='development'),
            console_api.ACCESS_ALL)

    def test_off_mode_never_opens_in_production(self):
        """生产环境把 AUTH_MODE 关掉是配置事故，控制台不能跟着裸奔。

        `test` 环境是例外：集成测试要能在没有登录态的情况下走通控制台，
        而它不可能出现在对外部署里。
        """
        self.assertEqual(self.scope(Principal(), auth_mode='off', app_env='production'), '')
        self.assertEqual(self.scope(Principal(), auth_mode='off', app_env='test'),
                         console_api.ACCESS_ALL)

    def test_anonymous_in_ticket_mode_has_no_access(self):
        self.assertEqual(self.scope(Principal()), '')

    def test_unknown_role_falls_back_to_no_access(self):
        self.assertEqual(self.scope(Principal(authenticated=True, role='superuser')), '')


class TrustedOriginTests(unittest.TestCase):
    def setUp(self):
        self.settings = replace(console_api.settings, app_env='development',
                                auth_allowed_origins=('http://127.0.0.1:8000',))

    def check(self, request, **overrides):
        with patch.object(console_api, 'settings', replace(self.settings, **overrides)):
            console_api.require_trusted_origin(request)

    def test_allowlisted_origin_passes(self):
        self.check(_request('http://127.0.0.1:8000'))

    def test_cross_origin_is_rejected(self):
        with self.assertRaises(Exception) as error:
            self.check(_request('http://evil.test'))
        self.assertEqual(error.exception.status_code, 403)

    def test_production_requires_an_origin(self):
        """生产环境不接受"没有 Origin"的写请求：浏览器对同源 PATCH 也会带 Origin，
        不带的就是脚本，而控制台不打算让脚本改数据。"""
        with self.assertRaises(Exception) as error:
            self.check(_request(''), app_env='production')
        self.assertEqual(error.exception.status_code, 403)

    def test_development_allows_originless_clients(self):
        self.check(_request(''))

    def test_development_allows_same_origin(self):
        self.check(_request('http://127.0.0.1:8000', host='127.0.0.1:8000'),
                   auth_allowed_origins=())

    def test_malformed_origin_is_rejected(self):
        for origin in ('http://127.0.0.1:8000/path', 'http://user:pw@127.0.0.1:8000',
                       'http://127.0.0.1:8000?x=1'):
            with self.subTest(origin=origin):
                with self.assertRaises(Exception):
                    self.check(_request(origin))


class DeleteAccountTests(unittest.TestCase):
    """删账号的边界。守卫本身在 `pg_admin.console_delete_user`（要事务和锁），
    这里测的是 API 层不放行不该放行的人。"""

    class _Store:
        def __init__(self):
            self.calls = []

        async def console_delete_user(self, **kwargs):
            self.calls.append(kwargs)
            return {'user_id': kwargs['user_id'], 'deleted': True,
                    'revoked_sessions': 2, 'identities': 1,
                    'retained': {'sessions': 3, 'messages': 4, 'tasks': 0}}

    def setUp(self):
        self.settings = replace(console_api.settings, app_env='development',
                                auth_mode='ticket', auth_allowed_origins=())
        self.request = _request('', host='127.0.0.1:8000')

    def call(self, principal, **payload):
        store = self._Store()
        with patch.object(console_api, 'settings', self.settings):
            response = asyncio.run(console_api.console_delete_user(
                user_id='u_target', payload=console_api.DeleteRequest(**payload),
                request=self.request, principal=principal, store=store))
        return response, store

    def test_only_platform_admin_can_delete(self):
        """租户运维能删掉这个人在**所有**租户的身份，超出"本企业运维"边界。"""
        with self.assertRaises(Exception) as error:
            self.call(Principal(authenticated=True, role='operator'),
                      confirm='u_target')
        self.assertEqual(error.exception.status_code, 403)

    def test_confirm_must_match_the_target(self):
        """确认串对不上就不删——这是防手滑点的最后一道。"""
        with self.assertRaises(Exception) as error:
            self.call(Principal(authenticated=True, role='platform_admin'),
                      confirm='u_other')
        self.assertEqual(error.exception.status_code, 400)

    def test_delete_passes_actor_for_the_audit_trail(self):
        response, store = self.call(
            Principal(authenticated=True, role='platform_admin', user_id='u_admin'),
            confirm='u_target', reason='离职')
        self.assertEqual(store.calls[0]['actor_id'], 'u_admin')
        self.assertEqual(store.calls[0]['reason'], '离职')
        self.assertTrue(json.loads(response.body)['deleted'])

    def test_only_two_roles_can_be_granted(self):
        """控制台只保留两档：普通用户与超管。operator 仍被识别，但不再授予。"""
        self.assertEqual(console_api._ASSIGNABLE_ROLES, ('member', 'platform_admin'))
        for role in ('member', 'platform_admin'):
            self.assertEqual(console_api.RoleRequest(role=role).role, role)
        with self.assertRaises(ValidationError):
            console_api.RoleRequest(role='operator')


if __name__ == '__main__':
    unittest.main()
