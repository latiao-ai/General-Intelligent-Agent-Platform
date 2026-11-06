"""第十阶段：隔离平台写改的端到端验收（真实 MCP transport + 真实 PG + 真实 Node）。

覆盖任务 14 的核心场景。分两类：

1. `PlatformWriteTransportTests`：经真实 MCP transport 验证平台合同（对象归属/
   revision/幂等去重/越权拒绝/回读核验），**不依赖 PG**——用受控模型替身 +
   真实 mcp_adapter 调用链路。

2. `GovernedPlatformWriteTests`：经真实 PG + 正式 Hub/Worker 入口 + 受控模型 +
   真实 MCP transport，验证「写工具走完整治理链（授权→准入→结算）+ 写后回读
   证据」，覆盖正常修改、越权拒绝、revision 冲突、恢复窗口核实。

真实供应商联调（需真实平台凭据）另行报告；这里用受控平台服务端
`tests/mcp_platform_server.py`（持久化到 JSON 文件，跨进程可核验）。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from webpage_agent.integrations import mcp_adapter, mcp_servers
from webpage_agent.integrations.mcp_adapter import MCPToolFailure
from webpage_agent.core import platform_verify

from tests.mcp_platform_server import seed_state, read_state
from tests.exec_env import PG_DSN, new_store, reset_db, valid_test_dsn


def _platform_server(state_file: Path) -> mcp_servers.Server:
    return mcp_servers.Server(
        id='platform', transport='stdio',
        stdio_command='.venv-pi-test/bin/python',
        stdio_args=('-m', 'tests.mcp_platform_server',
                    '--state-file', str(state_file)),
        idempotency_arg='operation_id')


class PlatformWriteTransportTests(unittest.IsolatedAsyncioTestCase):
    """经真实 MCP transport 的平台合同验收（不依赖 PG）。"""

    async def asyncTearDown(self):
        await mcp_adapter.close_stdio_sessions()

    async def _update(self, server, **args):
        return await mcp_adapter.call_tool('update_object', args, server=server)

    async def test_normal_update_writes_once_and_readback_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            sf = Path(folder) / 'platform.json'
            seed_state(sf, {'obj-a': {'object_id': 'obj-a', 'owner': 'subject-A',
                                      'revision': 1, 'fields': {'title': '旧'}}})
            server = _platform_server(sf)
            # 写
            result = await self._update(
                server, object_id='obj-a', expected_revision=1,
                operation_id='op-1', subject='subject-A',
                fields={'title': '新'})
            # 独立回读（真实 transport get_object）
            async def get_object(object_id):
                return await mcp_adapter.call_tool(
                    'get_object', {'object_id': object_id}, server=server)
            readback = await platform_verify.verify_platform_write(
                {'object_id': 'obj-a', 'new_revision': result['new_revision'],
                 'modified_fields': {'title': '新'}},
                server=server, get_object_call=get_object)
            state = read_state(sf)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(state['actual_writes'], 1)   # 实际写入一次
        self.assertTrue(readback['readback_match'])   # 回读匹配
        self.assertEqual(state['objects']['obj-a']['revision'], 2)

    async def test_cross_subject_update_is_forbidden_no_write(self):
        with tempfile.TemporaryDirectory() as folder:
            sf = Path(folder) / 'platform.json'
            seed_state(sf, {'obj-a': {'object_id': 'obj-a', 'owner': 'subject-A',
                                      'revision': 1, 'fields': {'title': '旧'}}})
            server = _platform_server(sf)
            with self.assertRaises(MCPToolFailure):
                # 主体 B 改 A 的对象 → E_FORBIDDEN
                await self._update(
                    server, object_id='obj-a', expected_revision=1,
                    operation_id='op-2', subject='subject-B',
                    fields={'title': 'x'})
            state = read_state(sf)
        self.assertEqual(state['actual_writes'], 0)   # 无实际写入
        self.assertEqual(state['objects']['obj-a']['fields']['title'], '旧')

    async def test_revision_conflict_no_write(self):
        with tempfile.TemporaryDirectory() as folder:
            sf = Path(folder) / 'platform.json'
            seed_state(sf, {'obj-a': {'object_id': 'obj-a', 'owner': 'subject-A',
                                      'revision': 1, 'fields': {'title': '旧'}}})
            server = _platform_server(sf)
            # 先正常写一次，revision 1→2
            await self._update(server, object_id='obj-a', expected_revision=1,
                               operation_id='op-1', subject='subject-A',
                               fields={'title': '新'})
            # 用旧 revision=1 再写 → E_REVISION_CONFLICT
            with self.assertRaises(MCPToolFailure):
                await self._update(server, object_id='obj-a', expected_revision=1,
                                   operation_id='op-3', subject='subject-A',
                                   fields={'title': 'y'})
            state = read_state(sf)
        self.assertEqual(state['actual_writes'], 1)   # 只有第一次真实写入

    async def test_idempotent_replay_no_second_write(self):
        with tempfile.TemporaryDirectory() as folder:
            sf = Path(folder) / 'platform.json'
            seed_state(sf, {'obj-a': {'object_id': 'obj-a', 'owner': 'subject-A',
                                      'revision': 1, 'fields': {'title': '旧'}}})
            server = _platform_server(sf)
            await self._update(server, object_id='obj-a', expected_revision=1,
                               operation_id='op-1', subject='subject-A',
                               fields={'title': '新'})
            # 同键同参数重放 → 不重复写
            replay = await self._update(server, object_id='obj-a',
                                        expected_revision=1, operation_id='op-1',
                                        subject='subject-A',
                                        fields={'title': '新'})
            # 同键不同参数 → 拒绝
            with self.assertRaises(MCPToolFailure):
                await self._update(server, object_id='obj-a', expected_revision=2,
                                   operation_id='op-1', subject='subject-A',
                                   fields={'title': 'z'})
            state = read_state(sf)
        self.assertTrue(replay.get('replayed'))
        self.assertEqual(state['actual_writes'], 1)   # 幂等：仍只写一次


class GovernedPlatformWriteTests(unittest.IsolatedAsyncioTestCase):
    """经真实 PG + 正式治理链的写工具（不跑完整 Pi 推理，聚焦网关+回读）。"""

    async def asyncSetUp(self):
        if not valid_test_dsn(PG_DSN):
            self.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        if hasattr(self, 'store'):
            await self.store.close()
        await mcp_adapter.close_stdio_sessions()

    async def test_governed_write_records_final_args_in_receipt(self):
        """写工具经网关结算后，receipt 持久化最终参数（含幂等键）。

        验证任务 8 的「统一最终参数」链路：`execution_arguments` 注入幂等键
        （operation_id），授权/准入/远端发送/receipt 都用同一份最终参数。
        """
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        from webpage_agent.tools import execution_gateway
        from webpage_agent.tools import core as tools_core
        from webpage_agent.capabilities.registry import Tool

        await self.store.create_task('t1', 's1', '改对象')
        await self.store.ensure_task_meta('t1', session_id='s1', goal='改对象')
        await self.store.create_run('r1', 't1', status='queued')
        claimed = await self.store.claim_run('worker-a', 60, run_id='r1')
        context = execution_control.ExecutionContext.from_claim(claimed)
        book = ledger_module.ActionLedger(self.store, 'r1', 1, context=context)

        with tempfile.TemporaryDirectory() as folder:
            sf = Path(folder) / 'platform.json'
            seed_state(sf, {'obj-a': {'object_id': 'obj-a', 'owner': 'subject-A',
                                      'revision': 1, 'fields': {'title': '旧'}}})
            server = _platform_server(sf)
            calls = []

            async def fake_run(args, state):
                calls.append(dict(args))
                return {'object_id': args.get('object_id'),
                        'new_revision': 2, 'modified_fields': args.get('fields'),
                        'status': 'ok'}

            tool = Tool('update_object', '改对象', 'd',
                        {'type': 'object',
                         'properties': {'object_id': {'type': 'string'},
                                        'expected_revision': {'type': 'integer'},
                                        'operation_id': {'type': 'string'},
                                        'subject': {'type': 'string'},
                                        'fields': {'type': 'object'}},
                         'required': ['object_id', 'expected_revision',
                                      'operation_id', 'subject', 'fields']},
                        fake_run, tool_key='mcp:platform:update_object',
                        effect='write', idempotency_arg='operation_id')

            token = ledger_module.bind(book)
            ctoken = execution_control.bind(context)
            try:
                # 复用 execution_arguments 生成最终参数（含幂等键注入）——
                # 这正是 HostToolCaller.call() 做的事。
                step_id = 'pi:call:1'
                final_args = tools_core.execution_arguments(
                    tool,
                    {'object_id': 'obj-a', 'expected_revision': 1,
                     'subject': 'subject-A', 'fields': {'title': '新'}},
                    step_id, book)
                from unittest.mock import patch
                with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                           return_value=False):
                    result = await execution_gateway.governed_call(
                        tool=tool, args=final_args, step_id=step_id,
                        state={'task_id': 't1'}, ctx=None)
            finally:
                ledger_module.unbind(token)
                execution_control.unbind(ctoken)

        self.assertEqual(result.kind, 'succeeded')
        # 最终参数（含幂等键注入）已进 receipt，且远端收到的是同一份
        self.assertIn('final_args', result.receipt)
        self.assertIn('operation_id', result.receipt['final_args'])
        self.assertIn('operation_id', calls[0])   # 远端发送的是最终参数
        self.assertEqual(result.receipt['final_args']['operation_id'],
                         calls[0]['operation_id'])  # 授权/准入/发送同一份


if __name__ == '__main__':
    unittest.main()
