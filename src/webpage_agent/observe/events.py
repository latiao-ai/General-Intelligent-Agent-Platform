"""执行过程中的细粒度事件，供前端实时展示"智能体正在做什么"。

节点级进度（`stream_mode='updates'`）只告诉你"某个节点跑完了"；这里发的是节点
**内部**的动作与阶段起止，前者用来呈现思考过程，后者用来做实时计时。

事件出口（第四阶段统一）：

    事件有两个出口，按优先级：

    1. **作用域绑定的宿主 sink**（`bind_sink` / `emit` 优先使用）：图外的运行时
       （如 PiRuntime）在没有 LangGraph writer 的上下文里，也能把事件有序转交给
       宿主的异步回调。sink 用 `contextvars` 绑定到当前异步作用域，正常结束、异常
       或取消路径都必须可靠解绑，避免跨请求串流。
    2. **LangGraph stream writer**（无 sink 绑定时的原图行为）：在节点内部通过
       `get_stream_writer()` 发出 custom 事件，由 `LangGraphRuntime` 透传。

    没有绑定 sink 且不在 LangGraph 流式上下文里时（单元测试直接调用节点、或调用
    方没开 custom 流），静默跳过——节点代码不必关心自己被谁、以何种方式调用。
"""
from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

from langgraph.config import get_stream_writer

# 各阶段的中文名，CLI / Streamlit / API 三处共用。
STAGE_LABELS = {
    # 编排本身
    'intake': '接续运行',
    'plan': '规划',
    'persist_plan': '保存计划',
    'revise_plan': '按新要求调整',
    'finalize': '收尾检查',
    'observe_result': '观察结果',
    'decide_next': '决定下一步',
    'wait_async': '等待外部任务',
    'verify_completion': '核对完成条件',
    'run_step': '执行步骤',
    # 工具（与 capabilities/registry.py 里 Tool.label 对应）
    'web_search': '检索来源',
    'fetch_pages': '抓取正文',
    'render_sources_html': '资料转 HTML',
    'answer_with_sources': '联网作答',
    'answer_directly': '直接回答',
    'deploy_site': '部署站点',
    'manual_deploy': '手动部署',
    # 合成阶段：从服务端收到指令，到规划完成、真正开始调用工具之间的空档。
    # 它跨越了 plan 阶段，因此与"规划"的耗时会有重叠，这是刻意的：
    # 用户关心的是"我发出去之后多久才真正开始干活"。
    'instruction': '收到指令 → 开始执行',
}


def label_of(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage)


# 宿主 sink：接收一个事件 dict，可以是同步函数或协程。绑定到当前异步作用域，
# 供图外运行时（PiRuntime 等）复用 emit() 把事件交回宿主回调。
# 用 ContextVar 而非全局变量，保证不同请求/执行之间相互隔离、不会串流。
Sink = Callable[[dict], Optional[Awaitable[None]]]
_sink: ContextVar[Optional[Sink]] = ContextVar('pi_event_sink', default=None)


def bind_sink(sink: Optional[Sink]):
    """在当前异步作用域内绑定宿主 sink，返回一个用于解绑的 token。

    用法（异步上下文里）：

        token = bind_sink(on_event)
        try:
            ... 驱动执行 ...
        finally:
            unbind_sink(token)

    正常结束、异常或取消路径都必须解绑，避免事件串到别的请求。
    """
    return _sink.set(sink)


def unbind_sink(token) -> None:
    """解除 sink 绑定，恢复之前的作用域值。幂等。"""
    _sink.reset(token)


def emit(event: dict) -> None:
    """发出一个细粒度事件。

    优先走**作用域绑定的宿主 sink**（图外运行时也能交回宿主）；没有 sink 时
    回退到 LangGraph stream writer（原图行为）。两者都没有则静默跳过。

    注意：`emit` 是**同步**函数，sink 若返回协程会在发射方被调度（fire-and-
    forget 语义上由调用方负责 await）。节点内部的事件量很小，这一约定与原
    LangGraph writer 的同步调用保持一致。
    """
    sink = _sink.get()
    if sink is not None:
        result = sink(event)
        # sink 可能返回协程；这里不 await（保持同步语义），由调用方决定是否
        # 需要等待。若需要严格有序，运行时应用 await 版的 emit（见 _emit_async）。
        if result is not None and hasattr(result, '__await__'):
            # 交给事件循环调度；错误会经 asyncio 上报，不静默吞掉。
            import asyncio
            asyncio.ensure_future(result)
        return
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    if writer is None:
        return
    writer(event)


async def emit_async(event: dict) -> None:
    """异步发出事件：sink 为协程时严格 await，保证有序转交。

    运行时若要求事件严格按序到达宿主（例如 PiRuntime 的 text_delta），应使用
    这个版本而非 emit()。
    """
    sink = _sink.get()
    if sink is not None:
        result = sink(event)
        if result is not None and hasattr(result, '__await__'):
            await result
        return
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    if writer is None:
        return
    writer(event)


def action(label: str, detail: str = '', icon: str = '') -> dict:
    """一个可展示的动作，如「已搜索网页 · 还珠格格 第一部 第3集」。"""
    return {'type': 'action', 'icon': icon, 'label': label, 'detail': detail}


def thought(text: str) -> dict:
    """智能体的计划陈述，如「我先帮你查找…」。"""
    return {'type': 'thought', 'text': text}


def draft(report: dict | None) -> dict:
    """整理报告过程中的"半成品"报告，或 None 表示清空。

    这类事件体积大（每份都是一整份报告的快照）且是过程性的，
    与其他事件一样先写 PostgreSQL，再通过 NOTIFY 唤醒各个 API 副本。
    """
    return {'type': 'draft', 'report': report}


def answer_delta(text: str) -> dict:
    """直接回答时的增量文本（打字机效果）。

    增量也写入 PostgreSQL；跨进程客户端和断线重连按 seq 补读与去重。
    """
    return {'type': 'answer_delta', 'text': text}


def stage_start(stage: str, label: str | None = None) -> dict:
    """阶段开始。

    带 `at`（epoch 秒）是因为前端要按"现在 - 开始时刻"实时累加：
    事件到达前端可能被轮询或网络延迟几十到几百毫秒，
    用本地收到时刻计时会偏短，用服务端开始时刻才准。

    label 可显式传入：MCP 工具是运行时发现的，中文名写不进 STAGE_LABELS。
    """
    return {'type': 'stage_start', 'stage': stage,
            'label': label or label_of(stage), 'at': time.time()}


def stage_end(stage: str, seconds: float, status: str = '',
              label: str | None = None) -> dict:
    """阶段结束，带该阶段实际耗时。"""
    return {'type': 'stage_end', 'stage': stage,
            'label': label or label_of(stage),
            'seconds': round(seconds, 2), 'status': status, 'at': time.time()}
