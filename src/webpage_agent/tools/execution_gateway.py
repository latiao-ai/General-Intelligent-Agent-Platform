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
import uuid
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
        # 第十一阶段 A 组第 5 点：会话 turn（chat/query）没有业务 run 行，用
        # turn_id 作为执行身份。可信主体回退到 session_turns 表的
        # tenant_id / owner_user_id（接纳时已落库），**不依赖业务 run 碰巧
        # 提供身份**，也不伪造一个"看起来像真的"主体。
        turn = None
        turn_reader = getattr(store, 'turn', None)
        if callable(turn_reader):
            try:
                turn = await turn_reader(context_.run_id)
            except Exception:                        # noqa: BLE001
                logger.warning('读取 turn 失败 turn=%s', context_.run_id,
                               exc_info=True)
                turn = None
        if not turn:
            return None
        tenant_id = str(turn.get('tenant_id') or '') or 'default'
        user_id = str(turn.get('owner_user_id') or '') or 'default'
        return tool_context_module.from_run(
            {'run_id': context_.run_id, 'tenant_id': tenant_id,
             'owner_user_id': user_id},
            user_id=user_id, role='member',
            step_id=step_id, action_id=action_id,
            intent_version=0, plan_version=1,
            authenticated=bool(tenant_id) and user_id != 'default')
    user_id = str(run.get('owner_user_id') or '') or 'default'
    role = str(run.get('owner_role') or '') or 'member'
    return tool_context_module.from_run(
        run, user_id=user_id, role=role, step_id=step_id, action_id=action_id,
        intent_version=int(state_intent_version(run)),
        plan_version=int(book.plan_version or 1),
        authenticated=bool(run.get('tenant_id')) and user_id != 'default')


def plan_intent(state: dict) -> int | None:
    """这份计划的依据版本。**只取已提交计划里的值**，不取模型自报的版本。

    第十阶段：Pi 运行时没有「图计划」，决策消费的版本经 `state['intent_version']`
    展平传入（见 run_task）。这里在无 `plan` 时回退到顶层 `intent_version`，
    让 Pi 的每个工具调用同样携带决策版本、由准入事务比较——否则 Pi 的工具
    调用会绕过「新要求到达后旧候选不得继续」的门禁。
    """
    value = (state.get('plan') or {}).get('intent_version')
    if value is None:
        value = state.get('intent_version')
    return int(value) if value is not None else None


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
        # 第七阶段收口：控制状态读取失败**必须停止**，不能放行——否则取消/暂停
        # 请求会被静默忽略，执行者继续做副作用。与取消/暂停一致地抛 ControlStopped。
        logger.exception('安全边界读取控制状态失败，停止本步 run=%s', context.run_id)
        state.setdefault('errors', []).append({
            'stage': 'boundary', 'type': type(exc).__name__,
            'error_code': getattr(exc, 'error_code', 'E_CONTROL_STATE_UNAVAILABLE'),
            'message': '边界检查读不到控制请求，本步停止（避免忽略取消/暂停）'})
        raise execution_control.ControlStopped(
            '边界控制状态不可用，停止本步以免忽略取消/暂停', 'unknown')
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
    # 已受理（accepted）时的远端任务身份与合同（第八阶段第 3 点）
    remote_task_id: str = ''
    contract_id: str = ''
    # 本地登记的 external task 身份（第八阶段收口第 1 点）：本地匹配主键，
    # 与 action_id / logical_call_id 一起构成持久关联；remote_task_id 仅远端查询用。
    external_task_id: str = ''


def _result_payload(updates: dict):
    """挑出结果里值得完整保存的部分（直接回执优先，其次整包）。"""
    for key in ('payload', 'result', 'response', 'data', 'mcp_calls'):
        if key in updates:
            return updates[key]
    return {key: value for key, value in updates.items()
            if isinstance(value, (str, int, float, bool, dict, list))}


async def _external_registration(book, ctx, *, tool_key: str,
                                 remote_task_id: str, idempotency_key: str,
                                 contract_id: str, contract_version: int,
                                 step_id: str) -> dict:
    """装配一次「已受理」外部任务的登记字段（第八阶段第 4 点）。

    从账本 run 行取 tenant / owner / credential 归属；从合同取轮询间隔与截止
    时间。`submission_key` 用 remote id（无 remote id 时用幂等键），`item_key`
    用逻辑调用身份（step_id），保证 `(run_id, action_id, item_key)` 唯一。
    """
    import time as _time
    from webpage_agent.core import async_contracts
    now = _time.time()
    contract = async_contracts.match_id(
        contract_id, int(contract_version) if contract_version else None)
    poll_interval = float(getattr(contract, 'poll_interval_seconds', 5) or 5)
    deadline = float(getattr(contract, 'deadline_seconds', 1800) or 1800)
    tenant_id = 'default'
    owner_user_id = 'default'
    credential_subject = ''
    if ctx is not None:
        tenant_id = str(getattr(ctx, 'tenant_id', None) or 'default')
        owner_user_id = str(getattr(ctx, 'user_id', None) or 'default')
    store = getattr(book, 'store', None)
    run_id = getattr(book, 'run_id', '')
    if store is not None and run_id:
        try:
            run = await store.run(run_id)
            if run:
                tenant_id = str(run.get('tenant_id') or tenant_id)
                owner_user_id = str(run.get('owner_user_id') or owner_user_id)
                credential_subject = str(run.get('platform_subject') or '')
        except Exception:                                # noqa: BLE001
            logger.warning('读取 run 归属失败 run=%s', run_id, exc_info=True)
    submission_key = remote_task_id or idempotency_key or step_id
    return {
        'external_task_id': 'et_' + uuid.uuid4().hex[:16],
        'remote_task_id': remote_task_id,
        'tenant_id': tenant_id,
        'owner_user_id': owner_user_id,
        'server_binding_id': '',
        'credential_binding': '',
        'credential_subject': credential_subject,
        'tool_key': tool_key,
        'contract_id': contract_id,
        'contract_version': int(contract_version or 1),
        'accepted_result_id': '',
        'submission_key': submission_key,
        'item_key': step_id,
        'deadline_at': now + deadline,
        'next_poll_at': now + poll_interval,
    }


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
    snapshot_schema: int | None = None,
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

    # 调用预算：只有**已准入**的动作才消耗一次工具调用预算。
    # 放在准入之后，而不是之前——否则「run 不存在」会被 consume_budget 的
    # `_owned` 抛成 BudgetExceeded，把 no_run 误判成 failed。准入本身就是
    # run / 租约存在性的权威裁决（admit 返回 no_run 或抛 LeaseLost），
    # 预算只对真正取得准入资格的动作有意义。
    try:
        await book.store.consume_budget(
            book.context, run_id=book.run_id, key='tool_calls',
            limit=settings.run_max_tool_calls)
    except Exception as exc:                       # noqa: BLE001
        hint = getattr(exc, 'safe_message', None) or type(exc).__name__
        emit(action('调用预算用尽', hint, '⚠️'))
        # 预算不足：这次动作没有发出，把它结算为失败，不留 in_flight。
        await book.fail_from_exception(
            step_id=step_id, action_id=action_id, attempt_id=attempt_id,
            exc=exc, effect=effect)
        return GovernedResult(
            kind='failed', error=hint, error_code='E_RUN_BUDGET',
            certainty='not_executed', effect=effect, settled=True)

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
    idempotency_key = str(updates.pop('idempotency_key', '') or '')
    contract_id = str(updates.pop('contract_id', '') or '')
    contract_version = updates.pop('contract_version', 1)
    execution_status = str(updates.pop('execution_status', '') or '')
    updates.pop('phase', None)

    # 第八阶段第 3 点：accepted 回执不能当成最终成功，也不能让 Pi 误以为拿到
    # 生成结果。区分三种结局：
    #   - async_accepted（有 remote id 或幂等键）→ 结算为 accepted，登记外部任务；
    #   - execution_status == 'unknown'（提交结果未知）→ 转待核实，不盲目重提；
    #   - 其余 → 同步完成，走 succeeded 结算。
    reconcile_unknown = bool(
        not async_accepted and execution_status in ('unknown', 'needs_reconcile'))

    if reconcile_unknown:
        # 提交结果未知：动作未确认落地，也不能盲目重提（重提 = 再产生副作用）。
        # 结算为 unknown + needs_reconcile，交由人工/恢复核实。不消费批准预留
        # （可能已花掉额度）。
        # 第十阶段收口第 6 点：unknown 写动作必须把**最终参数（含幂等键
        # operation_id）**写进 receipt，供恢复时按原 operation ID 调 get_operation
        # 核实「远端是否已写」——没有它就无法区分「已写」和「未写」，只能盲目待核实。
        if book is not None and action_id:
            await book.settle(
                step_id=step_id, action_id=action_id, attempt_id=attempt_id,
                status='unknown',
                receipt={'error_code': 'E_ASYNC_SUBMIT_UNKNOWN',
                         'phase': 'needs_reconcile',
                         'certainty': 'unknown', 'effect': effect,
                         'final_args': dict(args or {}),
                         'tool_key': tool_key},
                error_type='E_ASYNC_SUBMIT_UNKNOWN',
                step_status='unknown', state=snapshot_state,
                next_step_id=next_step_id, snapshot_schema=snapshot_schema)
        return GovernedResult(
            kind='blocked', action_id=action_id, attempt_id=attempt_id,
            error='提交结果未知（无 remote id 且无幂等键），需核实后再继续',
            error_code='E_ASYNC_SUBMIT_UNKNOWN', certainty='unknown',
            effect=effect, settled=bool(book is not None and action_id))

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
        if idempotency_key:
            tool_result['idempotency_key'] = idempotency_key
    if marker:
        updates['status'] = marker

    # 结算：receipt 保留完整内容（含 list/dict）。构造待提交快照：build_snapshot
    # 回调优先（可包含工具增量 + 游标推进 + 本步结果记录），否则用静态
    # snapshot_state。在真实调用后、结算前构造，避免「先宣布成功、再补写关键
    # 恢复状态」。
    receipt = {k: v for k, v in updates.items()
               if isinstance(v, (str, int, float, bool, dict, list))}
    receipt['tool_result'] = tool_result
    # 第十阶段第 4 点：持久化**最终参数**（含幂等键注入后的完整参数），让恢复能
    # 读回「同一次调用」的真实参数，而不是用模型重发的参数冒充原调用。授权、
    # 审批指纹、账本准入、远端发送针对的正是这份最终参数。
    receipt['final_args'] = dict(args or {})
    # 第八阶段收口第 2 点：accepted 时**先**装配 external（含 external_task_id），
    # 再把 external 交给 build_snapshot，让 awaiting_calls 在结算的同一事务里
    # 随快照写入。不能依赖结算返回后的第二次快照写补齐关键关联——否则崩溃在
    # 「accepted 已提交、awaiting 未落库」之间会漏跟踪。
    external = None
    if book is not None and action_id and async_accepted:
        external = await _external_registration(
            book, ctx, tool_key=tool_key, remote_task_id=remote_task_id,
            idempotency_key=idempotency_key, contract_id=contract_id,
            contract_version=contract_version, step_id=step_id)
        # 第八阶段收口第 1 点：action_id 是本地匹配主键之一，与 external_task_id /
        # logical_call_id 一起持久化，供恢复时精确定位外部任务。
        external['action_id'] = action_id
    snapshot = None
    if build_snapshot is not None:
        if external is not None:
            snapshot = build_snapshot(
                updates, tool_result=tool_result, external=external)
        else:
            snapshot = build_snapshot(updates, tool_result=tool_result)
    elif snapshot_state is not None:
        snapshot = snapshot_state
    if book is not None and action_id:
        if async_accepted:
            # 第八阶段第 4 点：accepted 结算 + external task 登记同一事务原子提交。
            # 动作写 accepted（非 succeeded），远端业务尚未完成。
            settled = await book.settle_accepted(
                step_id=step_id, action_id=action_id, attempt_id=attempt_id,
                receipt=receipt, external=external, state=snapshot,
                next_step_id=next_step_id, snapshot_schema=snapshot_schema)
            receipt['observation_seq'] = int(
                (settled or {}).get('observation_seq') or 0)
            # accepted 不消费批准预留（动作尚未真正完成，预留保留到结果交付）。
            return GovernedResult(
                kind='accepted', action_id=action_id, attempt_id=attempt_id,
                updates=updates, receipt=receipt, effect=effect, settled=True,
                tool_result=tool_result, data_ref=data_ref,
                remote_task_id=remote_task_id, contract_id=contract_id,
                external_task_id=str(external.get('external_task_id') or ''))
        settled = await book.settle(
            step_id=step_id, action_id=action_id, attempt_id=attempt_id,
            status='succeeded', receipt=receipt,
            step_status='succeeded', state=snapshot,
            next_step_id=next_step_id, snapshot_schema=snapshot_schema)
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
