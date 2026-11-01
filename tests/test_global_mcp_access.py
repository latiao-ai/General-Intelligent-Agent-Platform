"""通用服务（谁都能用）与平台私有服务的边界。

盯三件事：

1. 通用绑定（`platforms:['*'] + tenants:['*']`）对**任意平台、任意租户**都解析得到；
2. 平台私有绑定仍然只对对应平台生效，通配平台不等于通配租户；
3. 通用绑定的**内层工具白名单 fail-closed**：名字缺失、清单读不到、名字不在
   白名单里，都在本地就挡住，不发远端请求。
"""
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from webpage_agent.core import credentials
from webpage_agent.governance import execution_control
from webpage_agent.store import ledger
from webpage_agent.core import mcp_connections
from webpage_agent.tools import core as tools
from webpage_agent.tools import tool_authorization
from webpage_agent.tools import tool_context
from webpage_agent.tools import tool_policy
from webpage_agent.capabilities.base import ToolError
from webpage_agent.core.credentials import SHARED_TENANT, CredentialBinding
from webpage_agent.integrations.mcp_servers import Server


class _Store:
    """够用的内存仓储：只实现授权链真正用到的那几个方法。"""

    def __init__(self, servers, rows, policies=None):
        self.servers = servers
        self.rows = rows
        self.policies = policies or []
        self.run = AsyncMock(return_value=None)

    async def server_binding(self, tenant_id, server_id):
        for row in self.servers:
            if row['tenant_id'] == tenant_id and row['server_id'] == server_id:
                return row
        return None

    async def credential_bindings(self, *, tenant_id, server_binding_id='',
                                  subject_kind='', subject_id=''):
        out = []
        for row in self.rows:
            if row['tenant_id'] != tenant_id:
                continue
            if server_binding_id and row['server_binding_id'] != server_binding_id:
                continue
            out.append(row)
        return out

    async def tool_policies(self, tenant_id='default'):
        return [row for row in self.policies if row['tenant_id'] == tenant_id]


def _server(binding_id, tenant, server_id, enabled=1):
    return {'binding_id': binding_id, 'tenant_id': tenant,
            'server_id': server_id, 'enabled': enabled}


def _cred(binding_id, tenant, server_binding, ref, scope, kind='tenant',
          subject='', status='active', revision=1):
    return {'binding_id': binding_id, 'tenant_id': tenant, 'subject_kind': kind,
            'subject_id': subject or tenant, 'server_binding_id': server_binding,
            'credential_ref': ref, 'scope': scope, 'revision': revision,
            'status': status}


def _binding(scope, tenant=SHARED_TENANT, kind='tenant', subject=''):
    return CredentialBinding(binding_id='cb', tenant_id=tenant,
                             subject_kind=kind, subject_id=subject or tenant,
                             server_binding_id='sb', credential_ref='literal:',
                             scope=scope, revision=1, status='active')


class ScopeMatchTests(unittest.TestCase):
    """`matches()`：通配必须显式，缺键与空列表都不放行。"""

    def test_universal_platform_matches_every_platform(self):
        binding = _binding({'platforms': ['*'], 'tenants': ['*']})
        for platform in ('beeftv', 'othertv', ''):
            self.assertTrue(binding.matches('studio-demo', 'anyone', platform))

    def test_universal_tenant_does_not_leak_without_the_key(self):
        binding = _binding({'platforms': ['*']})
        self.assertTrue(binding.matches(SHARED_TENANT, 'anyone', 'beeftv'))
        self.assertFalse(binding.matches('studio-demo', 'anyone', 'beeftv'))

    def test_platform_scoped_binding_still_refuses_other_platforms(self):
        binding = _binding({'platforms': ['beeftv']}, tenant='studio-demo')
        self.assertTrue(binding.matches('studio-demo', 'u', 'beeftv'))
        self.assertFalse(binding.matches('studio-demo', 'u', 'othertv'))
        self.assertFalse(binding.matches('studio-demo', 'u', ''))

    def test_missing_or_empty_platform_list_is_not_a_wildcard(self):
        self.assertFalse(_binding({}, tenant='studio-demo')
                         .matches('studio-demo', 'u', 'beeftv'))
        self.assertFalse(_binding({'platforms': []}, tenant='studio-demo')
                         .matches('studio-demo', 'u', 'beeftv'))

    def test_universal_property_and_inner_tools(self):
        binding = _binding({'platforms': ['*'], 'inner_tools': ['video_post']})
        self.assertTrue(binding.universal)
        self.assertEqual(binding.inner_tools(), ['video_post'])
        self.assertFalse(_binding({'platforms': ['beeftv']}).universal)


class SharedBindingTests(unittest.IsolatedAsyncioTestCase):
    """共享绑定：别的租户也能解析到，平台私有的 still 只看平台。"""

    def setUp(self):
        credentials.invalidate_cache()
        self.store = _Store(
            [_server('sb-shared', SHARED_TENANT, 'publishing'),
             _server('sb-beeftv', 'studio-demo', 'beeftv')],
            [_cred('cb-shared', SHARED_TENANT, 'sb-shared', 'literal:',
                   {'platforms': ['*'], 'tenants': ['*']}),
             _cred('cb-beeftv', 'studio-demo', 'sb-beeftv', 'literal:',
                   {'platforms': ['beeftv']})])

    async def test_another_tenant_resolves_the_universal_server(self):
        for tenant in ('studio-demo', 'default'):
            binding = await mcp_connections.binding_for_server(
                self.store, tenant_id=tenant, user_id='u',
                server_id='publishing', platform_id='othertv')
            self.assertIsNotNone(binding, tenant)
            self.assertEqual(binding.binding_id, 'cb-shared')

    async def test_platform_private_server_stays_platform_scoped(self):
        ok = await mcp_connections.binding_for_server(
            self.store, tenant_id='studio-demo', user_id='u',
            server_id='beeftv', platform_id='beeftv')
        self.assertIsNotNone(ok)
        wrong = await mcp_connections.binding_for_server(
            self.store, tenant_id='studio-demo', user_id='u',
            server_id='beeftv', platform_id='othertv')
        self.assertIsNone(wrong)

    async def test_shared_row_without_tenant_wildcard_is_not_reusable(self):
        self.store.rows[0]['scope'] = {'platforms': ['*']}
        binding = await mcp_connections.binding_for_server(
            self.store, tenant_id='studio-demo', user_id='u',
            server_id='publishing', platform_id='beeftv')
        self.assertIsNone(binding)

    async def test_bearer_is_reused_for_every_platform(self):
        for platform in ('beeftv', 'othertv', ''):
            self.assertEqual(await mcp_connections.bearer_for(
                self.store, tenant_id='default', user_id='u',
                server_id='publishing', platform_id=platform), '')


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """发现期就按绑定过滤：平台私有服务连 transport 都不打开。"""

    def setUp(self):
        credentials.invalidate_cache()
        self.store = _Store(
            [_server('sb-shared', SHARED_TENANT, 'publishing'),
             _server('sb-beeftv', 'studio-demo', 'beeftv')],
            [_cred('cb-shared', SHARED_TENANT, 'sb-shared', 'literal:',
                   {'platforms': ['*'], 'tenants': ['*']}),
             _cred('cb-beeftv', 'studio-demo', 'sb-beeftv', 'literal:',
                   {'platforms': ['beeftv']})])
        self.servers = [Server('publishing', url='http://deploy.invalid/mcp'),
                        Server('beeftv', transport='stdio', stdio_command='true')]

    async def _discover_for(self, platform, tool_names=('deploy_site',)):
        self.store.run = AsyncMock(return_value={
            'tenant_id': 'studio-demo', 'owner_user_id': 'u',
            'platform_id': platform})
        context = execution_control.ExecutionContext('run-1', lease_epoch=1)
        book = ledger.ActionLedger(self.store, 'run-1', 1, context=context)
        discover = AsyncMock(return_value=(
            [{'name': name, 'inputSchema': {'type': 'object'}} for name in tool_names], ''))
        with ledger.use_ledger(book), execution_control.use(context), \
                patch.object(tools, 'settings', replace(tools.settings, tool_policy_enforced=True)), \
                patch.object(tools.mcp_servers, 'servers', return_value=self.servers), \
                patch.object(tools, '_discover', discover), \
                patch.object(tools.registry, 'mcp_placeholder', return_value=None), \
                patch.object(tools, '_discover_capabilities', AsyncMock(return_value={})):
            found = await tools.mcp_tools(force=True)
        return [call.args[0].id for call in discover.await_args_list], \
               [t.tool_key for t in found]

    async def test_beeftv_user_sees_universal_and_own_platform_only(self):
        touched, keys = await self._discover_for('beeftv')
        self.assertEqual(sorted(touched), ['beeftv', 'publishing'])
        self.assertIn('mcp:publishing:deploy_site', keys)
        self.assertTrue(all(k.startswith(('mcp:publishing:', 'mcp:beeftv:'))
                            for k in keys))

    async def test_other_platform_only_reaches_the_universal_server(self):
        touched, keys = await self._discover_for('othertv')
        self.assertEqual(touched, ['publishing'])
        self.assertEqual(keys, ['mcp:publishing:deploy_site'])

    async def test_credential_tool_whitelist_hides_the_other_tools(self):
        self.store.rows[0]['scope'] = {'platforms': ['*'], 'tenants': ['*'],
                                       'tools': ['mcp:publishing:deploy_site']}
        _touched, keys = await self._discover_for(
            'othertv', tool_names=('deploy_site', 'delete_site', 'list_sites'))
        self.assertEqual(keys, ['mcp:publishing:deploy_site'])

    async def test_without_whitelist_all_discovered_tools_are_visible(self):
        _touched, keys = await self._discover_for(
            'othertv', tool_names=('deploy_site', 'delete_site'))
        self.assertEqual(sorted(keys), ['mcp:publishing:delete_site',
                                        'mcp:publishing:deploy_site'])


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    """政策维度：通用服务缺政策放行（内置默认），平台私有服务缺政策照旧拒绝。"""

    def setUp(self):
        credentials.invalidate_cache()
        self.store = _Store(
            [_server('sb-shared', SHARED_TENANT, 'publishing'),
             _server('sb-beeftv', 'studio-demo', 'beeftv')],
            [_cred('cb-shared', SHARED_TENANT, 'sb-shared', 'literal:',
                   {'platforms': ['*'], 'tenants': ['*'],
                    'tools': ['mcp:publishing:deploy_site']}),
             _cred('cb-beeftv', 'studio-demo', 'sb-beeftv', 'literal:',
                   {'platforms': ['beeftv']})])

    def context(self, platform='othertv'):
        return tool_context.ToolCallContext(tenant_id='studio-demo',
                                            user_id='u', platform_id=platform)

    async def test_universal_server_needs_no_policy(self):
        with patch('webpage_agent.tools.tool_policy.enforcement_enabled', return_value=True):
            decision = await tool_authorization.authorize_call(
                self.store, self.context(), tool_key='mcp:publishing:deploy_site')
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.policy.policy_id, tool_authorization.UNIVERSAL_POLICY_ID)

    async def test_platform_server_still_needs_a_policy(self):
        with patch('webpage_agent.tools.tool_policy.enforcement_enabled', return_value=True):
            with self.assertRaises(tool_authorization.AuthorizationDenied) as ctx:
                await tool_authorization.authorize_call(
                    self.store, self.context('beeftv'), tool_key='mcp:beeftv:draw')
        self.assertEqual(ctx.exception.error_code, 'E_NO_POLICY')

    async def test_credential_tool_whitelist_blocks_other_tools(self):
        with patch('webpage_agent.tools.tool_policy.enforcement_enabled', return_value=True):
            with self.assertRaises(tool_authorization.AuthorizationDenied) as ctx:
                await tool_authorization.authorize_call(
                    self.store, self.context(), tool_key='mcp:publishing:delete_site')
        self.assertEqual(ctx.exception.error_code, 'E_CREDENTIAL_SCOPE')

    async def test_revocation_is_checked_for_shared_bindings(self):
        with patch('webpage_agent.tools.tool_policy.enforcement_enabled', return_value=True):
            decision = await tool_authorization.authorize_call(
                self.store, self.context(), tool_key='mcp:publishing:deploy_site')
            await tool_authorization.check_revocations(
                self.store, decision, tool_key='mcp:publishing:deploy_site',
                tenant_id='studio-demo', user_id='u', platform_id='othertv')
            self.store.rows[0]['status'] = 'inactive'
            with self.assertRaises(tool_authorization.AuthorizationDenied) as ctx:
                await tool_authorization.check_revocations(
                    self.store, decision, tool_key='mcp:publishing:deploy_site',
                    tenant_id='studio-demo', user_id='u', platform_id='othertv')
        self.assertEqual(ctx.exception.error_code, 'E_CREDENTIAL_REVOKED')

    async def test_policy_revocation_still_blocks_universal_server(self):
        self.store.policies = [{'policy_id': 'p1', 'tenant_id': 'studio-demo',
                                'tool_key': 'mcp:publishing:deploy_site',
                                'enabled': 0}]
        with patch('webpage_agent.tools.tool_policy.enforcement_enabled', return_value=True):
            with self.assertRaises(tool_authorization.AuthorizationDenied) as ctx:
                await tool_authorization.authorize_call(
                    self.store, self.context(), tool_key='mcp:publishing:deploy_site')
        self.assertEqual(ctx.exception.error_code, 'E_POLICY_DISABLED')


class InnerAllowlistTests(unittest.IsolatedAsyncioTestCase):
    """`run_tool(name=…)` 的内层白名单：通用绑定 fail-closed。"""

    def setUp(self):
        self.server = Server('openmontage', transport='stdio', stdio_command='true',
                             capability_tool='list_capabilities', tool_name_arg='name')

    def test_missing_inner_name_is_refused_even_without_capabilities(self):
        with self.assertRaises(ToolError) as ctx:
            tools._check_inner_name(self.server, {}, {}, strict_allow=['video_post'])
        self.assertEqual(ctx.exception.certainty, 'not_executed')

    def test_inner_name_outside_the_allowlist_is_refused(self):
        with self.assertRaises(ToolError) as ctx:
            tools._check_inner_name(self.server, {'name': 'video_generation'},
                                    {'video_generation': {'status': 'ready'},
                                     'video_post': {'status': 'ready'}},
                                    strict_allow=['video_post'])
        self.assertIn('白名单', str(ctx.exception))

    def test_failed_capability_discovery_is_fail_closed(self):
        with self.assertRaises(ToolError) as ctx:
            tools._check_inner_name(self.server, {'name': 'video_post'}, {},
                                    strict_allow=['video_post'])
        self.assertIn('能力清单', str(ctx.exception))

    def test_allowed_inner_name_passes(self):
        tools._check_inner_name(self.server, {'name': 'video_post'},
                                {'video_post': {'status': 'available'}},
                                strict_allow=['video_post'])

    def test_platform_scoped_binding_keeps_lenient_behaviour(self):
        # strict_allow=None（平台私有绑定）：沿用原行为，清单读不到不拦。
        tools._check_inner_name(self.server, {'name': 'whatever'}, {}, strict_allow=None)

    async def test_allowlist_comes_from_the_universal_binding(self):
        credentials.invalidate_cache()
        store = _Store([_server('sb-om', SHARED_TENANT, 'openmontage')],
                       [_cred('cb-om', SHARED_TENANT, 'sb-om', 'literal:',
                              {'platforms': ['*'], 'tenants': ['*'],
                               'inner_tools': ['video_post']})])
        store.run = AsyncMock(return_value={'tenant_id': 'studio-demo',
                                            'owner_user_id': 'u',
                                            'platform_id': 'othertv'})
        context = execution_control.ExecutionContext('run-1', lease_epoch=1)
        book = ledger.ActionLedger(store, 'run-1', 1, context=context)
        with ledger.use_ledger(book), execution_control.use(context):
            allow = await mcp_connections.inner_allowlist_for_current(self.server)
            # 平台私有绑定不改变旧行为
            credentials.invalidate_cache()
            store.rows[0]['scope'] = {'platforms': ['othertv'], 'tenants': ['*']}
            lenient = await mcp_connections.inner_allowlist_for_current(self.server)
        self.assertEqual(allow, ['video_post'])
        self.assertIsNone(lenient)


class AdminApiGuardTests(unittest.TestCase):
    """管理 API 不能自封通用：平台方自报的 platform_id 写不出 `'*'`。"""

    def test_reserved_scope_is_rejected(self):
        from fastapi import HTTPException
        from webpage_agent.api import routes as api
        for kwargs in ({'tenant_id': SHARED_TENANT}, {'tenant_id': 'studio-demo',
                                                      'platform_id': '*'}):
            with self.assertRaises(HTTPException) as ctx:
                api._reject_reserved_scope(**kwargs)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_normal_scope_passes(self):
        from webpage_agent.api import routes as api
        api._reject_reserved_scope('studio-demo', 'beeftv')


if __name__ == '__main__':
    unittest.main()
