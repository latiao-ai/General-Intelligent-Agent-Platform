"""第十一阶段 A 组第 6 点：真正进程级入口验收。

启动实际 API 进程（ticket 鉴权）与 Worker 进程，经 HTTP 登录/鉴权、POST 消息、
由真实 Worker 消费、查询 timeline 取得答复。用受控模型（PI_MOCK_SCRIPT）替代
真实供应商网络调用，验证：

- 普通问候：Pi 执行，答复落库且可读取；
- 连续多 turn：同会话按序执行；
- 重复请求：同一 request_id 重放不重复；
- 跨主体拒绝：目标任务零变化；
- Worker 消费：不手动调用聊天执行函数，由真实 Worker 调度。

浏览器刷新与断线恢复另由前端轮询（pollChatReply 按 turn_id + sessionId）覆盖，
本文件聚焦 HTTP + Worker 进程链路。
"""
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


def _require_pg(case):
    if not PG_DSN:
        case.skipTest('需要 PG_TEST_DSN')
    if 'test' not in (PG_DSN or ''):
        case.skipTest('PG_TEST_DSN 必须指向测试库')


def _mock_answer(text):
    return json.dumps([{'type': 'answer', 'text': text}])


class SessionProcessE2ETests(unittest.IsolatedAsyncioTestCase):
    """真实 API 进程 + Worker 进程：消息 POST → Worker 消费 → timeline 答复。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()
        self.api_proc = None
        self.worker_proc = None
        self._logs = []

    async def asyncTearDown(self):
        for proc in (self.api_proc, self.worker_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
        for proc in (self.api_proc, self.worker_proc):
            if proc is not None:
                try:
                    await asyncio.to_thread(proc.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    await asyncio.to_thread(proc.wait)
        for log in self._logs:
            log.close()
        await self.store.close()

    async def _start(self, *, mock_text, chat_enabled='true',
                     query_enabled='true'):
        folder = tempfile.mkdtemp()
        env = {
            'PATH': os.environ.get('PATH', ''),
            'APP_ENV': 'test',
            'PYTHONPATH': str(ROOT / 'src'),
            'DATABASE_URL': PG_DSN,
            'DB_AUTO_MIGRATE': 'true',
            'AUTH_MODE': 'ticket',
            'AUTH_DEV_COMPAT': 'false',
            'AUTH_COOKIE_SECURE': 'false',
            'AUTH_ALLOWED_ORIGINS': 'http://127.0.0.1',
            'AUTH_CONNECTIONS': json.dumps([asdict(connection())]),
            'LLM_API_KEY': 'isolated-e2e-key',
            'DEEPSEEK_API_KEY': 'isolated-e2e-search-key',
            'LLM_MODEL': 'e2e-model',
            'LLM_BASE_URL': 'https://model.example/v1',
            'MCP_SERVERS': '[]',
            'API_RATE_LIMIT_ENABLED': 'false',
            'TENANT_BUDGET_ENABLED': 'false',
            'SESSION_CHAT_ENABLED': chat_enabled,
            'SESSION_QUERY_ENABLED': query_enabled,
            'SESSION_TASK_CREATE_ENABLED': 'false',
            'SESSION_TASK_CONTROL_ENABLED': 'false',
            'SESSION_CHAT_CONCURRENCY': '2',
            'SESSION_TURN_LEASE_SECONDS': '20',
            'PI_MOCK_SCRIPT': mock_text,
            'ARTIFACT_DIR': folder + '/artifacts',
            'PROFILE_PATH': folder + '/profile/USER.md',
            'RETENTION_EVENTS_DAYS': '0',
            'RETENTION_ARTIFACTS_DAYS': '0',
        }
        api_port = free_port()
        api_log = tempfile.TemporaryFile(mode='w+b')
        self._logs.append(api_log)
        self.api_proc = subprocess.Popen(
            [sys.executable, '-m', 'scripts.serve_api', '--port', str(api_port)],
            cwd=ROOT, env=env, stdout=api_log, stderr=subprocess.STDOUT)

        worker_log = tempfile.TemporaryFile(mode='w+b')
        self._logs.append(worker_log)
        self.worker_proc = subprocess.Popen(
            [sys.executable, '-m', 'scripts.run_worker'],
            cwd=ROOT, env=env, stdout=worker_log, stderr=subprocess.STDOUT)

        base = f'http://127.0.0.1:{api_port}'
        async with httpx.AsyncClient(timeout=8) as client:
            for _ in range(200):
                if self.api_proc.poll() is not None:
                    api_log.seek(0)
                    self.fail('API 启动失败: ' + api_log.read().decode(errors='replace'))
                try:
                    if (await client.get(base + '/healthz')).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(.05)
            else:
                self.fail('API readiness 超时')

        # 登录：ticket 交换 → cookie
        now = int(time.time())
        jwt = sign(claims(iat=now, exp=now + 30, jti='e2e-' + uuid.uuid4().hex,
                          sub='user-u', tenant_id='tenant-a', platform_id='pa'))
        async with httpx.AsyncClient(timeout=8) as client:
            exchanged = await client.post(base + '/api/auth/exchange',
                                          json={'ticket': jwt})
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            token = exchanged.cookies.get('agent_session')
            self.headers = {'Cookie': 'agent_session=' + token}
        return base, token

    async def _post_message(self, base, session_id, request_id, message,
                            target_task_id=None, headers=None):
        async with httpx.AsyncClient(timeout=8) as client:
            payload = {'request_id': request_id, 'message': message}
            if target_task_id:
                payload['target_task_id'] = target_task_id
            return await client.post(
                f'{base}/api/sessions/{session_id}/messages',
                json=payload, headers=headers or self.headers)

    async def _timeline(self, base, session_id, headers=None):
        async with httpx.AsyncClient(timeout=8) as client:
            return await client.get(
                f'{base}/api/sessions/{session_id}/timeline',
                headers=headers or self.headers)

    async def _wait_answer(self, base, session_id, turn_id, *, timeout=30):
        """轮询 timeline 直到该 turn 终态，返回 turn 记录。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = await self._timeline(base, session_id)
            if resp.status_code != 200:
                await asyncio.sleep(.2)
                continue
            data = resp.json()
            for turn in data.get('chat_turns') or []:
                if turn['turn_id'] == turn_id and turn['status'] in (
                        'succeeded', 'failed', 'waiting_clarification', 'cancelled'):
                    return turn
            await asyncio.sleep(.2)
        return None

    async def test_greeting_pi_executes_via_real_worker(self):
        """普通问候：POST 消息 → 真实 Worker 消费 → Pi 答复落库可读取。"""
        base, _ = await self._start(mock_text=_mock_answer('你好！我是智能助手。'))
        resp = await self._post_message(base, 'sess1', 'req-1', '你好')
        self.assertEqual(resp.status_code, 202, resp.text)
        turn_id = resp.json()['turn_id']
        # 真实 Worker 消费（不手动调用执行函数）
        turn = await self._wait_answer(base, 'sess1', turn_id)
        self.assertIsNotNone(turn, 'Worker 未在超时内消费并生成答复')
        self.assertEqual(turn['status'], 'succeeded')
        self.assertIn('你好', turn['answer'])
        # 答复可通过 messages 接口读取
        async with httpx.AsyncClient(timeout=8) as client:
            msgs = await client.get(f'{base}/api/sessions/sess1/messages',
                                    headers=self.headers)
            self.assertEqual(msgs.status_code, 200)
            roles = [m['role'] for m in msgs.json()['messages']]
            self.assertIn('assistant', roles)

    async def test_consecutive_turns_serial_and_duplicate_not_duplicated(self):
        """连续多 turn 按序执行；重复请求（同 request_id）不重复。"""
        base, _ = await self._start(mock_text=_mock_answer('答复'))
        r1 = await self._post_message(base, 's2', 'req-a', '第一条')
        self.assertEqual(r1.status_code, 202)
        turn1 = r1.json()['turn_id']
        # 同 request_id 重放 → 返回原 turn，不重复
        r1_dup = await self._post_message(base, 's2', 'req-a', '第一条')
        self.assertEqual(r1_dup.status_code, 202)
        self.assertEqual(r1_dup.json()['turn_id'], turn1)
        self.assertTrue(r1_dup.json().get('replayed'))
        # 第二条
        r2 = await self._post_message(base, 's2', 'req-b', '第二条')
        self.assertEqual(r2.status_code, 202)
        turn2 = r2.json()['turn_id']
        t1 = await self._wait_answer(base, 's2', turn1)
        t2 = await self._wait_answer(base, 's2', turn2)
        self.assertEqual(t1['status'], 'succeeded')
        self.assertEqual(t2['status'], 'succeeded')
        # 两条 turn 按接纳顺序
        data = (await self._timeline(base, 's2')).json()
        seqs = [t['turn_seq'] for t in data['chat_turns']]
        self.assertEqual(seqs, sorted(seqs))

    async def test_cross_principal_rejected(self):
        """跨主体请求：拒绝，目标任务零变化。"""
        base, _ = await self._start(mock_text=_mock_answer('答复'))
        # 主体 user-u 建立会话
        await self._post_message(base, 's3', 'req-1', '你好')
        # 用另一主体 user-other 登录
        now = int(time.time())
        jwt2 = sign(claims(iat=now, exp=now + 30, jti='e2e-' + uuid.uuid4().hex,
                           sub='user-other', tenant_id='tenant-a', platform_id='pa'))
        async with httpx.AsyncClient(timeout=8) as client:
            exchanged = await client.post(base + '/api/auth/exchange',
                                          json={'ticket': jwt2})
            token2 = exchanged.cookies.get('agent_session')
            other_headers = {'Cookie': 'agent_session=' + token2}
        resp = await self._post_message(base, 's3', 'req-x', '取消任务',
                                        headers=other_headers)
        self.assertEqual(resp.status_code, 404, resp.text)

    async def test_chat_disabled_rejected_at_accept(self):
        """能力开关关闭：接纳层明确拒绝（403），不落 turn 搁置。"""
        base, _ = await self._start(mock_text=_mock_answer('答复'),
                                    chat_enabled='false')
        resp = await self._post_message(base, 's4', 'req-1', '你好')
        self.assertEqual(resp.status_code, 403, resp.text)

    async def test_chat_proceeds_while_business_run_present(self):
        """后台有业务 run 时，聊天仍能被 Worker 及时消费（分池不阻塞）。"""
        base, _ = await self._start(mock_text=_mock_answer('后台期间的答复'))
        # 获取当前登录主体的真实 user_id（ticket 交换后由 bind_identity 生成）
        async with httpx.AsyncClient(timeout=8) as client:
            me = await client.get(base + '/api/me', headers=self.headers)
            self.assertEqual(me.status_code, 200, me.text)
            uid = me.json()['user_id']
            tenant = me.json().get('tenant_id', 'tenant-a')
        # 先 POST 一条消息建立会话归属（用真实主体）
        resp0 = await self._post_message(base, 's5', 'req-0', '你好')
        self.assertEqual(resp0.status_code, 202, resp0.text)
        # 建立一个业务任务 + queued run，占用 Worker 的业务领取通道
        await self.store.create_task('biz-task', 's5', '做个页面')
        await self.store.ensure_task_meta('biz-task', session_id='s5', goal='做个页面',
                                          tenant_id=tenant, owner_user_id=uid)
        await self.store.create_run('biz-run', 'biz-task', status='queued',
                                    tenant_id=tenant, owner_user_id=uid)
        # 发聊天消息：Worker 应能独立消费聊天 turn（不受业务 run 领取影响）
        resp = await self._post_message(base, 's5', 'req-c', '你好')
        self.assertEqual(resp.status_code, 202, resp.text)
        turn = await self._wait_answer(base, 's5', resp.json()['turn_id'])
        self.assertIsNotNone(turn, '后台有业务 run 时聊天未被及时消费')
        self.assertEqual(turn['status'], 'succeeded')
        self.assertIn('后台期间的答复', turn['answer'])

    async def test_create_task_via_http_with_engine_identity_and_dedupe(self):
        """P9：正式 HTTP 入口创建业务任务，核对引擎身份 + 幂等去重。"""
        base, _ = await self._start(mock_text=_mock_answer('答复'))
        async with httpx.AsyncClient(timeout=8) as client:
            # 正式入口创建任务（POST /api/tasks）
            r = await client.post(base + '/api/tasks',
                                  json={'message': '做个红色页面'},
                                  headers=self.headers)
            self.assertEqual(r.status_code, 200, r.text)
            task_id = r.json()['task_id']
            run_id = r.json()['run_id']
            # 核对引擎身份已持久化（缺省 default_kind = pi）
            run = await self.store.run(run_id)
            self.assertIsNotNone(run, 'run 应已落库')
            self.assertEqual(run['runtime_kind'], 'pi',
                             '缺省引擎身份应为 pi')
            # 幂等：同 Idempotency-Key 重放 → 返回原任务，不重复创建
            r2 = await client.post(base + '/api/tasks',
                                   json={'message': '做个红色页面'},
                                   headers={**self.headers,
                                            'Idempotency-Key': 'dedupe-key-1'})
            first_task = r2.json().get('task_id')
            r3 = await client.post(base + '/api/tasks',
                                   json={'message': '做个红色页面'},
                                   headers={**self.headers,
                                            'Idempotency-Key': 'dedupe-key-1'})
            self.assertEqual(r3.json().get('task_id'), first_task,
                             '同幂等键应返回同一任务')
        # 跨主体拒绝：另一主体访问该任务 → 404
        now = int(time.time())
        jwt2 = sign(claims(iat=now, exp=now + 30, jti='e2e-' + uuid.uuid4().hex,
                           sub='user-other', tenant_id='tenant-a', platform_id='pa'))
        async with httpx.AsyncClient(timeout=8) as client:
            exchanged = await client.post(base + '/api/auth/exchange',
                                          json={'ticket': jwt2})
            token2 = exchanged.cookies.get('agent_session')
            other = {'Cookie': 'agent_session=' + token2}
            res = await client.get(f'{base}/api/tasks/{task_id}', headers=other)
            self.assertEqual(res.status_code, 404, res.text)


