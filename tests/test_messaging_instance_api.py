"""机器人实例（多应用）的控制面接口测试。

这一层测的是「界面能不能分清自己在配哪个机器人」：
实例的增删、实例之间的配置隔离、非默认实例的凭据不回落到基础键、
个人微信不进多实例、以及事件流不串台。
"""
import os
import unittest
import uuid

import httpx

from tests.exec_env import PG_DSN, new_store, reset_db


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class MessagingInstanceApiTests(unittest.IsolatedAsyncioTestCase):

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
        self._env_saved = {
            key: os.environ.get(key) for key in (
                'FEISHU_APP_ID', 'FEISHU_APP_SECRET',
                'WECOM_CORP_ID', 'WECOM_AGENT_ID', 'WECOM_SECRET',
                'WECOM_TOKEN', 'WECOM_AES_KEY')}
        for key in self._env_saved:
            os.environ.pop(key, None)
        os.environ['FEISHU_APP_ID'] = 'cli_default'
        os.environ['FEISHU_APP_SECRET'] = 'sec_default'
        os.environ.pop('FEISHU_ENCRYPT_KEY', None)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.api.hub.close()
        self.api.hub = self.saved_hub
        self.api.app.state.store = self.saved_state_store
        await self.store.close()
        for key, value in self._env_saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---------- 夹具 ----------

    async def _bot(self, bot_id='xzs', name='X助手', handle='xzs'):
        scoped = f'{self.suffix_of(bot_id)}'
        return await self.store.create_bot_row(
            bot_id=scoped, tenant_id=self.tenant, owner_user_id=self.user,
            created_at='2026-01-01', updated_at='2026-01-01',
            handle=handle, name=name, status='active')

    def suffix_of(self, bot_id: str) -> str:
        return f'{self.instance_seed}-{bot_id}'

    @property
    def instance_seed(self) -> str:
        return self.tenant.removeprefix('t_')

    async def _create(self, bot_id='xzs', name='X助手'):
        bot = await self._bot(bot_id=bot_id, name=name)
        res = await self.client.post('/api/messaging/instances',
                                     json={'bot_id': bot['bot_id']})
        self.assertEqual(res.status_code, 200, res.text)
        return bot, res.json()

    # ---------- 列表 ----------

    async def test_starts_with_only_default_instance(self):
        res = await self.client.get('/api/messaging/instances')
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(len(data['instances']), 1)
        only = data['instances'][0]
        self.assertTrue(only['builtin'])
        self.assertEqual(only['instance_id'], '')
        self.assertEqual(only['name'], '默认')
        # 默认实例必须有个人微信：它是唯一能挂这份登录态的地方
        self.assertEqual([item['id'] for item in only['channels']],
                         ['feishu', 'wecom', 'weixin'])
        self.assertEqual(only['state'], 'disabled')

    async def test_create_instance_exposes_channels_and_env_names(self):
        bot, body = await self._create(name='X助手')
        self.assertEqual(body['channels'], ['feishu', 'wecom'])
        self.assertFalse(body['instance']['bot_missing'])
        self.assertEqual(body['instance']['name'], 'X助手')

        # 建完就得知道要填哪些变量名：照着文档填基础键，这个实例永远读不到
        names = {item['env'] for item in body['env_required']}
        suffix = body['instance']['env_suffix']
        self.assertIn(f'FEISHU_APP_ID__{suffix}', names)
        self.assertIn(f'FEISHU_APP_SECRET__{suffix}', names)

        listed = (await self.client.get('/api/messaging/instances')).json()
        self.assertEqual([item['instance_id'] for item in listed['instances']],
                         ['', bot['bot_id']])

    async def test_instance_list_does_not_include_weixin(self):
        _, body = await self._create()
        ids = [item['id'] for item in body['instance']['channels']]
        self.assertNotIn('weixin', ids)

    async def test_duplicate_instance_is_rejected(self):
        bot, _ = await self._create()
        res = await self.client.post('/api/messaging/instances',
                                     json={'bot_id': bot['bot_id']})
        self.assertEqual(res.status_code, 409, res.text)

    async def test_unknown_bot_cannot_become_instance(self):
        res = await self.client.post('/api/messaging/instances',
                                     json={'bot_id': 'not-a-bot'})
        self.assertEqual(res.status_code, 404, res.text)

    # ---------- 隔离 ----------

    async def test_instance_config_is_separate_from_default(self):
        bot, _ = await self._create()
        inst = bot['bot_id']
        res = await self.client.put(
            '/api/messaging/channels/feishu',
            params={'instance': inst},
            json={'enabled': True,
                  'options': {'home_chat_id': 'oc_instance'}})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()['enabled'])
        self.assertEqual(res.json()['instance_id'], inst)

        default = (await self.client.get('/api/messaging/channels')).json()
        feishu = next(item for item in default['channels']
                      if item['id'] == 'feishu')
        self.assertFalse(feishu['enabled'], '默认实例不该被实例的开关带起来')
        self.assertEqual(feishu['options'][0]['value'], '')

        # 反向：默认实例保存不影响实例
        await self.client.put('/api/messaging/channels/feishu',
                              json={'options': {'home_chat_id': 'oc_default'}})
        inst_view = (await self.client.get(
            '/api/messaging/channels/feishu',
            params={'instance': inst})).json()
        self.assertEqual(inst_view['options'][0]['value'], 'oc_instance')

    async def test_instance_credentials_do_not_fall_back(self):
        bot, _ = await self._create()
        inst = bot['bot_id']
        view = (await self.client.get(
            '/api/messaging/channels/feishu',
            params={'instance': inst})).json()
        # 只配了基础键：这个实例必须是「缺 App ID」，不能悄悄共用默认应用
        self.assertFalse(view['configured'])
        self.assertIn('App ID', view['missing'])
        field = next(item for item in view['env'] if item['key'] == 'app_id')
        self.assertFalse(field['is_set'])
        self.assertTrue(field['env_name'].endswith('__' + view['instance_id']
                                                  .replace('-', '_').upper()))

        os.environ['FEISHU_APP_ID__' + view['instance_id'].replace('-', '_').upper()] = 'cli_xzs'
        os.environ['FEISHU_APP_SECRET__' + view['instance_id'].replace('-', '_').upper()] = 'sec_xzs'
        try:
            again = (await self.client.get(
                '/api/messaging/channels/feishu',
                params={'instance': inst})).json()
            field = next(item for item in again['env'] if item['key'] == 'app_id')
            self.assertTrue(field['is_set'])
            self.assertNotIn('App ID', again['missing'])
        finally:
            for key in list(os.environ):
                if '__' in key and key.startswith('FEISHU_'):
                    os.environ.pop(key, None)

    async def test_instance_bot_identity_is_fixed(self):
        bot, _ = await self._create()
        inst = bot['bot_id']
        other = f'{self.instance_seed}-other'
        await self.store.create_bot_row(
            bot_id=other, tenant_id=self.tenant, owner_user_id=self.user,
            created_at='2026-01-01', updated_at='2026-01-01',
            handle='other', name='别的机器人', status='active')
        res = await self.client.put(
            '/api/messaging/channels/feishu', params={'instance': inst},
            json={'bot_id': other})
        self.assertEqual(res.status_code, 400, res.text)

        ok = await self.client.put(
            '/api/messaging/channels/feishu', params={'instance': inst},
            json={'enabled': False})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()['bot_id'], inst)

    async def test_weixin_is_not_multi_instance(self):
        bot, _ = await self._create()
        res = await self.client.get('/api/messaging/channels/weixin',
                                    params={'instance': bot['bot_id']})
        self.assertEqual(res.status_code, 404, res.text)
        put = await self.client.put('/api/messaging/channels/weixin',
                                    params={'instance': bot['bot_id']},
                                    json={'enabled': True})
        self.assertEqual(put.status_code, 400, put.text)

    async def test_writing_unknown_instance_is_refused(self):
        res = await self.client.put('/api/messaging/channels/feishu',
                                    params={'instance': 'ghost-bot'},
                                    json={'enabled': True})
        self.assertEqual(res.status_code, 404, res.text)
        # 不该留下半行配置：它会以「用 id 兜底的名字」出现在实例列表里
        rows = await self.store.im_list_channels(
            tenant_id=self.tenant, owner_user_id=self.user)
        self.assertEqual([row['instance_id'] for row in rows], [])

    # ---------- 事件 ----------

    async def test_events_are_per_instance(self):
        bot, _ = await self._create()
        inst = bot['bot_id']
        await self.store.im_event_add(
            tenant_id=self.tenant, owner_user_id=self.user, channel_id='feishu',
            direction='in', chat_id_hash='h1', message_id='m-default',
            status='ok')
        await self.store.im_event_add(
            tenant_id=self.tenant, owner_user_id=self.user, channel_id='feishu',
            direction='in', chat_id_hash='h2', message_id='m-instance',
            status='ok', instance_id=inst)

        view = (await self.client.get(
            '/api/messaging/channels/feishu',
            params={'instance': inst})).json()
        self.assertEqual([item['message_id'] for item in view['events']],
                         ['m-instance'])

        default = (await self.client.get(
            '/api/messaging/channels/feishu')).json()
        self.assertEqual([item['message_id'] for item in default['events']],
                         ['m-default'])

    # ---------- 删除 ----------

    async def test_delete_instance(self):
        bot, _ = await self._create()
        inst = bot['bot_id']
        await self.client.put('/api/messaging/channels/feishu',
                              params={'instance': inst}, json={'enabled': True})

        res = await self.client.delete(f'/api/messaging/instances/{inst}')
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['removed'], 2)
        rows = await self.store.im_list_channels(
            tenant_id=self.tenant, owner_user_id=self.user)
        self.assertEqual(rows, [])
        self.assertIsNone(await self.store.im_runtime(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            instance_id=inst))

        again = await self.client.delete(f'/api/messaging/instances/{inst}')
        self.assertEqual(again.status_code, 404, again.text)

    async def test_default_instance_cannot_be_deleted(self):
        """默认实例是存量那一份：删它等于把渠道从部署里摘掉，那是「停用」干的活。"""
        with self.assertRaises(ValueError):
            await self.store.im_delete_instance(
                '', tenant_id=self.tenant, owner_user_id=self.user)

    async def test_deleted_bot_leaves_visible_instance(self):
        bot, _ = await self._create()
        inst = bot['bot_id']
        await self.store.delete_bot_row(
            inst, tenant_id=self.tenant, owner_user_id=self.user)
        listed = (await self.client.get('/api/messaging/instances')).json()
        target = next(item for item in listed['instances']
                      if item['instance_id'] == inst)
        # 机器人没了，配置还在：要看得见、能收拾，不能假装它没事
        self.assertTrue(target['bot_missing'])
        self.assertEqual(target['name'], inst)

    # ---------- 会话身份 ----------

    async def _session(self, chat_id='oc_1', *, bot_id='', instance=''):
        """造一条「这个群 ↔ 某条 Agent 会话」的映射。"""
        await self.store.im_create_session(
            f'im-{chat_id}', tenant_id=self.tenant, owner_user_id=self.user,
            bot_id=bot_id, title='飞书 · 张三')
        return await self.store.im_session_bind(
            tenant_id=self.tenant, owner_user_id=self.user, platform='feishu',
            chat_id=chat_id, chat_type='dm', session_id=f'im-{chat_id}',
            generation=1, bot_id=bot_id, instance_id=instance)

    async def _channel_bot(self, bot_id, *, instance=''):
        return await self.store.im_upsert_channel(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            instance_id=instance, bot_id=bot_id)

    async def test_sessions_follow_channel_bot_by_default(self):
        bot, _ = await self._create(name='X助手')
        await self._channel_bot(bot['bot_id'])
        await self._session()
        data = (await self.client.get(
            '/api/messaging/sessions?channel_id=feishu')).json()
        self.assertEqual(len(data['sessions']), 1)
        item = data['sessions'][0]
        self.assertEqual(item['bot_source'], 'channel')
        self.assertEqual(item['effective_bot_id'], bot['bot_id'])
        self.assertEqual(item['effective_bot_name'], 'X助手')
        self.assertEqual(item['bot_id'], '')

    async def test_session_override_wins_in_list(self):
        bot, _ = await self._create(name='X助手')
        other = await self._bot(bot_id='kefu', name='客服Bot', handle='kefu')
        await self._channel_bot(bot['bot_id'])
        await self._session(bot_id=other['bot_id'])
        item = (await self.client.get(
            '/api/messaging/sessions')).json()['sessions'][0]
        self.assertEqual(item['bot_source'], 'session')
        self.assertEqual(item['effective_bot_id'], other['bot_id'])
        self.assertEqual(item['effective_bot_name'], '客服Bot')

    async def test_bind_and_unbind_via_api(self):
        bot, _ = await self._create(name='X助手')
        await self._session()
        res = await self.client.put('/api/messaging/sessions', json={
            'channel_id': 'feishu', 'chat_id': 'oc_1',
            'bot_id': bot['bot_id']})
        self.assertEqual(res.status_code, 200, res.text)
        item = (await self.client.get(
            '/api/messaging/sessions')).json()['sessions'][0]
        self.assertEqual(item['effective_bot_id'], bot['bot_id'])
        res = await self.client.put('/api/messaging/sessions', json={
            'channel_id': 'feishu', 'chat_id': 'oc_1', 'bot_id': ''})
        self.assertEqual(res.status_code, 200, res.text)
        item = (await self.client.get(
            '/api/messaging/sessions')).json()['sessions'][0]
        self.assertEqual(item['bot_source'], 'channel')
        self.assertEqual(item['bot_id'], '')

    async def test_bind_unknown_bot_or_chat_is_rejected(self):
        res = await self.client.put('/api/messaging/sessions', json={
            'channel_id': 'feishu', 'chat_id': 'oc_1', 'bot_id': 'nope'})
        self.assertEqual(res.status_code, 404, res.text)
        res = await self.client.put('/api/messaging/sessions', json={
            'channel_id': 'feishu', 'chat_id': 'oc_missing', 'bot_id': ''})
        self.assertEqual(res.status_code, 404, res.text)

    async def test_sessions_are_scoped_by_instance(self):
        bot, _ = await self._create(name='X助手')
        await self._session(chat_id='oc_default')
        await self._session(chat_id='oc_xzs', instance=bot['bot_id'])
        default = (await self.client.get(
            '/api/messaging/sessions?channel_id=feishu&instance=')).json()
        scoped = (await self.client.get(
            f'/api/messaging/sessions?channel_id=feishu'
            f'&instance={bot["bot_id"]}')).json()
        self.assertEqual([item['chat_id'] for item in default['sessions']],
                         ['oc_default'])
        self.assertEqual([item['chat_id'] for item in scoped['sessions']],
                         ['oc_xzs'])


if __name__ == '__main__':
    unittest.main()
