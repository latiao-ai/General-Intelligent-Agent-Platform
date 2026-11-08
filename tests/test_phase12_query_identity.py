"""第十二阶段缺口修复：查询身份模型验收（评审点 1）。

验证「普通查询能用」的核心缺口修复：query turn 通过**内部查询执行记录**
（runs 行）+ 绑定的 ActionLedger 走完整治理链，让只读工具（web_search）经
`governed_call` 成功调用，而不是被 `no_run` 拒绝；同时证明跨主体 / 超预算 /
撤权仍被拒绝。

对照 `test_governed_pi_integration.py`（那里用 seed_run + claim_run 建业务 run），
这里用新增的 `create_turn_run`（内部查询执行记录）复现同一治理纪律：

- 成功：内部记录 + 账本 → 只读工具调用 succeeded，真实调用发生、账本落 action；
- 撤权/失租：错误 lease 身份 → LeaseLost，真实调用次数为零；
- 不污染领取池：租约未过期的内部记录不会被 `claim_run`（业务领取）领走；
- 收尾：`settle_turn_run` 置终态并清租约，不留 running 僵尸行。

依赖真实 PostgreSQL（`PG_TEST_DSN`，库名含 test）；缺库时明确跳过。
"""
import unittest
from unittest.mock import patch

from tests.exec_env import (PG_DSN, new_store, reset_db, valid_test_dsn)


def _require_pg(testcase):
    if not valid_test_dsn(PG_DSN):
        testcase.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')


class QueryIdentityGovernanceTests(unittest.IsolatedAsyncioTestCase):
    """内部查询执行记录 + 账本：只读工具走通治理 + 拒绝路径仍有效。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        if hasattr(self, 'store'):
            await self.store.close()

    async def _claim_query_turn(self, turn_id='turn-q1'):
        """建内部查询执行记录并取得账本/执行上下文（模拟 query turn 执行）。"""
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module

        created = await self.store.create_turn_run(
            run_id=turn_id, tenant_id='tenant-a', owner_user_id='user-u',
            lease_owner='worker-a', lease_epoch=1, lease_ttl=120.0)
        context = execution_control.ExecutionContext(
            run_id=turn_id, lease_owner='worker-a', lease_epoch=1)
        book = ledger_module.ActionLedger(
            self.store, turn_id, 1, context=context)
        return created, context, book

    async def test_create_turn_run_creates_internal_execution_record(self):
        """内部查询执行记录：runs 行存在，running + 租约 + pi 引擎，非业务 run。"""
        created, _, _ = await self._claim_query_turn('turn-rec')
        self.assertIsNotNone(created, '内部查询执行记录应已创建')
        row = await self.store.run('turn-rec')
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['runtime_kind'], 'pi')
        self.assertEqual(row['tenant_id'], 'tenant-a')
        self.assertEqual(row['owner_user_id'], 'user-u')
        self.assertEqual(row['lease_owner'], 'worker-a')
        self.assertEqual(row['lease_epoch'], 1)

    async def test_readonly_tool_succeeds_through_governance(self):
        """只读工具调用：内部记录 + 账本 → governed_call 走通，真实调用发生。"""
        from webpage_agent.store import ledger as ledger_module
        from webpage_agent.tools import execution_gateway
        from webpage_agent.governance import execution_control

        _, context, book = await self._claim_query_turn('turn-ok')
        token = ledger_module.bind(book)
        ctoken = execution_control.bind(context)

        calls = []

        async def fake_run(args, state):
            calls.append(args)
            return {'documents': [{'source_id': 's1', 'title': '标题',
                                   'url': 'https://x', 'chars': 10,
                                   'status': 'success'}],
                    'usage': [{'stage': 'search'}], 'status': 'searched'}

        from webpage_agent.capabilities.registry import Tool
        tool = Tool('web_search', '检索', 'd', {'type': 'object'}, fake_run,
                    tool_key='local:web:search', effect='read')
        try:
            with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                       return_value=False):
                result = await execution_gateway.governed_call(
                    tool=tool, args={'query': 'x'}, step_id='s1', state={},
                    ctx=await execution_gateway.tool_context(
                        step_id='s1', action_id=''))
        finally:
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)
        self.assertEqual(result.kind, 'succeeded', result.error)
        self.assertEqual(len(calls), 1)  # 真实调用发生（不再是 no_run）
        self.assertTrue(result.settled)
        self.assertTrue(result.action_id)

    async def test_lease_lost_still_blocks_readonly_call(self):
        """撤权/失租：错误 lease 身份 → LeaseLost，真实调用次数为零。"""
        from webpage_agent.store import ledger as ledger_module
        from webpage_agent.tools import execution_gateway
        from webpage_agent.governance import execution_control

        created, _, _ = await self._claim_query_turn('turn-lost')
        self.assertIsNotNone(created)
        # 用错误 lease_owner 模拟执行权已被别人接管
        stale = execution_control.ExecutionContext(
            run_id='turn-lost', lease_owner='ghost', lease_epoch=1)
        stale_book = ledger_module.ActionLedger(
            self.store, 'turn-lost', 1, context=stale)
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
                        ctx=await execution_gateway.tool_context(
                            step_id='s1', action_id=''))
        finally:
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)
        self.assertEqual(calls, [])  # 失租时真实调用次数为零

    async def test_budget_exhausted_blocks_readonly_call(self):
        """超预算：工具调用预算用尽 → kind='failed'（E_RUN_BUDGET），不真实调用。"""
        from webpage_agent.store import ledger as ledger_module
        from webpage_agent.tools import execution_gateway
        from webpage_agent.governance import execution_control
        from webpage_agent.core.config import settings

        _, context, book = await self._claim_query_turn('turn-budget')
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
        # 工具调用预算上限设 1：第一次成功，第二次超预算。
        original_limit = settings.run_max_tool_calls
        object.__setattr__(settings, 'run_max_tool_calls', 1)
        try:
            with patch('webpage_agent.tools.tool_policy.enforcement_enabled',
                       return_value=False):
                ctx = await execution_gateway.tool_context(
                    step_id='s1', action_id='')
                first = await execution_gateway.governed_call(
                    tool=tool, args={'query': 'a'}, step_id='s1', state={},
                    ctx=ctx)
                second = await execution_gateway.governed_call(
                    tool=tool, args={'query': 'b'}, step_id='s2', state={},
                    ctx=ctx)
        finally:
            object.__setattr__(settings, 'run_max_tool_calls', original_limit)
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)
        self.assertEqual(first.kind, 'succeeded')
        self.assertEqual(second.kind, 'failed', second.error)
        self.assertEqual(second.error_code, 'E_RUN_BUDGET')
        self.assertEqual(len(calls), 1)  # 只有第一次真实调用，第二次被预算拦截

    async def test_cross_principal_ctx_comes_from_turn_record(self):
        """跨主体防护：可信主体来自库里 turn 落库的 owner，不来自请求自报。"""
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        from webpage_agent.tools import execution_gateway

        # 建 turn（owner = user-u），并落库 session_turns 的 owner_user_id
        await self.store.create_turn(
            'turn-ctx', 'sess1', request_id='req-1', request_hash='h1',
            message='查一下', tenant_id='tenant-a', owner_user_id='user-u')
        # 领取 turn 得到 lease（模拟 Worker 领取）
        await self.store.claim_turn('worker-a', 60, session_id='sess1')
        # 建内部执行记录（owner=user-u，与 turn 一致）
        await self.store.create_turn_run(
            run_id='turn-ctx', tenant_id='tenant-a', owner_user_id='user-u',
            lease_owner='worker-a', lease_epoch=1, lease_ttl=120.0)
        context = execution_control.ExecutionContext(
            run_id='turn-ctx', lease_owner='worker-a', lease_epoch=1)
        book = ledger_module.ActionLedger(
            self.store, 'turn-ctx', 1, context=context)
        token = ledger_module.bind(book)
        ctoken = execution_control.bind(context)
        try:
            ctx = await execution_gateway.tool_context(step_id='s1', action_id='')
        finally:
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)
        self.assertIsNotNone(ctx, 'query turn 应取得可信主体')
        # 主体来自库里 run 行（内部记录），不是请求自报
        self.assertEqual(ctx.user_id, 'user-u')
        self.assertEqual(ctx.tenant_id, 'tenant-a')

    async def test_internal_record_not_claimed_by_business_pool(self):
        """内部查询记录不污染业务领取池：租约未过期不会被 claim_run 领走。"""
        created, _, _ = await self._claim_query_turn('turn-pool')
        self.assertIsNotNone(created)
        # 建一条真实业务 run（queued），确认 claim_run 领的是业务 run 而非内部记录
        await self.store.create_task('biz-task', 'sess1', '做个页面')
        await self.store.ensure_task_meta('biz-task', session_id='sess1',
                                          goal='做个页面')
        await self.store.create_run('biz-run', 'biz-task', status='queued')
        claimed = await self.store.claim_run('worker-b', 60)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed['run_id'], 'biz-run',
                         '业务领取池应领走业务 run，而非内部查询记录')

    async def test_settle_turn_run_clears_lease(self):
        """收尾：settle_turn_run 置终态并清租约，不留 running 僵尸行。"""
        created, _, _ = await self._claim_query_turn('turn-done')
        self.assertIsNotNone(created)
        ok = await self.store.settle_turn_run(
            run_id='turn-done', status='completed',
            lease_owner='worker-a', lease_epoch=1)
        self.assertTrue(ok)
        row = await self.store.run('turn-done')
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['lease_expires_at'], 0)

    async def test_settle_turn_run_wrong_lease_rejected(self):
        """收尾门禁：错误 lease 身份不能收尾。"""
        created, _, _ = await self._claim_query_turn('turn-guard')
        self.assertIsNotNone(created)
        ok = await self.store.settle_turn_run(
            run_id='turn-guard', status='completed',
            lease_owner='ghost', lease_epoch=1)
        self.assertFalse(ok, '错误 lease 身份不得收尾')


if __name__ == '__main__':
    unittest.main()
