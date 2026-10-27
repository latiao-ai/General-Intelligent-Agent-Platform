"""通用能力迁移的边界：只读约束、引用归属、完整计划和连接生命周期。"""
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anyio

from app import nodes, observation, outcome
from app.capabilities.base import ToolError
from app.capabilities.registry import Tool
from app.config import settings
from app.integrations import mcp_adapter


class GeneralPlatformTests(unittest.TestCase):
    def test_platform_query_rejects_unknown_effect_and_writes(self):
        tools = [Tool(name, name, '', {}, None, effect=effect,
                      server_id='inventory', tool_key=f'mcp:inventory:{name}')
                 for name, effect in [('read', 'read'), ('change', 'write'),
                                      ('unknown', 'unknown')]]
        issues = nodes.check_result_locus(
            'platform_state', [{'tool': 'unknown'}], tools)
        self.assertIn('platform_state_needs_read', {i['code'] for i in issues})
        issues = nodes.check_result_locus(
            'platform_state', [{'tool': 'read'}, {'tool': 'change'}], tools)
        self.assertIn('platform_state_cannot_write', {i['code'] for i in issues})

    def test_read_receipt_cannot_prove_an_artifact_exists(self):
        checked = observation.completion_state(
            [{'kind': 'artifact_exists', 'target': '交付产物'}], state={},
            results=[{'tool_key': 'mcp:inventory:list_items',
                      'execution_status': 'succeeded'}])
        self.assertFalse(checked['ok'])

    def test_effect_only_success_requires_the_whole_plan(self):
        state = {'plan': {'steps': [
            {'step_id': 's1', 'effect': 'write'},
            {'step_id': 's2', 'effect': 'write'}]},
                 'step_outputs': {'s1': {'__status__': 'succeeded'}}}
        self.assertEqual(outcome.final_status(state), 'failed')
        state['step_outputs']['s2'] = {'__status__': 'succeeded'}
        self.assertEqual(outcome.final_status(state), 'succeeded')

    def test_negative_array_index_is_not_a_json_pointer(self):
        with self.assertRaises(ToolError):
            nodes.resolve_inputs({'step_id': 's2', 'args': {
                'id': {'ref': {'step_id': 's1', 'pointer': '/ids/-1'}}}},
                {'s1': {'ids': ['first', 'last']}})

    def test_saved_reference_requires_an_unambiguous_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'result.json'
            path.write_text(json.dumps({'payload': {'result': {
                'left': {'id': 'a'}, 'right': {'id': 'b'}}}}))
            outputs = {'s1': {'tool_result': {'data_ref': 'result.json'}}}
            step = {'step_id': 's2', 'args': {'id': {'ref': {
                'step_id': 's1', 'pointer': '/id'}}}}
            with self.assertRaisesRegex(ToolError, '歧义'):
                nodes.resolve_inputs(step, outputs, folder)
            step['args']['id']['ref']['pointer'] = '/result/left/id'
            self.assertEqual(nodes.resolve_inputs(step, outputs, folder)['id'], 'a')

    def test_saved_reference_cannot_read_outside_the_task(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'task'
            root.mkdir()
            outside = Path(folder) / 'other.json'
            outside.write_text('{"id":"other-task"}')
            record = {'tool_result': {'data_ref': str(outside)}}
            self.assertEqual(nodes._saved_result_views(record, str(root)), [])


class StdioScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_enters_and_exits_anyio_scopes_in_one_task(self):
        owners = []

        @asynccontextmanager
        async def transport(_params):
            async with anyio.create_task_group():
                owners.append(asyncio.current_task())
                try:
                    yield 'read', 'write'
                finally:
                    owners.append(asyncio.current_task())

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def initialize(self):
                pass

        server = SimpleNamespace(id='inventory', transport='stdio',
                                 stdio_command='mock', stdio_args=('run',), token='')
        previous = settings.stdio_session_idle_seconds
        object.__setattr__(settings, 'stdio_session_idle_seconds', 600)
        try:
            with patch.object(mcp_adapter, 'stdio_client', transport), \
                    patch.object(mcp_adapter, 'ClientSession', lambda *a: Session()):
                async with mcp_adapter.connect_session(server):
                    pass
                with self.assertNoLogs(mcp_adapter.logger, level='WARNING'):
                    await asyncio.create_task(mcp_adapter.close_stdio_sessions())
            self.assertEqual(len(owners), 2)
            self.assertIs(owners[0], owners[1])
        finally:
            await mcp_adapter.close_stdio_sessions()
            object.__setattr__(settings, 'stdio_session_idle_seconds', previous)

