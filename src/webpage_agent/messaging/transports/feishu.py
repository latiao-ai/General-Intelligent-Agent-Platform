"""飞书（Lark）自建应用适配器：HTTP 回调收、REST 发。

**为什么是回调而不是长连接**：长连接要引入 `lark-oapi`（它要求 `websockets<16`，
会与本项目 MCP 依赖的 `websockets 16` 冲突）。回调模式只依赖 httpx，签名/解密
规则稳定，且与本项目"API 进程收、Worker 进程执行"的拓扑天然契合：

    飞书 → POST /api/messaging/webhook/feishu（验签、解密、归一化、落 im_inbox）
         → 立即 200（平台要求 3 秒内）
         → Worker 从 im_inbox 领取 → AgentBridge → 本 Transport.send()

回调地址必须是公网 HTTPS；本地开发用内网穿透（cloudflared / ngrok）也可以。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

from webpage_agent.messaging.base import (CallbackTransport, ChannelConfig,
                                          TransportError)
from webpage_agent.messaging.models import (Attachment, Capabilities, Inbound,
                                            Outbound, SendResult)
from webpage_agent.messaging.normalize import (attachment_note,
                                               feishu_extract_text, normalize_text)
from webpage_agent.messaging.security import (feishu_decrypt,
                                               verify_feishu_signature)

DEFAULT_BASE = 'https://open.feishu.cn'
MAX_TEXT = 10_000


@dataclass(frozen=True)
class WebhookEvent:
    """一次回调解析结果：URL 验证、一条消息、或什么都不做。"""

    kind: str                    # challenge | message | ignored
    challenge: str = ''
    inbound: Inbound | None = None
    reason: str = ''


class FeishuTransport(CallbackTransport):
    name = 'feishu'

    def __init__(self, config: ChannelConfig, **kwargs) -> None:
        super().__init__(config, **kwargs)
        self._token = ''
        self._token_expires = 0.0
        self._base = (config.secret('base_url') or DEFAULT_BASE).rstrip('/')

    def capabilities(self) -> Capabilities:
        return Capabilities(card=False, stream_edit=False, typing=False,
                            thread=True, reaction=True, group=True,
                            max_text_chars=MAX_TEXT)

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        config = self.config
        config.require('app_id')
        config.require('app_secret')
        try:
            await self._tenant_token(force=True)
        except httpx.HTTPError as exc:
            raise TransportError(f'飞书鉴权接口不可达：{type(exc).__name__}',
                                 code='unreachable', retryable=True) from None
        self.report('connected', '回调已就绪，等待飞书推送事件')

    async def stop(self) -> None:
        await self.close_http()

    # ---------- 发送 ----------

    async def send(self, chat_id: str, msg: Outbound) -> SendResult:
        text = normalize_text(msg.text or '')
        if not text:
            return SendResult(ok=False, error_code='empty_text', retryable=False)
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + '…'
        content = json.dumps({'text': text}, ensure_ascii=False)
        try:
            token = await self._tenant_token()
        except httpx.HTTPError:
            return SendResult(ok=False, error_code='unreachable', retryable=True)
        except TransportError as exc:
            return SendResult(ok=False, error_code=exc.code, retryable=exc.retryable)

        headers = {'Authorization': f'Bearer {token}',
                   'Content-Type': 'application/json; charset=utf-8'}
        try:
            if msg.reply_to:
                url = (f'{self._base}/open-apis/im/v1/messages/'
                       f'{msg.reply_to}/reply')
                reply = await self.http.post(url, headers=headers,
                                             json={'msg_type': 'text',
                                                   'content': content})
            else:
                url = f'{self._base}/open-apis/im/v1/messages'
                reply = await self.http.post(
                    url, headers=headers,
                    params={'receive_id_type': 'chat_id'},
                    json={'receive_id': chat_id, 'msg_type': 'text',
                          'content': content})
        except httpx.HTTPError:
            return SendResult(ok=False, error_code='unreachable', retryable=True)
        body = _body(reply)
        code = int(body.get('code', reply.status_code) or 0)
        if code == 0:
            data = body.get('data') or {}
            return SendResult(ok=True, message_id=str(data.get('message_id') or ''))
        if code in (99991663, 99991661, 99991664):
            # token 过期/无效：清缓存，交给出站队列重试一次。
            self._token = ''
            self._token_expires = 0
            return SendResult(ok=False, error_code='token_invalid', retryable=True)
        retryable = reply.status_code >= 500 or reply.status_code == 429 or code in (99991400,)
        return SendResult(ok=False, error_code=f'feishu_{code}',
                          retryable=retryable)

    async def _tenant_token(self, *, force: bool = False) -> str:
        now = time.time()
        if not force and self._token and now < self._token_expires - 300:
            return self._token
        response = await self.http.post(
            f'{self._base}/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': self.config.secret('app_id'),
                  'app_secret': self.config.secret('app_secret')})
        body = _body(response)
        code = body.get('code')
        if code is None or int(code) != 0 or not body.get('tenant_access_token'):
            raise TransportError(
                f'飞书鉴权失败：{body.get("msg") or body.get("code") or response.status_code}',
                code='auth_failed', retryable=False)
        self._token = str(body['tenant_access_token'])
        self._token_expires = now + float(body.get('expire') or 7200)
        return self._token

    # ---------- 回调解析（API 进程调用，无实例状态） ----------

    @classmethod
    def parse_webhook(cls, raw: bytes, headers, config: ChannelConfig) -> WebhookEvent:
        encrypt_key = config.secret('encrypt_key')
        verification = config.secret('verification_token')
        text = raw.decode('utf-8', errors='replace')
        if encrypt_key:
            if not verify_feishu_signature(headers, raw, encrypt_key):
                raise TransportError('飞书回调签名校验失败', code='bad_signature',
                                     retryable=False)
            try:
                envelope = json.loads(text or '{}')
                payload = json.loads(feishu_decrypt(str(envelope.get('encrypt') or ''),
                                                    encrypt_key))
            except (ValueError, TypeError) as exc:
                raise TransportError(f'飞书回调解密失败：{exc}', code='bad_encrypt',
                                     retryable=False) from None
        else:
            try:
                payload = json.loads(text or '{}')
            except ValueError:
                raise TransportError('飞书回调不是合法 JSON', code='bad_body',
                                     retryable=False) from None
            token = str(payload.get('token') or (payload.get('header') or {}).get('token') or '')
            if verification and token != verification:
                raise TransportError('飞书回调 token 不匹配', code='bad_token',
                                     retryable=False)
            if not verification:
                raise TransportError(
                    '飞书回调缺少校验方式：请配置 FEISHU_ENCRYPT_KEY 或 '
                    'FEISHU_VERIFICATION_TOKEN', code='not_configured',
                    retryable=False)

        if str(payload.get('type') or '') == 'url_verification':
            return WebhookEvent(kind='challenge',
                                challenge=str(payload.get('challenge') or ''))
        header = payload.get('header') or {}
        event_type = str(header.get('event_type') or payload.get('type') or '')
        if event_type != 'im.message.receive_v1':
            return WebhookEvent(kind='ignored', reason=f'event:{event_type or "unknown"}')
        event = payload.get('event') or {}
        return _to_inbound(event, config)


def _to_inbound(event: dict, config: ChannelConfig) -> WebhookEvent:
    message = event.get('message') or {}
    sender = event.get('sender') or {}
    sender_id = sender.get('sender_id') or {}
    if str(sender.get('sender_type') or '') == 'app':
        # 机器人自己/其他应用发的消息：不处理，否则两个 bot 会互相回声。
        return WebhookEvent(kind='ignored', reason='sender_is_app')
    user_id = str(sender_id.get('open_id') or sender_id.get('user_id') or '')
    chat_id = str(message.get('chat_id') or '')
    message_id = str(message.get('message_id') or '')
    if not user_id or not chat_id or not message_id:
        return WebhookEvent(kind='ignored', reason='incomplete')
    message_type = str(message.get('message_type') or '')
    mentions = message.get('mentions') or []
    text = feishu_extract_text(message_type, message.get('content'), mentions)
    chat_type = 'dm' if str(message.get('chat_type') or '') == 'p2p' else 'group'
    attachments: tuple[Attachment, ...] = ()
    note = _attachment_note(message_type)
    if note:
        text = f'{note}\n{text}'.strip()
    body = Inbound(
        platform='feishu',
        tenant=str(config.tenant_id or 'default'),
        chat_id=chat_id,
        chat_type=chat_type,  # type: ignore[arg-type]
        user_id=user_id,
        user_name=str((sender.get('sender_id') or {}).get('user_id') or '') or None,
        text=text,
        message_id=message_id,
        thread_id=str(message.get('thread_id') or '') or None,
        attachments=attachments,
        mentioned=bool(mentions),
        raw={},
    )
    if not body.text.strip() and not body.attachments:
        return WebhookEvent(kind='ignored', reason=f'type:{message_type}')
    return WebhookEvent(kind='message', inbound=body)


def _attachment_note(message_type: str) -> str:
    if message_type in ('image', 'file', 'audio', 'video'):
        return attachment_note({'audio': 'audio'}.get(message_type, message_type))
    return ''


def _body(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


