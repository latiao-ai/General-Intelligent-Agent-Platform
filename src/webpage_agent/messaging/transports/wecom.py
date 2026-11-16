"""企业微信（WeCom）自建应用适配器：AES 回调收、REST 主动发。

回调流程（必须公网 HTTPS）：

    GET  /api/messaging/webhook/wecom?msg_signature&timestamp&nonce&echostr
         → 验签 → 解密 echostr → 原样返回明文（URL 验证）
    POST 同上查询参数 + XML（<Encrypt>）
         → 验签 → 解密 → 解析 → 落 im_inbox → **立即返回空串**
         → Worker 执行后调 message/send 主动发送

企业微信要求 5 秒内响应，所以绝不能在回调里跑 Agent；被动回复 XML 也不用——
主动发送更适合"长任务 + 重试"。群机器人 webhook 只能发不能收，只作为通知出口。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx

from webpage_agent.messaging.base import (CallbackTransport, ChannelConfig,
                                          TransportError)
from webpage_agent.messaging.models import (Attachment, Capabilities, Inbound,
                                            Outbound, SendResult)
from webpage_agent.messaging.normalize import normalize_text
from webpage_agent.messaging.security import (parse_xml, verify_wecom_signature,
                                               wecom_decrypt, xml_text)

DEFAULT_BASE = 'https://qyapi.weixin.qq.com'
MAX_TEXT = 2000
# 这些错误码重试有用（限流/系统繁忙）；其余（IP 不在白名单、应用不可见）重试无用。
RETRYABLE_ERRCODES = {45009, -1, 45033}


@dataclass(frozen=True)
class WebhookEvent:
    kind: str                    # verify | message | ignored
    plaintext: str = ''
    inbound: Inbound | None = None
    reason: str = ''


class WeComTransport(CallbackTransport):
    name = 'wecom'

    def __init__(self, config: ChannelConfig, **kwargs) -> None:
        super().__init__(config, **kwargs)
        self._token = ''
        self._token_expires = 0.0
        self._base = (config.secret('base_url') or DEFAULT_BASE).rstrip('/')

    def capabilities(self) -> Capabilities:
        return Capabilities(card=False, stream_edit=False, typing=False,
                            thread=False, reaction=False, group=True,
                            max_text_chars=MAX_TEXT)

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        config = self.config
        for key in ('corp_id', 'agent_id', 'secret', 'token', 'aes_key'):
            config.require(key)
        agent_id = config.secret('agent_id')
        if not agent_id.isdigit():
            raise TransportError('WECOM_AGENT_ID 必须是数字',
                                 code='bad_config', retryable=False)
        try:
            await self._access_token(force=True)
        except httpx.HTTPError as exc:
            raise TransportError(f'企业微信鉴权接口不可达：{type(exc).__name__}',
                                 code='unreachable', retryable=True) from None
        self.report('connected', '回调已就绪，等待企业微信推送')

    async def stop(self) -> None:
        await self.close_http()

    # ---------- 发送 ----------

    async def send(self, chat_id: str, msg: Outbound) -> SendResult:
        text = normalize_text(msg.text or '')
        if not text:
            return SendResult(ok=False, error_code='empty_text', retryable=False)
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 1] + '…'
        try:
            token = await self._access_token()
        except httpx.HTTPError:
            return SendResult(ok=False, error_code='unreachable', retryable=True)
        except TransportError as exc:
            return SendResult(ok=False, error_code=exc.code, retryable=exc.retryable)

        target = str(chat_id or '')
        try:
            if target.startswith('chatid:'):
                url = f'{self._base}/cgi-bin/appchat/send'
                payload = {'chatid': target.split(':', 1)[1], 'msgtype': 'text',
                           'text': {'content': text}}
            else:
                url = f'{self._base}/cgi-bin/message/send'
                payload = {'touser': target, 'msgtype': 'text',
                           'agentid': int(self.config.secret('agent_id')),
                           'text': {'content': text}, 'safe': 0}
            reply = await self.http.post(url, params={'access_token': token},
                                         json=payload)
        except httpx.HTTPError:
            return SendResult(ok=False, error_code='unreachable', retryable=True)
        body = _body(reply)
        errcode = int(body.get('errcode', 0) or 0)
        if errcode == 0:
            return SendResult(ok=True, message_id=str(body.get('msgid') or ''))
        if errcode in (40014, 42001, 41001):
            self._token = ''
            self._token_expires = 0.0
        return SendResult(ok=False, error_code=f'wecom_{errcode}',
                          retryable=errcode in RETRYABLE_ERRCODES
                          or reply.status_code >= 500)

    async def _access_token(self, *, force: bool = False) -> str:
        now = time.time()
        if not force and self._token and now < self._token_expires - 300:
            return self._token
        response = await self.http.get(
            f'{self._base}/cgi-bin/gettoken',
            params={'corpid': self.config.secret('corp_id'),
                    'corpsecret': self.config.secret('secret')})
        body = _body(response)
        errcode = body.get('errcode')
        if errcode is None or int(errcode) != 0 or not body.get('access_token'):
            raise TransportError(
                f'企业微信鉴权失败：{body.get("errmsg") or body.get("errcode")}',
                code='auth_failed', retryable=False)
        self._token = str(body['access_token'])
        self._token_expires = now + float(body.get('expires_in') or 7200)
        return self._token

    # ---------- 回调解析 ----------

    @classmethod
    def parse_webhook(cls, *, method: str, query: dict, raw: bytes,
                      config: ChannelConfig) -> WebhookEvent:
        token = config.secret('token')
        aes_key = config.secret('aes_key')
        corp_id = config.secret('corp_id')
        if not token or not aes_key:
            raise TransportError('企业微信回调缺少 WECOM_TOKEN / WECOM_AES_KEY',
                                 code='not_configured', retryable=False)
        timestamp = str(query.get('timestamp') or '')
        nonce = str(query.get('nonce') or '')
        signature = str(query.get('msg_signature') or '')

        if method.upper() == 'GET':
            echostr = str(query.get('echostr') or '')
            if not echostr:
                raise TransportError('缺少 echostr', code='bad_request',
                                     retryable=False)
            if not verify_wecom_signature(token=token, timestamp=timestamp,
                                          nonce=nonce, encrypt=echostr,
                                          signature=signature):
                raise TransportError('企业微信验签失败', code='bad_signature',
                                     retryable=False)
            plaintext, receive_id = wecom_decrypt(echostr, aes_key)
            if corp_id and receive_id and receive_id != corp_id:
                raise TransportError('企业微信 receive_id 不匹配',
                                     code='bad_corp', retryable=False)
            return WebhookEvent(kind='verify', plaintext=plaintext)

        try:
            envelope = parse_xml(raw.decode('utf-8', errors='replace'))
        except Exception as exc:                    # noqa: BLE001
            raise TransportError(f'企业微信回调 XML 解析失败：{exc}',
                                 code='bad_body', retryable=False) from None
        encrypt = xml_text(envelope, 'Encrypt')
        if not encrypt:
            raise TransportError('企业微信回调缺少 Encrypt', code='bad_body',
                                 retryable=False)
        if not verify_wecom_signature(token=token, timestamp=timestamp,
                                      nonce=nonce, encrypt=encrypt,
                                      signature=signature):
            raise TransportError('企业微信验签失败', code='bad_signature',
                                 retryable=False)
        plaintext, receive_id = wecom_decrypt(encrypt, aes_key)
        if corp_id and receive_id and receive_id != corp_id:
            raise TransportError('企业微信 receive_id 不匹配', code='bad_corp',
                                 retryable=False)
        return _to_inbound(plaintext, config)


def _to_inbound(plaintext: str, config: ChannelConfig) -> WebhookEvent:
    try:
        root = parse_xml(plaintext)
    except Exception as exc:                        # noqa: BLE001
        raise TransportError(f'企业微信消息解析失败：{exc}', code='bad_body',
                             retryable=False) from None
    from_user = xml_text(root, 'FromUserName')
    msg_type = xml_text(root, 'MsgType').lower()
    if msg_type == 'event':
        return WebhookEvent(kind='ignored',
                            reason=f'event:{xml_text(root, "Event") or "unknown"}')
    chat_id_raw = xml_text(root, 'ChatId')
    chat_type = 'group' if chat_id_raw else 'dm'
    chat_id = f'chatid:{chat_id_raw}' if chat_id_raw else from_user
    if not from_user or not chat_id:
        return WebhookEvent(kind='ignored', reason='incomplete')
    text = ''
    attachments: tuple[Attachment, ...] = ()
    if msg_type == 'text':
        text = xml_text(root, 'Content')
    elif msg_type == 'image':
        text = '[图片]'
        attachments = (Attachment(kind='image', url=xml_text(root, 'PicUrl')),
                       )
    elif msg_type == 'voice':
        text = '[语音]'
    elif msg_type == 'video':
        text = '[视频]'
    elif msg_type == 'file':
        text = '[文件]'
    elif msg_type == 'location':
        text = '[位置]'
    elif msg_type == 'link':
        text = f'[链接] {xml_text(root, "Title")}'.strip()
    else:
        return WebhookEvent(kind='ignored', reason=f'type:{msg_type}')
    message_id = xml_text(root, 'MsgId')
    if not message_id:
        message_id = ('sha256:' + hashlib.sha256(
            f'{from_user}|{xml_text(root, "CreateTime")}|{text}'.encode('utf-8')
        ).hexdigest()[:24])
    mentioned = bool(chat_id_raw and '@' in text)
    event = Inbound(
        platform='wecom',
        tenant=str(config.tenant_id or 'default'),
        chat_id=chat_id,
        chat_type=chat_type,  # type: ignore[arg-type]
        user_id=from_user,
        user_name=from_user,
        text=text,
        message_id=message_id,
        attachments=attachments,
        mentioned=mentioned,
        raw={},
    )
    return WebhookEvent(kind='message', inbound=event)


def _body(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}

