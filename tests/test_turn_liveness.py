"""执行器失联的兜底：接单门禁 + 僵尸 turn 看门狗。

对应真实事故（2026-11-11）：Worker 进程被回收之后，两条「确定」在库里
停在 `accepted`，前端一直转圈，客户看到的只有"发出去没回复"，而 API
表面上完全正常。这一组用例锁死三条不许退化的性质：

1. 没有活 Worker 时，接纳层**当场拒绝**，不再收下一条没人执行的消息；
2. 有活 Worker 时也收，但不能把"排队"误判成"卡死"；
3. 不管哪种情况，卡住的 turn 最终都会**结算出一条用户看得见的答复**——
   只改状态不写答复，等于把"没回复"换个地方发生。
"""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_auth_exchange import PG_DSN, new_store, reset_db  # noqa: E402
from webpage_agent.observe import watchdog  # noqa: E402

EXTRA_TABLES = ('session_turns', 'service_heartbeats')


async def _truncate_extra(store):
    """基线的 TABLES 没覆盖 turn 与新表，这里补上（TRUNCATE 会级联）。"""
    async with store._lease() as conn:
        for table in EXTRA_TABLES:
            try:
                await conn.execute(f'TRUNCATE TABLE {table} CASCADE')
            except Exception:                  # noqa: BLE001 表可能还没建
                pass


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class TurnWatchdogTests(unittest.IsolatedAsyncioTestCase):
    """看门狗：把没人执行的 turn 结算成"有话说"的失败。"""

    async def asyncSetUp(self):
        self.store = new_store()
        await reset_db(self.store)
        await _truncate_extra(self.store)

    async def asyncTearDown(self):
        await self.store.close()

    async def _turn(self, turn_id='turn-1', session_id='sess-1'):
        await self.store.create_turn(
            turn_id, session_id, request_id=f'req-{turn_id}',
            request_hash='h', message='确定')
        return turn_id

    async def _age_turn(self, turn_id, seconds):
        """把 created_at 拨到过去：模拟"接纳后一直没人领"。"""
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S',
                              time.gmtime(time.time() - seconds))
        async with self.store._lease() as conn:
            await conn.execute(
                'UPDATE session_turns SET created_at=%s WHERE turn_id=%s',
                (stamp + '.000000+00:00', turn_id))
            await conn.commit()

    async def _answer_of(self, turn_id):
        row = await self.store.turn(turn_id)
        return row

    async def test_没人领的_turn_被结算成可见失败(self):
        await self._turn()
        await self._age_turn('turn-1', 3600)

        stats = await watchdog.reap_once(self.store)

        self.assertEqual(stats['stuck'], 1)
        row = await self._answer_of('turn-1')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error'], 'E_WORKER_OFFLINE')
        # 关键：必须有答复正文，否则用户仍然什么都看不到
        self.assertIn('没有执行成功', row['answer'])
        history = await self.store.messages('sess-1')
        assistant = [m for m in history if m['role'] == 'assistant']
        self.assertEqual(len(assistant), 1)
        self.assertIn('没有执行成功', assistant[0]['content'])

    async def test_同会话还有在跑的_turn_时算排队不误杀(self):
        await self._turn('turn-1')
        await self._turn('turn-2')
        await self._age_turn('turn-1', 3600)
        # turn-2 正在跑且租约有效：turn-1 属于排队，不是没人管
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE session_turns SET status='running', lease_owner='w1', "
                'lease_expires_at=%s WHERE turn_id=%s',
                (time.time() + 600, 'turn-2'))
            await conn.commit()

        stats = await watchdog.reap_once(self.store)

        self.assertEqual(stats['stuck'], 0)
        self.assertEqual((await self._answer_of('turn-1'))['status'], 'accepted')

    async def test_执行中断的_turn_被结算且不自动重试(self):
        await self._turn()
        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE session_turns SET status='running', lease_owner='w1', "
                'lease_expires_at=%s WHERE turn_id=%s',
                (time.time() - 900, 'turn-1'))
            await conn.commit()

        stats = await watchdog.reap_once(self.store)

        self.assertEqual(stats['lost'], 1)
        row = await self._answer_of('turn-1')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error'], 'E_WORKER_LOST')
        self.assertIn('结果未知', row['answer'])

    async def test_有活_worker_时排队容忍度更长(self):
        await self._turn('turn-1')
        await self._age_turn('turn-1', 200)
        await self.store.beat('worker', 'w1')

        stats = await watchdog.reap_once(self.store)

        # 200 秒：超过"无 Worker 容忍期"，但 Worker 在线，属于正常排队
        self.assertEqual(stats['stuck'], 0)
        self.assertEqual(stats['workers_alive'], 1)

        await self._age_turn('turn-1', 3600)
        stats = await watchdog.reap_once(self.store)
        self.assertEqual(stats['stuck'], 1)
        # 成因不同，写给用户的话也必须不同：这里不是"没有执行器"，
        # 而是"执行器在线但一直没轮到"，说成前者会把人带到错误的排查方向。
        row = await self._answer_of('turn-1')
        self.assertEqual(row['error'], 'E_TURN_STUCK')
        self.assertIn('排队太久', row['answer'])

    async def test_重复扫描不会写出第二条答复(self):
        await self._turn()
        await self._age_turn('turn-1', 3600)
        await watchdog.reap_once(self.store)
        await watchdog.reap_once(self.store)

        history = await self.store.messages('sess-1')
        assistant = [m for m in history if m['role'] == 'assistant']
        self.assertEqual(len(assistant), 1)


class NoWorkerGateTests(unittest.IsolatedAsyncioTestCase):
    """接纳门禁：没有活 Worker 时不能收下消息。"""

    def _hub(self, live):
        # 直接用 Hub：它的构造函数很轻（只挂 store 与几个内存字段），
        # 单独 import 两个 mixin 反而会踩到 api_hub ↔ hub_session 的循环导入。
        from webpage_agent.api.api_hub import Hub
        hub = Hub(store=SimpleNamespace(
            session=AsyncMock(return_value=None),
            session_tasks=AsyncMock(return_value=[]),
            turns_of=AsyncMock(return_value=[]),
            live_count=AsyncMock(return_value=live),
            create_turn=AsyncMock(return_value={
                'turn_id': 't1', 'message_id': 1, 'status': 'accepted',
                'replayed': False, 'kind': 'chat', 'turn_seq': 1}),
        ))
        return hub

    def _principal(self):
        return SimpleNamespace(tenant_id='default', user_id='default',
                               role='member', authenticated=False,
                               subject='')

    def setUp(self):
        from webpage_agent.core.config import settings
        from webpage_agent.api import hub_messages
        hub_messages.reset_alive_cache()
        self.settings = settings
        self._original = settings.require_live_worker
        object.__setattr__(settings, 'require_live_worker', True)

    def tearDown(self):
        from webpage_agent.api import hub_messages
        object.__setattr__(self.settings, 'require_live_worker', self._original)
        hub_messages.reset_alive_cache()

    async def test_没有活_worker_时拒收(self):
        from webpage_agent.api.hub_messages import NoWorkerAvailable
        hub = self._hub(live=0)
        with self.assertRaises(NoWorkerAvailable):
            await hub.accept_session_message(
                'sess-1', request_id='r1', message='你好',
                principal=self._principal())
        hub.store.create_turn.assert_not_awaited()

    async def test_有活_worker_时正常接纳(self):
        hub = self._hub(live=1)
        result = await hub.accept_session_message(
            'sess-1', request_id='r1', message='你好',
            principal=self._principal())
        self.assertEqual(result['status'], 'accepted')
        hub.store.create_turn.assert_awaited()


if __name__ == '__main__':
    unittest.main()
