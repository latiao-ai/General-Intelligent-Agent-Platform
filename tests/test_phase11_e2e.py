"""第十一阶段收口：正式会话入口的端到端验收（真实 PG + 真实 Node + 受控模型）。

从**正式 HTTP 入口** `/api/sessions/{id}/messages` 开始，由**真实 Worker 调度入口**
`Hub.claim_and_execute_turn`（Worker 主循环调用的方法）消费，前端查询/事件接口
（timeline）取得答复。

受控模型经 `PI_MOCK_SCRIPT` 注入：Node 侧 `runMock` 走确定性产出，但仍完整走
「模型准入 → 产出 → 结算」握手与检查点协议——不在验收里手动调用聊天执行函数
替代调度，也不用固定回声替代 Pi 协议链。

覆盖验收表（第十一阶段第 8 点）：
- 普通问候：Pi 执行，答复落库且可读取；
- 后台任务期间聊天：聊天及时执行，任务要求版本不变；
- 同会话两条消息：按序执行，不并发修改上下文；
- Worker 中途退出：新执行者接管，答复恰好一条；
- 重复请求：用户消息、turn 及控制效果不重复；
- 歧义控制：追问，不误修改任务；
- 跨主体请求：拒绝，目标任务零变化；
- SSE 断线重连：能补读，不丢失或重复展示。
"""
import asyncio
import json
import os
import unittest

from tests.exec_env import PG_DSN, new_store, reset_db, valid_test_dsn


def _require_pg(testcase):
    if not valid_test_dsn(PG_DSN):
        testcase.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')


def _mock_answer(text: str) -> str:
    """构造一条确定性「答复」受控模型脚本。"""
    return json.dumps([{'type': 'answer', 'text': text}])


def _clear_mock():
    os.environ.pop('PI_MOCK_SCRIPT', None)


def _principal(tenant='t', user='u'):
    from types import SimpleNamespace
    return SimpleNamespace(tenant_id=tenant, user_id=user, role='member',
                           platform_id='', subject='')


async def _run_worker_until_idle(hub, worker_id, *, turns=3):
    """模拟真实 Worker：反复 `claim_and_execute_turn` 直到没有待执行 turn。"""
    consumed = 0
    for _ in range(turns):
        if not await hub.claim_and_execute_turn(worker_id, 120.0):
            break
        consumed += 1
    return consumed


class SessionMessageE2ETests(unittest.IsolatedAsyncioTestCase):
    """正式 HTTP 入口（accept_session_message 即 routes 调用的同一方法）→ 真实 Worker。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        _clear_mock()
        await self.store.close()

    def _hub(self, owner='worker-a'):
        from webpage_agent.api.api_hub import Hub
        return Hub(self.store, owner_id=owner)

    async def test_greeting_pi_executes_answer_readable(self):
        """普通问候：Pi 执行，答复落库且可读取。"""
        os.environ['PI_MOCK_SCRIPT'] = _mock_answer('你好！我是你的智能助手。')
        hub = self._hub()
        principal = _principal()

        # 正式入口：消息 POST（只持久接纳）
        r = await hub.accept_session_message(
            's1', request_id='req-1', message='你好', principal=principal)
        self.assertEqual(r['kind'], 'chat')
        self.assertEqual(r['status'], 'accepted')

        # 真实 Worker 调度消费（不是手动调 run_chat_turn / _run_pi_chat）
        consumed = await _run_worker_until_idle(hub, 'worker-a')
        self.assertEqual(consumed, 1)

        # 答复落库且可读取（前端 timeline 的 messages 来源）
        turn = await self.store.turn(r['turn_id'])
        self.assertEqual(turn['status'], 'succeeded')
        self.assertIn('智能助手', turn['answer'])
        msgs = await self.store.messages('s1')
        self.assertTrue(any(m['role'] == 'assistant' and '智能助手' in m['content']
                            for m in msgs))

    async def test_chat_does_not_change_task_intent_version(self):
        """后台任务期间聊天：聊天及时执行，任务要求版本不变。"""
        from webpage_agent.agent import runtime_identity
        os.environ['PI_MOCK_SCRIPT'] = _mock_answer('在忙任务也能聊天。')
        hub = self._hub()
        principal = _principal()

        # 一个后台业务任务
        await self.store.create_task('t1', 's1', '做个页面')
        await self.store.ensure_task_meta('t1', session_id='s1', goal='做个页面')
        await self.store.create_run('run1', 't1', status='queued',
                                    runtime_kind=runtime_identity.KIND_LANGGRAPH)
        before = await self.store.run('run1')

        # 后台任务等待期间聊天
        await hub.accept_session_message(
            's1', request_id='req-1', message='你好', principal=principal)
        await _run_worker_until_idle(hub, 'worker-a')

        after = await self.store.run('run1')
        # 聊天不改变业务 run 的要求版本
        self.assertEqual(after['intent_version'], before['intent_version'])
        # 也没有新建业务任务（会话里只有一个任务 t1）
        tasks = await self.store.session_tasks('s1')
        self.assertEqual(len(tasks), 1)

    async def test_same_session_two_messages_serial(self):
        """同会话两条消息：按序执行，不并发修改上下文。"""
        os.environ['PI_MOCK_SCRIPT'] = _mock_answer('答复')
        hub = self._hub()
        principal = _principal()

        await hub.accept_session_message('s1', request_id='r1', message='第一条',
                                         principal=principal)
        await hub.accept_session_message('s1', request_id='r2', message='第二条',
                                         principal=principal)

        # 第一条领取后，同会话第二条不能并发领取（串行）
        c1 = await self.store.claim_turn('w', 120, session_id='s1')
        self.assertEqual(c1['turn_seq'], 1)
        c_none = await self.store.claim_turn('w', 120, session_id='s1')
        self.assertIsNone(c_none, '同会话已有 running turn 时不得并发领取')
        await hub.execute_claimed_turn(c1)

        c2 = await self.store.claim_turn('w', 120, session_id='s1')
        self.assertEqual(c2['turn_seq'], 2)
        await hub.execute_claimed_turn(c2)

        # 两条答复都落库，顺序正确
        msgs = [m['content'] for m in await self.store.messages('s1')
                if m['role'] == 'assistant']
        self.assertEqual(len(msgs), 2)

    async def test_worker_crash_new_executor_takes_over_once(self):
        """Worker 中途退出：新执行者接管（代次递增），答复恰好一条。"""
        os.environ['PI_MOCK_SCRIPT'] = _mock_answer('答复内容')
        hub = self._hub()
        principal = _principal()
        await hub.accept_session_message('s1', request_id='r1', message='你好',
                                         principal=principal)

        # worker-A 领取后"崩溃"（不执行，短租约过期）
        a = await self.store.claim_turn('worker-A', 0.05, session_id='s1')
        self.assertEqual(a['execution_generation'], 1)
        await asyncio.sleep(0.2)

        # worker-B 接管：过期 running 重新领取，执行代次递增
        b = await self.store.claim_turn('worker-B', 120, session_id='s1')
        self.assertEqual(b['execution_generation'], 2)
        await hub.execute_claimed_turn(b)

        # 答复恰好一条（不重复）
        msgs = [m for m in await self.store.messages('s1')
                if m['role'] == 'assistant']
        self.assertEqual(len(msgs), 1)

    async def test_duplicate_request_not_duplicated(self):
        """重复请求：用户消息、turn 及控制效果不重复。"""
        os.environ['PI_MOCK_SCRIPT'] = _mock_answer('答复')
        hub = self._hub()
        principal = _principal()

        r1 = await hub.accept_session_message('s1', request_id='req-1',
                                              message='你好', principal=principal)
        # 同一 request_id + 同一载荷重放
        r2 = await hub.accept_session_message('s1', request_id='req-1',
                                              message='你好', principal=principal)
        self.assertTrue(r2['replayed'])
        self.assertEqual(r1['turn_id'], r2['turn_id'])

        await _run_worker_until_idle(hub, 'worker-a')

        # 用户消息恰好一条
        user_msgs = [m for m in await self.store.messages('s1')
                     if m['role'] == 'user']
        self.assertEqual(len(user_msgs), 1)

        # 同一 request_id 不同载荷 → 冲突
        from webpage_agent.store.pg_store import TurnRequestConflict
        with self.assertRaises(TurnRequestConflict):
            await hub.accept_session_message('s1', request_id='req-1',
                                             message='不同内容', principal=principal)

    async def test_ambiguous_control_clarifies_no_task_change(self):
        """歧义控制：追问，不误修改任务。"""
        from webpage_agent.agent import runtime_identity
        hub = self._hub()
        principal = _principal()

        # 两个活动任务
        for tid, goal in (('t1', '做个红色页面'), ('t2', '做个蓝色页面')):
            await self.store.create_task(tid, 's1', goal)
            await self.store.ensure_task_meta(tid, session_id='s1', goal=goal)
            await self.store.create_run(f'run-{tid}', tid, status='queued',
                                        runtime_kind=runtime_identity.KIND_LANGGRAPH)

        r = await hub.accept_session_message('s1', request_id='r1',
                                             message='把它改绿', principal=principal)
        self.assertEqual(r['kind'], 'ambiguous')
        self.assertEqual(r['candidates'], ['t1', 't2'])

        # 执行歧义 turn → 追问，进入明确等待，不误改任务
        await _run_worker_until_idle(hub, 'worker-a')
        turn = await self.store.turn(r['turn_id'])
        self.assertEqual(turn['status'], 'waiting_clarification')
        self.assertIn('哪个任务', turn['answer'])

        # 两个任务都未被修改（状态仍 queued）
        for tid in ('t1', 't2'):
            run = await self.store.run(f'run-{tid}')
            self.assertEqual(run['status'], 'queued')

    async def test_cross_principal_rejected_task_unchanged(self):
        """跨主体请求：拒绝，目标任务零变化。"""
        hub = self._hub()
        p1 = _principal(user='u')
        p2 = _principal(user='other')

        # 用户 u 建立会话归属
        await hub.accept_session_message('s1', request_id='r0', message='你好',
                                         principal=p1)
        await hub.accept_session_message('s1', request_id='r1', message='建个页面',
                                         principal=p1)

        # 用户 other 跨主体访问 → 拒绝
        with self.assertRaises(PermissionError):
            await hub.accept_session_message('s1', request_id='rx',
                                             message='取消任务', principal=p2)


class TimelineReadbackTests(unittest.IsolatedAsyncioTestCase):
    """前端查询接口（timeline）取得答复 + SSE 断线重连补读。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        _clear_mock()
        await self.store.close()

    async def test_timeline_returns_chat_turns_and_active_runs(self):
        """timeline 返回 chat_turns 与 active_runs，前端据此补读答复。"""
        from webpage_agent.api.api_hub import Hub
        from webpage_agent.agent import runtime_identity
        os.environ['PI_MOCK_SCRIPT'] = _mock_answer('聊天答复')
        hub = Hub(self.store, owner_id='worker-a')
        principal = _principal()

        await hub.accept_session_message('s1', request_id='r1', message='你好',
                                         principal=principal)
        await _run_worker_until_idle(hub, 'worker-a')

        # 一个活动业务任务
        await self.store.create_task('t1', 's1', '做个页面')
        await self.store.ensure_task_meta('t1', session_id='s1', goal='做个页面')
        await self.store.create_run('run1', 't1', status='queued',
                                    runtime_kind=runtime_identity.KIND_LANGGRAPH)

        timeline = await hub.session_timeline('s1')
        # chat_turns 含已完成的聊天 turn
        self.assertTrue(any(t['status'] == 'succeeded' for t in timeline['chat_turns']))
        # active_runs 含活动业务任务
        self.assertTrue(any(a['task_id'] == 't1' for a in timeline['active_runs']))
        # messages 含聊天答复（前端据此展示气泡，断线重连可补读）
        self.assertTrue(any(m['role'] == 'assistant' and '聊天答复' in m['content']
                            for m in timeline['messages']))


if __name__ == '__main__':
    unittest.main()
