"""阶段 C：先提交计划，再执行；按步骤恢复（文档 §5、§8 恢复矩阵）。

这里模拟的不是"函数被调用"，而是**进程在某个时刻消失**留下的库状态：

| 库里留下的样子 | 恢复时必须做的事 |
|---|---|
| 有动作、没有计划 | 不能拿新规划冒充恢复（会绕开动作身份） |
| 动作只到 prepared | 请求没发出去，可以沿同一身份重做 |
| 动作 succeeded | 直接复用回执，不重发 |
| 动作 in_flight / unknown | 先核实，绝不自动重发 |

为此用例直接往库里写这些行，再走**真实**的执行入口，看远端调用次数和最终状态。
"""
import asyncio
import tempfile
import unittest

from unittest.mock import patch

from app import ledger as ledger_module
from tests.exec_env import (Remote, install_main, install_plan, install_tools,
                            new_store, one_step_plan, reset_db, seed_run)

STEPS = [{'tool': 'deploy', 'step_id': 's1', 'args': {},
          'tool_key': 'local:test:deploy', 'effect': 'write'}]


class RecoveryTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await reset_db()

    def hub(self, store, **kwargs):
        from app.api_hub import Hub
        return Hub(store, **kwargs)

    async def commit_plan(self, store, steps=STEPS, version=1):
        await store.save_plan('run1', version, goal='做个页面', steps=steps)
        await store.save_steps('run1', version, steps)

    async def execute(self, store, tools_by_name, steps=STEPS):
        """走真实执行入口（claim → execute_claimed_run）。"""
        import scripts.run_worker as worker
        hub = self.hub(store, owner_id='w1')
        plan_patch, calls = install_plan(steps)
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools_by_name), plan_patch:
            await worker.run_once(hub, 'w1', run_id='run1')
        return calls

    async def test_plan_is_committed_before_the_first_tool_call(self):
        """工具被调用的那一刻，计划与快照必须已经在库里。"""
        store = new_store()
        await seed_run(store)
        seen = {}

        async def spy(args, state):
            seen['plan'] = await store.plan('run1')
            seen['steps'] = await store.steps_of('run1')
            seen['snapshot'] = await store.run_snapshot('run1')
            return {'answer': '好了'}

        from app.tools import Tool
        tool = Tool('deploy', '部署', '', {}, spy,
                    tool_key='local:test:deploy', effect='write')
        await self.execute(store, {'deploy': tool})

        self.assertIsNotNone(seen['snapshot'], '执行前必须已经提交快照')
        self.assertEqual([s['step_id'] for s in seen['plan']['steps']], ['s1'])
        self.assertEqual([s['step_id'] for s in seen['steps']], ['s1'])
        await store.close()

    async def test_plan_commit_failure_means_zero_remote_calls(self):
        """计划提交失败 → 一个远端调用都不能发出去。"""
        store = new_store()
        await seed_run(store)
        remote = Remote()

        async def broken(*args, **kwargs):
            raise RuntimeError('数据库写不进去')

        with patch.object(store, 'commit_plan', broken):
            await self.execute(store, {'deploy': remote.tool('deploy')})

        self.assertEqual(remote.calls, [], '提交失败时不允许任何工具调用')
        self.assertEqual((await store.run('run1'))['status'], 'failed')
        await store.close()

    async def test_actions_without_a_plan_are_not_replanned(self):
        """有动作却没有计划：不能重新规划（那会绕开原动作身份），转待核实。"""
        store = new_store()
        await seed_run(store)
        await store.create_action(
            'a_old', run_id='run1', plan_version=1, step_id='s1',
            tool_key='local:test:deploy', args_hash='h', idempotency_key='k_old',
            effect='write')
        await store.update_action('a_old', status='succeeded')
        remote = Remote()
        plan_calls = await self.execute(store, {'deploy': remote.tool('deploy')})

        self.assertEqual(plan_calls, [], '不该规划')
        self.assertEqual(remote.calls, [], '也不该调用任何工具')
        self.assertEqual((await store.run('run1'))['status'], 'needs_reconcile')
        await store.close()

    async def test_prepared_action_is_retried_with_the_same_identity(self):
        """只到 prepared（请求没发出去）：可以沿同一动作身份重做。"""
        store = new_store()
        await seed_run(store)
        await self.commit_plan(store)
        args_hash = ledger_module.args_hash({})
        key = ledger_module.ActionLedger(store, 'run1', 1).key_of(
            's1', 'local:test:deploy', {})
        await store.create_action(
            'a_old', run_id='run1', plan_version=1, step_id='s1',
            tool_key='local:test:deploy', args_hash=args_hash,
            idempotency_key=key, effect='write')
        remote = Remote()
        await self.execute(store, {'deploy': remote.tool('deploy')})

        self.assertEqual(remote.name_count('deploy'), 1)
        actions = await store.actions_of('run1')
        self.assertEqual(len(actions), 1, '不能新建第二个动作')
        self.assertEqual(actions[0]['action_id'], 'a_old')
        self.assertEqual(actions[0]['status'], 'succeeded')
        self.assertEqual((await store.run('run1'))['status'], 'succeeded')
        await store.close()

    async def test_unknown_action_blocks_a_blind_resend(self):
        """服务端可能已经写入但回执丢了：不能再发一次。"""
        store = new_store()
        await seed_run(store)
        await self.commit_plan(store)
        key = ledger_module.ActionLedger(store, 'run1', 1).key_of(
            's1', 'local:test:deploy', {})
        await store.create_action(
            'a_old', run_id='run1', plan_version=1, step_id='s1',
            tool_key='local:test:deploy', args_hash=ledger_module.args_hash({}),
            idempotency_key=key, effect='write')
        await store.update_action('a_old', status='unknown')
        remote = Remote()
        await self.execute(store, {'deploy': remote.tool('deploy')})

        self.assertEqual(remote.calls, [], '结果未定的动作不能重发')
        self.assertEqual((await store.run('run1'))['status'], 'needs_reconcile')
        await store.close()

    async def test_succeeded_action_is_reused_after_a_crash(self):
        """已经成功的步骤：直接复用回执，远端调用计数不增加。"""
        store = new_store()
        await seed_run(store)
        await self.commit_plan(store)
        key = ledger_module.ActionLedger(store, 'run1', 1).key_of(
            's1', 'local:test:deploy', {})
        await store.create_action(
            'a_old', run_id='run1', plan_version=1, step_id='s1',
            tool_key='local:test:deploy', args_hash=ledger_module.args_hash({}),
            idempotency_key=key, effect='write')
        await store.update_action('a_old', status='succeeded',
                                  receipt_json={'answer': '上次就成功了'})
        remote = Remote()
        await self.execute(store, {'deploy': remote.tool('deploy')})

        self.assertEqual(remote.calls, [], '已成功的动作不能重发')
        run = await store.run('run1')
        self.assertEqual(run['status'], 'succeeded')
        task = await store.task('task1')
        self.assertEqual((task['state'] or {}).get('outcome', {}).get('kind'),
                         'answer', '复用的是回执，不是空结果')
        await store.close()

    async def test_commit_failure_after_the_tool_returns_does_not_advance(self):
        """工具已返回、结算提交失败：不能当作完成继续下一步。"""
        store = new_store()
        await seed_run(store)
        steps = [{'tool': 'first', 'step_id': 's1', 'args': {},
                  'tool_key': 'local:test:first', 'effect': 'write'},
                 {'tool': 'second', 'step_id': 's2', 'args': {},
                  'tool_key': 'local:test:second', 'effect': 'write'}]
        await self.commit_plan(store, steps)
        remote = Remote()

        async def broken_settle(*args, **kwargs):
            raise RuntimeError('提交时数据库断了')

        original = store.settle_action
        calls = {'n': 0}

        async def settle_once(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('提交时数据库断了')
            return await original(*args, **kwargs)

        with patch.object(store, 'settle_action', settle_once):
            await self.execute(store, {'first': remote.tool('first'),
                                       'second': remote.tool('second')},
                               steps=steps)

        self.assertEqual(remote.name_count('first'), 1)
        self.assertEqual(remote.name_count('second'), 0,
                         '第一步没提交成功，第二步不能执行')
        run = await store.run('run1')
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['lease_owner'], None, '失败也要交还租约')
        await store.close()


if __name__ == '__main__':
    unittest.main()
