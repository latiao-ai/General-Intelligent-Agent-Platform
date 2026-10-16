"""执行过程中的细粒度事件，供前端实时展示"智能体正在做什么"。

节点级进度（`stream_mode='updates'`）只告诉你"某个节点跑完了"；这里发的是节点
**内部**的动作与阶段起止，前者用来呈现思考过程，后者用来做实时计时。
"""
import time

from langgraph.config import get_stream_writer

# 各阶段的中文名，CLI / Streamlit / Web 三处共用。
STAGE_LABELS = {
    'parse_request': '解析需求',
    'discover_sources': '检索来源',
    'fetch_pages': '抓取正文',
    'compose_report': '整理报告',
    'render_html': '渲染 HTML',
    'validate_artifact': '校验产物',
    'deploy_report': '部署站点',
    'finish_failed': '提前终止',
    'manual_deploy': '手动部署',
}


def label_of(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage)


def emit(event: dict) -> None:
    """在节点内部发出自定义事件。

    不在 LangGraph 流式上下文里时（单元测试直接调用节点函数、或调用方没开
    custom 流）静默跳过，因此节点代码不必关心自己被谁、以何种方式调用。
    """
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    if writer is None:
        return
    writer(event)


def action(label: str, detail: str = '', icon: str = '') -> dict:
    """一个可展示的动作，如「已搜索网页 · 还珠格格 第一部 第3集」。"""
    return {'type': 'action', 'icon': icon, 'label': label, 'detail': detail}


def thought(text: str) -> dict:
    """智能体的计划陈述，如「我先帮你查找…」。"""
    return {'type': 'thought', 'text': text}


def outcome(**payload) -> dict:
    """终态结果（部署地址、产物路径等）。"""
    return {'type': 'result', **payload}


def stage_start(stage: str) -> dict:
    """阶段开始。

    带 `at`（epoch 秒）是因为前端要按"现在 - 开始时刻"实时累加：
    事件到达前端可能被轮询或网络延迟几十到几百毫秒，
    用本地收到时刻计时会偏短，用服务端开始时刻才准。
    """
    return {'type': 'stage_start', 'stage': stage, 'label': label_of(stage),
            'at': time.time()}


def stage_end(stage: str, seconds: float, status: str = '') -> dict:
    """阶段结束，带该阶段实际耗时。"""
    return {'type': 'stage_end', 'stage': stage, 'label': label_of(stage),
            'seconds': round(seconds, 2), 'status': status, 'at': time.time()}
