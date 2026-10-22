"""数据隔离：不同用户 / 企业 / 平台之间不能互相看见，也不能互相操作。

这是"登录正确但数据串号"的那一类 bug —— 登录流程本身全绿，业务数据却能看到
别人的。所以这里不看登录接口，只看**拿到合法会话之后能不能越界**。

覆盖验收表：
- B 用 A 的对象 ID 查询、追加消息、停止任务、下载产物、订阅事件 → 全部拒绝；
- 非本地属主返回 404（不区分"不存在"与"无权限"，避免枚举）；
- 两个平台各自的 `user-123` 是两个中台账号（身份源隔离）；
- 同一企业不同用户之间也互相隔离。
"""
import os
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import auth, policies, ticket_auth  # noqa: E402
from tests.test_auth_exchange import (PG_DSN, new_store, reset_db)  # noqa: E402
from tests.test_ticket_auth import (ISS, PRIVATE_A, PUBLIC_A,  # noqa: E402
                                    claims, connection, sign)

# 平台 A：企业 tenant-a / 身份源 idp-a（两个用户）
# 平台 B：企业 tenant-b / 身份源 idp-b（一个用户，sub 与 A 的用户同名）
CONNECTIONS = [
    connection(connection_id='conn-a', issuer=ISS, key=PUBLIC_A, kid='k1',
               tenants=('tenant-a',), platforms=('pa',), provider='idp-a'),
    connection(connection_id='conn-b', issuer=ISS, key=PUBLIC_A, kid='k1',
               tenants=('tenant-b',), platforms=('pb',), provider='idp-b'),
]


def ticket_for(sub, *, tenant='tenant-a', platform='pa', jti=None):
    stamp = int(time.time())
    return sign(claims(sub=sub, tenant_id=tenant, platform_id=platform,
                       iat=stamp, exp=stamp + 30,
                       jti=jti or f'jti-{sub}-{time.time_ns()}'))


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class IdentityIsolationTests(unittest.IsolatedAsyncioTestCase):
    """身份绑定层面的隔离（不经 HTTP）。"""

    async def asyncSetUp(self):
        self.store = new_store()
        await reset_db(self.store)
        self._patch = patch.object(ticket_auth.auth_connections, 'connections',
                                   lambda *a, **k: CONNECTIONS)
        self._patch.start()

    async def asyncTearDown(self):
        self._patch.stop()
        await self.store.close()

    async def test_same_sub_different_tenant_is_a_different_user(self):
        a = await auth.exchange(ticket_for('user-123', tenant='tenant-a',
                                           platform='pa'), self.store)
        b = await auth.exchange(ticket_for('user-123', tenant='tenant-b',
                                           platform='pb'), self.store)
        self.assertNotEqual(a['user_id'], b['user_id'],
                            '不同企业的同名 sub 不能合并成一个账号')
        self.assertEqual((a['tenant_id'], b['tenant_id']), ('tenant-a', 'tenant-b'))

    async def test_independent_identity_providers_do_not_merge(self):
        """身份源隔离：即使企业相同，不同身份源的 user-123 也是两个账号。"""
        connections = [
            connection(connection_id='conn-a1', issuer=ISS, key=PUBLIC_A,
                       tenants=('tenant-a',), platforms=('pa',), provider='idp-a1'),
            connection(connection_id='conn-a2', issuer=ISS, key=PUBLIC_A,
                       tenants=('tenant-a',), platforms=('pa2',), provider='idp-a2'),
        ]
        with patch.object(ticket_auth.auth_connections, 'connections',
                          lambda *a, **k: connections):
            one = await auth.exchange(
                ticket_for('user-123', tenant='tenant-a', platform='pa'),
                self.store)
            two = await auth.exchange(
                ticket_for('user-123', tenant='tenant-a', platform='pa2'),
                self.store)
        self.assertNotEqual(one['user_id'], two['user_id'])

    async def test_same_identity_twice_returns_same_user(self):
        first = await auth.exchange(ticket_for('user-1'), self.store)
        second = await auth.exchange(ticket_for('user-1'), self.store)
        self.assertEqual(first['user_id'], second['user_id'])
        async with self.store._lease() as conn:
            count = await self.store._one(
                conn, 'SELECT count(*) AS n FROM identities')
        self.assertEqual(int(count['n']), 1)

    async def test_different_users_are_different_accounts(self):
        one = await auth.exchange(ticket_for('user-1'), self.store)
        two = await auth.exchange(ticket_for('user-2'), self.store)
        self.assertNotEqual(one['user_id'], two['user_id'])

    async def test_concurrent_first_login_creates_one_binding(self):
        import asyncio

        results = await asyncio.gather(
            *(auth.exchange(ticket_for('racer'), self.store) for _ in range(5)))
        self.assertEqual({r['user_id'] for r in results}.__len__(), 1)
        async with self.store._lease() as conn:
            count = await self.store._one(
                conn, "SELECT count(*) AS n FROM identities WHERE subject='racer'")
        self.assertEqual(int(count['n']), 1)


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class ApiIsolationTests(unittest.IsolatedAsyncioTestCase):
    """HTTP 层隔离：A 建的一切，B 都拿不到。"""

    async def asyncSetUp(self):
        import app.api as api_module
        from app.api_hub import Hub

        self.store = new_store()
        await reset_db(self.store)
        self.api = api_module
        self.saved_hub = api_module.hub
        api_module.hub = Hub(self.store)
        self.saved_state_store = getattr(api_module.app.state, 'store', None)
        api_module.app.state.store = self.store

        replaced = replace(api_module.settings, auth_mode='ticket',
                           auth_cookie_secure=False, policy_legacy_visible=False)
        self._patches = [patch.object(ticket_auth.auth_connections, 'connections',
                                      lambda *a, **k: CONNECTIONS)]
        for module in (api_module, auth, policies, ticket_auth):
            self._patches.append(patch.object(module, 'settings', replaced))
        for item in self._patches:
            item.start()

        self.alice = await self._login(ticket_for('alice'))
        self.bob = await self._login(ticket_for('bob'))

    async def asyncTearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self.api.hub = self.saved_hub
        self.api.app.state.store = self.saved_state_store
        await self.store.close()

    async def _login(self, ticket):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.api.app),
            base_url='http://test')
        res = await client.post('/api/auth/exchange', json={'ticket': ticket})
        self.assertEqual(res.status_code, 200, res.text)
        return {'client': client, 'me': res.json()}

    async def _close(self, session):
        await session['client'].aclose()

    async def test_two_users_see_only_their_own_sessions(self):
        try:
            created = await self.alice['client'].post(
                '/api/tasks', json={'message': '你好，我是 alice'})
            self.assertEqual(created.status_code, 200, created.text)
            session_id = created.json()['session_id']

            mine = await self.alice['client'].get('/api/sessions')
            titles = [s['session_id'] for s in mine.json()['sessions']]
            self.assertIn(session_id, titles)

            others = await self.bob['client'].get('/api/sessions')
            self.assertNotIn(session_id,
                             [s['session_id'] for s in others.json()['sessions']])
        finally:
            await self._close(self.alice)
            await self._close(self.bob)

    async def test_bob_cannot_touch_alices_objects(self):
        """按对象 ID 越权：读任务/会话/停止/产物/事件流，全部 404 或 401。"""
        try:
            created = await self.alice['client'].post(
                '/api/tasks', json={'message': '你好'})
            task_id = created.json()['task_id']
            session_id = created.json()['session_id']
            # 等任务落定，产物接口才可能 200/404（而不是 409）
            for _ in range(100):
                record = await self.api.hub.get(task_id)
                if record is not None and record.done:
                    break
                await __import__('asyncio').sleep(0.05)

            checks = [
                ('get', f'/api/tasks/{task_id}'),
                ('get', f'/api/sessions/{session_id}/messages'),
                ('get', f'/api/sessions/{session_id}/timeline'),
                ('get', f'/api/sessions/{session_id}/memory'),
                ('get', f'/api/tasks/{task_id}/artifact'),
                ('post', f'/api/tasks/{task_id}/cancel'),
            ]
            for method, path in checks:
                res = await getattr(self.bob['client'], method)(path)
                with self.subTest(path=path):
                    self.assertIn(res.status_code, (401, 403, 404), path)

            # 追加进别人的会话也不行
            res = await self.bob['client'].post(
                '/api/tasks', json={'message': '混进去', 'session_id': session_id})
            self.assertEqual(res.status_code, 404)

            # 事件流：不是自己的任务就不能订阅
            res = await self.bob['client'].get(f'/api/tasks/{task_id}/events')
            self.assertEqual(res.status_code, 404)
        finally:
            await self._close(self.alice)
            await self._close(self.bob)

    async def test_profile_is_per_user(self):
        try:
            await self.alice['client'].put('/api/profile',
                                           json={'text': '姓名：爱丽丝\n爱好：天文\n'})
            mine = (await self.alice['client'].get('/api/profile')).json()
            theirs = (await self.bob['client'].get('/api/profile')).json()
            self.assertEqual(mine['facts'].get('姓名'), '爱丽丝')
            self.assertNotIn('爱丽丝', str(theirs.get('facts')))
        finally:
            await self._close(self.alice)
            await self._close(self.bob)

    async def test_cross_tenant_user_is_not_the_same_account(self):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.api.app), base_url='http://test')
        try:
            res = await client.post('/api/auth/exchange',
                                    json={'ticket': ticket_for('alice',
                                                              tenant='tenant-b',
                                                              platform='pb')})
            self.assertEqual(res.status_code, 200, res.text)
            self.assertNotEqual(res.json()['user_id'], self.alice['me']['user_id'])
            self.assertEqual(res.json()['tenant_id'], 'tenant-b')
        finally:
            await client.aclose()
            await self._close(self.alice)
            await self._close(self.bob)


if __name__ == '__main__':
    unittest.main()
