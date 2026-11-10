"""Pi 的脚本化执行：按既定步骤顺序调用工具，不启动模型。

补偿和「部署上一份产物」以前是 LangGraph 的固定计划（`resume_plan.steps`）。
Pi 的工具循环不认这份计划，只改 `runtime_kind` 会建一条不执行步骤的空转。

这里仍是 `KIND_PI` 的 `PiRuntime`：步骤由宿主事先写好（撤销列表或单步部署），
按顺序经 `HostToolCaller.call_declared` → `governed_call` 执行。模型桥接不启动，
模型也不能改步骤、换工具或插入别的调用。

脚本记在 Pi 快照的 `host_result_state.script` 里（已有字段，不升 schema）。
恢复读这份快照和账本，不走图的 `_resume_plan`：

- 账本里已成功的步骤跳过，不重新调用；
- `in_flight` / `unknown` 停在待核实，不猜着重发；
- `accepted` 停在 `waiting_external`，不把受理当成完成，也不重发；
- `awaiting_approval` 且恢复决策不是 `resume_admit` 时停在 `waiting_approval`，
  不重新发起调用。批准后的 `resume_admit` 才执行当前步，并由治理链再查一次授权。
"""
from __future__ import annotations

import logging

from webpage_agent.agent import pi_snapshot
from webpage_agent.agent import runtime_identity
from webpage_agent.core import outcome as outcome_module
from webpage_agent.governance import execution_control
from webpage_agent.observe.events import action, emit, stage_end, stage_start
from webpage_agent.store import ledger as ledger_module

logger = logging.getLogger(__name__)

_UNCERTAIN = frozenset({'in_flight', 'unknown'})


def scripted_resume(steps: list[dict], *, kind: str, summary: str,
                    intent_version: int) -> dict:
    """补偿 / 部署交给 PiRuntime 的续跑输入。

    `scripted` 是开关：没有它，Pi 会忽略 `steps`（那正是只改 runtime_kind 的空转）。
    `intent_version` 必须是这条新 run 上的要求版本，准入拿它做比对。
    """
    return {
        'scripted': True,
        'kind': kind,
        'steps': list(steps),
        'summary': summary,
        'intent_version': int(intent_version or 1),
        'plan_version': 1,
    }


def script_of(state: dict, snapshot: dict | None) -> dict | None:
    """从快照或本次 resume_plan 取出脚本。都没有则不是脚本化运行。

    快照优先：崩溃后续跑时，Hub 重新装配的是 `pi_resume`，原来的
    `resume_plan.steps` 不在了。脚本必须已经写进快照。
    """
    host = (snapshot or {}).get('host_result_state') or {}
    saved = host.get('script') if isinstance(host, dict) else None
    if isinstance(saved, dict) and saved.get('scripted'):
        return saved
    resumed = state.get('resume_plan') if isinstance(state, dict) else None
    if isinstance(resumed, dict) and resumed.get('scripted'):
        return {
            'scripted': True,
            'kind': str(resumed.get('kind') or ''),
            'steps': [dict(step) for step in (resumed.get('steps') or [])
                      if isinstance(step, dict)],
            'summary': str(resumed.get('summary') or ''),
            'cursor': int(resumed.get('step_index') or 0),
            'results': [],
        }
    return None


def progress(steps: list[dict], actions: list[dict]) -> tuple[int, str]:
    """按账本判断脚本下一步。

    返回 `(下标, 原因)`。原因：

    - `''`：从该下标继续执行；
    - `uncertain`：该步有在途或未知动作，不能重发；
    - `accepted`：该步已受理但未完成，继续等待；
    - `done`：每一步都已成功。
    """
    grouped: dict[str, list[str]] = {}
    for act in actions or []:
        grouped.setdefault(str(act.get('step_id') or ''), []).append(
            str(act.get('status') or ''))
    for index, step in enumerate(steps):
        statuses = grouped.get(str(step.get('step_id') or ''), [])
        if 'succeeded' in statuses:
            continue
        if any(item in _UNCERTAIN for item in statuses):
            return index, 'uncertain'
        if 'accepted' in statuses:
            return index, 'accepted'
        # 已确认失败的步骤不自动重试：重试是又一次调用，脚本恢复只继续没做过的步。
        if 'failed' in statuses:
            return index, 'failed'
        return index, ''
    return len(steps), 'done'


def hydrate(state: dict, steps: list[dict], actions: list[dict],
            kind: str) -> list[dict]:
    """用已成功动作的回执补回部署结果和补偿记录。

    崩溃发生在「动作已结算、任务状态还没写完」时，内存里的 state 是空的。
    终态仍要看见 `deployment` / `compensations`，不能因为空 state 当成没做过。
    """
    done = {}
    for act in actions or []:
        if str(act.get('status') or '') == 'succeeded':
            done[str(act.get('step_id') or '')] = act
    outputs = dict(state.get('step_outputs') or {})
    results = [item for item in (state.get('compensations') or [])
               if isinstance(item, dict)]
    seen = {str(item.get('step_id') or '') for item in results}
    for step in steps:
        step_id = str(step.get('step_id') or '')
        act = done.get(step_id)
        if act is None:
            continue
        receipt = act.get('receipt') if isinstance(act.get('receipt'), dict) else {}
        record = {'__status__': 'succeeded'}
        for key, value in receipt.items():
            if key == 'compensate':
                continue
            if isinstance(value, (str, int, float, bool, dict, list)):
                record[key] = value
        outputs.setdefault(step_id, record)
        deployment = receipt.get('deployment')
        if isinstance(deployment, dict) and not state.get('deployment'):
            state['deployment'] = deployment
        if kind == 'compensation':
            origin = str(step.get('compensates') or step_id)
            if origin not in seen:
                results.append({'step_id': origin, 'tool': step.get('tool') or '',
                                'status': 'succeeded'})
                seen.add(origin)
    state['step_outputs'] = outputs
    return results


async def execute(runtime, state: dict, script: dict) -> None:
    """按脚本跑完，或停在一个明确的等待 / 待核实状态。不启动模型桥接。"""
    snapshot = runtime._snapshot
    script = _copy_script(script)
    steps = script['steps']
    kind = script['kind']
    # 已经停在批准 / 外部等待的快照不能在进循环前被改回 running，
    # 否则恢复会把「还在等」看成「可以再调一次」。
    held = str(snapshot.get('phase') or '') in (
        'awaiting_approval', 'awaiting_external')
    _remember(snapshot, script, phase=None if held else 'running')
    await _persist(snapshot)

    if not steps:
        state['failure'] = '没有可执行的脚本步骤'
        state['status'] = 'failed'
        _apply_plan(state, script)
        _close(state, snapshot, script)
        return

    actions = await _actions(state)
    if actions:
        index, reason = progress(steps, actions)
    else:
        index = min(len(steps), max(0, int(script.get('cursor') or 0)))
        reason = 'done' if index >= len(steps) else ''
    script['cursor'] = index
    if actions:
        script['results'] = hydrate(state, steps, actions, kind)
    _remember(snapshot, script)

    recovery = _recovery_kind(state)
    phase = str(snapshot.get('phase') or '')
    if reason == 'failed':
        _stop(state, script, snapshot, status='failed',
              failure='该步骤上次已失败，恢复时不自动重试')
        return
    if reason == 'uncertain':
        _stop(state, script, snapshot, status='needs_reconcile',
              failure='有一步结果未定，未继续执行，避免重复副作用')
        return
    if reason == 'accepted':
        # 已受理不等于完成。脚本没有模型可以接着推理，也不重发这次写操作。
        _stop(state, script, snapshot, status='waiting_external',
              failure='已受理，等待远端结果', phase='awaiting_external')
        return
    if (phase == 'awaiting_approval' and reason != 'done'
            and recovery != 'resume_admit'):
        _stop(state, script, snapshot, status='waiting_approval',
              failure='等待批准，未重新发起调用', phase='awaiting_approval')
        return
    if reason == 'done':
        _apply_plan(state, script)
        _close(state, snapshot, script, phase='answered')
        return

    caller = _caller(runtime)
    # 走到这里说明当前步可以执行（含批准后的 resume_admit）。
    # 清掉上一轮的等待阶段，避免下一步恢复时又被当成还在等批准。
    snapshot['phase'] = 'running'
    from webpage_agent.agent.refs import resolve_inputs
    from webpage_agent.tools import core as tool_module
    from webpage_agent.tools import execution_gateway

    while index < len(steps):
        step = steps[index]
        step_id = str(step.get('step_id') or f's{index + 1}')
        name = str(step.get('tool') or '')
        ctx = await execution_gateway.tool_context(step_id=step_id, action_id='')
        tool = await tool_module.find_tool(name, ctx) if name else None
        if tool is None:
            _fail_step(state, script, snapshot, step, step_id,
                       f'工具 {name or "未命名"} 当前不可用')
            return
        try:
            args = resolve_inputs(
                step, state.get('step_outputs') or {},
                str(state.get('artifact_dir') or ''))
        except Exception as exc:  # noqa: BLE001 — 引用解析失败是这一步的失败
            hint = getattr(exc, 'safe_message', None) or type(exc).__name__
            _fail_step(state, script, snapshot, step, step_id, hint)
            return

        emit(stage_start(name, getattr(tool, 'label', '') or name))
        def build_snapshot(tool_updates, tool_result=None, external=None,
                           _step_id=step_id, _index=index):
            del tool_updates, external
            accepted = str((tool_result or {}).get('execution_status') or '') == 'accepted'
            if accepted:
                snapshot['phase'] = 'awaiting_external'
            else:
                script['cursor'] = _index + 1
                snapshot['phase'] = 'running'
            _remember(snapshot, script)
            committed = list(snapshot.get('committed_tool_results') or [])
            committed.append({
                'step_id': _step_id,
                'action_id': str((tool_result or {}).get('action_id') or ''),
                'attempt_id': str((tool_result or {}).get('attempt_id') or ''),
                'data_ref': str((tool_result or {}).get('data_ref') or ''),
            })
            snapshot['committed_tool_results'] = committed
            return pi_snapshot.serialize(snapshot)

        receipt = await caller.call_declared(
            step_id=step_id, tool=tool, args=args, state=state,
            build_snapshot=build_snapshot)
        label = getattr(tool, 'label', '') or name
        if receipt.needs_approval:
            emit(stage_end(name, 0, '等待批准', label))
            state['approval_id'] = receipt.approval_id
            _stop(state, script, snapshot, status='waiting_approval',
                  failure=receipt.error or '需要批准', phase='awaiting_approval')
            await _persist(snapshot, next_step_id=step_id)
            return
        if receipt.accepted:
            emit(stage_end(name, 0, '已受理', label))
            _stop(state, script, snapshot, status='waiting_external',
                  failure='已受理，等待远端结果', phase='awaiting_external')
            return
        if not receipt.ok:
            emit(stage_end(name, 0, '失败', label))
            status = 'needs_reconcile' if receipt.blocked else 'failed'
            _fail_step(state, script, snapshot, step, step_id,
                       receipt.error or '工具执行失败', status=status)
            return

        _merge_updates(state, receipt.updates)
        outputs = dict(state.get('step_outputs') or {})
        record = {'__status__': 'succeeded'}
        record.update(dict(receipt.updates or {}))
        outputs[step_id] = record
        state['step_outputs'] = outputs
        if kind == 'compensation':
            script['results'].append({
                'step_id': str(step.get('compensates') or step_id),
                'tool': name, 'status': 'succeeded'})
        script['cursor'] = index + 1
        _remember(snapshot, script)
        emit(stage_end(name, 0, '完成', label))
        emit(action('已执行脚本步骤', label, '↩️' if kind == 'compensation' else '▶'))
        index += 1

    _apply_plan(state, script)
    _close(state, snapshot, script, phase='answered')
    await _persist(snapshot)


def _copy_script(script: dict) -> dict:
    steps = [dict(step) for step in (script.get('steps') or [])
             if isinstance(step, dict)]
    results = [dict(item) for item in (script.get('results') or [])
               if isinstance(item, dict)]
    return {
        'scripted': True,
        'kind': str(script.get('kind') or ''),
        'steps': steps,
        'summary': str(script.get('summary') or ''),
        'cursor': int(script.get('cursor') or 0),
        'results': results,
    }


def _remember(snapshot: dict, script: dict, *, phase: str | None = None) -> None:
    host = dict(snapshot.get('host_result_state') or {})
    host['script'] = script
    snapshot['host_result_state'] = host
    if phase:
        snapshot['phase'] = phase


def _apply_plan(state: dict, script: dict) -> None:
    kind = script['kind']
    state['plan'] = {
        'steps': list(script['steps']),
        'summary': script['summary'],
        # 只有补偿用 kind 决定终态标签。部署看 state['deployment']，
        # 不能把 kind 写成 deployment，否则没有回执也会被当成补偿以外的另一种计划。
        'kind': 'compensation' if kind == 'compensation' else '',
        'version': 1,
        'intent_version': int(state.get('intent_version') or 0),
    }
    state['plan_version'] = int(state.get('plan_version') or 1)
    state['step_index'] = int(script.get('cursor') or 0)
    if kind == 'compensation':
        state['compensations'] = list(script.get('results') or [])


def _close(state: dict, snapshot: dict, script: dict, *, phase: str = '') -> None:
    _remember(snapshot, script, phase=phase or None)
    current = outcome_module.normalize_status(state.get('status') or '')
    if current not in outcome_module.TERMINAL_RUN_STATUSES:
        state['status'] = outcome_module.final_status(state)
    state['outcome'] = outcome_module.build(state)


def _stop(state: dict, script: dict, snapshot: dict, *, status: str,
          failure: str, phase: str = '') -> None:
    state['status'] = status
    state['failure'] = failure
    _apply_plan(state, script)
    _remember(snapshot, script, phase=phase or None)
    state['outcome'] = outcome_module.build(state)


def _fail_step(state: dict, script: dict, snapshot: dict, step: dict,
               step_id: str, message: str, *, status: str = 'failed') -> None:
    if script['kind'] == 'compensation':
        script['results'].append({
            'step_id': str(step.get('compensates') or step_id),
            'tool': str(step.get('tool') or ''),
            'status': 'failed', 'error': message})
    label = str(step.get('tool') or step_id)
    _stop(state, script, snapshot, status=status,
          failure=f'{label}失败：{message}',
          phase='failed' if status == 'failed' else '')


def _merge_updates(state: dict, updates: dict | None) -> None:
    for key, value in (updates or {}).items():
        state[key] = value


def _recovery_kind(state: dict) -> str:
    pi_resume = state.get('pi_resume')
    if not isinstance(pi_resume, dict):
        return ''
    recovery = pi_resume.get('recovery')
    if not isinstance(recovery, dict):
        return ''
    return str(recovery.get('kind') or '')


async def _actions(state: dict) -> list[dict]:
    book = ledger_module.current_ledger()
    store = getattr(book, 'store', None) if book is not None else None
    run_id = str(state.get('run_id') or getattr(book, 'run_id', '') or '')
    if store is None or not run_id or not hasattr(store, 'actions_of'):
        return []
    try:
        rows = await store.actions_of(run_id)
    except Exception:  # noqa: BLE001 — 读不到账本就不能假装没做过
        logger.warning('读取脚本动作失败 run=%s', run_id, exc_info=True)
        raise
    return list(rows or [])


async def _persist(snapshot: dict, *, next_step_id: str = '') -> None:
    """脚本写入版本化 Pi 快照。没有账本（纯单元测试）时只留在内存。"""
    book = ledger_module.current_ledger()
    context = getattr(book, 'context', None) if book is not None else None
    store = getattr(book, 'store', None) if book is not None else None
    if book is None or context is None or store is None:
        return
    if not getattr(context, 'lease_owner', ''):
        return
    if not hasattr(store, 'save_snapshot_owned'):
        return
    ok = await store.save_snapshot_owned(
        book.run_id,
        plan_version=int(getattr(book, 'plan_version', 1) or 1),
        state=pi_snapshot.serialize(snapshot),
        lease_owner=context.lease_owner,
        lease_epoch=int(getattr(context, 'lease_epoch', 0) or 0),
        next_step_id=next_step_id,
        snapshot_schema=runtime_identity.PI_STATE_SCHEMA)
    if ok is False:
        raise execution_control.LeaseLost(book.run_id, '脚本快照写入时已失租')


def _caller(runtime):
    caller = getattr(runtime, '_host_caller', None)
    if caller is not None:
        return caller
    from webpage_agent.agent.pi_host_tools import HostToolCaller, PiCallRegistry
    runner = getattr(runtime, '_tool_runner', None)
    executor = None
    if runner is not None:
        async def executor(tool, args, state):  # noqa: ARG001 — 与 PiRuntime 的注入形状一致
            ok, result = await runner(getattr(tool, 'name', ''), args)
            if not ok:
                raise RuntimeError(str(result))
            return result if isinstance(result, dict) else {}
    caller = HostToolCaller(
        PiCallRegistry(runtime._snapshot), executor=executor,
        tools_catalog=getattr(runtime, '_tool_objects', None))
    runtime._host_caller = caller
    return caller
