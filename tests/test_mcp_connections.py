"""租户凭据注入：同一服务端不能串用别人的 token，缺绑定不能退回全局钥匙。"""
import os
import unittest
from unittest.mock import patch

from app import credentials, mcp_connections, tool_policy
from app.integrations import mcp_adapter


class _Store:
    def __init__(self, servers, credentials_rows):
        self.servers = servers
        self.rows = credentials_rows

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


def _server(binding_id, tenant, server_id):
    return {'binding_id': binding_id, 'tenant_id': tenant, 'server_id': server_id,
            'enabled': 1}


def _cred(binding_id, tenant, kind, subject, server_binding, ref):
    return {'binding_id': binding_id, 'tenant_id': tenant, 'subject_kind': kind,
            'subject_id': subject, 'server_binding_id': server_binding,
            'credential_ref': ref, 'scope': {}, 'revision': 1, 'status': 'active'}


class BearerResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        credentials.invalidate_cache()
        self.store = _Store(
            [_server('sb-a', 'tenant-a', 'comic'),
             _server('sb-b', 'tenant-b', 'comic')],
            [_cred('c-a', 'tenant-a', 'tenant', 'tenant-a', 'sb-a', 'env:TOKEN_A'),
             _cred('c-user', 'tenant-a', 'user', 'zhang', 'sb-a', 'env:TOKEN_USER'),
             _cred('c-b', 'tenant-b', 'tenant', 'tenant-b', 'sb-b', 'env:TOKEN_B')])

    async def test_two_tenants_get_their_own_tokens(self):
        env = {'TOKEN_A': 'alpha', 'TOKEN_B': 'beta', 'TOKEN_USER': 'user-key'}
        with patch.dict(os.environ, env, clear=False):
            token_a = await mcp_connections.bearer_for(
                self.store, tenant_id='tenant-a', user_id='li', server_id='comic')
            token_b = await mcp_connections.bearer_for(
                self.store, tenant_id='tenant-b', user_id='li', server_id='comic')
            token_user = await mcp_connections.bearer_for(
                self.store, tenant_id='tenant-a', user_id='zhang', server_id='comic')
        self.assertEqual(token_a, 'alpha')
        self.assertEqual(token_b, 'beta')
        self.assertEqual(token_user, 'user-key')

    async def test_other_servers_credential_is_not_reused(self):
        with patch.object(tool_policy, 'enforcement_enabled', return_value=True):
            with self.assertRaises(credentials.CredentialMissing):
                await mcp_connections.bearer_for(
                    self.store, tenant_id='tenant-a', user_id='li',
                    server_id='video')

    async def test_unenforced_without_binding_inherits_process_token(self):
        with patch.object(tool_policy, 'enforcement_enabled', return_value=False):
            token = await mcp_connections.bearer_for(
                self.store, tenant_id='tenant-a', user_id='li', server_id='video')
        self.assertIsNone(token)

    def test_explicit_token_does_not_fall_back(self):
        self.assertEqual(
            mcp_adapter.auth_headers('tenant-secret', 'global-secret'),
            {'Authorization': 'Bearer tenant-secret'})
        self.assertIsNone(mcp_adapter.auth_headers('', 'global-secret'))
        self.assertEqual(
            mcp_adapter.auth_headers(None, 'global-secret'),
            {'Authorization': 'Bearer global-secret'})

    def test_tool_key_names_its_server(self):
        self.assertEqual(mcp_connections.server_id_of('mcp:comic:update_scene'),
                         'comic')
        self.assertEqual(mcp_connections.server_id_of('local:web:search'), '')


class RevocationLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_failure_stops_the_send(self):
        from app import tool_authorization

        class Broken:
            async def credential_bindings(self, **kwargs):
                raise RuntimeError('db down')

        decision = tool_policy.PolicyDecision(
            allowed=True,
            credential=credentials.CredentialBinding(
                binding_id='c-a', tenant_id='tenant-a', subject_kind='tenant',
                subject_id='tenant-a', server_binding_id='sb-a',
                credential_ref='env:TOKEN_A', scope={}, revision=1))
        policy = tool_policy.ToolPolicy(
            policy_id='p', tenant_id='tenant-a', tool_key='mcp:comic:draw',
            approval_mode=tool_policy.APPROVAL_AUTO, enabled=True)
        with patch.object(tool_policy, 'enforcement_enabled', return_value=True), \
                patch.object(tool_authorization, 'load_policies',
                             return_value=[policy]):
            with self.assertRaises(tool_authorization.AuthorizationDenied) as ctx:
                await tool_authorization.check_revocations(
                    Broken(), decision, tool_key='mcp:comic:draw',
                    tenant_id='tenant-a', user_id='li')
        self.assertEqual(ctx.exception.error_code, 'E_CREDENTIAL_LOOKUP_FAILED')
