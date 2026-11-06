"""第九阶段：单一 MCP 服务的受控接入与协议验收。

用**真实 MCP stdio transport** 拉起受控服务端（`tests/mcp_probe_server.py`），
经 `mcp_adapter` 的正式连接/发现/调用链路验证协议验收点，**不依赖任何外部
供应商**：

1. 协议发现与 schema：`list_server_tools` 返回 readOnlyHint / taskSupport 元数据；
2. 同步只读工具：经真实 transport 调用成功、结果归一、schema 校验失败确定性；
3. 异步任务：提交恰好一次（远端计数）→ 轮询 → completed → 结果交付含唯一标记；
4. 错误确定性：未知工具 / schema 失败 / 业务失败的确定性标注；
5. 取消：`cancel_task` 经真实 transport 的协作式取消语义。

主体隔离（tenant/user 凭据绑定）由 Python 侧授权层（`mcp_connections` /
`tool_authorization`）负责，已在第六/八阶段用 mock 账本覆盖；本测试聚焦
「真正通过 MCP transport」的协议验收，真实供应商联调另行报告。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from webpage_agent.integrations import mcp_adapter, mcp_servers
from webpage_agent.integrations.mcp_adapter import MCPToolFailure

from tests.mcp_probe_server import RESULT_MARKER, read_state


@contextmanager
def assigned(target, **fields):
    saved = {name: getattr(target, name) for name in fields}
    for name, value in fields.items():
        object.__setattr__(target, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            object.__setattr__(target, name, value)


def _probe_server(state_file: Path) -> mcp_servers.Server:
    """一个指向受控 stdio 服务端的 Server 配置。"""
    return mcp_servers.Server(
        id='probe', transport='stdio',
        stdio_command='.venv-pi-test/bin/python',
        stdio_args=('-m', 'tests.mcp_probe_server',
                    '--state-file', str(state_file)))


class McpDiscoveryContractTests(unittest.IsolatedAsyncioTestCase):
    """协议发现与 schema：真实 transport 下的工具元数据。"""

    async def asyncTearDown(self):
        await mcp_adapter.close_stdio_sessions()

    async def test_discovery_reads_readonly_and_task_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            server = _probe_server(Path(folder) / 'probe.json')
            tools = await mcp_adapter.list_server_tools(server)
        by_name = {t['name']: t for t in tools}
        self.assertIn('read_only_probe', by_name)
        self.assertIn('async_submit', by_name)
        # readOnlyHint → effect_type = read
        self.assertEqual(by_name['read_only_probe']['effect_type'], 'read')
        self.assertTrue(by_name['read_only_probe']['annotations']['readOnlyHint'])
        # taskSupport → required
        self.assertEqual(by_name['async_submit']['task_support'], 'required')
        # schema 完整带回（inputSchema 是 object 且有 required）
        self.assertEqual(
            by_name['read_only_probe']['inputSchema']['type'], 'object')
        self.assertIn('query',
                      by_name['read_only_probe']['inputSchema']['required'])


class SyncReadOnlyToolTests(unittest.IsolatedAsyncioTestCase):
    """同步只读工具：真实 transport 调用、结果归一、schema 校验。"""

    async def asyncTearDown(self):
        await mcp_adapter.close_stdio_sessions()

    async def test_read_only_tool_roundtrip_and_state(self):
        with tempfile.TemporaryDirectory() as folder:
            state_file = Path(folder) / 'probe.json'
            server = _probe_server(state_file)
            payload = await mcp_adapter.call_tool(
                'read_only_probe', {'query': 'hello'}, server=server)
            state = read_state(state_file)
        self.assertEqual(payload['ok'], True)
        self.assertEqual(payload['echo'], 'hello')
        self.assertEqual(payload['marker'], RESULT_MARKER)
        # 远端调用恰好一次
        self.assertEqual(state['read_only_calls'], 1)

    async def test_schema_validation_failure_is_not_executed(self):
        """schema 校验失败：确定性 not_executed，远端调用数为零。"""
        from jsonschema import ValidationError
        with tempfile.TemporaryDirectory() as folder:
            state_file = Path(folder) / 'probe.json'
            server = _probe_server(state_file)
            with self.assertRaises(ValidationError):
                # 缺 required 的 query
                await mcp_adapter.call_tool('read_only_probe', {}, server=server)
            state = read_state(state_file)
        self.assertEqual(state.get('read_only_calls', 0), 0)

    async def test_unknown_tool_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            server = _probe_server(Path(folder) / 'probe.json')
            with self.assertRaises(RuntimeError):
                await mcp_adapter.call_tool('nope', {}, server=server)


class AsyncTaskContractTests(unittest.IsolatedAsyncioTestCase):
    """异步任务：提交恰好一次 → 轮询 → completed → 结果交付。"""

    async def asyncTearDown(self):
        await mcp_adapter.close_stdio_sessions()

    async def _submit_and_await(self, server, state_file, payload_text):
        task = await mcp_adapter.call_tool_as_task(
            'async_submit', {'payload': payload_text}, server=server)
        task_id = task['taskId']
        self.assertTrue(task_id)
        # 轮询直到 completed 且结果可读
        result = None
        for _ in range(100):
            status = await mcp_adapter.task_status(task_id, server=server)
            if status.get('status') == 'completed' and not status.get('result_pending'):
                result = status.get('result')
                break
            await asyncio.sleep(0.05)
        return task_id, result, read_state(state_file)

    async def test_async_submit_once_and_deliver_marked_result(self):
        with tempfile.TemporaryDirectory() as folder:
            state_file = Path(folder) / 'probe.json'
            server = _probe_server(state_file)
            task_id, result, state = await self._submit_and_await(
                server, state_file, 'payload-1')
        # 结果交付含唯一标记（证明读到的是真实远端结果，非硬编码）
        self.assertIsNotNone(result)
        self.assertEqual(result['marker'], RESULT_MARKER)
        self.assertEqual(result['payload'], 'payload-1')
        # 远端提交恰好一次（不重复提交）
        self.assertEqual(state['async_submissions'], 1)

    async def test_two_submits_are_two_remote_submissions(self):
        """两次独立提交 → 远端提交计数为 2，各自结果独立。"""
        with tempfile.TemporaryDirectory() as folder:
            state_file = Path(folder) / 'probe.json'
            server = _probe_server(state_file)
            _, r1, _ = await self._submit_and_await(server, state_file, 'a')
            _, r2, state = await self._submit_and_await(server, state_file, 'b')
        self.assertEqual(r1['payload'], 'a')
        self.assertEqual(r2['payload'], 'b')
        self.assertEqual(state['async_submissions'], 2)

    async def test_cancel_task_is_collaborative(self):
        """tasks/cancel 经真实 transport 返回任务状态（协作式，可能已完成）。"""
        with tempfile.TemporaryDirectory() as folder:
            server = _probe_server(Path(folder) / 'probe.json')
            task = await mcp_adapter.call_tool_as_task(
                'async_submit', {'payload': 'x'}, server=server)
            task_id = task['taskId']
            cancelled = await mcp_adapter.cancel_task(task_id, server=server)
        # 取消返回任务引用（status 合法；服务端可能已完成，属协作式语义）
        self.assertIn(cancelled.get('status'),
                      ('working', 'completed', 'failed', 'cancelled'))


class ErrorCertaintyTests(unittest.IsolatedAsyncioTestCase):
    """错误确定性标注：区分「未执行」与「业务失败」。"""

    async def asyncTearDown(self):
        await mcp_adapter.close_stdio_sessions()

    def test_schema_validation_error_is_not_executed(self):
        from jsonschema import ValidationError
        self.assertEqual(
            mcp_adapter.certainty_of_error(ValidationError('bad')),
            'not_executed')

    def test_mcp_tool_failure_is_failed_confirmed(self):
        self.assertEqual(
            mcp_adapter.certainty_of_error(
                MCPToolFailure('失败', 'E_SOMETHING')),
            'failed_confirmed')

    def test_connect_error_is_not_executed(self):
        import httpx
        self.assertEqual(
            mcp_adapter.certainty_of_error(httpx.ConnectError('refused')),
            'not_executed')


if __name__ == '__main__':
    unittest.main()
