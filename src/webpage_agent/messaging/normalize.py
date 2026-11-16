"""归一化：富文本 → 纯文本、长消息按语义切分、附件摘要。

这里只做与平台 SDK 无关的纯函数，方便单测。平台报文的字段差异在各
`transports/*.py` 里处理完再调这里。
"""
from __future__ import annotations

import json
import re

_AT_PLACEHOLDER = re.compile(r'@_user_\d+')
_EMPTY_LINES = re.compile(r'\n{3,}')
_MARKER_RESERVE = 12          # "（12/34）" 这类续接标记的预算


def _json_value(content) -> dict:
    if isinstance(content, dict):
        return content
    try:
        value = json.loads(content or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def feishu_extract_text(message_type: str, content, mentions=None) -> str:
    """把飞书的 `message_type` + `content`（JSON 字符串）转成纯文本。

    - `text`：直接取 `text`，把 `@_user_1` 占位替换成 `@名字`；
    - `post`：标题 + 段落；`a` 取 text，`at` 取名字，`img/media` 降级成占位符；
    - 其余类型（image/file/audio/video/sticker）：返回空串，由调用方补占位符。
    """
    body = _json_value(content)
    kind = str(message_type or '').lower()
    if kind == 'text':
        text = str(body.get('text') or '')
        for item in (mentions or []):
            key = str((item or {}).get('key') or '')
            name = str((item or {}).get('name') or '')
            if key and name:
                text = text.replace(key, f'@{name}')
        return _EMPTY_LINES.sub('\n\n', text).strip()
    if kind == 'post':
        parts: list[str] = []
        title = str(body.get('title') or '').strip()
        if title:
            parts.append(title)
        for paragraph in body.get('content') or []:
            if not isinstance(paragraph, list):
                continue
            line = ''.join(_post_element(item) for item in paragraph
                           if isinstance(item, dict))
            if line.strip():
                parts.append(line.strip())
        return _EMPTY_LINES.sub('\n\n', '\n'.join(parts)).strip()
    return ''


def _post_element(item: dict) -> str:
    tag = str(item.get('tag') or '')
    if tag == 'text':
        return str(item.get('text') or '')
    if tag == 'a':
        return str(item.get('text') or item.get('href') or '')
    if tag == 'at':
        name = str(item.get('user_name') or item.get('user_id') or '')
        return f'@{name}' if name else '@'
    if tag == 'img':
        return '[图片]'
    if tag in ('media', 'video'):
        return '[视频]'
    if tag == 'emotion':
        return '[表情]'
    if tag == 'code_block':
        return str(item.get('text') or '')
    return ''


def attachment_note(kind: str, name: str | None = None) -> str:
    label = {'image': '图片', 'file': '文件', 'audio': '语音',
             'video': '视频'}.get(kind, '附件')
    return f'[{label}：{name}]' if name else f'[{label}]'


def normalize_text(text: str) -> str:
    """去首尾空白、压缩三个以上空行。不要动单个换行（代码块靠它）。"""
    return _EMPTY_LINES.sub('\n\n', str(text or '')).strip()


def chunk_text(text: str, limit: int, *, marker: bool = True) -> list[str]:
    """按语义边界切分长消息。

    优先段落（`\\n\\n`）→ 换行 → 中文句号/问叹号 → 英文句点 → 空格 → 硬切。
    多段时每段末尾带 `（i/n）` 续接标记；单段不加，避免污染正常回复。
    """
    content = str(text or '').strip()
    if not content:
        return []
    budget = max(50, int(limit or 2000))
    if len(content) <= budget:
        return [content]
    effective = max(40, budget - (_MARKER_RESERVE if marker else 0))
    chunks: list[str] = []
    rest = content
    while len(rest) > effective:
        window = rest[:effective]
        cut = _best_cut(window)
        piece = rest[:cut].rstrip()
        if piece:
            chunks.append(piece)
        rest = rest[cut:].lstrip('\n')
    if rest.strip():
        chunks.append(rest.strip())
    if len(chunks) <= 1 or not marker:
        return chunks or [content]
    total = len(chunks)
    return [f'{piece}\n（{index}/{total}）' if piece else piece
            for index, piece in enumerate(chunks, start=1)]


def _best_cut(window: str) -> int:
    """在窗口内找最靠后的语义边界；找不到就硬切。"""
    floor = int(len(window) * 0.55)
    for token in ('\n\n', '\n', '。', '！', '？', '. ', '! ', '? ', '；', '; ', '，'):
        index = window.rfind(token)
        if index >= floor:
            return index + len(token)
    index = window.rfind(' ')
    if index >= floor:
        return index + 1
    return len(window)


def command_of(text: str) -> str:
    """解析 `/new` `/stop` `/help`；大小写不敏感，允许后面跟参数。"""
    first = str(text or '').strip().split(None, 1)
    if not first:
        return ''
    head = first[0].lower()
    return head[1:] if head.startswith('/') else ''


def command_arg(text: str) -> str:
    """命令后面的第一个参数：`/bind X助手` → `X助手`。

    只取一段（不按空格切全部）：机器人名字里带空格是允许的，`/bind 客服 Bot`
    应该整体当一个名字去查，而不是取前半截。
    """
    parts = str(text or '').strip().split(None, 1)
    if len(parts) < 2 or not parts[0].startswith('/'):
        return ''
    return parts[1].strip()


def user_label(user_id: str, user_name: str | None, *, platform: str) -> str:
    """给 Agent 的发言署名：名字优先，没有就脱敏 id。"""
    name = str(user_name or '').strip()
    if name:
        return name
    clean = str(user_id or '').strip()
    if not clean:
        return '对方'
    return f'{platform}-{clean[:6]}'
