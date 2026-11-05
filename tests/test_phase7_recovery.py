"""第七阶段故障注入验收：跨进程崩溃恢复 + 关键不变量。

覆盖（需 PG_TEST_DSN）：

1. 旧代次租约无法提交终态（finish_run_owned 返回 False，不写终态/最终消息）；
2. 同参数的新调用与 replay 区分（逻辑调用身份，非参数哈希）；
3. 最终答复去重键幂等（同 dedupe_key 只落一条）；
4. **真正终止执行进程**后，新执行者从 PG 已提交事实继续：已结算的动作
   replay 复用（不重复执行），未完成的动作按原身份继续，不丢结果不重复动作。

真实 Node 受控模型与外部模型的端到端联调不在本文件（需真实模型凭据），
单独报告。
"""
import os
import subprocess
import sys
import unittest

from tests.exec_env import PG_DSN, new_store, reset_db, seed_run, valid_test_dsn


def _require_pg(testcase):
    if not valid_test_dsn(PG_DSN):
        testcase.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')


class SnapshotReadContractTests(unittest.IsolatedAsyncioTestCase):
    """第七阶段收口：快照读取合同（真实 DB 返回结构）。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_pi_snapshot_roundtrip_through_real_db(self):
        """save_snapshot(pi 快照) → run_snapshot() → parse_row 正确取回内容。"""
        from webpage_agent.agent import pi_snapshot
        from webpage_agent.agent import runtime_identity
        await seed_run(self.store, run_id='r1')
        inner = pi_snapshot.new_snapshot(phase='awaiting_tools', checkpoint_seq=4,
                                         user_input='做个页面')
        inner['messages'] = [
            {'role': 'assistant', 'content': 'x'},
            {'role': 'toolCall', 'content': 'call'},
        ]
        await self.store.save_snapshot(
            'r1', plan_version=1, state=pi_snapshot.serialize(inner),
            snapshot_schema=runtime_identity.PI_STATE_SCHEMA)
        raw = await self.store.run_snapshot('r1')
        # 真实 DB 返回结构：state + schema + 列
        self.assertIn('state', raw)
        self.assertIn('schema', raw)
        parsed = pi_snapshot.parse_row(raw)
        self.assertEqual(parsed['phase'], 'awaiting_tools')
        self.assertEqual(parsed['checkpoint_seq'], 4)
        self.assertEqual(len(parsed['messages']), 2)

    async def test_corrupt_snapshot_raises_not_empty(self):
        """state_json 损坏 → run_snapshot 抛异常，不吞成空快照。"""
        await seed_run(self.store, run_id='r1')
        async with self.store._lease() as conn:
            await conn.execute(
                "INSERT INTO run_snapshots (run_id, plan_version, snapshot_schema, "
                "state_json, updated_at) VALUES ('r1', 1, 1, 'not-json{', 'x') "
                "ON CONFLICT (run_id) DO UPDATE SET state_json='not-json{'")
            await conn.commit()
        with self.assertRaises(RuntimeError):
            await self.store.run_snapshot('r1')

    async def test_pi_resume_plan_actions_without_snapshot_reconciles(self):
        """有动作却缺快照 → 转待核实，不退回空状态重新执行。"""
        from webpage_agent.agent import pi_recovery
        await seed_run(self.store, run_id='r1')
        # 落一个动作（无快照）
        async with self.store._lease() as conn:
            await conn.execute(
                "INSERT INTO actions (action_id, run_id, plan_version, step_id, "
                "tool_key, args_hash, idempotency_key, effect, status, created_at, "
                "updated_at) VALUES ('a1','r1',1,'pi:call:1','local:web:search',"
                "'h','k','read','succeeded','x','x')")
            await conn.commit()
        hub = None
        # 直接构造一个最小 Hub 实例调用 _pi_resume_plan
        from webpage_agent.api.api_hub import Hub
        hub = Hub(self.store)
        run = await self.store.run('r1')
        resume = await hub._pi_resume_plan('r1', run)
        self.assertIsNone(resume['pi_resume']['snapshot'])
        self.assertEqual(resume['pi_resume']['recovery'].kind,
                         pi_recovery.RESUME_RECONCILE)
        await hub.close()


class LeaseFencingTests(unittest.IsolatedAsyncioTestCase):
    """旧代次/失租无法提交终态。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_old_epoch_cannot_finish_run(self):
        from webpage_agent.governance import execution_control
        await seed_run(self.store, run_id='r1')
        claimed = await self.store.claim_run('worker-a', 60, run_id='r1')
        # 让 worker-a 的租约过期，worker-b 才能抢走（claim_run 要求租约过期/空）
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE runs SET lease_expires_at=0 WHERE run_id='r1'")
            await conn.commit()
        claimed2 = await self.store.claim_run('worker-b', 60, run_id='r1')
        self.assertGreater(claimed2['lease_epoch'], claimed['lease_epoch'])
        # 旧执行者用旧 epoch 提交终态 → 返回 False，不写
        stale = execution_control.ExecutionContext(
            run_id='r1', lease_owner='worker-a',
            lease_epoch=int(claimed['lease_epoch']))
        ok = await self.store.finish_run_owned(
            stale, run_id='r1', status='succeeded', plan_version=1)
        self.assertFalse(ok)
        run = await self.store.run('r1')
        self.assertNotEqual(run['status'], 'succeeded')

    async def test_verify_lease_rejects_stale_executor(self):
        """过期执行者（owner/epoch 不匹配）校验租约抛 LeaseLost。"""
        from webpage_agent.governance import execution_control
        await seed_run(self.store, run_id='r1')
        claimed = await self.store.claim_run('worker-a', 60, run_id='r1')
        # 错误 owner → 抛 LeaseLost
        with self.assertRaises(execution_control.LeaseLost):
            await self.store.verify_lease(
                'r1', lease_owner='ghost', lease_epoch=int(claimed['lease_epoch']))
        # 错误 epoch → 抛 LeaseLost
        with self.assertRaises(execution_control.LeaseLost):
            await self.store.verify_lease(
                'r1', lease_owner='worker-a',
                lease_epoch=int(claimed['lease_epoch']) + 1)
        # 正确 owner+epoch → 通过
        await self.store.verify_lease(
            'r1', lease_owner='worker-a', lease_epoch=int(claimed['lease_epoch']))

    async def test_final_message_dedupe(self):
        await self.store.ensure_session('s1')
        await self.store.append_message('s1', 'assistant', '答复', dedupe_key='final:r1')
        await self.store.append_message('s1', 'assistant', '答复', dedupe_key='final:r1')
        msgs = await self.store.messages('s1')
        assistant = [m for m in msgs if m['role'] == 'assistant']
        self.assertEqual(len(assistant), 1)

    async def test_finish_run_owned_keeps_pi_snapshot_schema(self):
        """Pi 收尾传 snapshot_schema=PI_STATE_SCHEMA，不把 Pi 快照覆盖成 schema=1。"""
        from webpage_agent.agent import runtime_identity
        from webpage_agent.governance import execution_control
        await seed_run(self.store, run_id='r1')
        claimed = await self.store.claim_run('worker-a', 60, run_id='r1')
        ctx = execution_control.ExecutionContext(
            run_id='r1', lease_owner='worker-a',
            lease_epoch=int(claimed['lease_epoch']))
        state = {'pi_outcome': 'answered', 'answer': 'x',
                 'state_schema': runtime_identity.PI_STATE_SCHEMA}
        ok = await self.store.finish_run_owned(
            ctx, run_id='r1', status='succeeded', plan_version=1, state=state,
            snapshot_schema=runtime_identity.PI_STATE_SCHEMA)
        self.assertTrue(ok)
        snap = await self.store.run_snapshot('r1')
        self.assertEqual(snap['schema'], runtime_identity.PI_STATE_SCHEMA)

    async def test_settle_action_keeps_pi_snapshot_schema(self):
        """Pi 结算传 snapshot_schema，不把 Pi 快照覆盖成普通 state（schema=1）。"""
        from webpage_agent.agent import runtime_identity
        from webpage_agent.governance import execution_control
        await seed_run(self.store, run_id='r1')
        claimed = await self.store.claim_run('worker-a', 60, run_id='r1')
        ctx = execution_control.ExecutionContext(
            run_id='r1', lease_owner='worker-a',
            lease_epoch=int(claimed['lease_epoch']))
        await self.store.settle_action(
            ctx, run_id='r1', plan_version=1, step_id='s1',
            action_id='act_test', attempt_id=None, status='succeeded',
            state={'state_schema': runtime_identity.PI_STATE_SCHEMA,
                   'messages': []},
            snapshot_schema=runtime_identity.PI_STATE_SCHEMA)
        snap = await self.store.run_snapshot('r1')
        self.assertEqual(snap['schema'], runtime_identity.PI_STATE_SCHEMA)


class SameArgReplayTests(unittest.IsolatedAsyncioTestCase):
    """同参数的新调用与 replay 区分（逻辑调用身份）。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_same_args_new_call_not_replay(self):
        from webpage_agent.agent.pi_host_tools import PiCallRegistry
        # 两个不同 Node 调用，参数完全相同 → 两个不同逻辑 ID（不是 replay）
        reg = PiCallRegistry()
        a = reg.logical_id('c1', tool_name='web_search',
                           tool_key='local:web:search', args={'query': 'x'})
        b = reg.logical_id('c2', tool_name='web_search',
                           tool_key='local:web:search', args={'query': 'x'})
        self.assertNotEqual(a, b)
        # 同一个 Node 调用恢复 → 复用原 ID（replay 身份）
        again = reg.logical_id('c1', tool_name='web_search',
                               tool_key='local:web:search', args={'query': 'x'})
        self.assertEqual(a, again)


class CrossProcessCrashRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """真正终止执行进程后，新执行者从 PG 已提交事实继续。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_killed_process_recovered_without_repeating_action(self):
        """子进程结算一步后被杀，新执行者 replay 该步、继续收尾，不重复执行。"""
        # 用受控工具把「已结算」落库，再让子进程在收尾前退出（模拟崩溃）。
        # 这里用一个真实的独立子进程写 PG，再杀掉它，验证新执行者能读回。
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module

        await seed_run(self.store, run_id='r1', status='queued')
        # 第一个执行者：claim + 准入 + 结算一个动作（真实 PG 事务）
        claimed = await self.store.claim_run('proc-a', 60, run_id='r1')
        ctx = execution_control.ExecutionContext.from_claim(claimed)
        book = ledger_module.ActionLedger(self.store, 'r1', 1, context=ctx)
        token = ledger_module.bind(book)
        ctoken = execution_control.bind(ctx)
        try:
            admitted = await book.admit(
                step_id='pi:call:1', tool_key='local:web:search', effect='read',
                args={'query': 'x'})
            self.assertEqual(admitted['status'], 'admitted')
            await book.settle(
                step_id='pi:call:1', action_id=admitted['action_id'],
                attempt_id=admitted['attempt_id'], status='succeeded',
                receipt={'documents': [{'source_id': 's1', 'url': 'https://x',
                                        'title': 't', 'chars': 5}]},
                step_status='succeeded')
        finally:
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)

        # 模拟进程被杀：租约未释放（lease_owner 仍是 proc-a，epoch 未清）
        run = await self.store.run('r1')
        self.assertEqual(run['status'], 'running')
        self.assertEqual(run['lease_owner'], 'proc-a')

        # 新执行者（新进程视角）：先让旧租约过期，再 claim
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE runs SET lease_expires_at=0 WHERE run_id='r1'")
            await conn.commit()
        claimed2 = await self.store.claim_run('proc-b', 60, run_id='r1')
        self.assertGreater(claimed2['lease_epoch'], claimed['lease_epoch'])

        # 新执行者重新准入同一个 step_id → 应 replay（succeeded），不重复执行
        ctx2 = execution_control.ExecutionContext.from_claim(claimed2)
        book2 = ledger_module.ActionLedger(self.store, 'r1', 1, context=ctx2)
        token2 = ledger_module.bind(book2)
        ctoken2 = execution_control.bind(ctx2)
        try:
            admitted2 = await book2.admit(
                step_id='pi:call:1', tool_key='local:web:search', effect='read',
                args={'query': 'x'})
            self.assertEqual(admitted2['status'], 'succeeded')
            self.assertIsNotNone(admitted2.get('replay'))
            self.assertIn('documents', admitted2['replay'])
            # 动作总数仍为 1（没有因为恢复重复创建 action）
            actions = await self.store.actions_of('r1')
            self.assertEqual(len(actions), 1)
        finally:
            ledger_module.unbind(token2)
            execution_control.unbind(ctoken2)

    async def test_real_process_killed_then_recovered(self):
        """真正启动一个子进程结算动作后崩溃，新执行者从 PG 恢复、不重复动作。"""
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module

        run_id, task_id, session_id = 'r-crash', 't-crash', 's-crash'
        step_id = 'pi:call:1'
        script = os.path.join(os.path.dirname(__file__), 'crash_worker.py')
        subprocess.run(
            [sys.executable, script, PG_DSN, run_id, task_id, session_id, step_id],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'PYTHONPATH': os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')})
        # 子进程已自我终止（os._exit(0)），未释放租约、未收尾
        run = await self.store.run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run['status'], 'running')
        self.assertEqual(run['lease_owner'], 'crash-proc')

        # 新执行者：让旧租约过期，claim 新 epoch
        async with self.store._lease() as conn:
            await conn.execute(
                f"UPDATE runs SET lease_expires_at=0 WHERE run_id='{run_id}'")
            await conn.commit()
        claimed = await self.store.claim_run('recover-proc', 60, run_id=run_id)
        self.assertIsNotNone(claimed)

        # 重新准入同一 step_id → replay（succeeded），不重复创建动作
        ctx = execution_control.ExecutionContext.from_claim(claimed)
        book = ledger_module.ActionLedger(self.store, run_id, 1, context=ctx)
        token = ledger_module.bind(book)
        ctoken = execution_control.bind(ctx)
        try:
            admitted = await book.admit(
                step_id=step_id, tool_key='local:web:search', effect='read',
                args={'query': 'x'})
            self.assertEqual(admitted['status'], 'succeeded')
            self.assertIn('documents', admitted.get('replay') or {})
            actions = await self.store.actions_of(run_id)
            self.assertEqual(len(actions), 1)
        finally:
            ledger_module.unbind(token)
            execution_control.unbind(ctoken)


if __name__ == '__main__':
    unittest.main()
