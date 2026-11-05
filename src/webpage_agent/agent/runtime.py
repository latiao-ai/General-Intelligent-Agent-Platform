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


def get_runtime(kind: str = '') -> Runtime:
    """运行时工厂：宿主经这里取运行时，而不是写死具体实现。

    第七阶段起按**引擎归属**分流：

    - `kind` 为空（CLI 直调 / 无账本单测）→ 返回 `LangGraphRuntime`（生产默认）；
    - `kind == 'pi'` → 返回 `PiRuntime`；
    - 未知 `kind` → 抛 `UnknownRuntimeKind`，**不回退默认引擎**。

    调用方（`run_task` / Hub）负责从 `store.runtime_identity(run_id)` 读已存归属
    再传到这里。测试仍可 `patch` 本函数注入替身。
    """
    from webpage_agent.agent import runtime_identity

    normalized = runtime_identity.normalize_kind(kind) if kind else ''
    if not kind:
        normalized = runtime_identity.KIND_LANGGRAPH
    if normalized == runtime_identity.KIND_LANGGRAPH:
        from webpage_agent.agent.langgraph_runtime import LangGraphRuntime
        return LangGraphRuntime()
    if normalized == runtime_identity.KIND_PI:
        from webpage_agent.agent.pi_runtime import PiRuntime
        # 第七阶段收口：受控模型脚本（测试专用）经环境变量注入，让正式
        # Hub/Worker 入口在无外部凭据下用确定性模型产出做端到端恢复验证。
        import json as _json
        import os as _os
        mock_script = None
        raw_mock = _os.environ.get('PI_MOCK_SCRIPT')
        if raw_mock:
            try:
                parsed = _json.loads(raw_mock)
                if isinstance(parsed, list):
                    mock_script = parsed
            except ValueError:
                mock_script = None
        # 受控工具执行器（测试专用）：PI_MOCK_TOOL=1 时，web_search 走确定性
        # mock 结果（仍经完整受治理结算链路），不依赖真实网络。
        tool_runner = None
        if _os.environ.get('PI_MOCK_TOOL') == '1':
            async def tool_runner(name: str, args: dict):
                return True, {
                    'documents': [{
                        'source_id': f'mock-{name}', 'title': 'mock',
                        'url': 'https://mock.example', 'chars': 3,
                    }],
                }
        return PiRuntime(mock_script=mock_script, tool_runner=tool_runner)
    raise runtime_identity.UnknownRuntimeKind('<new-run>', kind)
