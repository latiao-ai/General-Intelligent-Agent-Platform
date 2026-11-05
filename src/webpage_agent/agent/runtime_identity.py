"""运行时的引擎归属、实现版本与状态格式版本。

每个 run 持久化「由哪个引擎驱动、用的是哪个运行时实现版本、快照按哪个状态
格式版本读写」。恢复入口据此**读取已存归属**再分流，绝不回退默认引擎：

- 存量 run 回填 LangGraph（历史库里的 run 都是 LangGraph 驱动出来的）；
- 新 run 创建时确定归属；
- 全局默认只影响**新建**任务；
- 未知引擎或不兼容版本明确阻断，不能「不认识就退回默认引擎」再跑一遍。

这里只放常量与判定函数，不碰数据库：字段读写留在 store 层。
"""
from __future__ import annotations

# 引擎类型（runs.runtime_kind 的取值）。
KIND_LANGGRAPH = 'langgraph'
KIND_PI = 'pi'

KNOWN_KINDS = (KIND_LANGGRAPH, KIND_PI)

# 各引擎当前实现版本（runs.runtime_version）。实现改协议/语义时递增。
LANGGRAPH_RUNTIME_VERSION = 1
PI_RUNTIME_VERSION = 1

# 状态格式版本（runs.runtime_state_schema）：快照 state_json 的读写 schema 代次。
# 与「实现版本」分开：实现可以升级，但快照 schema 不兼容时必须递增并阻断旧读。
LANGGRAPH_STATE_SCHEMA = 1
PI_STATE_SCHEMA = 1


def default_kind() -> str:
    """全局默认引擎（只作用于**新建** run）。生产默认 LangGraph。"""
    from webpage_agent.core.config import settings
    value = str(getattr(settings, 'default_runtime_kind', '') or '').strip().lower()
    if value in KNOWN_KINDS:
        return value
    return KIND_LANGGRAPH


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
    """从库里读已存归属，返回引擎类型；无 store / 无 run_id 时回默认 LangGraph。

    恢复/首次执行入口**在读取旧计划之前**调用它，据此分流：
    LangGraph 走原恢复路径，Pi 走自己的恢复装配。未知引擎 / 不兼容版本
    在这里直接抛出（不回退默认引擎）。
    """
    if store is None or not run_id:
        return default_kind()
    identity = await store.runtime_identity(run_id)
    return identity['kind']
