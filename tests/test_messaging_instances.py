"""消息平台多实例：配置面 / 运行态 / 队列 都按 `instance_id` 分开。

这一片只加了**维度**，没有加界面：默认实例（`instance_id=''`）就是存量那一份，
所有查询的默认值都是它，所以老行为一字不变。用例守住三件事：

1. 两个实例的配置互不覆盖（PK 加维了才可能做到，否则 upsert 会命中老行）；
2. 两个实例的运行态互不覆盖（合成一份会让 A 显示成 B 的 connected）；
3. 实例的密钥只读带后缀的环境变量，**不回落到基础键**——回落等于两个机器人
   偷偷共用一个飞书应用，正是这个功能要避免的事。
"""
import os
import unittest
import uuid

from tests.exec_env import PG_DSN, new_store, reset_db


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class ChannelInstanceTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        await self.store.ready()
        suffix = uuid.uuid4().hex[:8]
        self.tenant = f't_{suffix}'
        self.user = f'u_{suffix}'

    async def _upsert(self, *, instance: str, bot_id: str, **fields):
        return await self.store.im_upsert_channel(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            instance_id=instance, bot_id=bot_id, **fields)

    async def test_two_instances_do_not_overwrite_each_other(self):
        await self._upsert(instance='', bot_id='bot_default')
        await self._upsert(instance='bot_xzs', bot_id='bot_xzs')

        legacy = await self.store.im_channel(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user)
        other = await self.store.im_channel(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            instance_id='bot_xzs')

        self.assertEqual(legacy['bot_id'], 'bot_default')
        self.assertEqual(other['bot_id'], 'bot_xzs')
        self.assertEqual(len(await self.store.im_list_channels(
            tenant_id=self.tenant, owner_user_id=self.user)), 2)

    async def test_partial_update_stays_inside_one_instance(self):
        await self._upsert(instance='', bot_id='bot_default', dm_policy='open')
        await self._upsert(instance='bot_xzs', bot_id='bot_xzs',
                           dm_policy='allowlist')
        # 只改一个字段：另一个实例的同一个字段不能跟着变
        await self._upsert(instance='bot_xzs', bot_id='bot_xzs', dm_policy='pair')

        legacy = await self.store.im_channel(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user)
        other = await self.store.im_channel(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            instance_id='bot_xzs')
        self.assertEqual(legacy['dm_policy'], 'open')
        self.assertEqual(other['dm_policy'], 'pair')

    async def test_runtime_is_per_instance(self):
        await self._upsert(instance='', bot_id='bot_default', enabled=True)
        await self._upsert(instance='bot_xzs', bot_id='bot_xzs', enabled=True)

        await self.store.im_set_runtime(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            state='connected', fingerprint='fp-default', worker_id='w1')
        await self.store.im_set_runtime(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            state='startup_failed', detail='app_id 无效',
            error_code='auth_invalid', fingerprint='fp-xzs', worker_id='w1',
            instance_id='bot_xzs')

        legacy = await self.store.im_runtime(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user)
        other = await self.store.im_runtime(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            instance_id='bot_xzs')

        self.assertEqual(legacy['state'], 'connected')
        self.assertEqual(legacy['fingerprint'], 'fp-default')
        self.assertEqual(other['state'], 'startup_failed')
        self.assertEqual(other['error_code'], 'auth_invalid')

        # 清默认实例的运行态，不能顺手把另一个实例的清掉
        await self.store.im_clear_runtime(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user)
        self.assertIsNone(await self.store.im_runtime(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user))
        self.assertIsNotNone(await self.store.im_runtime(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            instance_id='bot_xzs'))

    async def test_outbox_claim_is_scoped_to_instance(self):
        """出站必须按实例领：领错了就会用 A 应用的凭据发 B 应用的消息。"""
        for instance in ('', 'bot_xzs'):
            await self.store.im_outbox_add(
                tenant_id=self.tenant, owner_user_id=self.user,
                channel_id='feishu', platform_chat_id='oc_1',
                sent_key=f'k-{instance or "default"}', text='你好',
                instance_id=instance)

        claimed = await self.store.im_outbox_claim(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user,
            instance_id='bot_xzs')
        self.assertEqual([row['text'] for row in claimed], ['你好'])
        self.assertEqual(claimed[0]['instance_id'], 'bot_xzs')

        # 默认实例那条还在队列里，没被顺手领走
        left = await self.store.im_outbox_claim(
            'feishu', tenant_id=self.tenant, owner_user_id=self.user)
        self.assertEqual(left[0]['instance_id'], '')


class InstanceSecretTests(unittest.TestCase):
    """实例密钥解析：纯函数，不需要数据库。"""

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in (
            'FEISHU_APP_ID', 'FEISHU_APP_SECRET',
            'FEISHU_APP_ID__BOT_XZS', 'FEISHU_APP_SECRET__BOT_XZS')}
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_suffix_is_env_safe(self):
        from webpage_agent.messaging.registry import instance_env_suffix
        self.assertEqual(instance_env_suffix(''), '')
        self.assertEqual(instance_env_suffix('bot_xzs'), 'BOT_XZS')
        # 机器人 handle 可能带连字符/中文，环境变量名只允许字母数字与下划线
        self.assertEqual(instance_env_suffix('feishu-客服'), 'FEISHU')

    def test_instance_reads_suffixed_key_only(self):
        from webpage_agent.messaging.registry import get_manifest, secrets_for
        os.environ['FEISHU_APP_ID'] = 'cli_default'
        os.environ['FEISHU_APP_SECRET'] = 'sec_default'
        os.environ['FEISHU_APP_ID__BOT_XZS'] = 'cli_xzs'
        os.environ['FEISHU_APP_SECRET__BOT_XZS'] = 'sec_xzs'
        manifest = get_manifest('feishu')

        self.assertEqual(secrets_for(manifest)['app_id'], 'cli_default')
        self.assertEqual(
            secrets_for(manifest, instance_id='bot_xzs')['app_id'], 'cli_xzs')

    def test_instance_does_not_fall_back_to_base_key(self):
        """只配了基础键 + 声明了实例：结果必须是「缺」，不能悄悄共用。"""
        from webpage_agent.messaging.registry import (diagnose, get_manifest,
                                                      secrets_for)
        os.environ['FEISHU_APP_ID'] = 'cli_default'
        os.environ['FEISHU_APP_SECRET'] = 'sec_default'
        manifest = get_manifest('feishu')

        self.assertEqual(secrets_for(manifest, instance_id='bot_xzs')['app_id'], '')
        configured, missing, _callback = diagnose(
            manifest, login_state=None, public_base_url='https://example.com',
            instance_id='bot_xzs')
        self.assertFalse(configured)
        self.assertIn('App ID', missing)

    def test_default_instance_keeps_working(self):
        from webpage_agent.messaging.registry import diagnose, get_manifest
        os.environ['FEISHU_APP_ID'] = 'cli_default'
        os.environ['FEISHU_APP_SECRET'] = 'sec_default'
        configured, missing, callback = diagnose(
            get_manifest('feishu'), login_state=None,
            public_base_url='https://example.com')
        self.assertTrue(configured, missing)
        self.assertEqual(callback, 'https://example.com/api/messaging/webhook/feishu')

    def test_config_key_carries_instance(self):
        """网关靠 ChannelConfig.key 区分实例，漏了就会互相顶掉。"""
        from webpage_agent.messaging.gateway import build_channel_config
        from webpage_agent.messaging.registry import get_manifest
        os.environ['FEISHU_APP_ID__BOT_XZS'] = 'cli_xzs'
        row = {'channel_id': 'feishu', 'tenant_id': 't1', 'owner_user_id': 'u1',
               'instance_id': 'bot_xzs', 'bot_id': 'bot_xzs'}
        config = build_channel_config(row, get_manifest('feishu'))
        self.assertEqual(config.key, 't1:u1:feishu:bot_xzs')
        self.assertEqual(config.secret('app_id'), 'cli_xzs')
