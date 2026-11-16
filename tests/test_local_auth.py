"""本地账号登录（第 4 种方式）的单元测试：不碰数据库、不碰网络。

覆盖密码哈希、归一化、失败路径的统一文案、失败锁定、强制改密，以及
「账号不存在时也跑满一次校验」这条防枚举约束；还有超管对任意用户
（含外部身份源）重置密码时两条分支的走向。
"""
import asyncio
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from starlette.requests import Request

from webpage_agent.api import console_api
from webpage_agent.api import local_auth as la
from webpage_agent.core.config import settings
from webpage_agent.governance.policies import Principal


class PasswordTests(unittest.TestCase):

    def test_hash_and_verify(self):
        encoded = la.hash_password('correct horse')
        self.assertTrue(la.verify_password('correct horse', encoded))
        self.assertFalse(la.verify_password('Correct horse', encoded))
        self.assertFalse(la.needs_rehash(encoded))
        self.assertNotEqual(encoded, la.hash_password('correct horse'))

    def test_old_parameters_need_rehash(self):
        self.assertTrue(la.needs_rehash(
            'scrypt$8192$8$1$c2FsdA==' + '$' + 'a' * 44))

    def test_dummy_hash_is_a_valid_encoding(self):
        # 账号不存在时用 DUMMY_HASH 跑满一次 scrypt；格式必须能被解析，
        # 否则「不存在」的响应时间会明显短于「密码错」。
        self.assertFalse(la.verify_password('anything', la.DUMMY_HASH))

    def test_username_normalization(self):
        self.assertEqual(la.normalize_username('  Test.User '), 'test.user')
        for bad in ('ab', 'has space', 'UPPER!', '', 'a' * 40):
            with self.assertRaises(la.LocalAuthError):
                la.normalize_username(bad)

    def test_password_policy(self):
        self.assertEqual(la.validate_password('12345678'), '12345678')
        with self.assertRaises(la.LocalAuthError) as err:
            la.validate_password('a' * 200)
        self.assertEqual(err.exception.status, 422)

    def test_production_requires_secure_cookie_and_long_password(self):
        with patch.object(la, 'settings', replace(
                settings, app_env='production', auth_mode='ticket',
                local_auth_enabled=True, auth_cookie_secure=True,
                local_auth_min_length=8)):
            self.assertTrue(la.enabled())
        with patch.object(la, 'settings', replace(
                settings, app_env='production', auth_mode='ticket',
                local_auth_enabled=True, auth_cookie_secure=False,
                local_auth_min_length=8)):
            self.assertFalse(la.enabled())
        with patch.object(la, 'settings', replace(
                settings, app_env='production', auth_mode='ticket',
                local_auth_enabled=True, auth_cookie_secure=True,
                local_auth_min_length=4)):
            self.assertFalse(la.enabled())
            with self.assertRaises(RuntimeError):
                la.startup_check()


class _FakeStore:
    def __init__(self, row=None):
        self.row = row
        self.failures = 0
        self.successes = 0
        self.rate_limited = False
        self.session_args = None

    async def rate_limit(self, scope, limit):
        return (not self.rate_limited, 30)

    async def local_account_for_login(self, username, tenant_id='default'):
        return self.row

    async def local_account_note_failure(self, username, *, threshold, seconds):
        self.failures += 1
        return {'failed_count': self.failures}

    async def local_account_note_success(self, username):
        self.successes += 1

    async def local_account_set_password(self, **kwargs):
        return True

    async def login_platform_identity(self, **kwargs):
        self.session_args = kwargs
        return {'user_id': 'u_test', 'tenant_id': 'default',
                'role': 'member', 'created': False,
                'auth_session_id': 'as_test'}


def _row(**overrides):
    base = {'username': 'test', 'tenant_id': 'default', 'user_id': 'u_test',
            'password_hash': la.hash_password('test'), 'password_algo': la.ALGO,
            'failed_count': 0, 'locked_until': 0, 'must_change_password': 0,
            'status': 'active', 'display_name': 'test'}
    base.update(overrides)
    return base


class SignInTests(unittest.TestCase):

    def _sign_in(self, store, username='test', password='test'):
        return asyncio.run(la.sign_in(store, username=username, password=password,
                                      client_ip='127.0.0.1'))

    def test_success_reuses_identity_pipeline(self):
        store = _FakeStore(_row())
        token, row = self._sign_in(store)
        self.assertEqual(len(token), 43)
        self.assertEqual(row['user_id'], 'u_test')
        self.assertEqual(store.successes, 1)
        self.assertEqual(store.session_args['platform_id'], 'local')
        self.assertEqual(store.session_args['identity_provider_id'], 'local')

    def test_wrong_password_and_missing_account_share_the_same_error(self):
        bad = _FakeStore(_row())
        with self.assertRaises(la.LocalAuthError) as wrong:
            self._sign_in(bad, password='nope')
        missing = _FakeStore(None)
        with self.assertRaises(la.LocalAuthError) as absent:
            self._sign_in(missing)
        self.assertEqual(wrong.exception.status, 401)
        self.assertEqual(wrong.exception.detail, absent.exception.detail)
        self.assertEqual(bad.failures, 1)
        self.assertEqual(missing.failures, 0)

    def test_disabled_account_is_not_distinguishable(self):
        store = _FakeStore(_row(status='disabled'))
        with self.assertRaises(la.LocalAuthError) as err:
            self._sign_in(store)
        self.assertEqual(err.exception.status, 401)

    def test_locked_account_returns_429(self):
        store = _FakeStore(_row(locked_until=time.time() + 300))
        with self.assertRaises(la.LocalAuthError) as err:
            self._sign_in(store)
        self.assertEqual(err.exception.status, 429)

    def test_must_change_password_returns_code(self):
        store = _FakeStore(_row(must_change_password=1))
        with self.assertRaises(la.LocalAuthError) as err:
            self._sign_in(store)
        self.assertEqual(err.exception.status, 403)
        self.assertEqual(err.exception.code, 'PASSWORD_CHANGE_REQUIRED')

    def test_ip_rate_limit_blocks_before_password_check(self):
        store = _FakeStore(_row())
        store.rate_limited = True
        with self.assertRaises(la.LocalAuthError) as err:
            self._sign_in(store)
        self.assertEqual(err.exception.status, 429)
        self.assertEqual(store.failures, 0)


class _AdminStore:
    def __init__(self, existing=None):
        self.existing = existing
        self.set_calls: list[dict] = []
        self.bind_calls: list[dict] = []
        self.unlock_calls: list[tuple] = []

    async def local_account_by_user(self, user_id):
        return self.existing

    async def local_account_set_password(self, **kwargs):
        self.set_calls.append(kwargs)
        return True

    async def local_account_bind_to_user(self, **kwargs):
        self.bind_calls.append(kwargs)
        return {'user_id': kwargs['user_id'], 'username': kwargs['username'],
                'role': 'member', 'created': True}

    async def local_account_unlock(self, *args, **kwargs):
        self.unlock_calls.append((args, kwargs))


class AdminResetPasswordTests(unittest.TestCase):
    """超管对**任意**用户重置密码：不再因为「不是本地账号」返回 404。"""

    def setUp(self):
        self.settings = replace(console_api.settings, app_env='development',
                                auth_mode='ticket', auth_allowed_origins=(),
                                local_auth_min_length=4)
        self.request = Request({'type': 'http', 'method': 'POST',
                                'headers': [(b'host', b'127.0.0.1:8000')]})
        self.principal = Principal(authenticated=True, role='platform_admin',
                                   user_id='u_admin')

    def _reset(self, store, *, password='newpass', username=''):
        with patch.object(console_api, 'settings', self.settings):
            return asyncio.run(console_api.console_reset_password(
                user_id='u_target',
                payload=console_api.ResetPasswordRequest(
                    password=password, username=username),
                request=self.request, principal=self.principal, store=store))

    def test_existing_local_account_only_changes_the_hash(self):
        store = _AdminStore({'username': 'test', 'user_id': 'u_target'})
        body = json.loads(self._reset(store).body)
        self.assertEqual(body['created'], False)
        self.assertEqual(body['username'], 'test')
        self.assertEqual(len(store.set_calls), 1)
        self.assertEqual(store.bind_calls, [])
        self.assertTrue(la.verify_password('newpass',
                                           store.set_calls[0]['password_hash']))

    def test_external_identity_is_bound_to_the_same_user_id(self):
        """BeeFTV / 短信 / mock-idp 的用户：补本地凭据，不新建账号。"""
        store = _AdminStore(None)
        body = json.loads(self._reset(store).body)
        self.assertEqual(body['created'], True)
        self.assertEqual(len(store.bind_calls), 1)
        bound = store.bind_calls[0]
        self.assertEqual(bound['user_id'], 'u_target')
        self.assertEqual(bound['username'], 'u_target')
        self.assertEqual(bound['actor_id'], 'u_admin')
        self.assertEqual(store.set_calls, [])

    def test_custom_username_is_normalized(self):
        store = _AdminStore(None)
        self._reset(store, username='  Demo.User ')
        self.assertEqual(store.bind_calls[0]['username'], 'demo.user')

    def test_bad_username_returns_422_not_401(self):
        store = _AdminStore(None)
        with self.assertRaises(Exception) as error:
            self._reset(store, username='has space')
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(store.bind_calls, [])

    def test_short_password_is_rejected(self):
        store = _AdminStore(None)
        with self.assertRaises(Exception) as error:
            self._reset(store, password='ab')
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(store.bind_calls, [])

    def test_unlock_without_local_credentials_is_a_noop(self):
        store = _AdminStore(None)
        with patch.object(console_api, 'settings', self.settings):
            response = asyncio.run(console_api.console_unlock_user(
                user_id='u_target', request=self.request,
                principal=self.principal, store=store))
        body = json.loads(response.body)
        self.assertTrue(body['ok'])
        self.assertTrue(body['noop'])
        self.assertEqual(store.unlock_calls, [])


if __name__ == '__main__':
    unittest.main()
