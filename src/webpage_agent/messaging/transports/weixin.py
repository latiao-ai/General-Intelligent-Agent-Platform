"""个人微信（腾讯 iLink Bot API）适配器：扫码登录 + getupdates 长轮询 + 回显 context_token。

这不是逆向协议，也不是 hook：走的是腾讯 iLink 的 bot 接口。必须如实说明能力边界：

- 身份是 **bot**（`xxx@im.bot`），不是本人微信号；
- 普通微信群基本收不到事件，所以 `group=False`；
- 会话会过期（`errcode=-14`）：过期后状态置 `fatal`，要求重新扫码，**不自动
  重试风暴**；
- 主动发消息必须回显该会话最近的 `context_token`；没有 token 时 iLink 可能接受
  一次降级发送，但仍要求对方先给 bot 发过消息。

登录态（token / account_id / base_url）由 `weixin_login.py` 扫码后写进
`im_login_state`，Worker 每次启动从这里读。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import uuid

import httpx

from webpage_agent.messaging.base import ChannelConfig, Transport, TransportError
from webpage_agent.messaging.models import (Attachment, Capabilities, Inbound,
                                            Outbound, SendResult)
from webpage_agent.messaging.normalize import (attachment_note, chunk_text,
                                               normalize_text)

logger = logging.getLogger(__name__)

ILINK_BASE = 'https://ilinkai.weixin.qq.com'
CDN_BASE = 'https://novac2c.cdn.weixin.qq.com/c2c'
APP_ID = 'bot'
CHANNEL_VERSION = '2.2.0'
CLIENT_VERSION = (2 << 16) | (2 << 8)
EP_UPDATES = 'ilink/bot/getupdates'
EP_SEND = 'ilink/bot/sendmessage'
EP_TYPING = 'ilink/bot/sendtyping'
EP_CONFIG = 'ilink/bot/getconfig'
LONG_POLL_TIMEOUT = 35.0
API_TIMEOUT = 15.0
MAX_TEXT = 2000
MAX_FAILURES = 8
SESSION_EXPIRED = -14
RATE_LIMITED = -2
STALE_ERRMSG = {'unknown error', 'prepare failed'}


def _headers(token: str = '', body_len: int = 0) -> dict[str, str]:
    """iLink 的鉴权头。`X-WECHAT-UIN` 是随机 32 位整数的 base64，不是身份。"""
    uin = base64.b64encode(
        str(int.from_bytes(secrets.token_bytes(4), 'big')).encode('utf-8')
    ).decode('ascii')
    headers = {
        'Content-Type': 'application/json',
        'AuthorizationType': 'ilink_bot_token',
        'X-WECHAT-UIN': uin,
        'iLink-App-Id': APP_ID,
        'iLink-App-ClientVersion': str(CLIENT_VERSION),
    }
    if body_len:
        headers['Content-Length'] = str(body_len)
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def _is_expired(payload: dict, ret, errcode) -> bool:
    if SESSION_EXPIRED in (ret, errcode):
        return True
    errmsg = str(payload.get('errmsg') or payload.get('msg') or '').lower()
    return (ret == RATE_LIMITED or errcode == RATE_LIMITED) and errmsg in STALE_ERRMSG


def _extract_text(item_list) -> tuple[str, tuple[Attachment, ...]]:
    parts: list[str] = []
    attachments: list[Attachment] = []
    for item in item_list or []:
        if not isinstance(item, dict):
            continue
        kind = int(item.get('type') or 0)
        if kind == 1:
            parts.append(str((item.get('text_item') or {}).get('text') or ''))
        elif kind == 2:
            parts.append(attachment_note('image'))
            attachments.append(Attachment(kind='image'))
        elif kind == 3:
            parts.append(attachment_note('audio'))
            attachments.append(Attachment(kind='audio'))
        elif kind == 4:
            name = str((item.get('file_item') or {}).get('file_name') or '')
            parts.append(attachment_note('file', name or None))
            attachments.append(Attachment(kind='file', name=name or None))
        elif kind == 5:
            parts.append(attachment_note('video'))
            attachments.append(Attachment(kind='video'))
    return '\n'.join(part for part in parts if part).strip(), tuple(attachments)


class WeixinTransport(Transport):
    name = 'weixin'
    pulls_inbound = True

    def __init__(self, config: ChannelConfig, **kwargs) -> None:
        super().__init__(config, **kwargs)
        self._base = (config.secret('base_url')
                      or config.option('base_url') or ILINK_BASE).rstrip('/')
        self._cdn = (config.secret('cdn_base_url')
                     or config.option('cdn_base_url') or CDN_BASE).rstrip('/')
        self._account = config.secret('account_id')
        self._token = config.secret('token')
        self._task: asyncio.Task | None = None
        self._running = False
        self._sync_buf = ''
        self._context: dict[str, str] = {}
        self._typing: dict[str, str] = {}
        self._failures = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(card=False, stream_edit=False, typing=True,
                            thread=False, reaction=False, group=False,
                            max_text_chars=MAX_TEXT)

    def alive(self) -> bool:
        return bool(self._running and self._task is not None
                    and not self._task.done())

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if not self._token:
            raise TransportError('个人微信尚未登录：请先扫码',
                                 code='not_logged_in', retryable=False)
        if not self._account:
            raise TransportError('登录态缺少 account_id，请重新扫码',
                                 code='not_logged_in', retryable=False)
        self._running = True
        self.report('connecting', '正在连接 iLink')
        self._task = asyncio.create_task(self._poll_loop(), name='im:weixin:poll')

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.close_http()

    # ---------- 入站 ----------

    async def _poll_loop(self) -> None:
        backoff = 2.0
        while self._running:
            try:
                payload = await self._get_updates()
            except (asyncio.TimeoutError, httpx.TimeoutException):
                # 长轮询按设计会超时：不是错误，立刻再来一轮。
                continue
            except asyncio.CancelledError:
                return
            except Exception as exc:                # noqa: BLE001
                self._failures += 1
                self.report('retrying', f'iLink 不可达：{type(exc).__name__}',
                            'unreachable')
                if self._failures >= MAX_FAILURES:
                    self.report('fatal', 'iLink 连续失败，已停止重试；请检查网络后重启 Worker',
                                'unreachable')
                    return
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 1.8)
                continue

            ret = payload.get('ret')
            errcode = payload.get('errcode')
            if _is_expired(payload, ret, errcode):
                self.report('fatal',
                            'iLink 会话已过期（errcode=-14），请重新扫码登录',
                            'session_expired')
                return
            if ret not in (0, None) or errcode not in (0, None):
                self._failures += 1
                self.report('retrying',
                            f'iLink 返回 ret={ret} errcode={errcode} '
                            f'{payload.get("errmsg") or ""}'.strip(),
                            'upstream_error')
                if self._failures >= MAX_FAILURES:
                    self.report('fatal', 'iLink 连续返回错误，已停止重试',
                                'upstream_error')
                    return
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 1.8)
                continue

            self._failures = 0
            backoff = 2.0
            if self._running:
                self.report('connected', 'iLink 长轮询中')
            for message in payload.get('msgs') or []:
                try:
                    await self._handle_message(message)
                except Exception:                   # noqa: BLE001
                    logger.exception('处理个人微信消息失败')
            new_buf = str(payload.get('get_updates_buf') or '')
            if new_buf and new_buf != self._sync_buf:
                self._sync_buf = new_buf
            suggested = payload.get('longpolling_timeout_ms')
            _ = suggested  # 目前固定 35s；平台建议值只用于观测

    async def _get_updates(self) -> dict:
        body = json.dumps({'get_updates_buf': self._sync_buf,
                           'base_info': {'channel_version': CHANNEL_VERSION}},
                          ensure_ascii=False, separators=(',', ':'))
        response = await self.http.post(
            f'{self._base}/{EP_UPDATES}', content=body,
            headers=_headers(self._token, len(body.encode('utf-8'))),
            timeout=LONG_POLL_TIMEOUT + 5)
        if response.status_code >= 400:
            raise RuntimeError(f'iLink HTTP {response.status_code}')
        return response.json() if response.content else {}

    async def _handle_message(self, message: dict) -> None:
        sender = str(message.get('from_user_id') or '').strip()
        if not sender or sender == self._account:
            return
        context_token = str(message.get('context_token') or '').strip()
        if context_token:
            self._context[sender] = context_token
        item_list = message.get('item_list') or []
        text, attachments = _extract_text(item_list)
        if not text and not attachments:
            return
        room_id = str(message.get('room_id') or message.get('chat_room_id') or '')
        chat_type = 'group' if room_id else 'dm'
        chat_id = room_id or sender
        message_id = str(message.get('message_id') or '')
        if not message_id:
            message_id = 'sha256:' + hashlib.sha256(
                f'{sender}|{message.get("create_time") or ""}|{text}'.encode()
            ).hexdigest()[:24]
        event = Inbound(
            platform='weixin',
            tenant=str(self.config.tenant_id or 'default'),
            chat_id=chat_id,
            chat_type=chat_type,  # type: ignore[arg-type]
            user_id=sender,
            user_name=sender,
            text=text,
            message_id=message_id,
            attachments=attachments,
            mentioned=True,
            raw={},
        )
        await self.on_inbound(event)
        if context_token:
            asyncio.create_task(self._typing_for(sender))

    # ---------- 出站 ----------

    async def send(self, chat_id: str, msg: Outbound) -> SendResult:
        text = normalize_text(msg.text or '')
        if not text:
            return SendResult(ok=False, error_code='empty_text', retryable=False)
        pieces = chunk_text(text, MAX_TEXT, marker=False)
        last_id = ''
        for piece in pieces:
            result = await self._send_chunk(chat_id, piece)
            if not result.ok:
                return result
            last_id = str(result.message_id or '')
        return SendResult(ok=True, message_id=last_id)

    async def _send_chunk(self, chat_id: str, text: str) -> SendResult:
        context_token = self._context.get(chat_id, '')
        client_id = uuid.uuid4().hex
        payload = {
            'msg': {
                'from_user_id': '',
                'to_user_id': chat_id,
                'client_id': client_id,
                'message_type': 2,
                'message_state': 2,
                'item_list': [{'type': 1, 'text_item': {'text': text}}],
            },
            'base_info': {'channel_version': CHANNEL_VERSION},
        }
        if context_token:
            payload['msg']['context_token'] = context_token
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        try:
            response = await self.http.post(
                f'{self._base}/{EP_SEND}', content=body,
                headers=_headers(self._token, len(body.encode('utf-8'))),
                timeout=API_TIMEOUT)
        except httpx.HTTPError:
            return SendResult(ok=False, error_code='unreachable', retryable=True)
        if response.status_code >= 500 or response.status_code == 429:
            return SendResult(ok=False, error_code='http_retryable',
                              retryable=True)
        if response.status_code >= 400:
            return SendResult(ok=False, error_code=f'http_{response.status_code}',
                              retryable=False)
        try:
            result = response.json()
        except ValueError:
            result = {}
        ret, errcode = result.get('ret'), result.get('errcode')
        if ret in (0, None) and errcode in (0, None):
            return SendResult(ok=True, message_id=client_id)
        if _is_expired(result, ret, errcode) and context_token:
            # 会话过期：清掉旧 token 再按"无 token"降级发一次（iLink 接受一次）。
            self._context.pop(chat_id, None)
            payload['msg'].pop('context_token', None)
            body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            try:
                response = await self.http.post(
                    f'{self._base}/{EP_SEND}', content=body,
                    headers=_headers(self._token, len(body.encode('utf-8'))),
                    timeout=API_TIMEOUT)
                result = response.json() if response.content else {}
            except (httpx.HTTPError, ValueError):
                return SendResult(ok=False, error_code='unreachable',
                                  retryable=True)
            ret, errcode = result.get('ret'), result.get('errcode')
            if ret in (0, None) and errcode in (0, None):
                return SendResult(ok=True, message_id=client_id)
        if _is_expired(result, ret, errcode):
            return SendResult(ok=False, error_code='session_expired',
                              retryable=False)
        if errcode == RATE_LIMITED:
            return SendResult(ok=False, error_code='rate_limited',
                              retryable=True)
        return SendResult(ok=False, error_code=f'ilink_{errcode}',
                          retryable=False)

    async def typing(self, chat_id: str) -> None:
        await self._typing_for(chat_id)

    async def _typing_for(self, chat_id: str) -> None:
        ticket = self._typing.get(chat_id)
        if not ticket:
            ticket = await self._fetch_ticket(chat_id)
            if not ticket:
                return
        body = json.dumps({'ilink_user_id': chat_id, 'typing_ticket': ticket,
                           'status': 1}, ensure_ascii=False,
                          separators=(',', ':'))
        try:
            await self.http.post(
                f'{self._base}/{EP_TYPING}', content=body,
                headers=_headers(self._token, len(body.encode('utf-8'))),
                timeout=API_TIMEOUT)
        except httpx.HTTPError:
            pass

    async def _fetch_ticket(self, chat_id: str) -> str:
        body = json.dumps({
            'ilink_user_id': chat_id,
            'context_token': self._context.get(chat_id, ''),
            'base_info': {'channel_version': CHANNEL_VERSION},
        }, ensure_ascii=False, separators=(',', ':'))
        try:
            response = await self.http.post(
                f'{self._base}/{EP_CONFIG}', content=body,
                headers=_headers(self._token, len(body.encode('utf-8'))),
                timeout=API_TIMEOUT)
            payload = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError):
            return ''
        ticket = str(payload.get('typing_ticket') or '')
        if ticket:
            self._typing[chat_id] = ticket
        return ticket

