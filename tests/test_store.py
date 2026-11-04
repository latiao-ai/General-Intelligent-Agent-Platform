"""会话记忆、仓储契约、PostgreSQL 一致性与连接池。"""
import asyncio
import unittest

from unittest.mock import patch

from tests.core_support import PG_DSN, new_store, reset_db
from tests.exec_env import valid_test_dsn


class SessionMemoryTests(unittest.IsolatedAsyncioTestCase):
    """P2：滚动摘要 + 关键事实。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    async def test_parse_and_render_facts(self):
        from webpage_agent.core import memory
        self.assertEqual(memory.parse_facts('目标：减脂\n- 已否决: 节食\n\n'),
                         {'目标': '减脂', '已否决': '节食'})
        self.assertEqual(memory.render_facts({'目标': '减脂'}), '目标：减脂')

    async def test_needs_compaction_thresholds(self):
        from webpage_agent.core import memory
        few = [{'content': '短'} for _ in range(3)]
        self.assertFalse(memory.needs_compaction(few))
        many = [{'content': '短'} for _ in range(memory.MAX_MESSAGES + 1)]
        self.assertTrue(memory.needs_compaction(many))
        fat = [{'content': '很长的内容' * 400} for _ in range(6)]
        self.assertTrue(memory.needs_compaction(fat))

    async def test_compact_folds_old_messages_into_summary(self):
        from webpage_agent.core import memory
        from webpage_agent.core.schemas import SessionMemory
        store = new_store()
        for i in range(memory.MAX_MESSAGES + 4):
            await store.append_message('s1', 'user' if i % 2 == 0 else 'assistant',
                                 f'第{i}条消息')

        async def fake_call(schema, system, user, **kw):
            return SessionMemory(summary='压缩后的摘要', facts={'目标': '减脂'}), None

        with patch.object(memory, 'structured_call', fake_call):
            result = await memory.compact(store, 's1')
        self.assertTrue(result['compacted'])
        self.assertEqual(result['folded'], memory.MAX_MESSAGES + 4 - memory.KEEP_RECENT)
        saved = await store.memory('s1')
        self.assertEqual(saved['summary'], '压缩后的摘要')
        self.assertEqual(saved['facts'], {'目标': '减脂'})
        # 未摘要部分只剩最近几条，不会越压越少
        self.assertEqual(len(await store.messages_after('s1', saved['upto'])),
                         memory.KEEP_RECENT)

    async def test_compact_failure_is_silent(self):
        from webpage_agent.core import memory
        store = new_store()
        for i in range(memory.MAX_MESSAGES + 4):
            await store.append_message('s1', 'user', f'第{i}条')

        async def boom(*a, **k):
            raise RuntimeError('模型不可用')

        with patch.object(memory, 'structured_call', boom):
            result = await memory.compact(store, 's1')
        # 压缩是优化，失败不能把任务搞挂
        self.assertFalse(result['compacted'])
        self.assertEqual((await store.memory('s1'))['summary'], '')


class MemoryScopeTests(unittest.IsolatedAsyncioTestCase):
    """长期记忆：作用域、版本替代、遗忘墓碑（文档 §12）。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    def store(self):
        return new_store()

    async def test_value_update_supersedes_previous(self):
        store = self.store()
        await store.remember(scope_type='user', scope_id='default', subject='偏好',
                       predicate='回答', value='简洁')
        await store.remember(scope_type='user', scope_id='default', subject='偏好',
                       predicate='回答', value='详细')
        active = await store.recall('user', 'default')
        self.assertEqual([(m['value'], m['version']) for m in active],
                         [('详细', 2)])

    async def test_scopes_do_not_leak(self):
        store = self.store()
        await store.remember(scope_type='user', scope_id='u1', subject='偏好',
                       predicate='回答', value='简洁')
        self.assertEqual(await store.recall('user', 'u2'), [])

    async def test_forget_blocks_regeneration_from_same_source(self):
        """只删不拦没用：后台任务会把已删事实重新提取回来。"""
        store = self.store()
        await store.remember(scope_type='user', scope_id='default', subject='电话',
                       predicate='号码', value='139')
        await store.forget(scope_type='user', scope_id='default', subject='电话',
                     predicate='号码')
        self.assertEqual(await store.recall('user', 'default'), [])
        self.assertTrue(await store.is_forgotten('user', 'default', '电话', '号码'))
        # 墓碑生效：同一事实不能再被写回来
        self.assertIsNone(await store.remember(scope_type='user', scope_id='default',
                                         subject='电话', predicate='号码',
                                         value='139'))


class StoreTests(unittest.IsolatedAsyncioTestCase):
    """SQLite 持久化层。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    @staticmethod
    def store():
        return new_store()

    async def test_messages_roundtrip(self):
        store = self.store()
        await store.append_message('sess1', 'user', '你好')
        await store.append_message('sess1', 'assistant', '收到')
        await store.append_message('sess2', 'user', '另一个会话')
        self.assertEqual([m['role'] for m in await store.messages('sess1')],
                         ['user', 'assistant'])
        self.assertEqual(len(await store.messages('sess2')), 1)

    async def test_list_sessions_orders_by_recent_and_uses_first_user_message(self):
        store = self.store()
        await store.append_message('s1', 'user', '帮我查一下 SQLite 的适用场景和限制以及和 Postgres 的区别')
        await store.append_message('s1', 'assistant', '好的')
        await store.append_message('s2', 'user', '你好')
        sessions = await store.list_sessions()
        self.assertEqual([s['session_id'] for s in sessions], ['s2', 's1'])
        by_id = {s['session_id']: s for s in sessions}
        self.assertTrue(by_id['s1']['title'].startswith('帮我查一下'))
        self.assertTrue(by_id['s1']['title'].endswith('…'))
        self.assertEqual(len(by_id['s1']['title']), 25)   # 24 字 + 省略号
        self.assertEqual(by_id['s1']['message_count'], 2)
        self.assertEqual(by_id['s2']['title'], '你好')
        # 只有助手消息（没有用户消息）时回退到「新会话」。
        store2 = self.store()
        await store2.append_message('sess-empty', 'assistant', '请问有什么可以帮您？')
        self.assertEqual((await store2.list_sessions())[0]['title'], '新会话')

    async def test_task_lifecycle_with_json_state(self):
        store = self.store()
        await store.create_task('t1', 'sess1', '做点事')
        self.assertEqual((await store.task('t1'))['status'], 'running')
        await store.update_task('t1', status='local_ready', artifact_id='a1',
                          state={'timings': [{'stage': 'parse_request'}]})
        data = await store.task('t1')
        self.assertEqual(data['status'], 'local_ready')
        self.assertEqual(data['artifact_id'], 'a1')
        self.assertEqual(data['state']['timings'][0]['stage'], 'parse_request')
        self.assertEqual(len(await store.tasks_of('sess1')), 1)

    async def test_event_seq_is_monotonic_and_isolated_per_task(self):
        store = self.store()
        first = [await store.append_event('t1', {'type': 'action', 'label': f'e{i}'})
                 for i in range(3)]
        self.assertEqual(first, [1, 2, 3])
        # 序号按任务独立计数，订阅去重依赖这个性质。
        self.assertEqual(await store.append_event('t2', {'type': 'action'}), 1)
        stored = await store.events('t1')
        self.assertEqual([seq for seq, _ in stored], [1, 2, 3])
        self.assertEqual(stored[1][1]['label'], 'e1')

    async def test_orphan_running_task_marked_interrupted_on_reopen(self):
        # 不再需要临时目录：库由 PG_TEST_DSN 指向的一次性测试库提供，
        # 这里的 with TemporaryDirectory 是 SQLite 时代的残留，folder 从未被使用。
        first = new_store()
        await first.create_task('t1', 's1', '中断前的任务')
        await first.close()
        # 重新打开：遗留的 running 任务必须被标记，否则前端会一直等下去。
        second = new_store()
        self.assertEqual((await second.task('t1'))['status'], 'interrupted')
        self.assertEqual(second.interrupted, 1)
        await second.close()

    async def test_missing_task_returns_none(self):
        self.assertIsNone(await self.store().task('nope'))


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 一致性测试')
class PostgresParityTests(unittest.IsolatedAsyncioTestCase):
    """在**真实 PostgreSQL** 上验证仓储实现。

    ⚠️ 每个用例开头都会 TRUNCATE 全部业务表——**PG_TEST_DSN 必须指向一次性
    测试库**（如 agent_platform_test），绝不能指向生产库。曾经因为拿生产库
    跑这套用例，把刚迁好的数据清空过一次。
    """

    async def asyncSetUp(self):
        from webpage_agent.store.pg_store import PostgresStore
        if not valid_test_dsn(PG_DSN):
            raise unittest.SkipTest('PG_TEST_DSN 必须指向测试库（库名含 test）')
        self.store = PostgresStore(PG_DSN)
        tables = ('memory_tombstones', 'memories', 'background_jobs', 'artifacts',
                  'action_attempts', 'actions', 'steps', 'plans', 'runs',
                  'tasks_meta', 'events', 'tasks', 'session_memory',
                  'session_summaries', 'messages', 'sessions')
        # 走公开的租借接口做清库：仓储内部怎么管连接（单连接还是连接池）
        # 属于实现细节，用例不该依赖
        async with self.store._lease() as conn:
            for table in tables:
                await conn.execute(f'TRUNCATE TABLE {table} CASCADE')

    async def asyncTearDown(self):
        await self.store.close()

    async def test_messages_and_sessions(self):
        store = self.store
        await store.append_message('s1', 'user', '帮我查一下 SQLite 的适用场景和限制以及和 Postgres 的区别')
        await store.append_message('s1', 'assistant', '好的')
        await store.append_message('s2', 'user', '你好')
        self.assertEqual([m['role'] for m in await store.messages('s1')],
                         ['user', 'assistant'])
        sessions = await store.list_sessions()
        self.assertEqual([s['session_id'] for s in sessions], ['s2', 's1'])
        self.assertEqual(sessions[1]['message_count'], 2)
        self.assertTrue(sessions[1]['title'].endswith('…'))
        self.assertEqual(len(await store.messages_after('s1', 0)), 2)

    async def test_memory_and_tombstone(self):
        store = self.store
        await store.set_memory('s1', summary='摘要', facts={'目标': '减脂'}, upto=5)
        self.assertEqual((await store.memory('s1'))['facts'], {'目标': '减脂'})
        await store.clear_memory('s1')
        self.assertEqual((await store.memory('s1'))['summary'], '')

        await store.remember(scope_type='user', scope_id='default', subject='偏好',
                       predicate='回答', value='简洁')
        await store.remember(scope_type='user', scope_id='default', subject='偏好',
                       predicate='回答', value='详细')
        self.assertEqual([(m['value'], m['version'])
                          for m in await store.recall('user', 'default')], [('详细', 2)])
        await store.forget(scope_type='user', scope_id='default', subject='偏好',
                     predicate='回答')
        self.assertEqual(await store.recall('user', 'default'), [])
        self.assertIsNone(await store.remember(scope_type='user', scope_id='default',
                                         subject='偏好', predicate='回答',
                                         value='又回来'))

    async def test_runs_leases_and_actions(self):
        store = self.store
        await store.create_task('t1', 's1', '做点事')
        await store.ensure_task_meta('t1', session_id='s1', goal='做点事')
        await store.create_run('r1', 't1')
        first = await store.claim_run('w1', lease_seconds=30)
        self.assertEqual(first['lease_epoch'], 1)
        self.assertIsNone(await store.claim_run('w2'))
        self.assertEqual((await store.claim_run('w2', now_ts=10 ** 12))['lease_epoch'], 2)
        self.assertFalse(await store.renew_lease('r1', 'w1'))

        plan = {'steps': [{'step_id': 's1', 'tool': 'web_search',
                           'tool_key': 'local:web:search', 'args': {}}]}
        await store.save_plan('r1', 1, goal='g', steps=plan['steps'], reason='why')
        await store.save_steps('r1', 1, plan['steps'])
        self.assertEqual((await store.plan('r1'))['steps'][0]['step_id'], 's1')
        self.assertEqual(len(await store.steps_of('r1')), 1)

        await store.create_action('a1', run_id='r1', plan_version=1,
                                  step_id='s1', tool_key='local:web:search',
                                  args_hash='h', idempotency_key='k1')
        self.assertEqual((await store.find_action('k1'))['action_id'], 'a1')
        await store.update_action('a1', status='in_flight')
        await store.add_attempt('att1', 'a1')
        await store.finish_attempt('att1', error_type='None')
        self.assertEqual(len(await store.attempts_of('a1')), 1)
        self.assertEqual(len(await store.actions_of('r1')), 1)

    async def test_reconcile_and_jobs(self):
        store = self.store
        await store.create_task('t1', 's1', 'g')
        await store.ensure_task_meta('t1', session_id='s1', goal='g')
        await store.create_run('r1', 't1', status='running')
        await store.create_action('a1', run_id='r1', plan_version=1, step_id='s1',
                            tool_key='t', args_hash='h', idempotency_key='k1')
        await store.update_action('a1', status='in_flight')
        stats = await store.reconcile_orphan_runs()
        self.assertEqual(stats['unknown_actions'], 1)
        self.assertEqual((await store.action('a1'))['status'], 'unknown')
        self.assertEqual((await store.run('r1'))['status'], 'reconciling')

        self.assertTrue(await store.enqueue_job('j1', 'session_summary', 's1:r1',
                                          {'session_id': 's1'}))
        self.assertFalse(await store.enqueue_job('j2', 'session_summary', 's1:r1',
                                           {'session_id': 's1'}))
        job = await store.claim_job('w1')
        self.assertEqual(job['kind'], 'session_summary')
        await store.finish_job(job['job_id'])
        self.assertEqual((await store.job(job['job_id']))['status'], 'done')

    async def test_artifacts_and_events(self):
        store = self.store
        await store.create_task('t1', 's1', 'g')
        await store.add_artifact('art1', run_id='r1', kind='sources',
                           storage_key='/tmp/x.html', size=245823, sha256='abc')
        meta = await store.artifact('art1')
        self.assertEqual(meta['kind'], 'sources')
        self.assertEqual(await store.append_event('t1', {'type': 'action'}), 1)
        self.assertEqual(await store.append_event('t1', {'type': 'action'}), 2)
        self.assertEqual([s for s, _ in await store.events('t1')], [1, 2])

    async def test_task_roundtrip(self):
        store = self.store
        await store.create_task('t1', 's1', '消息')
        self.assertEqual((await store.task('t1'))['status'], 'running')
        await store.update_task('t1', status='local_ready', artifact_id='a1',
                          state={'timings': [{'stage': 'plan'}]})
        data = await store.task('t1')
        self.assertEqual(data['status'], 'local_ready')
        self.assertEqual(data['state']['timings'][0]['stage'], 'plan')


class RepositoryContractTests(unittest.TestCase):
    """仓储接口：换实现前先保证方法齐备（文档 §9.1）。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    def test_store_satisfies_repository_protocol(self):
        from webpage_agent.store.persistence import Repository, check_repository
        store = new_store()
        self.assertEqual(check_repository(store), [])
        self.assertIsInstance(store, Repository)

    def test_missing_methods_are_reported(self):
        from webpage_agent.store.persistence import check_repository

        class Partial:
            def append_message(self, *a, **k): ...

        self.assertIn('claim_run', check_repository(Partial()))


class AsyncDriverTests(unittest.IsolatedAsyncioTestCase):
    """仓储接口必须是异步的：同步 DB 调用会卡住整个事件循环。

    项目只支持 PostgreSQL（SQLite 实现已删除），所以这里只校验 PG 侧契约。
    """

    def test_repository_protocol_is_async(self):
        import inspect
        from webpage_agent.store.persistence import Repository
        sync_methods = [name for name in dir(Repository)
                        if not name.startswith('_')
                        and callable(getattr(Repository, name, None))
                        and not inspect.iscoroutinefunction(getattr(Repository, name))]
        self.assertEqual(sync_methods, [])

    def test_only_postgres_is_supported(self):
        """没有 DATABASE_URL、或不是 PG 连接串时直接报错，不悄悄退回去。"""
        from webpage_agent.store.persistence import open_store
        with self.assertRaises(RuntimeError):
            open_store('')
        with self.assertRaises(RuntimeError):
            open_store('sqlite:///tmp/agent.db')

    async def test_postgres_uses_async_pool_not_a_sync_connection(self):
        """PG 侧用的是 psycopg 的**异步**连接池，不是同步连接 + 线程。"""
        import inspect
        from psycopg_pool import AsyncConnectionPool
        from webpage_agent.store.persistence import check_repository
        store = new_store()
        try:
            self.assertIsInstance(store._pool, AsyncConnectionPool)
            self.assertFalse(hasattr(store, '_conn'), '不该再有单连接字段')
            self.assertEqual(check_repository(store), [])
            self.assertTrue(inspect.iscoroutinefunction(store.append_message))
            self.assertTrue(inspect.iscoroutinefunction(store.close))
        finally:
            await store.close()

    async def test_event_loop_is_not_blocked_by_store_calls(self):
        """写库期间事件循环还能跑别的协程（同步实现做不到这一点）。"""
        store = new_store()
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(50):
                ticks += 1
                await asyncio.sleep(0)

        await asyncio.gather(
            ticker(),
            *(store.append_message('s1', 'user', f'm{i}') for i in range(20)))
        self.assertGreater(ticks, 10)
        self.assertEqual(len(await store.messages('s1')), 20)
        await store.close()


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class PostgresPoolTests(unittest.IsolatedAsyncioTestCase):
    """连接池：多线程真并发、异常不掉连接、池状态可观测。

    ⚠️ 同样只对一次性测试库跑（TRUNCATE 全部业务表）。
    """

    async def asyncSetUp(self):
        from webpage_agent.store.pg_store import PostgresStore
        if not valid_test_dsn(PG_DSN):
            raise unittest.SkipTest('PG_TEST_DSN 必须指向测试库（库名含 test）')
        self.store = PostgresStore(PG_DSN)
        tables = ('memory_tombstones', 'memories', 'background_jobs', 'artifacts',
                  'action_attempts', 'actions', 'steps', 'plans', 'runs',
                  'tasks_meta', 'events', 'tasks', 'session_memory',
                  'session_summaries', 'messages', 'sessions')
        async with self.store._lease() as conn:
            for table in tables:
                await conn.execute(f'TRUNCATE TABLE {table} CASCADE')

    async def asyncTearDown(self):
        await self.store.close()

    async def test_store_is_pooled_not_single_connection(self):
        self.assertFalse(hasattr(self.store, '_conn'),
                         '单连接字段应当已经不存在')
        stats = self.store.pool_stats()
        self.assertGreaterEqual(stats['max_size'], 2)
        self.assertEqual(stats['available'] + 0, stats['size'])
        # 仓储接口没被破坏
        from webpage_agent.store.persistence import check_repository
        self.assertEqual(check_repository(self.store), [])

    async def test_concurrent_writers_get_unique_event_seqs(self):
        """8 个协程同时往同一个任务写事件：seq 必须是连续且不重复的 1..N。

        seq 重复意味着 SSE 端会按序号把后到的事件当重复丢掉（静默丢消息），
        所以必须靠数据库保证：`pg_advisory_xact_lock` 按任务串行化 + 冲突重算兜底。
        异步驱动下"并发"不再是线程，而是同一事件循环里交错的多个事务——竞争
        照样存在（各自从池里借连接、同时算 MAX(seq)），这才是真实的并发形态。
        """
        task_id = 't-concurrent'
        await self.store.create_task(task_id, 's1', '并发写')
        total, per_worker = 8, 15

        async def worker(index: int) -> None:
            for n in range(per_worker):
                await self.store.append_event(
                    task_id, {'type': 'action', 'label': f'w{index}-{n}'})

        await asyncio.gather(*(worker(i) for i in range(total)))

        events = await self.store.events(task_id)
        self.assertEqual(len(events), total * per_worker)
        self.assertEqual([seq for seq, _ in events],
                         list(range(1, total * per_worker + 1)))

    async def test_concurrent_reads_and_writes_during_pool_load(self):
        """读写混跑：多个协程同时读和写，全部成功、数据不丢、连接不被吃光。"""
        await self.store.ensure_session('s1')

        async def writer(index: int) -> None:
            for n in range(10):
                await self.store.append_message('s1', 'user', f'w{index}-{n}')

        async def reader() -> None:
            for _ in range(40):
                await self.store.list_sessions(10)

        await asyncio.gather(*(writer(i) for i in range(4)),
                             *(reader() for _ in range(4)))
        self.assertEqual(len(await self.store.messages('s1')), 40)
        stats = self.store.pool_stats()
        self.assertLessEqual(stats['size'], stats['max_size'])
        self.assertEqual(stats['requests_waiting'], 0)

    async def test_failed_statement_rolls_back_and_returns_the_connection(self):
        """事务失败不能吃掉连接，也不能留下半截写入。"""
        await self.store.ensure_session('s1')
        before = self.store.pool_stats()['available']
        with self.assertRaises(Exception):
            async with self.store._lease() as conn:
                await conn.execute("INSERT INTO sessions (session_id, created_at, "
                                   "updated_at) VALUES ('s-broken', 'x', 'x')")
                await conn.execute('SELECT * FROM no_such_table')   # 触发回滚
        # 连接已经归还，池没有被吃掉
        self.assertEqual(self.store.pool_stats()['available'], before)
        self.assertNotIn('s-broken', [s['session_id']
                                      for s in await self.store.list_sessions(10)])
        self.assertLessEqual(self.store.pool_stats()['size'],
                             self.store.pool_stats()['max_size'])

    async def test_async_store_does_not_block_the_event_loop(self):
        """DB 调用是异步的：等待期间事件循环还能跑别的协程。

        这条盯的是"换 async 驱动"的目的本身——旧实现在事件循环线程里同步调用，
        一次慢查询就把所有 SSE 连接和其他任务一起冻住。
        """
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(50):
                ticks += 1
                await asyncio.sleep(0)

        await self.store.ensure_session('s1')
        await asyncio.gather(
            ticker(),
            *(self.store.append_message('s1', 'user', f'm{i}') for i in range(20)))
        # 每写一条都会让出控制权，ticker 才能插进来跑这么多次
        self.assertGreater(ticks, 10)
        self.assertEqual(len(await self.store.messages('s1')), 20)


if __name__ == '__main__':
    unittest.main()
