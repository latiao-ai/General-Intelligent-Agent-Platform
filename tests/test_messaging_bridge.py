"""AgentBridge：IM 消息 → 会话 turn → 答复（真实 PostgreSQL，假执行者）。

执行者模拟 Worker 的领取动作：从 `pending_turns` 拿到 turn 后直接结算。这样
bridge 的等待、幂等、超时、失败文案都是真的，只有模型调用被换掉。
"""
import asyncio
import unittest

from tests.exec_env import PG_DSN, new_store, reset_db

from webpage_agent.messaging.bridge import ImBridge
from webpage_agent.messaging.models import Inbound


def event(message_id='om_1', text='你好'):
    return Inbound(platform='feishu', tenant='t1', chat_id='oc_1',
                   chat_type='dm', user_id='ou_1', user_name='张三',
                   text=text, message_id=message_id)


class Executor:
    """模拟 Worker：领到一条 turn 就按给定结果结算。"""

    def __init__(self, store, *, status='succeeded', answer='收到'):
        self.store = store
        self.status = status
        self.answer = answer
        self.settled = []
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                rows = await self.store.pending_turns(5)
                for row in rows:
                    if row['turn_id'] in self.settled:
                        continue
                    await self.store.settle_turn(
                        row['turn_id'], status=self.status,
                        answer=self.answer if self.status == 'succeeded' else '',
                        error='boom' if self.status != 'succeeded' else '')
                    self.settled.append(row['turn_id'])
            except asyncio.CancelledError:
                raise
            except Exception:                       # noqa: BLE001
                # 清理顺序是 tearDown 先关池、cleanup 才取消任务；关池后这里
                # 必然报 PoolClosed，不该让它变成用例失败。
                pass
            await asyncio.sleep(0.02)

    async def close(self):
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN')
class BridgeTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_creates_hidden_im_session_and_returns_answer(self):
        executor = Executor(self.store, answer='这是答复')
        self.addAsyncCleanup(executor.close)
        bridge = ImBridge(self.store, timeout=5, poll_seconds=0.02)
        result = await bridge.run(event(), owner_user_id='u1', channel_bot_id='')
        self.assertEqual(result['text'], '这是答复')
        async with self.store._lease() as conn:
            session = await self.store._one(
                conn, 'SELECT * FROM sessions WHERE session_id=%s',
                (result['session_id'],))
            messages = await self.store._all(
                conn, 'SELECT role, content FROM messages WHERE session_id=%s '
                'ORDER BY id', (result['session_id'],))
        self.assertEqual(session['session_kind'], 'im_chat')
        self.assertEqual(int(session['hidden']), 1)
        self.assertEqual([item['role'] for item in messages],
                         ['user', 'assistant'])
        self.assertIn('【feishu · 私聊 · 张三】', messages[0]['content'])

    async def test_same_message_id_replays_turn(self):
        executor = Executor(self.store, answer='只答一次')
        self.addAsyncCleanup(executor.close)
        bridge = ImBridge(self.store, timeout=5, poll_seconds=0.02)
        first = await bridge.run(event(), owner_user_id='u1', channel_bot_id='')
        second = await bridge.run(event(), owner_user_id='u1', channel_bot_id='')
        self.assertEqual(first['turn_id'], second['turn_id'])
        self.assertEqual(first['text'], '只答一次')
        turns = await self.store.turns_of(first['session_id'])
        self.assertEqual(len(turns), 1)

    async def test_timeout_returns_readable_text(self):
        bridge = ImBridge(self.store, timeout=0.35, poll_seconds=0.05)
        result = await bridge.run(event(), owner_user_id='u1', channel_bot_id='')
        self.assertIn('还在处理', result['text'])

    async def test_failed_turn_does_not_leak_stack(self):
        executor = Executor(self.store, status='failed')
        self.addAsyncCleanup(executor.close)
        bridge = ImBridge(self.store, timeout=5, poll_seconds=0.02)
        result = await bridge.run(event(), owner_user_id='u1', channel_bot_id='')
        self.assertIn('抱歉', result['text'])
        self.assertIn('boom', result['text'])
        self.assertNotIn('Traceback', result['text'])

    async def test_bot_persona_is_bound_to_session(self):
        import uuid

        from webpage_agent.store.pg_store import now

        bot_id = 'bot_' + uuid.uuid4().hex[:10]
        async with self.store._lease() as conn:
            await self.store._run(conn,
                'DELETE FROM bots WHERE tenant_id=%s AND owner_user_id=%s',
                ('t1', 'u1'))
            await self.store._run(conn,
                'INSERT INTO bots (bot_id, tenant_id, owner_user_id, handle, '
                ' name, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (bot_id, 't1', 'u1', 'helper', '小助手', now(), now()))
        executor = Executor(self.store, answer='好')
        self.addAsyncCleanup(executor.close)
        bridge = ImBridge(self.store, timeout=5, poll_seconds=0.02)
        result = await bridge.run(event(), owner_user_id='u1', channel_bot_id=bot_id)
        async with self.store._lease() as conn:
            session = await self.store._one(
                conn, 'SELECT bot_id FROM sessions WHERE session_id=%s',
                (result['session_id'],))
        self.assertEqual(session['bot_id'], bot_id)
