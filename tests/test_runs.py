"""运行执行：动作账本、租约、生命周期、Worker、补偿与状态机。"""
import asyncio
import time
import unittest

from dataclasses import replace
from unittest.mock import AsyncMock, patch

from tests.core_support import PG_DSN, new_store, patch_all_emits, reset_db
from webpage_agent.agent import nodes


class ActionLedgerTests(unittest.IsolatedAsyncioTestCase):
    """动作账本：已成功不重发、结果不确定不重试（文档 §8）。

    本轮改造后账本只有两个写操作：`admit()`（准入）与 `settle()`（结算），
    各自一个事务；确定性由异常上的显式标注决定，不再按异常类名猜。
    """

    async def asyncSetUp(self):
        await reset_db()

    async def ledger(self, store):
        """账本必须挂在真实的 run 上：准入要校验运行状态与租约。"""
        from webpage_agent.store.ledger import ActionLedger
        await store.create_task('task1', 'sess1', '测试')
        await store.create_run('run1', 'task1', status='running')
        return ActionLedger(store, 'run1', 1)

    async def test_succeeded_action_is_replayed_not_resent(self):
        store = new_store()
        book = await self.ledger(store)
        first = await book.admit(step_id='s1', tool_key='local:publishing:deploy',
                                 effect='write', args={'a': 1})
        self.assertEqual(first['status'], 'admitted')
        await book.settle(step_id='s1', action_id=first['action_id'],
                          attempt_id=first['attempt_id'], status='succeeded',
                          receipt={'public_url': 'https://x/1'},
                          step_status='succeeded')

        # 同一个步骤、同样的参数再跑一次：必须直接复用回执，不再调用远端
        again = await book.admit(step_id='s1', tool_key='local:publishing:deploy',
                                 effect='write', args={'a': 1})
        self.assertEqual(again['status'], 'succeeded')
        self.assertEqual(again['replay'], {'public_url': 'https://x/1'})
        # 只该有一次真实尝试
        self.assertEqual(len(await store.attempts_of(first['action_id'])), 1)

    async def test_changed_args_is_a_different_action(self):
        store = new_store()
        book = await self.ledger(store)
        a = await book.admit(step_id='s1', tool_key='t', effect='write', args={'a': 1})
        b = await book.admit(step_id='s1', tool_key='t', effect='write', args={'a': 2})
        self.assertNotEqual(a['action_id'], b['action_id'])

    async def test_changed_tool_key_is_a_different_action(self):
        """稳定身份必须包含 tool_key：同一个步骤换了工具就是另一个动作。"""
        store = new_store()
        book = await self.ledger(store)
        a = await book.admit(step_id='s1', tool_key='local:web:search',
                             effect='read', args={})
        b = await book.admit(step_id='s1', tool_key='local:web:fetch',
                             effect='read', args={})
        self.assertNotEqual(a['action_id'], b['action_id'])
        self.assertIn('local:web:search', book.key_of('s1', 'local:web:search', {}))

    async def test_unknown_result_blocks_blind_retry(self):
        """结果不确定时不能当成失败去重发，也不能当成功复用。"""
        from webpage_agent.capabilities.base import ToolError
        store = new_store()
        book = await self.ledger(store)
        first = await book.admit(step_id='s1', tool_key='mcp:publishing:deploy_site',
                                 effect='write', args={})
        # 模拟"发出去之后超时"：异常上没有确定性标注 → 默认 unknown
        status = await book.fail_from_exception(
            step_id='s1', action_id=first['action_id'],
            attempt_id=first['attempt_id'],
            exc=ToolError('超时', certainty='unknown', phase='receive'))
        self.assertEqual(status, 'unknown')
        again = await book.admit(step_id='s1', tool_key='mcp:publishing:deploy_site',
                                 effect='write', args={})
        self.assertEqual(again['status'], 'unknown')
        self.assertIn('blocked', again)

    async def test_not_executed_is_recorded_as_failed_and_retryable(self):
        """确认没执行 → 记为 failed（可显式重试），不能落成 unknown。"""
        from webpage_agent.capabilities.base import ToolError
        store = new_store()
        book = await self.ledger(store)
        first = await book.admit(step_id='s1', tool_key='local:web:search',
                                 effect='read', args={})
        status = await book.fail_from_exception(
            step_id='s1', action_id=first['action_id'],
            attempt_id=first['attempt_id'],
            exc=ToolError('连不上', certainty='not_executed', phase='connect'))
        self.assertEqual(status, 'failed')
        stored = await store.action(first['action_id'])
        self.assertEqual(stored['status'], 'failed')

    async def test_failure_records_error_code_and_certainty(self):
        from webpage_agent.capabilities.base import ToolError
        store = new_store()
        book = await self.ledger(store)
        first = await book.admit(step_id='s1', tool_key='t', effect='write', args={})
        await book.fail_from_exception(
            step_id='s1', action_id=first['action_id'],
            attempt_id=first['attempt_id'],
            exc=ToolError('服务端拒绝', certainty='failed_confirmed',
                          phase='receive', error_code='E_INVALID_INPUT'))
        stored = await store.action(first['action_id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(stored['receipt']['error_code'], 'E_INVALID_INPUT')
        self.assertEqual(stored['receipt']['certainty'], 'failed_confirmed')


class RunLeaseTests(unittest.IsolatedAsyncioTestCase):
    """租约与 fencing：两个 Worker 抢同一个 run，只有一个能领到。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    async def test_claim_is_exclusive_and_epoch_increments(self):
        store = new_store()
        await store.create_run('run1', 'task1')
        first = await store.claim_run('w1', lease_seconds=30)
        self.assertEqual(first['run_id'], 'run1')
        self.assertEqual(first['lease_epoch'], 1)
        self.assertIsNone(await store.claim_run('w2'))          # 租约未过期，抢不到
        # 租约过期后另一个 Worker 可以接手，代次递增
        later = await store.claim_run('w2', now_ts=10 ** 12)
        self.assertEqual(later['lease_epoch'], 2)
        # 旧 Worker 续租失败，不能再覆盖状态
        self.assertFalse(await store.renew_lease('run1', 'w1'))

    async def test_jobs_are_deduped(self):
        store = new_store()
        self.assertTrue(await store.enqueue_job('j1', 'summary', 'sess:1'))
        self.assertFalse(await store.enqueue_job('j2', 'summary', 'sess:1'))


class RunLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """run 生命周期与断点续跑（文档 §7、§8）。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    def hub(self):
        from webpage_agent.api.api_hub import Hub
        return Hub(new_store())

    async def test_finishing_a_run_persists_plan_and_steps(self):
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        # 真实链路里 validate_plan 会给每步补 tool_key；这里按同一形状构造
        plan = {'steps': [{'tool': 'web_search', 'step_id': 's1', 'args': {},
                           'tool_key': 'local:web:search'},
                          {'tool': 'render_html', 'step_id': 's2', 'args': {},
                           'tool_key': 'local:reports:render'}],
                'summary': '先搜后渲染'}

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            return {'task_id': 'a1', 'status': 'local_ready', 'timings': [],
                    'deployment': {}, 'plan': plan,
                    'artifact': {'kind': 'sources', 'html_path': '/tmp/x.html'}}

        with patch.object(hub_module, 'run_task', fake):
            record = await hub.start('做点事')
            await asyncio.wait_for(record._task, timeout=10)

        run = await hub.store.run(record.run_id)
        self.assertEqual(run['status'], 'succeeded')
        saved = await hub.store.plan(record.run_id)
        self.assertEqual([s['step_id'] for s in saved['steps']], ['s1', 's2'])
        self.assertEqual([s['tool_key'] for s in await hub.store.steps_of(record.run_id)],
                         ['local:web:search', 'local:reports:render'])

    async def test_resume_reuses_saved_plan(self):
        """续跑沿用已存计划，不重新规划（§7：已完成步骤不可改写）。"""
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        seen = {}

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            seen['resume_plan'] = resume_plan
            return {'task_id': 'a1', 'status': 'local_ready', 'timings': [],
                    'deployment': {},
                    'plan': {'steps': [{'tool': 'render_html', 'step_id': 's1',
                                        'args': {}}], 'summary': 'x'}}

        with patch.object(hub_module, 'run_task', fake):
            record = await hub.start('第一次')
            await asyncio.wait_for(record._task, timeout=10)
            # 只有"停下来"的 run 才能续跑：把这次运行置成暂停态
            await hub.store.update_run(record.run_id, status='paused')
            resumed = await hub.resume(record.run_id)
            self.assertIsNotNone(resumed)
            await asyncio.wait_for(resumed._task, timeout=10)
        self.assertEqual([s['step_id'] for s in seen['resume_plan']['steps']],
                         ['s1'])

    async def test_resume_refuses_a_finished_run(self):
        """已成功的 run 不能"再跑一遍"：那是重复副作用（§7.3）。"""
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            return {'task_id': 'a1', 'status': 'local_ready', 'timings': [],
                    'deployment': {},
                    'plan': {'steps': [{'tool': 'render_html', 'step_id': 's1',
                                        'args': {}}], 'summary': 'x'}}

        with patch.object(hub_module, 'run_task', fake):
            record = await hub.start('第一次')
            await asyncio.wait_for(record._task, timeout=10)
        self.assertEqual((await hub.store.run(record.run_id))['status'],
                         'succeeded')
        self.assertIsNone(await hub.resume(record.run_id))

    async def test_reconcile_marks_inflight_actions_unknown(self):
        """重启时不能说"一律中断"——已发出未回执的调用必须保持 unknown。"""
        hub = self.hub()
        await hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        await hub.store.create_run('r1', 't1', status='running')
        await hub.store.create_action(
            'a1', run_id='r1', plan_version=1, step_id='s1',
            tool_key='mcp:publishing:deploy_site', args_hash='h',
            idempotency_key='k1', effect='write')
        await hub.store.update_action('a1', status='in_flight')
        # 另一条只到 prepared（请求根本没发出去）：可以安全重试，不该标 unknown
        await hub.store.create_action(
            'a2', run_id='r1', plan_version=1, step_id='s2',
            tool_key='mcp:publishing:update_site', args_hash='h2',
            idempotency_key='k2', effect='write')

        stats = await hub.reconcile()
        self.assertEqual(stats['runs'], 1)
        self.assertEqual(stats['unknown_actions'], 1)
        self.assertEqual((await hub.store.action('a1'))['status'], 'unknown')
        self.assertEqual((await hub.store.action('a2'))['status'], 'prepared')  # 可重试
        self.assertEqual((await hub.store.run('r1'))['status'], 'reconciling')

    async def test_unknown_action_blocks_instead_of_resending(self):
        """已发出但结果未定的动作，续跑时必须停下来而不是再发一次。"""
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.store.ledger import ActionLedger, use_ledger
        from webpage_agent.tools.core import Tool

        store = new_store()
        await store.create_task('task1', 'sess1', '测试')
        await store.create_run('run1', 'task1', status='running')
        book = ActionLedger(store, 'run1', 1)
        first = await book.admit(step_id='s1', tool_key='local:publishing:deploy',
                                 effect='write', args={})
        # 发出去了、回执没回来 → unknown（结果未定）
        await book.fail_from_exception(step_id='s1',
                                       action_id=first['action_id'],
                                       attempt_id=first['attempt_id'],
                                       exc=TimeoutError('回执超时'))

        async def should_not_run(args, state):
            raise AssertionError('结果未定的动作不能重发')

        fake = Tool('deploy_site', '部署站点', 'd', {}, should_not_run,
                    tool_key='local:publishing:deploy', effect='write')
        plan = {'steps': [{'tool': 'deploy_site', 'step_id': 's1', 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'deploy_site': fake}):
            with use_ledger(book):
                result = await nodes.run_step(
                    {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertIn('结果未确定', result['failure'])
        self.assertEqual(result['status'], 'needs_reconcile')

    async def test_pause_only_affects_active_runs(self):
        hub = self.hub()
        await hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        await hub.store.create_run('r1', 't1', status='running')
        # running：只登记请求，由执行者在下一处边界安全停下
        paused = await hub.pause('r1')
        self.assertEqual(paused, {'ok': True, 'status': 'running',
                                  'pause_requested': True})
        self.assertEqual((await hub.store.run('r1'))['pause_requested'], 1)
        # 排队中的 run 可以直接置 paused：没有执行者在跑，不用等边界
        await hub.store.create_run('r2', 't1', status='queued')
        self.assertEqual((await hub.pause('r2'))['status'], 'paused')
        self.assertEqual((await hub.store.run('r2'))['status'], 'paused')
        # 已结束的 run 不能被"暂停"：返回 ok=False，状态不变
        await hub.store.update_run('r1', status='succeeded')
        settled = await hub.pause('r1')
        self.assertFalse(settled['ok'])
        self.assertEqual(settled['status'], 'succeeded')
        self.assertEqual((await hub.store.run('r1'))['status'], 'succeeded')


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    """独立 Worker：领取租约、消费 outbox。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    def hub(self):
        from webpage_agent.api.api_hub import Hub
        return Hub(new_store())

    async def test_worker_claims_and_reports_nothing_to_do(self):
        from scripts.run_worker import run_once
        hub = self.hub()
        self.assertFalse(await run_once(hub, 'w1'))

    async def test_jobs_drain_is_idempotent(self):
        from scripts.run_worker import drain_jobs
        hub = self.hub()
        await hub.store.enqueue_job('j1', 'session_summary', 's1:r1',
                              {'session_id': 's1'})
        await hub.store.enqueue_job('j2', 'session_summary', 's1:r1',
                              {'session_id': 's1'})
        done = await drain_jobs(hub, 'w1')
        self.assertEqual(done, 1)       # 去重键相同，只有一个作业
        self.assertEqual(await drain_jobs(hub, 'w1'), 0)


class StatusDurabilityTests(unittest.IsolatedAsyncioTestCase):
    """内存终态不能早于库：否则回放接口会显示"已完成但还在排队"。

    这条不变量以前是坏的：收尾时先把 record.status 置成终态，tasks 行要等
    下一条 persist() 才写。测试轮询 record.done 看到 True 就去读库，读到的是
    旧状态。多进程下别的副本读到的同样是旧值，不只是测试时序问题。
    """

    async def test_memory_status_never_runs_ahead_of_the_row(self):
        from webpage_agent.api.api_hub import TaskRecord
        await reset_db()
        store = new_store()
        await store.ready()
        await store.create_task('sd1', 'sds1', 'x')
        record = TaskRecord(task_id='sd1', session_id='sds1', message='x',
                            store=store, status='queued')
        # 让写库慢一点，把窗口放大到可观测
        original = store.update_task

        async def slow(task_id, **fields):
            await asyncio.sleep(0.1)
            await original(task_id, **fields)

        with patch.object(store, 'update_task', slow):
            task = asyncio.create_task(record.persist(status='succeeded'))
            await asyncio.sleep(0.03)
            # 库里还没写，内存就不能先"完成"
            self.assertFalse(record.done, '库未落盘时内存不该已进终态')
            self.assertEqual((await store.task('sd1'))['status'], 'running')
            await task
        self.assertTrue(record.done)
        self.assertEqual((await store.task('sd1'))['status'], 'succeeded')
        await store.close()


class ExecutionPathTests(unittest.IsolatedAsyncioTestCase):
    """单一执行路径：就地执行也必须持有租约（评审 P0-1 / P0-5 / P0-6 / P0-7）。

    这些用例守的是同一件事：**同一份计划不能被执行两次**。一旦破防，部署类
    写操作会真的建出第二个站点，模型费用也会翻倍。
    """

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    @staticmethod
    def hub(store=None):
        from webpage_agent.api.api_hub import Hub
        return Hub(store if store is not None else new_store())

    @staticmethod
    def stub(delay=0.0, status='local_ready'):
        async def fake_run_task(text, progress=None, on_event=None, history=None,
                                on_start=None, prior=None, memory=None,
                                ledger=None, run_id=None, resume_plan=None):
            if delay:
                await asyncio.sleep(delay)
            return {'task_id': 'a1', 'status': status, 'timings': [],
                    'deployment': {}}
        return fake_run_task

    async def test_inline_execution_holds_a_lease_so_worker_cannot_steal(self):
        """API 就地执行也要占租约，否则 Worker 会把这条 running 当孤儿再跑一遍。"""
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=0.2)):
            record = await hub.start('做点事')
            run = await hub.store.run(record.run_id)
            self.assertEqual(run['status'], 'running')
            self.assertTrue(run['lease_owner'], '就地执行也必须写 lease_owner')
            self.assertIsNone(await hub.store.claim_run('w-other'),
                              '租约未过期时 Worker 不该领到同一个 run')
            await asyncio.wait_for(record._task, timeout=5)
        # 跑完要交还租约，否则这条 run 在过期前都无法被续跑
        self.assertIsNone((await hub.store.run(record.run_id))['lease_owner'])

    async def test_reconcile_spares_runs_held_by_another_instance(self):
        """第二个副本启动时不能替别人正在跑的 run 判死。"""
        import webpage_agent.api.api_hub as hub_module
        first = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=0.3)):
            record = await first.start('长任务')
            second = self.hub(first.store)
            stats = await second.reconcile(owner='other-instance')
            self.assertEqual(stats['runs'], 0)
            self.assertEqual((await first.store.run(record.run_id))['status'], 'running')
            await asyncio.wait_for(record._task, timeout=5)

    async def test_reconcile_requeues_runs_without_inflight_actions(self):
        """没有 in_flight 动作的遗留 run 应回到队列，而不是挂着等人核实。"""
        hub = self.hub()
        await hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        await hub.store.create_run('r1', 't1', status='running')
        stats = await hub.reconcile()
        self.assertEqual(stats['requeued'], 1)
        self.assertEqual((await hub.store.run('r1'))['status'], 'queued')

    async def test_cross_process_cancel_writes_a_flag_and_is_honest(self):
        """取消要真的送到执行者，且已经结束的运行不能假装取消成功。"""
        hub = self.hub()
        await hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        await hub.store.create_run('r1', 't1', status='running')
        self.assertTrue(await hub.cancel_run('r1'))
        self.assertIsNotNone((await hub.store.run('r1'))['cancel_requested_at'])
        await hub.store.update_run('r1', status='succeeded')
        self.assertFalse(await hub.cancel_run('r1'))

    async def test_resume_works_after_restart(self):
        """杀进程 → 新 Hub → 续跑：resume 必须回库，而不是只查内存。"""
        import webpage_agent.api.api_hub as hub_module
        # 库由 PG_TEST_DSN 提供；folder/path 是 SQLite 时代的残留，从未被使用。
        first = self.hub(new_store())
        with patch.object(hub_module, 'run_task', self.stub()):
            record = await first.start('第一次')
            await asyncio.wait_for(record._task, timeout=5)
        # 模拟"计划已存、执行被暂停后进程被杀"
        await first.store.save_plan(
            record.run_id, 1, goal='g',
            steps=[{'tool': 'render_html', 'step_id': 's1', 'args': {},
                    'tool_key': 'local:reports:render'}])
        await first.store.update_run(record.run_id, status='paused')
        task_id, run_id = record.task_id, record.run_id
        await first.store.close()

        second = self.hub(new_store())
        seen = {}

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            seen['plan'] = resume_plan
            seen['history'] = history
            return {'task_id': 'a1', 'status': 'local_ready', 'timings': [],
                    'deployment': {}}

        with patch.object(hub_module, 'run_task', fake):
            resumed = await second.resume(run_id)
            self.assertIsNotNone(resumed, '重启后必须还能续跑')
            await asyncio.wait_for(resumed._task, timeout=5)
        self.assertEqual([s['step_id'] for s in seen['plan']['steps']], ['s1'])
        # 续跑要带上历史，否则续出来的结果接不上前文
        self.assertTrue(any(m['content'] == '第一次'
                            for m in (seen['history'] or [])))
        self.assertEqual((await second.store.task(task_id))['task_id'], task_id)


class BackendJobTests(unittest.IsolatedAsyncioTestCase):
    """后台作业：必须真的 await，失败要退避重排而不是一次终结。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    def hub(self):
        from webpage_agent.api.api_hub import Hub
        return Hub(new_store())

    async def test_failed_job_is_requeued_with_backoff(self):
        from webpage_agent.core import memory as memory_module
        from scripts.run_worker import drain_jobs
        hub = self.hub()
        await hub.store.enqueue_job('j1', 'session_summary', 's1:r1',
                              {'session_id': 's1'})

        async def boom(store, session_id):
            raise RuntimeError('模型不可用')

        with patch.object(memory_module, 'compact', boom):
            self.assertEqual(await drain_jobs(hub, 'w1'), 0)
        job = await hub.store.job('j1')
        self.assertEqual(job['status'], 'queued', '一次失败不能当终态')
        self.assertEqual(job['last_error'], 'RuntimeError')
        # 退避时间放在 due_at（调度到期时间），不再占用租约期限：
        # 文档 §5 M5 要求把这两列分开，否则退避中的作业会被租约回收误领。
        self.assertGreater(job['due_at'], time.time(), '应处于退避中')
        self.assertIsNone(job['lease_expires_at'],
                          '退避中的作业不应持有租约')

    async def test_summary_job_actually_compacts_memory(self):
        """以前 asyncio.run() 在运行中的事件循环里必炸，摘要从来没成功过。"""
        from webpage_agent.core import memory as memory_module
        from scripts.run_worker import drain_jobs
        hub = self.hub()
        for index in range(14):
            await hub.store.append_message('s1', 'user', f'第{index}条')
        await hub.store.enqueue_job('j1', 'session_summary', 's1:r1',
                              {'session_id': 's1'})
        seen = {}

        async def fake_compact(store, session_id):
            seen['session'] = session_id
            return {'compacted': True, 'folded': 8}

        with patch.object(memory_module, 'compact', fake_compact):
            self.assertEqual(await drain_jobs(hub, 'w1'), 1)
        self.assertEqual(seen['session'], 's1')
        self.assertEqual((await hub.store.job('j1'))['status'], 'done')


class CompensationTests(unittest.IsolatedAsyncioTestCase):
    """补偿步骤（文档 §8.4）：撤销是有副作用动作，要单独执行、单独记账。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    @staticmethod
    def _tools(calls):
        from webpage_agent.tools import core as tool_module

        async def deploy(args, state):
            calls.append(('deploy', dict(args)))
            return {'deployment': {'site_id': 'site-1'}}

        class _Rejected(RuntimeError):
            # 业务明确拒绝：确认没有产生副作用（文档 §6.1 failed_confirmed）。
            # 不标注的话默认是 unknown，默认值刻意保守——那时不允许自动回滚。
            certainty = 'failed_confirmed'
            phase = 'receive'
            error_code = 'E_STEP_REJECTED'
            safe_message = '第二步炸了'

        async def explode(args, state):
            raise _Rejected('第二步炸了')

        async def undo(args, state):
            calls.append(('undo', dict(args)))
            return {}

        return {
            'deploy': tool_module.Tool(
                'deploy', '部署', '', {}, deploy,
                tool_key='local:publishing:deploy', effect='write'),
            'explode': tool_module.Tool(
                'explode', '会失败的步骤', '', {}, explode,
                tool_key='local:x:boom', effect='write'),
            'undo': tool_module.Tool(
                'undo', '撤销部署', '', {}, undo,
                tool_key='local:publishing:undo', effect='destructive'),
        }

    async def test_unknown_compensation_is_a_warning_not_a_blocker(self):
        """补偿工具写错不该把整份计划判死，但必须留痕。"""
        from webpage_agent.tools import core as tool_module
        steps, issues = nodes.validate_plan([
            {'tool': 'render_sources_html', 'step_id': 's1', 'args': {},
             'compensate': {'tool': 'ghost_tool', 'args': {}}},
        ], tool_module.BUILTIN_TOOLS)
        self.assertEqual([s['step_id'] for s in steps], ['s1'])
        self.assertNotIn('compensate', steps[0])
        self.assertEqual(issues[0]['code'], 'unknown_compensation')
        self.assertEqual(issues[0]['severity'], 'warning')

    async def test_compensation_is_kept_and_bound_to_a_real_tool(self):
        from webpage_agent.tools import core as tool_module
        steps, issues = nodes.validate_plan([
            {'tool': 'web_search', 'step_id': 's1', 'args': {'query': 'q'},
             'compensate': {'tool': 'render_sources_html', 'args': {}}},
        ], tool_module.BUILTIN_TOOLS)
        self.assertEqual(issues, [])
        self.assertEqual(steps[0]['compensate']['tool_key'],
                         'local:export:sources_html')

    async def test_failure_rolls_back_succeeded_writes_in_reverse(self):
        """前面成功、后面失败 → 逆序撤销已成功的写操作，并把明细写进状态。"""
        from webpage_agent.tools import core as tool_module
        calls = []
        tools = self._tools(calls)
        plan = {'steps': [
            {'tool': 'deploy', 'step_id': 's1', 'args': {},
             'compensate': {'tool': 'undo', 'args': {
                 'site_id': {'ref': {'step_id': 's1',
                                     'pointer': '/deployment/site_id'}}}}},
            {'tool': 'explode', 'step_id': 's2', 'args': {},
             'depends_on': ['s1']},
        ]}
        state = {'plan': plan, 'step_index': 0, 'step_outputs': {},
                 'user_input': '做个页面并发布'}

        async def fake_find(name, context=None):
            return tools.get(name)

        with patch.object(tool_module, 'find_tool', fake_find), \
                patch.object(nodes, 'emit', lambda event: None):
            state.update(await nodes.run_step(state))
            result = await nodes.run_step(state)

        self.assertEqual(calls[0][0], 'deploy')
        self.assertEqual(calls[1], ('undo', {'site_id': 'site-1'}),
                         '补偿必须拿到部署返回的站点 ID')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['compensations'][0]['status'], 'succeeded')
        self.assertIn('已回滚 1 步', result['failure'])

    async def test_failed_rollback_is_reported_as_partial(self):
        """撤销失败不能只说"失败"：线上可能还留着一个站点。"""
        from webpage_agent.tools import core as tool_module
        calls = []
        tools = self._tools(calls)

        async def broken_undo(args, state):
            raise RuntimeError('删除站点被拒绝')

        tools['undo'] = replace(tools['undo'], run=broken_undo)
        plan = {'steps': [
            {'tool': 'deploy', 'step_id': 's1', 'args': {},
             'compensate': {'tool': 'undo', 'args': {}}},
            {'tool': 'explode', 'step_id': 's2', 'args': {}},
        ]}
        state = {'plan': plan, 'step_index': 0, 'step_outputs': {},
                 'user_input': 'x'}

        async def fake_find(name, context=None):
            return tools.get(name)

        with patch.object(tool_module, 'find_tool', fake_find), \
                patch.object(nodes, 'emit', lambda event: None):
            state.update(await nodes.run_step(state))
            result = await nodes.run_step(state)
        self.assertEqual(result['status'], 'partially_succeeded')
        self.assertIn('回滚失败', result['failure'])

    async def test_unknown_action_is_not_compensated_blindly(self):
        """结果未定的写操作不能自动去撤：那是基于猜测再发一次写请求。"""
        from webpage_agent.tools import core as tool_module
        calls = []
        tools = self._tools(calls)

        # 没有标注确定性的异常 → 默认 unknown（不确定远端究竟做没做）
        async def ambiguous(args, state):
            raise RuntimeError('连接被重置')

        tools['explode'] = replace(tools['explode'], run=ambiguous)
        plan = {'steps': [
            {'tool': 'deploy', 'step_id': 's1', 'args': {},
             'compensate': {'tool': 'undo', 'args': {}}},
            {'tool': 'explode', 'step_id': 's2', 'args': {}},
        ]}
        state = {'plan': plan, 'step_index': 0, 'step_outputs': {},
                 'user_input': 'x'}

        async def fake_find(name, context=None):
            return tools.get(name)

        with patch.object(tool_module, 'find_tool', fake_find), \
                patch.object(nodes, 'emit', lambda event: None):
            state.update(await nodes.run_step(state))
            result = await nodes.run_step(state)
        self.assertEqual(result['status'], 'needs_reconcile')
        self.assertTrue(result['needs_reconciliation'])
        self.assertEqual(result['compensations'], [])
        self.assertFalse([c for c in calls if c[0] == 'undo'],
                         '结果未定时不能自动撤销')

    async def test_manual_compensation_creates_its_own_run(self):
        """手工补偿是一次独立运行，原运行只记 compensated_at。"""
        import webpage_agent.api.api_hub as hub_module
        from webpage_agent.api.api_hub import Hub
        hub = Hub(new_store())
        await hub.store.create_task('t1', 's1', '发布页面')
        await hub.store.ensure_task_meta('t1', session_id='s1', goal='发布页面')
        await hub.store.create_run('r1', 't1', status='succeeded')
        step = {'tool': 'deploy_site', 'step_id': 's1', 'args': {},
                'tool_key': 'local:publishing:deploy', 'effect': 'write',
                'compensate': {'tool': 'undo_site', 'args': {'site_id': 'x'},
                               'tool_key': 'local:publishing:undo',
                               'effect': 'destructive'}}
        await hub.store.save_plan('r1', 1, goal='发布页面', steps=[step])
        await hub.store.save_steps('r1', 1, [step])
        await hub.store.update_step('r1', 1, 's1', status='succeeded')

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            assert resume_plan['kind'] == 'compensation'
            # 真实 run_task 会把 resume_plan 落成 plan（含 kind），
            # outcome 就是靠它认出"这是一次补偿"
            return {'task_id': 'a1', 'status': 'succeeded', 'timings': [],
                    'plan': {'kind': 'compensation', 'steps': []},
                    'compensations': [
                        {'step_id': 's1', 'tool': 'undo_site',
                         'status': 'succeeded'}]}

        with patch.object(hub_module, 'run_task', fake):
            result = await hub.compensate('r1')

        self.assertIsNotNone(result)
        self.assertNotEqual(result['run_id'], 'r1')
        self.assertEqual((await hub.store.run('r1'))['compensated_at'] is not None, True)
        self.assertEqual(result['status'], 'compensated')
        # 再补一次要被拒绝：撤销两次可能误删重建的东西
        with self.assertRaises(RuntimeError):
            await hub.compensate('r1')

    async def test_compensation_skips_steps_that_never_succeeded(self):
        from webpage_agent.api.api_hub import Hub
        hub = Hub(new_store())
        await hub.store.create_task('t1', 's1', 'g')
        await hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        await hub.store.create_run('r1', 't1', status='failed')
        step = {'tool': 'deploy_site', 'step_id': 's1', 'args': {},
                'compensate': {'tool': 'undo_site', 'args': {}}}
        await hub.store.save_plan('r1', 1, goal='g', steps=[step])
        await hub.store.save_steps('r1', 1, [step])
        await hub.store.update_step('r1', 1, 's1', status='failed')
        self.assertEqual(await hub.compensation_steps('r1'), [])


class StatusVocabularyTests(unittest.IsolatedAsyncioTestCase):
    """状态机词汇封闭：业务结果进 outcome，展示词只出现在派生标签里。

    这条约束是上一轮讨论的落点——"接 N 个 MCP Server 就要加 N 个状态值"
    之所以不成立，全靠这里钉住：加一个业务终态会先让这组用例红。
    """

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    async def test_run_status_vocabulary_is_closed(self):
        from webpage_agent.core import outcome
        # 冻结契约：要加状态值，就得连这份清单一起改，是有意识的决定
        self.assertEqual(sorted(outcome.RUN_STATUSES), [
            'cancelled', 'failed', 'interrupted', 'needs_reconcile',
            'partially_succeeded', 'paused', 'queued', 'reconciling',
            'running', 'succeeded', 'waiting_approval', 'waiting_input'])
        for word in ('answered', 'local_ready', 'deployed', 'deploy_needs_check',
                     'compensated'):
            self.assertNotIn(word, outcome.RUN_STATUSES, word)
            # 这些词只允许作为"历史值"存在，不再参与状态机
            self.assertIn(word, outcome.LEGACY_STATUS, word)

    async def test_label_is_the_only_place_with_display_words(self):
        from webpage_agent.core import outcome
        cases = [
            ('succeeded', {'answer': 'hi'}, 'answered'),
            ('succeeded', {'artifact': {'html_path': '/x.html'}}, 'local_ready'),
            ('succeeded', {'deployment': {'status': 'success'}}, 'deployed'),
            ('succeeded', {'plan': {'kind': 'compensation'}}, 'compensated'),
            ('needs_reconcile', {'deployment': {'status': 'pending'}},
             'deploy_needs_check'),
            # 没有部署的"未核实"不该硬套部署的说法
            ('needs_reconcile', {}, 'needs_reconcile'),
            ('failed', {}, 'failed'),
            ('cancelled', {}, 'cancelled'),
            ('paused', {}, 'paused'),
        ]
        for status, state, expected in cases:
            self.assertEqual(outcome.label(status, state), expected, status)

    async def test_legacy_business_status_normalizes(self):
        from webpage_agent.core import outcome
        self.assertEqual(outcome.normalize_status('local_ready'), 'succeeded')
        self.assertEqual(outcome.normalize_status('deploy_needs_check'),
                         'needs_reconcile')
        self.assertEqual(outcome.normalize_status('succeeded'), 'succeeded')
        # 未知值不猜：原样交给上层，由 check_run_status 报错
        self.assertEqual(outcome.normalize_status('ticket_created'),
                         'ticket_created')

    async def test_out_of_vocabulary_status_is_loud_not_silent(self):
        """以前 `RUN_STATUS_OF.get(status, status)` 会把业务词静默写进状态机。"""
        from webpage_agent.core import outcome
        with self.assertLogs('webpage_agent.core.outcome', level='ERROR'):
            self.assertEqual(outcome.check_run_status('ticket_created'), 'failed')
        self.assertEqual(outcome.check_run_status('succeeded'), 'succeeded')

    async def test_outcome_records_produced_and_effects(self):
        from webpage_agent.core import outcome
        state = {
            'artifact': {'html_path': '/x/sources.html', 'kind': 'sources',
                         'sha256': 'abc', 'size_bytes': 10},
            'deployment': {'status': 'pending'},
            'plan': {'steps': [
                {'tool': 'web_search', 'step_id': 's1',
                 'tool_key': 'local:web:search', 'effect': 'read'},
                {'tool': 'render_sources_html', 'step_id': 's2',
                 'tool_key': 'local:export:sources_html', 'effect': 'write'},
                {'tool': 'deploy_site', 'step_id': 's3',
                 'tool_key': 'mcp:publishing:deploy_site', 'effect': 'write'},
                {'tool': 'render_sources_html', 'step_id': 's4',
                 'tool_key': 'local:export:sources_html', 'effect': 'write'},
            ]},
            'step_outputs': {
                's1': {'__status__': 'succeeded'},
                's2': {'__status__': 'succeeded',
                       'artifact': {'html_path': '/x/sources.html'}},
                's3': {'__status__': 'succeeded',
                       'deployment': {'status': 'pending'}},
                's4': {'__status__': 'failed'},
            },
        }
        record = outcome.build(state)
        self.assertEqual(record['kind'], 'deployment')
        self.assertEqual([item['path'] for item in record['produced']],
                         ['/x/sources.html'])
        # 只记**成功**的有副作用调用：read 不算，失败那步也不算
        self.assertEqual([item['tool_key'] for item in record['effects']],
                         ['local:export:sources_html',
                          'mcp:publishing:deploy_site'])
        # render 落地成文件即视为已核实；部署回执看不懂 → 未核实
        self.assertTrue(record['effects'][0]['verified'])
        self.assertEqual(record['effects'][0]['target_ref'], '')
        self.assertFalse(record['effects'][1]['verified'])
        self.assertTrue(record['unverified'])
        self.assertEqual(outcome.final_status(state), 'needs_reconcile')

    async def test_tool_status_is_a_stage_marker_not_a_run_status(self):
        """工具自报的 status 是阶段小结（searched/answered），不是 run 终态。"""
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool

        async def fake(args, state):
            return {'answer': '你好', 'status': 'answered'}

        tool = Tool('answer_directly', '直接回答', 'd', {}, fake,
                    tool_key='local:answers:direct', effect='read')
        plan = {'steps': [{'tool': 'answer_directly', 'step_id': 's1', 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'answer_directly': tool}), \
                patch_all_emits(lambda e: None):
            result = await nodes.run_step({'plan': plan, 'step_index': 0,
                                           'user_input': '你好'})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['outcome']['kind'], 'answer')
        # 阶段小结仍留在这一步的输出与耗时行上，但没写进 run 终态
        self.assertEqual(result['step_outputs']['s1']['status'], 'answered')

    async def test_hub_never_persists_a_business_status(self):
        """就算执行层交出旧词，落库的也必须是通用状态。"""
        import webpage_agent.api.api_hub as hub_module
        from webpage_agent.api.api_hub import Hub

        async def fake_run_task(text, progress=None, on_event=None, history=None,
                                on_start=None, prior=None, memory=None,
                                ledger=None, run_id=None, resume_plan=None):
            if on_start:
                await on_start('a1')
            return {'task_id': 'a1', 'status': 'local_ready',   # 旧词
                    'artifact': {'html_path': '/tmp/a1/sources.html',
                                 'kind': 'sources'},
                    'timings': []}

        hub = Hub(new_store())
        with patch.object(hub_module, 'run_task', fake_run_task):
            record = await hub.start('存成网页')
            await asyncio.wait_for(record._task, timeout=10)

        self.assertEqual(record.status, 'succeeded')
        self.assertEqual((await hub.store.task(record.task_id))['status'], 'succeeded')
        self.assertEqual((await hub.store.run(record.run_id))['status'], 'succeeded')
        # 展示标签仍然说得清"本地已生成"
        self.assertEqual(record.summary()['status'], 'local_ready')
        self.assertEqual(record.summary()['run_status'], 'succeeded')

    async def test_reply_for_needs_reconcile_shows_the_url_but_does_not_claim_success(self):
        from webpage_agent.core.messages import reply_text
        text = reply_text({'status': 'needs_reconcile',
                           'deployment': {'status': 'needs_check',
                                          'public_url': 'http://x/1/'}})
        self.assertIn('http://x/1/', text)
        self.assertIn('确认', text)
        self.assertNotIn('部署完成', text)

    async def test_migration_script_rewrites_legacy_rows(self):
        """历史数据迁移：业务终态 → 通用状态，且可重复执行。"""
        import importlib
        from types import SimpleNamespace as NS
        migrate = importlib.import_module('scripts.migrate_status_vocabulary')

        if True:
            store = new_store()
            await store.create_task('t1', 's1', '问个问题')
            await store.update_task('t1', status='answered')
            await store.create_task('t2', 's1', '存成网页')
            await store.update_task('t2', status='local_ready')
            await store.create_task('t3', 's1', '部署')
            await store.update_task('t3', status='deploy_needs_check')
            await store.create_run('r1', 't1', status='succeeded')
            await store.create_run('r2', 't2', status='succeeded')
            await store.create_run('r3', 't3', status='failed')      # 当年被折成失败
            await store.close()

            # 迁移脚本现在只认 PostgreSQL（SQLite 实现已删除）
            with patch.object(migrate, 'settings',
                              NS(database_url=PG_DSN)):
                self.assertEqual(migrate.main(['--apply']), 0)

            store = new_store()
            self.assertEqual((await store.task('t1'))['status'], 'succeeded')
            self.assertEqual((await store.task('t2'))['status'], 'succeeded')
            self.assertEqual((await store.task('t3'))['status'], 'needs_reconcile')
            # 部署待确认不是失败：对应 run 要纠正回来
            self.assertEqual((await store.run('r3'))['status'], 'needs_reconcile')
            await store.close()


class DependencyDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    """服务端缺依赖 vs 我们调度错：失败时要能分清并给出下一步。"""

    def test_dependency_signs_are_recognised(self):
        from webpage_agent.tools import core as tool_module
        note = tool_module._dependency_note('Piper failed (exit 1): ValueError: Unable to find voice: en_US-lessac-medium')
        self.assertIn('语音模型', note)
        self.assertIn('ffmpeg', tool_module._dependency_note('FileNotFoundError: ffmpeg: not found'))
        self.assertEqual(tool_module._dependency_note('项目不存在'), '')

    async def test_business_failure_carries_a_dependency_hint(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_adapter, mcp_servers
        from webpage_agent.tools.core import Tool

        server = mcp_servers.Server(id='montage', transport='streamable_http',
                                    url='https://a.example/mcp')

        async def fake_call(name, arguments, artifact=None, server=None):
            raise mcp_adapter.MCPToolFailure(
                'Piper failed (exit 1): Unable to find voice: en_US-lessac-medium',
                'E_UNKNOWN')

        tool = Tool('mcp__run_tool', 'MCP·run_tool', '', {'type': 'object'}, None,
                    tool_key='mcp:montage:run_tool', effect='write',
                    capability='mcp', server_id='montage')
        tool = Tool(tool.name, tool.label, tool.description, tool.args,
                    tool_module._make_mcp_runner('run_tool', server),
                    tool_key=tool.tool_key, effect=tool.effect,
                    capability=tool.capability, server_id=tool.server_id)
        plan = {'steps': [{'tool': 'mcp__run_tool', 'step_id': 's1',
                           'args': {'name': 'tts_selector', 'inputs': {}}}]}
        with patch.object(tool_module, 'find_tool', AsyncMock(return_value=tool)), \
                patch.object(tool_module.mcp_adapter, 'call_tool', fake_call), \
                patch_all_emits(lambda e: None):
            result = await nodes.run_step({'plan': plan, 'step_index': 0,
                                           'user_input': '做个视频'})
        self.assertEqual(result['status'], 'failed')
        # 既要服务端原话，也要"这是环境问题"的下一步
        self.assertIn('Unable to find voice', result['failure'])
        self.assertIn('语音模型', result['failure'])

    async def test_tool_hints_reach_the_planner_description(self):
        """本机特有用法（如 TTS 模型绝对路径）由配置提供，不靠模型猜。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_servers

        server = mcp_servers.Server(
            id='montage', transport='streamable_http', url='https://a.example/mcp',
            capability_tool='list_capabilities', tool_name_arg='name',
            tool_hints={'piper_tts': 'model 传 /data/voices/zh.onnx'})

        async def fake_list(_server):
            return [{'name': 'list_capabilities', 'inputSchema': {'type': 'object'}},
                    {'name': 'run_tool', 'inputSchema': {'type': 'object'}}]

        async def fake_call(name, arguments, artifact=None, server=None):
            return {'tools': [{'name': 'piper_tts', 'status': 'ToolStatus.AVAILABLE'}]}

        with patch.object(tool_module.mcp_adapter, 'list_server_tools', fake_list), \
                patch.object(tool_module.mcp_adapter, 'call_tool', fake_call), \
                patch.object(tool_module.mcp_servers, 'servers', lambda: [server]):
            tool_module._MCP_CACHE.update(at=0.0, tools=[], signature=(),
                                          capabilities={})
            tools = await tool_module.mcp_tools(force=True)
        run_tool = next(t for t in tools if t.name.endswith('run_tool'))
        self.assertIn('/data/voices/zh.onnx', run_tool.description)
        tool_module._MCP_CACHE['capabilities'] = {}


if __name__ == '__main__':
    unittest.main()
