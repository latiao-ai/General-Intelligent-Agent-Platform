"""运行时接口：在「执行入口」与「推理编排」之间划出的一道边界。

背景（Pi 改造）：

    现有执行入口 `webpage_agent.api.main.run_task()` 曾直接 `build_graph().astream(...)`，
    把 LangGraph 的驱动细节全部内联在宿主层。为给后续引入 PiRuntime 留出接入口，
    这里定义一份**轻量**运行时契约，并提供一个默认工厂：

        - 宿主（run_task / Hub / Worker）负责准备执行状态、产物目录、账本、
          事件回调与终态收尾；
        - 运行时只负责「驱动一次执行」——消费准备好的状态、把节点级进度与
          内部细粒度事件交回给回调，最后返回最终状态。

    唯一的执行引擎是 Pi。无 run 身份的调用按 `default_kind()`（pi）取。
    库里 `runtime_kind=langgraph` 的存量行不在这里启动旧引擎：执行入口先
    `prepare_execution`，没有副作用就改挂到 Pi，已经有副作用就停在待核实。

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
    - 步数预算用尽由运行时折成通用结果（`core.errors.RecursionBudgetExceeded`），
      宿主按可续跑的中断收尾，不关心计数发生在哪一层。
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

    生产实现是 PiRuntime。宿主侧（run_task）只依赖这份契约。
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

    - `kind` 为空或 `pi` → 返回 `PiRuntime`，系统提示来自 `settings.pi_system_prompt`；
    - `kind == 'langgraph'` → 抛 `RetiredRuntime`。存量行的改挂/拒收在
      `prepare_execution`，工厂不再提供第二套引擎；
    - 未知 `kind` → 抛 `UnknownRuntimeKind`，**不回退**。

    调用方（`run_task` / Hub）先 `prepare_execution`，再把得到的 `pi` 传到这里。
    测试仍可 `patch` 本函数注入替身。
    """
    from webpage_agent.agent import runtime_identity

    normalized = runtime_identity.normalize_kind(kind) if kind else ''
    if not kind:
        normalized = runtime_identity.default_kind()
    if normalized == runtime_identity.KIND_LANGGRAPH:
        raise runtime_identity.RetiredRuntime(
            '<runtime>',
            'LangGraph 引擎已下线。存量运行请走 prepare_execution：'
            '没有副作用会改挂到 Pi，已经产生步骤或动作则停在待核实。')
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
        # 第八阶段收口第 10 点：PI_MOCK_ASYNC=1 时，web_search 走「异步受理」回执
        # （async_accepted + 受控远端任务身份），用于端到端验证提交→等待→轮询→
        # 唤醒→交付→答复的完整链路，不依赖真实 MCP 供应商。
        tool_runner = None
        if _os.environ.get('PI_MOCK_ASYNC') == '1':
            async def tool_runner(name: str, args: dict):
                return True, {
                    'async_accepted': True,
                    'remote_task_id': _os.environ.get(
                        'PI_MOCK_REMOTE_ID', 'job-mock-1'),
                    'contract_id': _os.environ.get(
                        'PI_MOCK_CONTRACT_ID', 'mcp-task'),
                    'contract_version': 1,
                }
        elif _os.environ.get('PI_MOCK_TOOL') == '1':
            async def tool_runner(name: str, args: dict):
                return True, {
                    'documents': [{
                        'source_id': f'mock-{name}', 'title': 'mock',
                        'url': 'https://mock.example', 'chars': 3,
                    }],
                }
        # 凭据缺失会让真实链路直接 401：PiRuntime 的默认模型配置里没有 key，
        # 必须由宿主从 settings 注入，否则正式入口只能跑受控模型（PI_MOCK_SCRIPT）。
        from webpage_agent.core import model_choice
        from webpage_agent.core.config import settings
        return PiRuntime(
            mock_script=mock_script, tool_runner=tool_runner,
            model={
                'id': model_choice.active(getattr(settings, 'llm_model', '') or ''),
                'base_url': settings.llm_base_url,
                'provider': 'deepseek',
                'api': 'openai-completions',
            },
            api_key=settings.llm_key or None,
            # 系统提示是配置，不是临时环境变量说明。
            # settings.pi_system_prompt 默认带画布停手规则；PI_SYSTEM_PROMPT
            # 可覆盖，显式空字符串则不注入。
            system_prompt=settings.pi_system_prompt or None,
        )
    raise runtime_identity.UnknownRuntimeKind('<new-run>', kind)
