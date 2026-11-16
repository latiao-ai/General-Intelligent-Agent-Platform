"""会话级身份：换机器人之后，那个群到底由谁回话。

这一片测的是**界面显示与实际回话是否同一个人**。历史缺陷：
`im_create_session` 是 `ON CONFLICT DO NOTHING`，把渠道绑定的机器人从 A 换成 B
之后，已经聊过的群仍然由 A 回话（只有 `/new` 才换），而界面上写的是 B。

现在的规则：
- 有效身份 = 会话覆盖（`/bind` 指定）优先，否则跟随渠道默认；
- 映射指向的会话人格与有效身份不一致 → 换一代新会话（旧会话与历史保留）；
- 会话被清理过 → 用同一个 `session_id` 幂等补建，不换代。
"""
import unittest

from tests.exec_env import PG_DSN, new_store, reset_db

from webpage_agent.messaging.bridge import ImBridge
from webpage_agent.messaging.models import Inbound

BOT_A = 'bot_a'
BOT_B = 'bot_b'
BOT_C = 'bot_c'


def event(message_id='om_1', text='你好'):
    return Inbound(platform='feishu', tenant='t1', chat_id='oc_1',
                   chat_type='dm', user_id='ou_1', user_name='张三',
                   text=text, message_id=message_id)


class Executor:
    """模拟 Worker：领到 turn 就结算，让 bridge 的等待路径走完。"""

    def __init__(self, store, *, answer='收到'):
        self.store = store
        self.answer = answer
        self.settled = []
        import asyncio
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        import asyncio
        while True:
            try:
                for row in await self.store.pending_turns(5):
                    if row['turn_id'] in self.settled:
                        continue
                    await self.store.settle_turn(
                        row['turn_id'], status='succeeded', answer=self.answer)
                    self.settled.append(row['turn_id'])
            except asyncio.CancelledError:
                raise
            except Exception:                       # noqa: BLE001
                pass
            await asyncio.sleep(0.02)

    async def close(self):
        self.task.cancel()
        try:
            await self.task
        except (Exception, BaseException):          # noqa: BLE001
            pass


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN')
class SessionIdentityTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        await self.store.ready()
        self.executor = Executor(self.store)
        self.addAsyncCleanup(self.executor.close)
        self.bridge = ImBridge(self.store, timeout=5, poll_seconds=0.02)

    async def asyncTearDown(self):
        await self.store.close()

    async def _run(self, message_id, channel_bot_id):
        return await self.bridge.run(
            event(message_id), owner_user_id='u1',
            channel_bot_id=channel_bot_id)

    async def _mapping(self):
        return await self.store.im_session(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1')

    async def _session_bot(self, session_id):
        async with self.store._lease() as conn:
            row = await self.store._one(
                conn, 'SELECT bot_id FROM sessions WHERE session_id=%s',
                (session_id,))
        return row['bot_id'] if row else None

    # ---------- 核心：渠道换绑之后 ----------

    async def test_channel_bot_change_rolls_a_new_session(self):
        first = await self._run('om_1', BOT_A)
        second = await self._run('om_2', BOT_B)

        self.assertNotEqual(first['session_id'], second['session_id'])
        # 旧会话没被改写：它记着当时的人格，历史也还在
        self.assertEqual(await self._session_bot(first['session_id']), BOT_A)
        self.assertEqual(await self._session_bot(second['session_id']), BOT_B)
        mapping = await self._mapping()
        self.assertEqual(mapping['session_id'], second['session_id'])
        self.assertEqual(int(mapping['generation']), 2)

    async def test_same_bot_keeps_one_session(self):
        first = await self._run('om_1', BOT_A)
        second = await self._run('om_2', BOT_A)
        self.assertEqual(first['session_id'], second['session_id'])
        mapping = await self._mapping()
        self.assertEqual(int(mapping['generation']), 1)

    async def test_switching_back_gets_another_generation(self):
        first = await self._run('om_1', BOT_A)
        await self._run('om_2', BOT_B)
        third = await self._run('om_3', BOT_A)
        self.assertNotEqual(first['session_id'], third['session_id'])
        self.assertEqual(await self._session_bot(third['session_id']), BOT_A)
        mapping = await self._mapping()
        self.assertEqual(int(mapping['generation']), 3)

    async def test_unbound_channel_rolling_to_bound_bot(self):
        """渠道从「不绑定」改成绑定机器人，同样要换会话。"""
        first = await self._run('om_1', '')
        second = await self._run('om_2', BOT_A)
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(await self._session_bot(second['session_id']), BOT_A)

    # ---------- 会话覆盖 ----------

    async def test_session_override_wins_over_channel(self):
        first = await self._run('om_1', BOT_A)
        ok = await self.store.im_session_set_bot(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1', bot_id=BOT_C)
        self.assertTrue(ok)
        second = await self._run('om_2', BOT_A)
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(await self._session_bot(second['session_id']), BOT_C)
        # 覆盖值不该被"渠道默认"抹掉
        mapping = await self._mapping()
        self.assertEqual(mapping['bot_id'], BOT_C)

    async def test_clearing_override_returns_to_channel_bot(self):
        await self._run('om_1', BOT_A)
        await self.store.im_session_set_bot(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1', bot_id=BOT_C)
        with_override = await self._run('om_2', BOT_A)
        await self.store.im_session_set_bot(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1', bot_id='')
        back = await self._run('om_3', BOT_A)
        self.assertEqual(await self._session_bot(with_override['session_id']),
                         BOT_C)
        self.assertEqual(await self._session_bot(back['session_id']), BOT_A)
        mapping = await self._mapping()
        self.assertEqual(mapping['bot_id'], '')

    async def test_override_already_matching_channel_does_not_roll(self):
        first = await self._run('om_1', BOT_A)
        await self.store.im_session_set_bot(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1', bot_id=BOT_A)
        second = await self._run('om_2', BOT_A)
        self.assertEqual(first['session_id'], second['session_id'])

    # ---------- 存储层的边界 ----------

    async def test_set_bot_returns_false_without_mapping(self):
        ok = await self.store.im_session_set_bot(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_missing', bot_id=BOT_A)
        self.assertFalse(ok)

    async def test_bind_without_bot_id_keeps_override(self):
        """`/new` 走这条：换会话不该把手工指定的机器人清掉。"""
        await self._run('om_1', BOT_A)
        await self.store.im_session_set_bot(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1', bot_id=BOT_C)
        row = await self.store.im_session_bind(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1', chat_type='dm', session_id='im-next',
            generation=2)
        self.assertEqual(row['bot_id'], BOT_C)
        cleared = await self.store.im_session_bind(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1', chat_type='dm', session_id='im-next',
            generation=2, bot_id='')
        self.assertEqual(cleared['bot_id'], '')

    async def test_cleaned_session_is_rebuilt_on_same_id(self):
        first = await self._run('om_1', BOT_A)
        async with self.store._lease() as conn:
            await self.store._run(
                conn, 'DELETE FROM sessions WHERE session_id=%s',
                (first['session_id'],))
        second = await self._run('om_2', BOT_A)
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual(await self._session_bot(second['session_id']), BOT_A)

    async def test_session_bot_id_distinguishes_missing_from_generic(self):
        await self._run('om_1', '')
        mapping = await self._mapping()
        self.assertEqual(
            await self.store.im_session_bot_id(mapping['session_id']), '')
        self.assertIsNone(await self.store.im_session_bot_id('im-nope'))
