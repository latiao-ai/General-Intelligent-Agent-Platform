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
NODE_NAMES = ['plan', 'run_step']
PLAIN_NODES = {'run_step'}


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


def build_graph(checkpointer_=None):
    """Planner-Executor：先规划，再逐步执行，直到计划走完。

    唯一的环在 run_step 上——它每次只执行一步，所以"搜几次、要不要成稿、
    要不要部署"由计划决定，而不是由图的固定边决定。
    """
    graph = StateGraph(AgentState)
    for name in NODE_NAMES:
        node = getattr(nodes, name)
        graph.add_node(name, node if name in PLAIN_NODES
                       else instrument(name, node))
    graph.add_edge(START, 'plan')
    graph.add_edge('plan', 'run_step')
    graph.add_conditional_edges('run_step', nodes.next_step, {
        'run_step': 'run_step',
        'end': END,
    })
    return graph.compile(checkpointer=checkpointer_)
