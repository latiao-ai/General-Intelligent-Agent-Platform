"""运行时接口：在「执行入口」与「推理编排」之间划出的一道边界。

背景（Pi 改造第一阶段）：

    现有执行入口 `webpage_agent.api.main.run_task()` 直接 `build_graph().astream(...)`，
    把 LangGraph 的驱动细节（流式模式、增量合并、custom 事件转发）全部内联在宿主层。
    要为后续引入 PiRuntime 留出接入口，先在这里定义一份**轻量**运行时契约：

        - 宿主（run_task / Hub / Worker）负责准备执行状态、产物目录、账本、
          事件回调与终态收尾；
        - 运行时只负责「驱动一次执行」——消费准备好的状态、把节点级进度与
          内部细粒度事件交回给回调，最后返回最终状态。

    本阶段仍然只有 `LangGraphRuntime` 一个实现，外部行为与改造前完全一致。

契约约定：

    - 入参 `state` 是**已经准备好**的完整初始状态（含 task_id、user_input、
      产物目录 artifact_dir、run_id、resume_plan 等），运行时不再二次初始化；
    - `on_event` 是节点内部细粒度事件回调（action / thought / stage_start /
      stage_end / draft / answer_delta），可以是普通函数或协程；运行时负责 await；
    - `progress` 是节点级进度回调 `(node, status, elapsed)`，签名与宿主原有约定一致；
    - 返回最终的 `state`（`dict`），字段与原 `run_task()` 返回值保持一致；
    - 控制流异常（`LeaseLost` / `ControlStopped`）与取消（`CancelledError`）必须
      **原样穿透**，不得被运行时吞掉或包成普通异常。
"""
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

# 节点内部细粒度事件回调：接收一个事件 dict，可以是普通函数或协程。
# 与宿主原有约定一致：运行时负责 await（api_hub 传入的 relay 是协程）。
EventCallback = Callable[[dict], Optional[Awaitable[None]]]

# 节点级进度回调：与宿主原有约定一致 (node, status, elapsed)。
# 保持**同步**调用（CLI 传入的是同步 print lambda，原实现未 await）。
ProgressCallback = Callable[[str, str, float], None]


@runtime_checkable
class Runtime(Protocol):
    """驱动一次智能体执行的最小接口。

    所有实现（当前是 LangGraphRuntime，后续是 PiRuntime）都遵守同一份契约，
    宿主侧（run_task）不感知底层是 LangGraph 还是 Pi。
    """

    async def run(
        self,
        state: dict,
        *,
        on_event: EventCallback | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict:
        """驱动执行并返回最终状态。

        - `state`：准备好的初始状态，运行时直接消费；
        - `on_event`：节点内部细粒度事件回调；
        - `progress`：节点级进度回调。

        返回最终状态 dict。异常与取消按契约原样传播。
        """
        ...
