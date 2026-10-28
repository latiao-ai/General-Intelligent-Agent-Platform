"""stdio 会话在同一进程里复用，调用结束不关子进程。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.integrations import mcp_adapter
from app.integrations.mcp_adapter import MCPToolFailure


class _StdioCM:
    live = 0
    enters = 0

    def __init__(self, params):
        self.params = params

    async def __aenter__(self):
        type(self).enters += 1
        type(self).live += 1
        return ('read', 'write')

    async def __aexit__(self, *args):
        type(self).live -= 1
        return False


class _Session:
    def __init__(self, read, write):
        self.read = read
        self.write = write

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def initialize(self):
        return None


def _server(server_id='inventory'):
    return SimpleNamespace(
        id=server_id, transport='stdio',
        stdio_command='tool', stdio_args=('run',), token='')


class StdioSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _StdioCM.live = 0
        _StdioCM.enters = 0
        self._idle = settings.stdio_session_idle_seconds
        object.__setattr__(settings, 'stdio_session_idle_seconds', 600)
        self._patches = [
            patch.object(mcp_adapter, 'stdio_client', lambda params: _StdioCM(params)),
            patch.object(mcp_adapter, 'ClientSession', _Session),
        ]
        for item in self._patches:
            item.start()

    async def asyncTearDown(self):
        await mcp_adapter.close_stdio_sessions()
        object.__setattr__(settings, 'stdio_session_idle_seconds', self._idle)
        for item in self._patches:
            item.stop()

    async def test_same_server_reuses_the_process(self):
        server = _server()
        async with mcp_adapter.connect_session(server) as first:
            pass
        async with mcp_adapter.connect_session(server) as second:
            self.assertIs(first, second)
        self.assertEqual(_StdioCM.enters, 1)
        self.assertEqual(_StdioCM.live, 1)

    async def test_platform_customers_get_distinct_processes_and_transport_identity(self):
        from app import execution_control, ledger
        from unittest.mock import AsyncMock
        server = _server()
        server.forward_subject = True
        store = SimpleNamespace(run=AsyncMock(return_value={'platform_subject': 'alice'}))
        ctx = execution_control.ExecutionContext('r', lease_epoch=1)
        book = ledger.ActionLedger(store, 'r', 1, context=ctx)
        params_seen = []
        def client(params):
            params_seen.append(params)
            return _StdioCM(params)
        with ledger.use_ledger(book), execution_control.use(ctx), \
                patch.object(mcp_adapter, 'stdio_client', client):
            async with mcp_adapter.connect_session(server, token='platform-key') as alice:
                pass
            store.run.return_value = {'platform_subject': 'bob'}
            async with mcp_adapter.connect_session(server, token='platform-key') as bob:
                self.assertIsNot(alice, bob)
            self.assertEqual(params_seen[0].env['MCP_CALLER_SUBJECT'], 'alice')
            self.assertEqual(params_seen[1].env['MCP_CALLER_SUBJECT'], 'bob')
            self.assertEqual(params_seen[0].env['MCP_BEARER_TOKEN'], 'platform-key')
            store.run.return_value = {}
            with self.assertRaises(RuntimeError):
                async with mcp_adapter.connect_session(server, token='platform-key'):
                    pass

    async def test_other_server_gets_its_own_process(self):
        async with mcp_adapter.connect_session(_server('openmontage')):
            pass
        async with mcp_adapter.connect_session(_server('analytics')):
            pass
        self.assertEqual(_StdioCM.enters, 2)
        self.assertEqual(_StdioCM.live, 2)

    async def test_business_failure_keeps_the_process(self):
        server = _server()
        with self.assertRaises(MCPToolFailure):
            async with mcp_adapter.connect_session(server):
                raise MCPToolFailure('渲染失败', 'E_RENDER')
        async with mcp_adapter.connect_session(server):
            pass
        self.assertEqual(_StdioCM.enters, 1)
        self.assertEqual(_StdioCM.live, 1)

    async def test_broken_stream_starts_a_new_process(self):
        server = _server()
        with self.assertRaises(ConnectionResetError):
            async with mcp_adapter.connect_session(server):
                raise ConnectionResetError()
        self.assertEqual(_StdioCM.live, 0)
        async with mcp_adapter.connect_session(server):
            pass
        self.assertEqual(_StdioCM.enters, 2)
        self.assertEqual(_StdioCM.live, 1)

    async def test_idle_zero_closes_after_the_call(self):
        object.__setattr__(settings, 'stdio_session_idle_seconds', 0)
        async with mcp_adapter.connect_session(_server()):
            pass
        self.assertEqual(_StdioCM.live, 0)
        async with mcp_adapter.connect_session(_server()):
            pass
        self.assertEqual(_StdioCM.enters, 2)

    async def test_shutdown_closes_the_process(self):
        async with mcp_adapter.connect_session(_server()):
            pass
        await mcp_adapter.close_stdio_sessions()
        self.assertEqual(_StdioCM.live, 0)
        async with mcp_adapter.connect_session(_server()):
            pass
        self.assertEqual(_StdioCM.enters, 2)


if __name__ == '__main__':
    unittest.main()
