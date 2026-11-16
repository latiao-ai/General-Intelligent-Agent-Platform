"""消息平台（IM 网关）的单元测试。

分四层：

1. **纯函数**：状态派生、切分、富文本解析、鉴权矩阵、清单诊断；
2. **加解密**：企微官方向量、飞书签名/加解密、回调验签；
3. **适配器契约**：能力声明与清单一致；MockTransport 下的发送报文；
4. **回调归一化**：飞书/企微的真实报文形状 → `Inbound`。

不碰真实平台、不花钱；需要凭据的用例全部用 `httpx.MockTransport` 替身。
"""
import json
import time
import unittest

import httpx

from webpage_agent.messaging import normalize, router, state
from webpage_agent.messaging.base import ChannelConfig
from webpage_agent.messaging.models import Inbound, Outbound
from webpage_agent.messaging.registry import (CHANNELS, FEISHU, WEIXIN, WECOM,
                                              diagnose, get_manifest)
from webpage_agent.messaging.security import (assert_public_http_url,
                                              feishu_decrypt, feishu_encrypt,
                                              feishu_signature, parse_xml,
                                              verify_feishu_signature,
                                              verify_wecom_signature,
                                              wecom_decrypt, wecom_encrypt,
                                              wecom_signature)
from webpage_agent.messaging.transports.feishu import FeishuTransport
from webpage_agent.messaging.transports.wecom import WeComTransport
from webpage_agent.messaging.transports.weixin import WeixinTransport


def event(**overrides) -> Inbound:
    base = dict(platform='feishu', tenant='t1', chat_id='c1', chat_type='dm',
                user_id='u1', user_name='张三', text='你好', message_id='m1')
    base.update(overrides)
    return Inbound(**base)


def cfg(channel='feishu', **kwargs) -> ChannelConfig:
    base = dict(channel_id=channel, tenant_id='t1', owner_user_id='u1',
                options={}, secrets={}, dm_policy='pair',
                group_policy='mention', allowed_users=(),
                public_base_url='https://im.example.com')
    base.update(kwargs)
    return ChannelConfig(**base)


# ---------------------------------------------------------------- 状态


class StateTests(unittest.TestCase):

    def test_disabled(self):
        got = state.derive(enabled=False, configured=True, gateway_alive=True,
                           runtime=None, fingerprint='f')
        self.assertEqual(got['state'], 'disabled')
        self.assertEqual(got['tone'], 'muted')

    def test_not_configured_before_gateway(self):
        got = state.derive(enabled=True, configured=False, gateway_alive=True,
                           runtime={'state': 'connected'}, fingerprint='f')
        self.assertEqual(got['state'], 'not_configured')

    def test_gateway_stopped(self):
        got = state.derive(enabled=True, configured=True, gateway_alive=False,
                           runtime={'state': 'connected'}, fingerprint='f')
        self.assertEqual(got['state'], 'gateway_stopped')

    def test_pending_restart_without_runtime(self):
        got = state.derive(enabled=True, configured=True, gateway_alive=True,
                           runtime=None, fingerprint='f')
        self.assertEqual(got['state'], 'pending_restart')
        self.assertEqual(got['tone'], 'warn')

    def test_pending_restart_on_fingerprint_change(self):
        got = state.derive(
            enabled=True, configured=True, gateway_alive=True,
            runtime={'state': 'connected', 'fingerprint': 'old'},
            fingerprint='new')
        self.assertEqual(got['state'], 'pending_restart')

    def test_connected(self):
        got = state.derive(
            enabled=True, configured=True, gateway_alive=True,
            runtime={'state': 'connected', 'fingerprint': 'f',
                     'updated_at_ts': time.time()}, fingerprint='f')
        self.assertEqual(got['state'], 'connected')
        self.assertEqual(got['tone'], 'good')

    def test_stale_runtime_becomes_pending(self):
        got = state.derive(
            enabled=True, configured=True, gateway_alive=True,
            runtime={'state': 'connected', 'fingerprint': 'f',
                     'updated_at_ts': time.time() - 3600}, fingerprint='f')
        self.assertEqual(got['state'], 'pending_restart')

    def test_fatal_tone_bad_and_startup_failed_alias(self):
        got = state.derive(enabled=True, configured=True, gateway_alive=True,
                           runtime={'state': 'fatal', 'fingerprint': 'f',
                                    'detail': 'token 失效'},
                           fingerprint='f')
        self.assertEqual(got['tone'], 'bad')
        self.assertIn('token', got['reason'])
        alias = state.derive(enabled=True, configured=True, gateway_alive=True,
                             runtime={'state': 'startup_failed',
                                      'fingerprint': 'f'},
                             fingerprint='f')
        self.assertEqual(alias['state'], 'startup_failed')

    def test_fingerprint_is_sensitive_but_hides_secret(self):
        first = state.config_fingerprint(
            enabled=True, bot_id='b', dm_policy='pair', group_policy='mention',
            allowed_users=['a'], config={'home_chat_id': 'x'},
            secrets={'app_secret': 's3cret'}, public_base_url='https://x')
        second = state.config_fingerprint(
            enabled=True, bot_id='b', dm_policy='pair', group_policy='mention',
            allowed_users=['a'], config={'home_chat_id': 'x'},
            secrets={'app_secret': 's3cret2'}, public_base_url='https://x')
        self.assertNotEqual(first, second)
        self.assertNotIn('s3cret', first)
        self.assertEqual(len(first), 16)


# ---------------------------------------------------------------- 归一化与切分


class ChunkTests(unittest.TestCase):

    def test_short_text_is_one_chunk(self):
        self.assertEqual(normalize.chunk_text('你好', 100), ['你好'])

    def test_prefers_paragraph_boundary(self):
        text = '第一段。' * 10 + '\n\n' + '第二段。' * 10
        chunks = normalize.chunk_text(text, 60)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].startswith('第一段。'))
        self.assertIn('（1/', chunks[0])

    def test_each_chunk_within_budget(self):
        text = '字' * 500
        chunks = normalize.chunk_text(text, 80)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks), chunks)

    def test_marker_can_be_disabled(self):
        chunks = normalize.chunk_text('字' * 200, 60, marker=False)
        self.assertFalse(any('（1/' in chunk for chunk in chunks))


class FeishuTextTests(unittest.TestCase):

    def test_text_with_mention_placeholder(self):
        content = json.dumps({'text': '@_user_1 请看一下'})
        got = normalize.feishu_extract_text(
            'text', content, mentions=[{'key': '@_user_1', 'name': '小助手'}])
        self.assertEqual(got, '@小助手 请看一下')

    def test_post_rich_text(self):
        content = json.dumps({
            'title': '周报',
            'content': [[
                {'tag': 'text', 'text': '本周完成 '},
                {'tag': 'a', 'text': '需求', 'href': 'https://x'},
                {'tag': 'img'},
            ], [{'tag': 'at', 'user_name': '李四'}, {'tag': 'text', 'text': ' 看一下'}]],
        })
        got = normalize.feishu_extract_text('post', content)
        self.assertIn('周报', got)
        self.assertIn('本周完成 需求', got)
        self.assertIn('[图片]', got)
        self.assertIn('@李四 看一下', got)

    def test_unknown_type_is_empty(self):
        self.assertEqual(normalize.feishu_extract_text('sticker', '{}'), '')

    def test_commands(self):
        self.assertEqual(normalize.command_of('/NEW'), 'new')
        self.assertEqual(normalize.command_of('/help 一下'), 'help')
        self.assertEqual(normalize.command_of('你好'), '')
        self.assertEqual(router.command_action('stop'), 'stop')
        self.assertEqual(router.command_action('unknown'), '')


# ---------------------------------------------------------------- 鉴权


class AuthzTests(unittest.TestCase):

    def authorize(self, e=None, **kwargs):
        base = dict(dm_policy='pair', group_policy='mention',
                    allowed_users=[], paired_users=[])
        base.update(kwargs)
        return router.authorize(e or event(), **base)

    def test_dm_pairing_default(self):
        self.assertEqual(self.authorize().action, router.PAIR)

    def test_dm_allowlist_ignores_unknown(self):
        got = self.authorize(dm_policy='allowlist')
        self.assertEqual(got.action, router.IGNORE)

    def test_dm_allowlist_allows_listed(self):
        got = self.authorize(dm_policy='allowlist', allowed_users=['u1'])
        self.assertEqual(got.action, router.ALLOW)

    def test_dm_paired_user_allowed(self):
        got = self.authorize(paired_users=['u1'])
        self.assertEqual(got.action, router.ALLOW)

    def test_wildcard_allows_all(self):
        got = self.authorize(dm_policy='allowlist', allowed_users=['*'])
        self.assertEqual(got.action, router.ALLOW)

    def test_open(self):
        self.assertEqual(self.authorize(dm_policy='open').action, router.ALLOW)

    def test_disabled_and_unknown_policy_deny(self):
        self.assertEqual(self.authorize(dm_policy='disabled').action, router.IGNORE)
        self.assertEqual(self.authorize(dm_policy='typo').action, router.PAIR)

    def test_group_requires_mention(self):
        group = event(chat_type='group', mentioned=False)
        got = self.authorize(group, group_policy='mention', allowed_users=['u1'])
        self.assertEqual(got.action, router.IGNORE)

    def test_group_mentioned_and_listed(self):
        group = event(chat_type='group', mentioned=True)
        got = self.authorize(group, group_policy='mention', allowed_users=['u1'])
        self.assertEqual(got.action, router.ALLOW)

    def test_group_disabled(self):
        group = event(chat_type='group', mentioned=True)
        got = self.authorize(group, group_policy='disabled', allowed_users=['u1'])
        self.assertEqual(got.action, router.IGNORE)

    def test_group_does_not_issue_pairing(self):
        group = event(chat_type='group', mentioned=True)
        got = self.authorize(group, group_policy='mention', allowed_users=[])
        self.assertEqual(got.action, router.IGNORE)
        self.assertEqual(got.reason, 'group_not_listed')

    def test_session_key_includes_thread(self):
        e = event(chat_id='c1', thread_id='th')
        self.assertEqual(router.session_key(e), 'feishu:t1:c1:th')


# ---------------------------------------------------------------- 加解密


class WeComCryptoTests(unittest.TestCase):
    # 官方文档示例向量；实现错了这条必挂。
    TOKEN = 'QDG6eK'
    AES_KEY = 'jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C'
    CORP_ID = 'wx5823bf96d3bd56c7'
    ECHOSTR = ('P9nAzCzyDtyTWESHep1vC5X9xho/qYX3Zpb4yKa9SKld1DsH3Iyt3tP3z'
               'Ndtp+4RPcs8TgAE7OaBO+FZXvnaqQ==')
    SIGNATURE = '5c45ff5e21c57e6ad56bac8758b79b1d9ac89fd3'

    def test_sample_vector(self):
        self.assertTrue(verify_wecom_signature(
            token=self.TOKEN, timestamp='1409659589', nonce='263014780',
            encrypt=self.ECHOSTR, signature=self.SIGNATURE))
        plaintext, receive_id = wecom_decrypt(self.ECHOSTR, self.AES_KEY)
        self.assertEqual(plaintext, '1616140317555161061')
        self.assertEqual(receive_id, self.CORP_ID)

    def test_roundtrip(self):
        body = '<xml><MsgType>text</MsgType><Content>你好</Content></xml>'
        encrypted = wecom_encrypt(body, self.AES_KEY, self.CORP_ID)
        plaintext, receive_id = wecom_decrypt(encrypted, self.AES_KEY)
        self.assertEqual(plaintext, body)
        self.assertEqual(receive_id, self.CORP_ID)

    def test_signature_order_is_sorted(self):
        one = wecom_signature('a', 'b', 'c', 'd')
        two = wecom_signature('d', 'c', 'b', 'a')
        self.assertEqual(one, two)

    def test_bad_signature_rejected(self):
        self.assertFalse(verify_wecom_signature(
            token=self.TOKEN, timestamp='1', nonce='2', encrypt='x',
            signature='nope'))


class FeishuCryptoTests(unittest.TestCase):

    def test_roundtrip(self):
        key = 'test-encrypt-key'
        payload = json.dumps({'type': 'url_verification', 'challenge': 'abc'})
        encrypted = feishu_encrypt(payload, key)
        self.assertEqual(feishu_decrypt(encrypted, key), payload)

    def test_signature_uses_raw_body(self):
        raw = b'{"a":1}'
        signature = feishu_signature('1700000000', 'nonce', 'key', raw)
        headers = {'x-lark-request-timestamp': '1700000000',
                   'x-lark-request-nonce': 'nonce',
                   'x-lark-signature': signature}
        self.assertTrue(verify_feishu_signature(headers, raw, 'key'))
        self.assertFalse(verify_feishu_signature(headers, b'{"a":2}', 'key'))
        self.assertFalse(verify_feishu_signature(headers, raw, 'other'))


class SecurityTests(unittest.TestCase):

    def test_public_url_blocks_private(self):
        for url in ('http://127.0.0.1/x', 'http://10.0.0.1/x',
                    'http://192.168.1.2/x', 'file:///etc/passwd',
                    'http://localhost/x'):
            with self.assertRaises(ValueError, msg=url):
                assert_public_http_url(url)
        self.assertEqual(assert_public_http_url('https://example.com/a'),
                         'https://example.com/a')

    def test_xml_rejects_dtd(self):
        with self.assertRaises(ValueError):
            parse_xml('<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')
        self.assertEqual(parse_xml('<x><a>1</a></x>').findtext('a'), '1')


# ---------------------------------------------------------------- 清单


class RegistryTests(unittest.TestCase):

    def test_three_channels_and_capability_contract(self):
        self.assertEqual([item.id for item in CHANNELS], [FEISHU, WECOM, WEIXIN])
        for manifest in CHANNELS:
            transport_cls = manifest.transport()
            # 能力声明必须与适配器一致：UI 与 Router 都读清单。
            inst = transport_cls(cfg(manifest.id), on_inbound=_noop)
            self.assertEqual(inst.capabilities(), manifest.capabilities)

    def test_feishu_diagnose(self):
        manifest = get_manifest(FEISHU)
        old = {item.env: __import__('os').environ.get(item.env) for item in manifest.env_fields}
        try:
            import os
            for item in manifest.env_fields:
                os.environ.pop(item.env, None)
            ok, missing, callback = diagnose(
                manifest, login_state={}, public_base_url='')
            self.assertFalse(ok)
            self.assertTrue(any('公网' in item for item in missing))
            os.environ['FEISHU_APP_ID'] = 'cli_x'
            os.environ['FEISHU_APP_SECRET'] = 'secret'
            ok, missing, callback = diagnose(
                manifest, login_state={},
                public_base_url='https://im.example.com/')
            self.assertTrue(ok, missing)
            self.assertEqual(callback,
                             'https://im.example.com/api/messaging/webhook/feishu')
            ok, missing, _ = diagnose(
                manifest, login_state={}, public_base_url='http://im.example.com')
            self.assertFalse(ok)
            self.assertTrue(any('https' in item for item in missing))
        finally:
            import os
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_weixin_diagnose_needs_login(self):
        manifest = get_manifest(WEIXIN)
        ok, missing, callback = diagnose(
            manifest, login_state={}, public_base_url='')
        self.assertFalse(ok)
        self.assertTrue(any('扫码' in item for item in missing))
        self.assertEqual(callback, '')
        ok, _, _ = diagnose(manifest, login_state={'token': 'tok'},
                            public_base_url='')
        self.assertTrue(ok)


async def _noop(_event):
    return None


# ---------------------------------------------------------------- 适配器发送


class FeishuSendTests(unittest.IsolatedAsyncioTestCase):

    async def test_start_and_send(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/tenant_access_token/internal'):
                return httpx.Response(200, json={
                    'code': 0, 'tenant_access_token': 't-1', 'expire': 7200})
            if request.url.path.endswith('/im/v1/messages'):
                seen['params'] = dict(request.url.params)
                seen['body'] = json.loads(request.content)
                return httpx.Response(200, json={
                    'code': 0, 'data': {'message_id': 'om_9'}})
            return httpx.Response(404)

        transport = FeishuTransport(
            cfg(FEISHU, secrets={'app_id': 'cli_x', 'app_secret': 'sec'}),
            on_inbound=_noop, http_transport=httpx.MockTransport(handler))
        await transport.start()
        result = await transport.send('oc_1', Outbound(text='你好'))
        await transport.stop()
        self.assertTrue(result.ok)
        self.assertEqual(result.message_id, 'om_9')
        self.assertEqual(seen['params']['receive_id_type'], 'chat_id')
        self.assertEqual(seen['body']['receive_id'], 'oc_1')
        self.assertEqual(json.loads(seen['body']['content'])['text'], '你好')

    async def test_reply_uses_reply_endpoint(self):
        paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith('/tenant_access_token/internal'):
                return httpx.Response(200, json={
                    'code': 0, 'tenant_access_token': 't', 'expire': 7200})
            return httpx.Response(200, json={
                'code': 0, 'data': {'message_id': 'om_r'}})

        transport = FeishuTransport(
            cfg(FEISHU, secrets={'app_id': 'a', 'app_secret': 'b'}),
            on_inbound=_noop, http_transport=httpx.MockTransport(handler))
        result = await transport.send('oc_1', Outbound(text='x', reply_to='om_0'))
        await transport.stop()
        self.assertTrue(result.ok)
        self.assertTrue(any(path.endswith('/im/v1/messages/om_0/reply')
                            for path in paths))

    async def test_auth_failure_is_not_retryable(self):
        transport = FeishuTransport(
            cfg(FEISHU, secrets={'app_id': 'a', 'app_secret': 'b'}),
            on_inbound=_noop,
            http_transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={
                    'code': 10003, 'msg': 'invalid credential'})))
        with self.assertRaises(Exception):
            await transport.start()
        await transport.stop()


class WeComSendTests(unittest.IsolatedAsyncioTestCase):

    async def test_start_and_send(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/gettoken'):
                return httpx.Response(200, json={
                    'errcode': 0, 'access_token': 'at-1', 'expires_in': 7200})
            if request.url.path.endswith('/message/send'):
                seen['params'] = dict(request.url.params)
                seen['body'] = json.loads(request.content)
                return httpx.Response(200, json={'errcode': 0, 'msgid': 'w-1'})
            return httpx.Response(404)

        transport = WeComTransport(
            cfg(WECOM, secrets={
                'corp_id': 'corp', 'agent_id': '100001', 'secret': 's',
                'token': 'tok', 'aes_key': 'k' * 43}),
            on_inbound=_noop, http_transport=httpx.MockTransport(handler))
        await transport.start()
        result = await transport.send('zhangsan', Outbound(text='hello'))
        await transport.stop()
        self.assertTrue(result.ok)
        self.assertEqual(result.message_id, 'w-1')
        self.assertEqual(seen['params']['access_token'], 'at-1')
        self.assertEqual(seen['body']['touser'], 'zhangsan')
        self.assertEqual(seen['body']['agentid'], 100001)

    async def test_group_chat_uses_appchat(self):
        paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith('/gettoken'):
                return httpx.Response(200, json={
                    'errcode': 0, 'access_token': 'at', 'expires_in': 7200})
            return httpx.Response(200, json={'errcode': 0, 'msgid': 'w-2'})

        transport = WeComTransport(
            cfg(WECOM, secrets={
                'corp_id': 'c', 'agent_id': '1', 'secret': 's',
                'token': 't', 'aes_key': 'k' * 43}),
            on_inbound=_noop, http_transport=httpx.MockTransport(handler))
        result = await transport.send('chatid:wr_1', Outbound(text='hi'))
        await transport.stop()
        self.assertTrue(result.ok)
        self.assertTrue(any('/cgi-bin/appchat/send' in path for path in paths))


class WeixinSendTests(unittest.IsolatedAsyncioTestCase):

    async def test_send_echoes_context_token(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen['body'] = json.loads(request.content)
            return httpx.Response(200, json={'ret': 0, 'errcode': 0})

        transport = WeixinTransport(
            cfg(WEIXIN, secrets={'token': 'tok', 'account_id': 'bot@im.bot'}),
            on_inbound=_noop, http_transport=httpx.MockTransport(handler))
        transport._context['u1'] = 'ctx-1'      # 入站时缓存
        result = await transport.send('u1', Outbound(text='你好'))
        await transport.stop()
        self.assertTrue(result.ok)
        self.assertEqual(seen['body']['msg']['context_token'], 'ctx-1')
        self.assertEqual(seen['body']['msg']['to_user_id'], 'u1')
        self.assertEqual(seen['body']['msg']['item_list'][0]['text_item']['text'],
                         '你好')

    async def test_expired_session_retries_without_token(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append('context_token' in body['msg'])
            if len(calls) == 1:
                return httpx.Response(200, json={'ret': -14, 'errcode': -14})
            return httpx.Response(200, json={'ret': 0, 'errcode': 0})

        transport = WeixinTransport(
            cfg(WEIXIN, secrets={'token': 'tok', 'account_id': 'bot@im.bot'}),
            on_inbound=_noop, http_transport=httpx.MockTransport(handler))
        transport._context['u1'] = 'ctx'
        result = await transport.send('u1', Outbound(text='hi'))
        await transport.stop()
        self.assertTrue(result.ok)
        self.assertEqual(calls, [True, False])

    async def test_session_not_ready_is_not_retryable(self):
        transport = WeixinTransport(
            cfg(WEIXIN, secrets={'token': 'tok', 'account_id': 'bot@im.bot'}),
            on_inbound=_noop, http_transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={'ret': -2, 'errcode': -2,
                               'errmsg': 'unknown error'})))
        result = await transport.send('u1', Outbound(text='hi'))
        await transport.stop()
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertEqual(result.error_code, 'session_expired')


# ---------------------------------------------------------------- 回调归一化


class FeishuWebhookTests(unittest.TestCase):

    def _config(self, encrypt_key=''):
        return cfg(FEISHU, secrets={'verification_token': 'vt',
                                    'encrypt_key': encrypt_key})

    def test_url_verification_plaintext(self):
        raw = json.dumps({'type': 'url_verification', 'challenge': 'c-1',
                          'token': 'vt'}).encode()
        parsed = FeishuTransport.parse_webhook(raw, {}, self._config())
        self.assertEqual(parsed.kind, 'challenge')
        self.assertEqual(parsed.challenge, 'c-1')

    def test_message_plaintext(self):
        payload = {
            'schema': '2.0',
            'header': {'event_type': 'im.message.receive_v1', 'token': 'vt'},
            'event': {
                'sender': {'sender_type': 'user',
                           'sender_id': {'open_id': 'ou_1'}},
                'message': {
                    'message_id': 'om_1', 'chat_id': 'oc_1', 'chat_type': 'p2p',
                    'message_type': 'text',
                    'content': json.dumps({'text': '帮我总结'}),
                },
            },
        }
        parsed = FeishuTransport.parse_webhook(
            json.dumps(payload).encode(), {}, self._config())
        self.assertEqual(parsed.kind, 'message')
        self.assertIsNotNone(parsed.inbound)
        self.assertEqual(parsed.inbound.chat_id, 'oc_1')
        self.assertEqual(parsed.inbound.user_id, 'ou_1')
        self.assertEqual(parsed.inbound.text, '帮我总结')

    def test_bad_token_rejected(self):
        raw = json.dumps({'type': 'url_verification', 'challenge': 'x',
                          'token': 'wrong'}).encode()
        with self.assertRaises(Exception):
            FeishuTransport.parse_webhook(raw, {}, self._config())

    def test_encrypted_and_signed(self):
        payload = {
            'schema': '2.0',
            'header': {'event_type': 'im.message.receive_v1'},
            'event': {'sender': {'sender_type': 'user',
                                 'sender_id': {'open_id': 'ou_2'}},
                      'message': {'message_id': 'om_2', 'chat_id': 'oc_2',
                                  'chat_type': 'group', 'message_type': 'text',
                                  'content': json.dumps({'text': '@_user_1 hi'}),
                                  'mentions': [{'key': '@_user_1',
                                                'name': '小助手'}]}},
        }
        key = 'enc-key'
        encrypted = feishu_encrypt(json.dumps(payload), key)
        raw = json.dumps({'encrypt': encrypted}).encode()
        headers = {'x-lark-request-timestamp': '1700',
                   'x-lark-request-nonce': 'n',
                   'x-lark-signature': feishu_signature('1700', 'n', key, raw)}
        parsed = FeishuTransport.parse_webhook(raw, headers, self._config(key))
        self.assertEqual(parsed.kind, 'message')
        self.assertEqual(parsed.inbound.chat_type, 'group')
        self.assertTrue(parsed.inbound.mentioned)
        self.assertIn('@小助手', parsed.inbound.text)

    def test_bad_signature_rejected(self):
        raw = json.dumps({'encrypt': 'x'}).encode()
        headers = {'x-lark-request-timestamp': '1',
                   'x-lark-request-nonce': 'n', 'x-lark-signature': 'bad'}
        with self.assertRaises(Exception):
            FeishuTransport.parse_webhook(raw, headers, self._config('k'))


class WeComWebhookTests(unittest.TestCase):
    TOKEN = 'QDG6eK'
    AES_KEY = 'jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C'
    CORP_ID = 'wx5823bf96d3bd56c7'

    def config(self):
        return cfg(WECOM, secrets={
            'corp_id': self.CORP_ID, 'agent_id': '1', 'secret': 's',
            'token': self.TOKEN, 'aes_key': self.AES_KEY})

    def test_get_verification(self):
        plaintext = '1616140317555161061'
        encrypted = wecom_encrypt(plaintext, self.AES_KEY, self.CORP_ID)
        query = {'msg_signature': wecom_signature(
                     self.TOKEN, '1409659589', '263014780', encrypted),
                 'timestamp': '1409659589', 'nonce': '263014780',
                 'echostr': encrypted}
        parsed = WeComTransport.parse_webhook(
            method='GET', query=query, raw=b'', config=self.config())
        self.assertEqual(parsed.kind, 'verify')
        self.assertEqual(parsed.plaintext, plaintext)

    def test_text_message(self):
        body = ('<xml><ToUserName><![CDATA[corp]]></ToUserName>'
                '<FromUserName><![CDATA[zhangsan]]></FromUserName>'
                '<CreateTime>1700000000</CreateTime>'
                '<MsgType><![CDATA[text]]></MsgType>'
                '<Content><![CDATA[你好机器人]]></Content>'
                '<MsgId>123456789</MsgId>'
                '<AgentID>1</AgentID></xml>')
        encrypted = wecom_encrypt(body, self.AES_KEY, self.CORP_ID)
        raw = f'<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>'.encode()
        query = {'msg_signature': wecom_signature(
                     self.TOKEN, '1700000000', 'nonce', encrypted),
                 'timestamp': '1700000000', 'nonce': 'nonce'}
        parsed = WeComTransport.parse_webhook(
            method='POST', query=query, raw=raw, config=self.config())
        self.assertEqual(parsed.kind, 'message')
        self.assertEqual(parsed.inbound.chat_id, 'zhangsan')
        self.assertEqual(parsed.inbound.text, '你好机器人')
        self.assertEqual(parsed.inbound.message_id, '123456789')

    def test_group_message_uses_chatid(self):
        body = ('<xml><FromUserName><![CDATA[zhangsan]]></FromUserName>'
                '<CreateTime>1700000000</CreateTime>'
                '<MsgType><![CDATA[text]]></MsgType>'
                '<Content><![CDATA[@bot 看一下]]></Content>'
                '<MsgId>2</MsgId><ChatId><![CDATA[wr_1]]></ChatId></xml>')
        encrypted = wecom_encrypt(body, self.AES_KEY, self.CORP_ID)
        raw = f'<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>'.encode()
        query = {'msg_signature': wecom_signature(
                     self.TOKEN, '1', 'n', encrypted),
                 'timestamp': '1', 'nonce': 'n'}
        parsed = WeComTransport.parse_webhook(
            method='POST', query=query, raw=raw, config=self.config())
        self.assertEqual(parsed.inbound.chat_id, 'chatid:wr_1')
        self.assertEqual(parsed.inbound.chat_type, 'group')
        self.assertTrue(parsed.inbound.mentioned)


if __name__ == '__main__':
    unittest.main()
