import asyncio
import os
import sqlite3
import uuid
import time
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import httpx

from webpage_agent.api import auth, routes
from webpage_agent.store.pg_auth import PgAuth


class QueryStore(PgAuth):
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE app_users (user_id TEXT, display_name TEXT, email TEXT, status TEXT);
            CREATE TABLE identities (user_id TEXT, tenant_id TEXT);
            INSERT INTO app_users VALUES ('a', 'Alice', 'a@example.test', 'active');
            INSERT INTO app_users VALUES ('b', 'Bob', 'b@example.test', 'disabled');
            INSERT INTO app_users VALUES ('c', 'Carol', 'c@example.test', 'active');
            INSERT INTO identities VALUES ('a', 'one'), ('a', 'one'),
                ('b', 'two'), ('c', 'one'), ('c', 'two');
        ''')

    @asynccontextmanager
    async def _lease(self):
        yield self.db

    async def _all(self, conn, sql, args):
        return conn.execute(sql.replace('%s', '?'), args).fetchall()


class AdminUsersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = QueryStore()
        self.store.auth_session = AsyncMock(return_value={
            'tenant_id': 'one', 'user_id': 'a', 'role': 'operator',
            'auth_session_id': 'session', 'expires_at': time.time() + 300})
        self.store.user = AsyncMock(return_value={'status': 'active'})
        self.settings = replace(auth.settings, auth_mode='ticket', api_rate_limit_enabled=False)
        self.patches = [patch.object(auth, 'settings', self.settings),
                        patch.object(routes.hub, 'store', self.store),
                        patch.object(routes.app.state, 'store', self.store, create=True)]
        for p in self.patches:
            p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=routes.app),
                                        base_url='http://test',
                                        cookies={auth.settings.auth_session_cookie: 'token'})

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        self.store.db.close()

    async def test_tenant_list_deduplicates_and_ignores_client_tenant(self):
        response = await self.client.get('/api/admin/users?tenant_id=two')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json(), {'total': 2, 'users': [
            {'user_id': 'a', 'display_name': 'Alice', 'email': 'a@example.test', 'status': 'active'},
            {'user_id': 'c', 'display_name': 'Carol', 'email': 'c@example.test', 'status': 'active'}]})
        self.store.auth_session.return_value['tenant_id'] = 'two'
        response = await self.client.get('/api/admin/users')
        self.assertEqual([u['user_id'] for u in response.json()['users']], ['b', 'c'])
        self.assertEqual(response.json()['total'], 2)

    async def test_requires_active_session_and_explicit_role(self):
        self.store.auth_session.return_value['role'] = 'member'
        with patch.dict('os.environ', {'AUTH_OPERATORS': 'a'}):
            self.assertEqual((await self.client.get('/api/admin/users')).status_code, 403)
        self.store.auth_session.return_value['role'] = 'operator'
        self.store.auth_session.return_value['revoked_at'] = 'now'
        self.assertEqual((await self.client.get('/api/admin/users')).status_code, 401)
        self.store.auth_session.return_value['revoked_at'] = None
        self.store.auth_session.return_value['expires_at'] = time.time() - 1
        self.assertEqual((await self.client.get('/api/admin/users')).status_code, 401)
        self.store.auth_session.return_value['expires_at'] = time.time() + 300
        self.store.user.return_value = {'status': 'disabled'}
        self.store.revoke_user_sessions = AsyncMock()
        self.assertEqual((await self.client.get('/api/admin/users')).status_code, 401)
        self.store.revoke_user_sessions.assert_not_awaited()

    async def test_off_and_optional_anonymous_are_rejected(self):
        for mode in ('off', 'optional'):
            with self.subTest(mode=mode), patch.object(auth, 'settings', replace(self.settings, auth_mode=mode)):
                self.store.auth_session.return_value = None
                response = await self.client.get('/api/admin/users', headers={'x-dsh-user': 'default'})
                self.assertEqual(response.status_code, 401)

    async def test_status_mutation_requires_allowed_origin(self):
        self.store.set_tenant_user_status = AsyncMock(return_value={'user_id': 'b', 'status': 'disabled'})
        with patch.object(routes, 'settings', replace(routes.settings, auth_allowed_origins=('http://test',))):
            self.assertEqual((await self.client.patch('/api/admin/users/b/status',
                json={'status': 'disabled'})).status_code, 403)
            response = await self.client.patch('/api/admin/users/b/status', json={'status': 'disabled'},
                                               headers={'Origin': 'http://test'})
        self.assertEqual(response.status_code, 200)
        self.store.set_tenant_user_status.assert_awaited_once()


@unittest.skipUnless(os.getenv('PG_TEST_DSN'), '需要独立 PostgreSQL 测试库')
class AdminUsersPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.test_auth_exchange import new_store, reset_db
        self.store = new_store()
        await reset_db(self.store)
        self.admin = await self.login('admin')
        self.other = await self.login('other')
        self.member = await self.login('member')
        async with self.store._lease() as conn:
            await conn.execute("UPDATE identities SET role='operator' WHERE user_id IN (%s,%s)",
                               (self.admin['user_id'], self.other['user_id']))
        settings = replace(auth.settings, auth_mode='ticket', api_rate_limit_enabled=False,
                           auth_allowed_origins=('http://test',))
        self.patches = [patch.object(auth, 'settings', settings),
                        patch.object(routes, 'settings', settings),
                        patch.object(routes.hub, 'store', self.store),
                        patch.object(routes.app.state, 'store', self.store, create=True)]
        for p in self.patches:
            p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=routes.app),
                                        base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        await self.store.close()

    async def login(self, subject):
        token = uuid.uuid4().hex
        row = await self.store.redeem_ticket(issuer='test', jti=token,
            expires_at=int(time.time()) + 300, connection_id='conn', platform_id='platform',
            tenant_id='one', identity_provider_id='idp', subject=subject,
            session_id='as_' + token, session_digest=auth.digest_of(token),
            session_expires_at=time.time() + 300)
        row['token'] = token
        return row

    async def change(self, actor, target, status='disabled', origin='http://test'):
        return await self.client.patch('/api/admin/users/' + target['user_id'] + '/status',
            json={'status': status}, headers={'Origin': origin} if origin else {},
            cookies={auth.settings.auth_session_cookie: actor['token']})

    async def test_authorization_origin_and_sessions(self):
        self.assertEqual((await self.change(self.member, self.other)).status_code, 403)
        self.assertEqual((await self.change(self.admin, self.member, origin='')).status_code, 403)
        self.assertEqual((await self.change(self.admin, self.member, origin='http://evil')).status_code, 403)
        self.assertEqual((await self.change(self.admin, self.admin)).status_code, 409)
        extra = await self.login('member')
        self.assertEqual((await self.change(self.admin, self.member)).status_code, 200)
        self.assertTrue((await self.store.auth_session(auth.digest_of(extra['token'])))['revoked_at'])
        with self.assertRaises(PermissionError):
            await self.login('member')
        self.assertEqual((await self.change(self.admin, self.member, 'active')).status_code, 200)
        self.assertEqual((await self.login('member'))['user_id'], self.member['user_id'])
        async with self.store._lease() as conn:
            audit = await self.store._one(conn, "SELECT count(*) AS n FROM events WHERE payload LIKE %s",
                                          ('%admin_user_status%',))
        self.assertEqual(audit['n'], 2)

    async def test_cross_tenant_and_competing_operators(self):
        async with self.store._lease() as conn:
            await conn.execute("INSERT INTO identities (provider, subject, user_id, tenant_id, "
                "identity_provider_id, created_at, last_seen_at) VALUES ('other-idp','x',%s,'two',"
                "'other-idp','now','now')", (self.member['user_id'],))
        self.assertEqual((await self.change(self.admin, self.member)).status_code, 409)
        results = await asyncio.gather(self.change(self.admin, self.other),
                                       self.change(self.other, self.admin))
        self.assertEqual(sum(r.status_code == 200 for r in results), 1)
        users = [await self.store.user(x['user_id']) for x in (self.admin, self.other)]
        self.assertEqual(sum(u['status'] == 'active' for u in users), 1)

    async def test_audit_failure_rolls_back_status_and_revocation(self):
        original = self.store._run

        async def fail_audit(conn, sql, params=()):
            if sql.startswith('INSERT INTO events'):
                raise RuntimeError('audit unavailable')
            return await original(conn, sql, params)

        with patch.object(self.store, '_run', side_effect=fail_audit):
            with self.assertRaises(RuntimeError):
                await self.store.set_tenant_user_status(tenant_id='one',
                    user_id=self.member['user_id'], operator_id=self.admin['user_id'],
                    session_id=self.admin['auth_session_id'], status='disabled')
        self.assertEqual((await self.store.user(self.member['user_id']))['status'], 'active')
        self.assertIsNone((await self.store.auth_session(auth.digest_of(self.member['token'])))['revoked_at'])

    async def test_bind_and_login_share_identity_under_concurrency(self):
        bound, logged = await asyncio.gather(
            self.store.bind_identity('idp', 'new-subject', tenant_id='one', identity_provider_id='idp'),
            self.login('new-subject'))
        self.assertEqual(bound['user_id'], logged['user_id'])

    async def test_disabled_lookup_does_not_revoke_new_session(self):
        await self.change(self.admin, self.member)
        observed = asyncio.Event()
        resume = asyncio.Event()
        original = self.store.user

        async def delayed_user(user_id):
            result = await original(user_id)
            if user_id == self.member['user_id']:
                observed.set()
                await resume.wait()
            return result

        with patch.object(self.store, 'user', side_effect=delayed_user):
            # 模拟停用前已读到旧会话、停用后才读到用户状态的请求。
            from starlette.requests import Request
            request = Request({'type': 'http', 'headers': [(b'cookie',
                (auth.settings.auth_session_cookie + '=' + self.member['token']).encode())]})
            session = await self.store.auth_session(auth.digest_of(self.member['token']))
            session['revoked_at'] = None
            with patch.object(self.store, 'auth_session', AsyncMock(return_value=session)):
                pending = asyncio.create_task(auth.principal_from_session(request, self.store))
                await asyncio.wait_for(observed.wait(), 5)
                await self.store.set_tenant_user_status(tenant_id='one', user_id=self.member['user_id'],
                    operator_id=self.admin['user_id'], session_id=self.admin['auth_session_id'], status='active')
                new_session = await self.login('member')
                resume.set()
                with self.assertRaises(Exception) as error:
                    await asyncio.wait_for(pending, 5)
                self.assertEqual(error.exception.status_code, 401)
            self.assertIsNone((await self.store.auth_session(auth.digest_of(new_session['token'])))['revoked_at'])

    async def test_operator_locks_serialize_logout_role_and_status_changes(self):
        statements = [
            ("UPDATE auth_sessions SET revoked_at='logout' WHERE auth_session_id=%s", self.admin['auth_session_id']),
            ("UPDATE identities SET role='member' WHERE user_id=%s", self.admin['user_id']),
            ("UPDATE app_users SET status='disabled' WHERE user_id=%s", self.admin['user_id']),
        ]
        for sql, value in statements:
            with self.subTest(sql=sql):
                verified = asyncio.Event()
                resume = asyncio.Event()
                started = asyncio.Event()
                original = self.store._one

                async def pause_after_check(conn, query, params=()):
                    result = await original(conn, query, params)
                    if query.startswith('SELECT 1 FROM auth_sessions s JOIN app_users'):
                        verified.set()
                        await resume.wait()
                    return result

                async def mutate():
                    async with self.store._lease() as conn:
                        started.set()
                        await conn.execute(sql, (value,))

                with patch.object(self.store, '_one', side_effect=pause_after_check):
                    change = asyncio.create_task(self.store.set_tenant_user_status(
                        tenant_id='one', user_id=self.member['user_id'],
                        operator_id=self.admin['user_id'], session_id=self.admin['auth_session_id'],
                        status='active'))
                    await asyncio.wait_for(verified.wait(), 5)
                    mutation = asyncio.create_task(mutate())
                    await asyncio.wait_for(started.wait(), 5)
                    try:
                        with self.assertRaises(asyncio.TimeoutError):
                            await asyncio.wait_for(asyncio.shield(mutation), 0.2)
                    finally:
                        resume.set()
                    await asyncio.wait_for(asyncio.gather(change, mutation), 5)
                async with self.store._lease() as conn:
                    await conn.execute("UPDATE auth_sessions SET revoked_at=NULL WHERE auth_session_id=%s", (self.admin['auth_session_id'],))
                    await conn.execute("UPDATE identities SET role='operator' WHERE user_id=%s", (self.admin['user_id'],))
                    await conn.execute("UPDATE app_users SET status='active' WHERE user_id=%s", (self.admin['user_id'],))

    async def test_prior_operator_changes_reject_status_mutation(self):
        statements = [
            ("UPDATE auth_sessions SET revoked_at='logout' WHERE auth_session_id=%s", self.admin['auth_session_id']),
            ("UPDATE identities SET role='member' WHERE user_id=%s", self.admin['user_id']),
            ("UPDATE app_users SET status='disabled' WHERE user_id=%s", self.admin['user_id']),
        ]
        for sql, value in statements:
            with self.subTest(sql=sql):
                async with self.store._lease() as conn:
                    await conn.execute(sql, (value,))
                with self.assertRaises(PermissionError):
                    await self.store.set_tenant_user_status(
                        tenant_id='one', user_id=self.member['user_id'],
                        operator_id=self.admin['user_id'], session_id=self.admin['auth_session_id'],
                        status='disabled')
                self.assertEqual((await self.store.user(self.member['user_id']))['status'], 'active')
                async with self.store._lease() as conn:
                    audit = await self.store._one(conn, "SELECT count(*) AS n FROM events WHERE payload LIKE %s",
                                                  ('%admin_user_status%',))
                    self.assertEqual(audit['n'], 0)
                    await conn.execute("UPDATE auth_sessions SET revoked_at=NULL WHERE auth_session_id=%s", (self.admin['auth_session_id'],))
                    await conn.execute("UPDATE identities SET role='operator' WHERE user_id=%s", (self.admin['user_id'],))
                    await conn.execute("UPDATE app_users SET status='active' WHERE user_id=%s", (self.admin['user_id'],))

    async def test_login_racing_disable(self):
        results = await asyncio.gather(self.change(self.admin, self.member), self.login('member'),
                                       return_exceptions=True)
        self.assertEqual(results[0].status_code, 200)
        async with self.store._lease() as conn:
            row = await self.store._one(conn, 'SELECT count(*) AS n FROM auth_sessions '
                'WHERE user_id=%s AND revoked_at IS NULL', (self.member['user_id'],))
        self.assertEqual(row['n'], 0)
