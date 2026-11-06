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
RESUME_WAIT = 'resume_wait'            # 已受理的外部任务未完成：继续等待，不重新推理


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
    # 已受理外部任务完成后、待注入 Node 的最终结果（RESUME_DELIVER 时非空，
    # 第八阶段收口第 3 点）。每项：{call_id, tool_name, result, is_error}，
    # Node 据此 pushToolResult 后继续推理，**不重新派发**（结果已拿到）。
    deliver_results: list[dict] = None
    # 说明（排障用）
    reason: str = ''

    def __post_init__(self) -> None:
        if self.tool_results is None:
            self.tool_results = []
        if self.pending_calls is None:
            self.pending_calls = []
        if self.deliver_results is None:
            self.deliver_results = []

    def to_dict(self) -> dict:
        """转成可 JSON 序列化的 dict（恢复事实随 pi_resume 进入 state 落盘用）。"""
        return {
            'kind': self.kind,
            'tool_results': list(self.tool_results),
            'final_answer': self.final_answer,
            'pending_calls': list(self.pending_calls),
            'deliver_results': list(self.deliver_results),
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


# 外部任务开放状态（仍在等远端结果）。
_OPEN_EXTERNAL = ('accepted', 'running', 'materializing')


def decide_external(snapshot: dict | None, external_tasks: list[dict]) -> RecoveryDecision:
    """按「已受理外部任务」的状态产出恢复决策（第八阶段第 5 点）。

    恢复时**先判断外部任务**，不能把普通 pending 重派发规则套到已受理任务上：

    - 还有开放（accepted/running/materializing）的外部任务 → `RESUME_WAIT`：
      继续等待，不重新推理、不重派发，不重复提交；
    - 已受理但外部任务已 `completed` → 由宿主按原 toolCall ID 注入一次
      toolResult 再继续（见宿主吸收路径，这里返回 `RESUME_DELIVER` 的补交事实
      由宿主另行装配）；
    - 外部任务 `failed` / `cancelled` / `unknown` / 缺失 → `RESUME_RECONCILE`：
      转待核实，不盲目重提。

    无 `awaiting_calls`（快照没有受理事实）时返回 None（交给常规 decide）。
    """
    awaiting = list((snapshot or {}).get('awaiting_calls') or [])
    if not awaiting:
        return None
    # 第八阶段收口第 1 点：本地匹配主键是 external_task_id + action_id +
    # logical_call_id；remote_task_id 仅远端查询身份，不能单独用于本地匹配。
    # 按这三类键各建一张「完整任务 dict」索引（不只 state，还需 last_result_id /
    # output_manifest_ref 供交付装配），优先 external_task_id，退回 action_id，
    # 再退回 logical_call_id。
    by_ext = {str(t.get('external_task_id') or ''): t
              for t in (external_tasks or []) if isinstance(t, dict)}
    by_action = {str(t.get('action_id') or ''): t
                 for t in (external_tasks or []) if isinstance(t, dict)}
    by_logical = {str(t.get('step_id') or ''): t
                  for t in (external_tasks or []) if isinstance(t, dict)}
    any_open = False
    any_terminal_bad = False   # 有 failed / cancelled / 其它非 completed 终态
    to_deliver: list[dict] = []
    # 第八阶段收口第 3 点：用「messages 里是否已有该 call_id 的 toolResult」作为
    # 吸收水位。已吸收的 completed 结果**不再重复交付**——重复恢复不重复追加结果。
    from webpage_agent.agent import pi_snapshot as _snap
    absorbed_ids = _snap.tool_result_ids((snapshot or {}).get('messages') or [])
    for item in awaiting:
        if not isinstance(item, dict):
            continue
        task = None
        for key, index in (
                (item.get('external_task_id'), by_ext),
                (item.get('action_id'), by_action),
                (item.get('logical_call_id'), by_logical),
                (item.get('step_id'), by_logical)):
            if key and str(key) in index:
                task = index[str(key)]
                break
        if task is None:
            # 第八阶段收口第 1 点：缺少关联记录 → 转待核实，**不能**当「继续
            # 等待」。否则登记丢失/错位时 run 会永久挂在 waiting_external。
            return RecoveryDecision(kind=RESUME_RECONCILE,
                                    reason='有受理事实但缺少外部任务关联记录，需核实')
        state = str(task.get('state') or '')
        if state in _OPEN_EXTERNAL:
            any_open = True
        elif state == 'completed':
            call_id = str(item.get('call_id') or '')
            # 已吸收（messages 里有对应 toolResult）→ 不再交付。
            if call_id and call_id in absorbed_ids:
                continue
            to_deliver.append({
                'call_id': call_id,
                'logical_call_id': str(item.get('logical_call_id') or ''),
                'tool_name': str(item.get('tool_name') or ''),
                'external_task_id': str(task.get('external_task_id') or ''),
                'action_id': str(task.get('action_id') or ''),
                'last_result_id': str(task.get('last_result_id') or ''),
                'output_manifest_ref': str(task.get('output_manifest_ref') or ''),
            })
        else:
            # failed / cancelled / 未知终态 → 结果不可交付，需核实。
            any_terminal_bad = True
    if any_open:
        return RecoveryDecision(kind=RESUME_WAIT, pending_calls=[],
                                reason='已受理的外部任务尚未完成，继续等待')
    if to_deliver:
        # 第八阶段收口第 4 点：外部结果交付时，**同时带上**剩余未派发的同步调用
        # （快照 pending_calls 里那些还没 accepted 的候选）。恢复时 Node 先注入
        # 外部结果（deliver_results），再按原顺序重派发这些 pending——不重新生成
        # 候选、不丢失调用。
        pending = list((snapshot or {}).get('pending_calls') or [])
        return RecoveryDecision(kind=RESUME_DELIVER, tool_results=to_deliver,
                                pending_calls=pending,
                                reason='外部任务已完成，注入结果后按原顺序处理剩余调用')
    if any_terminal_bad:
        return RecoveryDecision(kind=RESUME_RECONCILE,
                                reason='外部任务结束但结果未定（失败/取消），需核实')
    # 没有待交付（可能已全部吸收）且没有开放/异常任务：交给常规 decide 收尾。
    return RecoveryDecision(kind=RESUME_ADMIT, pending_calls=[],
                            reason='外部任务结果已全部吸收，继续常规恢复')
