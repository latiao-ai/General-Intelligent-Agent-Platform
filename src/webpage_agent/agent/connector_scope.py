"""当前 run 允许使用的连接器集合。

`None` 表示“没有显式选择”，沿用旧行为（所有已配置 MCP 工具可见）；
空集合表示用户明确不选任何连接器，MCP 工具全部不注入。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_ALLOWED: ContextVar[tuple[str, ...] | None] = ContextVar(
    'run_connector_scope', default=None)
_SUBJECT: ContextVar[str | None] = ContextVar(
    'run_connector_subject', default=None)


def current() -> tuple[str, ...] | None:
    return _ALLOWED.get()


def current_subject() -> str | None:
    return _SUBJECT.get()


def use_subject(subject: str | None):
    return _SUBJECT.set(str(subject or '') or None)


def reset_subject(token) -> None:
    _SUBJECT.reset(token)


def use(ids: list[str] | tuple[str, ...] | None):
    if ids is None:
        return _ALLOWED.set(None)
    return _ALLOWED.set(tuple(str(item) for item in ids if str(item).strip()))


def reset(token) -> None:
    _ALLOWED.reset(token)


@contextmanager
def scope(ids: list[str] | tuple[str, ...] | None) -> Iterator[None]:
    token = use(ids)
    try:
        yield
    finally:
        reset(token)
