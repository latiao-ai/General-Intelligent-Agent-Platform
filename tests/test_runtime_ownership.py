"""第七阶段：run 引擎归属持久化（runtime_kind / runtime_version / runtime_state_schema）。

验证：
- 新 run 创建时确定归属，默认 LangGraph；
- 显式指定 pi 时按 pi 的版本/schema 落库；
- 恢复读取已存归属；未知引擎 / 不兼容版本明确阻断，不回退默认引擎。

依赖真实 PostgreSQL（PG_TEST_DSN，库名含 test）。
"""
import unittest

from tests.core_support import PG_DSN, new_store, reset_db, valid_test_dsn


def _require_pg(testcase):
    if not valid_test_dsn(PG_DSN):
        testcase.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')


class RuntimeOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def _seed_task(self, task_id='t1'):
        await self.store.create_task(task_id, 's1', '测试')

    async def test_default_kind_is_langgraph(self):
        """显式 langgraph 仍按图的版本落库（排空前的恢复身份）。"""
        from webpage_agent.agent import runtime_identity
        await self._seed_task()
        await self.store.create_run('r1', 't1', status='queued',
                                    runtime_kind='langgraph')
        run = await self.store.run('r1')
        self.assertEqual(run['runtime_kind'], 'langgraph')
        self.assertEqual(run['runtime_version'],
                         runtime_identity.LANGGRAPH_RUNTIME_VERSION)
        self.assertEqual(run['runtime_state_schema'],
                         runtime_identity.LANGGRAPH_STATE_SCHEMA)

    async def test_configured_pi_default_persists_pi_identity(self):
        """DEFAULT_RUNTIME_KIND=pi 时，新建 run 的引擎身份是 pi，而不是图。"""
        from webpage_agent.agent import runtime_identity
        from tests.exec_env import override_settings
        await self._seed_task('t-pi')
        with override_settings(default_runtime_kind='pi'):
            self.assertEqual(runtime_identity.default_kind(), 'pi')
            await self.store.create_run('r-pi', 't-pi', status='queued')
        run = await self.store.run('r-pi')
        self.assertEqual(run['runtime_kind'], 'pi')
        self.assertEqual(run['runtime_version'], runtime_identity.PI_RUNTIME_VERSION)
        self.assertEqual(run['runtime_state_schema'],
                         runtime_identity.PI_STATE_SCHEMA)

    async def test_explicit_pi_kind(self):
        from webpage_agent.agent import runtime_identity
        await self._seed_task()
        await self.store.create_run('r1', 't1', status='queued',
                                    runtime_kind='pi')
        run = await self.store.run('r1')
        self.assertEqual(run['runtime_kind'], 'pi')
        self.assertEqual(run['runtime_version'], runtime_identity.PI_RUNTIME_VERSION)
        self.assertEqual(run['runtime_state_schema'], runtime_identity.PI_STATE_SCHEMA)

    async def test_runtime_identity_roundtrip(self):
        await self._seed_task()
        await self.store.create_run('r1', 't1', status='queued', runtime_kind='pi')
        ident = await self.store.runtime_identity('r1')
        self.assertEqual(ident['kind'], 'pi')

    async def test_unknown_kind_blocked(self):
        from webpage_agent.agent import runtime_identity
        await self._seed_task()
        await self.store.create_run('r1', 't1', status='queued')
        # 直接写一个未知 kind，模拟坏数据
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE runs SET runtime_kind='bogus' WHERE run_id='r1'")
            await conn.commit()
        with self.assertRaises(runtime_identity.UnknownRuntimeKind):
            await self.store.runtime_identity('r1')

    async def test_incompatible_version_blocked(self):
        from webpage_agent.agent import runtime_identity
        await self._seed_task()
        await self.store.create_run('r1', 't1', status='queued')
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE runs SET runtime_version=999 WHERE run_id='r1'")
            await conn.commit()
        with self.assertRaises(runtime_identity.IncompatibleRuntimeVersion):
            await self.store.runtime_identity('r1')

    async def test_incompatible_schema_blocked(self):
        from webpage_agent.agent import runtime_identity
        await self._seed_task()
        await self.store.create_run('r1', 't1', status='queued')
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE runs SET runtime_state_schema=999 WHERE run_id='r1'")
            await conn.commit()
        with self.assertRaises(runtime_identity.IncompatibleRuntimeVersion):
            await self.store.runtime_identity('r1')

    async def test_legacy_run_backfilled_langgraph(self):
        """存量 run（迁移前没有这三列，default 回填 langgraph）正常恢复。"""
        await self._seed_task()
        await self.store.create_run('r1', 't1', status='queued')
        # 模拟迁移前的行：把三列设回 default 值（等价于回填 LangGraph）
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE runs SET runtime_kind='langgraph', "
                "runtime_version=1, runtime_state_schema=1 WHERE run_id='r1'")
            await conn.commit()
        ident = await self.store.runtime_identity('r1')
        self.assertEqual(ident['kind'], 'langgraph')
        self.assertEqual(ident['version'], 1)
        self.assertEqual(ident['state_schema'], 1)


if __name__ == '__main__':
    unittest.main()
