"""会话级选择的执行期作用域。

`api_hub` 里解析出来的 addons（专家人设 + 技能索引 + 模型）要交给 `run_task`，
但 `run_task` 的签名被大量测试替身按位置/关键字固定过——加一个 `addons=` 关键字
会让那些替身全部 TypeError。所以这里照 `sandbox.use_workspace` 的做法用
ContextVar 传：调用方 `use()`，`run_task` 里 `current()` 取，结束后 `reset()`。

作用域是"每轮执行"，不是进程级：同一进程并发跑两个会话时互不影响。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_CURRENT: ContextVar[dict] = ContextVar('session_addons', default={})


def current() -> dict:
    data = _CURRENT.get()
    return data if isinstance(data, dict) else {}


def use(addons: dict | None):
    return _CURRENT.set(dict(addons) if isinstance(addons, dict) else {})


def reset(token) -> None:
    _CURRENT.reset(token)


@contextmanager
def scope(addons: dict | None) -> Iterator[None]:
    token = use(addons)
    try:
        yield
    finally:
        reset(token)
