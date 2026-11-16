"""消息平台的领域模型：统一入站事件、统一出站消息、发送结果与能力声明。

这些是**契约**，不是某个渠道的实现细节。渠道适配器只负责把平台报文翻译成
`Inbound` / `Outbound`，Router 与 AgentBridge 只认这几个类型。改这里的字段
等于改所有渠道，必须先看 `docs/messaging-platforms.md` 的契约表。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ChatType = Literal['dm', 'group', 'channel']
AttachmentKind = Literal['image', 'file', 'audio', 'video']


@dataclass(frozen=True)
class Attachment:
    """一个附件。`url` 是平台临时地址，可能很快过期；`path` 是本地缓存。"""

    kind: AttachmentKind = 'file'
    url: str | None = None
    path: str | None = None
    name: str | None = None
    mime: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class Inbound:
    """归一化后的一条入站消息。

    `message_id` 是幂等键：平台重投同一条消息时它不变。没有稳定 id 的平台
    （部分 webhook）由适配器按内容算一个，宁可少收也不能重复执行。
    """

    platform: str
    tenant: str
    chat_id: str
    chat_type: ChatType
    user_id: str
    user_name: str | None
    text: str
    message_id: str
    thread_id: str | None = None
    attachments: tuple[Attachment, ...] = ()
    mentioned: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass
class Outbound:
    """要发出去的一条消息。渠道按自己的 `Capabilities` 降级。"""

    text: str | None = None
    card: dict | None = None
    reply_to: str | None = None
    attachments: tuple[Attachment, ...] = ()


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    error_code: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class Capabilities:
    """**必须如实声明**。Router 按这里的值降级，撒谎只会让用户看到空消息。"""

    card: bool = False
    stream_edit: bool = False
    typing: bool = False
    thread: bool = False
    reaction: bool = False
    group: bool = True
    max_text_chars: int = 2000


def inbound_to_payload(event: Inbound) -> dict:
    """入库用的紧凑表示。`raw` 不落库：平台原始报文可能很大，也不该长期留存。"""
    return {
        'platform': event.platform,
        'tenant': event.tenant,
        'chat_id': event.chat_id,
        'chat_type': event.chat_type,
        'user_id': event.user_id,
        'user_name': event.user_name or '',
        'text': event.text,
        'message_id': event.message_id,
        'thread_id': event.thread_id or '',
        'mentioned': bool(event.mentioned),
        'attachments': [
            {'kind': item.kind, 'url': item.url, 'path': item.path,
             'name': item.name, 'mime': item.mime, 'size': item.size}
            for item in event.attachments
        ],
    }


def inbound_from_payload(payload: dict) -> Inbound:
    attachments = tuple(
        Attachment(kind=str(item.get('kind') or 'file'),
                   url=item.get('url'), path=item.get('path'),
                   name=item.get('name'), mime=item.get('mime'),
                   size=item.get('size'))
        for item in (payload.get('attachments') or []) if isinstance(item, dict)
    )
    return Inbound(
        platform=str(payload.get('platform') or ''),
        tenant=str(payload.get('tenant') or 'default'),
        chat_id=str(payload.get('chat_id') or ''),
        chat_type=str(payload.get('chat_type') or 'dm'),  # type: ignore[arg-type]
        user_id=str(payload.get('user_id') or ''),
        user_name=str(payload.get('user_name') or '') or None,
        text=str(payload.get('text') or ''),
        message_id=str(payload.get('message_id') or ''),
        thread_id=str(payload.get('thread_id') or '') or None,
        attachments=attachments,
        mentioned=bool(payload.get('mentioned')),
    )
