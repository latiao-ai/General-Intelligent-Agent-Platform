"""一次受治理的工具调用：授权、准入、撤权复查、真实调用、结果归一、结算。

这是从 `agent/nodes.py` 的 `run_step` 里**抽取**出来的「单次执行边界」，供
LangGraph 图（`run_step`）与 Pi 运行时（`pi_runtime`）**共同复用**。两者都必须
走同一条治理链：控制检查 → 授权 → 调用预算 → 准入（`ActionLedger.admit`）→
撤权复查 → 真实调用 → 结果归一 → 结算（`ActionLedger.settle`）。

为什么抽出来：以前这套逻辑只活在 `run_step` 里，Pi 要接真实工具就得再抄一遍，
两处账本、两处授权、两处结算迟早分叉——"模型看不见但远端被调用了"或者
"结算漏了"这类事故就再也说不清是谁放行的。

边界：

- 这里**只负责一次受治理调用**，不认识计划、步骤游标、依赖解析、图路由、
  补偿编排；那些属于调用方（`run_step` 或 `pi_runtime`）的编排职责；
- 可信主体（`ToolCallContext`）、账本（`ActionLedger`）、执行权（`ExecutionContext`）
  由 Python 持有并通过 ContextVar 传入，**不**从 Node / 模型输出取授权依据；
- 返回一个结构化结果 `GovernedResult`，由调用方决定如何映射回自己的状态
  （图节点返回 updates，Pi 返回回执）。

第六阶段（共享网关）补齐的合同，以 `run_step` 现有行为为兼容基准：

- **准入只有明确 `admitted` 才调用**：`no_run`、`blocked`、`in_flight`、`unknown`、
  以及任何「返回了一个字典但状态不是 admitted」的情况一律阻断，绝不落到真实调用；
- **真实调用前必须持有有效 run / 租约 / 执行上下文 / 账本**：正式执行路径
  缺任何一个就拒绝（返回 `kind='no_run'` / `denied`），不伪造主体；
- **结果归一落盘**：完整结果走 `files.save_result` 落 `results/`，返回
  `ToolResult`（含 `data_ref`、摘要、分页、证据引用），不把 list/dict 丢掉；
- **结算补齐**：`receipt` 保留完整可恢复内容（含 list/dict），并随动作一次提交
  步骤状态、快照（`state`）与 `next_step_id`——不先宣布成功再补写关键恢复状态。

不使用本模块也不影响旧路径：`run_step` 改为调用它后行为保持不变，旧测试继续通过。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from webpage_agent.core import files as files_module
from webpage_agent.core.config import settings
from webpage_agent.governance import execution_control
from webpage_agent.observe import errors as errors_module
from webpage_agent.observe import observation as observation_module
from webpage_agent.observe.events import action, emit
from webpage_agent.store import ledger as ledger_module
from webpage_agent.tools import tool_authorization as authorization_module
from webpage_agent.tools import tool_context as tool_context_module

logger = logging.getLogger(__name__)


# ---------- 可信执行主体（从 run_step 迁移，供两处复用） ----------

def state_intent_version(run: dict) -> int:
    """run 行上的要求版本。取不到就当 0（"没有版本约束"）。"""
    try:
        return int((run or {}).get('intent_version') or 0)
    except (TypeError, ValueError):
        return 0


async def tool_context(step_id: str = '', action_id: str = ''):
    """构造本次调用的**可信执行主体**。

    主体来自**库里已提交的 run 行**（不是模型输出、不是请求头里的自报字段）：
    租户、用户、要求版本、计划版本都在 run 上，Worker 与 API 就地执行
    拿到的是同一份。

    没有账本（CLI 直调、单元测试）时返回 None：调用点据此走兼容路径，
    而不是伪造一个"看起来像真的"主体。
    """
    book = ledger_module.current_ledger()
    context_ = execution_control.require()
    if book is None or context_ is None:
        return None
    store = getattr(book, 'store', None)
    if store is None:
        return None
    try:
        run = await store.run(context_.run_id)
    except Exception:                                # noqa: BLE001
        logger.warning('读取 run 失败 run=%s', context_.run_id, exc_info=True)
        return None
    if not run:
        return None
    user_id = str(run.get('owner_user_id') or '') or 'default'
    role = str(run.get('owner_role') or '') or 'member'
    return tool_context_module.from_run(
        run, user_id=user_id, role=role, step_id=step_id, action_id=action_id,
        intent_version=int(state_intent_version(run)),
        plan_version=int(book.plan_version or 1),
        authenticated=bool(run.get('tenant_id')) and user_id != 'default')


def plan_intent(state: dict) -> int | None:
    """这份计划的依据版本。**只取已提交计划里的值**，不取模型自报的版本。"""
    value = (state.get('plan') or {}).get('intent_version')
    return int(value) if value else None


async def check_boundary(state: dict) -> str:
    """每步准入前的控制检查：暂停/取消优先于继续执行。

    返回空串表示可以继续；抛 ControlStopped / IntentChanged 表示应当停下。
    """
    context = execution_control.require()
    book = ledger_module.current_ledger()
    if context is None or book is None:
        return ''
    try:
        control = await book.store.control_state(context.run_id)
    except Exception as exc:                           # noqa: BLE001
        logger.exception('安全边界读取控制状态失败，本次放行 run=%s', context.run_id)
        state.setdefault('errors', []).append({
            'stage': 'boundary', 'type': type(exc).__name__,
            'error_code': getattr(exc, 'error_code', 'E_CONTROL_STATE_UNAVAILABLE'),
            'message': '边界检查读不到控制请求，本步已放行（取消/暂停可能被忽略）'})
        return ''
    if control.get('cancel_requested_at'):
        raise execution_control.ControlStopped('运行已被取消', 'cancelled')
    if control.get('pause_requested'):
        raise execution_control.ControlStopped('已请求暂停', 'paused')
    plan_version_intent = (state.get('plan') or {}).get('intent_version')
    current = int(control.get('intent_version') or 1)
    if plan_version_intent is not None and int(plan_version_intent) != current:
        raise execution_control.IntentChanged(
            context.run_id, int(plan_version_intent), current)
    return ''


async def open_approval(book, ctx, *, tool_key: str, args: dict) -> str:
    """把待确认清单写入批准表。失败时返回空串，调用本身仍然停在等待批准。"""
    if book is None or getattr(book, 'store', None) is None:
        return ''
    from webpage_agent.governance import approvals as approvals_module
    from webpage_agent.tools import tool_policy as policy_module
    tenant_id = getattr(ctx, 'tenant_id', None) or 'default'
    user_id = getattr(ctx, 'user_id', None) or 'default'
    args_hash = policy_module.canonical_hash(args or {})
    manifest = approvals_module.ApprovalManifest(items=[{
        'key': 'call',
        'tool_key': tool_key,
        'args_hash': args_hash,
        'resource_scope': '',
        'units': 1,
    }], expires_at=time.time() + settings.approval_ttl_seconds,
        max_actions=1)
    try:
        row = await book.store.create_approval(
            tenant_id=tenant_id, owner_user_id=user_id,
            requester_user_id=user_id, run_id=book.run_id,
            manifest={'items': manifest.items, 'max_actions': manifest.max_actions,
                      'expires_at': manifest.expires_at, 'limits': manifest.limits},
            manifest_hash=manifest.hash(),
            expires_at=manifest.expires_at)
    except Exception:                                  # noqa: BLE001
        logger.warning('写入批准申请失败', exc_info=True)
        return ''
    return str(row.get('approval_id') or '')


# ---------- 一次受治理调用的结果 ----------

@dataclass
class GovernedResult:
    """一次受治理调用的结构化结果。

    `kind` 区分结局：

    - `succeeded`：真实调用完成并已结算（成功）；
    - `replay`：该动作此前已成功，复用已有回执（未再次调用）；
    - `blocked`：结果未定（in_flight / unknown），必须核实，未调用；
    - `no_run`：没有有效 run / 租约 / 执行上下文 / 账本，拒绝调用；
    - `needs_approval`：需要批准，未调用；
    - `denied`：授权被拒或撤权，未调用（远端调用计数为零）；
    - `failed`：调用失败（已按确定性结算为 failed / unknown）。

    `updates` 是工具返回的 state 增量（成功时非空）；`receipt` 是结算回执
    （保留完整内容，含 list/dict）；`tool_result` 是归一后的 `ToolResult`
    （含 data_ref / 摘要 / 分页 / 证据引用），供观察层与宿主复用。
    """
    kind: str
    action_id: str = ''
    attempt_id: str = ''
    updates: dict = field(default_factory=dict)
    receipt: dict = field(default_factory=dict)
    replay: dict | None = None
    tool_result: dict = field(default_factory=dict)
    data_ref: str = ''
    approval_id: str = ''
    error: str = ''
    error_code: str = ''
    certainty: str = 'not_executed'
    effect: str = 'unknown'
    # 结算是否已经发生（succeeded/failed 时为 True；denied/blocked/no_run/needs_approval 为 False）
    settled: bool = False
    # 原始异常（failed 时保留，供调用方做 read 工具可恢复失败的精确区分）
    exc: Any = None


def _result_payload(updates: dict):
    """挑出结果里值得完整保存的部分（直接回执优先，其次整包）。"""
    for key in ('payload', 'result', 'response', 'data', 'mcp_calls'):
        if key in updates:
            return updates[key]
    return {key: value for key, value in updates.items()
            if isinstance(value, (str, int, float, bool, dict, list))}


def build_tool_result(state: dict, *, step_id: str, tool_key: str,
                      action_id: str = '', attempt_id: str = '', payload=None,
                      marker: str = '', execution_status: str = 'succeeded',
                      error: dict | None = None, tool_name: str = '') -> tuple[dict, str]:
    """把一次调用归一成 `ToolResult`，并把完整结果落到本 run 的产物目录。

    返回 `(tool_result, data_ref)`。完整结果落盘的理由：观察需要真实清单/嵌套
    结构，摘要只用来喂模型。落盘失败不影响执行——只是这条结果不能"按引用再读"，
    摘要仍然可用。
    """
    data_ref = ''
    folder = str(state.get('artifact_dir') or '')
    if folder and payload is not None:
        try:
            data_ref = files_module.save_result(
                folder, f'{step_id}-{action_id or "result"}', payload)
        except Exception:                                # noqa: BLE001
            logger.warning('保存工具完整结果失败 step=%s', step_id, exc_info=True)
            data_ref = ''
    artifact = state.get('artifact') or {}
    artifact_refs = []
    path = artifact.get('path') or artifact.get('html_path')
    if path:
        artifact_refs.append(str(path))
    deployment = state.get('deployment') or {}
    receipts = []
    if deployment.get('public_url'):
        receipts.append(str(deployment['public_url']))
    if deployment.get('site_id'):
        receipts.append(str(deployment['site_id']))
    result = observation_module.build_result(
        step_id=step_id, tool_key=tool_key, tool_name=tool_name,
        action_id=action_id, attempt_id=attempt_id, payload=payload,
        execution_status=execution_status, data_ref=data_ref,
        artifact_refs=artifact_refs, external_receipt_refs=receipts,
        phase=('completed' if execution_status == 'succeeded' else ''),
        error=error or {})
    return result, data_ref


async def governed_call(
    *,
    tool,
    args: dict,
    step_id: str,
    state: dict,
    ctx=None,
    executor=None,
    next_step_id: str = '',
    snapshot_state: dict | None = None,
    build_snapshot=None,
    require_ledger: bool = True,
) -> GovernedResult:
    """执行一次受治理的工具调用。

    参数：

    - `tool`：`Tool` 对象（含 name/tool_key/effect/run）；
    - `args`：已解析的参数；
    - `step_id`：步骤标识（进账本幂等键）；
    - `state`：当前执行状态（含 task_id / artifact_dir / documents 等，传给真实调用）；
    - `ctx`：可信执行主体（None 表示没有可信主体，正式路径拒绝）；
    - `executor`：可选的 `async (tool, args, state) -> updates` 覆盖 `tool.run`
      （Pi 注入用；默认用 tool.run）；
    - `next_step_id`：下一个步骤 ID（结算时写入快照，用于恢复）；
    - `snapshot_state`：待提交的静态快照（None 则不写快照）；
    - `build_snapshot`：可选的 `(tool_updates: dict) -> dict | None` 回调，在真实
      调用成功、结算之前被调用，用于**构造包含工具增量与游标推进的完整快照**
      （run_step 用它把 step_outputs / step_index 一起落进快照）。返回 None 表示
      不写快照。`build_snapshot` 优先于 `snapshot_state`；
    - `require_ledger`：是否要求有账本。默认 True（正式路径：无账本返回 no_run）。
      设 False 时，无账本走「直接真实调用 + 结果归一、不结算」的兼容路径——仅
      用于 CLI 直调 / 单元测试 / 无治理 smoke 的独立测试路径，**不是**生产路径。

    复用 run_step 的同一条链：控制检查 → 授权 → 调用预算 → 准入 → 撤权复查 →
    真实调用 → 结果归一 → 结算。**不复制账本**——准入与结算都走当前账本。

    准入纪律（第六阶段）：只有 `admit` 明确返回 `status == 'admitted'` 且拿到
    action_id / attempt_id 才实际调用；`no_run`、`blocked`、`in_flight`、`unknown`
    以及任何未知返回全部阻断，绝不落到真实调用。
    """
    name = getattr(tool, 'name', '')
    tool_key = getattr(tool, 'tool_key', '') or name
    effect = getattr(tool, 'effect', 'unknown')
    book = ledger_module.current_ledger()

    # 控制检查（暂停/取消/版本变化在准入前）
    await check_boundary(state)

    # 无账本：正式路径（require_ledger=True）一律拒绝；require_ledger=False 时
    # 走「直接真实调用 + 结果归一、不结算」的兼容路径（CLI/单测/无治理 smoke）。
    if book is None:
        if require_ledger:
            return GovernedResult(
                kind='no_run', error='没有执行账本，拒绝真实工具调用',
                error_code='E_NO_LEDGER', certainty='not_executed', effect=effect)
        # 兼容路径：直接调用，结果归一，不结算
        try:
            updates = await (executor(tool, args, state)
                             if executor is not None else tool.run(args, state))
        except execution_control.LeaseLost:
            raise
        except Exception as exc:                       # noqa: BLE001
            outcome = errors_module.as_outcome(exc)
            return GovernedResult(
                kind='failed', error=outcome.safe_message or type(exc).__name__,
                error_code=outcome.error_code, certainty=outcome.certainty,
                effect=effect, settled=False, exc=exc)
        updates = dict(updates) if isinstance(updates, dict) else {}
        marker = str(updates.pop('status', '') or '')
        tool_result, data_ref = build_tool_result(
            state, step_id=step_id, tool_key=tool_key, tool_name=name,
            action_id='', attempt_id='',
            payload=_result_payload(updates), marker=marker,
            execution_status='succeeded')
        if marker:
            updates['status'] = marker
        return GovernedResult(
            kind='succeeded', action_id='', attempt_id='',
            updates=updates, effect=effect, settled=False,
            tool_result=tool_result, data_ref=data_ref)

    action_id, attempt_id = '', ''

    # 调用预算：工具调用次数进运行预算
    try:
        await book.store.consume_budget(
            book.context, run_id=book.run_id, key='tool_calls',
            limit=settings.run_max_tool_calls)
    except Exception as exc:                       # noqa: BLE001
        hint = getattr(exc, 'safe_message', None) or type(exc).__name__
        emit(action('调用预算用尽', hint, '⚠️'))
        return GovernedResult(
            kind='failed', error=hint, error_code='E_RUN_BUDGET',
            certainty='not_executed', effect=effect)

    # 授权（先授权再准入；拒绝/等待批准都还没有 in_flight）
    # 没有可信主体（ctx 为 None）时 authorize_call 依据缺失 → 拒绝，不伪造主体。
    try:
        decision = await authorization_module.authorize_call(
            book.store, ctx, tool_key=tool_key, effect=effect, args=args)
    except authorization_module.ApprovalRequired as exc:
        approval_id = await open_approval(
            book, ctx, tool_key=tool_key, args=args)
        emit(action('等待批准', exc.safe_message, '⏸️'))
        return GovernedResult(
            kind='needs_approval', approval_id=approval_id,
            error=exc.safe_message, error_code=getattr(exc, 'error_code', 'E_APPROVAL_REQUIRED'),
            certainty='not_executed', effect=effect)
    except authorization_module.AuthorizationDenied as exc:
        emit(action('没有调用资格', exc.safe_message, '⛔'))
        return GovernedResult(
            kind='denied', error=exc.safe_message,
            error_code=getattr(exc, 'error_code', 'E_POLICY_DENIED'),
            certainty='not_executed', effect=effect)

    # 授权留痕（authorization dict）
    authorization = None
    if settings.tool_policy_enforced and decision is not None and ctx is not None:
        from webpage_agent.tools import tool_policy as policy_module
        credential = decision.credential
        authorization = {
            'tenant_id': ctx.tenant_id,
            'user_id': ctx.user_id,
            'tool_key': tool_key,
            'args_hash': policy_module.canonical_hash(args or {}),
            'credential_binding': credential.binding_id if credential else '',
            'credential_subject': credential.subject_id if credential else '',
            'credential_revision': credential.revision if credential else 0,
            'policy_revision': decision.policy.revision if decision.policy else 0,
            'approval_id': decision.approval_id,
            'approval_item_key': decision.approval_item_key or 'call',
        }

    # 准入：只有明确 admitted 才调用
    try:
        admitted = await book.admit(
            step_id=step_id, tool_key=tool_key, effect=effect, args=args,
            expected_intent_version=plan_intent(state),
            authorization=authorization)
    except execution_control.IntentChanged:
        # 旧要求版本：准入事务里版本不等抛 IntentChanged，未调用
        emit(action('要求已更新', f'{tool.label} 尚未派发', '📝'))
        raise
    except execution_control.ControlStopped as exc:
        emit(action('停止派发', exc.reason, '⏸️'))
        raise
    except execution_control.LeaseLost as exc:
        emit(action('执行权已丢失', str(exc), '⛔'))
        raise

    admitted_status = admitted.get('status')
    if admitted_status == 'blocked' or admitted.get('blocked'):
        emit(action('该步骤结果未定',
                    f'{tool.label}（{admitted.get("blocked") or admitted_status}），需核实后再继续', '❓'))
        return GovernedResult(
            kind='blocked', error=str(admitted.get('blocked') or admitted_status),
            certainty='unknown', effect=effect)
    if admitted_status == 'no_run':
        emit(action('没有可执行的运行', f'{tool.label} 无有效 run/租约', '⛔'))
        return GovernedResult(
            kind='no_run', error='没有可执行的运行（run 缺失或租约失效）',
            error_code='E_NO_RUN', certainty='not_executed', effect=effect)
    if admitted_status == 'succeeded' or admitted.get('replay') is not None:
        emit(action('复用已有结果', f'{tool.label}（该动作此前已成功）', '♻️'))
        return GovernedResult(kind='replay', replay=admitted.get('replay'),
                              effect=effect)
    if admitted_status not in ('admitted', 'prepared'):
        # in_flight / unknown / 其它未知状态：一律阻断，不落到真实调用
        emit(action('该步骤结果未定',
                    f'{tool.label}（{admitted_status}），需核实后再继续', '❓'))
        return GovernedResult(
            kind='blocked', error=str(admitted_status or '未知准入状态'),
            certainty='unknown', effect=effect)
    if admitted_status == 'prepared':
        # 旧版本遗留的 prepared 行：仓库层已把它重新 stamp 成 in_flight 并返回
        # admitted（见 admit_action 的 prepared 分支），这里不会走到；防御性兜底。
        pass

    action_id = admitted.get('action_id', '')
    attempt_id = admitted.get('attempt_id', '')
    if not action_id or not attempt_id:
        # 准入返回 admitted 却没给身份：异常，阻断
        emit(action('准入未分配执行身份', f'{tool.label}', '⛔'))
        return GovernedResult(
            kind='blocked', error='准入未分配 action/attempt 身份',
            error_code='E_NO_ACTION_ID', certainty='unknown', effect=effect)

    # 撤权复查
    if ctx is not None and decision is not None:
        try:
            await authorization_module.check_revocations(
                book.store, decision, tool_key=tool_key,
                tenant_id=ctx.tenant_id, user_id=ctx.user_id,
                role=ctx.role, platform_id=ctx.platform_id)
        except authorization_module.AuthorizationDenied as exc:
            if action_id:
                await book.fail_from_exception(
                    step_id=step_id, action_id=action_id,
                    attempt_id=attempt_id, exc=exc, effect=effect)
                if decision is not None and decision.approval_id:
                    try:
                        await book.store.release_approval_reservation(
                            approval_id=decision.approval_id, action_id=action_id,
                            manifest_item_key=decision.approval_item_key or 'call')
                    except Exception:          # noqa: BLE001
                        logger.warning('释放批准预留失败 action=%s', action_id,
                                       exc_info=True)
            emit(action('权限已撤销', exc.safe_message, '⛔'))
            return GovernedResult(
                kind='denied', action_id=action_id, error=exc.safe_message,
                error_code=getattr(exc, 'error_code', 'E_POLICY_REVOKED'),
                certainty='not_executed', effect=effect, settled=True)

    if ctx is not None and action_id and authorization is None:
        await authorization_module.record_authorization(
            book.store, ctx, tool_key=tool_key, action_id=action_id,
            args=args, decision=decision)

    # 真实调用
    try:
        if executor is not None:
            updates = await executor(tool, args, state)
        else:
            updates = await tool.run(args, state)
    except execution_control.LeaseLost:
        raise
    except Exception as exc:                           # noqa: BLE001
        outcome = errors_module.as_outcome(exc)
        hint = outcome.safe_message or type(exc).__name__
        if book is not None and action_id:
            await book.fail_from_exception(
                step_id=step_id, action_id=action_id, attempt_id=attempt_id,
                exc=exc, effect=effect, state=snapshot_state,
                next_step_id=next_step_id)
            # 确认未执行（not_executed）才释放批准预留：unknown 不允许释放
            # （可能已花掉额度，不能放给第二个动作再花一次）。
            if (decision is not None and decision.approval_id
                    and outcome.certainty == 'not_executed'):
                try:
                    await book.store.release_approval_reservation(
                        approval_id=decision.approval_id, action_id=action_id,
                        manifest_item_key=decision.approval_item_key or 'call')
                except Exception:                  # noqa: BLE001
                    logger.warning('释放批准预留失败 action=%s', action_id,
                                   exc_info=True)
        # 归一一个失败的 ToolResult 供观察层读取（完整错误也落盘）
        failed_result, failed_ref = build_tool_result(
            state, step_id=step_id, tool_key=tool_key, tool_name=name,
            action_id=action_id, attempt_id=attempt_id, payload=None,
            marker='failed',
            execution_status='unknown' if outcome.certainty == 'unknown' else 'failed',
            error={'code': outcome.error_code, 'phase': outcome.phase,
                   'execution_certainty': outcome.certainty, 'message': hint})
        return GovernedResult(
            kind='failed', action_id=action_id, attempt_id=attempt_id,
            error=hint, error_code=outcome.error_code,
            certainty=outcome.certainty, effect=effect,
            settled=(book is not None and bool(action_id)),
            tool_result=failed_result, data_ref=failed_ref, exc=exc)

    updates = dict(updates) if isinstance(updates, dict) else {}

    # 结果归一：先构造可恢复结果（ToolResult + data_ref 落盘），再结算。
    # 工具返回的 `status` 是阶段小结（searched / fetched / rendered / deployed），
    # 不是 run 终态：run 终态由宿主的 outcome 统一判定。
    marker = str(updates.pop('status', '') or '')
    async_accepted = bool(updates.pop('async_accepted', False))
    remote_task_id = str(updates.pop('remote_task_id', '') or '')
    contract_id = str(updates.pop('contract_id', '') or '')
    contract_version = updates.pop('contract_version', 1)
    updates.pop('execution_status', None)
    updates.pop('phase', None)

    tool_result, data_ref = build_tool_result(
        state, step_id=step_id, tool_key=tool_key, tool_name=name,
        action_id=action_id, attempt_id=attempt_id,
        payload=_result_payload(updates), marker=marker,
        execution_status='accepted' if async_accepted else 'succeeded')
    if async_accepted:
        tool_result['phase'] = 'accepted'
        tool_result['remote_task_id'] = remote_task_id
        tool_result['contract_id'] = contract_id
        tool_result['contract_version'] = contract_version
    if marker:
        updates['status'] = marker

    # 结算（成功）：动作 + attempt + 步骤 + 快照一次提交。receipt 保留完整内容
    # （含 list/dict），不丢掉来源清单等可恢复数据。结算失败向上抛，不推进游标。
    receipt = {k: v for k, v in updates.items()
               if isinstance(v, (str, int, float, bool, dict, list))}
    receipt['tool_result'] = tool_result
    # 构造待提交快照：build_snapshot 回调优先（可包含工具增量 + 游标推进），
    # 否则用静态 snapshot_state。在真实调用后、结算前构造，避免「先宣布成功、
    # 再补写关键恢复状态」。
    snapshot = None
    if build_snapshot is not None:
        snapshot = build_snapshot(updates)
    elif snapshot_state is not None:
        snapshot = snapshot_state
    if book is not None and action_id:
        settled = await book.settle(
            step_id=step_id, action_id=action_id, attempt_id=attempt_id,
            status='succeeded', receipt=receipt,
            step_status='succeeded', state=snapshot,
            next_step_id=next_step_id)
        receipt['observation_seq'] = int((settled or {}).get('observation_seq') or 0)
        # 成功后消费批准预留（reserved → consumed），幂等。
        if decision is not None and decision.approval_id and action_id:
            try:
                await book.store.consume_approval_reservation(
                    approval_id=decision.approval_id, action_id=action_id,
                    manifest_item_key=decision.approval_item_key or 'call')
            except Exception:                      # noqa: BLE001
                logger.warning('消费批准预留失败 action=%s', action_id, exc_info=True)
        return GovernedResult(
            kind='succeeded', action_id=action_id, attempt_id=attempt_id,
            updates=updates, receipt=receipt, effect=effect, settled=True,
            tool_result=tool_result, data_ref=data_ref)

    # 无账本分支已在上方拒绝，此处不会到达；防御性返回
    return GovernedResult(
        kind='no_run', action_id=action_id, attempt_id=attempt_id,
        updates=updates, effect=effect,
        tool_result=tool_result, data_ref=data_ref)
