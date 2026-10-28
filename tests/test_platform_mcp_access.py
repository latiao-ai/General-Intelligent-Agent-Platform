"""Platform-wide access without cross-platform discovery or credential reuse."""
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from app import credentials, execution_control, ledger, mcp_connections, tools
from app import tool_authorization, tool_context, tool_policy
from app.integrations.mcp_servers import Server
from tests.test_mcp_connections import _Store, _server, _cred


class PlatformMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        credentials.invalidate_cache()
        a = _cred('ca', 'customer', 'tenant', 'customer', 'sa', 'literal:')
        b = _cred('cb', 'customer', 'tenant', 'customer', 'sb', 'literal:')
        a['scope'] = {'platforms': ['a']}
        b['scope'] = {'platforms': ['b']}
        self.store = _Store([_server('sa', 'customer', 'a'),
                             _server('sb', 'customer', 'b')], [a, b])
        self.store.run = AsyncMock(return_value={
            'tenant_id': 'customer', 'owner_user_id': 'new-customer', 'platform_id': 'a'})
        self.store.tool_policies = AsyncMock(return_value=[
            {'policy_id': 'pa', 'tenant_id': 'customer', 'tool_key': 'mcp:a:*'},
            {'policy_id': 'pb', 'tenant_id': 'customer', 'tool_key': 'mcp:b:*'}])

    async def test_all_customers_share_platform_access_but_not_another_platform(self):
        for user in ('first-customer', 'new-customer'):
            self.assertEqual(await mcp_connections.bearer_for(
                self.store, tenant_id='customer', user_id=user,
                platform_id='a', server_id='a'), '')
            with patch('app.tool_policy.enforcement_enabled', return_value=True):
                with self.assertRaises(credentials.CredentialMissing):
                    await mcp_connections.bearer_for(
                        self.store, tenant_id='customer', user_id=user,
                        platform_id='a', server_id='b')

    async def test_discovery_never_contacts_foreign_server_and_preserves_names(self):
        context = execution_control.ExecutionContext('run-a', lease_epoch=1)
        book = ledger.ActionLedger(self.store, 'run-a', 1, context=context)
        servers = [Server('a', url='http://a.invalid'), Server('b', url='http://b.invalid')]
        discover = AsyncMock(return_value=([{'name': 'draw', 'inputSchema': {'type': 'object'}}], ''))
        with ledger.use_ledger(book), execution_control.use(context), \
                patch.object(tools, 'settings', replace(tools.settings, tool_policy_enforced=True)), \
                patch.object(tools.mcp_servers, 'servers', return_value=servers), \
                patch.object(tools, '_discover', discover), \
                patch.object(tools.registry, 'mcp_placeholder', return_value=None), \
                patch.object(tools, '_discover_capabilities', AsyncMock(return_value={})):
            found = await tools.mcp_tools(force=True)
        self.assertEqual([call.args[0].id for call in discover.await_args_list], ['a'])
        self.assertEqual([t.name for t in found], ['mcp__a__draw'])
        self.assertEqual(found[0].tool_key, 'mcp:a:draw')

    async def test_execution_denies_foreign_platform_and_local_tools(self):
        context = tool_context.ToolCallContext(
            tenant_id='customer', user_id='new-customer', platform_id='a')
        with patch('app.tool_policy.enforcement_enabled', return_value=True):
            decision = await tool_authorization.authorize_call(
                self.store, context, tool_key='mcp:a:draw')
            self.assertTrue(decision.allowed)
            for key in ('mcp:b:draw', 'local:web:search'):
                with self.assertRaises(tool_authorization.AuthorizationDenied):
                    await tool_authorization.authorize_call(self.store, context, tool_key=key)
            self.store.rows[0]['scope'] = {'platforms': ['b']}
            with self.assertRaises(tool_authorization.AuthorizationDenied):
                await tool_authorization.check_revocations(
                    self.store, decision, tool_key='mcp:a:draw',
                    tenant_id='customer', user_id=context.user_id, platform_id='a')

    def test_exact_policy_overrides_server_default_without_matching_other_servers(self):
        default = tool_policy.ToolPolicy('p', 'customer', tool_key='mcp:a:*')
        exact = tool_policy.ToolPolicy('deny', 'customer', tool_key='mcp:a:delete',
                                       approval_mode='never')
        self.assertIs(tool_policy.pick_policy([default, exact], tenant_id='customer',
                                              tool_key='mcp:a:delete'), exact)
        self.assertIsNone(tool_policy.pick_policy([default], tenant_id='customer',
                                                   tool_key='mcp:another:draw'))

    def test_platform_from_run_is_not_taken_from_scene(self):
        context = tool_context.from_run({'platform_id': 'a', 'scene': {'platform_id': 'b'}})
        self.assertEqual(context.platform_id, 'a')

    async def test_large_catalog_selects_only_authorized_tools_and_keeps_reply(self):
        from app import nodes
        from app.schemas import ToolSelection
        from app.capabilities.registry import Tool
        async def run(args, state): return {}
        catalog = [Tool('read', 'read', '读取画布' + 'x' * 30000, {}, run,
                        tool_key='mcp:a:read'),
                   Tool('answer_directly', 'answer', '回答', {}, run,
                        tool_key='local:answers:direct')]
        with patch.object(nodes, 'structured_call', AsyncMock(return_value=(ToolSelection(tools=[0]), {}))):
            selected, _ = await nodes._planning_catalog(catalog, '读取画布')
            self.assertEqual([t.name for t in selected], ['read', 'answer_directly'])
        with patch.object(nodes, 'structured_call', AsyncMock(return_value=(ToolSelection(tools=[99]), {}))):
            with self.assertRaises(ValueError):
                await nodes._planning_catalog(catalog, '读取画布')

    def test_schema_preview_is_bounded_but_original_validation_schema_is_preserved(self):
        from app.capabilities.registry import Tool
        async def run(args, state): return {}
        schema = {'type': 'object', 'properties': {'body': {'type': 'string', 'description': 'x' * 30000}}, 'required': ['body']}
        tool = Tool('write', 'write', '', schema, run)
        preview = tools.planning_schema(tool)
        self.assertLess(len(str(preview)), 500)
        self.assertEqual(tool.args['properties']['body']['description'], 'x' * 30000)
