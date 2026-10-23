"""阶段 D：结果未定不再靠异常名字猜（文档 §6、§8 后四行）。

三条纪律：

1. **确定性由最了解现场的那一层标注**（`app/errors.py`），不是按异常类名猜——
   包装过的异常类型名必然丢失；
2. 业务失败（服务端回了 `status: error`）是 `failed_confirmed`：允许重试；
3. 超时/断流是 `unknown`：账本置 unknown，续跑必须停下来等人核实，不能自动再发。

这里的"远端"是假的，但包装链路是真的：`_make_mcp_runner` → `ToolError` → 账本 →
run 终态。要证明的正是"包装之后确定性还在"。
"""
import asyncio
import tempfile
import unittest

from unittest.mock import patch

from tests.exec_env import (install_main, install_plan, install_tools,
                            new_store, reset_db, seed_run)

def publishing_tool_name() -> str:
    from app.capabilities import publishing
    return publishing.TOOL_NAME


# 工具名用平台侧的部署名：它的身份（tool_key）仍然指向那个 MCP 工具
STEPS = [{'tool': 'deploy_site', 'step_id': 's1',
          'args': {'files': []},
          'tool_key': 'mcp:pub:deploy_site', 'effect': 'write'}]


class McpUncertaintyTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await reset_db()

    def server(self):
        from app.integrations import mcp_servers
        return mcp_servers.Server(id='pub', transport='streamable_http',
                                  url='https://mcp.example/mcp')

    def deploy_tool(self, run=None):
        """平台侧部署工具：身份仍是那个 MCP 工具（tool_key=mcp:<server>:<name>）。"""
        from app import tools as tool_module
        from app.tools import Tool
        runner = run or tool_module._make_mcp_runner('deploy_site', self.server())
        return Tool(publishing_tool_name(), 'MCP·deploy_site', '部署站点',
                    {'type': 'object'}, runner,
                    tool_key='mcp:pub:deploy_site', effect='write')

    async def execute(self, store, call_tool, steps=STEPS):
        """走真实执行入口，只把 MCP 传输换成受控实现。"""
        import scripts.run_worker as worker
        from app.api_hub import Hub
        from app.integrations import mcp_adapter

        hub = Hub(store, owner_id='w1')
        plan_patch, _ = install_plan(steps)
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'deploy_site': self.deploy_tool()}), \
                plan_patch, patch.object(mcp_adapter, 'call_tool', call_tool):
            await worker.run_once(hub, 'w1', run_id='run1')

    async def action_of(self, store):
        actions = await store.actions_of('run1')
        self.assertEqual(len(actions), 1, '只应该有一个动作')
        return actions[0]

    async def test_business_failure_is_confirmed_and_retryable(self):
        """服务端明确回了失败 → failed_confirmed：状态 failed，不是"待核实"。"""
        store = new_store()
        await seed_run(store)
        calls = []

        from app.integrations import mcp_adapter

        async def call_tool(name, arguments, artifact=None, server=None):
            calls.append(name)
            raise mcp_adapter.MCPToolFailure('站点名冲突', 'E_DUPLICATE')

        await self.execute(store, call_tool)

        self.assertEqual(len(calls), 1)
        action = await self.action_of(store)
        self.assertEqual(action['status'], 'failed', '明确失败不该记成 unknown')
        self.assertEqual(action['receipt']['certainty'], 'failed_confirmed')
        run = await store.run('run1')
        self.assertEqual(run['status'], 'failed')
        await store.close()

    async def test_timeout_is_unknown_and_blocks_a_resend(self):
        """发出去了但没等到回执 → unknown：run 待核实，远端不能再被调一次。"""
        store = new_store()
        await seed_run(store)
        calls = []

        async def call_tool(name, arguments, artifact=None, server=None):
            calls.append(name)
            raise asyncio.TimeoutError('等回执超时')

        await self.execute(store, call_tool)

        self.assertEqual(len(calls), 1)
        action = await self.action_of(store)
        self.assertEqual(action['status'], 'unknown')
        self.assertEqual(action['receipt']['certainty'], 'unknown')
        self.assertEqual((await store.run('run1'))['status'], 'needs_reconcile')
        await store.close()

    async def test_connect_error_means_not_executed(self):
        """连接都建不起来 → not_executed：这一步确实没发生，可以显式重试。"""
        import httpx

        store = new_store()
        await seed_run(store)

        async def call_tool(name, arguments, artifact=None, server=None):
            raise httpx.ConnectError('连不上')

        await self.execute(store, call_tool)

        action = await self.action_of(store)
        self.assertEqual(action['status'], 'failed')
        self.assertEqual(action['receipt']['certainty'], 'not_executed')
        await store.close()

    async def test_wrapped_error_keeps_its_certainty(self):
        """包装成 ToolError 之后确定性还在（以前按类型名猜，到这里必丢）。"""
        from app.integrations import mcp_adapter

        store = new_store()
        await seed_run(store)
        seen = {}
        # 拿"未包装"的调用通道，自己包一层来观察异常类型
        from app import tools as tool_module
        runner = tool_module._make_mcp_runner('deploy_site', self.server())

        async def call_tool(name, arguments, artifact=None, server=None):
            raise mcp_adapter.MCPToolFailure('远端拒绝了', 'E_REJECT')

        async def spy(args, state):
            try:
                return await runner(args, state)
            except Exception as exc:                    # noqa: BLE001
                seen['type'] = type(exc).__name__
                seen['certainty'] = getattr(exc, 'certainty', '')
                seen['code'] = getattr(exc, 'error_code', '')
                raise

        tool = self.deploy_tool(run=spy)

        import scripts.run_worker as worker
        from app.api_hub import Hub
        hub = Hub(store, owner_id='w1')
        plan_patch, _ = install_plan(STEPS)
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'deploy_site': tool}), plan_patch, \
                patch.object(mcp_adapter, 'call_tool', call_tool):
            await worker.run_once(hub, 'w1', run_id='run1')

        self.assertEqual(seen['type'], 'ToolError')
        self.assertEqual(seen['certainty'], 'failed_confirmed',
                         '包装后确定性不能丢')
        self.assertEqual(seen['code'], 'E_REJECT', '错误码要透出来')
        self.assertEqual((await self.action_of(store))['status'], 'failed')
        await store.close()

    async def test_resume_refuses_while_a_result_is_unknown(self):
        """有结果未定的动作时，resume 不能把它当"没发生"清掉。"""
        store = new_store()
        await seed_run(store)

        async def call_tool(name, arguments, artifact=None, server=None):
            raise asyncio.TimeoutError('等回执超时')

        await self.execute(store, call_tool)
        self.assertEqual((await store.run('run1'))['status'], 'needs_reconcile')

        from app.api_hub import Hub
        hub = Hub(store, owner_id='w2')
        # 先把它挪到可恢复状态，再验证 resume 仍然拒绝（未知动作是硬门禁）
        await store.update_run('run1', status='paused', lease_owner=None,
                               lease_expires_at=None)
        self.assertIsNone(await hub.resume('run1'),
                          '结果未定时不能靠 resume 掩盖')
        self.assertEqual((await store.run('run1'))['status'], 'paused')
        await store.close()

    async def test_cancel_with_an_inflight_call_is_not_reported_as_stopped(self):
        """取消不能声称"远端也停了"：有在途写入时保留待核实。"""
        store = new_store()
        await seed_run(store)
        await store.save_plan('run1', 1, goal='部署', steps=STEPS)
        await store.create_action(
            'a1', run_id='run1', plan_version=1, step_id='s1',
            tool_key='mcp:pub:deploy_site', args_hash='h',
            idempotency_key='k1', effect='write')
        await store.update_action('a1', status='in_flight')

        from app.api_hub import Hub
        hub = Hub(store, owner_id='w1')
        self.assertTrue(await hub._has_uncertain_actions('run1'))
        self.assertIsNone(await hub.resume('run1'))
        await store.close()


if __name__ == '__main__':
    unittest.main()
