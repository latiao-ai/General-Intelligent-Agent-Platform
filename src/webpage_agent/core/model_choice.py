"""按用户记住模型选择。进程级 `LLM_MODEL` 仍是默认值，一个人切换不影响其他人。"""
from __future__ import annotations

import contextvars
import json
import re
from pathlib import Path

from webpage_agent.core.config import settings

_CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar(
    'user_llm_model', default='')

_SAFE = re.compile(r'[^A-Za-z0-9_.-]')


def use(model_id: str) -> contextvars.Token:
    return _CURRENT.set(str(model_id or ''))


def reset(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def active(fallback: str = '') -> str:
    """当前上下文里的用户模型。没有绑定时用调用方给出的进程默认值。"""
    chosen = _CURRENT.get()
    return chosen or fallback


def _safe(user_id: str) -> str:
    return _SAFE.sub('_', str(user_id or 'default'))[:64] or 'default'


def _dir() -> Path:
    return Path(settings.profile_path).parent / 'models'


def user_path(user_id: str) -> Path:
    return _dir() / f'{_safe(user_id)}.json'


def legacy_path() -> Path:
    return Path(settings.profile_path).parent / 'model.json'


def _read(path: Path) -> str:
    try:
        saved = json.loads(path.read_text(encoding='utf-8')).get('id')
    except (OSError, ValueError, AttributeError):
        return ''
    return str(saved or '').strip()


def load_id(user_id: str) -> str:
    saved = _read(user_path(user_id))
    if saved:
        return saved
    # 旧的全局文件只还给本地单用户，不能当成所有人的选择。
    if _safe(user_id) == 'default':
        return _read(legacy_path())
    return ''


def save(user_id: str, model_id: str) -> None:
    path = user_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'id': model_id}, ensure_ascii=False),
                    encoding='utf-8')


def bind(user_id: str, fallback: str, allowed: set[str] | None = None
         ) -> contextvars.Token:
    saved = load_id(user_id)
    if allowed is not None and saved not in allowed:
        saved = ''
    return use(saved or fallback)
