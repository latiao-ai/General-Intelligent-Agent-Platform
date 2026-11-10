"""规划节点的进程内驱动，只给仍在保护「计划 / 修订 / 观察 / 核对」的用例用。

生产工厂 `get_runtime` 只返回 Pi。这里没有 LangGraph：按节点函数直接往下走，
用来在不启动 Node 的情况下保住宿主侧那些断言（租约、暂停、计划先于工具提交）。
"""
import inspect
import time
from pathlib import Path

from webpage_agent.agent import decide, nodes
from webpage_agent.core.config import settings
from webpage_agent.core.errors import RecursionBudgetExceeded
from webpage_agent.core.files import save_json
from webpage_agent.observe import events as _events
from webpage_agent.observe.events import bind_sink, emit, stage_end, stage_start, unbind_sink


async def _flush_events():
    """把 sink 交回的协程按顺序 await。第一笔失败向上抛，其余关掉以免泄漏。"""
    queue = _events._pending.get()
    if not queue:
        return
    pending = list(queue)
    queue.clear()
    error = None
    for coro in pending:
        if error is not None:
            coro.close()
            continue
        try:
            await coro
        except BaseException as exc:  # noqa: BLE001 — 交给调用方，其余协程先关掉
            error = exc
    if error is not None:
        raise error

_DECIDE = frozenset({'observe_result', 'decide_next', 'wait_async'})
_PLAIN = frozenset({'run_step', 'intake', 'persist_plan', 'observe_result'})
_TERMINAL = frozenset({'end', 'stop', ''})


def _status_of(result) -> str:
    return result.get('status', '') if isinstance(result, dict) else ''


def _instrument(name, fn):
    is_async = inspect.iscoroutinefunction(fn)

    async def wrapper(state):
        emit(stage_start(name))
        started = time.perf_counter()
        try:
            result = await fn(state) if is_async else fn(state)
        except BaseException:
            emit(stage_end(name, time.perf_counter() - started, 'failed'))
            raise
        emit(stage_end(name, time.perf_counter() - started, _status_of(result)))
        return result

    return wrapper


def _fn(name):
    raw = getattr(decide if name in _DECIDE else nodes, name)
    return raw if name in _PLAIN else _instrument(name, raw)


def _next_after_persist(state) -> str:
    if state.get('intent_changed'):
        return 'revise_plan'
    if state.get('status') in ('waiting_input', 'waiting_approval', 'failed',
                               'paused', 'interrupted'):
        return 'end'
    if (state.get('plan') or {}).get('steps'):
        return 'run_step'
    return 'verify_completion'


def _next_after_revise(state) -> str:
    if state.get('status') in ('waiting_input', 'waiting_approval', 'failed',
                               'paused', 'interrupted'):
        return 'end'
    steps = (state.get('plan') or {}).get('steps') or []
    if steps and not state.get('intent_changed'):
        return 'persist_plan'
    return 'verify_completion'


def _next_after_finalize(state) -> str:
    return 'revise_plan' if state.get('intent_changed') else 'end'


def _next_after_verify(state) -> str:
    route = state.get('next_route') or 'stop'
    if route == nodes.observation_module.ROUTE_DECIDE:
        return 'decide_next'
    return 'finalize' if not state.get('status') or state.get('status') in (
        'succeeded', 'partially_succeeded') else 'end'


def _after(name, state) -> str:
    if name == 'plan':
        return 'persist_plan'
    if name == 'run_step':
        return 'observe_result'
    if name == 'wait_async':
        return 'end'
    if name == 'intake':
        return nodes.route_entry(state)
    if name == 'persist_plan':
        return _next_after_persist(state)
    if name == 'revise_plan':
        return _next_after_revise(state)
    if name == 'observe_result':
        return decide.route_after_observation(state)
    if name == 'decide_next':
        return decide.route_after_decision(state)
    if name == 'verify_completion':
        return _next_after_verify(state)
    if name == 'finalize':
        return _next_after_finalize(state)
    return 'end'


class HostLoopRuntime:
    """按规划节点走一遍。实现 Runtime.run 的形态，但不是生产引擎。"""

    async def run(self, state: dict, *, on_event=None, progress=None) -> dict:
        resumed = state.get('resume_plan') if isinstance(state.get('resume_plan'), dict) else {}
        pi_resume = state.get('pi_resume') if isinstance(state.get('pi_resume'), dict) else None
        if pi_resume is None and isinstance(resumed.get('pi_resume'), dict):
            pi_resume = resumed.get('pi_resume')
        recovery = (pi_resume or {}).get('recovery') if isinstance(pi_resume, dict) else None
        kind = recovery.get('kind') if isinstance(recovery, dict) else ''
        # 有动作却没有计划：Pi 的恢复决定是待核实。没有步骤可复用时停住，不重新规划。
        if kind in ('resume_reconcile', 'resume_block') and not resumed.get('steps'):
            state['status'] = 'needs_reconcile'
            state['failure'] = str((recovery or {}).get('reason') or '需核实')
            return state
        if kind == 'resume_finish':
            state['answer'] = (recovery or {}).get('final_answer') or ''
            state['status'] = 'succeeded'
            return state
        token = bind_sink(on_event) if on_event else None
        started = time.perf_counter()
        marked = started
        limit = int(getattr(settings, 'graph_recursion_limit', 40) or 40)
        folder = Path(state['artifact_dir']) if state.get('artifact_dir') else None
        try:
            current = 'intake'
            hops = 0
            while current not in _TERMINAL:
                hops += 1
                if hops > limit:
                    raise RecursionBudgetExceeded('这一轮已经用满步数预算', limit=limit)
                result = _fn(current)(state)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, dict):
                    state.update(result)
                now = time.perf_counter()
                label = (result.get('step_label') if isinstance(result, dict) else None) or current
                state.setdefault('timings', []).append({
                    'stage': label,
                    'seconds': round(now - marked, 2),
                    'status': state.get('status'),
                })
                marked = now
                if progress:
                    progress(label, state.get('status'), round(now - started, 2))
                if folder is not None:
                    save_json(folder / 'state.json', state)
                if token is not None:
                    await _flush_events()
                current = _after(current, state)
            if token is not None:
                await _flush_events()
        finally:
            if token is not None:
                unbind_sink(token)
        return state
