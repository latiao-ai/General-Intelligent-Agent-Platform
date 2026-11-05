"""Pi 宿主工具调用对象：把「模型发起的工具调用」接到受治理的真实执行。

第五阶段：Pi 不再用假工具（echo/add），而是通过这里调用**唯一开放的真实工具**
`local:web:search`（web_search）。每一次调用都走 `tools/execution_gateway.py` 的
同一条治理链（授权 → 准入 → 撤权复查 → 真实调用 → 结算），并把搜索结果转成
模型可用的回执。

关键纪律：

- **调用身份由 Python 持有**：可信主体（租户/用户/run/租约/计划版本）来自
  库里已提交的 run 行，**不**从 Node 或模型输出取；桥接 `call_id` 只用于
  往返关联，Python 分配稳定执行身份（step_id / action_id / attempt_id），
  不假定模型生成的 ID 能用于重启恢复；
- **只开放 local:web:search**：执行点再次解析并检查工具，其它工具一律拒绝；
- **成功结算前不向 Pi 发成功回执**：结算失败就停止后续模型执行；
- **结果归一**：工具内部返回的 `status="searched"` 是阶段小结，不是业务终态；
  回执保留来源 ID / 标题 / URL / 限长正文摘录，正文经 sources 存储读取，
  不把整份 state 发给 Node；
- **增量合并**：真实调用成功后，把工具返回的认可增量（documents / usage /
  结果引用）合并回宿主 state，不让任意结果覆盖身份、租约等宿主字段。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from webpage_agent.capabilities import registry
from webpage_agent.core import sources
from webpage_agent.store import ledger as ledger_module
from webpage_agent.tools import execution_gateway

logger = logging.getLogger(__name__)

# 本阶段 Pi 唯一开放的真实工具（稳定键）。
ALLOWED_TOOL_KEY = 'local:web:search'

# 单条来源给模型的正文摘录上限（字符）。正文太长只摘开头，避免撑爆上下文。
SNIPPET_MAX_CHARS = 400

# 一次回执最多带回多少个来源。
MAX_SOURCES = 8

# 合并回宿主 state 时允许写入的**认可字段**（白名单）。工具返回的增量里只有这些
# 键会被合并，其余（status、phase 等内部字段）一律丢弃，避免任意结果覆盖身份、
# 租约、任务状态等宿主字段。
_MERGEABLE_KEYS = ('documents', 'usage', 'errors')


@dataclass
class ToolReceipt:
    """一次工具调用转给模型（Pi）的回执。

    `ok` 为 True 时 `content` 是结构化结果（来源列表）；为 False 时 `error`
    是原始错误正文（保留，不吞）。`updates` 是**已结算、可合并回宿主 state**
    的认可增量（成功时非空）。
    """
    ok: bool
    content: Any = None
    error: str = ''
    # 执行身份（Python 分配，用于账本/审计），不用于重启恢复的幂等键。
    action_id: str = ''
    attempt_id: str = ''
    step_id: str = ''
    # 已结算的认可增量（合并回 state 用）
    updates: dict | None = None


def _resolve_web_search():
    """执行点再次解析并检查工具：只认稳定键 local:web:search。"""
    for tool in registry.load():
        if tool.tool_key == ALLOWED_TOOL_KEY:
            return tool
    return None


def _source_excerpt(doc: dict) -> dict:
    """把一条来源元数据转成给模型的内容：ID / 标题 / URL / 限长正文摘录。"""
    body = sources.load_text(doc)
    snippet = (body or doc.get('snippet') or '')[:SNIPPET_MAX_CHARS]
    return {
        'source_id': str(doc.get('source_id') or ''),
        'title': str(doc.get('title') or ''),
        'url': str(doc.get('url') or ''),
        'excerpt': snippet,
        'chars': int(doc.get('chars') or 0),
    }


def _search_receipt(updates: dict) -> dict:
    """把 web_search 的 state 增量归一成给模型的内容。

    不把整份 state 发给 Node：只带来源 ID / 标题 / URL / 限长摘录。
    `status="searched"` 是阶段小结，这里原样保留为事实，由上层决定终态。

    `updates` 可以是真实调用后的增量，也可以是重放时从 receipt 恢复的完整
    内容（含 `documents`）。两者共用同一个 `documents` 字段。
    """
    documents = updates.get('documents') or []
    errors = updates.get('errors') or []
    result = {
        'tool': 'web_search',
        'status': str(updates.get('status') or 'searched'),
        'sources': [_source_excerpt(d) for d in documents[:MAX_SOURCES]
                    if isinstance(d, dict)],
        'total_sources': len(documents),
    }
    if errors:
        result['errors'] = [{'stage': e.get('stage'), 'type': e.get('type')}
                            for e in errors if isinstance(e, dict)]
    return result


def _stable_step_id(tool_key: str, args: dict) -> str:
    """由 Python 分配的稳定执行身份（第 4 点）。

    桥接 `call_id` 只用于一次通信往返关联，**不进账本幂等键**。这里的 step_id
    由 `tool_key + args 指纹` 确定性生成，使「同一轮重复发起的相同调用」命中
    账本的同一幂等键，复用已有动作，而不是分配新身份再调用一次。
    """
    return f'pi:{tool_key}:{ledger_module.args_hash(args)[:16]}'


def _mergeable_updates(updates: dict) -> dict:
    """从工具增量里挑出**认可**、可合并回宿主 state 的字段（白名单）。"""
    return {key: value for key, value in (updates or {}).items()
            if key in _MERGEABLE_KEYS}


class HostToolCaller:
    """Pi 宿主工具调用对象：受治理地执行模型发起的工具调用。

    一次 `run()` 独占一个 caller；它不持有凭据或身份，可信主体每次从当前
    执行上下文（账本 + 执行权）推导。
    """

    async def call(self, *, call_id: str, tool_name: str,
                   args: dict, state: dict) -> ToolReceipt:
        """执行一次工具调用并返回模型回执。

        `call_id` 是桥接往返关联 ID（不进账本幂等键）；稳定执行身份由
        `_stable_step_id` 生成，`execution_gateway.governed_call` 内的
        `ActionLedger.admit` 据此分配 action_id / attempt_id。
        """
        # 执行点再次解析并检查工具：只开放 web_search
        tool = _resolve_web_search()
        if tool is None:
            return ToolReceipt(ok=False, error=f'真实工具不可用：{ALLOWED_TOOL_KEY}')
        if tool.name != tool_name:
            return ToolReceipt(
                ok=False, error=f'工具未开放：{tool_name}（仅 {tool.name}）')

        tool_key = tool.tool_key or tool.name
        step_id = _stable_step_id(tool_key, args)

        # 可信主体由 Python 持有（库里的 run 行），不是模型输出
        ctx = await execution_gateway.tool_context(step_id=step_id, action_id='')

        # 受治理调用：授权 → 准入 → 撤权复查 → 真实调用 → 结算
        result = await execution_gateway.governed_call(
            tool=tool, args=args, step_id=step_id, state=state, ctx=ctx)

        if result.kind == 'replay':
            # 此前已成功：复用已有回执（不重新搜索）。重放从 receipt 恢复完整
            # 结果（含 documents），缺内容时明确报错，不重新搜索冒充重放。
            replay = result.replay if isinstance(result.replay, dict) else {}
            if not isinstance(replay.get('documents'), list):
                return ToolReceipt(
                    ok=False, action_id=result.action_id,
                    attempt_id=result.attempt_id, step_id=step_id,
                    error='重放缺少搜索结果内容（documents），不能重新搜索冒充重放')
            return ToolReceipt(
                ok=True, content=_search_receipt(replay),
                action_id=result.action_id, attempt_id=result.attempt_id,
                step_id=step_id, updates=_mergeable_updates(replay))
        if result.kind == 'succeeded':
            return ToolReceipt(
                ok=True, content=_search_receipt(result.updates),
                action_id=result.action_id, attempt_id=result.attempt_id,
                step_id=step_id, updates=_mergeable_updates(result.updates))
        if result.kind == 'denied':
            return ToolReceipt(ok=False, error=result.error,
                               action_id=result.action_id,
                               attempt_id=result.attempt_id, step_id=step_id)
        if result.kind == 'no_run':
            return ToolReceipt(ok=False, error=result.error or '没有可执行的运行',
                               step_id=step_id)
        if result.kind == 'needs_approval':
            return ToolReceipt(ok=False, error=result.error or '需要批准',
                               step_id=step_id)
        if result.kind == 'blocked':
            return ToolReceipt(ok=False, error=result.error or '结果未定，需核实',
                               step_id=step_id)
        # failed
        return ToolReceipt(ok=False, error=result.error or '工具执行失败',
                           action_id=result.action_id,
                           attempt_id=result.attempt_id, step_id=step_id)
