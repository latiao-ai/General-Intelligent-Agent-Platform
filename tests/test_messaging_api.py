"""消息平台控制面接口测试：清单、配置、回调、配对审批、页面。"""
import hashlib
import json
import os
import unittest
import uuid

import httpx

from tests.exec_env import PG_DSN, new_store, reset_db

PAGE_MARKERS = ('消息平台', '飞书', '企业微信', '个人微信')


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class MessagingApiTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        import webpage_agent.api.routes as api_module
        from webpage_agent.api.api_hub import Hub

        await reset_db()
        self.store = new_store()
        await self.store.ready()
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
        os.environ.setdefault('FEISHU_APP_ID', 'cli_test')
        os.environ.setdefault('FEISHU_APP_SECRET', 'sec_test')
        os.environ['FEISHU_VERIFICATION_TOKEN'] = 'vt-test'
        os.environ.pop('FEISHU_ENCRYPT_KEY', None)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.api.hub.close()
        self.api.hub = self.saved_hub
        self.api.app.state.store = self.saved_state_store
        await self.store.close()
        os.environ.pop('FEISHU_VERIFICATION_TOKEN', None)

    async def test_list_has_three_channels(self):
        res = await self.client.get('/api/messaging/channels')
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual([item['id'] for item in data['channels']],
                         ['feishu', 'wecom', 'weixin'])
        self.assertIn('gateway_alive', data)
        for item in data['channels']:
            self.assertFalse(item['enabled'])
            self.assertIn(item['state'], ('disabled', 'not_configured'))
            self.assertTrue(item['env'])
            for field in item['env']:
                if field['secret']:
                    self.assertEqual(field['preview'], '')
                    self.assertEqual(field['masked'], '')

    async def test_page_renders(self):
        res = await self.client.get('/api/ui/messaging')
        self.assertEqual(res.status_code, 200)
        for marker in PAGE_MARKERS:
            self.assertIn(marker, res.text)

    async def test_update_and_get_channel(self):
        res = await self.client.put('/api/messaging/channels/feishu', json={
            'enabled': True, 'dm_policy': 'allowlist',
            'group_policy': 'mention', 'allowed_users': ['ou_1', 'ou_2'],
            'options': {'home_chat_id': 'oc_test', 'display_name': '测试'}})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body['enabled'])
        self.assertEqual(body['dm_policy'], 'allowlist')
        self.assertEqual(body['allowed_users'], ['ou_1', 'ou_2'])
        self.assertEqual(body['state'], 'not_configured')  # 缺公网地址

        one = await self.client.get('/api/messaging/channels/feishu')
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()['options'][0]['value'], 'oc_test')
        self.assertTrue(one.json()['configured'] is False)
        self.assertTrue(any('公网' in item for item in one.json()['missing']))

    async def test_reload_marks_config_for_rebuild(self):
        await self.client.put('/api/messaging/channels/feishu',
                              json={'enabled': True})
        res = await self.client.post('/api/messaging/channels/feishu/reload')
        self.assertEqual(res.status_code, 200, res.text)
        row = await self.store.im_channel('feishu', tenant_id=self.tenant,
                                          owner_user_id=self.user)
        self.assertTrue(row['config'].get('_reload'))

    async def test_reject_bad_policy_and_option(self):
        bad_policy = await self.client.put(
            '/api/messaging/channels/feishu', json={'dm_policy': 'whatever'})
        self.assertEqual(bad_policy.status_code, 400)
        bad_option = await self.client.put(
            '/api/messaging/channels/feishu',
            json={'options': {'unknown_key': 'x'}})
        self.assertEqual(bad_option.status_code, 400)

    async def test_other_owner_cannot_take_same_channel(self):
        res = await self.client.put('/api/messaging/channels/feishu',
                                    json={'enabled': True})
        self.assertEqual(res.status_code, 200, res.text)
        other = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.api.app),
            base_url='http://test',
            headers={'x-dsh-tenant': self.tenant, 'x-dsh-user': 'other-user'})
        try:
            conflict = await other.put('/api/messaging/channels/feishu',
                                       json={'enabled': True})
            self.assertEqual(conflict.status_code, 409, conflict.text)
        finally:
            await other.aclose()

    async def test_feishu_webhook_url_verification(self):
        await self.client.put('/api/messaging/channels/feishu',
                              json={'enabled': True})
        raw = json.dumps({'type': 'url_verification', 'challenge': 'c-1',
                          'token': 'vt-test'})
        res = await self.client.post('/api/messaging/webhook/feishu',
                                     content=raw.encode())
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['challenge'], 'c-1')

    async def test_feishu_webhook_message_lands_in_inbox(self):
        await self.client.put('/api/messaging/channels/feishu',
                              json={'enabled': True})
        payload = {
            'schema': '2.0',
            'header': {'event_type': 'im.message.receive_v1',
                       'token': 'vt-test'},
            'event': {
                'sender': {'sender_type': 'user',
                           'sender_id': {'open_id': 'ou_in'}},
                'message': {'message_id': 'om_in_1', 'chat_id': 'oc_in',
                            'chat_type': 'p2p', 'message_type': 'text',
                            'content': json.dumps({'text': '你好'})},
            },
        }
        res = await self.client.post('/api/messaging/webhook/feishu',
                                     content=json.dumps(payload).encode())
        self.assertEqual(res.status_code, 200, res.text)
        backlog = await self.store.im_inbox_backlog(tenant_id=self.tenant,
                                                    owner_user_id=self.user)
        self.assertEqual(backlog, 1)
        # 同一 message_id 重投：只落一行
        res = await self.client.post('/api/messaging/webhook/feishu',
                                     content=json.dumps(payload).encode())
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(await self.store.im_inbox_backlog(
            tenant_id=self.tenant, owner_user_id=self.user), 1)

    async def test_feishu_webhook_bad_token_rejected(self):
        await self.client.put('/api/messaging/channels/feishu',
                              json={'enabled': True})
        raw = json.dumps({'type': 'url_verification', 'challenge': 'c',
                          'token': 'wrong'})
        res = await self.client.post('/api/messaging/webhook/feishu',
                                     content=raw.encode())
        self.assertEqual(res.status_code, 403)

    async def test_pairing_approve_by_code(self):
        await self.client.put('/api/messaging/channels/feishu',
                              json={'enabled': True, 'dm_policy': 'pair'})
        code = '12345678'
        await self.store.im_pairing_issue(
            tenant_id=self.tenant, owner_user_id=self.user, platform='feishu',
            user_id='ou_x', chat_id='oc_x',
            code_hash=hashlib.sha256(code.encode()).hexdigest())
        res = await self.client.post('/api/messaging/pairing/approve',
                                     json={'code': code})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['user_id'], 'ou_x')
        listing = await self.client.get('/api/messaging/pairing')
        approved = listing.json()['approved']
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]['platform'], 'feishu')
        wrong = await self.client.post('/api/messaging/pairing/approve',
                                       json={'code': '00000000'})
        self.assertEqual(wrong.status_code, 404)

    async def test_weixin_login_status_idle(self):
        res = await self.client.get('/api/messaging/channels/weixin/login/status')
        self.assertEqual(res.status_code, 200, res.text)
        self.assertFalse(res.json()['logged_in'])
        self.assertEqual(res.json()['status'], 'idle')

    async def test_events_endpoint_empty(self):
        res = await self.client.get('/api/messaging/events?limit=10')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['events'], [])
