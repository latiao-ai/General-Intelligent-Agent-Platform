"""消息网关的集成测试：入站队列 → 鉴权 → 假 Bridge → 出站队列 → 审计事件。

用真实 PostgreSQL（`PG_TEST_DSN`）跑队列与幂等，用 MockTransport 顶掉飞书 HTTP，
用假 Bridge 顶掉模型调用。**不碰网络、不花钱**，但走的是和 Worker 一样的代码路径。
"""
import asyncio
import hashlib
import json
import os
import unittest
from types import SimpleNamespace

import httpx

from tests.exec_env import PG_DSN, new_store, override_settings, reset_db

FEISHU_URL = 'https://open.feishu.cn'


class FakeBridge:
    def __init__(self, answer='pong'):
        self.answer = answer
        self.calls = []

    async def run(self, event, **kwargs):
        self.calls.append(event)
        return {'text': self.answer, 'session_id': 's', 'turn_id': 't',
                'status': 'ok'}


class GatewayHarness:
    def __init__(self, handler):
        self.sent = []
        inner = handler

        def capture(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/im/v1/messages'):
                self.sent.append(json.loads(request.content))
            return inner(request)

        self.transport = httpx.MockTransport(capture)


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith('/tenant_access_token/internal'):
        return httpx.Response(200, json={
            'code': 0, 'tenant_access_token': 't', 'expire': 7200})
    if request.url.path.endswith('/im/v1/messages'):
        return httpx.Response(200, json={
            'code': 0, 'data': {'message_id': 'om_reply'}})
    return httpx.Response(404)


def _set_env(app_id='cli_test', secret='sec_test'):
    os.environ.setdefault('FEISHU_APP_ID', app_id)
    os.environ.setdefault('FEISHU_APP_SECRET', secret)


def active_channel(gateway, *, channel='feishu', instance=''):
    """网关内部的渠道键 `tenant:owner:channel:instance`。

    把拼键只放这一处：键里加实例维度时只改这个函数，不用逐条改断言。
    """
    return gateway._active[f't1:u1:{channel}:{instance}']


def _event(message_id='om_1', text='ping', user='ou_1'):
    from webpage_agent.messaging.models import Inbound
    return Inbound(platform='feishu', tenant='t1', chat_id='oc_1',
                   chat_type='dm', user_id=user, user_name='张三',
                   text=text, message_id=message_id)


class GatewayTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        if not PG_DSN:
            raise unittest.SkipTest('需要 PG_TEST_DSN')
        _set_env()
        await reset_db()
        self.store = new_store()
        await self.store.ready()
        self.hub = SimpleNamespace(store=self.store)
        self.harness = GatewayHarness(handler)
        self.gateway = None
        self.stack = override_settings(
            messaging_enabled=True,
            messaging_public_base_url='https://im.example.com',
            messaging_tick_seconds=1.0,
            messaging_reply_timeout_seconds=30.0,
            messaging_inbox_max_attempts=2,
            messaging_outbox_max_attempts=2,
            messaging_alive_window_seconds=30.0,
            messaging_concurrency=3)
        self.stack.__enter__()

    async def asyncTearDown(self):
        self.stack.__exit__(None, None, None)
        if self.gateway is not None:
            await self.gateway.stop()
        await self.store.close()

    async def _gateway(self, *, dm_policy='open'):
        from webpage_agent.messaging.gateway import MessagingGateway
        await self.store.im_upsert_channel(
            'feishu', tenant_id='t1', owner_user_id='u1', enabled=True,
            dm_policy=dm_policy,
            config={'home_chat_id': 'oc_1', 'display_name': '测试飞书'})
        gateway = MessagingGateway(self.hub, worker_id='w1',
                                   http_transport=self.harness.transport)
        self.assertTrue(await gateway.start())
        self.gateway = gateway
        gateway._bridge = FakeBridge()
        return gateway

    async def _settle(self, gateway):
        """等一拍派生的入站任务全部完成。"""
        while gateway._inflight:
            await asyncio.gather(*list(gateway._inflight),
                                 return_exceptions=True)

    async def test_inbound_reply_and_audit(self):
        gateway = await self._gateway(dm_policy='open')
        active = active_channel(gateway)
        await gateway._on_inbound(active.config, _event())
        await gateway.tick()
        await self._settle(gateway)
        self.assertEqual(len(self.harness.sent), 1)
        self.assertEqual(
            json.loads(self.harness.sent[0]['content'])['text'], 'pong')
        self.assertEqual(self.harness.sent[0]['receive_id'], 'oc_1')
        events = await self.store.im_events_list(
            tenant_id='t1', owner_user_id='u1', channel_id='feishu')
        statuses = {row['status'] for row in events}
        self.assertIn('answered', statuses)
        self.assertIn('sent', statuses)

    async def test_same_message_twice_only_answered_once(self):
        gateway = await self._gateway(dm_policy='open')
        active = active_channel(gateway)
        await gateway._on_inbound(active.config, _event())
        await gateway._on_inbound(active.config, _event())
        await gateway.tick()
        await self._settle(gateway)
        await gateway.tick()
        await self._settle(gateway)
        self.assertEqual(len(gateway._bridge.calls), 1)
        self.assertEqual(len(self.harness.sent), 1)

    async def test_pairing_code_then_approve(self):
        import re
        gateway = await self._gateway(dm_policy='pair')
        active = active_channel(gateway)
        await gateway._on_inbound(active.config, _event(user='ou_new'))
        await gateway.tick()
        await self._settle(gateway)
        self.assertEqual(len(gateway._bridge.calls), 0)
        self.assertEqual(len(self.harness.sent), 1)
        text = json.loads(self.harness.sent[0]['content'])['text']
        matched = re.search(r'配对码：(\d{8})', text)
        self.assertIsNotNone(matched, text)
        code = matched.group(1)
        approved = await self.store.im_pairing_approve(
            tenant_id='t1', owner_user_id='u1',
            code_hash=hashlib.sha256(code.encode()).hexdigest())
        self.assertIsNotNone(approved)
        await gateway._on_inbound(active.config, _event(message_id='om_2',
                                                        user='ou_new'))
        await gateway.tick()
        await self._settle(gateway)
        self.assertEqual(len(gateway._bridge.calls), 1)

    async def test_help_command_does_not_reach_agent(self):
        gateway = await self._gateway(dm_policy='open')
        active = active_channel(gateway)
        await gateway._on_inbound(active.config, _event(text='/help'))
        await gateway.tick()
        await self._settle(gateway)
        self.assertEqual(len(gateway._bridge.calls), 0)
        text = json.loads(self.harness.sent[0]['content'])['text']
        self.assertIn('/new', text)

    async def test_new_command_starts_fresh_session(self):
        gateway = await self._gateway(dm_policy='open')
        active = active_channel(gateway)
        await gateway._on_inbound(active.config, _event(text='/new'))
        await gateway.tick()
        await self._settle(gateway)
        row = await self.store.im_session(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1')
        self.assertEqual(int(row['generation']), 2)
        self.assertGreater(len(row['session_id']), 10)

    # ---------- 会话级身份：/who /bind /unbind ----------

    async def _bot(self, bot_id='bot_xzs', name='X助手', handle='xzs'):
        return await self.store.create_bot_row(
            bot_id=bot_id, tenant_id='t1', owner_user_id='u1',
            created_at='2026-01-01', updated_at='2026-01-01',
            handle=handle, name=name, status='active')

    async def _say(self, gateway, text, message_id='om_1'):
        active = active_channel(gateway)
        await gateway._on_inbound(active.config,
                                  _event(message_id=message_id, text=text))
        await gateway.tick()
        await self._settle(gateway)
        return json.loads(self.harness.sent[-1]['content'])['text']

    async def _mapping(self):
        return await self.store.im_session(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1')

    async def test_who_says_generic_assistant_by_default(self):
        gateway = await self._gateway(dm_policy='open')
        text = await self._say(gateway, '/who')
        self.assertIn('通用助手', text)
        self.assertIn('跟随渠道默认', text)
        self.assertEqual(len(gateway._bridge.calls), 0)

    async def test_bind_creates_mapping_and_switches_identity(self):
        gateway = await self._gateway(dm_policy='open')
        bot = await self._bot()
        text = await self._say(gateway, '/bind X助手')
        self.assertIn('X助手', text)
        row = await self._mapping()
        self.assertEqual(row['bot_id'], bot['bot_id'])
        self.assertGreater(len(row['session_id']), 10)
        # 身份变了，下一条消息交给 Agent；先确认 /who 已经按新身份答
        who = await self._say(gateway, '/who', message_id='om_2')
        self.assertIn('X助手', who)
        self.assertIn('单独指定', who)

    async def test_bind_on_existing_chat_keeps_session_and_writes_override(self):
        gateway = await self._gateway(dm_policy='open')
        bot = await self._bot()
        # 已有映射（真实 bridge 会建；这里用 FakeBridge，直接造一条）
        await self.store.im_create_session(
            'im-existing', tenant_id='t1', owner_user_id='u1', bot_id='',
            title='飞书 · 张三')
        await self.store.im_session_bind(
            tenant_id='t1', owner_user_id='u1', platform='feishu',
            chat_id='oc_1', chat_type='dm', session_id='im-existing',
            generation=1)
        await self._say(gateway, '/bind X助手')
        after = await self._mapping()
        # 改身份不改映射：换会话是下一条消息进来时 bridge 的事
        self.assertEqual(after['session_id'], 'im-existing')
        self.assertEqual(after['bot_id'], bot['bot_id'])

    async def test_bind_menu_lists_bots_and_marks_current(self):
        gateway = await self._gateway(dm_policy='open')
        await self._bot()
        await self._bot(bot_id='bot_kefu', name='客服Bot', handle='kefu')
        menu = await self._say(gateway, '/bind')
        self.assertIn('X助手', menu)
        self.assertIn('客服Bot', menu)
        await self._say(gateway, '/bind X助手', message_id='om_2')
        again = await self._say(gateway, '/bind', message_id='om_3')
        self.assertIn('（当前）', again)

    async def test_bind_unknown_bot_does_not_change_identity(self):
        gateway = await self._gateway(dm_policy='open')
        text = await self._say(gateway, '/bind 张三的助手')
        self.assertIn('没找到机器人', text)
        self.assertIsNone(await self._mapping())

    async def test_unbind_restores_channel_default(self):
        gateway = await self._gateway(dm_policy='open')
        await self._bot()
        await self._say(gateway, '/bind X助手')
        text = await self._say(gateway, '/unbind', message_id='om_2')
        self.assertIn('恢复跟随渠道默认', text)
        self.assertEqual((await self._mapping())['bot_id'], '')

    async def test_new_keeps_session_override(self):
        gateway = await self._gateway(dm_policy='open')
        bot = await self._bot()
        await self._say(gateway, '/bind X助手')
        await self._say(gateway, '/new', message_id='om_2')
        row = await self._mapping()
        self.assertEqual(row['bot_id'], bot['bot_id'])
        self.assertEqual(int(row['generation']), 2)

    async def test_command_help_mentions_bind(self):
        gateway = await self._gateway(dm_policy='open')
        text = await self._say(gateway, '/help')
        self.assertIn('/bind', text)
        self.assertIn('/who', text)

    async def test_disabled_channel_is_not_started(self):
        from webpage_agent.messaging.gateway import MessagingGateway
        await self.store.im_upsert_channel(
            'feishu', tenant_id='t1', owner_user_id='u1', enabled=False)
        gateway = MessagingGateway(self.hub, worker_id='w2',
                                   http_transport=self.harness.transport)
        self.assertTrue(await gateway.start())
        self.assertEqual(gateway._active, {})
        await gateway.stop()

    async def test_not_configured_channel_is_skipped(self):
        from webpage_agent.messaging.gateway import MessagingGateway
        old = os.environ.get('FEISHU_APP_ID')
        os.environ['FEISHU_APP_ID'] = ''
        try:
            await self.store.im_upsert_channel(
                'feishu', tenant_id='t1', owner_user_id='u1', enabled=True)
            gateway = MessagingGateway(self.hub, worker_id='w3',
                                       http_transport=self.harness.transport)
            self.assertTrue(await gateway.start())
            self.assertEqual(gateway._active, {})
            await gateway.stop()
        finally:
            if old is None:
                os.environ.pop('FEISHU_APP_ID', None)
            else:
                os.environ['FEISHU_APP_ID'] = old
