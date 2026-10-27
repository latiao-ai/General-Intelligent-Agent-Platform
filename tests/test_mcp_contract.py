"""协议验收：真实 stdio 服务、跨行业接入、动作身份与多主体并发隔离。"""
import asyncio
import base64
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.types import CallToolResult, ImageContent, TextContent

from app import execution_control, ledger, mcp_contract, tool_results, tools
from app.integrations import mcp_adapter, mcp_servers
from scripts import check_mcp_contract
from tests.exec_env import (install_main, install_plan, install_tools, new_store,
                            override_settings, reset_db, seed_run)

FAKE = Path(__file__).with_name('fake_contract_mcp.py')


def server(domain='inventory'):
    return mcp_servers.Server(
        id=domain, transport='stdio', stdio_command=sys.executable,
        stdio_args=(str(FAKE),), stdio_env={'TEST_DOMAIN': 'literal:' + domain})


class ContractTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await mcp_adapter.close_stdio_sessions()

    async def test_two_domains_use_the_same_client_and_distinct_action_identity(self):
        for domain in ('inventory', 'support'):
            source = server(domain)
            descriptors = await mcp_adapter.list_server_tools(source)
            self.assertEqual(mcp_contract.inspect_tool(descriptors[0]), [])
            with mcp_contract.action_scope('act_one'):
                first = await mcp_adapter.call_tool('create_record', {'name': 'same'}, server=source)
                replay = await mcp_adapter.call_tool('create_record', {'name': 'same'}, server=source)
            with mcp_contract.action_scope('act_two'):
                second = await mcp_adapter.call_tool('create_record', {'name': 'same'}, server=source)
            self.assertEqual(first['record_id'], replay['record_id'])
            self.assertNotEqual(first['record_id'], second['record_id'])
            self.assertTrue(first['record_id'].startswith(domain))
            self.assertEqual(first['operation_id'], 'act_one')

    async def test_missing_action_or_forged_action_does_not_send(self):
        source = server()
        with self.assertRaises(mcp_contract.ContractError):
            await mcp_adapter.call_tool('create_record', {'name': 'same'}, server=source)
        with mcp_contract.action_scope('trusted'):
            with self.assertRaises(mcp_contract.ContractError):
                await mcp_adapter.call_tool('create_record',
                    {'name': 'same', 'operation_id': 'forged'}, server=source)
            first = await mcp_adapter.call_tool('create_record', {'name': 'same'}, server=source)
        self.assertEqual(first['record_id'], 'inventory-1')

    async def test_discovery_hides_internal_field_and_keeps_output_schema(self):
        with patch.object(mcp_servers, 'servers', return_value=[server()]), \
                patch.object(tools.registry, 'adapt_mcp_tool', return_value=None):
            discovered = await tools.mcp_tools(force=True)
        spec = discovered[0]
        self.assertNotIn('operation_id', spec.args['properties'])
        self.assertEqual(spec.args['required'], ['name'])
        self.assertIn('record_id', tools.describe_tool(spec))
        self.assertTrue(spec.annotations['idempotentHint'])

    async def test_readonly_check_accepts_descriptor_without_creating_records(self):
        with patch.object(mcp_servers, 'servers', return_value=[server()]):
            report, code = await check_mcp_contract.run('inventory')
        self.assertEqual(code, 0, report)
        self.assertTrue(report['ok'])

    async def test_stdio_credential_is_injected_and_not_in_cache_key(self):
        from contextlib import asynccontextmanager
        seen = []
        @asynccontextmanager
        async def fake_transport(params):
            seen.append(params.env)
            yield 'read', 'write'
        from tests.test_stdio_session import _Session
        source = replace(server(), credential_env='ACCESS_TOKEN')
        with patch.object(mcp_adapter, 'stdio_client', fake_transport), \
                patch.object(mcp_adapter, 'ClientSession', _Session):
            async with mcp_adapter.connect_session(source, token='private-one'):
                pass
            async with mcp_adapter.connect_session(source, token='private-two'):
                pass
        self.assertEqual([env['ACCESS_TOKEN'] for env in seen], ['private-one', 'private-two'])
        self.assertEqual(seen[0]['TEST_DOMAIN'], 'inventory')
        self.assertNotIn('LLM_API_KEY', seen[0])
        self.assertNotIn('private', str(list(mcp_adapter._stdio_slots)))

    async def test_stdio_credential_without_mapping_is_rejected(self):
        from app.credentials import CredentialMissing
        with self.assertRaises(CredentialMissing):
            async with mcp_adapter.connect_session(server(), token='secret'):
                self.fail('must not open a session')

    async def test_input_rejection_and_output_rejection_have_different_certainty(self):
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock
        from jsonschema import ValidationError
        descriptor = SimpleNamespace(name='write', meta={}, inputSchema={
            'type': 'object', 'required': ['name'], 'properties': {'name': {'type': 'string'}}},
            outputSchema={'type': 'object', 'required': ['record_id']})
        session = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(
            structuredContent={'wrong': 'shape'}, content=[])))
        @asynccontextmanager
        async def connect(*args, **kwargs):
            yield session
        with patch.object(mcp_adapter, 'connect_session', connect), \
                patch.object(mcp_adapter, 'all_tools', AsyncMock(return_value=[descriptor])):
            with self.assertRaises(mcp_contract.ContractError) as local:
                await mcp_adapter.call_tool('write', {}, server=server())
            session.call_tool.assert_not_awaited()
            self.assertEqual(mcp_adapter.certainty_of_error(local.exception), 'not_executed')
            with self.assertRaises(ValidationError) as remote:
                await mcp_adapter.call_tool('write', {'name': 'sample'}, server=server())
            session.call_tool.assert_awaited_once()
            self.assertEqual(mcp_adapter.certainty_of_error(remote.exception), 'unknown')


class ResultTests(unittest.TestCase):
    def test_invalid_protocol_declaration_is_reported(self):
        item = {'description': 'write', 'inputSchema': {'type': 'object'},
                'outputSchema': {'type': 'object'},
                'annotations': {'readOnlyHint': False},
                '_meta': {'agent_hub': {'version': 1, 'idempotency_field': 'missing'}}}
        codes = {issue['code'] for issue in mcp_contract.inspect_tool(item)}
        self.assertEqual(codes, {'invalid_contract', 'missing_destructiveHint',
                                 'missing_idempotentHint'})

    def test_text_and_image_survive_real_payload_conversion(self):
        raw = b'\x89PNG\r\n\xff'
        result = CallToolResult(content=[
            TextContent(type='text', text='first'),
            ImageContent(type='image', mimeType='image/png', data=base64.b64encode(raw).decode()),
            TextContent(type='text', text='last')])
        payload = mcp_adapter.result_payload(result)
        self.assertEqual(payload['text'], 'first\nlast')
        self.assertNotIn(base64.b64encode(raw).decode(), json.dumps(payload))
        with tempfile.TemporaryDirectory() as folder:
            staged = tool_results.stage_result(folder, payload.captured)
            part = staged['parts'][1]
            stored = tool_results.open_registered_file(folder, tenant_id='default',
                result_id=staged['result_id'], storage_key=part['storage_key'])
            self.assertEqual(stored.read_bytes(), raw)
            self.assertEqual(len(staged['parts']), 3)
            page = tool_results.read_part(folder, 'default', part,
                result_id=staged['result_id'], limit=4)
            self.assertEqual(base64.b64decode(page['data_base64']), raw[:4])
            self.assertFalse(page['complete'])

    def test_structured_content_does_not_discard_other_blocks(self):
        payload = mcp_adapter.result_payload(CallToolResult(
            structuredContent={'record_id': '1'}, content=[
                TextContent(type='text', text='one'), TextContent(type='text', text='two')]))
        self.assertEqual(dict(payload), {'record_id': '1'})
        self.assertEqual(payload.captured.text(), 'one\ntwo')

    def test_declared_error_is_confirmed_failure(self):
        with self.assertRaises(mcp_adapter.MCPToolFailure) as caught:
            mcp_adapter.result_payload(CallToolResult(isError=True,
                content=[TextContent(type='text', text='sensitive backend trace')]))
        self.assertEqual(mcp_adapter.certainty_of_error(caught.exception), 'failed_confirmed')
        self.assertNotIn('sensitive', str(caught.exception))


class DiscoveryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_users_keep_distinct_catalogs_and_inner_capabilities(self):
        class Store:
            async def run(self, run_id):
                return {'tenant_id': run_id, 'owner_user_id': 'user'}
        store = Store()
        async def discover(source):
            tenant = execution_control.require().run_id
            await asyncio.sleep(0)
            return [{'name': tenant, 'inputSchema': {'type': 'object'}}], ''
        async def capabilities(source):
            await asyncio.sleep(0)
            return {execution_control.require().run_id: {'status': 'AVAILABLE'}}
        async def one(tenant):
            with ledger.use_ledger(ledger.ActionLedger(store, tenant, 1)), \
                    execution_control.use(execution_control.ExecutionContext(tenant)):
                result = await tools.mcp_tools()
                await asyncio.sleep(0)
                self.assertEqual(set(tools.known_inner_names('inventory')), {tenant})
                return [tool.name for tool in result]
        with override_settings(tool_policy_enforced=False, deploy_enabled=False), \
                patch.object(mcp_servers, 'servers', return_value=[server()]), \
                patch.object(tools, '_discover', discover), \
                patch.object(tools, '_discover_capabilities', capabilities), \
                patch.object(tools.registry, 'adapt_mcp_tool', return_value=None):
            a, b = await asyncio.gather(one('tenant_a'), one('tenant_b'))
        self.assertEqual(a, ['mcp__tenant_a'])
        self.assertEqual(b, ['mcp__tenant_b'])

    def test_reconnect_signature_changes_when_arguments_change(self):
        with patch.object(mcp_servers, 'servers', return_value=[server()]):
            first = tools._signature()
        with patch.object(mcp_servers, 'servers', return_value=[replace(server(), stdio_args=('new',))]):
            self.assertNotEqual(tools._signature(), first)


class WorkerContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_injects_persisted_action_id_into_external_server(self):
        from app.api_hub import Hub
        from scripts.run_worker import run_once
        await reset_db()
        store = new_store()
        try:
            await seed_run(store)
            spec = tools.Tool('create_record', '新建记录', '', {'type': 'object'},
                tools._make_mcp_runner('create_record', server()),
                tool_key='mcp:inventory:create_record', effect='write')
            steps = [{'tool': spec.name, 'step_id': 's1', 'args': {'name': 'sample'},
                      'tool_key': spec.tool_key, 'effect': 'write'}]
            plan_patch, _ = install_plan(steps)
            with tempfile.TemporaryDirectory() as folder, \
                    override_settings(tool_policy_enforced=False, observe_enabled=False,
                                      result_envelope_v2=True, artifact_dir=Path(folder)), \
                    install_main(folder), install_tools({spec.name: spec}), plan_patch:
                await run_once(Hub(store, owner_id='contract-worker'), 'contract-worker', run_id='run1')
                actions = await store.actions_of('run1')
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]['status'], 'succeeded')
                state = (await store.task('task1'))['state']
                if isinstance(state, str):
                    state = json.loads(state)
                receipt = state['mcp_calls'][0]['payload']
                self.assertEqual(receipt['operation_id'], actions[0]['action_id'])
                self.assertEqual(receipt['record_id'], 'inventory-1')
                result_id = state['step_outputs']['s1']['result_id']
                manifest = await store.result_manifest(result_id)
                self.assertEqual(manifest['run_id'], 'run1')
                parts = await store.result_parts(result_id)
                self.assertEqual(len(parts), 1)
                self.assertEqual(parts[0]['kind'], 'text')
        finally:
            await mcp_adapter.close_stdio_sessions()
            await store.close()
