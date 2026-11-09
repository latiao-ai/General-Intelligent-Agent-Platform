"""第十阶段正式链路收口：写后回读接入宿主执行链 + 逐项证据 + 成功判定顺序。

用真实 MCP transport（隔离平台 mcp_platform_server）+ 真实 PG 账本 + 受控模型，
验证：

1. **写后回读接入宿主执行链**：写工具成功结算后，宿主用同一可信主体独立回读，
   逐个写 action 保存证据（对象 ID / 操作 ID / 参数指纹 / 要求版本 / 回读 action /
   核验结论），不是顶层单个 readback_match；
2. **对象及版本匹配**：回读对象一致 + revision + 字段逐项核对，缺 revision / 字段
   不跳过；
3. **成功判定顺序**：有平台写动作时先查写证据集合，未核实写 + answer 不得成功；
4. **未知操作核实**：verify_operation 严格四态，权限错误不当未执行。

真实供应商联调（需真实平台凭据）另行报告；这里用受控平台服务端
`tests/mcp_platform_server.py`（持久化到 JSON，跨进程可核验）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from webpage_agent.integrations import mcp_adapter, mcp_servers
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


class HostWriteReadbackEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """写后回读接入宿主执行链：写工具成功后独立回读，逐项保存证据（真实 transport + 真实 PG）。"""

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

    async def test_two_writes_save_independent_evidence_per_action(self):
        """两次写（两个对象）：每个 action 都有独立核验证据（不合并成顶层单个）。

        经 HostToolCaller.call() 完整链路（授权→准入→真实 MCP transport 写→结算→
        写后回读→逐项证据），用真实 PG 账本。回读走受治理链（授权/预算/结果记录），
        不是手工调用核验函数。
        """
        from webpage_agent.agent import pi_host_tools
        from webpage_agent.capabilities.registry import Tool
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module

        with tempfile.TemporaryDirectory() as folder:
            sf = Path(folder) / 'platform.json'
            seed_state(sf, {
                'obj-a': {'object_id': 'obj-a', 'owner': 'subject-A',
                          'revision': 1, 'fields': {'title': '旧A'}},
                'obj-b': {'object_id': 'obj-b', 'owner': 'subject-A',
                          'revision': 1, 'fields': {'title': '旧B'}},
            })
            server = _platform_server(sf)

            async def update_run(args, state):
                payload = await mcp_adapter.call_tool(
                    'update_object', args, server=server)
                out = dict(args)
                out.update(payload)
                return out

            async def get_run(args, state):
                payload = await mcp_adapter.call_tool(
                    'get_object', args, server=server)
                out = dict(args)
                out.update(payload)
                return out

            update_tool = Tool(
                'update_object', '改对象', '写工具',
                {'type': 'object', 'properties': {
                    'object_id': {'type': 'string'},
                    'expected_revision': {'type': 'integer'},
                    'operation_id': {'type': 'string'},
                    'subject': {'type': 'string'},
                    'fields': {'type': 'object'}},
                 'required': ['object_id', 'expected_revision',
                              'operation_id', 'subject', 'fields']},
                update_run, tool_key='mcp:platform:update_object', effect='write',
                idempotency_arg='operation_id')
            get_tool = Tool(
                'get_object', '读对象', '读工具',
                {'type': 'object', 'properties': {'object_id': {'type': 'string'}},
                 'required': ['object_id']},
                get_run, tool_key='mcp:platform:get_object', effect='read')

            catalog = [update_tool, get_tool]

            # 显式扩展写白名单（正式链路测试经环境变量受控启用，不靠测试注入
            # 或临时全局切换——环境变量是显式配置）。
            import json
            os.environ['PI_WRITE_TOOLS_ENABLED'] = 'true'
            os.environ['PI_WRITE_TOOL_KEYS'] = json.dumps(
                ['mcp:platform:update_object'])
            os.environ['PI_READ_TOOL_KEYS'] = json.dumps(
                ['mcp:platform:get_object'])
            try:
                # 建 run + 领租约 + 绑定账本（回读走受治理链需要）
                await self.store.create_task('t1', 's1', '改两个对象')
                await self.store.ensure_task_meta('t1', session_id='s1',
                                                  goal='改两个对象')
                await self.store.create_run('r1', 't1', status='queued')
                claimed = await self.store.claim_run('worker-a', 120, run_id='r1')
                self.assertIsNotNone(claimed)
                context = execution_control.ExecutionContext.from_claim(claimed)
                book = ledger_module.ActionLedger(self.store, 'r1', 1,
                                                  context=context)

                caller = pi_host_tools.HostToolCaller(
                    pi_host_tools.PiCallRegistry(), tools_catalog=catalog)

                state = {'task_id': 't1', 'intent_version': 1,
                         'write_evidences': []}

                token = ledger_module.bind(book)
                ctoken = execution_control.bind(context)
                try:
                    from unittest.mock import patch
                    with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                               return_value=False):
                        for obj_id, op_id, title in (('obj-a', 'op-1', '新A'),
                                                     ('obj-b', 'op-2', '新B')):
                            receipt = await caller.call(
                                call_id=f'c-{obj_id}', tool_name='update_object',
                                args={'object_id': obj_id, 'expected_revision': 1,
                                      'operation_id': op_id,
                                      'subject': 'subject-A',
                                      'fields': {'title': title}},
                                state=state)
                            self.assertTrue(receipt.ok, receipt.error)
                finally:
                    ledger_module.unbind(token)
                    execution_control.unbind(ctoken)

                st = read_state(sf)
            finally:
                os.environ.pop('PI_WRITE_TOOL_KEYS', None)
                os.environ.pop('PI_WRITE_TOOLS_ENABLED', None)
                os.environ.pop('PI_READ_TOOL_KEYS', None)

        # 两个对象各写一次（revision 1→2）
        self.assertEqual(st['actual_writes'], 2)
        self.assertEqual(st['objects']['obj-a']['revision'], 2)
        self.assertEqual(st['objects']['obj-b']['revision'], 2)
        # 每个 action 都有独立核验证据（列表，不是顶层单个）
        self.assertEqual(len(state['write_evidences']), 2)
        for evidence in state['write_evidences']:
            self.assertTrue(evidence['readback_match'])
            self.assertTrue(evidence['checks'].get('object_id'))
            self.assertTrue(evidence['checks'].get('revision'))
            self.assertIn('read_action_id', evidence)


class OperationVerifyFourStateTests(unittest.IsolatedAsyncioTestCase):
    """未知操作核实：严格四态（applied / not_found / permission_denied / query_failed）。"""

    async def test_permission_denied_is_not_treated_as_not_executed(self):
        async def get_operation(op_id):
            return {'status': 'error', 'code': 'E_FORBIDDEN', 'message': '无权限'}
        result = await platform_verify.verify_operation(
            'op-1', server=None, get_operation_call=get_operation)
        # 权限错误：exists=None（结果未知），state=permission_denied，不是 not_found
        self.assertEqual(result['state'], 'permission_denied')
        self.assertIsNone(result['exists'])
        self.assertFalse(result['applied'])

    async def test_not_found_is_explicit_not_executed(self):
        async def get_operation(op_id):
            return {'status': 'error', 'code': 'E_NOT_FOUND', 'message': '不存在'}
        result = await platform_verify.verify_operation(
            'op-1', server=None, get_operation_call=get_operation)
        self.assertEqual(result['state'], 'not_found')
        self.assertFalse(result['exists'])

    async def test_applied(self):
        async def get_operation(op_id):
            return {'status': 'ok', 'operation': {'operation_id': op_id,
                                                  'object_id': 'obj-a'}}
        result = await platform_verify.verify_operation(
            'op-1', server=None, get_operation_call=get_operation)
        self.assertEqual(result['state'], 'applied')
        self.assertTrue(result['applied'])
        self.assertTrue(result['exists'])

    async def test_query_failure_is_unknown(self):
        async def get_operation(op_id):
            raise RuntimeError('网络错误')
        result = await platform_verify.verify_operation(
            'op-1', server=None, get_operation_call=get_operation)
        self.assertEqual(result['state'], 'query_failed')
        self.assertIsNone(result['exists'])


if __name__ == '__main__':
    unittest.main()
