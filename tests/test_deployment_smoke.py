"""Two real production-mode API processes, shared PostgreSQL, no provider network calls."""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import httpx

from tests.exec_env import PG_DSN, new_store, reset_db
from tests.test_ticket_auth import claims, connection, sign

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class DeploymentSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_production_replicas_share_auth_streams_and_terminal_state(self):
        await reset_db()
        store = new_store()
        await store.ready()
        processes, logs = [], []
        try:
            with tempfile.TemporaryDirectory() as folder:
                env = {
                    'PATH': os.environ.get('PATH', ''), 'APP_ENV': 'production',
                    'PYTHONPATH': str(ROOT / 'src'),
                    'DATABASE_URL': PG_DSN, 'DB_AUTO_MIGRATE': 'false',
                    'AUTH_MODE': 'ticket', 'AUTH_DEV_COMPAT': 'false',
                    'AUTH_COOKIE_SECURE': 'true', 'AUTH_ALLOWED_ORIGINS': 'https://agent.example',
                    'AUTH_CONNECTIONS': json.dumps([asdict(connection())]),
                    'LLM_API_KEY': 'isolated-smoke-key', 'DEEPSEEK_API_KEY': 'isolated-smoke-search-key',
                    'LLM_MODEL': 'smoke-model', 'LLM_BASE_URL': 'https://model.example/v1',
                    'MCP_SERVERS': '[]', 'API_RATE_LIMIT_ENABLED': 'true',
                    'TENANT_BUDGET_ENABLED': 'true',
                    'LLM_INPUT_PRICE_MICROS_PER_MILLION': '1000000',
                    'LLM_OUTPUT_PRICE_MICROS_PER_MILLION': '1000000',
                    'SEARCH_REQUEST_COST_MICROS': '100',
                    'ARTIFACT_DIR': folder+'/artifacts', 'PROFILE_PATH': folder+'/profile/USER.md',
                    'RETENTION_EVENTS_DAYS': '0', 'RETENTION_ARTIFACTS_DAYS': '0',
                }
                ports = [free_port(), free_port()]
                for port in ports:
                    log = tempfile.TemporaryFile(mode='w+b')
                    logs.append(log)
                    processes.append(subprocess.Popen(
                        [sys.executable, '-m', 'scripts.serve_api', '--port', str(port)],
                        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
                async with httpx.AsyncClient(timeout=8) as client:
                    for process, port, log in zip(processes, ports, logs):
                        base = f'http://127.0.0.1:{port}'
                        for _ in range(100):
                            if process.poll() is not None:
                                log.seek(0)
                                self.fail('Production API startup failed: '+log.read().decode(errors='replace'))
                            try:
                                response = await client.get(base+'/healthz')
                                if response.status_code == 200:
                                    break
                            except httpx.TransportError:
                                pass
                            await asyncio.sleep(.05)
                        else:
                            self.fail('Production readiness timeout')
                    first, second = (f'http://127.0.0.1:{p}' for p in ports)
                    self.assertEqual((await client.get(first+'/api/me')).status_code, 401)
                    self.assertIn('<html', (await client.get(first+'/')).text.lower())
                    now = int(time.time())
                    jwt = sign(claims(iat=now, exp=now+30, jti='smoke-'+uuid.uuid4().hex))
                    exchanged = await client.post(first+'/api/auth/exchange', json={'ticket': jwt})
                    self.assertEqual(exchanged.status_code, 200, exchanged.text)
                    self.assertIn('Secure', exchanged.headers['set-cookie'])
                    self.assertIn('HttpOnly', exchanged.headers['set-cookie'])
                    token = exchanged.cookies.get('agent_session')
                    headers = {'Cookie': 'agent_session='+token}
                    self.assertEqual((await client.get(second+'/api/me', headers=headers)).status_code, 200)
                    identity = exchanged.json()
                    await store.ensure_session('smoke-session', tenant_id=identity['tenant_id'], owner_user_id=identity['user_id'])
                    await store.create_task('smoke-task', 'smoke-session', 'synthetic smoke task')
                    await store.ensure_task_meta('smoke-task', session_id='smoke-session', goal='synthetic',
                        tenant_id=identity['tenant_id'], owner_user_id=identity['user_id'])
                    await store.create_run('smoke-run', 'smoke-task', status='running',
                        tenant_id=identity['tenant_id'], owner_user_id=identity['user_id'])
                    await store.update_run('smoke-run', lease_owner='smoke-worker', lease_expires_at=time.time()+60)
                    self.assertEqual((await client.get(second+'/api/tasks/smoke-task', headers=headers)).status_code, 200)
                    async with client.stream('GET', second+'/api/tasks/smoke-task/events', headers=headers) as stream:
                        self.assertEqual(stream.status_code, 200)
                        lines = stream.aiter_lines()
                        async def event():
                            async for line in lines:
                                if line.startswith('data: '):
                                    return json.loads(line[6:])
                        await store.append_event('smoke-task', {'type': 'answer_delta', 'text': 'from another process'})
                        delta = await asyncio.wait_for(event(), 5)
                        self.assertEqual(delta['text'], 'from another process')
                        await store.update_run('smoke-run', status='succeeded')
                        await store.update_task('smoke-task', status='succeeded')
                        await store.append_event('smoke-task', {'type': 'status', 'run_status': 'succeeded'})
                        self.assertEqual((await asyncio.wait_for(event(), 5))['type'], 'status')
                        self.assertEqual((await asyncio.wait_for(event(), 5))['type'], 'end')
                    # A passive replica must refresh its formerly-running cached record.
                    result = await client.get(second+'/api/tasks/smoke-task', headers=headers)
                    self.assertEqual(result.json()['run_status'], 'succeeded')
                    replay = await client.get(first+'/api/tasks/smoke-task/events',
                        headers={**headers, 'Last-Event-ID': '1'})
                    self.assertNotIn('from another process', replay.text)
                    self.assertIn('"type": "status"', replay.text)
                    self.assertIn('"type": "end"', replay.text)
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    await asyncio.to_thread(process.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            for log in logs:
                log.close()
            await store.close()
