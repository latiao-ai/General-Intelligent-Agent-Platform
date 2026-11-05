"""Pi 恢复决策：按数据库事实决定恢复行为（第七阶段）。

Pi 恢复与 LangGraph 不同：它没有「图计划 + 步骤游标」，恢复的权威事实来自

- `runs`（状态、租约、要求版本）；
- `actions`（工具调用的准入/结算状态）；
- `run_snapshots`（版本化 Pi 快照：阶段、检查点序号、完整模型消息、
  逻辑调用序号、待处理调用、已提交工具结果引用、最终答复候选）。

决策表（与第七阶段规格一致）：

| 数据库事实 | 恢复行为 |
|---|---|
| 调用候选已存，尚未准入 | 使用原身份继续准入 |
| 动作在途、没有确定结果 | 转待核实；不能推断未执行 |
| 工具结果已提交，Node 未吸收 | 补入对应 toolResult，再继续 Pi |
| 最终答复已提交，run 未收尾 | 只补收尾，不重新推理或调用 |

对结果未知的只读调用也**保守阻断**：不推断「未执行」从而重试；若确有重试
策略，必须显式记录新尝试，绝不把重试结果称作「成功重放」。

本模块只产出**决策**（一个可枚举的结果），不执行副作用；执行由 `pi_runtime`
或 Hub 按决策落地。
"""
from __future__ import annotations

from dataclasses import dataclass

# 恢复决策的可枚举结果。
RESUME_ADMIT = 'resume_admit'          # 候选已存、尚未准入：沿用原身份继续准入
RESUME_RECONCILE = 'resume_reconcile'  # 动作在途/未知：转待核实
RESUME_DELIVER = 'resume_deliver'      # 结果已提交、Node 未吸收：补 toolResult 再继续
RESUME_FINISH = 'resume_finish'        # 最终答复已提交、run 未收尾：只补收尾
RESUME_BLOCK = 'resume_block'          # 结果未知的只读调用：保守阻断


@dataclass
class RecoveryDecision:
    """一次恢复决策：kind + 供执行方使用的恢复事实。"""
    kind: str
    # 需要补入 Pi 上下文的 toolResult 消息（RESUME_DELIVER 时非空）
    tool_results: list[dict] = None
    # 已提交的最终答复候选（RESUME_FINISH 时非空）
    final_answer: str | None = None
    # 需要继续准入的候选调用（RESUME_ADMIT 时非空）
    pending_calls: list[dict] = None
    # 说明（排障用）
    reason: str = ''

    def __post_init__(self) -> None:
        if self.tool_results is None:
            self.tool_results = []
        if self.pending_calls is None:
            self.pending_calls = []

    def to_dict(self) -> dict:
        """转成可 JSON 序列化的 dict（恢复事实随 pi_resume 进入 state 落盘用）。"""
        return {
            'kind': self.kind,
            'tool_results': list(self.tool_results),
            'final_answer': self.final_answer,
            'pending_calls': list(self.pending_calls),
            'reason': self.reason,
        }


def _action_status(action: dict) -> str:
    return str(action.get('status') or '')


def decide(snapshot: dict | None, actions: list[dict], run_status: str) -> RecoveryDecision:
    """按数据库事实产出 Pi 恢复决策。

    参数：

    - `snapshot`：版本化 Pi 快照（已 `pi_snapshot.parse`，或 None 表示首次执行）；
    - `actions`：该 run 的 actions 列表（含 status / receipt / step_id）；
    - `run_status`：`runs.status`（succeeded / running / paused / interrupted …）。

    返回 `RecoveryDecision`。首次执行（无快照、无动作）返回 `RESUME_ADMIT`
    且 pending_calls 为空——表示「没有要恢复的候选，正常从头开始」。
    """
    if snapshot is None:
        # 首次执行：没有快照就没有候选，正常开始（不阻断）。
        return RecoveryDecision(kind=RESUME_ADMIT, pending_calls=[],
                                reason='首次执行，无快照')

    # 1. 最终答复已提交、run 未收尾 → 只补收尾
    final_answer = snapshot.get('final_answer_candidate')
    if final_answer and run_status not in ('succeeded', 'failed', 'cancelled'):
        return RecoveryDecision(kind=RESUME_FINISH, final_answer=str(final_answer),
                                reason='最终答复已提交但 run 未收尾，只补收尾')

    # 2. 动作在途/未知 → 转待核实（不能推断未执行）
    for action in actions:
        status = _action_status(action)
        if status in ('in_flight', 'unknown'):
            return RecoveryDecision(kind=RESUME_RECONCILE,
                                    reason=f'动作 {action.get("action_id")} 状态 {status}，结果未定，需核实')

    # 3. 工具结果已提交、Node 未吸收 → 补 toolResult 再继续
    #    判断依据：committed_tool_results 里有结果，但快照 messages 里没有
    #    对应的 toolResult（按 toolCallId 配对，Node 没吸收）。
    committed = list(snapshot.get('committed_tool_results') or [])
    if committed:
        call_id_by_logical = _call_id_by_logical(snapshot)
        to_deliver: list[dict] = []
        for ref in committed:
            if not isinstance(ref, dict):
                continue
            if not _absorbed(ref, snapshot.get('messages') or [],
                             call_id_by_logical):
                to_deliver.append(ref)
        if to_deliver:
            return RecoveryDecision(kind=RESUME_DELIVER, tool_results=to_deliver,
                                    reason='工具结果已提交但 Node 未吸收，补 toolResult 再继续')

    # 4. 调用候选已存、尚未准入 → 沿用原身份继续准入
    pending = list(snapshot.get('pending_calls') or [])
    if pending:
        return RecoveryDecision(kind=RESUME_ADMIT, pending_calls=pending,
                                reason='调用候选已存、尚未准入，沿用原身份继续准入')

    # 没有未完成事实：正常继续（或已收尾）。
    return RecoveryDecision(kind=RESUME_ADMIT, pending_calls=[],
                            reason='无未完成的恢复事实，继续执行')


def _absorbed(ref: dict, messages: list, call_id_by_logical: dict) -> bool:
    """判断一个已提交工具结果是否已被 Node 吸收（messages 里有对应 toolResult）。

    第七阶段收口：配对键是**实际字段** `ToolResultMessage.toolCallId ==
    ToolCall.id`，不是消息角色、不是 action_id/step_id 猜。逻辑调用 ID
    （ref['step_id']）先经 pending_calls 映射到桥接 call_id，再比对 messages
    里的 `toolResult.toolCallId`。
    """
    from webpage_agent.agent import pi_snapshot
    step_id = str(ref.get('step_id') or '')
    # 逻辑 ID → 桥接 call_id（无映射时退回 step_id 本身，兼容旧数据）
    call_id = call_id_by_logical.get(step_id, step_id)
    absorbed_ids = pi_snapshot.tool_result_ids(messages)
    return bool(call_id) and call_id in absorbed_ids


def _call_id_by_logical(snapshot: dict) -> dict:
    """从快照 pending_calls 建立 logical_call_id → call_id 映射（唯一结构）。"""
    mapping: dict = {}
    for item in (snapshot.get('pending_calls') or []):
        if not isinstance(item, dict):
            continue
        logical = str(item.get('logical_call_id') or '')
        call_id = str(item.get('call_id') or '')
        if logical and call_id:
            mapping[logical] = call_id
    return mapping


def conservative_block(action: dict) -> bool:
    """结果未知的只读调用：保守阻断。

    只读（effect == 'read'）但结果未知（unknown）的动作，宁可停下核实，也
    不推断「未执行」后重试——重试可能把一次昂贵的检索重复执行。返回 True 表示
    应当阻断；若确有重试策略，调用方必须显式记录新尝试。
    """
    effect = str(action.get('effect') or '')
    status = _action_status(action)
    return effect == 'read' and status == 'unknown'
