"""来源正文的落盘与取回。

**为什么单独存**：正文是上下文里最占地方的东西。实测单个任务的 `state.json`
能到 635 KB，其中 42%~83% 是 `documents[*].raw_content`；而 `state` 每过一个
节点就全量重写一次，多轮承接时还会把整份正文复制进下一个任务。

这里把正文挪到 `artifacts/<task_id>/sources/<source_id>.txt`，`state` 里只留
元数据（id / 标题 / url / 字数 / 状态）。需要正文时按 (task_id, source_id) 读回——
文档记录自带 `task_id`，所以上一轮的来源也能直接取，不用复制。
"""
import re
from pathlib import Path

from app.config import settings

_SAFE = re.compile(r'[^A-Za-z0-9_-]')


def _safe(name: str) -> str:
    """task_id / source_id 都是本代码生成的，这里仍做一次白名单过滤，
    避免任何情况下拼出目录穿越路径。"""
    return _SAFE.sub('', str(name or ''))[:64]


def folder(task_id: str) -> Path:
    return settings.artifact_dir / _safe(task_id) / 'sources'


def text_path(task_id: str, source_id: str) -> Path:
    return folder(task_id) / f'{_safe(source_id)}.txt'


def save_text(task_id: str, source_id: str, text: str) -> int:
    """存正文，返回字数。空正文不落盘（返回 0），避免留一堆空文件。"""
    body = (text or '').strip()
    if not body:
        return 0
    path = text_path(task_id, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    return len(body)


def load_text(document: dict) -> str:
    """按文档记录取回正文；兼容还带 raw_content 的旧数据。"""
    legacy = document.get('raw_content')
    if legacy:
        return legacy
    task_id = document.get('task_id')
    source_id = document.get('source_id')
    if not task_id or not source_id:
        return ''
    path = text_path(task_id, source_id)
    try:
        return path.read_text(encoding='utf-8') if path.exists() else ''
    except OSError:
        return ''


def chars_of(document: dict) -> int:
    """这份来源有多少正文字符。0 表示还没有正文。

    只认"有没有正文"，不认 status：检索接口经常直接带回正文，
    此时状态仍是 pending，但内容完全可用。
    """
    if document.get('status') == 'failed':
        return 0
    if document.get('raw_content'):
        return len(document['raw_content'])
    recorded = document.get('chars')
    if recorded:
        return int(recorded)
    return len(load_text(document))


def has_text(document: dict) -> bool:
    return chars_of(document) > 0


def metadata(task_id: str, source_id: str, *, url: str, title: str,
             snippet: str = '', text: str = '', status: str | None = None,
             fetched_at: str | None = None) -> dict:
    """拼一条只含元数据的文档记录；正文写盘，不留在 state 里。"""
    chars = save_text(task_id, source_id, text)
    if status is None:
        status = 'success' if chars else 'pending'
    return {
        'source_id': source_id,
        'task_id': task_id,          # 正文存放在哪个任务的目录下
        'url': url,
        'title': title,
        'snippet': (snippet or '')[:400],
        'chars': chars,
        'status': status,
        'fetched_at': fetched_at,
        'error': None,
    }

