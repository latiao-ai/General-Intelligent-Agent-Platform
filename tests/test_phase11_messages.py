"""第十一阶段：会话消息入口、路由与 turn 的单元/仓储测试。

分层证据（不合并）：
- 路由纯逻辑（无 PG）：route_message 的聊天/查询/控制/歧义判定；
- PG 仓储（真实 PG）：create_turn 的幂等（同键同载荷重放、同键不同载荷冲突）、
  归属校验、turn 顺序。
"""
import unittest

from webpage_agent.api.hub_messages import route_message, request_hash
from tests.exec_env import PG_DSN, new_store, reset_db, valid_test_dsn


class RouteMessageTests(unittest.TestCase):
    """路由纯逻辑（无 PG）。"""

    def test_greeting_is_chat(self):
        self.assertEqual(route_message('你好', active_tasks=[])['kind'], 'chat')

    def test_query_is_query(self):
        self.assertEqual(route_message('今天天气怎么样', active_tasks=[])['kind'],
                         'query')

    def test_control_single_active_target(self):
        route = route_message('把它改蓝', active_tasks=[
            {'task_id': 't1', 'run_id': 'r1'}])
        self.assertEqual(route['kind'], 'control')
        self.assertEqual(route['target_task_id'], 't1')

    def test_control_multiple_active_is_ambiguous(self):
        route = route_message('把它改蓝', active_tasks=[
            {'task_id': 't1', 'run_id': 'r1'}, {'task_id': 't2', 'run_id': 'r2'}])
        self.assertEqual(route['kind'], 'ambiguous')
        self.assertEqual(route['candidates'], ['t1', 't2'])

    def test_control_no_active_targets_create(self):
        self.assertEqual(route_message('建个页面', active_tasks=[])['kind'],
                         'control')

    def test_explicit_target_wins(self):
        route = route_message('补充一下', active_tasks=[
            {'task_id': 't1', 'run_id': 'r1'}, {'task_id': 't2', 'run_id': 'r2'}],
            target_task_id='t2')
        self.assertEqual(route['kind'], 'control')
        self.assertEqual(route['target_task_id'], 't2')

    def test_greeting_never_control(self):
        # "你好" 不含控制动词，也不含查询词 → chat，绝不更新 intent_version
        self.assertEqual(route_message('你好', active_tasks=[
            {'task_id': 't1', 'run_id': 'r1'}])['kind'], 'chat')

    def test_request_hash_stable_and_distinct(self):
        self.assertEqual(request_hash('a', 't'), request_hash('a', 't'))
        self.assertNotEqual(request_hash('a', 't'), request_hash('b', 't'))


class TurnStorageTests(unittest.IsolatedAsyncioTestCase):
    """turn 持久化（真实 PG）。"""

    async def asyncSetUp(self):
        if not valid_test_dsn(PG_DSN):
            self.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_create_turn_idempotent_replay(self):
        r1 = await self.store.create_turn(
            'turn-a', 'sess1', request_id='req-1', request_hash='h1',
            message='你好', tenant_id='t', owner_user_id='u')
        self.assertFalse(r1['replayed'])
        self.assertEqual(r1['status'], 'accepted')
        # 同键同载荷 → 重放原 turn
        r2 = await self.store.create_turn(
            'turn-b', 'sess1', request_id='req-1', request_hash='h1',
            message='你好', tenant_id='t', owner_user_id='u')
        self.assertTrue(r2['replayed'])
        self.assertEqual(r2['turn_id'], 'turn-a')

    async def test_create_turn_conflict_on_different_payload(self):
        await self.store.create_turn(
            'turn-a', 'sess1', request_id='req-1', request_hash='h1',
            message='你好', tenant_id='t', owner_user_id='u')
        from webpage_agent.store.pg_store import TurnRequestConflict
        with self.assertRaises(TurnRequestConflict):
            await self.store.create_turn(
                'turn-b', 'sess1', request_id='req-1', request_hash='h2',
                message='不同', tenant_id='t', owner_user_id='u')

    async def test_turn_sequence_monotonic(self):
        await self.store.create_turn(
            'turn-a', 'sess2', request_id='r1', request_hash='h1',
            message='a', tenant_id='t', owner_user_id='u')
        r2 = await self.store.create_turn(
            'turn-b', 'sess2', request_id='r2', request_hash='h2',
            message='b', tenant_id='t', owner_user_id='u')
        self.assertGreater(r2['turn_seq'], 1)

    async def test_turn_ownership_enforced(self):
        await self.store.create_turn(
            'turn-a', 'sess3', request_id='r1', request_hash='h1',
            message='a', tenant_id='t', owner_user_id='u')
        # 不同主体写同一 session → 拒绝
        with self.assertRaises(PermissionError):
            await self.store.create_turn(
                'turn-b', 'sess3', request_id='r2', request_hash='h2',
                message='b', tenant_id='t', owner_user_id='other')


class ChatTurnRoundtripTests(unittest.IsolatedAsyncioTestCase):
    """聊天 turn 完整闭环：接纳 → 领取 → 答复 → 结算 → 消息（真实 PG）。"""

    async def asyncSetUp(self):
        if not valid_test_dsn(PG_DSN):
            self.skipTest('需要 PG_TEST_DSN（指向一次性测试库，库名含 test）')
        await reset_db()
        self.store = new_store()
        await self.store.ready()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_chat_turn_produces_answer_and_message(self):
        from webpage_agent.api.api_hub import Hub
        from types import SimpleNamespace

        hub = Hub(self.store, owner_id='worker-a')
        principal = SimpleNamespace(tenant_id='t', user_id='u', role='member',
                                    platform_id='', subject='')

        # 受控答复生成器：模型替身依据消息作答（验证「聊天答复」真实产生）
        seen = []
        async def responder(message, kind):
            seen.append((message, kind))
            return f'回声：{message}'

        hub.chat_responder = responder

        # 接纳"你好" → chat turn
        result = await hub.accept_session_message(
            'sess1', request_id='req-1', message='你好', principal=principal)
        self.assertEqual(result['kind'], 'chat')
        self.assertEqual(result['status'], 'accepted')

        # 领取并执行 turn
        done = await hub.run_chat_turn('sess1', owner='worker-a')
        self.assertIsNotNone(done)
        self.assertEqual(done['status'], 'succeeded')
        self.assertEqual(done['answer'], '回声：你好')
        self.assertEqual(seen, [('你好', 'chat')])

        # 结算后 turn 状态 succeeded，assistant 消息已写
        turn = await self.store.turn(done['turn_id'])
        self.assertEqual(turn['status'], 'succeeded')
        msgs = await self.store.messages('sess1')
        self.assertTrue(any(m['role'] == 'assistant' and '回声' in m['content']
                            for m in msgs))

    async def test_chat_does_not_change_task_intent_version(self):
        from webpage_agent.api.api_hub import Hub
        from types import SimpleNamespace

        hub = Hub(self.store, owner_id='worker-a')
        principal = SimpleNamespace(tenant_id='t', user_id='u', role='member',
                                    platform_id='', subject='')
        # 聊天只产生答复，不建业务任务
        result = await hub.accept_session_message(
            'sess1', request_id='req-1', message='你好', principal=principal)
        self.assertEqual(result['kind'], 'chat')
        # 没有产生任何业务任务
        tasks = await self.store.session_tasks('sess1')
        self.assertEqual(len(tasks), 0)


if __name__ == '__main__':
    unittest.main()
