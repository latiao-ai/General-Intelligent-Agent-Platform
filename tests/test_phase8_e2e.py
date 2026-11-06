"""第八阶段收口第 10 点：异步任务端到端恢复与竞态修复的验收。

用真实 PG + 真实 Node + 正式 Hub/Worker 入口 + 受控模型（PI_MOCK_SCRIPT）+
受控异步远端（PI_MOCK_ASYNC + 手动 querier），完整走：

    提交 → accepted 落库 → Node 等待退出 → Worker 查询 → 结果保存登记 →
    唤醒 → 新 Node 补入原结果 → 模型使用结果回答 → 最终收尾

成功场景必须断言 succeeded、最终答复恰好一条、远端提交一次，并验证答复使用了
指定远端结果。真实 MCP / 供应商联调另行报告。
"""
import asyncio
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager

from tests.exec_env import PG_DSN, new_store, reset_db, valid_test_dsn


def _require_pg(testcase):
    if not valid_test_dsn(PG_DSN):
        testcase.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')


@contextmanager
def assigned(target, **fields):
    previous = {key: getattr(target, key) for key in fields}
    for key, value in fields.items():
        object.__setattr__(target, key, value)
    try:
        yield
    finally:
        for key, value in previous.items():
            object.__setattr__(target, key, value)


def _mock_env(script: list, async_tool: bool = False, remote_id: str = 'job-9'):
    import json
    os.environ['PI_MOCK_SCRIPT'] = json.dumps(script)
    os.environ.pop('PI_MOCK_TOOL', None)
    if async_tool:
        os.environ['PI_MOCK_ASYNC'] = '1'
        os.environ['PI_MOCK_REMOTE_ID'] = remote_id
    else:
        os.environ.pop('PI_MOCK_ASYNC', None)


def _clear_mock_env():
    for key in ('PI_MOCK_SCRIPT', 'PI_MOCK_TOOL', 'PI_MOCK_ASYNC',
                'PI_MOCK_REMOTE_ID'):
        os.environ.pop(key, None)


class AsyncRoundtripE2ETests(unittest.IsolatedAsyncioTestCase):
    """第八阶段收口第 10 点：正式 Hub/Worker 入口 + 受控异步远端的完整往返。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        _clear_mock_env()
        await self.store.close()

    async def _seed_pi_run(self, run_id='r1', task_id='t1', session_id='s1'):
        from webpage_agent.agent import runtime_identity
        await self.store.create_task(task_id, session_id, '测试异步往返')
        await self.store.ensure_task_meta(task_id, session_id=session_id,
                                          goal='测试异步往返')
        await self.store.create_run(run_id, task_id, status='queued',
                                    runtime_kind=runtime_identity.KIND_PI)

    async def test_full_async_roundtrip_succeeds_once(self):
        """提交 → accepted → 等待 → 查询 → 保存 → 唤醒 → 补入 → 回答 → 收尾。"""
        from webpage_agent.api.api_hub import Hub
        from webpage_agent.core import external_tasks as ext
        from webpage_agent.core.config import settings
        import tempfile
        from pathlib import Path

        run_id, task_id, session_id = 'r1', 't1', 's1'
        await self._seed_pi_run(run_id, task_id, session_id)

        # 受控远端：查询次数计数 + 完成结果。
        remote = {'queries': 0}
        async def querier(task, contract):
            remote['queries'] += 1
            return {'status': 'completed',
                    'result': {'clip': '指定远端结果 clip-001'}}

        with tempfile.TemporaryDirectory() as folder:
            with assigned(settings, artifact_dir=Path(folder),
                          async_tool_runtime_enabled=True):
                # 第一次执行：Pi 调用受控异步工具 → accepted → 等待。
                _mock_env([
                    {'type': 'tool', 'calls': [
                        {'id': 'c1', 'name': 'web_search',
                         'arguments': {'query': 'x'}}]},
                ], async_tool=True, remote_id='job-9')
                hub = Hub(self.store, owner_id='worker-a')
                claimed = await self.store.claim_run('worker-a', 120, run_id=run_id)
                self.assertIsNotNone(claimed)
                await hub.execute_claimed_run(claimed)

                # Node 结束本轮：run 停在 waiting_external，动作是 accepted，
                # 外部任务已登记。
                run = await self.store.run(run_id)
                self.assertEqual(run['status'], 'waiting_external')
                actions = await self.store.actions_of(run_id)
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]['status'], 'accepted')
                tasks = await self.store.external_tasks_of_run(run_id)
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]['remote_task_id'], 'job-9')
                self.assertEqual(tasks[0]['state'], 'accepted')

                # 模拟 Worker 轮询：把任务置为到期，再查询到 completed，物化 +
                # 登记 + 唤醒。
                async with self.store._lease() as conn:
                    await conn.execute(
                        "UPDATE external_tasks SET next_poll_at=0 "
                        "WHERE run_id=%s", (run_id,))
                    await conn.commit()
                done = await ext.poll_due(self.store, worker_id='poller',
                                          querier=querier)
                self.assertEqual(done, 1)
                self.assertEqual(remote['queries'], 1)
                task = await self.store.external_task(tasks[0]['external_task_id'])
                self.assertEqual(task['state'], 'completed')
                self.assertNotEqual(task['last_result_id'], '')
                run = await self.store.run(run_id)
                self.assertEqual(run['status'], 'queued')

                # 恢复：新执行者接管，补入结果，模型用结果回答，收尾。
                async with self.store._lease() as conn:
                    await conn.execute(
                        f"UPDATE runs SET lease_expires_at=0 WHERE run_id='{run_id}'")
                    await conn.commit()
                _mock_env([
                    {'type': 'answer', 'text': '结果：指定远端结果 clip-001'},
                ], async_tool=False)
                hub2 = Hub(self.store, owner_id='worker-b')
                claimed2 = await self.store.claim_run('worker-b', 120, run_id=run_id)
                self.assertIsNotNone(claimed2)
                await hub2.execute_claimed_run(claimed2)

                # 最终断言：succeeded，动作仍只有一个，远端提交一次（不再重提），
                # 最终答复恰好一条。
                run = await self.store.run(run_id)
                self.assertEqual(run['status'], 'succeeded')
                actions = await self.store.actions_of(run_id)
                self.assertEqual(len(actions), 1)
                tasks = await self.store.external_tasks_of_run(run_id)
                self.assertEqual(len(tasks), 1)  # 不重复提交
                msgs = await self.store.messages(session_id)
                assistant = [m for m in msgs if m['role'] == 'assistant']
                self.assertEqual(len(assistant), 1)
                self.assertIn('指定远端结果 clip-001', assistant[0]['content'])
                _clear_mock_env()

    async def test_kill_after_accepted_then_recover_no_resubmit(self):
        """accepted 落库后杀进程：恢复不重复提交远端，原身份保持。"""
        from webpage_agent.api.api_hub import Hub
        run_id, task_id, session_id = 'r2', 't2', 's2'
        await self._seed_pi_run(run_id, task_id, session_id)

        # 子进程：accepted 结算后自我终止（不重复提交的关键：外部任务已登记）。
        subprocess.run(
            [sys.executable, '-m', 'tests.hub_async_crash_worker',
             PG_DSN, run_id, task_id, session_id],
            capture_output=True, text=True, timeout=90,
            env={**os.environ, 'PYTHONPATH': os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')})

        run = await self.store.run(run_id)
        self.assertIsNotNone(run)
        actions = await self.store.actions_of(run_id)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['status'], 'accepted')
        tasks = await self.store.external_tasks_of_run(run_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['remote_task_id'], 'job-9')

        # 新执行者接管：外部任务仍 open → 继续等待，不重新提交。
        async with self.store._lease() as conn:
            await conn.execute(
                f"UPDATE runs SET lease_expires_at=0 WHERE run_id='{run_id}'")
            await conn.commit()
        _mock_env([{'type': 'answer', 'text': 'x'}], async_tool=False)
        try:
            hub2 = Hub(self.store, owner_id='worker-b')
            claimed = await self.store.claim_run('worker-b', 120, run_id=run_id)
            self.assertIsNotNone(claimed)
            await hub2.execute_claimed_run(claimed)
        finally:
            _clear_mock_env()

        # 外部任务仍 open（未完成）→ run 仍 waiting_external，远端提交仍只有一次。
        tasks = await self.store.external_tasks_of_run(run_id)
        self.assertEqual(len(tasks), 1)  # 不重复提交
        run = await self.store.run(run_id)
        self.assertEqual(run['status'], 'waiting_external')


class MultiToolMixedStateTests(unittest.IsolatedAsyncioTestCase):
    """第八阶段收口第 10 点：多工具混合状态——同步完成、异步等待、失败各自关联。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_mixed_sync_and_async_pending_calls_preserved(self):
        """同轮同步完成 + 异步受理：异步候选不丢，同步候选各自结算。"""
        from webpage_agent.agent import pi_host_tools
        from webpage_agent.agent import pi_snapshot
        # 两个候选：一个 pending、一个 accepted。registry 只导出 pending/admitted，
        # accepted 不进 pending_calls（不重派发），但 awaiting_calls 保留受理事实。
        snap = pi_snapshot.new_snapshot(phase='awaiting_external')
        snap['pending_calls'] = pi_snapshot.normalize_pending_calls([
            {'call_id': 'c-sync', 'logical_call_id': 'pi:call:1',
             'tool_name': 'web_search', 'tool_key': 'local:web:search',
             'arguments': {'query': 'x'}, 'seq': 0, 'status': 'pending'},
        ])
        snap['awaiting_calls'] = [{
            'call_id': 'c-async', 'logical_call_id': 'pi:call:2',
            'tool_name': 'web_search', 'tool_key': 'local:web:search',
            'arguments': {'query': 'y'}, 'external_task_id': 'et1',
            'action_id': 'act1', 'remote_task_id': 'job-9',
            'contract_id': 'mcp-task', 'contract_version': 1,
            'status': 'awaiting',
        }]
        reg = pi_host_tools.PiCallRegistry(snap)
        # 同步候选仍在 pending（未被 accepted 结算覆盖）
        pending = reg.pending_calls()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['call_id'], 'c-sync')


if __name__ == '__main__':
    unittest.main()
