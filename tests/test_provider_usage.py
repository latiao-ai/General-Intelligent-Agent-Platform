"""按运行的 token 明细，以及单次运行的 token 闸。

假模型、真 PostgreSQL。断言对的是落账行和运行状态，不是「函数存在」。
"""
import asyncio
import csv
import io
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import psycopg
from openai import AuthenticationError
from psycopg.conninfo import make_conninfo

from webpage_agent.api.api_hub import Hub
from webpage_agent.core import services
from webpage_agent.core.config import settings
from webpage_agent.core.errors import BudgetExceeded
from webpage_agent.core.schemas import ExecutionPlan, ObservationDecision
from webpage_agent.governance import core as governance
from webpage_agent.governance import policies
from webpage_agent.store import migrations
from webpage_agent.store.pg_store import SCHEMA, MIGRATIONS
from tests.exec_env import (PG_DSN, Remote, install_main, install_tools,
                            new_store, reset_db, seed_run, valid_test_dsn)
from tests.test_runtime_closure import assigned


def _dsn(schema: str) -> str:
    return make_conninfo(PG_DSN, options='-csearch_path=' + schema)


class Chunk:
    """流式分片：带 tool_calls 和 usage_metadata，能被 structured_call 累加。"""

    def __init__(self, args, usage, name):
        self.tool_calls = [{'name': name, 'args': args}]
        self.tool_call_chunks = []
        self.usage_metadata = usage
        self.content = ''

    def __add__(self, other):
        return other


class Scripted:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def astream(self, messages):
        self.calls += 1
        for piece in self.rounds.pop(0):
            yield piece


def _new_llm(model):
    def factory(model_name=None, disable_thinking=False):
        return model
    return factory


class MigrationSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """存量库只跑 v4，不能靠重放 SCHEMA。跑完后列和索引要和新库一致。"""

    async def asyncSetUp(self):
        if not valid_test_dsn(PG_DSN):
            self.skipTest('PG_TEST_DSN required')
        self.names = []

    async def asyncTearDown(self):
        async with await psycopg.AsyncConnection.connect(PG_DSN, autocommit=True) as conn:
            for name in self.names:
                await conn.execute('DROP SCHEMA IF EXISTS ' + name + ' CASCADE')

    async def _schema(self) -> str:
        name = 'usage_' + uuid.uuid4().hex
        self.names.append(name)
        async with await psycopg.AsyncConnection.connect(PG_DSN, autocommit=True) as conn:
            await conn.execute('CREATE SCHEMA ' + name)
        return name

    async def _columns(self, schema: str) -> dict[str, set[str]]:
        async with await psycopg.AsyncConnection.connect(_dsn(schema)) as conn:
            cur = await conn.execute(
                'SELECT table_name, column_name FROM information_schema.columns '
                'WHERE table_schema=%s', (schema,))
            found: dict[str, set[str]] = {}
            for table, column in await cur.fetchall():
                found.setdefault(table, set()).add(column)
            return found

    async def _charge_indexes(self, schema: str) -> set[str]:
        async with await psycopg.AsyncConnection.connect(_dsn(schema)) as conn:
            cur = await conn.execute(
                'SELECT indexname FROM pg_indexes '
                'WHERE schemaname=%s AND tablename=%s',
                (schema, 'provider_charges'))
            return {row[0] for row in await cur.fetchall()}

    async def test_upgrade_from_v3_matches_a_fresh_database(self):
        parsed = migrations.parse_schema(SCHEMA)
        self.assertIn('purpose', parsed['provider_charges'])
        self.assertIn('tokens_estimated', parsed['provider_charges'])

        fresh = await self._schema()
        async with await psycopg.AsyncConnection.connect(_dsn(fresh)) as conn:
            changed = await migrations.apply(conn, baseline=SCHEMA, legacy_ids=MIGRATIONS)
            self.assertIn(migrations.USAGE_DETAIL, changed)
            await migrations.require_current(conn)

        old = await self._schema()
        async with await psycopg.AsyncConnection.connect(_dsn(old)) as conn:
            # 今天的 SCHEMA 已经带上新列。存量库的样子是：表在，但没有这两列、也没有按 run 的索引。
            await conn.execute(SCHEMA)
            await conn.execute('ALTER TABLE provider_charges DROP COLUMN purpose')
            await conn.execute('ALTER TABLE provider_charges DROP COLUMN tokens_estimated')
            await conn.execute('DROP INDEX idx_provider_charges_run')
            for key, sql in migrations.UPGRADES:
                if key == migrations.USAGE_DETAIL:
                    continue
                await conn.execute(sql)
                await conn.execute(
                    'INSERT INTO schema_migrations VALUES (%s, %s)',
                    (key, 'old'))
            await conn.execute(
                'INSERT INTO schema_migrations VALUES (%s, %s) ON CONFLICT DO NOTHING',
                (migrations.BASELINE, 'old'))
        self.assertNotIn('purpose', (await self._columns(old)).get('provider_charges', set()))
        self.assertNotIn('idx_provider_charges_run', await self._charge_indexes(old))

        async with await psycopg.AsyncConnection.connect(_dsn(old)) as conn:
            changed = await migrations.apply(conn, baseline=SCHEMA, legacy_ids=MIGRATIONS)
            self.assertEqual(changed, [migrations.USAGE_DETAIL])
            self.assertNotIn(migrations.BASELINE, changed)
            await migrations.require_current(conn)

        self.assertEqual(await self._columns(old), await self._columns(fresh))
        indexes = await self._charge_indexes(old)
        self.assertEqual(indexes, await self._charge_indexes(fresh))
        self.assertIn('idx_provider_charges_run', indexes)
        self.assertIn('purpose', (await self._columns(old))['provider_charges'])
        self.assertIn('tokens_estimated', (await self._columns(old))['provider_charges'])


class UsageLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not valid_test_dsn(PG_DSN):
            self.skipTest('PG_TEST_DSN required')
        await reset_db()
        self.store = new_store()
        await self.store.ready()
        async with self.store._lease() as conn:
            await conn.execute('TRUNCATE provider_charges, tenant_usage_days')

    async def asyncTearDown(self):
        await self.store.close()

    async def _rows(self, run_id: str) -> list[dict]:
        async with self.store._lease() as conn:
            return await self.store._all(
                conn, 'SELECT purpose, input_tokens, output_tokens, tokens_estimated, '
                'status, charged_micros, reserved_micros, provider FROM provider_charges '
                'WHERE run_id=%s ORDER BY created_at', (run_id,))

    async def test_two_requests_match_usage_metadata_and_purpose(self):
        plan_usage = {'input_tokens': 120, 'output_tokens': 30, 'total_tokens': 150}
        observe_usage = {'input_tokens': 40, 'output_tokens': 15, 'total_tokens': 55}
        model = Scripted([
            [Chunk({'steps': [{'tool': 'read'}]}, plan_usage, 'ExecutionPlan')],
            [Chunk({'kind': 'continue'}, observe_usage, 'ObservationDecision')],
        ])
        run_id = 'usage-run'
        await seed_run(self.store, run_id=run_id, task_id='usage-task', session_id='usage-sess')
        token = governance.bind(self.store, 'acme', run_id)
        try:
            with patch.object(services, 'new_llm', _new_llm(model)):
                plan, _ = await services.structured_call(ExecutionPlan, '规则', '规划')
                decision, _ = await services.structured_call(
                    ObservationDecision, '规则', '观察')
        finally:
            governance.unbind(token)
        self.assertEqual(plan.steps[0].tool, 'read')
        self.assertEqual(decision.kind, 'continue')
        self.assertEqual(model.calls, 2)

        rows = await self._rows(run_id)
        self.assertEqual([row['purpose'] for row in rows], ['plan', 'observe'])
        self.assertEqual(rows[0]['input_tokens'], plan_usage['input_tokens'])
        self.assertEqual(rows[0]['output_tokens'], plan_usage['output_tokens'])
        self.assertEqual(rows[1]['input_tokens'], observe_usage['input_tokens'])
        self.assertEqual(rows[1]['output_tokens'], observe_usage['output_tokens'])
        self.assertFalse(rows[0]['tokens_estimated'])
        self.assertFalse(rows[1]['tokens_estimated'])

        report = await self.store.run_usage(run_id)
        by_purpose = {item['purpose']: item for item in report['by_purpose']}
        self.assertEqual(by_purpose['plan']['requests'], 1)
        self.assertEqual(by_purpose['plan']['input_tokens'], plan_usage['input_tokens'])
        self.assertEqual(by_purpose['plan']['output_tokens'], plan_usage['output_tokens'])
        self.assertEqual(by_purpose['plan']['estimated_requests'], 0)
        self.assertEqual(by_purpose['observe']['input_tokens'], observe_usage['input_tokens'])
        self.assertEqual(by_purpose['observe']['output_tokens'], observe_usage['output_tokens'])
        self.assertEqual(report['total']['requests'], 2)
        self.assertEqual(report['total']['input_tokens'],
                         plan_usage['input_tokens'] + observe_usage['input_tokens'])
        self.assertEqual(report['total']['output_tokens'],
                         plan_usage['output_tokens'] + observe_usage['output_tokens'])
        consumed = (await self.store.run(run_id))['consumed']
        self.assertNotIn('tokens', consumed)
        self.assertNotIn('input_tokens', consumed)
        self.assertNotIn('output_tokens', consumed)

    async def test_schema_repair_is_tagged_separately_from_the_first_attempt(self):
        first = {'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15}
        second = {'input_tokens': 13, 'output_tokens': 6, 'total_tokens': 19}
        model = Scripted([
            [Chunk({'steps': []}, first, 'ExecutionPlan')],
            [Chunk({'steps': [{'tool': 'read'}]}, second, 'ExecutionPlan')],
        ])
        run_id = 'repair-run'
        token = governance.bind(self.store, 'acme', run_id)
        try:
            with patch.object(services, 'new_llm', _new_llm(model)):
                parsed, _ = await services.structured_call(ExecutionPlan, '规则', '规划')
        finally:
            governance.unbind(token)
        self.assertEqual(parsed.steps[0].tool, 'read')
        rows = await self._rows(run_id)
        self.assertEqual([row['purpose'] for row in rows], ['plan', 'repair'])
        self.assertEqual(rows[0]['input_tokens'], first['input_tokens'])
        self.assertEqual(rows[1]['output_tokens'], second['output_tokens'])

    async def test_failed_request_keeps_reservation_and_marks_estimate(self):
        error = AuthenticationError(
            'secret', body={}, response=httpx.Response(
                401, request=httpx.Request('POST', 'https://invalid')))
        model = Scripted([])

        async def astream(messages):
            model.calls += 1
            raise error
            yield  # pragma: no cover

        model.astream = astream
        price = 1_000_000
        run_id = 'fail-run'
        token = governance.bind(self.store, 'acme', run_id)
        try:
            with assigned(settings, tenant_budget_enabled=True,
                          tenant_daily_provider_requests=10,
                          tenant_daily_cost_micros=10**12,
                          llm_input_price_micros_per_million=price,
                          llm_output_price_micros_per_million=price), \
                    patch.object(services, 'new_llm', _new_llm(model)):
                with self.assertRaises(services.ModelConfigurationError):
                    await services.chat_reply('规则', [{'role': 'user', 'content': '问题'}])
        finally:
            governance.unbind(token)
        self.assertEqual(model.calls, 1)
        row = (await self._rows(run_id))[0]
        expected = (settings.llm_context_window * price
                    + settings.llm_output_tokens * price + 999_999) // 1_000_000
        self.assertEqual(row['status'], 'uncertain')
        self.assertIsNotNone(row['input_tokens'])
        self.assertIsNotNone(row['output_tokens'])
        self.assertEqual(row['input_tokens'], settings.llm_context_window)
        self.assertEqual(row['output_tokens'], settings.llm_output_tokens)
        self.assertTrue(row['tokens_estimated'])
        self.assertEqual(row['charged_micros'], row['reserved_micros'])
        self.assertEqual(row['charged_micros'], expected)
        report = await self.store.run_usage(run_id)
        self.assertEqual(report['total']['estimated_requests'], 1)
        self.assertEqual(report['by_purpose'][0]['purpose'], 'final_answer')

    async def test_search_records_purpose_without_inventing_model_tokens(self):
        from webpage_agent.integrations import search

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {'results': []}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json=None, headers=None):
                return FakeResponse()

        run_id = 'search-run'
        token = governance.bind(self.store, 'acme', run_id)
        try:
            with patch.object(search, 'httpx', SimpleNamespace(AsyncClient=FakeClient)), \
                    patch.object(search, 'settings', SimpleNamespace(
                        deepseek_api_key='sk-test',
                        deepseek_base_url='https://api.deepseek.com',
                        max_pages=2)):
                await search.search('查询')
                await search.fetch(['https://example.com/a'])
        finally:
            governance.unbind(token)
        rows = await self._rows(run_id)
        self.assertEqual({row['purpose'] for row in rows}, {'search', 'fetch'})
        self.assertTrue(all(row['input_tokens'] is None for row in rows))
        self.assertTrue(all(row['output_tokens'] is None for row in rows))
        self.assertTrue(all(row['tokens_estimated'] is False for row in rows))

    async def test_reserved_request_counts_toward_the_ceiling_without_locking(self):
        run_id = 'reserved-run'
        charge = 'charge-reserved'
        await self.store.reserve_provider_charge(
            charge, tenant_id='acme', run_id=run_id, provider='llm', model='m',
            cost_micros=10, max_requests=10, max_cost_micros=10**9,
            purpose='plan', enforce_quota=False)
        ceiling = settings.llm_context_window + settings.llm_output_tokens
        with assigned(settings, run_max_tokens=ceiling - 1):
            with self.assertRaises(BudgetExceeded) as caught:
                await self.store.enforce_run_token_budget(run_id)
        self.assertIn(str(ceiling), str(caught.exception))
        self.assertIn(str(ceiling - 1), str(caught.exception))
        self.assertNotIn('FOR UPDATE', governance._TOKEN_SPEND_SQL)
        self.assertNotIn('FOR UPDATE', governance._USAGE_SELECT)

        async with await psycopg.AsyncConnection.connect(PG_DSN) as locker:
            await locker.execute(
                'SELECT * FROM provider_charges WHERE charge_id=%s FOR UPDATE', (charge,))
            started = time.monotonic()
            report = await asyncio.wait_for(self.store.run_usage(run_id), timeout=2)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(report['total']['requests'], 1)
            await locker.rollback()

    async def test_usage_endpoints_reuse_owner_and_operator_checks(self):
        from webpage_agent.api import routes as api

        plan_usage = {'input_tokens': 8, 'output_tokens': 2, 'total_tokens': 10}
        model = Scripted([[Chunk({'steps': [{'tool': 'read'}]}, plan_usage, 'ExecutionPlan')]])
        run_id = 'api-run'
        await self.store.create_task('api-task', 'api-sess', '做一下')
        await self.store.ensure_task_meta(
            'api-task', session_id='api-sess', goal='做一下',
            tenant_id='acme', owner_user_id='alice')
        await self.store.create_run('api-run', 'api-task', tenant_id='acme',
                                    owner_user_id='alice')
        token = governance.bind(self.store, 'acme', run_id)
        try:
            with patch.object(services, 'new_llm', _new_llm(model)):
                await services.structured_call(ExecutionPlan, '规则', '规划')
        finally:
            governance.unbind(token)
        # 另一家企业的账不能混进导出
        await self.store.reserve_provider_charge(
            'other-tenant', tenant_id='other', run_id='other-run', provider='llm',
            model='m', cost_micros=1, max_requests=5, max_cost_micros=100,
            purpose='plan', enforce_quota=False)
        await self.store.settle_provider_charge(
            'other-tenant', actual_micros=1, tokens=(9, 9), status='succeeded')

        old = api.hub.store
        api.hub.store = self.store
        api.app.state.store = self.store
        holder = {'principal': policies.Principal(
            tenant_id='acme', user_id='alice', role='member', authenticated=True)}
        api.app.dependency_overrides[policies.current_principal] = lambda: holder['principal']
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=api.app),
                    base_url='http://test') as client:
                own = await client.get(f'/api/runs/{run_id}/usage')
                embedded = await client.get(f'/api/runs/{run_id}')
                holder['principal'] = policies.Principal(
                    tenant_id='other', user_id='bob', role='member', authenticated=True)
                foreign = await client.get(f'/api/runs/{run_id}/usage')
                denied = await client.get('/api/ops/usage?tenant=acme')
                holder['principal'] = policies.Principal(
                    tenant_id='acme', user_id='alice', role='operator', authenticated=True)
                exported = await client.get('/api/ops/usage?tenant=acme&format=csv')
                empty = await client.get('/api/ops/usage?tenant=acme&from=2099-01-01&to=2099-01-02')
                bad_day = await client.get('/api/ops/usage?from=yesterday')
                metrics = await client.get('/api/ops/metrics')
        finally:
            api.hub.store = old
            api.app.state.store = old
            api.app.dependency_overrides.clear()

        self.assertEqual(own.status_code, 200)
        body = own.json()
        self.assertEqual(body['total']['input_tokens'], plan_usage['input_tokens'])
        self.assertEqual(body['total']['output_tokens'], plan_usage['output_tokens'])
        self.assertEqual(body['by_purpose'][0]['purpose'], 'plan')
        self.assertEqual(embedded.status_code, 200)
        self.assertEqual(embedded.json()['usage'], body)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(exported.status_code, 200)
        self.assertIn('text/csv', exported.headers['content-type'])
        table = list(csv.reader(io.StringIO(exported.text)))
        by_purpose = {row[3]: row for row in table[1:]}
        self.assertEqual(by_purpose['plan'][5], '8')
        self.assertEqual(by_purpose['plan'][6], '2')
        self.assertEqual(by_purpose['__total__'][5], '8')
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()['total']['requests'], 0)
        self.assertEqual(bad_day.status_code, 422)
        self.assertEqual(metrics.status_code, 200)
        pool = metrics.json()['db_pool']
        for key in ('min_size', 'max_size', 'size', 'available'):
            self.assertIsInstance(pool[key], int)
        self.assertGreaterEqual(pool['available'], 0)

    async def test_tiny_token_cap_stops_before_the_second_model_call(self):
        from scripts.run_worker import run_once

        usage = {'input_tokens': 20, 'output_tokens': 5, 'total_tokens': 25}
        model = Scripted([
            [Chunk({'steps': []}, usage, 'ExecutionPlan')],
            [Chunk({'steps': [{'tool': 'read', 'args': {}}]},
                   {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
                   'ExecutionPlan')],
        ])
        remote = Remote()
        tools = {'read': remote.tool('read', effect='read', output={'answer': '好'})}
        await seed_run(self.store, message='请处理这件事')
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), assigned(settings, run_max_tokens=10), \
                patch.object(services, 'new_llm', _new_llm(model)):
            await run_once(Hub(self.store), 'worker', run_id='run1')

        self.assertEqual(model.calls, 1)
        run = await self.store.run('run1')
        self.assertEqual(run['status'], 'waiting_input')
        snapshot = await self.store.run_snapshot('run1')
        failure = snapshot['state']['planning_failure']
        self.assertEqual(failure['code'], 'E_RUN_BUDGET')
        self.assertTrue(failure['recoverable'])
        self.assertIn('25', run['error'])
        self.assertIn('10', run['error'])
        self.assertNotIn('tokens', run['consumed'])
        # 被 token 闸拦住的那一次没有发出去，次数必须停在实际发出的模型请求上。
        self.assertEqual(run['consumed']['provider_requests'], 1)
        rows = await self._rows('run1')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['input_tokens'], usage['input_tokens'])
        self.assertEqual(rows[0]['output_tokens'], usage['output_tokens'])
        self.assertEqual(remote.name_count('read'), 0)

    async def test_token_block_before_the_second_request_does_not_keep_the_count(self):
        """第一次请求正常成功；第二次发出前 token 已超上限，次数不能留成 2。

        计划本身是合法的，工具也真的跑了。拦住的是下一步的模型请求，
        不是结构没过再修一次。先扣次数再查 token、拦住了不退，这条会红：
        `provider_requests` 变成 2，而假模型只被调用了 1 次。
        """
        from scripts.run_worker import run_once

        usage = {'input_tokens': 30, 'output_tokens': 12, 'total_tokens': 42}
        plan = {
            'steps': [{'tool': 'read', 'args': {}}],
            'summary': '先读一下',
            'after_segment': 'observe',
            'observation_goal': '确认读到的内容能不能用',
            'result_locus': 'chat',
        }
        model = Scripted([
            [Chunk(plan, usage, 'ExecutionPlan')],
            [Chunk({'kind': 'continue'},
                   {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
                   'ObservationDecision')],
        ])
        remote = Remote()
        tools = {'read': remote.tool('read', effect='read', output={'answer': '好'})}
        await seed_run(self.store, message='请读一下再决定')
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), assigned(settings, run_max_tokens=20), \
                patch.object(services, 'new_llm', _new_llm(model)):
            await run_once(Hub(self.store), 'worker', run_id='run1')

        self.assertEqual(model.calls, 1)
        self.assertEqual(remote.name_count('read'), 1)
        run = await self.store.run('run1')
        self.assertEqual(run['status'], 'waiting_input')
        snapshot = await self.store.run_snapshot('run1')
        failure = snapshot['state']['planning_failure']
        self.assertEqual(failure['code'], 'E_RUN_BUDGET')
        self.assertIn('42', run['error'])
        self.assertIn('20', run['error'])
        rows = await self._rows('run1')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['purpose'], 'plan')
        self.assertEqual(rows[0]['status'], 'succeeded')
        self.assertEqual(rows[0]['input_tokens'], usage['input_tokens'])
        self.assertEqual(rows[0]['output_tokens'], usage['output_tokens'])
        # 次数必须等于已经发出并落账的那一次。拦住时不退，这里会是 2。
        self.assertEqual(run['consumed']['provider_requests'], 1)
        # 退回只动这一次次数，不能把同一次运行里已经记下的工具调用抹掉。
        self.assertEqual(run['consumed'].get('tool_calls'), 1)

    async def test_stale_executor_cannot_refund_provider_requests(self):
        """失去租约的执行者退不回次数，也不能改掉别人已经记下的用量。"""
        from webpage_agent.governance import execution_control

        await seed_run(self.store, run_id='refund-run', task_id='refund-task',
                       session_id='refund-sess')
        claimed = await self.store.claim_run('worker-a', 60, run_id='refund-run')
        context = execution_control.ExecutionContext.from_claim(claimed)
        await self.store.consume_budget(
            context, run_id='refund-run', key='provider_requests', limit=4)
        await self.store.consume_budget(
            context, run_id='refund-run', key='tool_calls', limit=4)
        await self.store.update_run('refund-run', lease_expires_at=0)
        await self.store.claim_run('worker-b', 60, run_id='refund-run')
        with self.assertRaises(execution_control.LeaseLost):
            await self.store.refund_budget(
                context, run_id='refund-run', key='provider_requests')
        consumed = (await self.store.run('refund-run'))['consumed']
        self.assertEqual(consumed['provider_requests'], 1)
        self.assertEqual(consumed['tool_calls'], 1)
