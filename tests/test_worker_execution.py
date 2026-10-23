"""阶段 A：Worker 真的领取并执行（文档 §3、§8 的前四行）。

这一组用例的存在理由很具体：上一版的 Worker 有三个会让它**完全跑不起来**的
问题——漏 await（把协程当值传下去）、初始化异常在 try 之外（一次失败带走整个
循环）、`{'steps': []} or None`（空计划被当成有计划的续跑）。所以这里的断言
不看"函数被调用了"，而是看**远端调用次数**、**库里的真实状态**与**执行上下文
的真实取值**。
"""
import asyncio
import tempfile
import unittest

from unittest.mock import patch

from tests.exec_env import (Remote, install_main, install_plan, install_tools,
                            new_store, one_step_plan, reset_db, seed_run)


class WorkerExecutionTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await reset_db()

    def hub(self, store, **kwargs):
        from app.api_hub import Hub
        return Hub(store, **kwargs)

    async def test_worker_claims_and_executes_a_run_once(self):
        """领取 → 执行 → 收尾：远端只被调一次，上下文是库里的真实值。"""
        import scripts.run_worker as worker
        from app import execution_control

        store = new_store()
        await seed_run(store)
        remote = Remote()
        seen = {}

        async def spy(args, state):
            remote.calls.append(('deploy', dict(args)))
            context = execution_control.require()
            seen['run_id'] = context.run_id
            seen['owner'] = context.lease_owner
            seen['epoch'] = context.lease_epoch
            return {'answer': '已部署'}

        from app.tools import Tool
        tool = Tool('deploy', '部署', '', {'type': 'object'}, spy,
                    tool_key='local:test:deploy', effect='write')
        plan_patch, plan_calls = install_plan(one_step_plan())
        hub = self.hub(store, owner_id='w1')
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'deploy': tool}), plan_patch:
            worked = await worker.run_once(hub, 'w1')

        self.assertTrue(worked, 'worker 必须真的领到并执行了这条 run')
        self.assertEqual(remote.name_count('deploy'), 1)
        self.assertEqual(plan_calls, ['做个页面'], '没有计划时应当规划一次')

        run = await store.run('run1')
        self.assertEqual(run['status'], 'succeeded')
        self.assertEqual(run['lease_owner'], None, '跑完必须交还租约')
        # 上下文来自领取结果，不是随手拼的默认值
        self.assertEqual(seen['run_id'], 'run1')
        self.assertEqual(seen['owner'], 'w1')
        self.assertGreaterEqual(seen['epoch'], 1)
        await store.close()

    async def test_two_workers_claiming_at_once_execute_once(self):
        """两个执行者抢同一条 run：只有一方拿到调用资格。"""
        import scripts.run_worker as worker
        from app.tools import Tool

        store_a, store_b = new_store(), new_store()
        await seed_run(store_a)
        remote = Remote()

        async def slow(args, state):
            remote.calls.append(('deploy', dict(args)))
            await asyncio.sleep(0.2)
            return {'answer': '已部署'}

        tool = Tool('deploy', '部署', '', {'type': 'object'}, slow,
                    tool_key='local:test:deploy', effect='write')
        hub_a = self.hub(store_a, owner_id='wA')
        hub_b = self.hub(store_b, owner_id='wB')
        plan_patch, _ = install_plan(one_step_plan())
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'deploy': tool}), plan_patch:
            results = await asyncio.gather(worker.run_once(hub_a, 'wA'),
                                           worker.run_once(hub_b, 'wB'))

        self.assertEqual(sorted(results), [False, True], '只能有一个领取者')
        self.assertEqual(remote.name_count('deploy'), 1,
                         '同一动作只能获得一次有效准入')
        await store_a.close()
        await store_b.close()

    async def test_execution_error_does_not_kill_the_worker(self):
        """一次执行抛异常只影响这一条 run，Worker 还活着且能领下一条。"""
        import scripts.run_worker as worker
        from app.tools import Tool
        from app.tools import Tool

        store = new_store()
        await seed_run(store, task_id='t1', run_id='r1', session_id='s1')
        await seed_run(store, task_id='t2', run_id='r2', session_id='s2')

        async def explode(args, state):
            raise RuntimeError('工具内部炸了')

        async def fine(args, state):
            return {'answer': '好'}

        boom = Tool('deploy', '部署', '', {'type': 'object'}, explode,
                    tool_key='local:test:deploy', effect='read')
        ok = Tool('deploy', '部署', '', {'type': 'object'}, fine,
                  tool_key='local:test:deploy', effect='read')
        hub = self.hub(store, owner_id='w1')
        plan_patch, _ = install_plan(one_step_plan(effect='read'))
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                plan_patch:
            with install_tools({'deploy': boom}):
                self.assertTrue(await worker.run_once(hub, 'w1'))
            first = await store.run('r1')
            with install_tools({'deploy': ok}):
                self.assertTrue(await worker.run_once(hub, 'w1'),
                                'Worker 不能被上一条的错误带走')

        self.assertEqual(first['status'], 'failed')
        second = await store.run('r2')
        self.assertEqual(second['status'], 'succeeded')
        await store.close()

    async def test_empty_stored_plan_is_not_treated_as_a_resume(self):
        """库里只有一份空计划：那是"没计划"，必须重新规划（旧代码的坑）。"""
        import scripts.run_worker as worker
        from app.tools import Tool

        store = new_store()
        await seed_run(store)
        await store.save_plan('run1', 1, goal='做个页面', steps=[])
        remote = Remote()
        tool = remote.tool('deploy')
        hub = self.hub(store, owner_id='w1')
        plan_patch, plan_calls = install_plan(one_step_plan())
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'deploy': tool}), plan_patch:
            await worker.run_once(hub, 'w1')

        self.assertEqual(plan_calls, ['做个页面'], '空计划不等于续跑')
        self.assertEqual(remote.name_count('deploy'), 1)
        self.assertEqual((await store.run('run1'))['status'], 'succeeded')
        await store.close()

    async def test_worker_has_no_unawaited_coroutines(self):
        """整条链路跑一遍，必须没有"协程没被 await"的 RuntimeWarning。

        这一条对应阶段 A 的复现结论：漏 await 时传下去的是协程对象，而它既不会
        报错也不会生效——只在解释器退出时冒一句 RuntimeWarning。
        """
        import gc
        import warnings

        import scripts.run_worker as worker

        store = new_store()
        await seed_run(store)
        remote = Remote()
        hub = self.hub(store, owner_id='w1')
        plan_patch, _ = install_plan(one_step_plan())
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools({'deploy': remote.tool('deploy')}), plan_patch:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                await worker.run_once(hub, 'w1')
                gc.collect()

        bad = [str(w.message) for w in caught
               if issubclass(w.category, RuntimeWarning)
               and 'never awaited' in str(w.message)]
        self.assertEqual(bad, [], '执行链路上有协程没有被 await')
        await store.close()


if __name__ == '__main__':
    unittest.main()
