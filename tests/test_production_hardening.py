"""Production boundaries: real PostgreSQL, fake providers, isolated filesystems."""
import asyncio
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import psycopg
from psycopg.conninfo import make_conninfo
from langchain_core.messages import AIMessageChunk

from webpage_agent.store import artifact_store
from webpage_agent.governance import core as governance
from webpage_agent.observe import maintenance
from webpage_agent.store import migrations
from webpage_agent.core import services
from webpage_agent.observe import telemetry
from webpage_agent.core.config import settings
from webpage_agent.integrations import mcp_adapter, mcp_servers
from webpage_agent.store.pg_store import MIGRATIONS, SCHEMA, PostgresStore
from webpage_agent.observe import runtime_check
from scripts.package_release import package
from tests.exec_env import new_store, PG_DSN, reset_db, seed_run, valid_test_dsn
from tests.test_runtime_closure import assigned


class ReleaseTests(unittest.TestCase):
    def test_release_excludes_runtime_secrets_and_personal_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('.env', 'secrets/token', 'profile/users/u.md', 'artifacts/task/report',
                         'deploy/production.env', 'deploy/config/auth-connections.json',
                         'src/__pycache__/x.pyc', 'src/.env', 'src/secrets/token'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('DO_NOT_SHIP')
            (root / 'src' / 'ok.py').write_text('print(1)')
            (root / 'deploy' / 'mcp.json.example').write_text('[]')
            target = root / 'release.tar.gz'
            digest = package(root, target)
            self.assertEqual(len(digest), 64)
            with tarfile.open(target) as archive:
                self.assertEqual(set(archive.getnames()), {'src/ok.py', 'deploy/mcp.json.example', 'SHA256SUMS'})
                self.assertTrue(all(b'DO_NOT_SHIP' not in archive.extractfile(p).read() for p in archive.getnames()))
            with self.assertRaises(FileExistsError):
                package(root, target)

    def test_secret_scanner_blocks_embedded_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'src').mkdir()
            (root / 'src' / 'bad.py').write_bytes(b'key="sk-' + b'x'*30 + b'"')
            with self.assertRaises(ValueError):
                package(root, root / 'release.tar.gz')
            self.assertFalse((root / 'release.tar.gz').exists())

    def test_release_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'src').mkdir()
            (root / 'src' / 'data').symlink_to('/etc/hosts')
            with self.assertRaises(ValueError):
                package(root, root / 'release.tar.gz')

    def test_gc_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root = Path(folder)
            leaf = Path(outside) / 'orphan'
            leaf.mkdir()
            (leaf / 'keep').write_text('private')
            os.utime(leaf, (1, 1))
            (root / '_tool_results').mkdir()
            (root / '_tool_results' / 'tenant').symlink_to(outside)
            self.assertEqual(artifact_store.gc_unregistered(root, grace_seconds=1), 0)
            self.assertTrue((leaf / 'keep').exists())


class ProductionConfigurationTests(unittest.TestCase):
    def test_production_never_loads_a_local_dotenv_even_with_whitespace(self):
        import subprocess
        import sys
        import webpage_agent.core.config
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'webpage_agent' / 'core').mkdir(parents=True)
            (root / 'webpage_agent' / '__init__.py').write_text('')
            (root / 'webpage_agent' / 'core' / '__init__.py').write_text('')
            (root / 'webpage_agent' / 'core' / 'config.py').write_text(
                Path(webpage_agent.core.config.__file__).read_text())
            (root / '.env').write_text('LLM_API_KEY=must-not-load-local-secret')
            for mode in ('production', ' production '):
                completed = subprocess.run([sys.executable, '-c',
                    "from webpage_agent.core.config import settings; assert settings.llm_key == ''"],
                    cwd=root, env={'APP_ENV': mode, 'PATH': os.environ.get('PATH', '')},
                    capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_transport_rejects_remote_http_and_url_credentials(self):
        for url in ('http://42.194.146.23:3457/mcp', 'http://10.0.0.1/mcp',
                    'https://user:password@example.com/mcp', 'file:///tmp/mcp'):
            self.assertTrue(runtime_check.transport_issue(url, allow_loopback=True))
        self.assertFalse(runtime_check.transport_issue('https://mcp.example/mcp'))
        self.assertTrue(runtime_check.transport_issue('http://127.0.0.1/mcp'))
        self.assertFalse(runtime_check.transport_issue('http://127.0.0.1/mcp', allow_loopback=True))

    def test_production_checks_reject_dev_flags_and_unpriced_billing(self):
        with assigned(settings, app_env='production', auth_mode='off', auth_cookie_secure=False,
                      auth_dev_compat=True, db_auto_migrate=True, api_rate_limit_enabled=False,
                      tenant_budget_enabled=False, auth_allowed_origins=(),
                      llm_input_price_micros_per_million=0), patch.dict(os.environ, {'MCP_SERVERS': '[]'}, clear=False):
            found = runtime_check.issues()
        for value in ('AUTH_MODE', 'AUTH_COOKIE_SECURE', 'AUTH_DEV_COMPAT', 'DB_AUTO_MIGRATE',
                      'API_RATE_LIMIT_ENABLED', 'TENANT_BUDGET_ENABLED', 'pricing'):
            self.assertTrue(any(value in issue for issue in found), value)

    def test_portable_registry_escapes_env_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'servers.json'
            path.write_text('[{"id":"remote","url":"${URL}","token":"${TOKEN}"}]')
            with patch.dict(os.environ, {'MCP_SERVERS_FILE': str(path), 'URL': 'https://mcp.example',
                                         'TOKEN': 'quoted"token\\line'}, clear=False):
                self.assertEqual(mcp_servers.servers()[0].token, 'quoted"token\\line')
                self.assertEqual(mcp_servers.LAST_ERROR, '')

    def test_bad_registry_never_falls_back_to_global_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'servers.json'
            for content in ('{}', 'not-json', '[{"id":"broken"}]',
                            '[{"id":"x","url":"${NOT_SET_HARDENING}"}]',
                            '[{"id":"x","url":"https://a"},{"id":"x","url":"https://b"}]'):
                path.write_text(content)
                with patch.dict(os.environ, {'MCP_SERVERS_FILE': str(path), 'MCP_URL': 'https://fallback'}, clear=False):
                    self.assertEqual(mcp_servers.servers(), [])
                    self.assertTrue(mcp_servers.LAST_ERROR)

    def test_production_registry_mode_is_case_insensitive_and_never_falls_back(self):
        with patch.dict(os.environ, {'APP_ENV': ' Production ', 'MCP_SERVERS_FILE': '',
                                     'MCP_SERVERS': 'not-json', 'MCP_URL': 'https://fallback'}):
            self.assertEqual(mcp_servers.servers(), [])
            self.assertTrue(mcp_servers.LAST_ERROR)

    def test_traceparent_validation(self):
        parent = '00-'+'a'*32+'-'+'b'*16+'-01'
        self.assertEqual(telemetry.new_trace(parent), 'a'*32)
        self.assertNotEqual(telemetry.new_trace('00-'+'0'*32+'-'+'b'*16+'-01'), '0'*32)
        self.assertEqual(len(telemetry.new_trace('garbage\ncredentials')), 32)

    def test_prometheus_contains_numeric_metrics_only(self):
        text = telemetry.prometheus({'runs_queued': 3, 'provider_charged_micros': 100,
                                     'secret': 'do not emit'})
        self.assertIn('webpage_runs_queued 3', text)
        self.assertNotIn('secret', text)


class DatabaseGovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        await self.store.ready()
        async with self.store._lease() as conn:
            await conn.execute('TRUNCATE api_rate_windows,provider_charges,tenant_usage_days')

    async def asyncTearDown(self):
        await self.store.close()

    async def reserve(self, charge, tenant='tenant-a', cost=10, requests=100, cap=100):
        await self.store.reserve_provider_charge(charge, tenant_id=tenant, run_id='r',
            provider='llm', model='test', cost_micros=cost, max_requests=requests, max_cost_micros=cap)

    async def test_rate_limit_is_atomic_across_store_instances(self):
        other = new_store()
        await other.ready()
        try:
            results = await asyncio.gather(*((self.store if i % 2 else other).rate_limit(
                'same-principal', 7, now_ts=1000) for i in range(40)))
            self.assertEqual(sum(allowed for allowed, _ in results), 7)
            self.assertTrue((await other.rate_limit('different', 7, now_ts=1000))[0])
            self.assertTrue((await other.rate_limit('same-principal', 7, now_ts=1020))[0])
            async with self.store._lease() as conn:
                row = await self.store._one(conn, 'SELECT scope FROM api_rate_windows LIMIT 1')
                self.assertNotIn('principal', row['scope'])
        finally:
            await other.close()

    async def test_daily_cost_cannot_be_overspent_by_parallel_reservations(self):
        results = await asyncio.gather(*(self.reserve(str(i), cost=25, cap=100)
                                        for i in range(20)), return_exceptions=True)
        self.assertEqual(sum(r is None for r in results), 4)
        self.assertEqual(sum(isinstance(r, governance.QuotaExceeded) for r in results), 16)
        stats = await self.store.billing_stats()
        self.assertEqual(stats['provider_charged_micros'], 100)
        self.assertEqual(stats['provider_requests'], 4)

    async def test_daily_request_cap_and_tenant_isolation(self):
        await self.reserve('one', requests=1)
        with self.assertRaises(governance.QuotaExceeded):
            await self.reserve('two', requests=1)
        await self.reserve('other', tenant='tenant-b', requests=1)

    async def test_settlement_refunds_once_and_survives_reopen(self):
        await self.reserve('one', cost=100)
        await self.store.settle_provider_charge('one', actual_micros=30, tokens=(10, 20), status='succeeded')
        await self.store.settle_provider_charge('one', actual_micros=0, status='succeeded')
        await self.store.close()
        await self.store.ready()
        await self.reserve('two', cost=70)
        with self.assertRaises(governance.QuotaExceeded):
            await self.reserve('three', cost=1)
        stats = await self.store.billing_stats()
        self.assertEqual(stats['provider_charged_micros'], 100)
        self.assertEqual(stats['provider_input_tokens'], 10)

    async def test_unknown_provider_outcome_does_not_refund(self):
        await self.reserve('one', cost=100)
        await self.store.settle_provider_charge('one', status='uncertain')
        with self.assertRaises(governance.QuotaExceeded):
            await self.reserve('two', cost=1)

    async def test_reused_charge_id_rolls_back_the_daily_counter(self):
        await self.reserve('one', cost=30)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            await self.reserve('one', cost=30)
        await self.reserve('two', cost=70)
        self.assertEqual((await self.store.billing_stats())['provider_charged_micros'], 100)

    async def test_model_quota_rejection_prevents_network_call(self):
        class FakeModel:
            calls = 0
            async def astream(self, messages):
                self.calls += 1
                yield AIMessageChunk(content='hello')
        model = FakeModel()
        token = governance.bind(self.store, 'tenant-a', 'r')
        try:
            with assigned(settings, tenant_budget_enabled=True, tenant_daily_provider_requests=1,
                          tenant_daily_cost_micros=1000000,
                          llm_input_price_micros_per_million=1,
                          llm_output_price_micros_per_million=1), patch.object(services, 'new_llm', return_value=model):
                self.assertEqual(await services.chat_reply('system', [{'role': 'user', 'content': 'hi'}]), 'hello')
                with self.assertRaises(governance.QuotaExceeded):
                    await services.chat_reply('system', [{'role': 'user', 'content': 'hi'}])
            self.assertEqual(model.calls, 1)
        finally:
            governance.unbind(token)

    async def test_missing_billing_scope_fails_closed(self):
        with assigned(settings, tenant_budget_enabled=True):
            with self.assertRaises(RuntimeError):
                await governance.reserve()

    async def test_provider_cancellation_keeps_reservation(self):
        class FakeModel:
            async def astream(self, messages):
                raise asyncio.CancelledError()
                yield
        token = governance.bind(self.store, 'tenant-a')
        try:
            with assigned(settings, tenant_budget_enabled=True,
                          llm_input_price_micros_per_million=1000000,
                          llm_output_price_micros_per_million=1000000), patch.object(services, 'new_llm', return_value=FakeModel()):
                with self.assertRaises(asyncio.CancelledError):
                    await services.chat_reply('system', [{'role': 'user', 'content': 'hi'}])
            async with self.store._lease() as conn:
                row = await self.store._one(conn, 'SELECT status,charged_micros FROM provider_charges')
            self.assertEqual(row['status'], 'uncertain')
            self.assertGreater(row['charged_micros'], 0)
        finally:
            governance.unbind(token)

    async def test_stream_deltas_are_replayable_from_another_store(self):
        from webpage_agent.api.api_hub import TaskRecord
        await seed_run(self.store)
        record = TaskRecord(task_id='task1', session_id='sess1', message='hi', store=self.store)
        await record.publish({'type': 'answer_delta', 'text': 'hello'})
        await record.publish({'type': 'draft', 'report': {'x': 1}})
        other = new_store()
        try:
            events = await other.events('task1')
            self.assertEqual([e['type'] for _, e in events], ['answer_delta', 'draft'])
        finally:
            await other.close()

    async def test_notification_arrives_on_separate_postgres_connection(self):
        async with await psycopg.AsyncConnection.connect(PG_DSN, autocommit=True) as conn:
            await conn.execute('LISTEN agent_task_events')
            async def receive():
                async for notice in conn.notifies():
                    return notice.payload
            task = asyncio.create_task(receive())
            await self.store.append_event('notify-task', {'type': 'action'})
            self.assertEqual(await asyncio.wait_for(task, 3), 'notify-task')

    async def test_maintenance_retains_waiting_runs_and_last_event_sequence(self):
        await seed_run(self.store, status='succeeded')
        for _ in range(3):
            await self.store.append_event('task1', {'type': 'action'})
        async with self.store._lease() as conn:
            await conn.execute("UPDATE events SET created_at='2000-01-01T00:00:00+00:00'")
        with tempfile.TemporaryDirectory() as folder, assigned(settings, retention_events_days=1,
                                                               retention_artifacts_days=0):
            stats = await maintenance.run_once(self.store, root=folder)
        self.assertEqual(stats['events'], 2)
        self.assertEqual(await self.store.append_event('task1', {'type': 'action'}), 4)
        await self.store.update_run('run1', status='waiting_input')
        with tempfile.TemporaryDirectory() as folder, assigned(settings, retention_events_days=1,
                                                               retention_artifacts_days=0):
            self.assertEqual((await maintenance.run_once(self.store, root=folder))['events'], 0)

    async def test_http_rate_limit_and_operator_prometheus(self):
        from webpage_agent.api import routes as api
        from webpage_agent.governance import policies
        old = api.hub.store
        api.hub.store = self.store
        api.app.state.store = self.store
        try:
            with assigned(settings, api_rate_limit_enabled=True, api_rate_limit_per_minute=1,
                          user_rate_limit_per_minute=100, auth_mode='off', metrics_format='prometheus'):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
                    first = await client.get('/api/me')
                    second = await client.get('/api/me')
                    self.assertEqual(first.status_code, 200)
                    self.assertEqual(second.status_code, 429)
                    self.assertGreater(int(second.headers['retry-after']), 0)
                    self.assertEqual(len(first.headers['x-request-id']), 32)
            with assigned(settings, api_rate_limit_enabled=False, auth_mode='off', metrics_format='prometheus'):
                api.app.dependency_overrides[policies.current_principal] = lambda: policies.Principal(role='operator', authenticated=True)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
                    response = await client.get('/api/ops/metrics')
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('text/plain', response.headers['content-type'])
                    self.assertIn('webpage_provider_charged_micros', response.text)
                api.app.dependency_overrides.clear()
        finally:
            api.hub.store = old
            api.app.state.store = old
            api.app.dependency_overrides.clear()

    async def test_retention_deletes_only_expired_final_artifacts_and_registered_results(self):
        from webpage_agent.tools import tool_results
        await seed_run(self.store, status='succeeded')
        await seed_run(self.store, task_id='paused-task', run_id='paused-run', status='paused')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            expired_name, paused_name = uuid.uuid4().hex, uuid.uuid4().hex
            for name in (expired_name, paused_name):
                (root / name).mkdir()
                (root / name / 'output.txt').write_text('synthetic')
            await self.store.update_task('task1', artifact_id=expired_name)
            await self.store.update_task('paused-task', artifact_id=paused_name)
            # 结果必须真的落盘：下面要断言"过期目录被清掉"，光写元数据不够
            tool_results.stage_result(root, tool_results.CapturedResult(structured={'value': 1}),
                                      tenant_id='default', result_id='expired-result')
            result_folder = tool_results.result_dir(root, 'default', 'expired-result')
            async with self.store._lease() as conn:
                await conn.execute("INSERT INTO tool_result_manifests (result_id,tenant_id,run_id,created_at) VALUES (%s,%s,%s,%s)",
                    ('expired-result', 'default', 'run1', '2000-01-01T00:00:00+00:00'))
                await conn.execute("UPDATE tasks SET updated_at='2000-01-01T00:00:00+00:00'")
                await conn.execute("UPDATE runs SET updated_at='2000-01-01T00:00:00+00:00'")
            with assigned(settings, retention_events_days=0, retention_artifacts_days=1):
                results = await asyncio.gather(maintenance.run_once(self.store, root=root),
                                               maintenance.run_once(self.store, root=root))
            self.assertEqual(sum(r['artifacts'] for r in results), 1)
            self.assertEqual(sum(r['results'] for r in results), 1)
            self.assertFalse((root / expired_name).exists())
            self.assertFalse(result_folder.exists())
            self.assertTrue((root / paused_name / 'output.txt').exists())
            self.assertIsNone(await self.store.result_manifest('expired-result'))

    async def test_rate_backend_outage_fails_closed_without_exception_details(self):
        from webpage_agent.api import routes as api
        old = api.hub.store
        api.hub.store = self.store
        try:
            with assigned(settings, api_rate_limit_enabled=True), patch.object(
                    self.store, 'rate_limit', AsyncMock(side_effect=RuntimeError('sensitive detail'))):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
                    response = await client.get('/api/me')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('sensitive detail', response.text)
        finally:
            api.hub.store = old

    async def test_trace_is_persisted_with_new_goals_and_runs(self):
        token = telemetry.trace_id.set('a'*32)
        try:
            await self.store.create_goal('trace-task', 'trace-run', 'trace-session', 'synthetic',
                tenant_id='tenant-a', owner_user_id='user-a')
            await self.store.create_run('trace-run-2', 'trace-task')
            for run_id in ('trace-run', 'trace-run-2'):
                self.assertEqual((await self.store.run(run_id))['trace_id'], 'a'*32)
        finally:
            telemetry.trace_id.reset(token)

    async def test_operator_role_is_loaded_from_trusted_identity_on_each_request(self):
        from webpage_agent.api import auth
        from tests.test_ticket_auth import connection, claims, sign
        now = int(time.time())
        with patch.object(auth.ticket_auth.auth_connections, 'connections', return_value=[connection()]):
            result = await auth.exchange(sign(claims(iat=now, exp=now+30, jti=uuid.uuid4().hex)), self.store)
        async with self.store._lease() as conn:
            await conn.execute('UPDATE identities SET role=%s WHERE user_id=%s', ('operator', result['user_id']))
        row = await self.store.auth_session(auth.digest_of(result['token']))
        self.assertEqual(row['role'], 'operator')
        async with self.store._lease() as conn:
            await conn.execute('UPDATE identities SET role=%s WHERE user_id=%s', ('member', result['user_id']))
        row = await self.store.auth_session(auth.digest_of(result['token']))
        self.assertEqual(row['role'], 'member')


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not valid_test_dsn(PG_DSN):
            self.skipTest('PG_TEST_DSN required')
        self.schema = 'hardening_' + uuid.uuid4().hex
        async with await psycopg.AsyncConnection.connect(PG_DSN, autocommit=True) as conn:
            await conn.execute('CREATE SCHEMA ' + self.schema)
        self.dsn = make_conninfo(PG_DSN, options='-csearch_path='+self.schema)

    async def asyncTearDown(self):
        async with await psycopg.AsyncConnection.connect(PG_DSN, autocommit=True) as conn:
            await conn.execute('DROP SCHEMA ' + self.schema + ' CASCADE')

    async def test_concurrent_startups_apply_each_migration_once(self):
        stores = [PostgresStore(self.dsn) for _ in range(6)]
        try:
            await asyncio.gather(*(s.ready() for s in stores))
            async with stores[0]._lease() as conn:
                before = await stores[0]._all(conn, 'SELECT * FROM schema_migrations ORDER BY migration_id')
                self.assertEqual(await migrations.apply(conn, baseline=SCHEMA, legacy_ids=MIGRATIONS), [])
                after = await stores[0]._all(conn, 'SELECT * FROM schema_migrations ORDER BY migration_id')
                self.assertEqual(before, after)
                self.assertIn(migrations.LATEST, [r['migration_id'] for r in after])
        finally:
            await asyncio.gather(*(s.close() for s in stores))

    async def test_production_does_not_run_ddl_on_startup(self):
        store = PostgresStore(self.dsn)
        try:
            with assigned(settings, db_auto_migrate=False):
                with self.assertRaisesRegex(RuntimeError, 'scripts.migrate'):
                    await store.ready()
            async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
                await migrations.apply(conn, baseline=SCHEMA, legacy_ids=MIGRATIONS)
            with assigned(settings, db_auto_migrate=False):
                await store.ready()
        finally:
            await store.close()

    async def test_upgrade_preserves_legacy_data(self):
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            await conn.execute(SCHEMA)
            await conn.execute("INSERT INTO sessions VALUES ('old','tenant','user','old','old')")
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            changed = await migrations.apply(conn, baseline=SCHEMA, legacy_ids=MIGRATIONS)
            self.assertIn(migrations.BASELINE, changed)
            cursor = await conn.execute("SELECT owner_user_id FROM sessions WHERE session_id='old'")
            self.assertEqual((await cursor.fetchone())[0], 'user')

    async def test_failed_migration_rolls_back_schema_and_version(self):
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            with self.assertRaises(psycopg.Error):
                await migrations.apply(conn, baseline='CREATE TABLE partial(id int); INVALID SQL', legacy_ids=())
            await conn.rollback()
            cursor = await conn.execute("SELECT to_regclass('partial'), to_regclass('schema_migrations')")
            self.assertEqual(await cursor.fetchone(), (None, None))

class AdditionalBoundaryTests(unittest.TestCase):
    def test_orphan_gc_ignores_symlinked_storage_root(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root = Path(folder)
            old = Path(outside) / 'tenant' / 'result'
            old.mkdir(parents=True)
            os.utime(old, (1, 1))
            (root / '_tool_results').symlink_to(outside)
            self.assertEqual(artifact_store.gc_unregistered(root, grace_seconds=1), 0)
            self.assertTrue(old.exists())

    def test_orphan_gc_can_remove_staged_manifest_missing_from_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('registered', 'abandoned'):
                leaf = root / '_tool_results' / 'tenant' / name
                leaf.mkdir(parents=True)
                (leaf / 'manifest.json').write_text(json.dumps({'result_id': name}))
                os.utime(leaf, (1, 1))
            removed = artifact_store.gc_unregistered(root, grace_seconds=1, registered_ids={'registered'})
            self.assertEqual(removed, 1)
            self.assertTrue((root / '_tool_results' / 'tenant' / 'registered').exists())

    def test_release_detects_opaque_copied_local_token(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            token = 'opaque-' + 'sensitive-token-for-this-test'
            (root / '.env').write_text('MCP_BEARER_TOKEN='+token)
            (root / 'src').mkdir()
            (root / 'src' / 'bad.py').write_text('TOKEN = '+repr(token))
            with self.assertRaisesRegex(ValueError, 'Potential secret'):
                package(root, root / 'release.tar.gz')


class AdditionalAsyncBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_transport_refuses_before_opening_network(self):
        server = mcp_servers.Server(id='insecure', url='http://remote.example/mcp', token='opaque')
        with assigned(settings, app_env='production'), patch.object(mcp_adapter.httpx, 'AsyncClient') as client:
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                async with mcp_adapter.connect_session(server):
                    pass
            client.assert_not_called()

    async def test_production_redirect_hook_refuses_http_downgrade(self):
        with assigned(settings, app_env='production'):
            with self.assertRaises(ValueError):
                await mcp_adapter._secure_mcp_request(httpx.Request('GET', 'http://remote.example/mcp'))


class TestDatabaseSafetyTests(unittest.TestCase):
    def test_a_test_password_does_not_authorize_truncating_production_database(self):
        self.assertFalse(valid_test_dsn('postgresql://test_user:test_password@localhost/live_database'))
        self.assertTrue(valid_test_dsn('postgresql://user@localhost/agent_test'))
