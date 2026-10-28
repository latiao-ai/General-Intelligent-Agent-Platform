"""客户接入评估中发现的撤权、运维入口和日志泄密回归。"""
import io
import os
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import auth, policies, tool_authorization, tool_policy


class PolicyRevocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.default = tool_policy.ToolPolicy('service', 'tenant', tool_key='mcp:s:*')
        self.disabled = tool_policy.ToolPolicy(
            'specific', 'tenant', tool_key='mcp:s:delete', enabled=False)

    def test_disabled_exact_policy_blocks_service_fallback(self):
        for rows in ([self.default, self.disabled], [self.disabled, self.default]):
            with self.subTest(rows=[p.policy_id for p in rows]):
                decision = tool_policy.decide(
                    tenant_id='tenant', tool_key='mcp:s:delete', policies=rows,
                    require_credential=False, args={})
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.code, 'E_POLICY_DISABLED')
                self.assertIs(decision.policy, self.disabled)

    def test_disabled_policy_does_not_hide_other_tools_or_users(self):
        scoped = replace(self.disabled, user_id='alice')
        for tool, user, allowed in [('mcp:s:delete', 'alice', False),
                                    ('mcp:s:delete', 'bob', True),
                                    ('mcp:s:read', 'alice', True),
                                    ('mcp:other:read', 'alice', False)]:
            with self.subTest(tool=tool, user=user):
                result = tool_policy.decide(
                    tenant_id='tenant', tool_key=tool, user_id=user,
                    policies=[self.default, scoped], require_credential=False)
                self.assertEqual(result.allowed, allowed)

    def test_disabled_service_blocks_tenant_fallback_but_exact_override_works(self):
        rows = [tool_policy.ToolPolicy('tenant', 'tenant'),
                replace(self.default, enabled=False)]
        self.assertFalse(tool_policy.decide(
            tenant_id='tenant', tool_key='mcp:s:read', policies=rows,
            require_credential=False).allowed)
        rows.append(tool_policy.ToolPolicy('read', 'tenant', tool_key='mcp:s:read'))
        self.assertTrue(tool_policy.decide(
            tenant_id='tenant', tool_key='mcp:s:read', policies=rows,
            require_credential=False).allowed)

    def test_disabled_and_forbidden_tools_are_hidden_but_approval_tools_remain(self):
        tools = [SimpleNamespace(tool_key='mcp:s:' + name)
                 for name in ('delete', 'forbidden', 'approve', 'read')]
        rows = [self.default, self.disabled,
                tool_policy.ToolPolicy('never', 'tenant', tool_key='mcp:s:forbidden',
                                       approval_mode='never'),
                tool_policy.ToolPolicy('approval', 'tenant', tool_key='mcp:s:approve',
                                       approval_mode='required')]
        visible, hidden = tool_policy.filter_catalog(tools, rows, tenant_id='tenant')
        self.assertEqual([t.tool_key for t in visible], ['mcp:s:approve', 'mcp:s:read'])
        self.assertEqual(hidden, ['mcp:s:delete', 'mcp:s:forbidden'])

    async def test_disabled_policy_blocks_authorization_and_pre_send_recheck(self):
        store = SimpleNamespace(tool_policies=AsyncMock(return_value=[
            vars(self.default), vars(self.disabled)]))
        context = SimpleNamespace(tenant_id='tenant', user_id='alice', role='member',
                                  platform_id='platform')
        with patch.object(tool_policy, 'enforcement_enabled', return_value=True), \
                patch('app.mcp_connections.binding_for_server', AsyncMock(return_value=None)):
            with self.assertRaises(tool_authorization.AuthorizationDenied) as raised:
                await tool_authorization.authorize_call(
                    store, context, tool_key='mcp:s:delete', args={})
            self.assertEqual(raised.exception.error_code, 'E_POLICY_DISABLED')
            with self.assertRaises(tool_authorization.AuthorizationDenied) as raised:
                await tool_authorization.check_revocations(
                    store, tool_policy.PolicyDecision(allowed=True, policy=self.default),
                    tool_key='mcp:s:delete', tenant_id='tenant', user_id='alice')
            self.assertEqual(raised.exception.error_code, 'E_POLICY_REVOKED')


class OperationsEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import api
        self.api = api
        self.old_store = getattr(api.app.state, 'store', None)
        self.store = SimpleNamespace(stats=AsyncMock(return_value={'runs_queued': 2}),
                                     auth_session=AsyncMock(return_value=None))
        api.app.state.store = self.store
        self.hub_patch = patch.object(api, 'hub', SimpleNamespace(
            store=self.store, owner_id='test-worker', _inflight=lambda: 0))
        self.hub_patch.start()
        self.env_patch = patch.dict(os.environ, {'AUTH_OPERATORS': 'designated-operator'})
        self.env_patch.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url='http://test')

    async def asyncTearDown(self):
        self.api.app.dependency_overrides.clear()
        self.api.app.state.store = self.old_store
        self.env_patch.stop()
        self.hub_patch.stop()
        await self.client.aclose()

    async def test_ticket_mode_rejects_anonymous_detailed_operations(self):
        with patch.object(auth, 'settings', replace(auth.settings, auth_mode='ticket')):
            for url in ('/api/health', '/api/recovery'):
                response = await self.client.get(url)
                self.assertEqual(response.status_code, 401, response.text)
        self.store.stats.assert_not_awaited()

    async def test_member_cannot_read_detailed_operations(self):
        self.api.app.dependency_overrides[policies.current_principal] = lambda: policies.Principal(
            user_id='member', authenticated=True)
        for url in ('/api/health', '/api/recovery'):
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 403, response.text)
        self.store.stats.assert_not_awaited()

    async def test_role_and_configured_operators_can_read_operations(self):
        for principal in (policies.Principal(user_id='ops', role='operator', authenticated=True),
                          policies.Principal(user_id='designated-operator', authenticated=True)):
            self.api.app.dependency_overrides[policies.current_principal] = lambda: principal
            health = await self.client.get('/api/health')
            self.assertEqual(health.status_code, 200, health.text)
            self.assertEqual(health.json()['runs_queued'], 2)
            self.assertEqual((await self.client.get('/api/recovery')).status_code, 200)

    async def test_minimal_probes_remain_public_in_ticket_mode(self):
        with patch.object(auth, 'settings', replace(auth.settings, auth_mode='ticket')):
            for url in ('/api/health/live', '/api/health/ready'):
                response = await self.client.get(url)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {'ok': True})
        self.store.auth_session.assert_not_awaited()


class WorkerLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_start_does_not_log_database_credentials(self):
        from scripts import run_worker as worker
        secret_dsn = 'postgresql://test-user:test-password@localhost/test?password=second-secret'
        hub = SimpleNamespace(store=SimpleNamespace(ready=AsyncMock()),
                              reconcile=AsyncMock(return_value={}))
        output = io.StringIO()
        with patch.object(worker, 'settings', replace(
                worker.settings, database_url=secret_dsn, async_tool_runtime_enabled=False)), \
                patch.object(worker, 'drain_jobs', AsyncMock(return_value=0)), \
                redirect_stdout(output), self.assertLogs('worker', level='INFO') as logs:
            await worker.main_loop(hub, 'review-worker', once=True, jobs_only=True, interval=1)
        text = output.getvalue() + '\n'.join(logs.output)
        self.assertIn('review-worker', text)
        for private in (secret_dsn, 'test-user', 'test-password', 'second-secret'):
            self.assertNotIn(private, text)
