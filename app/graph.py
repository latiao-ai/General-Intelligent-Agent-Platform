"""Planner-Executor 执行图。

控制流（文档 harness-live-input §5 第十步）：

    START → intake
    intake → plan | revise_plan | run_step | finalize
    plan → persist_plan
    revise_plan → persist_plan
    persist_plan → run_step | finalize | END
    run_step ↺ → run_step | revise_plan | finalize | END
    finalize → revise_plan（又有新补充）| END

`run_step` 每次只执行**一步**，所以每一步都有独立的阶段事件与耗时。

`intake` 放在最前面是有意的：恢复、续跑、运行中补充是同一件事的三个入口，
它们都要先看"库里现在要求的是哪一版、计划是哪一份、还有哪些补充没人处理"。
把这段逻辑放进图里，CLI 与 Worker 才真的只有一条路径。
"""
import inspect
import time

from langgraph.graph import StateGraph, START, END
from app.events import emit, stage_end, stage_start
from app.schemas import AgentState
from app import nodes

# 节点清单；执行器计时与前端耗时表都以此为准。
# run_step 会自我循环，每次只跑计划里的一步——故意**不**套 instrument，
# 这样它想发的是按工具名区分的阶段（"检索来源"/"整理报告"…），而不是笼统的
# "执行步骤"，前端耗时表才看得出来每一步干了什么。
NODE_NAMES = ['intake', 'plan', 'persist_plan', 'revise_plan', 'run_step',
              'finalize']
# 这几个节点很轻（只读写数据库/组装提示词），不需要自己的阶段行
PLAIN_NODES = {'run_step', 'intake', 'persist_plan'}


def _status_of(result) -> str:
    return result.get('status', '') if isinstance(result, dict) else ''


def instrument(name: str, fn):
    """给节点包一层，在进入/退出时发出阶段事件，供前端实时计时。

    放在这里而不是各节点内部：一处生效，新增节点也不会漏。

    包成 async 是有意为之：LangGraph 会把**同步**节点丢进线程池执行，而
    `get_stream_writer()` 依赖 contextvars，跨线程后拿不到 writer，事件会静默丢失。
    统一成 async 后全部在事件循环内执行，contextvars 正常。
    这些同步节点只做文件读写和模板渲染（实测 <0.1 秒），放在事件循环里没有影响。
    """
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

    wrapper.__name__ = name
    return wrapper


def checkpointer():
    """**本版本不接持久检查点**（文档 §2.1）。

    恢复的唯一权威来源是 PostgreSQL 的 plan / step / action / attempt 与运行快照
    （`run_snapshots`）。只给 `build_graph` 传一个 saver 并不能带来恢复能力：
    它还要处理异步 saver 生命周期、thread_id、恢复调用方式与动作提交的协调，
    而"图重放"绕过账本就会重复外部写操作。

    所以这里明确返回 None（LangGraph 用内存态）。以后接持久检查点时，恢复流程
    仍然必须经过动作账本，不能由检查点直接重放远端调用。
    """
    return None


def _next_after_persist(state) -> str:
    """计划提交之后：要求又变了就重新修订，否则继续执行（或直接收尾）。"""
    if state.get('intent_changed'):
        return 'revise_plan'
    if (state.get('plan') or {}).get('steps'):
        return 'run_step'
    return 'finalize'


def _next_after_revise(state) -> str:
    """修订之后：拿到新计划就去提交，否则按停下来的原因退出。"""
    steps = (state.get('plan') or {}).get('steps') or []
    if steps and not state.get('intent_changed'):
        return 'persist_plan'
    if state.get('status') in ('waiting_input', 'failed', 'paused'):
        return 'end'
    return 'finalize'


def _next_after_finalize(state) -> str:
    """收尾门禁发现"又有新补充"时回到修订，否则结束。"""
    return 'revise_plan' if state.get('intent_changed') else 'end'


def _map(*targets):
    """把路由函数的返回值映射到图节点名；`end`/`stop` 都收敛到 END。"""
    mapping = {name: name for name in targets}
    mapping['end'] = END
    mapping['stop'] = END
    return mapping


def build_graph(checkpointer_=None):
    """Planner-Executor + 受控修订：先规划，再逐步执行，中途可纳入补充要求。"""
    graph = StateGraph(AgentState)
    for name in NODE_NAMES:
        node = getattr(nodes, name)
        graph.add_node(name, node if name in PLAIN_NODES
                       else instrument(name, node))
    graph.add_edge(START, 'intake')
    graph.add_conditional_edges('intake', nodes.route_entry,
                                _map('plan', 'revise_plan', 'run_step', 'finalize'))
    graph.add_edge('plan', 'persist_plan')
    graph.add_conditional_edges('persist_plan', _next_after_persist,
                                _map('revise_plan', 'run_step', 'finalize'))
    graph.add_conditional_edges('revise_plan', _next_after_revise,
                                _map('persist_plan', 'finalize'))
    graph.add_conditional_edges('run_step', nodes.next_step,
                                _map('run_step', 'revise_plan', 'finalize'))
    graph.add_conditional_edges('finalize', _next_after_finalize,
                                _map('revise_plan'))
    return graph.compile(checkpointer=checkpointer_)
