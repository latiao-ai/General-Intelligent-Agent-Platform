"""LangGraph 运行时：把「Planner-Executor 图」的驱动逻辑封装成一份运行时实现。

这是 `agent.runtime.Runtime` 契约的**当前唯一生产实现**。它把原先散落在
`api.main.run_task()` 里的图驱动细节——`build_graph()`、`astream()`、状态增量合并、
节点耗时记录、custom 事件转发——原样搬迁进来，图配置与流式行为保持不变。

职责边界：

    - **运行时（本模块）**：构造图、驱动 `astream()` 流、把 `updates` 增量合并进
      state、记录节点耗时、把 `custom` 事件透传给回调、把中间态落盘，并把
      LangGraph 专属的 `GraphRecursionError` 折成通用 `RecursionBudgetExceeded`；
    - **宿主（run_task）**：准备初始状态与产物目录、绑定账本、包一层超时、处理
      控制流异常、做终态与 outcome 收口。

    异常与取消不在这里处理：`LeaseLost` / `ControlStopped` / `CancelledError`
    都**原样向上抛**，由宿主统一收口。唯一的适配是递归预算耗尽——它是
    LangGraph 的专属信号，在这里转成通用异常，宿主才不必 import `langgraph.errors`。
"""
import logging
import time
from pathlib import Path

from langgraph.errors import GraphRecursionError

from webpage_agent.core.config import settings
from webpage_agent.core.errors import RecursionBudgetExceeded
from webpage_agent.core.files import save_json
from webpage_agent.agent.graph import build_graph
from webpage_agent.agent.runtime import invoke_callback

logger = logging.getLogger(__name__)


class LangGraphRuntime:
    """驱动 LangGraph 执行图，实现 `agent.runtime.Runtime` 契约。

    只消费**已准备好**的初始状态；`state['artifact_dir']` 必须已存在（宿主在
    构造状态前就建好了产物目录），这里用它落中间态 state.json。运行时**原地
    修改**这份 state，异常时已合并的部分增量仍留在其中，宿主据此收尾。
    """

    async def run(self, state: dict, *, on_event=None, progress=None) -> dict:
        started = time.perf_counter()
        marked = started
        folder = Path(state['artifact_dir'])
        # 图配置与流式模式原样保留：recursion_limit 由 settings 提供，
        # stream_mode=['updates', 'custom'] 同时产出节点增量与节点内部事件。
        try:
            async for mode, chunk in build_graph().astream(
                state, config={'recursion_limit': settings.graph_recursion_limit},
                stream_mode=['updates', 'custom'],
            ):
                if mode == 'custom':
                    # 节点内部发出的动作事件，直接透传给调用方。
                    if on_event:
                        await invoke_callback(on_event, chunk)
                    continue
                for name, update in chunk.items():
                    now = time.perf_counter()
                    if isinstance(update, dict):
                        state.update(update)
                    # 计划的落库**不再依赖这里**：图里有一个显式的 `persist_plan` 节点。
                    # stream_mode='updates' 在该节点跑完后才产出事件，所以相邻事件的
                    # 时间差≈该节点耗时（含少量调度开销）。run_step 会自我循环多次，
                    # 用 step_label 记具体是哪一步。
                    label = (update.get('step_label')
                             if isinstance(update, dict) else None) or name
                    state['timings'].append({
                        'stage': label,
                        'seconds': round(now - marked, 2),
                        'status': state.get('status'),
                    })
                    marked = now
                    if progress:
                        progress(label, state.get('status'), round(now - started, 2))
                    save_json(folder / 'state.json', state)
        except GraphRecursionError as exc:
            # 适配层职责：把 LangGraph 专属的递归耗尽折成通用结果。语义是「预算用尽、
            # 可续跑」，不是普通失败。state 里已经保留全部已完成的步骤与副作用，
            # 由宿主按 interrupted 收尾。
            logger.warning('任务 %s 达到递归上限（%s），转成可续跑的中断',
                           state.get('task_id'), settings.graph_recursion_limit)
            raise RecursionBudgetExceeded(
                '这一轮已经用满步数预算',
                limit=settings.graph_recursion_limit) from exc
        return state
