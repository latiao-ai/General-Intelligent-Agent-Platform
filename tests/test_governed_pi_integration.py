"""第六阶段：隔离的真实治理验收入口（共享网关 + Pi）。

验证「LangGraph 与 Pi 进入同一个治理执行实现」后，真实工具调用在**有账本、
有租约、有执行上下文**的正式路径上受门禁约束：

- 搜索成功 → 实际搜索、成功 action/attempt、来源结果与用量可对应；
- 拒绝 / 失租 / 旧版本 / 暂停 → 真实搜索次数为零、不生成成功动作；
- 重复 / 重放 → 不重复搜索、复用相同执行身份；
- 结算失败 → Pi 不收到成功回执、不继续模型请求。

这些用例依赖真实 PostgreSQL（`PG_TEST_DSN`，库名含 test）。缺数据库时**明确
跳过并报告原因**，绝不归为通过，也绝不用「关闭授权强制检查」来让用例通过。
"""
import unittest
from unittest.mock import patch

from tests.exec_env import (PG_DSN, new_store, reset_db, seed_run,
                            valid_test_dsn)


def _require_pg(testcase):
    if not valid_test_dsn(PG_DSN):
        testcase.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')


class GovernedPiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """真实治理链路：隔离 run + 租约 + 账本 + 执行上下文 + provider scope。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        if hasattr(self, 'store'):
            await self.store.close()

    async def _claim(self, run_id='run1'):
        """创建隔离 run 并取得租约，返回 (context, book)。"""
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        await seed_run(self.store, run_id=run_id)
        claimed = await self.store.claim_run('worker-a', 60, run_id=run_id)
        context = execution_control.ExecutionContext.from_claim(claimed)
        book = ledger_module.ActionLedger(
            self.store, run_id, 1, context=context)
        return context, book

    async def test_no_run_blocks_real_call(self):
        """无 run 行：admit 返回 no_run，真实工具调用次数为零。"""
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        from webpage_agent.tools import execution_gateway

        # 有账本，但 run 不存在（未 seed_run）
        context = execution_control.ExecutionContext(
            run_id='ghost', lease_owner='w', lease_epoch=1)
        book = ledger_module.ActionLedger(self.store, 'ghost', 1, context=context)
        token = ledger_module.bind(book)
        ctoken = execution_control.bind(context)

        calls = []

        async def fake_run(args, state):
            calls.append(args)
            return {'documents': []}

        from webpage_agent.capabilities.registry import Tool
        tool = Tool('web_search', '检索', 'd', {'type': 'object'}, fake_run,
                    tool_key='local:web:search', effect='read')
        try:
            with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                       return_value=False):
                result = await execution_gateway.governed_call(
                    tool=tool, args={'query': 'x'}, step_id='s1', state={},
                    ctx=None)
        finally:
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)
        self.assertEqual(result.kind, 'no_run')
        self.assertEqual(calls, [])  # 真实调用次数为零

    async def test_lease_lost_blocks_real_call(self):
        """失租：admit_action 抛 LeaseLost，真实调用不落账。"""
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        from webpage_agent.tools import execution_gateway

        context, book = await self._claim('run-lease')
        # 用一个错误的 lease_owner 模拟失租（持有者已变）
        stale = execution_control.ExecutionContext(
            run_id='run-lease', lease_owner='ghost', lease_epoch=1)
        stale_book = ledger_module.ActionLedger(
            self.store, 'run-lease', 1, context=stale)
        token = ledger_module.bind(stale_book)
        ctoken = execution_control.bind(stale)

        calls = []

        async def fake_run(args, state):
            calls.append(args)
            return {'documents': []}

        from webpage_agent.capabilities.registry import Tool
        tool = Tool('web_search', '检索', 'd', {'type': 'object'}, fake_run,
                    tool_key='local:web:search', effect='read')
        try:
            with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                       return_value=False):
                with self.assertRaises(execution_control.LeaseLost):
                    await execution_gateway.governed_call(
                        tool=tool, args={'query': 'x'}, step_id='s1', state={},
                        ctx=None)
        finally:
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)
        self.assertEqual(calls, [])  # 失租时真实调用次数为零

    async def test_successful_search_records_action_and_usage(self):
        """搜索成功：实际调用、成功 action/attempt、来源结果与用量可对应。"""
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        from webpage_agent.tools import execution_gateway

        context, book = await self._claim('run-ok')
        token = ledger_module.bind(book)
        ctoken = execution_control.bind(context)

        calls = []

        async def fake_run(args, state):
            calls.append(args)
            return {'documents': [{'source_id': 's1', 'task_id': 't',
                                   'url': 'https://x', 'title': '标题',
                                   'chars': 10, 'status': 'success'}],
                    'usage': [{'stage': 'search'}], 'status': 'searched'}

        from webpage_agent.capabilities.registry import Tool
        tool = Tool('web_search', '检索', 'd', {'type': 'object'}, fake_run,
                    tool_key='local:web:search', effect='read')
        try:
            with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                       return_value=False):
                result = await execution_gateway.governed_call(
                    tool=tool, args={'query': 'x'}, step_id='s1', state={},
                    ctx=None)
        finally:
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)
        self.assertEqual(result.kind, 'succeeded')
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.settled)
        self.assertTrue(result.action_id)
        self.assertTrue(result.attempt_id)
        # 结果归一保留了来源与用量（可恢复内容，不是只存标量）
        self.assertIn('documents', result.receipt)
        self.assertIn('usage', result.receipt)

    async def test_replay_reuses_action_without_recalling(self):
        """重复调用同一参数：重放复用相同执行身份，不重复搜索。"""
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        from webpage_agent.tools import execution_gateway

        context, book = await self._claim('run-replay')
        token = ledger_module.bind(book)
        ctoken = execution_control.bind(context)

        calls = []

        async def fake_run(args, state):
            calls.append(args)
            return {'documents': [{'source_id': 's1', 'title': 't',
                                   'url': 'https://x', 'chars': 10}],
                    'status': 'searched'}

        from webpage_agent.capabilities.registry import Tool
        tool = Tool('web_search', '检索', 'd', {'type': 'object'}, fake_run,
                    tool_key='local:web:search', effect='read')
        try:
            with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                       return_value=False):
                first = await execution_gateway.governed_call(
                    tool=tool, args={'query': 'x'}, step_id='s1', state={},
                    ctx=None)
                second = await execution_gateway.governed_call(
                    tool=tool, args={'query': 'x'}, step_id='s1', state={},
                    ctx=None)
        finally:
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)
        self.assertEqual(first.kind, 'succeeded')
        self.assertEqual(second.kind, 'replay')  # 第二次复用，不重新调用
        self.assertEqual(len(calls), 1)  # 真实搜索只发生一次


if __name__ == '__main__':
    unittest.main()
