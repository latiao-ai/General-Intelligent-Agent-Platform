"""运行时的引擎归属、实现版本与状态格式版本。

每个 run 持久化「由哪个引擎驱动、用的是哪个运行时实现版本、快照按哪个状态
格式版本读写」。恢复入口据此**读取已存归属**再分流，绝不回退默认引擎：

- 新 run 创建时确定归属，唯一可执行引擎是 Pi；
- 全局默认只影响**新建**任务，未配置时是 pi；
- 库里仍可能有 `runtime_kind=langgraph` 的存量行。执行前要么在没有副作用时
  改挂到 Pi，要么明确停在待核实。不再启动 LangGraph；
- 未知引擎或不兼容的 Pi 版本明确阻断，不能「不认识就退回默认引擎」再跑一遍。

这里只放常量与判定函数，不碰数据库：字段读写留在 store 层。
"""
from __future__ import annotations

# 引擎类型（runs.runtime_kind 的取值）。
# langgraph 只作为存量行上的历史标记，不是可执行引擎。
KIND_LANGGRAPH = 'langgraph'
KIND_PI = 'pi'

KNOWN_KINDS = (KIND_LANGGRAPH, KIND_PI)
EXECUTABLE_KINDS = (KIND_PI,)

# 各引擎当前实现版本（runs.runtime_version）。实现改协议/语义时递增。
# LangGraph 的版本号只用于识别存量行，不再对应一份运行时实现。
LANGGRAPH_RUNTIME_VERSION = 1
PI_RUNTIME_VERSION = 1

# 状态格式版本（runs.runtime_state_schema）：快照 state_json 的读写 schema 代次。
# Pi 用 2。1 是旧图快照的代次，读到它不能按 Pi 快照解析。
LANGGRAPH_STATE_SCHEMA = 1
PI_STATE_SCHEMA = 2

# 已经产生副作用的存量图 run：不能改挂后重放，也不能再启动旧引擎。
RETIRED_REASON = (
    '这条运行记录在已下线的 LangGraph 引擎上，并且已经产生了步骤、动作'
    '或未完成的外部任务。为避免重复执行，已停止并标记为待核实。请新开一次运行。')

_PROGRESSED_STEP = frozenset({
    'succeeded', 'failed', 'running', 'accepted', 'compensated', 'needs_reconcile',
})


def default_kind() -> str:
    """全局默认引擎（只作用于**新建** run）。唯一可执行引擎是 pi。

    环境里如果还写着 langgraph，也不再把它当成引擎：新建 run 一律是 pi。
    """
    return KIND_PI


def known_kind(kind: str) -> bool:
    return str(kind or '').strip().lower() in KNOWN_KINDS


def version_for_kind(kind: str) -> int:
    return {
        KIND_LANGGRAPH: LANGGRAPH_RUNTIME_VERSION,
        KIND_PI: PI_RUNTIME_VERSION,
    }[normalize_kind(kind)]


def schema_for_kind(kind: str) -> int:
    return {
        KIND_LANGGRAPH: LANGGRAPH_STATE_SCHEMA,
        KIND_PI: PI_STATE_SCHEMA,
    }[normalize_kind(kind)]


def normalize_kind(kind: str) -> str:
    """归一化引擎类型；未知返回空串（调用方据此阻断，而非退回默认）。"""
    value = str(kind or '').strip().lower()
    return value if value in KNOWN_KINDS else ''


class RetiredRuntime(RuntimeError):
    """存量 run 记着已下线的引擎，且不能安全改挂到 Pi。"""

    def __init__(self, run_id: str, reason: str = RETIRED_REASON) -> None:
        super().__init__(reason)
        self.run_id = run_id
        self.reason = reason
        self.safe_message = reason


class UnknownRuntimeKind(RuntimeError):
    """run 上记录的引擎类型未知：不能回退默认引擎继续执行。"""

    def __init__(self, run_id: str, kind: str) -> None:
        super().__init__(
            f'run {run_id} 的引擎类型未知（{kind!r}），拒绝执行：'
            '不能退回默认引擎再跑一遍')
        self.run_id = run_id
        self.kind = kind


class IncompatibleRuntimeVersion(RuntimeError):
    """run 上记录的运行时实现版本或状态 schema 与当前不兼容。"""

    def __init__(self, run_id: str, kind: str, field: str,
                 stored: int, current: int) -> None:
        super().__init__(
            f'run {run_id} 的引擎 {kind} 版本不兼容：{field}={stored}，'
            f'当前实现要求 {current}')
        self.run_id = run_id
        self.kind = kind
        self.field = field
        self.stored = stored
        self.current = current


async def resolve_kind(store, run_id: str) -> str:
    """从库里读已存归属；无 store / 无 run_id 时回默认 Pi。

    这只是读取。存量 `langgraph` 不会在这里被执行：执行入口要走
    `prepare_execution`，能改挂才改挂，不能改挂就停在待核实。
    """
    if store is None or not run_id:
        return default_kind()
    identity = await store.runtime_identity(run_id)
    return identity['kind']


def legacy_resume_safe(facts: dict | None) -> bool:
    """存量 langgraph run 能不能改挂到 Pi 后从头执行。

    没有动作、没有已经跑过的步骤、没有图快照里的步骤产出、也没有未完成的
    外部等待时，重跑不会重复副作用。除此之外一律视为不安全。
    """
    facts = facts or {}
    if facts.get('actions'):
        return False
    if int(facts.get('waiting_external') or 0):
        return False
    for step in facts.get('steps') or []:
        if str((step or {}).get('status') or '') in _PROGRESSED_STEP:
            return False
    snap = facts.get('snapshot_state') or {}
    if isinstance(snap, dict) and snap.get('step_outputs'):
        return False
    return True


async def collect_legacy_facts(store, run_id: str, run: dict | None = None) -> dict:
    """读判断「能不能改挂」所需的事实。仓储缺方法时按空处理，不因此崩溃。"""
    run = run if run is not None else {}
    if not run and store is not None and hasattr(store, 'run'):
        run = await store.run(run_id) or {}
    actions = []
    if store is not None and hasattr(store, 'actions_of'):
        actions = await store.actions_of(run_id) or []
    steps = []
    if store is not None and hasattr(store, 'steps_of'):
        steps = await store.steps_of(run_id) or []
    snapshot_state: dict = {}
    if store is not None and hasattr(store, 'run_snapshot'):
        try:
            raw = await store.run_snapshot(run_id)
        except Exception:  # noqa: BLE001 — 快照损坏本身就是不能改挂重放的理由
            raw = {'_broken': True}
        if isinstance(raw, dict):
            state = raw.get('state') if isinstance(raw.get('state'), dict) else {}
            snapshot_state = dict(state)
            if raw.get('_broken') or int(raw.get('schema') or 0) == LANGGRAPH_STATE_SCHEMA:
                if state.get('step_outputs') or raw.get('_broken'):
                    snapshot_state['step_outputs'] = state.get('step_outputs') or {'_broken': True}
    return {
        'actions': list(actions),
        'steps': list(steps),
        'snapshot_state': snapshot_state,
        'waiting_external': run.get('waiting_external') or 0,
        'status': str(run.get('status') or ''),
    }


def refused_resume(run: dict | None, reason: str = RETIRED_REASON) -> dict:
    """给执行器一份「不要重放」的恢复输入。Pi 看到后停在待核实，不调工具。"""
    run = run or {}
    return {
        'runtime_kind': KIND_PI,
        'refused': True,
        'reason': reason,
        'intent_version': int(run.get('intent_version') or 0),
        'processed_intent_version': int(run.get('processed_intent_version') or 0),
        'pi_resume': {
            'snapshot': None,
            'recovery': {'kind': 'resume_reconcile', 'reason': reason},
        },
    }


def fresh_pi_resume(run: dict | None) -> dict:
    """没有副作用的存量 run：丢掉旧图快照，按 Pi 首次执行。"""
    run = run or {}
    return {
        'runtime_kind': KIND_PI,
        'intent_version': int(run.get('intent_version') or 0),
        'processed_intent_version': int(run.get('processed_intent_version') or 0),
        'plan_version': int(run.get('plan_version') or 0),
        'user_input': str(run.get('goal') or ''),
        'pi_resume': None,
    }


async def prepare_execution(store, run_id: str, *, lease_owner: str,
                            lease_epoch: int) -> str:
    """执行前把归属收成 Pi。

    - 已经是 pi：原样返回；
    - langgraph 且没有副作用：在当前租约下改挂成 pi；
    - langgraph 且已经有步骤/动作/外部等待，或版本对不上：抛 `RetiredRuntime`，
      调用方写成待核实，不启动任何引擎。
    """
    try:
        ident = await store.runtime_identity(run_id)
    except IncompatibleRuntimeVersion as exc:
        if getattr(exc, 'kind', '') == KIND_LANGGRAPH:
            raise RetiredRuntime(run_id, RETIRED_REASON) from exc
        raise
    kind = ident['kind']
    if kind == KIND_PI:
        return KIND_PI
    if kind != KIND_LANGGRAPH:
        raise UnknownRuntimeKind(run_id, kind)
    run = await store.run(run_id) or {}
    facts = await collect_legacy_facts(store, run_id, run)
    if not legacy_resume_safe(facts):
        raise RetiredRuntime(run_id, RETIRED_REASON)
    adopted = await store.adopt_pi_runtime(
        run_id, lease_owner=lease_owner, lease_epoch=int(lease_epoch or 0))
    if not adopted:
        raise RetiredRuntime(
            run_id, '未能把存量运行改挂到 Pi（租约已变更），已停止以免重复执行')
    return KIND_PI
