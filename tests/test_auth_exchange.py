"""票据兑换与登录会话：一次性、并发、事务、身份绑定、撤销。

只测这些，不调模型、不碰 MCP。数据库用 `PG_TEST_DSN` 指向的一次性测试库。

覆盖验收表：
- 正确票据 → 兑换成功、会话可用；
- 同一票据重复兑换 → 409，且不产生第二个会话；
- 并发兑换同一张票据 → 只有一个成功；
- 兑换失败/回滚 → 不留半成品；
- 库里没有明文票据与会话凭证；
- 会话过期 / 撤销 / 用户停用 → 401；
- 同一用户新票据再登录 → 中台 user_id 不变；
- API 重启后（新进程 = 新 store 实例）会话仍然有效。
"""
import asyncio
import json
import os
from dataclasses import replace
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import auth, auth_connections, ticket_auth  # noqa: E402
from tests.test_ticket_auth import (AUD, ISS, PRIVATE_A, PRIVATE_B,  # noqa: E402
                                    PUBLIC_A, claims, connection, sign)

PG_DSN = os.getenv('PG_TEST_DSN', '')
TABLES = ('auth_sessions', 'ticket_redemptions', 'identities', 'app_users',
          'memory_tombstones', 'memories', 'background_jobs', 'artifacts',
          'action_attempts', 'actions', 'steps', 'plans', 'runs', 'tasks_meta',
          'events', 'tasks', 'session_memory', 'session_summaries', 'messages',
          'sessions', 'request_keys')


def new_store():
    if not PG_DSN:
        raise unittest.SkipTest('需要 PG_TEST_DSN（一次性测试库）')
    if 'test' not in PG_DSN:
        raise unittest.SkipTest('PG_TEST_DSN 必须指向测试库（库名含 test）')
    from app.pg_store import PostgresStore
    return PostgresStore(PG_DSN)


async def reset_db(store):
    async with store._lease() as conn:
        for table in TABLES:
            try:
                await conn.execute(f'TRUNCATE TABLE {table} CASCADE')
            except Exception:                       # 表可能还没建（首次运行）
                pass


def ticket(sub='user-123', *, jti=None, ttl=30, private=PRIVATE_A,
           tenant='tenant-a', platform='pa'):
    stamp = int(time.time())
    payload = claims(sub=sub, tenant_id=tenant, platform_id=platform,
                     iat=stamp, exp=stamp + ttl,
                     jti=jti or f'jti-{time.time_ns()}')
    return sign(payload, private=private)


CONNECTIONS = [connection()]


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class ExchangeTests(unittest.IsolatedAsyncioTestCase):
    """兑换 = 验票 + 一次性登记 + 绑定身份 + 建会话（同一事务）。"""

    async def asyncSetUp(self):
        self.store = new_store()
        await reset_db(self.store)
        # 票据验证走测试里构造的登记表，避免依赖真实 .env
        self._patch = patch.object(ticket_auth.auth_connections, 'connections',
                                   lambda *a, **k: CONNECTIONS)
        self._patch.start()

    async def asyncTearDown(self):
        self._patch.stop()
        await self.store.close()

    async def test_exchange_creates_session_and_identity(self):
        result = await auth.exchange(ticket(), self.store)
        self.assertTrue(result['token'])
        self.assertEqual(result['tenant_id'], 'tenant-a')
        self.assertEqual(result['platform_id'], 'pa')

        row = await self.store.auth_session(auth.digest_of(result['token']))
        self.assertIsNotNone(row)
        self.assertEqual(row['user_id'], result['user_id'])
        self.assertEqual(row['connection_id'], 'conn-a')
        self.assertIsNone(row['revoked_at'])

    async def test_same_ticket_cannot_be_redeemed_twice(self):
        one = ticket(jti='once')
        await auth.exchange(one, self.store)
        with self.assertRaises(auth.ExchangeError) as ctx:
            await auth.exchange(one, self.store)
        self.assertEqual(ctx.exception.status, 409)
        # 只有一个会话
        async with self.store._lease() as conn:
            count = await self.store._one(
                conn, 'SELECT count(*) AS n FROM auth_sessions')
        self.assertEqual(int(count['n']), 1)

    async def test_concurrent_redemption_succeeds_once(self):
        """同一张票据并发兑换：数据库唯一约束裁决，只成功一次。"""
        one = ticket(jti='race')
        results = await asyncio.gather(
            *(auth.exchange(one, self.store) for _ in range(6)),
            return_exceptions=True)
        ok = [r for r in results if isinstance(r, dict)]
        conflicts = [r for r in results
                     if isinstance(r, auth.ExchangeError) and r.status == 409]
        self.assertEqual(len(ok), 1, [type(r).__name__ for r in results])
        self.assertEqual(len(conflicts), 5)

    async def test_invalid_ticket_creates_nothing(self):
        with self.assertRaises(auth.ExchangeError):
            await auth.exchange(ticket(private=PRIVATE_B), self.store)
        async with self.store._lease() as conn:
            sessions = await self.store._one(
                conn, 'SELECT count(*) AS n FROM auth_sessions')
            tickets = await self.store._one(
                conn, 'SELECT count(*) AS n FROM ticket_redemptions')
        self.assertEqual(int(sessions['n']), 0)
        self.assertEqual(int(tickets['n']), 0)

    async def test_failed_transaction_leaves_no_half_state(self):
        """绑定阶段出错 → 票据登记也回滚（否则用户会被永久锁在"已兑换"）。"""
        original = self.store.redeem_ticket

        async def boom(**kwargs):
            raise RuntimeError('模拟事务中途失败')

        with patch.object(self.store, 'redeem_ticket', boom):
            with self.assertRaises(auth.ExchangeError) as ctx:
                await auth.exchange(ticket(jti='half'), self.store)
        self.assertEqual(ctx.exception.status, 503)
        # 换掉实现后库里什么都没写；再用同一 jti 兑换应当还能成功
        self.store.redeem_ticket = original
        result = await auth.exchange(ticket(jti='half'), self.store)
        self.assertTrue(result['token'])

    async def test_no_plaintext_stored(self):
        one = ticket(jti='plain')
        result = await auth.exchange(one, self.store)
        async with self.store._lease() as conn:
            rows = await self.store._all(
                conn, 'SELECT * FROM auth_sessions')
            tickets = await self.store._all(
                conn, 'SELECT * FROM ticket_redemptions')
        blob = json.dumps([dict(r) for r in rows + tickets], default=str)
        self.assertNotIn(result['token'], blob)
        self.assertNotIn(one, blob)          # 票据原文也不落库
        self.assertIn(auth.digest_of(result['token']), blob)   # 只存摘要

    async def test_same_user_new_ticket_keeps_the_same_local_user(self):
        first = await auth.exchange(ticket(jti='a'), self.store)
        second = await auth.exchange(ticket(jti='b'), self.store)
        self.assertEqual(first['user_id'], second['user_id'])
        self.assertNotEqual(first['auth_session_id'], second['auth_session_id'])

    async def test_logout_revokes_session(self):
        result = await auth.exchange(ticket(), self.store)
        digest = auth.digest_of(result['token'])
        row = await self.store.auth_session(digest)
        self.assertTrue(await self.store.revoke_auth_session(row['auth_session_id']))
        again = await self.store.auth_session(digest)
        self.assertIsNotNone(again['revoked_at'])
        # 再撤一次是幂等的
        self.assertFalse(await self.store.revoke_auth_session(row['auth_session_id']))

    async def test_expired_session_is_detected(self):
        result = await auth.exchange(ticket(), self.store)
        digest = auth.digest_of(result['token'])
        async with self.store._lease() as conn:
            await self.store._run(
                conn, 'UPDATE auth_sessions SET expires_at=%s WHERE token_digest=%s',
                (time.time() - 10, digest))
        row = await self.store.auth_session(digest)
        self.assertLess(row['expires_at'], time.time())

    async def test_session_survives_a_new_store_instance(self):
        """API 重启 = 新的 store 实例：库里有效的会话必须继续可用。"""
        result = await auth.exchange(ticket(), self.store)
        other = new_store()
        try:
            row = await other.auth_session(auth.digest_of(result['token']))
            self.assertEqual(row['user_id'], result['user_id'])
        finally:
            await other.close()


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class ApiExchangeTests(unittest.IsolatedAsyncioTestCase):
    """HTTP 层：兑换 / me / 退出，以及"票据不能当业务凭证"。"""

    async def asyncSetUp(self):
        import app.api as api_module
        from app.api_hub import Hub

        self.store = new_store()
        await reset_db(self.store)
        self.api = api_module
        self.saved_hub = api_module.hub
        api_module.hub = Hub(self.store)
        # ASGITransport 不跑 lifespan，这里手动把仓储挂上去（lifespan 里也做同一件事）
        self.saved_state_store = getattr(api_module.app.state, 'store', None)
        api_module.app.state.store = self.store
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url='http://test')
        # settings 是 frozen dataclass：不能就地改字段，只能换一个实例。
        # 每个读取 settings 的模块都持有自己的引用，所以逐模块 patch。
        replaced = replace(api_module.settings, auth_mode='ticket',
                           auth_cookie_secure=False, policy_legacy_visible=False)
        self._patches = [
            patch.object(ticket_auth.auth_connections, 'connections',
                         lambda *a, **k: CONNECTIONS),
        ]
        from app import policies as policies_module
        for module in (api_module, auth, policies_module, ticket_auth):
            self._patches.append(patch.object(module, 'settings', replaced))
        for item in self._patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self._patches):
            item.stop()
        await self.client.aclose()
        self.api.hub = self.saved_hub
        self.api.app.state.store = self.saved_state_store
        await self.store.close()

    async def test_exchange_sets_cookie_and_me_returns_identity(self):
        res = await self.client.post('/api/auth/exchange',
                                     json={'ticket': ticket()})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body['tenant_id'], 'tenant-a')
        self.assertIn('agent_session', res.cookies)

        me = await self.client.get('/api/me')
        self.assertEqual(me.status_code, 200)
        data = me.json()
        self.assertEqual(data['user_id'], body['user_id'])
        self.assertEqual(data['platform_id'], 'pa')
        self.assertTrue(data['authenticated'])

    async def test_business_calls_require_a_session(self):
        res = await self.client.get('/api/sessions')
        self.assertEqual(res.status_code, 401)

    async def test_ticket_is_not_a_business_credential(self):
        """把票据当 cookie/头传进业务接口必须无效——票据只能兑换。"""
        one = ticket()
        res = await self.client.get('/api/sessions',
                                    headers={'Authorization': f'Bearer {one}'},
                                    cookies={'agent_session': one})
        self.assertEqual(res.status_code, 401)

    async def test_spoofed_headers_do_not_change_identity(self):
        created = (await self.client.post('/api/auth/exchange',
                                          json={'ticket': ticket()})).json()
        spoof = await self.client.get(
            '/api/me', headers={'X-DSH-User': 'u_attacker',
                                'X-DSH-Tenant': 'tenant-evil'})
        self.assertEqual(spoof.status_code, 200)
        self.assertEqual(spoof.json()['user_id'], created['user_id'])
        self.assertEqual(spoof.json()['tenant_id'], 'tenant-a')

    async def test_logout_clears_session(self):
        await self.client.post('/api/auth/exchange', json={'ticket': ticket()})
        self.assertEqual((await self.client.get('/api/me')).status_code, 200)
        out = await self.client.delete('/api/auth/session')
        self.assertTrue(out.json()['revoked'])
        self.assertEqual((await self.client.get('/api/me')).status_code, 401)

    async def test_repeat_exchange_conflicts(self):
        one = ticket(jti='dup')
        first = await self.client.post('/api/auth/exchange', json={'ticket': one})
        self.assertEqual(first.status_code, 200)
        second = await self.client.post('/api/auth/exchange', json={'ticket': one})
        self.assertEqual(second.status_code, 409)

    async def test_invalid_ticket_is_401(self):
        res = await self.client.post('/api/auth/exchange',
                                     json={'ticket': ticket(private=PRIVATE_B)})
        self.assertEqual(res.status_code, 401)
        self.assertIn('票据无效', res.json()['detail'])

    async def test_user_disabled_loses_access_immediately(self):
        created = (await self.client.post('/api/auth/exchange',
                                          json={'ticket': ticket()})).json()
        async with self.store._lease() as conn:
            await self.store._run(
                conn, "UPDATE app_users SET status='disabled' WHERE user_id=%s",
                (created['user_id'],))
        res = await self.client.get('/api/me')
        self.assertEqual(res.status_code, 401)
        self.assertIn('停用', res.json()['detail'])


if __name__ == '__main__':
    unittest.main()
