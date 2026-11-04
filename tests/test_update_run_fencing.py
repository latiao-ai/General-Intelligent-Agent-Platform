"""`update_run` 的 fencing：失去执行权的旧执行者不能覆盖接管者的进度。

背景：`update_run_owned` 一直存在（`claim_run` 里用 `FOR UPDATE SKIP LOCKED` +
`lease_epoch`），但全仓有多处直接调 `update_run` 改 status，其中一部分是**执行者
自己的收尾**。那些地方等于绕开了代次门禁：一个租约已经过期的执行者仍能以"最新
状态"写入，把接管者刚推进的进度改回去。

本组用例只测两件事：旧代次写不进去（执行者路径），以及管理动作**仍然**写得进去
（否则运维取消会失效——那是另一个方向的 bug）。
"""
import unittest

from webpage_agent.governance import execution_control
from tests.exec_env import new_store, reset_db, seed_run


class FencedWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        await seed_run(self.store, task_id='task1', run_id='run1')

    async def asyncTearDown(self):
        await self.store.close()

    async def _claim(self, worker: str, lease_seconds: float = 60.0) -> dict:
        return await self.store.claim_run(worker, lease_seconds, run_id='run1')

    async def test_stale_epoch_cannot_overwrite_status(self):
        """旧代次的执行者写终态必须被拒，接管者的状态原样保留。"""
        first = await self._claim('worker-a')
        # 租约过期 → 另一个 Worker 接管，epoch 递增
        await self.store.update_run('run1', lease_expires_at=0)
        second = await self._claim('worker-b')
        self.assertGreater(second['lease_epoch'], first['lease_epoch'])

        written = await self.store.update_run_owned(
            'run1', lease_owner=first['lease_owner'],
            lease_epoch=first['lease_epoch'],
            status='failed', error='旧执行者以为失败了')
        self.assertFalse(written)

        row = await self.store.run('run1')
        self.assertEqual(row['status'], 'running')      # 接管者领走时写的是 running
        self.assertNotEqual(row['error'], '旧执行者以为失败了')
        self.assertEqual(row['lease_owner'], 'worker-b')

    async def test_current_epoch_can_write(self):
        """同代次（真正的执行者）必须写得进去，否则门禁就成了阻断。"""
        claimed = await self._claim('worker-a')
        written = await self.store.update_run_owned(
            'run1', lease_owner=claimed['lease_owner'],
            lease_epoch=claimed['lease_epoch'],
            status='succeeded', error='')
        self.assertTrue(written)
        self.assertEqual((await self.store.run('run1'))['status'], 'succeeded')

    async def test_stale_executor_cannot_register_waiting_external(self):
        """`remember_waiting` 那条路：旧执行者不能把接管者的 waiting_external 改回 1。"""
        first = await self._claim('worker-a')
        await self.store.update_run('run1', lease_expires_at=0)
        second = await self._claim('worker-b')
        # 接管者已经把外部任务等完了
        await self.store.update_run_owned(
            'run1', lease_owner=second['lease_owner'],
            lease_epoch=second['lease_epoch'], waiting_external=0)

        await self.store.update_run_owned(
            'run1', lease_owner=first['lease_owner'],
            lease_epoch=first['lease_epoch'], waiting_external=1)
        self.assertEqual((await self.store.run('run1'))['waiting_external'], 0)

    async def test_requeue_only_works_for_the_owner_who_claimed(self):
        """补领失败要把 run 放回队列——但只能放回自己领到的那条。"""
        claimed = await self._claim('worker-a')
        # 别人接手之后，worker-a 的 requeue 必须落空
        await self.store.update_run('run1', lease_expires_at=0)
        await self._claim('worker-b')
        stale = await self.store.requeue_run_owned(
            'run1', lease_owner=claimed['lease_owner'],
            lease_epoch=claimed['lease_epoch'])
        self.assertFalse(stale)
        row = await self.store.run('run1')
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['lease_owner'], 'worker-b')

        # 真正的持有者能把自己的 run 放回队列，并释放租约
        await self.store.update_run('run1', lease_expires_at=0)
        owner = await self.store.claim_run('worker-c', 60.0, run_id='run1')
        ok = await self.store.requeue_run_owned(
            'run1', lease_owner=owner['lease_owner'],
            lease_epoch=owner['lease_epoch'])
        self.assertTrue(ok)
        row = await self.store.run('run1')
        self.assertEqual(row['status'], 'queued')
        self.assertIsNone(row['lease_owner'])

    async def test_management_cancel_still_works_without_any_lease(self):
        """**反向护栏**：管理动作（运维取消）没有租约也必须生效。

        把管理动作也改成 `_owned` 会让取消永远落空——这条用例就是盯这个回归的。
        """
        hub_cancel = self.store.update_run      # 管理路径用的正是这个
        await hub_cancel('run1', cancel_requested_at='now')
        self.assertEqual((await self.store.run('run1'))['cancel_requested_at'], 'now')

    async def test_management_pause_of_a_queued_run_works(self):
        await self.store.update_run('run1', status='paused', pause_requested=1)
        row = await self.store.run('run1')
        self.assertEqual(row['status'], 'paused')
        self.assertEqual(row['pause_requested'], 1)

    async def test_lease_lost_is_raised_for_a_stale_context(self):
        """`_owned` 的语义没变：失租抛异常，不给调用方"当作成功继续"的机会。"""
        first = await self._claim('worker-a')
        await self.store.update_run('run1', lease_expires_at=0)
        await self._claim('worker-b')
        stale = execution_control.ExecutionContext(
            run_id='run1', lease_owner=first['lease_owner'],
            lease_epoch=int(first['lease_epoch']))
        with self.assertRaises(execution_control.LeaseLost):
            async with self.store._lease() as conn:
                await self.store._owned(conn, stale, run_id='run1')


class ExecutorCallSiteTests(unittest.IsolatedAsyncioTestCase):
    """盯**调用点**而不是盯仓储方法：证明执行入口的收尾走了带门禁的那条路。

    只测 `update_run_owned` 本身是不够的——它一直是对的，出问题的是没人调用它。
    所以这里走真实的 `execute_claimed_run`，让"任务行不存在"这条早退分支去写终态，
    并让旧执行者带着过期代次来写。
    """

    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        await seed_run(self.store, task_id='task1', run_id='run1')

    async def asyncTearDown(self):
        await self.store.close()

    def _hub(self):
        from webpage_agent.api.api_hub import Hub
        return Hub(self.store)

    async def test_executor_early_exit_cannot_overwrite_a_taken_over_run(self):
        first = await self.store.claim_run('worker-a', 60.0, run_id='run1')
        # 让这条 run 被别人接手（代次 +1），并把任务行删掉，逼执行入口走早退分支
        await self.store.update_run('run1', lease_expires_at=0)
        await self.store.claim_run('worker-b', 60.0, run_id='run1')
        async with self.store._lease() as conn:
            await conn.execute("DELETE FROM tasks WHERE task_id='task1'")

        result = await self._hub().execute_claimed_run(dict(first))
        self.assertIsNone(result)

        row = await self.store.run('run1')
        self.assertEqual(row['status'], 'running',
                         '旧执行者的收尾不能把接管者的 run 改成 failed')
        self.assertEqual(row['lease_owner'], 'worker-b')
        self.assertIsNone(row['error'])
