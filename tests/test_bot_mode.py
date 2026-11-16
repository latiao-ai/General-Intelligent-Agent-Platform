"""机器人（Bot Mode）的单元测试。

分三层：

1. **纯函数**：handle/`@` 解析、花名册、能力指纹、房间规划、窗口截断。不碰库，
   跑得最快，也最该覆盖——这几处错了不会报错，只会让机器人行为悄悄变形。
2. **仓储**：规范会话幂等、投递收据状态机、房间单步推进。需要 `PG_TEST_DSN`。
3. **接口**：建机器人、花名册、规范会话、发消息、跨用户隔离。

设计说明见 `docs/bot-mode.md`。
"""
import unittest
import uuid

from webpage_agent.bot import planner, prompts
from webpage_agent.bot.mailbox import (
    BotMailbox, delivery_id_for, reply_delivery_id)
from tests.exec_env import PG_DSN, new_store


def bot_row(bot_id='b1', handle='writer', name='写手', **extra):
    row = {'bot_id': bot_id, 'handle': handle, 'name': name, 'title': '',
           'description': '', 'soul': '', 'status': 'active', 'hidden': False,
           'skill_ids': [], 'connector_ids': [], 'model': ''}
    row.update(extra)
    return row


# ---------------------------------------------------------------- 纯函数


class HandleTests(unittest.TestCase):

    def test_normalize_ascii(self):
        self.assertEqual(prompts.normalize_handle('Code Review'), 'code-review')
        self.assertEqual(prompts.normalize_handle('  @Writer_01 '), 'writer_01')
        self.assertEqual(prompts.normalize_handle('a--b'), 'a-b')

    def test_normalize_chinese_falls_back_to_stable_hash(self):
        first = prompts.normalize_handle('代码审查员')
        self.assertTrue(first.startswith('bot-'))
        self.assertEqual(first, prompts.normalize_handle('代码审查员'))
        self.assertTrue(prompts.is_valid_handle(first))

    def test_normalize_empty(self):
        self.assertEqual(prompts.normalize_handle(''), '')
        # 只有标点：没有可读字符，退化成稳定哈希（仍合法、仍非空）
        self.assertTrue(prompts.is_valid_handle(prompts.normalize_handle('!!!')))

    def test_parse_mentions_dedupes_and_lowercases(self):
        self.assertEqual(
            prompts.parse_mentions('@Writer 看一下 @writer 和 @bob-x 以及 @'),
            ['writer', 'bob-x'])


class SilenceTests(unittest.TestCase):

    def test_tokens(self):
        for text in ('（无补充）', '无补充', '(无补充)', ' （无补充）。 ', 'PASS', ''):
            self.assertTrue(prompts.is_silence(text), text)

    def test_real_answer_is_not_silence(self):
        self.assertFalse(prompts.is_silence('我建议周五发布，因为…'))
        self.assertFalse(prompts.is_silence('无补充，但有个前提：先回归'))


class RosterTests(unittest.TestCase):

    def test_excludes_self_hidden_and_archived(self):
        roster = [
            bot_row('a', 'alpha', '甲'),
            bot_row('b', 'beta', '乙', hidden=True),
            bot_row('c', 'gamma', '丙', status='archived'),
            bot_row('d', 'delta', '丁'),
        ]
        text = prompts.render_roster(roster, self_bot_id='a')
        self.assertNotIn('@alpha', text)
        self.assertNotIn('@beta', text)
        self.assertNotIn('@gamma', text)
        self.assertIn('@delta', text)

    def test_line_is_bounded(self):
        long = bot_row('b', 'beta', '乙', title='长' * 80, description='也长' * 80)
        text = prompts.render_roster([long])
        self.assertLessEqual(len(text.splitlines()[0]), prompts.ROSTER_LINE_MAX)

    def test_caps_number_of_lines(self):
        roster = [bot_row(f'b{i}', f'h{i}', f'n{i}') for i in range(60)]
        text = prompts.render_roster(roster)
        self.assertEqual(len(text.splitlines()), prompts.ROSTER_MAX)


class FingerprintTests(unittest.TestCase):

    def test_changes_when_capability_changes(self):
        base = bot_row(soul='A', skill_ids=['s1'])
        roster = [base, bot_row('b2', 'other', '队友')]
        epoch = prompts.capability_fingerprint(base, roster)
        self.assertEqual(len(epoch), 12)
        self.assertNotEqual(epoch, prompts.capability_fingerprint(
            dict(base, soul='B'), roster))
        self.assertNotEqual(epoch, prompts.capability_fingerprint(
            dict(base, skill_ids=['s2']), roster))
        self.assertNotEqual(epoch, prompts.capability_fingerprint(
            dict(base, model='m2'), roster))
        self.assertNotEqual(epoch, prompts.capability_fingerprint(
            base, roster + [bot_row('b3', 'new', '新队友')]))

    def test_skill_order_does_not_matter(self):
        roster = [bot_row()]
        one = prompts.capability_fingerprint(bot_row(skill_ids=['a', 'b']), roster)
        two = prompts.capability_fingerprint(bot_row(skill_ids=['b', 'a']), roster)
        self.assertEqual(one, two)

    def test_capability_notice_only_on_change(self):
        self.assertEqual(prompts.capability_notice('abc', 'abc'), '')
        self.assertEqual(prompts.capability_notice('', 'abc'), '')
        self.assertIn('能力面已更新', prompts.capability_notice('abc', 'def'))


class PersonaTests(unittest.TestCase):

    def test_persona_has_identity_roster_and_epoch(self):
        me = bot_row('a', 'reviewer', '审查员', title='看代码',
                     description='只挑会出事的毛病', soul='你只报事故级问题')
        text = prompts.persona_block(me, roster=[me, bot_row('b', 'writer', '写手')],
                                     epoch='abc123abc123')
        self.assertIn('@reviewer', text)
        self.assertIn('你只报事故级问题', text)
        self.assertIn('@writer', text)
        self.assertIn('Capability epoch: abc123abc123', text)
        self.assertIn('message_bot', text)


class TranscriptTests(unittest.TestCase):

    def test_window_truncates_long_message(self):
        events = [
            {'kind': 'message.user', 'actor': 'user:u', 'payload': {'text': '议题'}},
            {'kind': 'message.member', 'actor': 'bot:b1',
             'payload': {'bot_id': 'b1', 'text': 'x' * 5000}},
            {'kind': 'message.member', 'actor': 'bot:b2',
             'payload': {'bot_id': 'b2', 'text': '最后一句'}},
        ]
        text = prompts.room_transcript(events, handles={'b1': 'writer', 'b2': 'ops'},
                                       limit_messages=3, limit_chars=3000)
        self.assertIn('最后一句', text)
        # 超长消息截断并标注，而不是把整段塞进上下文
        self.assertIn('已截断', text)
        self.assertLess(len(text), 3000)

    def test_window_drops_old_messages_by_char_budget(self):
        events = [
            {'kind': 'message.member', 'actor': 'bot:b1',
             'payload': {'bot_id': 'b1', 'text': 'y' * 900}},
            {'kind': 'message.member', 'actor': 'bot:b2',
             'payload': {'bot_id': 'b2', 'text': '最新一句'}},
        ]
        text = prompts.room_transcript(events, handles={}, limit_messages=10,
                                       limit_chars=500)
        self.assertIn('最新一句', text)
        # 被字符预算挡掉的那一条要如实写进省略提示（不是静默丢弃）
        self.assertIn('省略了更早的 1 条', text)

    def test_omitted_notice(self):
        events = [{'kind': 'message.user', 'actor': 'user:u',
                   'payload': {'text': f'第{i}条'}} for i in range(10)]
        text = prompts.room_transcript(events, handles={}, limit_messages=3)
        self.assertIn('省略了更早的 7 条', text)


class DeliveryIdTests(unittest.TestCase):

    def test_deterministic_and_scoped(self):
        one = delivery_id_for(turn_id='t1', from_bot='a', to_bot='b', body='hi')
        self.assertEqual(one, delivery_id_for(turn_id='t1', from_bot='a',
                                             to_bot='b', body='hi'))
        self.assertNotEqual(one, delivery_id_for(turn_id='t2', from_bot='a',
                                                to_bot='b', body='hi'))
        self.assertNotEqual(one, delivery_id_for(turn_id='t1', from_bot='a',
                                                to_bot='c', body='hi'))

    def test_reply_id_is_stable_and_distinct(self):
        self.assertEqual(reply_delivery_id('d1'), reply_delivery_id('d1'))
        self.assertNotEqual(reply_delivery_id('d1'), reply_delivery_id('d2'))


# ---------------------------------------------------------------- 房间规划


def room_row(max_rounds=3, max_messages=10, status='open'):
    return {'room_id': 'r1', 'status': status, 'max_rounds': max_rounds,
            'max_messages': max_messages}


MEMBERS = [bot_row('a', 'alpha', '甲'), bot_row('b', 'beta', '乙')]


def user_event(text='议题', epoch=1, mentions=None):
    return {'kind': 'message.user', 'actor': 'user:u',
            'payload': {'text': text, 'epoch': epoch, 'mentions': mentions or []}}


def member_event(bot_id, round_no=0, epoch=1, text='答', silent=False,
                 mentions=None):
    return {'kind': 'message.member', 'actor': f'bot:{bot_id}',
            'payload': {'bot_id': bot_id, 'round': round_no, 'epoch': epoch,
                        'text': text, 'silent': silent,
                        'mentions': mentions or []}}


class PlannerTests(unittest.TestCase):

    def test_opening_respects_mention(self):
        plan = planner.plan_next_task(room_row(), [user_event(mentions=['beta'])],
                                      MEMBERS)
        self.assertEqual(plan['action'], 'speak')
        self.assertEqual(plan['bot_id'], 'b')
        self.assertEqual(plan['reason'], 'mentioned')

    def test_opening_without_mention_is_first_member(self):
        plan = planner.plan_next_task(room_row(), [user_event()], MEMBERS)
        self.assertEqual(plan['bot_id'], 'a')
        self.assertEqual(plan['reason'], 'opening')

    def test_rotation_after_speaker(self):
        events = [user_event(), member_event('a')]
        plan = planner.plan_next_task(room_row(), events, MEMBERS)
        self.assertEqual(plan['bot_id'], 'b')
        self.assertEqual(plan['reason'], 'rotation')

    def test_next_round_after_all_spoke(self):
        events = [user_event(), member_event('a'), member_event('b')]
        plan = planner.plan_next_task(room_row(), events, MEMBERS)
        self.assertEqual(plan['action'], 'speak')
        self.assertEqual(plan['round'], 1)
        self.assertEqual(plan['position'], 1)

    def test_round_limit_settles(self):
        events = [user_event()]
        for round_no in range(2):
            events.append(member_event('a', round_no))
            events.append(member_event('b', round_no))
        plan = planner.plan_next_task(room_row(max_rounds=2), events, MEMBERS)
        self.assertEqual(plan['action'], 'settle')
        self.assertEqual(plan['reason'], 'round_limit')

    def test_message_limit_settles(self):
        events = [user_event()] + [member_event('a'), member_event('b'),
                                   member_event('a'), member_event('b')]
        plan = planner.plan_next_task(room_row(max_messages=5), events, MEMBERS)
        self.assertEqual(plan['action'], 'settle')
        self.assertEqual(plan['reason'], 'message_limit')

    def test_silent_round_settles(self):
        events = [user_event(), member_event('a', silent=True),
                  member_event('b', silent=True)]
        plan = planner.plan_next_task(room_row(), events, MEMBERS)
        self.assertEqual(plan['action'], 'settle')
        self.assertEqual(plan['reason'], 'silent_round')

    def test_failed_member_is_skipped_not_retried(self):
        # 开场就失败：不能再点 a 一次，应该轮到 b
        events = [user_event(), {'kind': 'turn.failed', 'actor': 'bot:a',
                                 'payload': {'bot_id': 'a', 'round': 0,
                                             'epoch': 1, 'silent': True}}]
        plan = planner.plan_next_task(room_row(), events, MEMBERS)
        self.assertEqual(plan['bot_id'], 'b')

    def test_all_members_failed_settles(self):
        events = [user_event()] + [
            {'kind': 'turn.failed', 'actor': f'bot:{bot_id}',
             'payload': {'bot_id': bot_id, 'round': 0, 'epoch': 1,
                         'silent': True}}
            for bot_id in ('a', 'b')]
        plan = planner.plan_next_task(room_row(), events, MEMBERS)
        self.assertEqual(plan['action'], 'settle')
        self.assertEqual(plan['reason'], 'all_failed')

    def test_new_user_message_opens_new_epoch(self):
        events = [user_event(), member_event('a'), member_event('b'),
                  user_event('追问', epoch=2)]
        plan = planner.plan_next_task(room_row(), events, MEMBERS)
        self.assertEqual(plan['epoch'], 2)
        self.assertEqual(plan['round'], 0)
        self.assertEqual(plan['position'], 1)

    def test_closed_room_does_not_plan(self):
        plan = planner.plan_next_task(room_row(status='closed'), [user_event()],
                                      MEMBERS)
        self.assertEqual(plan['action'], 'settle')

    def test_no_members_settles(self):
        plan = planner.plan_next_task(room_row(), [user_event()], [])
        self.assertEqual(plan['reason'], 'no_members')

    def test_task_ids_are_stable_and_unique(self):
        one = planner.task_id_for('r1', 1, 0, 1)
        self.assertEqual(one, planner.task_id_for('r1', 1, 0, 1))
        self.assertNotEqual(one, planner.task_id_for('r1', 2, 0, 1))
        self.assertNotEqual(one, planner.task_id_for('r1', 1, 1, 1))


# ---------------------------------------------------------------- 仓储


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class BotStoreTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.store = new_store()
        self.suffix = uuid.uuid4().hex[:8]
        self.tenant = f't_{self.suffix}'
        self.user = f'u_{self.suffix}'

    async def asyncTearDown(self):
        await self.store.close()

    async def _bot(self, bot_id='b1', handle='writer', **extra):
        # bot_id 是全局主键（生产里由接口生成随机 id），测试里带上用例后缀，
        # 否则同一个测试库连跑两次会撞主键。
        scoped = f'{self.suffix}-{bot_id}'
        return await self.store.create_bot_row(
            bot_id=scoped, tenant_id=self.tenant, owner_user_id=self.user,
            created_at='2026-01-01', updated_at='2026-01-01', handle=handle,
            name=extra.pop('name', handle), status='active', **extra)

    async def test_canonical_session_is_idempotent(self):
        b1 = f'{self.suffix}-b1'
        await self._bot()
        one = await self.store.ensure_bot_session(
            bot_id=b1, tenant_id=self.tenant, owner_user_id=self.user,
            session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
        two = await self.store.ensure_bot_session(
            bot_id=b1, tenant_id=self.tenant, owner_user_id=self.user,
            session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
        self.assertEqual(one['session_id'], two['session_id'])
        self.assertEqual(one['session_kind'], 'bot_chat')
        self.assertEqual(int(one['hidden']), 1)
        # 规范会话不进普通侧栏
        listed = await self.store.list_sessions(tenant_id=self.tenant,
                                               owner_user_id=self.user)
        self.assertNotIn(one['session_id'], [row['session_id'] for row in listed])

    async def test_roster_carries_preview_and_session(self):
        b1 = f'{self.suffix}-b1'
        await self._bot(handle='writer')
        session = await self.store.ensure_bot_session(
            bot_id=b1, tenant_id=self.tenant, owner_user_id=self.user,
            session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
        await self.store.append_message(session['session_id'], 'user', '你好',
                                        tenant_id=self.tenant,
                                        owner_user_id=self.user)
        rows = await self.store.list_bot_rows(tenant_id=self.tenant,
                                              owner_user_id=self.user)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['last_preview'], '你好')
        self.assertEqual(rows[0]['chat_session_id'], session['session_id'])
        self.assertEqual(rows[0]['message_count'], 1)

    async def test_roster_is_isolated_by_owner(self):
        await self._bot()
        other = await self.store.list_bot_rows(tenant_id=self.tenant,
                                                owner_user_id='someone-else')
        self.assertEqual(other, [])

    async def test_delivery_pump_settles_and_replies(self):
        b1 = f'{self.suffix}-b1'
        await self._bot('b1', 'writer', name='写手')
        await self._bot('b2', 'reviewer', name='审查员')
        mailbox = BotMailbox(self.store)
        sender = await self.store.bot_row(b1)
        receipt = await mailbox.deliver(from_bot=sender, to_handle='@reviewer',
                                        body='帮我看看这段', turn_id='t1', hop=1)
        self.assertEqual(receipt['status'], 'queued')
        # 同一轮重复发同一条：同一张收据，不重复排队
        again = await mailbox.deliver(from_bot=sender, to_handle='reviewer',
                                      body='帮我看看这段', turn_id='t1', hop=1)
        self.assertTrue(again.get('replayed'))
        self.assertEqual(again['delivery_id'], receipt['delivery_id'])

        stats = await mailbox.pump(limit=5)
        self.assertEqual(stats['claimed'], 1)
        row = await self.store.delivery(receipt['delivery_id'])
        self.assertEqual(row['status'], 'claimed')
        turn = await self.store.turn(row['turn_id'])
        self.assertIn('Message from', turn['message'])
        self.assertIn('帮我看看这段', turn['message'])

        await self.store.settle_turn(turn['turn_id'], status='succeeded',
                                     answer='看过了，没问题', lease_owner='',
                                     lease_epoch=0, execution_generation=0)
        stats = await mailbox.pump(limit=5)
        self.assertEqual(stats['settled'], 1)
        self.assertEqual(stats['replied'], 1)
        parent = await self.store.delivery(receipt['delivery_id'])
        self.assertEqual(parent['status'], 'settled')
        inbox = await self.store.list_deliveries(bot_id=b1,
                                                  tenant_id=self.tenant,
                                                  owner_user_id=self.user)
        self.assertEqual(inbox[0]['kind'], 'reply')
        self.assertEqual(inbox[0]['hop'], 2)
        self.assertEqual(inbox[0]['status'], 'claimed')

    async def test_delivery_hop_limit_refuses_without_turn(self):
        b1 = f'{self.suffix}-b1'
        await self._bot('b1', 'writer', name='写手')
        await self._bot('b2', 'reviewer', name='审查员')
        mailbox = BotMailbox(self.store, max_hops=2)
        sender = await self.store.bot_row(b1)
        receipt = await mailbox.deliver(from_bot=sender, to_handle='reviewer',
                                        body='第三跳', turn_id='t9', hop=3)
        await mailbox.pump(limit=5)
        row = await self.store.delivery(receipt['delivery_id'])
        self.assertEqual(row['status'], 'refused')
        self.assertEqual(row['reason'], 'hop_limit')

    async def test_unknown_target_is_refused_immediately(self):
        b1 = f'{self.suffix}-b1'
        await self._bot('b1', 'writer', name='写手')
        mailbox = BotMailbox(self.store)
        sender = await self.store.bot_row(b1)
        receipt = await mailbox.deliver(from_bot=sender, to_handle='nobody',
                                        body='喂', turn_id='t1')
        self.assertEqual(receipt['status'], 'refused')
        self.assertEqual(receipt['reason'], 'unknown_target')

    async def test_self_target_is_refused(self):
        b1 = f'{self.suffix}-b1'
        await self._bot('b1', 'writer', name='写手')
        mailbox = BotMailbox(self.store)
        receipt = await mailbox.deliver(from_bot=await self.store.bot_row(b1),
                                        to_handle='writer', body='喂',
                                        turn_id='t1')
        self.assertEqual(receipt['reason'], 'self_target')

    async def test_room_step_advances_one_speaker_at_a_time(self):
        b1, b2 = f'{self.suffix}-b1', f'{self.suffix}-b2'
        await self._bot('b1', 'alpha', name='甲')
        await self._bot('b2', 'beta', name='乙')
        room = await self.store.create_room_row(
            room_id='room_' + self.suffix, tenant_id=self.tenant,
            owner_user_id=self.user, name='评审', members=[b1, b2],
            topic='发布计划', max_rounds=1, max_messages=4)
        room_id = room['room_id']
        await self.store.append_room_event(room_id, event_id=f'{room_id}:topic',
                                           kind='message.user',
                                           actor=f'user:{self.user}',
                                           payload={'text': '发布计划',
                                                    'epoch': 1, 'mentions': []})
        from webpage_agent.bot.rooms import RoomDriver
        driver = RoomDriver(self.store, worker_id='test')
        first = await driver.step(room_id)
        self.assertEqual(first['action'], 'speak')
        self.assertEqual(first['bot_id'], b1)
        # 上一轮还没跑完：本拍只能等
        second = await driver.step(room_id)
        self.assertEqual(second['action'], 'waiting')

        await self.store.settle_turn(first['turn_id'], status='succeeded',
                                     answer='建议周五', lease_owner='',
                                     lease_epoch=0, execution_generation=0)
        third = await driver.step(room_id)
        self.assertEqual(third['action'], 'speak')
        self.assertEqual(third['bot_id'], b2)
        events = await self.store.room_events(room_id)
        published = [event for event in events if event['kind'] == 'message.member']
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]['payload']['text'], '建议周五')

        await self.store.settle_turn(third['turn_id'], status='succeeded',
                                     answer=prompts.SILENCE_TOKEN,
                                     lease_owner='', lease_epoch=0,
                                     execution_generation=0)
        fourth = await driver.step(room_id)
        # max_rounds=1：这一轮说完就收束（全员静默也收束）
        self.assertEqual(fourth['action'], 'settle')
        room_after = await self.store.room_row(room_id)
        self.assertEqual(room_after['status'], 'settled')

    async def test_room_failure_is_not_retried(self):
        b1, b2 = f'{self.suffix}-b1', f'{self.suffix}-b2'
        await self._bot('b1', 'alpha', name='甲')
        await self._bot('b2', 'beta', name='乙')
        room = await self.store.create_room_row(
            room_id='room_' + self.suffix, tenant_id=self.tenant,
            owner_user_id=self.user, name='评审', members=[b1, b2],
            topic='议题', max_rounds=2, max_messages=6)
        room_id = room['room_id']
        from webpage_agent.bot.rooms import RoomDriver
        driver = RoomDriver(self.store, worker_id='test')
        first = await driver.step(room_id)
        self.assertEqual(first['bot_id'], b1)
        await self.store.settle_turn(first['turn_id'], status='failed',
                                     error='boom', lease_owner='',
                                     lease_epoch=0, execution_generation=0)
        second = await driver.step(room_id)
        self.assertEqual(second['action'], 'speak')
        self.assertEqual(second['bot_id'], b2)      # 不是重试 b1


# ---------------------------------------------------------------- 接口


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class BotApiTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        import httpx
        import webpage_agent.api.routes as api_module
        from webpage_agent.api.api_hub import Hub

        self.store = new_store()
        self.api = api_module
        self.saved_state_store = getattr(api_module.app.state, 'store', None)
        self.saved_hub = api_module.hub
        api_module.app.state.store = self.store
        api_module.hub = Hub(self.store)
        suffix = uuid.uuid4().hex[:8]
        self.tenant = f't_{suffix}'
        self.user = f'u_{suffix}'
        self.headers = {'x-dsh-tenant': self.tenant, 'x-dsh-user': self.user}
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.api.app),
            base_url='http://test', headers=self.headers)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.api.hub.close()
        self.api.hub = self.saved_hub
        self.api.app.state.store = self.saved_state_store
        await self.store.close()

    async def _create(self, name='写手', handle='writer', **extra):
        res = await self.client.post('/api/bots', json={
            'name': name, 'handle': handle, **extra})
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    async def test_create_list_and_chat_idempotent(self):
        bot = await self._create(title='写文案', soul='你写短句。')
        self.assertTrue(bot['bot_id'].startswith('bot_'))
        self.assertEqual(bot['handle'], 'writer')

        roster = await self.client.get('/api/bots')
        self.assertEqual(roster.status_code, 200)
        data = roster.json()
        self.assertEqual(len(data['items']), 1)
        self.assertTrue(data['enabled'])

        one = await self.client.get(f"/api/bots/{bot['bot_id']}/chat")
        two = await self.client.get(f"/api/bots/{bot['bot_id']}/chat")
        self.assertEqual(one.json()['session_id'], two.json()['session_id'])
        self.assertEqual(one.json()['bot']['soul'], '你写短句。')

    async def test_handle_is_unique_and_normalized(self):
        await self._create(name='写手', handle='writer')
        res = await self.client.post('/api/bots',
                                     json={'name': '另一个', 'handle': 'Writer'})
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()['handle'], 'writer-2')

    async def test_patch_and_archive(self):
        bot = await self._create()
        res = await self.client.patch(f"/api/bots/{bot['bot_id']}",
                                      json={'title': '新职责', 'pinned': True})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['title'], '新职责')
        self.assertTrue(res.json()['pinned'])

        res = await self.client.delete(f"/api/bots/{bot['bot_id']}?archive=1")
        self.assertEqual(res.status_code, 200, res.text)
        roster = await self.client.get('/api/bots')
        self.assertEqual(roster.json()['items'], [])

    async def test_another_user_cannot_see_or_open_bot(self):
        bot = await self._create()
        other = {'x-dsh-tenant': self.tenant, 'x-dsh-user': 'someone-else'}
        roster = await self.client.get('/api/bots', headers=other)
        self.assertEqual(roster.json()['items'], [])
        opened = await self.client.get(f"/api/bots/{bot['bot_id']}/chat",
                                       headers=other)
        self.assertEqual(opened.status_code, 404)

    async def test_send_message_into_bot_chat(self):
        bot = await self._create()
        res = await self.client.post(f"/api/bots/{bot['bot_id']}/message",
                                     json={'message': '你好', 'request_id': 'r1'})
        self.assertEqual(res.status_code, 202, res.text)
        body = res.json()
        self.assertTrue(body['turn_id'])
        self.assertTrue(body['session_id'])
        # 同一 request_id 重放：返回原 turn，而不是再接纳一条
        again = await self.client.post(f"/api/bots/{bot['bot_id']}/message",
                                       json={'message': '你好', 'request_id': 'r1'})
        self.assertEqual(again.status_code, 202, again.text)
        self.assertEqual(again.json()['turn_id'], body['turn_id'])
        self.assertTrue(again.json()['replayed'])

    async def test_bot_chat_never_routes_to_task_control(self):
        """机器人会话里的"新建…"不能被判成任务控制，否则会回"能力未开放"。"""
        bot = await self._create()
        res = await self.client.post(f"/api/bots/{bot['bot_id']}/message",
                                     json={'message': '帮我新建一个 HTML 报告',
                                           'request_id': 'r-ctrl'})
        self.assertEqual(res.status_code, 202, res.text)
        self.assertEqual(res.json()['kind'], 'chat')
        turn = await self.store.turn(res.json()['turn_id'])
        self.assertEqual(turn['kind'], 'chat')

    async def test_room_needs_two_distinct_members(self):
        bot = await self._create(name='甲', handle='alpha')
        res = await self.client.post('/api/bot-rooms', json={
            'name': '评审', 'members': [bot['bot_id'], bot['bot_id']]})
        self.assertEqual(res.status_code, 400)

    async def test_room_lifecycle(self):
        one = await self._create(name='甲', handle='alpha')
        two = await self._create(name='乙', handle='beta')
        res = await self.client.post('/api/bot-rooms', json={
            'name': '评审', 'topic': '说下发布计划', 'members': [one['bot_id'],
                                                              two['bot_id']]})
        self.assertEqual(res.status_code, 201, res.text)
        room = res.json()
        detail = await self.client.get(f"/api/bot-rooms/{room['room_id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        payload = detail.json()
        self.assertEqual(len(payload['members']), 2)
        self.assertEqual(payload['plan']['action'], 'speak')
        self.assertEqual(payload['plan']['bot_id'], one['bot_id'])

        stepped = await self.client.post(
            f"/api/bot-rooms/{room['room_id']}/step")
        self.assertEqual(stepped.status_code, 200, stepped.text)
        self.assertEqual(stepped.json()['action'], 'speak')

        posted = await self.client.post(
            f"/api/bot-rooms/{room['room_id']}/messages",
            json={'message': '再补一句'})
        self.assertEqual(posted.status_code, 201, posted.text)


if __name__ == '__main__':
    unittest.main()
