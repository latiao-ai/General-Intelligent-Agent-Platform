"""出站 webhook 投递：企业微信群机器人 / 飞书自定义机器人 / 通用 JSON。

只做一件事：把一段纯文本发到一个用户配好的地址，并把"失败"如实抛出去。
**不把 5xx、非 2xx、解析失败当成功**——投递记录的意义就在于失败能查。

渠道差异只在报文形状与成功判定：

| channel | 请求体 | 成功判定 |
| --- | --- | --- |
| `wecom` | `{"msgtype":"markdown","markdown":{"content":...}}` | 2xx 且 `errcode == 0` |
| `feishu` | `{"msg_type":"text","content":{"text":...}}` | 2xx 且 `code`/`StatusCode` 为 0（或没有该字段） |
| `webhook` | `{"task":..., "status":..., "summary":..., ...}` | 2xx |

`webhook` 是给自建接收端用的，形状稳定、字段少，方便用户自己接。
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import httpx

from webpage_agent.core.config import settings

CHANNELS = ('wecom', 'feishu', 'webhook')
# 企业微信/飞书都把业务错误放在 200 的 body 里，只看状态码会把"发失败了"当成功。
_LOOPBACK = {'127.0.0.1', 'localhost', '::1'}


class DeliveryError(RuntimeError):
    """投递失败。`code` 进投递记录，`retryable` 决定 Worker 要不要退避重试。"""

    def __init__(self, message: str, *, code: str = 'delivery_failed',
                 status: int = 502, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


def validate_url(url: str) -> str:
    """只接受 https；本机回环允许 http（本地联调）。

    这个检查放在**保存任务时**也放在**投递时**：任务行里的地址可能被后来改过，
    也可能来自早期没校验的写入。
    """
    text = (url or '').strip()
    if not text:
        raise DeliveryError('推送地址为空', code='invalid_url', status=400,
                            retryable=False)
    parts = urlsplit(text)
    if parts.scheme not in ('http', 'https'):
        raise DeliveryError('推送地址只支持 http/https', code='invalid_url',
                            status=400, retryable=False)
    host = (parts.hostname or '').lower()
    if parts.scheme == 'http' and host not in _LOOPBACK:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise DeliveryError('非本机地址必须用 https', code='invalid_url',
                                status=400, retryable=False) from None
        if not address.is_loopback:
            raise DeliveryError('非本机地址必须用 https', code='invalid_url',
                                status=400, retryable=False)
    if not host:
        raise DeliveryError('推送地址缺少主机名', code='invalid_url', status=400,
                            retryable=False)
    return text


def build_payload(channel: str, *, name: str, status_text: str, summary: str,
                  occurred_at: str = '', run_id: str = '', task_url: str = '') -> dict:
    """按渠道拼报文。`summary` 已经是人类可读的一段话，这里不做二次加工。"""
    lines = [f'**{name}**', status_text]
    if summary:
        lines.append(summary)
    if occurred_at:
        lines.append(f'（{occurred_at}）')
    content = '\n\n'.join(lines).strip()
    if channel == 'wecom':
        return {'msgtype': 'markdown', 'markdown': {'content': content}}
    if channel == 'feishu':
        text = content.replace('**', '')
        return {'msg_type': 'text', 'content': {'text': text}}
    return {'task': name, 'status': status_text, 'summary': summary,
            'occurred_at': occurred_at, 'run_id': run_id,
            'url': task_url}


def _wecom_ok(body: object) -> bool:
    if not isinstance(body, dict):
        return False
    return int(body.get('errcode', 0) or 0) == 0


def _feishu_ok(body: object) -> bool:
    if not isinstance(body, dict):
        return True            # 自定义机器人成功时可能回空 body
    for key in ('code', 'StatusCode', 'status_code'):
        if key in body:
            try:
                return int(body[key] or 0) == 0
            except (TypeError, ValueError):
                return False
    return True


async def post_webhook(url: str, payload: dict, *, channel: str = 'webhook',
                       timeout: float | None = None,
                       transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """发一次。成功返回响应体（可能为空字典），失败抛 `DeliveryError`。

    `transport` 只给测试注入用（httpx.MockTransport），生产不传。
    """
    target = validate_url(url)
    limit = settings.webhook_timeout_seconds if timeout is None else timeout
    try:
        async with httpx.AsyncClient(
                timeout=limit, follow_redirects=False,
                trust_env=settings.webhook_trust_env,
                transport=transport) as client:
            reply = await client.post(target, json=payload)
    except httpx.HTTPError as exc:
        # 连接超时/中断：不知道对方收没收到，按可重试处理。
        raise DeliveryError(f'推送地址不可达：{type(exc).__name__}',
                            code='unreachable') from None
    if reply.status_code >= 500:
        raise DeliveryError(f'推送端返回 {reply.status_code}', code='http_5xx')
    if reply.status_code >= 400:
        # 4xx 是地址/凭证/格式问题，重试不会变好。
        raise DeliveryError(f'推送端拒绝请求（{reply.status_code}）',
                            code='http_4xx', status=502, retryable=False)
    try:
        body: object = reply.json()
    except ValueError:
        body = {}
    if channel == 'wecom' and not _wecom_ok(body):
        raise DeliveryError(f'企业微信返回错误：{body}', code='wecom_error')
    if channel == 'feishu' and not _feishu_ok(body):
        raise DeliveryError(f'飞书返回错误：{body}', code='feishu_error')
    return body if isinstance(body, dict) else {}
