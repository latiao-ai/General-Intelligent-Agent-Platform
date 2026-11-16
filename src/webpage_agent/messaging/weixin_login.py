"""个人微信扫码登录（iLink Bot）：二维码获取 + 状态轮询 + 二维码渲染。

登录在 **API 进程**里跑（只是一串 HTTP 请求），登录成功后把 token 写进
`im_login_state`；Worker 下一次同步时读到 token 就自动建连。这样扫码与消息网关
互不依赖：API 重启最多丢"正在扫的这张码"，不会丢已经登录的凭据。

二维码渲染不引额外依赖：vendored 的 `qrcodegen` 生成矩阵，这里拼成 SVG data URI。
iLink 返回的 `qrcode_img_content` 是微信 liteapp 链接（要拿微信扫），不是图片，
所以必须自己渲染。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field

import httpx

from webpage_agent.messaging.transports.weixin import (APP_ID, CLIENT_VERSION,
                                                       ILINK_BASE)
from webpage_agent.messaging.vendor import qrcodegen

logger = logging.getLogger(__name__)

EP_GET_QR = 'ilink/bot/get_bot_qrcode'
EP_QR_STATUS = 'ilink/bot/get_qrcode_status'
QR_TIMEOUT = 35.0
LOGIN_TTL_SECONDS = 480
MAX_REFRESH = 3


@dataclass
class LoginSession:
    tenant_id: str
    owner_user_id: str
    qrcode: str = ''
    qr_url: str = ''
    qr_svg: str = ''
    status: str = 'starting'       # starting|wait|scaned|confirmed|expired|error|cancelled
    message: str = ''
    account_id: str = ''
    base_url: str = ILINK_BASE
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = field(default=None, compare=False)

    def view(self) -> dict:
        return {
            'status': self.status,
            'message': self.message,
            'qrcode': self.qrcode,
            'qr_url': self.qr_url,
            'qr_svg': self.qr_svg,
            'account_id': self.account_id,
            'base_url': self.base_url,
            'expires_in': max(0, int(self.created_at + LOGIN_TTL_SECONDS
                                     - time.time())),
        }


class LoginManager:
    """进程内登录会话。按 `(tenant, owner)` 隔离，绝不串账号。"""

    def __init__(self, http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._sessions: dict[str, LoginSession] = {}
        self._http_transport = http_transport
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(tenant_id: str, owner_user_id: str) -> str:
        return f'{tenant_id}:{owner_user_id}'

    def client(self, timeout: float = QR_TIMEOUT) -> httpx.AsyncClient:
        kwargs: dict = {'timeout': timeout, 'follow_redirects': False}
        if self._http_transport is not None:
            kwargs['transport'] = self._http_transport
        return httpx.AsyncClient(**kwargs)

    async def start(self, tenant_id: str, owner_user_id: str,
                    persist=None) -> LoginSession:
        async with self._lock:
            key = self._key(tenant_id, owner_user_id)
            old = self._sessions.get(key)
            if old and old.task and not old.task.done():
                old.task.cancel()
            session = LoginSession(tenant_id=tenant_id, owner_user_id=owner_user_id)
            self._sessions[key] = session
            try:
                qrcode_value, qr_url = await self._fetch_qr()
            except Exception as exc:                # noqa: BLE001
                session.status = 'error'
                session.message = f'获取二维码失败：{type(exc).__name__}'
                return session
            session.qrcode = qrcode_value
            session.qr_url = qr_url
            session.qr_svg = qr_data_uri(qr_url or qrcode_value)
            session.status = 'wait'
            session.message = '请用微信扫码'
            session.task = asyncio.create_task(
                self._poll(session, persist), name='im:weixin:login')
            return session

    def get(self, tenant_id: str, owner_user_id: str) -> LoginSession | None:
        return self._sessions.get(self._key(tenant_id, owner_user_id))

    async def cancel(self, tenant_id: str, owner_user_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(self._key(tenant_id, owner_user_id), None)
            if session and session.task and not session.task.done():
                session.task.cancel()

    async def _fetch_qr(self) -> tuple[str, str]:
        async with self.client() as client:
            response = await client.get(
                f'{ILINK_BASE}/{EP_GET_QR}',
                params={'bot_type': '3'},
                headers={'iLink-App-Id': APP_ID,
                         'iLink-App-ClientVersion': str(CLIENT_VERSION)})
            response.raise_for_status()
            payload = response.json()
        qrcode_value = str(payload.get('qrcode') or '')
        qr_url = str(payload.get('qrcode_img_content') or '')
        if not qrcode_value:
            raise RuntimeError('iLink 未返回 qrcode')
        return qrcode_value, qr_url

    async def _poll(self, session: LoginSession, persist=None) -> None:
        base_url = session.base_url
        refresh_count = 0
        while time.time() < session.created_at + LOGIN_TTL_SECONDS:
            try:
                async with self.client() as client:
                    response = await client.get(
                        f'{base_url}/{EP_QR_STATUS}',
                        params={'qrcode': session.qrcode},
                        headers={'iLink-App-Id': APP_ID,
                                 'iLink-App-ClientVersion': str(CLIENT_VERSION)})
                    response.raise_for_status()
                    payload = response.json()
            except asyncio.CancelledError:
                return
            except Exception:                       # noqa: BLE001
                await asyncio.sleep(1.5)
                continue
            status = str(payload.get('status') or 'wait')
            if status == 'wait':
                session.status, session.message = 'wait', '等待扫码'
            elif status == 'scaned':
                session.status, session.message = 'scaned', '已扫码，请在微信里确认'
            elif status == 'scaned_but_redirect':
                host = str(payload.get('redirect_host') or '')
                if host:
                    base_url = f'https://{host}'
                    session.base_url = base_url
                session.status, session.message = 'scaned', '已扫码，正在跳转'
            elif status == 'expired':
                refresh_count += 1
                if refresh_count > MAX_REFRESH:
                    session.status, session.message = 'expired', '二维码多次过期，请重新发起登录'
                    return
                try:
                    qrcode_value, qr_url = await self._fetch_qr()
                except Exception:                   # noqa: BLE001
                    session.status, session.message = 'error', '刷新二维码失败'
                    return
                session.qrcode, session.qr_url = qrcode_value, qr_url
                session.qr_svg = qr_data_uri(qr_url or qrcode_value)
                session.status, session.message = 'wait', '二维码已刷新，请重新扫码'
            elif status == 'confirmed':
                account_id = str(payload.get('ilink_bot_id') or '')
                token = str(payload.get('bot_token') or '')
                if not account_id or not token:
                    session.status, session.message = 'error', '登录确认但凭据不完整'
                    return
                session.account_id = account_id
                session.base_url = str(payload.get('baseurl') or base_url)
                session.status = 'confirmed'
                session.message = '登录成功'
                if persist is not None:
                    try:
                        await persist(verify_login_payload(payload))
                    except Exception:               # noqa: BLE001
                        logger.warning('保存个人微信登录态失败', exc_info=True)
                        session.status, session.message = 'error', '登录成功但保存凭据失败'
                return
            await asyncio.sleep(1.0)
        session.status, session.message = 'expired', '登录超时，请重新发起'


def qr_data_uri(text: str) -> str:
    """把文本渲染成 SVG 二维码（data URI）。空文本返回空串。"""
    content = str(text or '').strip()
    if not content:
        return ''
    qr = qrcodegen.QrCode.encode_text(content, qrcodegen.QrCode.Ecc.MEDIUM)
    border = 2
    size = qr.get_size() + border * 2
    parts = []
    for y in range(qr.get_size()):
        for x in range(qr.get_size()):
            if qr.get_module(x, y):
                parts.append(f'M{x + border} {y + border}h1v1h-1z')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        'shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path d="{"".join(parts)}" fill="#111"/></svg>'
    )
    encoded = base64.b64encode(svg.encode('utf-8')).decode('ascii')
    return f'data:image/svg+xml;base64,{encoded}'


def verify_login_payload(payload: dict) -> dict:
    """把 confirmed 响应整理成要落库的登录态（不写日志、不回显 token）。"""
    return {
        'token': str(payload.get('bot_token') or ''),
        'account_id': str(payload.get('ilink_bot_id') or ''),
        'user_id': str(payload.get('ilink_user_id') or ''),
        'base_url': str(payload.get('baseurl') or ILINK_BASE),
        'phase': 'confirmed',
        'saved_at': int(time.time()),
    }

