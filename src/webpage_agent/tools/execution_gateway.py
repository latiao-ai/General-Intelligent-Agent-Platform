"""一次受治理的工具调用：授权、准入、撤权复查、真实调用、结果归一、结算。

这是从 `agent/nodes.py` 的 `run_step` 里**抽取**出来的「单次执行边界」，供
LangGraph 图（`run_step`）与 Pi 运行时（`pi_runtime`）**共同复用**。两者都必须
走同一条治理链：控制检查 → 授权 → 调用预算 → 准入（`ActionLedger.admit`）→
撤权复查 → 真实调用 → 结果归一 → 结算（`ActionLedger.settle`）。

为什么抽出来：以前这套逻辑只活在 `run_step` 里，Pi 要接真实工具就得再抄一遍，
两处账本、两处授权、两处结算迟早分叉——"模型看不见但远端被调用了"或者
"结算漏了"这类事故就再也说不清是谁放行的。

边界：

- 这里**只负责一次受治理调用**，不认识计划、步骤游标、依赖解析、图路由；
- 可信主体（`ToolCallContext`）、账本（`ActionLedger`）、执行权（`ExecutionContext`）
  由 Python 持有并通过 ContextVar 传入，**不**从 Node / 模型输出取授权依据；
- 返回一个结构化结果 `GovernedResult`，由调用方决定如何映射回自己的状态
  （图节点返回 updates，Pi 返回回执）。

不使用本模块也不影响旧路径：`run_step` 改为调用它后，行为保持不变，
旧测试继续通过；Pi 再通过它接入真实工具，不复制账本。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from webpage_agent.core.config import settings
from webpage_agent.governance import execution_control
from webpage_agent.observe import errors as errors_module
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

    - `succeeded`：真实调用完成并已结算；
    - `replay`：该动作此前已成功，复用已有回执（未再次调用）；
    - `blocked`：结果未定（in_flight / unknown），必须核实，未调用；
    - `needs_approval`：需要批准，未调用；
    - `denied`：授权被拒，未调用（远端调用计数为零）；
    - `failed`：调用失败（可能已结算为 failed / unknown）。

    `updates` 是工具返回的 state 增量（成功时非空）；`receipt` 是结算回执。
    """
    kind: str
    action_id: str = ''
    attempt_id: str = ''
    updates: dict = field(default_factory=dict)
    receipt: dict = field(default_factory=dict)
    replay: dict | None = None
    approval_id: str = ''
    error: str = ''
    error_code: str = ''
    certainty: str = 'not_executed'
    effect: str = 'unknown'
    # 结算是否已经发生（succeeded/failed 时为 True；denied/blocked/needs_approval 为 False）
    settled: bool = False


async def governed_call(
    *,
    tool,
    args: dict,
    step_id: str,
    state: dict,
    ctx=None,
    executor=None,
) -> GovernedResult:
    """执行一次受治理的工具调用。

    参数：

    - `tool`：`Tool` 对象（含 name/tool_key/effect/run）；
    - `args`：已解析的参数；
    - `step_id`：步骤标识（进账本幂等键）；
    - `state`：当前执行状态（只读，用于真实调用）；
    - `ctx`：可信执行主体（None 表示走兼容路径，不授权）；
    - `executor`：可选的 `async (tool, args, state) -> updates` 覆盖 `tool.run`
      （Pi 注入用；默认用 tool.run）。

    复用 run_step 的同一条链：控制检查 → 授权 → 调用预算 → 准入 → 撤权复查 →
    真实调用 → 结果归一 → 结算。**不复制账本**——准入与结算都走当前账本。
    """
    name = getattr(tool, 'name', '')
    tool_key = getattr(tool, 'tool_key', '') or name
    effect = getattr(tool, 'effect', 'unknown')
    book = ledger_module.current_ledger()

    # 控制检查（暂停/取消/版本变化在准入前）
    await check_boundary(state)

    action_id, attempt_id = '', ''

    # 有账本才走治理链；无账本（CLI/单测）直接真实调用，不进预算/授权/账本。
    if book is not None:
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

        # 准入
        admitted = await book.admit(
            step_id=step_id, tool_key=tool_key, effect=effect, args=args,
            expected_intent_version=plan_intent(state),
            authorization=authorization)
        if admitted.get('blocked'):
            emit(action('该步骤结果未定',
                        f'{tool.label}（{admitted["blocked"]}），需核实后再继续', '❓'))
            return GovernedResult(kind='blocked', error=str(admitted['blocked']),
                                  certainty='unknown', effect=effect)
        if admitted.get('replay') is not None:
            emit(action('复用已有结果', f'{tool.label}（该动作此前已成功）', '♻️'))
            return GovernedResult(kind='replay', replay=admitted['replay'],
                                  effect=effect)
        action_id = admitted.get('action_id', '')
        attempt_id = admitted.get('attempt_id', '')

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
                exc=exc, effect=effect)
        return GovernedResult(
            kind='failed', action_id=action_id, attempt_id=attempt_id,
            error=hint, error_code=outcome.error_code,
            certainty=outcome.certainty, effect=effect,
            settled=(book is not None and bool(action_id)))

    updates = dict(updates) if isinstance(updates, dict) else {}

    # 结算（成功）：动作 + attempt + 步骤 + 快照一次提交
    if book is not None and action_id:
        marker = str(updates.pop('status', '') or '')
        receipt = {k: v for k, v in updates.items()
                   if isinstance(v, (str, int, float, bool))}
        await book.settle(
            step_id=step_id, action_id=action_id, attempt_id=attempt_id,
            status='succeeded', receipt=receipt,
            step_status='succeeded')
        if marker:
            updates['status'] = marker
        return GovernedResult(
            kind='succeeded', action_id=action_id, attempt_id=attempt_id,
            updates=updates, receipt=receipt, effect=effect, settled=True)

    return GovernedResult(
        kind='succeeded', action_id=action_id, attempt_id=attempt_id,
        updates=updates, effect=effect)
