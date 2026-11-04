"""迁移通道守卫：`SCHEMA` 只服务全新库，存量库的变更必须走 `UPGRADES`。

为什么单独一组：这条通道的失效方式是**静默**的。往 `SCHEMA` 里加一列，
全新库能建出来、测试也能过（测试库往往是新建的），只有已经记录过 `v1_baseline`
的存量库拿不到那一列，而且不报错。等到某次读写碰到它，才在一个和"迁移"
毫无关系的地方炸。所以这里既要测"新 UPGRADES 能补列"，也要测"只改 SCHEMA
会被当场抓出来"。
"""
import unittest
import unittest.mock
import uuid

import psycopg
from psycopg.conninfo import make_conninfo

from webpage_agent.store import migrations
from webpage_agent.store.pg_store import MIGRATIONS, SCHEMA
from tests.exec_env import PG_DSN, valid_test_dsn


def _dsn_in_schema(schema: str) -> str:
    return make_conninfo(PG_DSN, options='-csearch_path=' + schema)


class SchemaSnapshotParsingTests(unittest.TestCase):
    """SCHEMA 解析器本身：它错了，护栏就是假的。"""

    def test_parse_collects_tables_from_create_and_alter(self):
        parsed = migrations.parse_schema(SCHEMA)
        # 两张只在 ALTER 里出现新列的表的列也要在快照里
        self.assertIn('intent_version', parsed['runs'])
        self.assertIn('scene_context_json', parsed['tasks_meta'])
        self.assertIn('run_id', parsed['runs'])

    def test_parse_strips_comments_and_keeps_constraint_lines_out(self):
        parsed = migrations.parse_schema(SCHEMA)
        self.assertNotIn('PRIMARY', parsed['runs'])
        self.assertNotIn('--', parsed['runs'])

    def test_parse_handles_types_with_parentheses(self):
        sql = ('CREATE TABLE t (\n  id BIGSERIAL PRIMARY KEY,\n'
               '  amount NUMERIC(10, 2) NOT NULL,\n  note TEXT\n);\n'
               'ALTER TABLE t ADD COLUMN IF NOT EXISTS flag INTEGER NOT NULL DEFAULT 0;')
        parsed = migrations.parse_schema(sql)
        self.assertEqual(parsed['t'], {'id', 'amount', 'note', 'flag'})


class MigrationChannelTests(unittest.IsolatedAsyncioTestCase):
    """每条用例一个独立 schema，互不干扰，也不碰测试库的公共表。"""

    async def asyncSetUp(self):
        if not valid_test_dsn(PG_DSN):
            self.skipTest('PG_TEST_DSN required')
        self.schema = 'migchannel_' + uuid.uuid4().hex
        async with await psycopg.AsyncConnection.connect(PG_DSN, autocommit=True) as conn:
            await conn.execute('CREATE SCHEMA ' + self.schema)
        self.dsn = _dsn_in_schema(self.schema)

    async def asyncTearDown(self):
        async with await psycopg.AsyncConnection.connect(PG_DSN, autocommit=True) as conn:
            await conn.execute('DROP SCHEMA ' + self.schema + ' CASCADE')

    async def _columns(self, table: str) -> set[str]:
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            cur = await conn.execute(
                'SELECT column_name FROM information_schema.columns '
                'WHERE table_schema=%s AND table_name=%s', (self.schema, table))
            return {row[0] for row in await cur.fetchall()}

    async def _applied(self) -> set[str]:
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            cur = await conn.execute('SELECT migration_id FROM schema_migrations')
            return {row[0] for row in await cur.fetchall()}

    #: 只在 SCHEMA 的 ALTER 里出现的列：旧库根本没有它
    DROPPED_COLUMN = "ALTER TABLE runs ADD COLUMN IF NOT EXISTS observation_seq INTEGER NOT NULL DEFAULT 0;"

    async def _make_old_database(self) -> None:
        """造一个"只跑过基线"的旧库，结构与今天的 SCHEMA 只差一列。

        做法是拿今天的 SCHEMA 建库、但**抹掉**那条给 `runs` 补
        `observation_seq` 的 ALTER，然后登记 `v1_baseline`。这就是真实存量库的
        样子：那一列只写在 SCHEMA 里，对已记录基线的库永远不执行——本项要修的
        正是这种静默丢失。其余表列齐全，所以护栏不会因为"整库都缺"而报错。
        """
        old_schema = SCHEMA.replace(self.DROPPED_COLUMN, '')
        self.assertNotEqual(old_schema, SCHEMA, '夹具失效：SCHEMA 里找不到要抹掉的那条 ALTER')
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute(old_schema)
            await conn.execute(
                "INSERT INTO schema_migrations VALUES (%s, 'old') "
                'ON CONFLICT DO NOTHING', (migrations.BASELINE,))

    async def test_new_upgrade_reaches_a_database_that_only_ran_the_baseline(self):
        await self._make_old_database()
        self.assertNotIn('observation_seq', await self._columns('runs'))

        extra = ('v_test_add_observation_seq',
                 'ALTER TABLE runs ADD COLUMN observation_seq INTEGER NOT NULL DEFAULT 0;')
        with unittest.mock.patch.object(migrations, 'UPGRADES',
                                      (*migrations.UPGRADES, extra)):
            async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
                changed = await migrations.apply(conn, baseline=SCHEMA,
                                                 legacy_ids=MIGRATIONS)

        self.assertIn('v_test_add_observation_seq', changed)
        self.assertNotIn(migrations.BASELINE, changed)   # 基线没被重跑
        self.assertIn('observation_seq', await self._columns('runs'))
        self.assertIn('v_test_add_observation_seq', await self._applied())

    async def test_guard_names_the_column_when_schema_drifted_past_upgrades(self):
        """只改 SCHEMA、不写 UPGRADES：护栏必须报出 `表.列`，而不是放过。"""
        await self._make_old_database()
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            with self.assertRaises(migrations.SchemaDrift) as caught:
                await migrations.apply(conn, baseline=SCHEMA,
                                       legacy_ids=MIGRATIONS)
        self.assertEqual(caught.exception.missing, ['runs.observation_seq'])
        # 报的是具体列名，运维据此就能写出补列的那条 UPGRADES
        self.assertIn('runs.observation_seq', str(caught.exception))

    async def test_guard_lists_every_missing_column_not_just_the_first(self):
        """多列漂移要一次报全：只报第一列会让"补齐"变成反复试错。"""
        second = ("ALTER TABLE runs ADD COLUMN IF NOT EXISTS owner_role TEXT "
                  "NOT NULL DEFAULT 'member';")
        old_schema = SCHEMA.replace(self.DROPPED_COLUMN, '').replace(second, '')
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute(old_schema)
            with self.assertRaises(migrations.SchemaDrift) as caught:
                await migrations.verify_schema(conn, SCHEMA)
        self.assertEqual(caught.exception.missing,
                         ['runs.observation_seq', 'runs.owner_role'])

    async def test_guard_reports_a_whole_missing_table(self):
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            with self.assertRaises(migrations.SchemaDrift) as caught:
                await migrations.verify_schema(conn, SCHEMA)
        self.assertIn('sessions（整表缺失）', caught.exception.missing)

    async def test_fresh_database_passes_the_guard(self):
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await migrations.apply(conn, baseline=SCHEMA, legacy_ids=MIGRATIONS)
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            report = await migrations.verify_schema(conn, SCHEMA)
            await migrations.require_current(conn)
        self.assertGreater(report['tables'], 30)
        self.assertGreater(report['columns'], 300)

    async def test_require_current_rejects_a_current_version_with_missing_columns(self):
        """版本号登记了但列不在——这正是旧实现查不出来的那种库。"""
        await self._make_old_database()
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            for key, _ in migrations.UPGRADES:
                await conn.execute(
                    "INSERT INTO schema_migrations VALUES (%s, 'old') "
                    'ON CONFLICT DO NOTHING', (key,))
            # 版本号"最新"，但 runs 依然缺列：老实现直接放行
            with self.assertRaises(migrations.SchemaDrift):
                await migrations.require_current(conn)

    async def test_require_current_still_rejects_an_old_version(self):
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute('CREATE TABLE schema_migrations ('
                               'migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
            with self.assertRaisesRegex(RuntimeError, 'scripts.migrate'):
                await migrations.require_current(conn)
