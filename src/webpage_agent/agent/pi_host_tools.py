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
    """**已废弃**（第七阶段）：按参数指纹派生执行身份会导致「相同参数=相同身份」。

    保留仅为向后兼容旧测试引用；新代码一律用 `PiCallRegistry` 分配**持久化的
    逻辑调用 ID**（run 内序号），与参数哈希解耦。
    """
    return f'pi:{tool_key}:{ledger_module.args_hash(args)[:16]}'


def _mergeable_updates(updates: dict) -> dict:
    """从工具增量里挑出**认可**、可合并回宿主 state 的字段（白名单）。"""
    return {key: value for key, value in (updates or {}).items()
            if key in _MERGEABLE_KEYS}


class PiCallRegistry:
    """Pi 逻辑调用身份注册表：把「Node 发起的调用」映射到宿主分配并持久化的
    逻辑调用 ID（run 内单调序号），与参数哈希解耦。

    为什么不能再用 `_stable_step_id(tool_key, args)`：

    - 相同工具 + 相同参数会被派生为**同一个**身份，后续**有意的重复查询**会被
      误判成 replay；
    - 身份必须由宿主**分配并持久化**：第一次见到某个 Node 调用时分配一个新
      序号；恢复时按快照里已提交的 `pending_calls` 重建映射，让**同一次调用**
      复用原序号，而**新的调用**（哪怕参数相同）拿到新序号。

    绑定关系：`node_call_key`（Node 侧稳定标识，来自桥接 call_id + 工具名）→
    `logical_call_id`；同时记录 `(tool_key, args_hash)`。同一 `logical_call_id`
    若参数哈希变化 → 拒绝（不能静默换参数）。
    """

    def __init__(self, snapshot: dict | None = None) -> None:
        # node_call_key -> {'logical_call_id', 'tool_key', 'args_hash'}
        self._bindings: dict[str, dict] = {}
        self._seq = 0
        self._restore(snapshot)

    def _restore(self, snapshot: dict | None) -> None:
        """从快照的 pending_calls 重建已分配的调用身份与序号。"""
        if not snapshot:
            return
        calls = snapshot.get('pending_calls') or []
        if not isinstance(calls, list):
            return
        for item in calls:
            if not isinstance(item, dict):
                continue
            key = str(item.get('node_call_key') or '')
            logical = str(item.get('logical_call_id') or '')
            if not key or not logical:
                continue
            self._bindings[key] = {
                'logical_call_id': logical,
                'tool_key': str(item.get('tool_key') or ''),
                'args_hash': str(item.get('args_hash') or ''),
            }
        self._seq = int(snapshot.get('logical_call_seq') or 0)

    @property
    def seq(self) -> int:
        return self._seq

    def logical_id(self, node_call_key: str, *, tool_key: str,
                   args: dict) -> str:
        """取或分配逻辑调用 ID。恢复复用原 ID，新调用分配新 ID，参数变化拒绝。"""
        args_digest = ledger_module.args_hash(args)
        existing = self._bindings.get(node_call_key)
        if existing is not None:
            # 同一 node_call_key：复用原身份，但参数必须一致
            if existing.get('args_hash') and existing['args_hash'] != args_digest:
                raise RuntimeError(
                    f'逻辑调用 {existing["logical_call_id"]} 的参数已变化，拒绝复用')
            return existing['logical_call_id']
        # 新调用：分配新序号（哪怕参数与历史上某次相同）
        self._seq += 1
        logical = f'pi:call:{self._seq}'
        self._bindings[node_call_key] = {
            'logical_call_id': logical,
            'tool_key': tool_key,
            'args_hash': args_digest,
        }
        return logical

    def pending_calls(self) -> list[dict]:
        """导出待处理调用清单（写进快照，恢复时重建映射）。"""
        return [
            {'node_call_key': key, **value}
            for key, value in self._bindings.items()
        ]

    def node_call_key(self, call_id: str, tool_name: str) -> str:
        """Node 侧稳定标识：桥接 call_id + 工具名（同一轮内唯一）。"""
        return f'{tool_name}:{call_id}'


class HostToolCaller:
    """Pi 宿主工具调用对象：受治理地执行模型发起的工具调用。

    一次 `run()` 独占一个 caller；它不持有凭据或身份，可信主体每次从当前
    执行上下文（账本 + 执行权）推导。

    第七阶段起，`caller` 持有一个 `PiCallRegistry`（逻辑调用身份），由宿主在
    恢复时用快照初始化，据此把「同一次调用」复用原身份、把「新调用」分配新
    身份——与参数哈希解耦，避免相同参数的重复查询被误判成 replay。
    """

    def __init__(self, registry: PiCallRegistry | None = None) -> None:
        self._registry = registry or PiCallRegistry()

    @property
    def registry(self) -> PiCallRegistry:
        return self._registry

    async def call(self, *, call_id: str, tool_name: str,
                   args: dict, state: dict, build_snapshot=None) -> ToolReceipt:
        """执行一次工具调用并返回模型回执。

        `call_id` 是桥接往返关联 ID（不进账本幂等键）；逻辑调用身份由
        `PiCallRegistry` 分配（run 内序号），`execution_gateway.governed_call`
        内的 `ActionLedger.admit` 据此分配 action_id / attempt_id。

        `build_snapshot`：可选的 `(updates, tool_result) -> dict` 回调，在真实
        调用成功、结算前被调用，把本次动作的恢复事实（含已提交工具结果引用）
        写进快照，与 `settle_action` 同一事务提交（第七阶段）。
        """
        # 执行点再次解析并检查工具：只开放 web_search
        tool = _resolve_web_search()
        if tool is None:
            return ToolReceipt(ok=False, error=f'真实工具不可用：{ALLOWED_TOOL_KEY}')
        if tool.name != tool_name:
            return ToolReceipt(
                ok=False, error=f'工具未开放：{tool_name}（仅 {tool.name}）')

        tool_key = tool.tool_key or tool.name
        # 逻辑调用身份：宿主分配并持久化的 run 内序号，与参数哈希解耦
        node_key = self._registry.node_call_key(call_id, tool_name)
        try:
            step_id = self._registry.logical_id(
                node_key, tool_key=tool_key, args=args)
        except RuntimeError as exc:
            return ToolReceipt(ok=False, error=str(exc), step_id='')

        # 可信主体由 Python 持有（库里的 run 行），不是模型输出
        ctx = await execution_gateway.tool_context(step_id=step_id, action_id='')

        # 受治理调用：授权 → 准入 → 撤权复查 → 真实调用 → 结算
        result = await execution_gateway.governed_call(
            tool=tool, args=args, step_id=step_id, state=state, ctx=ctx,
            build_snapshot=build_snapshot)

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
