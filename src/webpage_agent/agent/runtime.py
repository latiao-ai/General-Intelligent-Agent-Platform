"""运行时接口：在「执行入口」与「推理编排」之间划出的一道边界。

背景（Pi 改造）：

    现有执行入口 `webpage_agent.api.main.run_task()` 曾直接 `build_graph().astream(...)`，
    把 LangGraph 的驱动细节全部内联在宿主层。为给后续引入 PiRuntime 留出接入口，
    这里定义一份**轻量**运行时契约，并提供一个默认工厂：

        - 宿主（run_task / Hub / Worker）负责准备执行状态、产物目录、账本、
          事件回调与终态收尾；
        - 运行时只负责「驱动一次执行」——消费准备好的状态、把节点级进度与
          内部细粒度事件交回给回调，最后返回最终状态。

    生产默认只有 `LangGraphRuntime` 一个实现，外部行为与改造前完全一致。

契约约定（状态归属是关键）：

    - 入参 `state` 是**已经准备好**的完整初始状态（含 task_id、user_input、
      产物目录 artifact_dir、run_id、resume_plan 等），运行时不再二次初始化；
    - 运行时**原地修改**这份 `state`（同一 dict 引用），正常结束时把它返回。
      返回的 dict 与入参是**同一个对象**，宿主可以直接 `await` 后继续读它，
      也可以接收返回值——两者等价；
    - **异常、取消、失租时**，运行时已经写进 `state` 的部分增量**仍然留在
      这份 state 里**（因为运行时只改引用、从不重新绑定变量）。宿主在自己的
      `try/except/finally` 里读的是同一份 state，因此能拿到"异常发生前已经
      完成的那部分状态"，再据此做终态收口与落盘。这是本接口最重要的承诺：
      不要用「运行时内部另建一份 state、失败就丢弃」的实现，否则宿主在
      异常后拿不到任何已发生的真实副作用；
    - 运行时**不负责**终态归一、outcome 生成与最终落盘——那些是宿主收尾的
      职责，宿主保证「最终落盘、outcome 生成、Hub 收尾」读取同一份有效 state；
    - 控制流异常（`LeaseLost` / `ControlStopped`）与取消（`CancelledError`）必须
      **原样穿透**，不得被运行时吞掉或包成普通异常；
    - 底层引擎专属的预算耗尽（如 LangGraph 的递归上限）由**适配层**折成通用
      结果（`core.errors.RecursionBudgetExceeded`），宿主不感知底层引擎类型。
"""
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

import inspect

# 节点内部细粒度事件回调：接收一个事件 dict。
#
# **同步或异步皆可**：运行时用 `inspect.isawaitable` 区分，同步回调按普通函数
# 调用，异步回调（如 api_hub 传入的 relay 协程）则 await。回调内部抛出的异常
# **原样向上传播**，绝不静默吞掉——事件转发失败应当打断这次执行，而不是
# 让前端以为自己还在推进。
EventCallback = Callable[[dict], Optional[Awaitable[None]]]

# 节点级进度回调：与宿主原有约定一致 (node, status, elapsed)，保持**同步**调用
# （CLI 传入的是同步 print lambda，原实现未 await）。
ProgressCallback = Callable[[str, str, float], None]


async def invoke_callback(callback, event: dict) -> None:
    """统一回调协议：同步回调按普通函数调用，异步回调则 await。

    回调抛出的异常**原样向上传播**，绝不静默吞掉——事件转发失败应打断执行。
    各运行时实现共用这一份调用约定，避免各自写一套。
    """
    result = callback(event)
    if inspect.isawaitable(result):
        await result


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

        - `state`：准备好的初始状态，运行时**原地修改**并返回同一对象；
        - `on_event`：节点内部细粒度事件回调（同步或异步皆可）；
        - `progress`：节点级进度回调（同步）。

        正常结束返回最终状态 dict（与入参同一对象）。异常、取消、失租时
        state 里已保留「异常前已完成」的部分增量，控制流异常与取消原样传播。
        """
        ...


def get_runtime() -> Runtime:
    """运行时工厂：宿主经这里取运行时，而不是写死具体实现。

    生产默认返回 `LangGraphRuntime`；测试可以通过 `patch` 掉本函数注入替代
    实现（例如一个只记录调用、不改状态的假运行时），从而在不驱动真实图
    的前提下验证宿主的收尾、终态与落盘行为。

    刻意**不**做数据库字段、注册中心、插件发现或全局引擎切换配置——那些
    留给 Pi 接入阶段。这里只是一个轻量的依赖注入点。
    """
    from webpage_agent.agent.langgraph_runtime import LangGraphRuntime
    return LangGraphRuntime()
