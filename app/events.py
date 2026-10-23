"""执行过程中的细粒度事件，供前端实时展示"智能体正在做什么"。

节点级进度（`stream_mode='updates'`）只告诉你"某个节点跑完了"；这里发的是节点
**内部**的动作与阶段起止，前者用来呈现思考过程，后者用来做实时计时。
"""
import time

from langgraph.config import get_stream_writer

# 各阶段的中文名，CLI / Streamlit / API 三处共用。
STAGE_LABELS = {
    # 编排本身
    'intake': '接续运行',
    'plan': '规划',
    'persist_plan': '保存计划',
    'revise_plan': '按新要求调整',
    'finalize': '收尾检查',
    'run_step': '执行步骤',
    # 工具（与 app/tools.py 的 Tool.label 对应）
    'web_search': '检索来源',
    'fetch_pages': '抓取正文',
    'render_sources_html': '资料转 HTML',
    'answer_with_sources': '联网作答',
    'answer_directly': '直接回答',
    'deploy_site': '部署站点',
    'manual_deploy': '手动部署',
    # 合成阶段：从服务端收到指令，到规划完成、真正开始调用工具之间的空档。
    # 它跨越了 plan 阶段，因此与"规划"的耗时会有重叠，这是刻意的：
    # 用户关心的是"我发出去之后多久才真正开始干活"。
    'instruction': '收到指令 → 开始执行',
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



def draft(report: dict | None) -> dict:
    """整理报告过程中的"半成品"报告，或 None 表示清空。

    这类事件体积大（每份都是一整份报告的快照）且是过程性的，
    **不写入 SQLite**（见 api_hub.TaskRecord.publish）；重连的客户端看不到草稿
    也不影响——最终报告本来就在 report.json / index.html 里。
    """
    return {'type': 'draft', 'report': report}


def answer_delta(text: str) -> dict:
    """直接回答时的增量文本（打字机效果）。

    条数多（一次回答可能上千片），**不写入 SQLite**；最终答案随 status 事件的
    detail 落库，重连时仍能看到完整回答。
    """
    return {'type': 'answer_delta', 'text': text}


def stage_start(stage: str, label: str | None = None) -> dict:
    """阶段开始。

    带 `at`（epoch 秒）是因为前端要按"现在 - 开始时刻"实时累加：
    事件到达前端可能被轮询或网络延迟几十到几百毫秒，
    用本地收到时刻计时会偏短，用服务端开始时刻才准。

    label 可显式传入：MCP 工具是运行时发现的，中文名写不进 STAGE_LABELS。
    """
    return {'type': 'stage_start', 'stage': stage,
            'label': label or label_of(stage), 'at': time.time()}


def stage_end(stage: str, seconds: float, status: str = '',
              label: str | None = None) -> dict:
    """阶段结束，带该阶段实际耗时。"""
    return {'type': 'stage_end', 'stage': stage,
            'label': label or label_of(stage),
            'seconds': round(seconds, 2), 'status': status, 'at': time.time()}
