"""版本化 Pi 快照：Pi 执行上下文的可恢复事实与严格读写。

Pi 与 LangGraph 不同——它没有「图计划 + 步骤游标」，恢复靠的是**一次完整模型
轮次里的对话事实与调用进度**。快照必须保存足以「既不漏结果、也不重复已完成
动作」的最少完整状态：

- `phase`：运行阶段（started / running / awaiting_tools / answered / failed /
  cancelled）；
- `checkpoint_seq`：检查点序号（单调递增，Node 每发一次检查点 +1）；
- `intent_version` / `processed_intent_version`：最新及已处理的要求版本；
- `host_result_state`：宿主结果状态（已结算工具结果引用的游标/水位）；
- `messages`：完整模型消息（**必须保留 assistant / toolCall / toolResult 配对**）；
- `logical_call_seq`：逻辑调用序号（宿主分配并持久化的调用身份，见 Task 43）；
- `pending_calls`：待处理调用（已确定候选、尚未准入/完成）；
- `committed_tool_results`：已提交工具结果引用（data_ref / action_id / attempt_id）；
- `final_answer_candidate`：最终答复候选（已产出、待提交）。

**权限与租约不进快照**：租约、凭据、授权由当前宿主重新装配，绝不让恢复从
快照里「复活」已过期的授权。快照缺失 / 损坏 / 版本不兼容时明确停止，不回退。

快照状态格式代次：`PI_STATE_SCHEMA`（定义在 `runtime_identity`）。改字段结构
（增删字段、改语义）就递增，旧代次读不了就抛 `IncompatibleRuntimeVersion`。
"""
from __future__ import annotations

from typing import Any

from webpage_agent.agent import runtime_identity

# 快照顶层字段白名单。只保留这些键：多余键（历史遗留 / 未来版本）在**写**时
# 丢弃，读时忽略，避免把权限、租约或宿主字段混进快照。
_SNAPSHOT_KEYS = (
    'phase',
    'checkpoint_seq',
    'intent_version',
    'processed_intent_version',
    'host_result_state',
    'messages',
    'logical_call_seq',
    'pending_calls',
    'awaiting_calls',
    'awaiting_approval',
    'committed_tool_results',
    'final_answer_candidate',
    'user_input',
)

# 快照 `messages` 允许的角色（assistant / toolCall / toolResult 必须成对保留）。
_MESSAGE_ROLES = ('user', 'assistant', 'toolCall', 'toolResult', 'system')

# 待处理调用的**唯一版本化结构**（第七阶段收口）：Node 检查点、Python 注册表、
# 动作回执、恢复决策共用这一份映射。字段含义：
#   call_id          桥接调用 ID（Node 侧 ToolCall.id）
#   logical_call_id  宿主分配并持久化的逻辑调用 ID（run 内序号 pi:call:N）
#   tool_name        工具名
#   tool_key         稳定工具键（local:web:search）
#   arguments        完整参数
#   args_hash        完整参数哈希（sha256，与账本幂等键一致）
#   seq              调用顺序（同一轮内从 0 递增）
#   status           pending / admitted / succeeded / delivered
_PENDING_CALL_KEYS = (
    'call_id', 'logical_call_id', 'tool_name', 'tool_key',
    'arguments', 'args_hash', 'seq', 'status',
)


def normalize_pending_call(item: dict) -> dict:
    """把一份待处理调用规整成唯一结构（缺失字段给默认值，多余字段丢弃）。"""
    if not isinstance(item, dict):
        return {}
    return {
        'call_id': str(item.get('call_id') or ''),
        'logical_call_id': str(item.get('logical_call_id') or ''),
        'tool_name': str(item.get('tool_name') or ''),
        'tool_key': str(item.get('tool_key') or ''),
        'arguments': dict(item.get('arguments') or {}),
        'args_hash': str(item.get('args_hash') or ''),
        'seq': int(item.get('seq') or 0),
        'status': str(item.get('status') or 'pending'),
    }


def normalize_pending_calls(calls: list) -> list[dict]:
    """规整一组待处理调用（只保留 normalize_pending_call 的字段）。"""
    if not isinstance(calls, list):
        return []
    out = []
    for item in calls:
        normalized = normalize_pending_call(item)
        if normalized:
            out.append(normalized)
    return out


def assistant_tool_calls(messages: list) -> list[dict]:
    """从完整消息里提取 assistant 消息里的工具调用（ToolCall 嵌在 content 里）。

    识别**实际字段**：Pi SDK 的 `AssistantMessage.content` 是数组，其中
    `type == 'toolCall'` 的元素是工具调用，其 `id` 才是桥接调用 ID
    （对应 `ToolResultMessage.toolCallId`）。不能只看消息角色。
    """
    result: list[dict] = []
    if not isinstance(messages, list):
        return result
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get('role') or '') != 'assistant':
            continue
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'toolCall':
                result.append({
                    'id': str(block.get('id') or ''),
                    'name': str(block.get('name') or ''),
                    'arguments': dict(block.get('arguments') or {}),
                })
    return result


def tool_result_ids(messages: list) -> set[str]:
    """从完整消息里提取 toolResult 消息的 toolCallId 集合（配对键）。"""
    ids: set[str] = set()
    if not isinstance(messages, list):
        return ids
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get('role') or '') != 'toolResult':
            continue
        call_id = str(msg.get('toolCallId') or '')
        if call_id:
            ids.add(call_id)
    return ids


def unabsorbed_tool_calls(messages: list) -> list[dict]:
    """返回 assistant 里尚未被 toolResult 吸收（toolCallId 未配对）的工具调用。

    配对键是 `ToolCall.id == ToolResultMessage.toolCallId`，不是消息角色。
    """
    results = tool_result_ids(messages)
    return [call for call in assistant_tool_calls(messages)
            if call['id'] and call['id'] not in results]


class SnapshotError(RuntimeError):
    """Pi 快照缺失 / 损坏 / 版本不兼容：不能继续，必须明确停止。"""


def current_schema() -> int:
    return runtime_identity.PI_STATE_SCHEMA


def validate_schema(schema: int) -> None:
    """校验快照代次；不兼容抛 `IncompatibleRuntimeVersion`。"""
    if int(schema or 0) != current_schema():
        raise runtime_identity.IncompatibleRuntimeVersion(
            '<pi-snapshot>', runtime_identity.KIND_PI,
            'runtime_state_schema', int(schema or 0), current_schema())


def new_snapshot(*, phase: str = 'started', checkpoint_seq: int = 0,
                 intent_version: int = 0, processed_intent_version: int = 0,
                 user_input: str = '') -> dict:
    """构造一份空的新快照（首次执行前）。"""
    return {
        'state_schema': current_schema(),
        'phase': phase,
        'checkpoint_seq': int(checkpoint_seq or 0),
        'intent_version': int(intent_version or 0),
        'processed_intent_version': int(processed_intent_version or 0),
        'host_result_state': {},
        'messages': [],
        'logical_call_seq': 0,
        'pending_calls': [],
        'awaiting_calls': [],
        'awaiting_approval': [],
        'committed_tool_results': [],
        'final_answer_candidate': None,
        'user_input': str(user_input or ''),
    }


def _clean_state(state: dict) -> dict:
    """从一份可能混入宿主字段的 dict 里，挑出快照允许的键，写回规整结构。"""
    out: dict[str, Any] = {
        'state_schema': current_schema(),
        'phase': str(state.get('phase') or 'started'),
        'checkpoint_seq': int(state.get('checkpoint_seq') or 0),
        'intent_version': int(state.get('intent_version') or 0),
        'processed_intent_version': int(
            state.get('processed_intent_version') or 0),
        'host_result_state': dict(state.get('host_result_state') or {}),
        'messages': list(state.get('messages') or []),
        'logical_call_seq': int(state.get('logical_call_seq') or 0),
        'pending_calls': list(state.get('pending_calls') or []),
        'awaiting_calls': list(state.get('awaiting_calls') or []),
        'awaiting_approval': list(state.get('awaiting_approval') or []),
        'committed_tool_results': list(state.get('committed_tool_results') or []),
        'final_answer_candidate': state.get('final_answer_candidate'),
        'user_input': str(state.get('user_input') or ''),
    }
    return out


def serialize(state: dict) -> dict:
    """把快照 state 规整成可落库的 dict（剥离权限/租约等非快照字段）。"""
    return _clean_state(state)


def _is_blank_state(raw: dict) -> bool:
    """判断一份快照内容是否**空白**（不能作为可恢复事实，也不能被清洗成默认值）。

    第八阶段第 1 点：空快照（`{}`）**不得**被 `_clean_state` 清洗成一份可继续
    执行的默认快照——那会让恢复误判为「首次执行、无事实」而从头重新推理，
    重复副作用。真正的 Pi 快照一定由 `serialize` 写过、带 `state_schema` 与
    至少一个核心字段（phase / messages / checkpoint_seq）。这里判断是否空白。
    """
    if not raw:
        return True
    # 只有 state_schema 而没有其它任何核心字段，也视为空白（等于没写内容）。
    core = ('phase', 'messages', 'checkpoint_seq', 'pending_calls',
            'committed_tool_results', 'final_answer_candidate', 'user_input',
            'logical_call_seq')
    return not any(raw.get(key) is not None for key in core)


def parse(raw: dict | None) -> dict:
    """严格读快照：缺失 / 版本不兼容明确抛，不返回空 dict 冒充空快照。

    调用方（`pi_runtime` 恢复入口）据此区分「没有快照（首次执行）」与
    「快照损坏 / 版本不兼容（必须停止）」。缺失用 `raw is None` 判断，
    损坏/不兼容这里抛 `SnapshotError`。
    """
    if raw is None:
        raise SnapshotError('Pi 快照缺失')
    if not isinstance(raw, dict):
        raise SnapshotError('Pi 快照损坏：不是对象')
    if _is_blank_state(raw):
        raise SnapshotError('Pi 快照损坏：内容为空，不能清洗成可执行的默认值')
    schema = int((raw.get('state_schema') or raw.get('schema') or 0))
    validate_schema(schema)
    return _clean_state(raw)


def parse_row(row: dict | None) -> dict:
    """从 `store.run_snapshot()` 返回的**数据库行结构**里解析 Pi 快照。

    `run_snapshot()` 返回 `{state: <解析后的 state_json>, schema: <snapshot_schema>,
    ...其余 run_snapshots 列}`。快照内容在 `row['state']`，版本号以
    `state.state_schema`（写入时由 `serialize` 烙上的代次）为准，缺失时退回
    DB 列 `snapshot_schema`。

    与 `parse` 的区别：`parse` 接受内部快照 dict，`parse_row` 接受 DB 行包装。
    统一在这里做「取内容 + 严格解析」，调用方不再手工抽 `row['state']`。
    """
    if row is None:
        raise SnapshotError('Pi 快照缺失')
    if not isinstance(row, dict):
        raise SnapshotError('Pi 快照损坏：不是对象')
    state = row.get('state')
    if state is None:
        # 行存在但 state 为空：可能是损坏（state_json 解析失败被吞成 {}）。
        raise SnapshotError('Pi 快照损坏：state 为空')
    if not isinstance(state, dict):
        raise SnapshotError('Pi 快照损坏：state 不是对象')
    inner = dict(state)
    if _is_blank_state(inner):
        raise SnapshotError('Pi 快照损坏：内容为空，不能清洗成可执行的默认值')
    # 版本号：优先快照内 state_schema（serialize 烙上的），退回 DB 列。
    schema = int(inner.get('state_schema') or row.get('schema') or 0)
    validate_schema(schema)
    return _clean_state(inner)


def messages_ok(messages: list) -> bool:
    """校验消息序列的结构合法性：角色合法 + assistant 工具调用与 toolResult 配对。

    第七阶段收口：**不能只检查消息角色**。assistant 消息里的 `toolCall`（嵌在
    content 数组、`type=='toolCall'`）必须被 `toolResult`（`toolCallId == id`）
    覆盖；否则说明「工具结果已提交但 Node 未吸收」，属于恢复决策表要处理的
    场景。这里只做结构合法性判断，不做恢复决策。
    """
    if not isinstance(messages, list):
        return False
    for item in messages:
        if not isinstance(item, dict):
            return False
        role = str(item.get('role') or '')
        if role not in _MESSAGE_ROLES:
            return False
    # 配对检查：每个 assistant toolCall 都要有对应 toolResult
    absorbed = tool_result_ids(messages)
    for call in assistant_tool_calls(messages):
        if call['id'] and call['id'] not in absorbed:
            return False
    return True
