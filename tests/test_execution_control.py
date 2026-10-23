"""阶段 B/E：租约代次、暂停边界、取消与工具消失（文档 §4、§7、§8）。

这一组用例只关心一件事：**执行权是不是真的有效**。租约与代次不是装饰——
它们决定"这条 run 还归不归我推进"，决定暂停能不能在派发前生效，也决定
旧执行者会不会把新执行者刚推进的状态盖掉。所以断言都落在库里的行上，而不是
内存里的对象上。
"""
import asyncio
import tempfile
import unittest

from unittest.mock import patch

from app import execution_control
from tests.exec_env import (Remote, install_main, install_plan, install_tools,
                            new_store, one_step_plan, reset_db, seed_run)


class LeaseFencingTests(unittest.IsolatedAsyncioTestCase):
    """旧代次的执行者不能推进任何东西。"""

    async def asyncSetUp(self):
        await reset_db()

    def hub(self, store, **kwargs):
        from app.api_hub import Hub
        return Hub(store, **kwargs)

    async def test_stale_epoch_cannot_advance_the_run(self):
        store = new_store()
        await seed_run(store, status='queued')
        first = await store.claim_run('wA', 30.0)
        self.assertEqual(first['lease_epoch'], 1)
        old = execution_control.ExecutionContext.from_claim(first)

        # 租约过期后被 B 接手：代次 +1
        await store.update_run('run1', lease_expires_at=0)
        second = await store.claim_run('wB', 30.0)
        self.assertEqual(second['lease_epoch'], 2)

        # 旧执行者的四件事全部必须被拒
        with self.assertRaises(execution_control.LeaseLost):
            await store.admit_action(
                old, run_id='run1', plan_version=1, step_id='s1',
                tool_key='local:test:deploy', args_hash='h', idempotency_key='k1',
                action_id='a1', attempt_id='at1')
        with self.assertRaises(execution_control.LeaseLost):
            await store.settle_action(
                old, run_id='run1', plan_version=1, step_id='s1',
                action_id='a1', attempt_id='at1', status='succeeded')
        self.assertFalse(await store.finish_run_owned(old, run_id='run1',
                                                      status='succeeded'))
        self.assertFalse(await store.renew_lease('run1', 'wA', 30.0,
                                                 epoch=old.lease_epoch))
        # 释放也不能把 B 的租约清掉
        await store.release_lease('run1', 'wA', epoch=old.lease_epoch)
        run = await store.run('run1')
        self.assertEqual(run['lease_owner'], 'wB')
        self.assertEqual(run['lease_epoch'], 2)
        self.assertEqual(run['status'], 'running', '旧执行者不能改状态')
        await store.close()

    async def test_renewal_failure_stops_before_the_next_step(self):
        """续租失败 = 已经不该继续：停在这里，且**不写任何业务状态**。"""
        from app.tools import Tool

        store = new_store()
        await seed_run(store, status='queued')
        remote = Remote()
        second_calls = []

        async def slow(args, state):
            remote.calls.append(('first', dict(args)))
            await asyncio.sleep(1.4)          # 盖住一次续租周期
            return {'answer': '一步'}

        async def second(args, state):
            second_calls.append(1)
            return {'answer': '两步'}

        tools = {
            'first': Tool('first', '第一步', '', {}, slow,
                          tool_key='local:test:first', effect='write'),
            'second': Tool('second', '第二步', '', {}, second,
                           tool_key='local:test:second', effect='write'),
        }
        steps = [{'tool': 'first', 'step_id': 's1', 'args': {}},
                 {'tool': 'second', 'step_id': 's2', 'args': {}}]
        hub = self.hub(store, owner_id='w', lease_seconds=1.0)
        plan_patch, _ = install_plan(steps)

        async def no_renew(*args, **kwargs):
            return False

        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), plan_patch, \
                patch.object(store, 'renew_lease', no_renew):
            claimed = await store.claim_run('w', 1.0)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(hub.execute_claimed_run(claimed), timeout=20)

        self.assertEqual(second_calls, [], '失租后不能再派发下一步')
        run = await store.run('run1')
        self.assertEqual(run['status'], 'running',
                         '失租的执行者不能把状态写成功或失败')
        await store.close()


class PauseBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """暂停必须作用在**安全边界**上（文档 §7.1、§7.2）。"""

    async def asyncSetUp(self):
        await reset_db()

    def hub(self, store, **kwargs):
        from app.api_hub import Hub
        return Hub(store, **kwargs)

    async def test_pause_committed_before_admission_blocks_the_call(self):
        import scripts.run_worker as worker

        store = new_store()
        await seed_run(store, status='running')
        await store.update_run('run1', pause_requested=1)
        remote = Remote()
        hub = self.hub(store, owner_id='w1')
        plan_patch, _ = install_plan(one_step_plan())
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'deploy': remote.tool('deploy')}), plan_patch:
            await worker.run_once(hub, 'w1', run_id='run1')

        self.assertEqual(remote.calls, [], '暂停先提交时这一步不能获得调用资格')
        run = await store.run('run1')
        self.assertEqual(run['status'], 'paused')
        self.assertIsNone(run['lease_owner'], '停下时要交还租约')
        await store.close()

    async def test_pause_after_admission_only_finishes_the_current_step(self):
        """准入之后的暂停：只允许该步收尾，后续不再派发。"""
        import scripts.run_worker as worker

        store = new_store()
        await seed_run(store)
        remote = Remote()
        tools = {'first': remote.tool('first', tool_key='local:test:first'),
                 'second': remote.tool('second', tool_key='local:test:second')}
        steps = [{'tool': 'first', 'step_id': 's1', 'args': {}},
                 {'tool': 'second', 'step_id': 's2', 'args': {}}]
        hub = self.hub(store, owner_id='w1')
        plan_patch, _ = install_plan(steps)

        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), plan_patch:
            job = asyncio.create_task(worker.run_once(hub, 'w1', run_id='run1'))
            await asyncio.wait_for(remote.enter('first').wait(), timeout=10)
            paused = await hub.pause('run1')
            self.assertTrue(paused['ok'])
            remote.gate('first').set()
            await asyncio.wait_for(job, timeout=20)

        self.assertEqual(remote.name_count('first'), 1, '已准入的这一步要收尾')
        self.assertEqual(remote.name_count('second'), 0, '暂停后不再派发下一步')
        self.assertEqual((await store.run('run1'))['status'], 'paused')
        # 暂停快照必须是**可恢复**的：游标停在下一步，plan 也在里面
        snapshot = await store.run_snapshot('run1')
        self.assertEqual(snapshot['next_step_id'], 's2')
        self.assertEqual(snapshot['state']['step_index'], 1)
        self.assertEqual([s['step_id'] for s in snapshot['state']['plan']['steps']],
                         ['s1', 's2'])
        await store.close()

    async def test_pausing_a_queued_run_keeps_the_worker_away(self):
        import scripts.run_worker as worker

        store = new_store()
        await seed_run(store, status='queued')
        hub = self.hub(store, owner_id='w1')
        self.assertEqual((await hub.pause('run1'))['status'], 'paused')

        remote = Remote()
        plan_patch, _ = install_plan(one_step_plan())
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'deploy': remote.tool('deploy')}), plan_patch:
            worked = await worker.run_once(hub, 'w1')

        self.assertFalse(worked, '暂停中的 run 不该被领取')
        self.assertEqual(remote.calls, [])
        self.assertEqual((await store.run('run1'))['status'], 'paused')
        await store.close()

    async def test_resume_after_pause_continues_from_the_saved_plan(self):
        """暂停→续跑：沿用已提交计划，已成功的那一步不重发。"""
        import scripts.run_worker as worker

        store = new_store()
        await seed_run(store)
        remote = Remote()
        tools = {'first': remote.tool('first', tool_key='local:test:first'),
                 'second': remote.tool('second', tool_key='local:test:second')}
        steps = [{'tool': 'first', 'step_id': 's1', 'args': {}},
                 {'tool': 'second', 'step_id': 's2', 'args': {}}]
        hub = self.hub(store, owner_id='w1')
        plan_patch, plan_calls = install_plan(steps)

        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), plan_patch:
            job = asyncio.create_task(worker.run_once(hub, 'w1', run_id='run1'))
            await asyncio.wait_for(remote.enter('first').wait(), timeout=10)
            await hub.pause('run1')
            remote.gate('first').set()
            await asyncio.wait_for(job, timeout=20)
            self.assertEqual((await store.run('run1'))['status'], 'paused')

            resumed = await hub.resume('run1')
            self.assertIsNotNone(resumed, '暂停的 run 必须能续跑')
            await asyncio.wait_for(resumed._task, timeout=20)

        self.assertEqual(plan_calls, ['做个页面', '沿用已保存的计划'],
                         '续跑必须沿用库里的计划，不能再规划一次')
        self.assertEqual(remote.name_count('first'), 1, '已成功的步骤不能重发')
        self.assertEqual(remote.name_count('second'), 1)
        self.assertEqual((await store.run('run1'))['status'], 'succeeded')
        await store.close()

    async def test_cancel_request_stops_the_dispatch(self):
        import scripts.run_worker as worker
        from app.pg_store import now

        store = new_store()
        await seed_run(store, status='running')
        await store.update_run('run1', cancel_requested_at=now())
        remote = Remote()
        hub = self.hub(store, owner_id='w1')
        plan_patch, _ = install_plan(one_step_plan())
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'deploy': remote.tool('deploy')}), plan_patch:
            await worker.run_once(hub, 'w1', run_id='run1')

        self.assertEqual(remote.calls, [])
        self.assertEqual((await store.run('run1'))['status'], 'cancelled')
        await store.close()

    async def test_missing_tool_fails_instead_of_silently_skipping(self):
        """工具在计划之后消失：依赖它的步骤不能"假装成功"。"""
        import scripts.run_worker as worker

        store = new_store()
        await seed_run(store)
        remote = Remote()
        steps = [{'tool': 'ghost', 'step_id': 's1', 'args': {},
                  'depends_on': []},
                 {'tool': 'second', 'step_id': 's2', 'args': {},
                  'depends_on': ['s1']}]
        hub = self.hub(store, owner_id='w1')
        plan_patch, _ = install_plan(steps)
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'second': remote.tool('second')}), plan_patch:
            await worker.run_once(hub, 'w1', run_id='run1')

        self.assertEqual(remote.calls, [], '后续步骤不能执行')
        run = await store.run('run1')
        self.assertEqual(run['status'], 'failed')
        self.assertIn('没有执行', run.get('error') or '', '失败原因要说清后果')
        await store.close()

    async def test_pause_api_reports_the_run_it_actually_touched(self):
        store = new_store()
        await seed_run(store, status='queued')
        hub = self.hub(store, owner_id='w1')
        self.assertEqual(await hub.pause('nope'), {'ok': False, 'status': '',
                                                   'pause_requested': False})
        await store.update_run('run1', status='succeeded')
        settled = await hub.pause('run1')
        self.assertFalse(settled['ok'], '已结束的 run 不能被暂停')
        self.assertEqual(settled['status'], 'succeeded')
        await store.close()


if __name__ == '__main__':
    unittest.main()
