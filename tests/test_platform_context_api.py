"""A host-agnostic scene handshake must not start tasks or trust caller identity."""
import unittest
from dataclasses import replace
from unittest.mock import patch, AsyncMock

import httpx

from app import auth_connections, policies, scene_context
from tests.test_scene_context import CONTEXT, _sign
from tests.test_ticket_auth import connection


class PlatformContextApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Import constructs a lazy PG pool; no startup/lifespan or database I/O.
        from app.config import settings
        with patch('app.config.settings', replace(
                settings, database_url='postgresql://unused:unused@127.0.0.1/unused_test')):
            import app.api as api
        self.api = api
        self.principal = policies.Principal(
            tenant_id='tenant-a', user_id='internal-user', subject='user-123',
            platform_id='pa', connection_id='conn-a', authenticated=True)
        self.api.app.dependency_overrides[policies.current_principal] = lambda: self.principal
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url='http://test')
        self.connections = patch.object(auth_connections, 'connections', return_value=[connection()])
        self.connections.start()

    async def asyncTearDown(self):
        self.connections.stop()
        self.api.app.dependency_overrides.clear()
        await self.client.aclose()

    async def test_verified_scene_is_returned_without_execution(self):
        with patch.object(self.api.hub, 'start') as start:
            response = await self.client.post('/api/context/verify', json={
                'context': CONTEXT, 'context_assertion': _sign(CONTEXT)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['verified'])
        self.assertEqual(response.json()['context'], CONTEXT)
        start.assert_not_called()

    async def test_tampering_and_missing_signature_are_rejected(self):
        for payload in [
            {'context': CONTEXT},
            {'context': {'project_id': 'foreign'}, 'context_assertion': _sign(CONTEXT)},
        ]:
            response = await self.client.post('/api/context/verify', json=payload)
            self.assertEqual(response.status_code, 400, response.text)

    async def test_scene_must_match_the_current_login(self):
        for changes in [dict(subject='other'), dict(tenant_id='other'),
                        dict(platform_id='other'), dict(connection_id='other')]:
            self.principal = replace(self.principal, **changes)
            response = await self.client.post('/api/context/verify', json={
                'context': CONTEXT, 'context_assertion': _sign(CONTEXT)})
            self.assertEqual(response.status_code, 400, response.text)
            self.principal = replace(self.principal, tenant_id='tenant-a',
                                     subject='user-123', platform_id='pa', connection_id='conn-a')

    async def test_anonymous_preview_is_rejected(self):
        self.principal = replace(self.principal, authenticated=False)
        response = await self.client.post('/api/context/verify', json={
            'context': CONTEXT, 'context_assertion': _sign(CONTEXT)})
        self.assertEqual(response.status_code, 401)


class PlatformSceneBudgetTests(unittest.TestCase):
    def test_context_budget_is_configurable_and_marks_truncation(self):
        from app import nodes
        scene = {'verified': True, 'context': {'document': 'x' * 2000}}
        with patch.object(nodes, 'settings', replace(nodes.settings, platform_context_max_chars=3000)):
            self.assertNotIn('已截断', nodes._scene_brief(scene))
        with patch.object(nodes, 'settings', replace(nodes.settings, platform_context_max_chars=800)):
            self.assertIn('已截断', nodes._scene_brief(scene))


class PlatformSceneAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_receives_the_same_host_scene_as_planner(self):
        from app.capabilities import answers
        chat = AsyncMock(return_value='已读取现场')
        with patch.object(answers, 'chat_reply', chat):
            result = await answers._answer_directly({'question': '当前页面是什么'}, {
                'scene': {'verified': True, 'context': {'document': 'HOST-CONTEXT-CHECK'}},
            })
        self.assertIn('HOST-CONTEXT-CHECK', str(chat.call_args.args))
        self.assertEqual(result['answer'], '已读取现场')

    async def test_missing_external_credentials_preserve_policy_filtered_local_tools(self):
        from app import tools, credentials
        principal = object()
        local = list(tools.registry.registered())
        filtered = AsyncMock(return_value=local[:1])
        with patch.object(tools, 'settings', replace(tools.settings, tool_policy_enforced=True)), \
                patch.object(tools, 'mcp_tools', AsyncMock(side_effect=credentials.CredentialMissing('unbound'))), \
                patch.object(tools, 'visible_tools', filtered):
            result = await tools.catalog(principal)
        self.assertEqual(result, local[:1])
        filtered.assert_awaited_once_with(local, principal)
