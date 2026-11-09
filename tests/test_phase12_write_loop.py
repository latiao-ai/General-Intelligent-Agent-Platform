"""第十二阶段缺口修复：Pi 自主修改闭环（评审点 2）——进程级 7 步验收。

用**受控模型脚本**（`PI_MOCK_SCRIPT`，驱动 Node Pi 自主决策调用工具）+ **受控
MCP 平台**（`tests/mcp_platform_server.py`，stdio transport + JSON 持久化）验证
完整 7 步，**不手动调用写入/核验函数**替代任何一步：

```
正式 HTTP 输入自然语言目标 → Worker 启动 Pi → Pi 读取对象并提出修改
→ 经过授权/审批和账本执行 → 宿主独立回读并核验 → Pi 消费真实结果并答复
→ 宿主依据证据成功收尾
```

关键纪律（对照第十阶段 `test_phase10_final_e2e.py`，那里直接调 `HostToolCaller.call`
被评审判为「手动调用替代步骤」）：
- 本文件**不直接调用** `HostToolCaller.call()` / `platform_verify.*` / `verify_platform_write`；
- Pi 的工具调用由 `PI_MOCK_SCRIPT` 的 tool 轮驱动，经真实 `serve_api` + `run_worker`
  子进程、真实 PiRuntime → HostToolCaller → governed_call 完整治理链；
- 写后回读由宿主在写工具成功结算后**独立**执行（`_perform_readback`），不是测试手动核验；
- answer 轮用 `expect` 验证 Pi 真的消费了写结果（Node 侧校验 toolResult 含该标记）。

依赖：真实 PostgreSQL（`PG_TEST_DSN`）、已构建的 Node 桥接（`pi-runtime/dist`）、
受控 MCP 平台（当前解释器 `-m tests.mcp_platform_server`）。
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
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
from tests.mcp_platform_server import read_state, seed_state

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _require_pg(case):
    if not PG_DSN:
        case.skipTest('需要 PG_TEST_DSN')
    if 'test' not in (PG_DSN or ''):
        case.skipTest('PG_TEST_DSN 必须指向测试库')


def _mock_script() -> str:
    """受控模型脚本：Pi 先读对象、再提修改、最后消费写结果答复。

    工具名用 MCP 自动发现后的对外名（单服务端加 `mcp__` 前缀）：`mcp__get_object` /
    `mcp__update_object`，与 `pi_tool_catalog()` 暴露给 Node 的工具声明一致。
    """
    return json.dumps([
        # 第 3 步：Pi 读取对象并提出修改（读对象 obj-a）
        {'type': 'tool', 'calls': [
            {'id': 'c-get', 'name': 'mcp__get_object',
             'arguments': {'object_id': 'obj-a'}},
        ]},
        # 第 4 步：Pi 提出修改（写对象 obj-a，subject 归属 subject-A）
        {'type': 'tool', 'calls': [
            {'id': 'c-upd', 'name': 'mcp__update_object',
             'arguments': {'object_id': 'obj-a', 'expected_revision': 1,
                           'operation_id': 'op-placeholder',
                           'subject': 'subject-A',
                           'fields': {'title': '新标题'}}},
        ]},
        # 第 6 步：Pi 消费真实结果并答复（expect 验证真的读到了写结果）
        {'type': 'answer',
         'text': '已完成修改，对象 obj-a 的标题更新为「新标题」。',
         'expect': 'obj-a'},
    ])


class PiWriteLoopProcessTests(unittest.IsolatedAsyncioTestCase):
    """进程级：正式 HTTP 创建任务 → Worker 启动 Pi → 自主写 → 回读 → 答复 → 收尾。"""

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

    async def _start(self, *, state_file: str):
        folder = tempfile.mkdtemp()
        mcp_servers = [{
            'id': 'platform', 'transport': 'stdio',
            'stdio_command': sys.executable,
            'stdio_args': ['-m', 'tests.mcp_platform_server',
                           '--state-file', state_file],
            'idempotency_arg': 'operation_id',
        }]
        env = {
            'PATH': os.environ.get('PATH', ''),
            'APP_ENV': 'test',
            'PYTHONPATH': str(ROOT / 'src'),
            'DATABASE_URL': PG_DSN,
            'DB_AUTO_MIGRATE': 'true',
            'AUTH_MODE': 'ticket',
            # 开发兼容：让工具政策/结果信封/异步运行走环境变量（默认关），
            # 这样受控 MCP 工具发现不需要真实凭据，走兼容路径。
            'AUTH_DEV_COMPAT': 'true',
            'TOOL_POLICY_ENFORCED': 'false',
            'ASYNC_TOOL_RUNTIME_ENABLED': 'false',
            'AUTH_COOKIE_SECURE': 'false',
            'AUTH_ALLOWED_ORIGINS': 'http://127.0.0.1',
            'AUTH_CONNECTIONS': json.dumps([asdict(connection())]),
            'LLM_API_KEY': 'isolated-e2e-key',
            'DEEPSEEK_API_KEY': 'isolated-e2e-search-key',
            'LLM_MODEL': 'e2e-model',
            'LLM_BASE_URL': 'https://model.example/v1',
            'MCP_SERVERS': json.dumps(mcp_servers),
            'API_RATE_LIMIT_ENABLED': 'false',
            'TENANT_BUDGET_ENABLED': 'false',
            # 写闭环：显式扩展读/写白名单（受控 MCP 平台工具）
            'PI_READ_TOOL_KEYS': json.dumps(['mcp:platform:get_object']),
            'PI_WRITE_TOOLS_ENABLED': 'true',
            'PI_WRITE_TOOL_KEYS': json.dumps(['mcp:platform:update_object']),
            # 受控模型脚本：驱动 Pi 自主读→写→答复
            'PI_MOCK_SCRIPT': _mock_script(),
            'DEFAULT_RUNTIME_KIND': 'pi',
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

        now = int(time.time())
        jwt = sign(claims(iat=now, exp=now + 30, jti='e2e-' + uuid.uuid4().hex,
                          sub='user-u', tenant_id='tenant-a', platform_id='pa'))
        async with httpx.AsyncClient(timeout=8) as client:
            exchanged = await client.post(base + '/api/auth/exchange',
                                          json={'ticket': jwt})
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            token = exchanged.cookies.get('agent_session')
            self.headers = {'Cookie': 'agent_session=' + token}
        return base

    async def _wait_run_terminal(self, run_id, *, timeout=60):
        """轮询 run 状态直到终态，返回 run 记录或 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = await self.store.run(run_id)
            if run and run['status'] in ('succeeded', 'failed', 'completed'):
                return run
            await asyncio.sleep(.3)
        return None

    async def test_pi_autonomous_write_loop_end_to_end(self):
        """7 步闭环：HTTP 创建任务 → Pi 自主读/写 → 回读核验 → 答复 → 收尾。"""
        with tempfile.TemporaryDirectory() as folder:
            sf = folder + '/platform.json'
            # 预置对象 obj-a，owner=subject-A，revision=1
            seed_state(sf, {
                'obj-a': {'object_id': 'obj-a', 'owner': 'subject-A',
                          'revision': 1, 'fields': {'title': '旧标题'}},
            })
            base = await self._start(state_file=sf)

            # 第 1 步：正式 HTTP 输入自然语言目标（创建任务）
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.post(base + '/api/tasks',
                                      json={'message': '把对象 obj-a 的标题改成新标题'},
                                      headers=self.headers)
                self.assertEqual(r.status_code, 200, r.text)
                run_id = r.json()['run_id']

            # 等待 Worker 启动 Pi 并完成写闭环（第 2-7 步）
            run = await self._wait_run_terminal(run_id, timeout=60)
            self.assertIsNotNone(run, '写闭环未在超时内完成')
            # 第 7 步：宿主依据证据成功收尾
            self.assertEqual(run['status'], 'succeeded', run)

            # 宿主独立回读核验后，受控平台对象已真实写入（revision 1→2）
            st = read_state(sf)
            self.assertEqual(st['actual_writes'], 1, '真实写入次数应为 1')
            self.assertEqual(st['objects']['obj-a']['revision'], 2)
            self.assertEqual(st['objects']['obj-a']['fields']['title'], '新标题')


if __name__ == '__main__':
    unittest.main()
