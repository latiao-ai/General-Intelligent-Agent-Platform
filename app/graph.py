import inspect
import time

from langgraph.graph import StateGraph, START, END
from app.events import emit, stage_end, stage_start
from app.schemas import AgentState
from app import nodes

# 节点清单（即实际执行顺序）；执行器计时与前端耗时表都以此为准。
NODE_NAMES = [
    'parse_request', 'discover_sources', 'fetch_pages',
    'compose_report', 'render_html', 'validate_artifact',
    'deploy_report', 'finish_failed',
]


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


def build_graph():
    graph = StateGraph(AgentState)
    for name in NODE_NAMES:
        graph.add_node(name, instrument(name, getattr(nodes, name)))
    graph.add_edge(START, 'parse_request')
    graph.add_edge('parse_request', 'discover_sources')
    graph.add_edge('discover_sources', 'fetch_pages')
    graph.add_conditional_edges('fetch_pages', nodes.assess_coverage, {
        'discover_sources': 'discover_sources',
        'compose_report': 'compose_report',
        'finish_failed': 'finish_failed',
    })
    graph.add_edge('compose_report', 'render_html')
    graph.add_edge('render_html', 'validate_artifact')
    graph.add_conditional_edges('validate_artifact', nodes.route_validation, {
        'deploy_report': 'deploy_report', 'finish_failed': 'finish_failed',
    })
    graph.add_edge('deploy_report', END)
    graph.add_edge('finish_failed', END)
    return graph.compile()
