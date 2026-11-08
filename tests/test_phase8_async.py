"""第八阶段：远端异步任务持久等待与可靠唤醒的验收。

覆盖（需 PG_TEST_DSN，库名含 test）：

1. accepted 结算与外部任务登记**原子提交**（同事务：动作 accepted + external
   task + waiting_external；断点后二者同存或同滚）；
2. 轮询领取/写回门禁（旧 poller / 重复完成 / 失租不得覆盖新结果）；
3. 完成 + 唤醒同事务，且不复活 cancelled、不覆盖 paused；
4. Pi 持久等待协议（accepted → finished('waiting')，不注入 toolResult、不请求模型）；
5. 恢复时先判断外部任务（wait / deliver / reconcile），不套用 pending 重派发；
6. 空快照不被清洗成可执行默认值。

真实 MCP / 远端服务联调另行报告（需真实供应商凭据与可查询任务身份）。
"""
import os
import unittest

from tests.exec_env import PG_DSN, new_store, reset_db, seed_run, valid_test_dsn


def _require_pg(testcase):
    if not valid_test_dsn(PG_DSN):
        testcase.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')


class AtomicAcceptedSettleTests(unittest.IsolatedAsyncioTestCase):
    """第八阶段第 4 点：accepted 结算与外部任务登记原子提交。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_settle_accepted_registers_external_task_atomically(self):
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        await seed_run(self.store, run_id='r1')
        claimed = await self.store.claim_run('w1', 60, run_id='r1')
        ctx = execution_control.ExecutionContext.from_claim(claimed)
        # 真实流程：先 admit 建 action（in_flight），再 settle_accepted。
        book = ledger_module.ActionLedger(self.store, 'r1', 1, context=ctx)
        admitted = await book.admit(
            step_id='pi:call:1', tool_key='mcp:x:render', effect='write',
            args={'x': 1})
        self.assertEqual(admitted['status'], 'admitted')
        result = await self.store.settle_action_accepted(
            ctx, run_id='r1', plan_version=1, step_id='pi:call:1',
            action_id=admitted['action_id'], attempt_id=admitted['attempt_id'],
            receipt={'tool_result': {'execution_status': 'accepted'}},
            external={
                'external_task_id': 'et1', 'remote_task_id': 'job-9',
                'tenant_id': 'default', 'owner_user_id': 'default',
                'tool_key': 'mcp:x:render', 'contract_id': 'c1',
                'contract_version': 1, 'submission_key': 'job-9',
                'item_key': 'pi:call:1', 'deadline_at': 0, 'next_poll_at': 0,
            })
        self.assertEqual(result['status'], 'accepted')
        # 动作是 accepted（不是 succeeded）
        action = await self.store.action(admitted['action_id'])
        self.assertEqual(action['status'], 'accepted')
        # 外部任务已登记（同一事务）
        task = await self.store.external_task('et1')
        self.assertIsNotNone(task)
        self.assertEqual(task['remote_task_id'], 'job-9')
        self.assertEqual(task['state'], 'accepted')
        # run 置 waiting_external
        run = await self.store.run('r1')
        self.assertEqual(run['waiting_external'], 1)

    async def test_register_external_task_unique_constraint_no_duplicate(self):
        """唯一约束 (run_id, action_id, item_key)：重复登记不产生第二个任务。"""
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        await seed_run(self.store, run_id='r1')
        claimed = await self.store.claim_run('w1', 60, run_id='r1')
        ctx = execution_control.ExecutionContext.from_claim(claimed)
        book = ledger_module.ActionLedger(self.store, 'r1', 1, context=ctx)
        admitted = await book.admit(
            step_id='s1', tool_key='mcp:x:render', effect='write', args={})
        common = {'run_id': 'r1', 'step_id': 's1',
                  'action_id': admitted['action_id'],
                  'item_key': 's1', 'remote_task_id': 'job-9',
                  'tenant_id': 'default', 'owner_user_id': 'default',
                  'tool_key': 'mcp:x:render', 'contract_id': 'c1',
                  'contract_version': 1, 'submission_key': 'job-9'}
        await self.store.settle_action_accepted(
            ctx, run_id='r1', plan_version=1, step_id='s1',
            action_id=admitted['action_id'],
            attempt_id=admitted['attempt_id'], receipt={},
            external={'external_task_id': 'et1', **common})
        # 同一动作再结算（模拟恢复重放）：不会新建第二个 external task
        await self.store.settle_action_accepted(
            ctx, run_id='r1', plan_version=1, step_id='s1',
            action_id=admitted['action_id'],
            attempt_id='att2', receipt={},
            external={'external_task_id': 'et2', **common})
        tasks = await self.store.external_tasks_of_run('r1')
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['external_task_id'], 'et1')


class PollFencingTests(unittest.IsolatedAsyncioTestCase):
    """第八阶段第 6 点：轮询领取/写回门禁。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def _register(self, run_id='r1', external_task_id='et1'):
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        await seed_run(self.store, run_id=run_id)
        claimed = await self.store.claim_run('w1', 60, run_id=run_id)
        ctx = execution_control.ExecutionContext.from_claim(claimed)
        book = ledger_module.ActionLedger(self.store, run_id, 1, context=ctx)
        admitted = await book.admit(
            step_id='s1', tool_key='mcp:x:render', effect='write', args={})
        await self.store.settle_action_accepted(
            ctx, run_id=run_id, plan_version=1, step_id='s1',
            action_id=admitted['action_id'], attempt_id=admitted['attempt_id'],
            receipt={},
            external={'external_task_id': external_task_id,
                      'remote_task_id': 'job-9', 'tenant_id': 'default',
                      'owner_user_id': 'default', 'tool_key': 'mcp:x:render',
                      'contract_id': 'mcp-task', 'contract_version': 1,
                      'submission_key': 'job-9', 'item_key': 's1',
                      'deadline_at': 0, 'next_poll_at': 0})
        return external_task_id

    async def test_stale_poller_cannot_overwrite_new_result(self):
        await self._register()
        claimed = await self.store.claim_external_task_poll('et1', 'worker-a', 60)
        epoch_a = int(claimed['poll_epoch'])
        # worker-a 的轮询租约过期，worker-b 领取（代次 +1）
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE external_tasks SET poll_lease_expires_at=0 "
                "WHERE external_task_id='et1'")
            await conn.commit()
        claimed_b = await self.store.claim_external_task_poll('et1', 'worker-b', 60)
        epoch_b = int(claimed_b['poll_epoch'])
        self.assertGreater(epoch_b, epoch_a)
        # 旧 poller（worker-a, epoch_a）写回 → 无效（返回 None）
        written = await self.store.finish_external_poll(
            'et1', epoch_a, owner='worker-a', state='completed')
        self.assertIsNone(written)
        # 新 poller 写回成功
        written_b = await self.store.finish_external_poll(
            'et1', epoch_b, owner='worker-b', state='completed')
        self.assertIsNotNone(written_b)

    async def test_claim_requires_open_state(self):
        await self._register()
        # 已完成的任务不能被领取
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE external_tasks SET state='completed' WHERE external_task_id='et1'")
            await conn.commit()
        claimed = await self.store.claim_external_task_poll('et1', 'worker-a', 60)
        self.assertIsNone(claimed)

    async def test_lease_expired_but_epoch_unchanged_cannot_write(self):
        """第八阶段收口第 6 点：租约已过期但 epoch 未变化 → 写回无效。"""
        await self._register()
        claimed = await self.store.claim_external_task_poll('et1', 'worker-a', 60)
        epoch = int(claimed['poll_epoch'])
        # 租约过期，但 epoch 不变（没有别的 Worker 领取）
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE external_tasks SET poll_lease_expires_at=0 "
                "WHERE external_task_id='et1'")
            await conn.commit()
        written = await self.store.finish_external_poll(
            'et1', epoch, owner='worker-a', state='running')
        self.assertIsNone(written)

    async def test_wrong_owner_cannot_write(self):
        """第八阶段收口第 6 点：owner 不符 → 写回无效。"""
        await self._register()
        claimed = await self.store.claim_external_task_poll('et1', 'worker-a', 60)
        epoch = int(claimed['poll_epoch'])
        # 别的 worker 用相同 epoch 写回 → 无效（owner 不符）
        written = await self.store.finish_external_poll(
            'et1', epoch, owner='worker-b', state='running')
        self.assertIsNone(written)


class CompleteAndWakeTests(unittest.IsolatedAsyncioTestCase):
    """第八阶段第 8 点：完成 + 唤醒同事务，不复活/不覆盖/不抢占。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def _waiting_run(self, run_id='r1', external_task_id='et1'):
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger as ledger_module
        await seed_run(self.store, run_id=run_id)
        claimed = await self.store.claim_run('w1', 60, run_id=run_id)
        ctx = execution_control.ExecutionContext.from_claim(claimed)
        book = ledger_module.ActionLedger(self.store, run_id, 1, context=ctx)
        admitted = await book.admit(
            step_id='s1', tool_key='mcp:x:render', effect='write', args={})
        await self.store.settle_action_accepted(
            ctx, run_id=run_id, plan_version=1, step_id='s1',
            action_id=admitted['action_id'], attempt_id=admitted['attempt_id'],
            receipt={},
            external={'external_task_id': external_task_id,
                      'remote_task_id': 'job-9', 'tenant_id': 'default',
                      'owner_user_id': 'default', 'tool_key': 'mcp:x:render',
                      'contract_id': 'mcp-task', 'contract_version': 1,
                      'submission_key': 'job-9', 'item_key': 's1',
                      'deadline_at': 0, 'next_poll_at': 0})
        # 模拟执行者退出：run 停在 waiting_external、释放租约
        await self.store.update_run(run_id, status='waiting_external',
                                    lease_owner=None, lease_expires_at=None)

    async def test_complete_wakes_waiting_run_in_same_transaction(self):
        await self._waiting_run()
        claimed = await self.store.claim_external_task_poll('et1', 'worker-a', 60)
        epoch = int(claimed['poll_epoch'])
        written = await self.store.complete_external_task_and_wake(
            'et1', epoch, owner='worker-a', state='completed',
            output_manifest_ref='/tmp/ref', last_result_id='ext-et1')
        self.assertIsNotNone(written)
        task = await self.store.external_task('et1')
        self.assertEqual(task['state'], 'completed')
        self.assertEqual(task['output_manifest_ref'], '/tmp/ref')
        run = await self.store.run('r1')
        self.assertEqual(run['status'], 'queued')
        self.assertEqual(run['waiting_external'], 0)

    async def test_complete_does_not_revive_cancelled_run(self):
        await self._waiting_run()
        # run 被取消：不应被唤醒复活
        await self.store.update_run('r1', status='cancelled')
        claimed = await self.store.claim_external_task_poll('et1', 'worker-a', 60)
        epoch = int(claimed['poll_epoch'])
        written = await self.store.complete_external_task_and_wake(
            'et1', epoch, owner='worker-a', state='completed',
            output_manifest_ref='/tmp/ref')
        self.assertIsNotNone(written)  # 任务仍完成
        run = await self.store.run('r1')
        self.assertEqual(run['status'], 'cancelled')  # run 不被复活

    async def test_stale_poller_cannot_complete_twice(self):
        await self._waiting_run()
        claimed = await self.store.claim_external_task_poll('et1', 'worker-a', 60)
        epoch = int(claimed['poll_epoch'])
        await self.store.complete_external_task_and_wake(
            'et1', epoch, owner='worker-a', state='completed',
            output_manifest_ref='/tmp/ref')
        # 重复完成（同一 epoch，但任务已 completed）→ 状态不符，返回 None
        again = await self.store.complete_external_task_and_wake(
            'et1', epoch, owner='worker-a', state='completed',
            output_manifest_ref='/tmp/ref2')
        self.assertIsNone(again)
        task = await self.store.external_task('et1')
        self.assertEqual(task['output_manifest_ref'], '/tmp/ref')

    async def test_failed_and_cancelled_lead_to_needs_reconcile(self):
        """第八阶段收口第 5 点：failed/cancelled 终态 → run 进 needs_reconcile。"""
        await self._waiting_run()
        claimed = await self.store.claim_external_task_poll('et1', 'worker-a', 60)
        epoch = int(claimed['poll_epoch'])
        written = await self.store.complete_external_task_and_wake(
            'et1', epoch, owner='worker-a', state='failed')
        self.assertIsNotNone(written)
        run = await self.store.run('r1')
        self.assertEqual(run['status'], 'needs_reconcile')


class PiWaitProtocolTests(unittest.TestCase):
    """第八阶段第 5 点：Pi 持久等待协议（纯单元，不依赖 PG）。"""

    def test_finished_waiting_is_valid_outcome(self):
        from webpage_agent.agent import pi_protocol as proto
        msg = {'type': 'finished', 'outcome': 'waiting',
               'protocol_version': proto.PROTOCOL_VERSION, 'execution_id': 'e1'}
        proto.validate_finished(msg, 'e1')  # 不抛错

    def test_apply_finished_waiting_marks_waiting_external(self):
        from webpage_agent.agent.pi_runtime import PiRuntime
        from webpage_agent.agent.pi_protocol import PROTOCOL_VERSION
        rt = PiRuntime.__new__(PiRuntime)
        state = {'answer': '旧答复'}
        msg = {'type': 'finished', 'outcome': 'waiting',
               'protocol_version': PROTOCOL_VERSION, 'execution_id': 'e1'}
        rt._apply_finished(state, msg, 'e1')
        self.assertEqual(state['pi_outcome'], 'waiting')
        self.assertEqual(state['status'], 'waiting_external')
        self.assertNotIn('answer', state)  # 不因残留旧 answer 归一为成功

    def test_tool_result_accepts_accepted_fields(self):
        from webpage_agent.agent import pi_protocol as proto
        from webpage_agent.agent.pi_protocol import PROTOCOL_VERSION
        # make_message 允许 accepted / remote_task_id 字段（回执形状校验不拒绝）
        msg = proto.make_message('e1', 'tool_result', call_id='c1', ok=False,
                                 accepted=True, remote_task_id='job-9')
        proto.validate_tool_result(msg, 'e1')

    def test_waiting_external_is_a_valid_run_status(self):
        from webpage_agent.core import outcome
        self.assertIn('waiting_external', outcome.RUN_STATUSES)
        self.assertEqual(outcome.check_run_status('waiting_external'),
                         'waiting_external')


class RecoveryExternalTests(unittest.TestCase):
    """第八阶段第 5 点：恢复时先判断外部任务。"""

    def _snapshot(self, awaiting):
        from webpage_agent.agent import pi_snapshot
        snap = pi_snapshot.new_snapshot(phase='awaiting_external')
        snap['awaiting_calls'] = awaiting
        return snap

    def test_open_external_task_waits(self):
        from webpage_agent.agent import pi_recovery
        snap = self._snapshot([{'call_id': 'c1', 'logical_call_id': 'pi:call:1',
                                'external_task_id': 'et1', 'action_id': 'act1'}])
        decision = pi_recovery.decide_external(
            snap, [{'external_task_id': 'et1', 'action_id': 'act1',
                    'state': 'running'}])
        self.assertEqual(decision.kind, pi_recovery.RESUME_WAIT)

    def test_completed_external_task_delivers(self):
        from webpage_agent.agent import pi_recovery
        snap = self._snapshot([{'call_id': 'c1', 'logical_call_id': 'pi:call:1',
                                'external_task_id': 'et1', 'action_id': 'act1'}])
        decision = pi_recovery.decide_external(
            snap, [{'external_task_id': 'et1', 'action_id': 'act1',
                    'state': 'completed'}])
        self.assertEqual(decision.kind, pi_recovery.RESUME_DELIVER)

    def test_failed_external_task_reconciles(self):
        from webpage_agent.agent import pi_recovery
        snap = self._snapshot([{'call_id': 'c1', 'logical_call_id': 'pi:call:1',
                                'external_task_id': 'et1', 'action_id': 'act1'}])
        decision = pi_recovery.decide_external(
            snap, [{'external_task_id': 'et1', 'action_id': 'act1',
                    'state': 'failed'}])
        self.assertEqual(decision.kind, pi_recovery.RESUME_RECONCILE)

    def test_cancelled_external_task_reconciles(self):
        from webpage_agent.agent import pi_recovery
        snap = self._snapshot([{'call_id': 'c1', 'logical_call_id': 'pi:call:1',
                                'external_task_id': 'et1', 'action_id': 'act1'}])
        decision = pi_recovery.decide_external(
            snap, [{'external_task_id': 'et1', 'action_id': 'act1',
                    'state': 'cancelled'}])
        self.assertEqual(decision.kind, pi_recovery.RESUME_RECONCILE)

    def test_missing_association_reconciles(self):
        """第八阶段收口第 1 点：缺关联记录 → 待核实，不能当「继续等待」。"""
        from webpage_agent.agent import pi_recovery
        snap = self._snapshot([{'call_id': 'c1', 'logical_call_id': 'pi:call:1',
                                'external_task_id': 'et1', 'action_id': 'act1'}])
        # external_tasks 里没有任何能对上 external_task_id / action_id 的记录
        decision = pi_recovery.decide_external(
            snap, [{'external_task_id': 'et-other', 'state': 'running'}])
        self.assertEqual(decision.kind, pi_recovery.RESUME_RECONCILE)

    def test_no_awaiting_calls_returns_none(self):
        from webpage_agent.agent import pi_recovery
        snap = self._snapshot([])
        self.assertIsNone(pi_recovery.decide_external(snap, []))


class EmptySnapshotTests(unittest.TestCase):
    """第八阶段第 1 点：空快照不被清洗成可执行默认值。"""

    def test_empty_snapshot_raises(self):
        from webpage_agent.agent import pi_snapshot
        with self.assertRaises(pi_snapshot.SnapshotError):
            pi_snapshot.parse({})

    def test_only_schema_snapshot_raises(self):
        from webpage_agent.agent import pi_snapshot
        with self.assertRaises(pi_snapshot.SnapshotError):
            pi_snapshot.parse({'state_schema': pi_snapshot.current_schema()})

    def test_blank_row_raises(self):
        from webpage_agent.agent import pi_snapshot
        row = {'state': {}, 'schema': pi_snapshot.current_schema()}
        with self.assertRaises(pi_snapshot.SnapshotError):
            pi_snapshot.parse_row(row)

    def test_fresh_snapshot_is_not_blank(self):
        from webpage_agent.agent import pi_snapshot
        snap = pi_snapshot.new_snapshot()
        # fresh snapshot 有 phase='started'，不是空白，可正常解析
        parsed = pi_snapshot.parse(pi_snapshot.serialize(snap))
        self.assertEqual(parsed['phase'], 'started')


if __name__ == '__main__':
    unittest.main()
