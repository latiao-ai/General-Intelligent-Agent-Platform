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

# 本阶段 Pi 同步开放的**只读**工具白名单（稳定键）。显式声明，不是「自动开放
# 所有只读工具」。默认只开放 web_search，保持既有行为；新增只读工具（如平台
# 的 get_object / get_operation）需显式加入，确认 effect=read 后再放行。
SYNC_READ_TOOL_KEYS = ('local:web:search',)

# 本阶段 Pi 同步开放的**写**工具白名单（稳定键）。**默认空**——写工具绝不默认
# 开放。第十阶段仅启用受控平台的 update_object（经显式配置注入，见
# `pi_tool_catalog()`），生产不开放通用写工具。
WRITE_TOOL_KEYS: tuple[str, ...] = ()

# 第九阶段保留别名（向后兼容旧测试引用）：只读白名单。
READ_TOOL_KEYS = SYNC_READ_TOOL_KEYS


def _effective_read_keys() -> set[str]:
    """当前生效的读白名单：模块级常量 + 环境变量显式扩展（正式链路测试用）。"""
    import json as _json
    import os as _os
    keys = set(SYNC_READ_TOOL_KEYS)
    extra = _os.environ.get('PI_READ_TOOL_KEYS')
    if extra:
        try:
            keys |= {str(k) for k in _json.loads(extra)}
        except ValueError:
            pass
    return keys


def _effective_write_keys() -> set[str]:
    """当前生效的写白名单：模块级常量 + 环境变量显式扩展（正式链路测试用）。"""
    import json as _json
    import os as _os
    keys = set(WRITE_TOOL_KEYS)
    extra = _os.environ.get('PI_WRITE_TOOL_KEYS')
    if extra:
        try:
            keys |= {str(k) for k in _json.loads(extra)}
        except ValueError:
            pass
    return keys

# 单条来源给模型的正文摘录上限（字符）。正文太长只摘开头，避免撑爆上下文。
SNIPPET_MAX_CHARS = 400

# 一次回执最多带回多少个来源。
MAX_SOURCES = 8

# 合并回宿主 state 时允许写入的**认可字段**（白名单）。工具返回的增量里只有这些
# 键会被合并，其余（status、phase 等内部字段）一律丢弃，避免任意结果覆盖身份、
# 租约、任务状态等宿主字段。
# 第十阶段第 7 点：扩展以保存通用写结果的完整引用（对象 ID / 操作 ID / revision /
# 修改字段 / mcp_calls 结果引用），供回读核验与恢复使用——但只认这几个字段，
# 不让工具增量覆盖身份、租约或终态。
_MERGEABLE_KEYS = ('documents', 'usage', 'errors',
                   'mcp_calls', 'object_id', 'operation_id', 'revision',
                   'new_revision', 'modified_fields', 'write_result',
                   'readback', 'readback_match')


@dataclass
class ToolReceipt:
    """一次工具调用转给模型（Pi）的回执。

    `ok` 为 True 时 `content` 是结构化结果（来源列表）；为 False 时 `error`
    是原始错误正文（保留，不吞）。`updates` 是**已结算、可合并回宿主 state**
    的认可增量（成功时非空）。

    第八阶段第 3 点：`accepted` 为 True 表示「远端已受理、结果未定」——这不是
    成功，也不是失败，Pi 不得把它当最终结果继续推理，而应结束本轮等待唤醒。
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
    # 已受理（accepted）标记与远端任务身份
    accepted: bool = False
    remote_task_id: str = ''
    contract_id: str = ''
    contract_version: int = 1
    # 本地登记的 external task 身份（第八阶段收口第 1 点）：本地匹配主键。
    external_task_id: str = ''
    # 需要批准（第十阶段第 5 点）：保留 approval_id 与原调用关联，供批准后恢复
    # 原候选。不是失败——动作尚未发出，用户确认后继续。
    needs_approval: bool = False
    approval_id: str = ''
    # 最终发送参数（含幂等键注入）与参数指纹：needs_approval 时随回执带回，
    # 供批准后恢复原调用、重查约束（不能靠模型重发的参数冒充原调用）。
    final_args: dict | None = None
    args_hash: str = ''


def _resolve_web_search():
    """执行点解析只读工具：从**可信目录**按 `SYNC_READ_TOOL_KEYS` 白名单查找。

    第九阶段去硬编码：不再写死 `local:web:search`，而是从能力包 registry 里按
    白名单查找并校验 `effect == 'read'`。第十阶段改为按请求工具名唯一匹配
    （见 `HostToolCaller.resolve_tool`），此函数保留为「默认目录下解析第一个
    白名单只读工具」的兼容入口（测试引用）。
    """
    allowed = {key for key in SYNC_READ_TOOL_KEYS}
    for tool in registry.load():
        if tool.tool_key not in allowed:
            continue
        if str(tool.effect or '') != 'read':
            continue
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


def _receipt_content(updates: dict, tool_name: str) -> dict:
    """把一次只读工具的 state 增量归一成给模型的内容。

    第九阶段去硬编码：不再写死 `web_search` 的工具名与结构。工具名由调用方
    传入；增量归一沿用**通用字段**（documents / errors / usage），这些是能力
    包统一的产物形态（来源登记、错误登记、用量），不是某个工具专属。

    不把整份 state 发给 Node：只带来源 ID / 标题 / URL / 限长摘录。
    `status` 是阶段小结，原样保留为事实，由上层决定终态。

    `updates` 可以是真实调用后的增量，也可以是重放时从 receipt 恢复的完整
    内容（含 `documents`）。两者共用同一个 `documents` 字段。
    """
    documents = updates.get('documents') or []
    errors = updates.get('errors') or []
    result = {
        'tool': tool_name,
        'status': str(updates.get('status') or 'searched'),
        'sources': [_source_excerpt(d) for d in documents[:MAX_SOURCES]
                    if isinstance(d, dict)],
        'total_sources': len(documents),
    }
    if errors:
        result['errors'] = [{'stage': e.get('stage'), 'type': e.get('type')}
                            for e in errors if isinstance(e, dict)]
    # 第十阶段第 7 点：写工具（如 update_object）的回执带对象 ID / 操作 ID /
    # revision / 修改字段，必须原样保留给模型，供回读核验与恢复（不丢字段）。
    for key in ('object_id', 'operation_id', 'new_revision', 'revision',
                'modified_fields', 'write_result', 'readback',
                'readback_match'):
        if key in updates:
            result[key] = updates[key]
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


def _tool_server_prefix(tool_key: str) -> str:
    """从 MCP 工具键 `mcp:<server>:<name>` 提取 server 前缀 `mcp:<server>:`。

    非 MCP 键（本地能力包 `local:*` 等）返回空串。用于判断写工具与回读工具
    是否属于同一 server（写后回读必须同 server、同主体）。
    """
    key = str(tool_key or '')
    if not key.startswith('mcp:'):
        return ''
    parts = key.split(':', 2)
    if len(parts) < 3:
        return ''
    return f'mcp:{parts[1]}:'


def _readback_payload_from_result(read_result) -> dict:
    """从受治理回读结果（GovernedResult）提取 `get_object` 的对象 payload。

    回读工具经 MCP runner 时，get_object 返回的完整 payload（含 `fields` dict）
    存在 `updates['mcp_calls'][-1]['payload']` 里（MCP runner 只把标量提到顶层，
    `fields` 是 dict 不会被提）。这里优先从 mcp_calls 取完整 payload，兜底用
    updates 顶层的标量字段（object_id / revision）。
    """
    updates = (read_result.updates or {}) if read_result is not None else {}
    # 完整 payload 在 mcp_calls 最后一条里
    mcp_calls = updates.get('mcp_calls') or []
    if isinstance(mcp_calls, list) and mcp_calls:
        last = mcp_calls[-1]
        if isinstance(last, dict) and isinstance(last.get('payload'), dict):
            payload = last['payload']
            return {
                'status': payload.get('status', 'ok'),
                'object_id': payload.get('object_id', updates.get('object_id', '')),
                'owner': payload.get('owner', ''),
                'revision': payload.get('revision', updates.get('revision')),
                'fields': payload.get('fields') or {},
            }
    # 兜底：updates 顶层标量
    return {
        'status': updates.get('status', 'ok'),
        'object_id': updates.get('object_id', ''),
        'owner': updates.get('owner', ''),
        'revision': updates.get('revision'),
        'fields': updates.get('fields') or {},
    }


async def _return(value):
    """把一个值包成 awaitable（供 verify_platform_write 的 get_object_call 用）。"""
    return value


class PiCallRegistry:
    """Pi 逻辑调用身份注册表：把「Node 发起的调用」映射到宿主分配并持久化的
    逻辑调用 ID（run 内单调序号），与参数哈希解耦。

    为什么不能再用 `_stable_step_id(tool_key, args)`：

    - 相同工具 + 相同参数会被派生为**同一个**身份，后续**有意的重复查询**会被
      误判成 replay；
    - 身份必须由宿主**分配并持久化**：第一次见到某个 Node 调用时分配一个新
      序号；恢复时按快照里已提交的 `pending_calls` 重建映射，让**同一次调用**
      复用原序号，而**新的调用**（哪怕参数相同）拿到新序号。

    绑定关系：桥接 `call_id`（Node 侧 `ToolCall.id`）→ 逻辑调用 ID；同时记录
    `(tool_key, args_hash, seq, status)`。同一 `logical_call_id` 若参数哈希变化
    → 拒绝（不能静默换参数）。

    第七阶段收口：`pending_calls()` 导出**唯一版本化结构**（见
    `pi_snapshot.normalize_pending_call`），与 Node 检查点、动作回执、恢复决策
    共用同一映射，不再各自维护一套字段名。
    """

    def __init__(self, snapshot: dict | None = None) -> None:
        # call_id -> {'logical_call_id', 'tool_name', 'tool_key', 'arguments',
        #             'args_hash', 'seq', 'status'}
        self._bindings: dict[str, dict] = {}
        self._seq = 0
        self._restore(snapshot)

    def _restore(self, snapshot: dict | None) -> None:
        """从快照的 pending_calls（唯一结构）重建已分配的调用身份与序号。

        第八阶段第 1 点：恢复时**完整**重建参数与工具绑定（arguments / tool_key /
        args_hash），不能只回填逻辑 ID——否则结算当前调用时无法核对其参数与
        工具是否与派发时一致，同轮尚未派发的候选也会因为缺参数而无法继续。
        """
        if not snapshot:
            return
        calls = snapshot.get('pending_calls') or []
        if not isinstance(calls, list):
            return
        for item in calls:
            if not isinstance(item, dict):
                continue
            call_id = str(item.get('call_id') or '')
            logical = str(item.get('logical_call_id') or '')
            if not call_id or not logical:
                continue
            self._bindings[call_id] = {
                'logical_call_id': logical,
                'tool_name': str(item.get('tool_name') or ''),
                'tool_key': str(item.get('tool_key') or ''),
                'arguments': dict(item.get('arguments') or {}),
                'args_hash': str(item.get('args_hash') or ''),
                'seq': int(item.get('seq') or 0),
                'status': str(item.get('status') or 'pending'),
            }
        self._seq = int(snapshot.get('logical_call_seq') or 0)

    @property
    def seq(self) -> int:
        return self._seq

    def logical_id(self, call_id: str, *, tool_name: str, tool_key: str,
                   args: dict) -> str:
        """取或分配逻辑调用 ID。恢复复用原 ID，新调用分配新 ID，参数变化拒绝。"""
        args_digest = ledger_module.args_hash(args)
        existing = self._bindings.get(call_id)
        if existing is not None:
            # 同一 call_id：复用原身份，但参数与工具绑定必须一致
            if existing.get('args_hash') and existing['args_hash'] != args_digest:
                raise RuntimeError(
                    f'逻辑调用 {existing["logical_call_id"]} 的参数已变化，拒绝复用')
            if existing.get('tool_key') and existing['tool_key'] != tool_key:
                raise RuntimeError(
                    f'逻辑调用 {existing["logical_call_id"]} 的工具绑定已变化'
                    f'（{existing["tool_key"]} → {tool_key}），拒绝复用')
            return existing['logical_call_id']
        # 新调用：分配新序号（哪怕参数与历史上某次相同）
        self._seq += 1
        logical = f'pi:call:{self._seq}'
        self._bindings[call_id] = {
            'logical_call_id': logical,
            'tool_name': tool_name,
            'tool_key': tool_key,
            'arguments': dict(args or {}),
            'args_hash': args_digest,
            'seq': self._seq,
            'status': 'pending',
        }
        return logical

    def mark(self, call_id: str, status: str) -> None:
        """更新某个调用的处理状态（pending → admitted/succeeded/delivered）。

        只动**这个 call_id** 自己的绑定，不影响同轮尚未派发的其它候选——
        结算当前调用绝不能把同轮别的 pending 候选一并清掉或改状态。
        """
        binding = self._bindings.get(call_id)
        if binding is not None:
            binding['status'] = status

    def pending_calls(self) -> list[dict]:
        """导出**尚未派发/尚未完成**的调用清单（唯一版本化结构，写进快照）。

        第八阶段第 1 点：已交付/已成功（`delivered` / `succeeded`）的调用
        **不得**再次作为 pending 导出——否则恢复时会把已完成的调用当新候选
        重新派发，重复执行工具。只导出 `pending` / `admitted` 两类。
        """
        from webpage_agent.agent import pi_snapshot
        active = [
            {'call_id': call_id, **binding}
            for call_id, binding in self._bindings.items()
            if str(binding.get('status') or 'pending') in ('pending', 'admitted')
        ]
        return pi_snapshot.normalize_pending_calls(active)

    def node_call_key(self, call_id: str, tool_name: str) -> str:
        """兼容旧调用点：桥接 call_id + 工具名。第七阶段起内部按 call_id 绑定。"""
        return f'{tool_name}:{call_id}'


class HostToolCaller:
    """Pi 宿主工具调用对象：受治理地执行模型发起的工具调用。

    一次 `run()` 独占一个 caller；它不持有凭据或身份，可信主体每次从当前
    执行上下文（账本 + 执行权）推导。

    第七阶段起，`caller` 持有一个 `PiCallRegistry`（逻辑调用身份），由宿主在
    恢复时用快照初始化，据此把「同一次调用」复用原身份、把「新调用」分配新
    身份——与参数哈希解耦，避免相同参数的重复查询被误判成 replay。
    """

    def __init__(self, registry: PiCallRegistry | None = None,
                 executor=None, tools_catalog: list | None = None) -> None:
        self._registry = registry or PiCallRegistry()
        # 可注入的工具执行器（测试用）：覆盖真实 tool.run，但仍走完整的受治理
        # 结算链路，用于无网络/无外部依赖下验证恢复不变量。
        self._executor = executor
        # 工具目录（第十阶段）：一组 Tool 对象，供按**请求工具名唯一匹配**。
        # None 表示回退到「只读白名单里的默认工具」（本地能力包 registry）。
        self._tools_catalog = tools_catalog

    @property
    def registry(self) -> PiCallRegistry:
        return self._registry

    def resolve_tool(self, tool_name: str):
        """按请求工具名**唯一匹配**一个 Tool（第十阶段）。

        纪律：不能取「白名单首项」。工具目录里按 name 精确匹配，且必须落在
        读/写白名单内；读工具校验 effect=read，写工具校验 effect != read 且
        键在 WRITE_TOOL_KEYS 里。匹配不到或不在白名单 → 返回 None（拒绝）。

        无显式目录时回退到本地能力包 registry（只读白名单）。
        """
        catalog = self._tools_catalog
        if catalog is None:
            from webpage_agent.capabilities import registry as _reg
            catalog = list(_reg.load())
        allowed_read = _effective_read_keys()
        allowed_write = _effective_write_keys()
        for tool in catalog:
            if getattr(tool, 'name', '') != tool_name:
                continue
            key = getattr(tool, 'tool_key', '') or tool_name
            effect = str(getattr(tool, 'effect', '') or 'unknown')
            if key in allowed_read and effect == 'read':
                return tool
            if key in allowed_write:
                return tool
            # 名字匹配但不在白名单 → 拒绝（不返回，交给上层报「未开放」）
            return None
        return None

    def _find_readback_tool(self, write_tool):
        """找与写工具同 server 的只读回读工具（get_object）。

        第十阶段第 1/7 点：写后回读必须用**同一可信主体**读同一对象，因此回读
        工具必须与写工具属于同一 MCP server（tool_key 前缀 `mcp:<server>:` 一致）。
        找不到（未在目录里开放读工具）返回 None——此时不能做回读，写效果判为
        未核实（缺回读不得成功）。

        按名字 `get_object` 匹配，且校验其 tool_key 与写工具同 server 前缀、
        effect == 'read'。不能跨 server 回读（那读的是别的平台的对象）。
        """
        if self._tools_catalog is None:
            return None
        write_key = getattr(write_tool, 'tool_key', '') or ''
        server_prefix = _tool_server_prefix(write_key)
        for tool in self._tools_catalog:
            # 第十二阶段缺口修复：自动发现的 MCP 工具名带 `mcp__` 前缀
            # （单服务端 `mcp__get_object`，多服务端 `mcp__<server>__get_object`），
            # 而第十阶段测试里手动构造的 Tool 用无前缀 `name='get_object'`。
            # 因此不能只按裸名 `get_object` 匹配——按 tool_key 的尾缀 `:get_object`
            # 匹配，兼容两种命名，且仍校验同 server 前缀 + effect=read。
            name = str(getattr(tool, 'name', '') or '')
            key = str(getattr(tool, 'tool_key', '') or '')
            if key.endswith(':get_object') or name == 'get_object':
                if str(getattr(tool, 'effect', '') or 'unknown') != 'read':
                    continue
                if server_prefix and _tool_server_prefix(key) != server_prefix:
                    continue
                return tool
        return None

    async def _perform_readback(self, write_tool, write_updates: dict,
                                state: dict, write_step_id: str) -> dict | None:
        """写成功后，用同一可信主体独立读回对象，返回写证据（第十阶段第 1/7 点）。

        只处理「平台对象写」：写结果带 `object_id` 才需要回读。回读走**同一条
        受治理链**（governed_call，read effect）——授权、预算、结果记录都不绕过，
        不是手工调用核验函数。

        返回写证据 dict（含 object_id / operation_id / args_hash / intent_version
        / read_action_id / readback_match / checks / status），或 None（无需回读 /
        找不到回读工具 / 回读失败——回读失败也返回证据，只是 readback_match=False，
        让宿主据此判 needs_reconcile，不能默默跳过）。

        纪律：回读失败、回读工具缺失、字段不符，都**保留证据**而不是吞掉——
        写效果「未核实」是事实，宿主据此决定不得成功收尾。
        """
        from webpage_agent.core import platform_verify

        # 第十二阶段缺口修复：回读只在**写工具**（effect != read）后做。读工具
        # （如 get_object）的返回里也有 object_id，若不加 effect 判断会把「读对象」
        # 误当成「写对象」触发回读，产生一条 readback_match=False 的伪证据，让
        # 宿主把本已成功的写闭环误判为 needs_reconcile。
        effect = str(getattr(write_tool, 'effect', '') or 'unknown')
        if effect == 'read':
            return None

        if not platform_verify.needs_readback(write_updates):
            return None

        object_id = str(write_updates.get('object_id') or '')
        operation_id = str(write_updates.get('operation_id') or '')
        new_revision = write_updates.get('new_revision')
        modified_fields = write_updates.get('modified_fields') or {}

        # 回读工具：与写工具同 server 的 get_object（读工具）。
        readback_tool = self._find_readback_tool(write_tool)
        if readback_tool is None:
            # 没有可用的回读读工具：写效果无法核实，记录证据（未核实）。
            return {
                'object_id': object_id, 'operation_id': operation_id,
                'new_revision': new_revision, 'modified_fields': modified_fields,
                'args_hash': ledger_module.args_hash(
                    (write_updates.get('final_args') or {})),
                'intent_version': int(state.get('intent_version') or 0),
                'read_action_id': '', 'readback': None, 'readback_match': False,
                'checks': {'readback_tool': False}, 'status': 'unverified',
            }

        # 回读也走受治理链：分配新的逻辑调用身份（与写调用解耦），复用同一
        # 账本/执行上下文。回读是 read 工具，effect=read，白名单里需已放行。
        read_call_id = f'{write_step_id}:readback'
        try:
            read_step_id = self._registry.logical_id(
                read_call_id, tool_name='get_object',
                tool_key=getattr(readback_tool, 'tool_key', '') or 'get_object',
                args={'object_id': object_id})
        except RuntimeError:
            read_step_id = f'{write_step_id}:readback'

        # 最终参数：get_object 只需 object_id，无需幂等键。
        from webpage_agent.tools import core as tools_core
        book = ledger_module.current_ledger()
        read_args = tools_core.execution_arguments(
            readback_tool, {'object_id': object_id}, read_step_id, book)

        ctx = await execution_gateway.tool_context(
            step_id=read_step_id, action_id='')

        from webpage_agent.agent import runtime_identity
        try:
            read_result = await execution_gateway.governed_call(
                tool=readback_tool, args=read_args, step_id=read_step_id,
                state=state, ctx=ctx,
                snapshot_schema=runtime_identity.PI_STATE_SCHEMA)
        except Exception as exc:                    # noqa: BLE001
            logger.warning('写后回读调用失败 object=%s：%s', object_id, exc)
            return {
                'object_id': object_id, 'operation_id': operation_id,
                'new_revision': new_revision, 'modified_fields': modified_fields,
                'args_hash': ledger_module.args_hash(
                    (write_updates.get('final_args') or {})),
                'intent_version': int(state.get('intent_version') or 0),
                'read_action_id': '', 'readback': None, 'readback_match': False,
                'checks': {'error': type(exc).__name__}, 'status': 'unverified',
            }

        read_action_id = read_result.action_id
        read_payload = _readback_payload_from_result(read_result)
        # 回读工具经 MCP runner 时，get_object 的 payload 里 object_id/revision/
        # fields 提到 updates 顶层。这里用 platform_verify 做逐项核验。
        verification = await platform_verify.verify_platform_write(
            {'object_id': object_id, 'new_revision': new_revision,
             'modified_fields': modified_fields},
            server=None,
            get_object_call=lambda oid: _return(read_payload))

        return {
            'object_id': object_id, 'operation_id': operation_id,
            'new_revision': new_revision, 'modified_fields': modified_fields,
            'args_hash': ledger_module.args_hash(
                (write_updates.get('final_args') or {})),
            'intent_version': int(state.get('intent_version') or 0),
            'read_action_id': read_action_id,
            'readback': read_payload or None,
            'readback_match': bool(verification.get('readback_match')),
            'checks': verification.get('checks') or {},
            'status': 'verified' if verification.get('readback_match') else 'unverified',
        }

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
        # 执行点再次解析并检查工具：按请求工具名唯一匹配，且必须在读/写白名单
        # 内（第十阶段）。不能取「白名单首项」——不同工具名必须匹配到不同工具。
        tool = self.resolve_tool(tool_name)
        if tool is None:
            return ToolReceipt(
                ok=False,
                error=f'工具未开放或不在白名单：{tool_name}')

        tool_key = tool.tool_key or tool.name
        # 逻辑调用身份：宿主分配并持久化的 run 内序号，与参数哈希解耦。
        # 第七阶段收口：按桥接 call_id 直接绑定（ToolCall.id），不再拼 node_call_key。
        try:
            step_id = self._registry.logical_id(
                call_id, tool_name=tool_name, tool_key=tool_key, args=args)
        except RuntimeError as exc:
            return ToolReceipt(ok=False, error=str(exc), step_id='')

        # 第十阶段第 4 点：宿主分配逻辑调用身份后，复用 execution_arguments 生成
        # **最终发送参数**（含幂等键注入）。授权、审批指纹、账本准入、远端发送
        # 必须针对同一份最终参数——不能各用各的。
        from webpage_agent.tools import core as tools_core
        book = ledger_module.current_ledger()
        final_args = tools_core.execution_arguments(tool, args, step_id, book)

        # 可信主体由 Python 持有（库里的 run 行），不是模型输出
        ctx = await execution_gateway.tool_context(step_id=step_id, action_id='')

        # 受治理调用：授权 → 准入 → 撤权复查 → 真实调用 → 结算。快照标注为
        # 版本化 Pi 快照（第七阶段收口），避免 settle 把 Pi 快照覆盖成普通 state。
        from webpage_agent.agent import runtime_identity
        result = await execution_gateway.governed_call(
            tool=tool, args=final_args, step_id=step_id, state=state, ctx=ctx,
            build_snapshot=build_snapshot,
            snapshot_schema=runtime_identity.PI_STATE_SCHEMA,
            executor=self._executor)

        if result.kind == 'replay':
            # 此前已成功：复用已有回执（不重新执行）。重放从 receipt 恢复完整
            # 结果。读工具要求有 documents，写工具要求有结果引用（object_id 或
            # write_result 或 mcp_calls），缺内容时明确报错，不重新执行冒充重放。
            replay = result.replay if isinstance(result.replay, dict) else {}
            has_content = isinstance(replay.get('documents'), list) or \
                bool(replay.get('object_id') or replay.get('write_result')
                     or replay.get('mcp_calls') or replay.get('new_revision'))
            if not has_content:
                return ToolReceipt(
                    ok=False, action_id=result.action_id,
                    attempt_id=result.attempt_id, step_id=step_id,
                    error='重放缺少结果内容，不能重新执行冒充重放')
            # 复用已有结果：调用身份标记为 delivered，不再作为 pending 导出。
            self._registry.mark(call_id, 'delivered')
            return ToolReceipt(
                ok=True, content=_receipt_content(replay, tool_name),
                action_id=result.action_id, attempt_id=result.attempt_id,
                step_id=step_id, updates=_mergeable_updates(replay))
        if result.kind == 'succeeded':
            # 第七阶段收口：故障注入钩子（测试专用）。工具已结算（action/attempt/
            # 快照已落库）后、Node 尚未吸收结果前，模拟进程崩溃——验证「工具已
            # 结算、Node 未吸收」的恢复不重复执行工具。
            import os as _os
            if _os.environ.get('PI_CRASH_AFTER_SETTLE') == '1':
                _os._exit(0)
            # 第十阶段第 1/7 点：写工具成功后，用同一可信主体独立回读同一对象，
            # 逐项保存写证据（对象 ID / 操作 ID / 参数指纹 / 要求版本 / 回读 action
            # / 核验结论）。证据追加到 state['write_evidences']（不是顶层单个
            # readback_match），供宿主 final_status 逐项核验。回读失败/缺回读工具
            # 也保留证据（readback_match=False），让写效果被判「未核实」。
            write_updates = dict(result.updates or {})
            write_updates.setdefault('final_args', (result.receipt or {}).get('final_args') or {})
            write_evidence = await self._perform_readback(
                tool, write_updates, state, step_id)
            if write_evidence is not None:
                existing = list(state.get('write_evidences') or [])
                existing.append(write_evidence)
                state['write_evidences'] = existing
            # 结算成功：标记 delivered，该调用不再作为 pending 导出（第八阶段第 1 点）。
            self._registry.mark(call_id, 'delivered')
            return ToolReceipt(
                ok=True, content=_receipt_content(result.updates, tool_name),
                action_id=result.action_id, attempt_id=result.attempt_id,
                step_id=step_id, updates=_mergeable_updates(result.updates))
        if result.kind == 'denied':
            return ToolReceipt(ok=False, error=result.error,
                               action_id=result.action_id,
                               attempt_id=result.attempt_id, step_id=step_id)
        if result.kind == 'accepted':
            # 第八阶段第 3 点：远端已受理、结果未定。不是成功也不是失败——
            # Pi 不得把受理当最终结果继续推理。标记 accepted 并带远端任务身份，
            # 由上层（pi_runtime）结束本轮、进入持久等待。
            # 第八阶段收口第 10 点：故障注入钩子（测试专用）。accepted 已结算
            # （action accepted + external task 已登记）后、Node 尚未退出前，
            # 模拟进程崩溃——验证「accepted 已提交」的恢复不重复提交远端。
            import os as _os
            if _os.environ.get('PI_CRASH_AFTER_ACCEPTED') == '1':
                _os._exit(0)
            return ToolReceipt(
                ok=False, accepted=True, error='',
                action_id=result.action_id, attempt_id=result.attempt_id,
                step_id=step_id, remote_task_id=result.remote_task_id,
                contract_id=result.contract_id,
                external_task_id=result.external_task_id)
        if result.kind == 'no_run':
            return ToolReceipt(ok=False, error=result.error or '没有可执行的运行',
                               step_id=step_id)
        if result.kind == 'needs_approval':
            return ToolReceipt(ok=False, needs_approval=True,
                               approval_id=result.approval_id,
                               error=result.error or '需要批准',
                               step_id=step_id,
                               final_args=final_args,
                               args_hash=ledger_module.args_hash(final_args or {}))
        if result.kind == 'blocked':
            return ToolReceipt(ok=False, error=result.error or '结果未定，需核实',
                               step_id=step_id)
        # failed
        return ToolReceipt(ok=False, error=result.error or '工具执行失败',
                           action_id=result.action_id,
                           attempt_id=result.attempt_id, step_id=step_id)
