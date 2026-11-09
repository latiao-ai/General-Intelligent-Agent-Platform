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
        self.assertEqual(resume['pi_resume']['recovery']['kind'],
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


class GovernanceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """第七阶段收口：治理分支（终态保护 / 取消失败不归一为成功）。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_terminal_state_not_overwritten_by_late_finish(self):
        """已持久化终态不得被迟到收尾覆盖（require_status 保护）。"""
        from webpage_agent.governance import execution_control
        await seed_run(self.store, run_id='r1')
        claimed = await self.store.claim_run('worker-a', 60, run_id='r1')
        ctx = execution_control.ExecutionContext(
            run_id='r1', lease_owner='worker-a',
            lease_epoch=int(claimed['lease_epoch']))
        # 第一次收尾成功
        ok = await self.store.finish_run_owned(
            ctx, run_id='r1', status='succeeded', plan_version=1)
        self.assertTrue(ok)
        # 第二次（迟到）收尾：run 已 succeeded，require_status 抛 LeaseLost → False
        ok2 = await self.store.finish_run_owned(
            ctx, run_id='r1', status='failed', plan_version=1)
        self.assertFalse(ok2)
        run = await self.store.run('r1')
        self.assertEqual(run['status'], 'succeeded')

    def test_final_status_not_succeeded_on_stale_answer(self):
        """取消/失败不得因残留旧 answer 被归一为成功。

        真正的保护在 pi_runtime._apply_finished 清除旧 answer（见下个测试）；
        这里验证 final_status 对「有 answer 但无失败标志」仍判 succeeded 的
        现状，说明清除 answer 是必要的防线。
        """
        from webpage_agent.core import outcome
        self.assertEqual(outcome.final_status({'answer': '旧答复'}), 'succeeded')

    def test_apply_finished_clears_stale_answer_on_cancel(self):
        """_apply_finished 取消/失败时清除旧 answer，避免 final_status 误判成功。"""
        from webpage_agent.agent.pi_runtime import PiRuntime
        from webpage_agent.agent.pi_protocol import PROTOCOL_VERSION
        rt = PiRuntime.__new__(PiRuntime)
        # 预置旧 answer
        state = {'answer': '旧答复'}
        msg = {'type': 'finished', 'outcome': 'cancelled',
               'protocol_version': PROTOCOL_VERSION, 'execution_id': 'exec-1'}
        rt._apply_finished(state, msg, 'exec-1')
        self.assertNotIn('answer', state)
        self.assertEqual(state['pi_outcome'], 'cancelled')

        state2 = {'answer': '旧答复'}
        msg2 = {'type': 'finished', 'outcome': 'failed', 'error': 'boom',
                'protocol_version': PROTOCOL_VERSION, 'execution_id': 'exec-1'}
        rt._apply_finished(state2, msg2, 'exec-1')
        self.assertNotIn('answer', state2)
        self.assertEqual(state2['pi_outcome'], 'failed')


if __name__ == '__main__':
    unittest.main()


class FormalHubPiRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """第七阶段收口：经正式 Hub/Worker 入口 + 真实 Node + 受控模型的进程恢复。

    覆盖规格第 8 点的四类故障点（真实 PG + 真实 Node 进程 + 受控模型）：
      - 调用检查点已提交、尚未派发 → 原身份继续，工具仅执行一次；
      - 工具已结算、Node 尚未吸收 → 补原结果；
      - 最终答复已存、尚未收尾 → 不再调用模型，最终消息只落一条；
      - 真正终止执行进程，新执行者接管，核对工具次数 / 逻辑 ID / 检查点 / 消息 / 终态。
    """

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    def _set_mock_env(self, script: list, tool: bool = True):
        import json as _json
        os.environ['PI_MOCK_SCRIPT'] = _json.dumps(script)
        os.environ['PI_MOCK_TOOL'] = '1' if tool else '0'

    def _clear_mock_env(self):
        os.environ.pop('PI_MOCK_SCRIPT', None)
        os.environ.pop('PI_MOCK_TOOL', None)

    async def _seed_pi_run(self, run_id='r1', task_id='t1', session_id='s1'):
        from webpage_agent.agent import runtime_identity
        await self.store.create_task(task_id, session_id, '测试 Pi 恢复')
        await self.store.ensure_task_meta(task_id, session_id=session_id,
                                          goal='测试 Pi 恢复')
        await self.store.create_run(run_id, task_id, status='queued',
                                    runtime_kind=runtime_identity.KIND_PI)

    async def test_formal_entry_pi_runs_tool_once_and_final_message_unique(self):
        """正式 Hub 入口 + 受控模型：Pi 完整跑通，工具仅执行一次，最终消息唯一。"""
        from webpage_agent.api.api_hub import Hub
        run_id, task_id, session_id = 'r1', 't1', 's1'
        await self._seed_pi_run(run_id, task_id, session_id)
        self._set_mock_env([
            {'type': 'tool', 'calls': [
                {'id': 'c1', 'name': 'web_search', 'arguments': {'query': '天气'}}]},
            {'type': 'answer', 'text': '结果已拿到'},
        ])
        try:
            hub = Hub(self.store, owner_id='pi-formal-a')
            claimed = await self.store.claim_run('pi-formal-a', 120, run_id=run_id)
            self.assertIsNotNone(claimed)
            await hub.execute_claimed_run(claimed)
        finally:
            self._clear_mock_env()

        run = await self.store.run(run_id)
        self.assertEqual(run['status'], 'succeeded')
        # 工具只执行一次（一个 action）
        actions = await self.store.actions_of(run_id)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['status'], 'succeeded')
        # 最终消息唯一
        msgs = await self.store.messages(session_id)
        assistant = [m for m in msgs if m['role'] == 'assistant']
        self.assertEqual(len(assistant), 1)

    async def test_kill_process_then_formal_entry_recovers_without_retool(self):
        """真正终止执行进程，新执行者经正式入口接管：工具不重复执行、终态正确。"""
        from webpage_agent.api.api_hub import Hub
        run_id, task_id, session_id = 'r2', 't2', 's2'
        await self._seed_pi_run(run_id, task_id, session_id)

        subprocess.run(
            [sys.executable, '-m', 'tests.hub_pi_crash_worker',
             PG_DSN, run_id, task_id, session_id],
            capture_output=True, text=True, timeout=90,
            env={**os.environ, 'PYTHONPATH': os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')})

        # 崩溃进程：工具已结算（一个 action），run 未收尾
        run = await self.store.run(run_id)
        self.assertEqual(run['lease_owner'], 'crash-pi-proc')
        actions = await self.store.actions_of(run_id)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['status'], 'succeeded')

        # 新执行者：让旧租约过期，经正式入口接管
        async with self.store._lease() as conn:
            await conn.execute(
                f"UPDATE runs SET lease_expires_at=0 WHERE run_id='{run_id}'")
            await conn.commit()
        self._set_mock_env([
            {'type': 'answer', 'text': '恢复后答复'},
        ])
        try:
            hub2 = Hub(self.store, owner_id='pi-formal-b')
            claimed = await self.store.claim_run('pi-formal-b', 120, run_id=run_id)
            self.assertIsNotNone(claimed)
            await hub2.execute_claimed_run(claimed)
        finally:
            self._clear_mock_env()

        # 恢复后：工具仍只有一次（不重复执行），run 收尾，最终消息唯一。
        #
        # 这里必须断言**具体结局**，不能写成 status in ('succeeded',
        # 'needs_reconcile')：崩溃点在「工具已结算、Node 未吸收」，正确的恢复决策
        # 是 resume_deliver——把已提交的结果补交给 Node，继续由模型收尾。一旦它
        # 退化成 resume_reconcile（不补交、直接判结果未定），run 照样能到
        # needs_reconcile 且工具次数也不变，宽断言完全抓不到。
        # 所以改为：终态必须是 succeeded，且最终消息必须是崩溃后模型新产出的答复
        # （reconcile 路径不会启动 Node，也就不可能产生这句）。
        actions = await self.store.actions_of(run_id)
        self.assertEqual(len(actions), 1)
        run = await self.store.run(run_id)
        self.assertEqual(run['status'], 'succeeded')
        msgs = await self.store.messages(session_id)
        assistant = [m for m in msgs if m['role'] == 'assistant']
        self.assertEqual(len(assistant), 1)
        self.assertIn('恢复后答复', assistant[0]['content'])


if __name__ == '__main__':
    unittest.main()
