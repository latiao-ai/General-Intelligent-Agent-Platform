"""第十一阶段最后四项定点修复的测试。

覆盖：
1. 按实际在途数量限制聊天并发（超过额度多会话持续输入，实际并发不超限）；
2. 续租 guard 正确取消父执行（执行中撤销租约，父执行停止，Worker 继续）；
3. 聊天历史按 turn 顺序包含上一轮完整答复、排除未来输入；
4. 取消一个聊天执行后，其他聊天和业务任务仍能继续（Worker 安全回收）。
"""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace

from tests.exec_env import PG_DSN, new_store, reset_db, valid_test_dsn


def _require_pg(testcase):
    if not valid_test_dsn(PG_DSN):
        testcase.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')


def _mock_answer(text: str) -> str:
    return json.dumps([{'type': 'answer', 'text': text}])


def _clear_mock():
    os.environ.pop('PI_MOCK_SCRIPT', None)


def _principal(tenant='t', user='u'):
    return SimpleNamespace(tenant_id=tenant, user_id=user, role='member',
                           platform_id='', subject='')


class HistoryOrderTests(unittest.IsolatedAsyncioTestCase):
    """定点修复 3：聊天历史按 turn 顺序，含上一轮答复、排除未来输入。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        _clear_mock()
        await self.store.close()

    async def test_second_turn_sees_first_answer_not_future_input(self):
        os.environ['PI_MOCK_SCRIPT'] = _mock_answer('第一轮答复')
        from webpage_agent.api.api_hub import Hub
        hub = Hub(self.store, owner_id='worker-a')
        p = _principal()

        # 第一条：用户消息 + 答复
        await hub.accept_session_message('s1', request_id='r1',
                                         message='第一条问题', principal=p)
        c1 = await self.store.claim_turn('w', 120, session_id='s1')
        await hub.execute_claimed_turn(c1)

        # 第二条、第三条都已接纳（第二条未执行，第三条是"未来输入"）
        await hub.accept_session_message('s1', request_id='r2',
                                         message='第二条问题', principal=p)
        await hub.accept_session_message('s1', request_id='r3',
                                         message='第三条未来输入', principal=p)

        # 执行第二条时，历史 = 第一条问题 + 第一轮答复（不含第二条问题、第三条）
        c2 = await self.store.claim_turn('w', 120, session_id='s1')
        hist = await self.store.chat_history('s1', c2['turn_id'])
        contents = [h['content'] for h in hist]
        roles = [h['role'] for h in hist]

        self.assertEqual(contents, ['第一条问题', '第一轮答复'],
                         f'第二轮历史应恰好是第一轮问答，实际 {list(zip(roles, contents))}')
        self.assertNotIn('第二条问题', contents)
        self.assertNotIn('第三条未来输入', contents)


class WorkerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """定点修复 1/2/4：并发限流、guard 取消父执行、取消后继续运行。"""

    async def asyncSetUp(self):
        _require_pg(self)
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        _clear_mock()
        await self.store.close()

    async def test_guard_cancels_parent_on_lease_loss(self):
        """定点修复 2：执行中撤销租约，父执行被取消（不再发模型/工具请求）。"""
        from webpage_agent.api.api_hub import Hub
        from webpage_agent.core.config import settings
        hub = Hub(self.store, owner_id='worker-a')
        p = _principal()

        # 用可注入的慢答复生成器模拟"执行中"：能被取消，且记录是否被取消。
        cancelled = asyncio.Event()
        started = asyncio.Event()

        async def slow_responder(message, kind):
            started.set()
            try:
                await asyncio.sleep(30)  # 模拟长时间模型调用
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return '不应到达的答复'

        hub.chat_responder = slow_responder

        # 极短租约，让 guard 快速检测到失租
        object.__setattr__(settings, 'session_turn_lease_seconds', 0.6)

        await hub.accept_session_message('s1', request_id='r1',
                                         message='你好', principal=p)
        claimed = await self.store.claim_turn('w', 0.6, session_id='s1')

        # 在独立 task 里执行（模拟 Worker 派发）
        exec_task = asyncio.create_task(hub.execute_claimed_turn(claimed))
        await asyncio.wait_for(started.wait(), timeout=5)

        # 撤销租约：手动把 lease_owner 改成别人，让 guard 续租失败（模拟被接管）
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE session_turns SET lease_owner='intruder' WHERE turn_id=%s",
                (claimed['turn_id'],))

        # guard 续租失败 → 取消父执行 → slow_responder 收到 CancelledError
        await asyncio.wait_for(cancelled.wait(), timeout=10)

        # 父执行应被取消结束（guard 取消的是父 task，不是 guard 自己）
        try:
            await asyncio.wait_for(exec_task, timeout=10)
            parent_cancelled = False
        except asyncio.CancelledError:
            parent_cancelled = True
        self.assertTrue(parent_cancelled or exec_task.cancelled(),
                        '父执行应被取消（guard 取消父 task）')

        # turn 仍保持 running（失租后不写答复，由新执行者接管）
        turn = await self.store.turn(claimed['turn_id'])
        self.assertIn(turn['status'], ('running', 'accepted', 'retry'),
                      f'失租后不应结算为 succeeded，实际 {turn["status"]}')

        object.__setattr__(settings, 'session_turn_lease_seconds', 120.0)
        hub.chat_responder = None

    async def test_concurrent_chat_limited_by_inflight(self):
        """定点修复 1：多会话持续输入，实际在途聊天数不超额度。

        限流在 main_loop 里通过 `chat_quota = concurrency - len(chat_inflight)`
        实现：在途聊天 task 未回收前，不再领取新的聊天 turn，保证实际并发 ≤ 额度。
        """
        import scripts.run_worker as worker
        from webpage_agent.api.api_hub import Hub
        hub = Hub(self.store, owner_id='worker-a')
        p = _principal()

        # 慢答复模拟"在途"（不被取消，只是慢，持续占住并发额度）
        async def slow_responder(message, kind):
            await asyncio.sleep(0.4)
            return '答复:' + message

        hub.chat_responder = slow_responder
        concurrency = 2

        # 6 个不同会话持续输入
        for i in range(6):
            await hub.accept_session_message(
                f'sess{i}', request_id=f'r{i}', message=f'消息{i}', principal=p)

        # 模拟 main_loop 的限流：chat_inflight 追踪在途，chat_quota 按实际在途算
        chat_inflight: set = set()
        for _ in range(5):  # 多轮循环持续领取
            chat_quota = max(0, concurrency - len(chat_inflight))
            if chat_quota > 0:
                chat_tasks = await worker.drain_chat_turns(hub, 'worker-a',
                                                          chat_quota)
                chat_inflight.update(chat_tasks)
            # 回收已完成的任务（模拟 main_loop 收口）
            if chat_inflight:
                done, pending = await asyncio.wait(chat_inflight, timeout=0)
                chat_inflight = pending
            # 断言：实际在途数始终不超额度
            self.assertLessEqual(len(chat_inflight), concurrency,
                                 '在途聊天数不得超过并发额度')
            await asyncio.sleep(0.05)

        # 等剩余完成
        if chat_inflight:
            await asyncio.gather(*chat_inflight, return_exceptions=True)
        hub.chat_responder = None

    async def test_cancel_one_chat_others_continue(self):
        """定点修复 4：取消一个聊天执行，其他聊天和业务任务仍能继续。"""
        from webpage_agent.api.api_hub import Hub
        hub = Hub(self.store, owner_id='worker-a')
        p = _principal()

        os.environ['PI_MOCK_SCRIPT'] = _mock_answer('正常答复')
        hub.chat_responder = None

        # 三条聊天消息
        for i in range(3):
            await hub.accept_session_message(
                f's{i}', request_id=f'r{i}', message=f'消息{i}', principal=p)

        # 领取三条
        c1 = await self.store.claim_turn('w', 120, session_id='s0')
        c2 = await self.store.claim_turn('w', 120, session_id='s1')
        c3 = await self.store.claim_turn('w', 120, session_id='s2')

        # 把 c1 的执行 task 取消（模拟取消一个聊天执行）
        t1 = asyncio.create_task(hub.execute_claimed_turn(c1))
        t2 = asyncio.create_task(hub.execute_claimed_turn(c2))
        t3 = asyncio.create_task(hub.execute_claimed_turn(c3))

        # 取消 t1
        t1.cancel()
        try:
            await t1
        except asyncio.CancelledError:
            pass
        self.assertTrue(t1.cancelled(), 't1 应被取消')

        # 其他两个应正常完成
        await asyncio.gather(t2, t3)
        turn2 = await self.store.turn(c2['turn_id'])
        turn3 = await self.store.turn(c3['turn_id'])
        self.assertEqual(turn2['status'], 'succeeded', '取消一个不应影响其他聊天')
        self.assertEqual(turn3['status'], 'succeeded', '取消一个不应影响其他聊天')

        # 被取消的 c1 保持 running（不写答复），可被重新领取接管
        turn1 = await self.store.turn(c1['turn_id'])
        self.assertIn(turn1['status'], ('running', 'accepted'),
                      f'被取消的 turn 应保持可恢复状态，实际 {turn1["status"]}')

        # 业务任务仍能创建（验证取消不影响业务链路）
        await self.store.create_task('biz1', 's0', '做个页面')
        self.assertIsNotNone(await self.store.task('biz1'))
