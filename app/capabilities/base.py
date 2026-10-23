"""能力包共享底座：源文档视图、工具结果上下文、错误类型。

原来这些都散在 tools.py 里，被多个能力包共用来回绕；抽出来之后
web / reports / publishing / answers 只依赖这一层。
"""
import json

from app import context, sources
from app.config import settings


class ToolError(RuntimeError):
    """工具执行失败。safe_message 会被透出到用户可见的失败原因里。

    **执行确定性必须显式带上**（`certainty`），不要靠异常类名去猜：

    - `not_executed`：确认没调用到远端（本地校验失败、连不上时明确未发送）；
    - `failed_confirmed`：远端明确回了失败；
    - `unknown`（默认）：说不清请求有没有送达——按未定处理，交给人核实。

    默认取 unknown 是刻意的：包装异常最容易丢信息，保守一点不会错。
    """

    def __init__(self, message: str, *, certainty: str = 'unknown',
                 phase: str = '', error_code: str = '',
                 receipt_ref: str = '') -> None:
        super().__init__(message)
        self.safe_message = message
        self.certainty = certainty
        self.phase = phase
        self.error_code = error_code or type(self).__name__
        self.receipt_ref = receipt_ref


def error_entry(stage: str, exc: BaseException) -> dict:
    return {'stage': stage, 'type': type(exc).__name__}


def _with_text(documents) -> list[dict]:
    """挑出真正能用的来源：已经拿到正文、且不是抓取失败的。

    判定依据是**有没有正文**，不是 status：检索接口常常直接带回正文，此时
    状态可能仍是 pending。只认 success 会让"搜索 → 作答/成稿"这种少一步的
    计划误报"还没有可用正文"。命中时顺手把状态提升成 success，
    后续导出页面才会把它们列进来源清单。
    """
    usable = []
    for doc in documents or []:
        if not sources.has_text(doc):
            continue
        if doc.get('status') != 'success':
            doc = dict(doc)
            doc['status'] = 'success'
        usable.append(doc)
    return usable


def usable_documents(state: dict) -> tuple[list[dict], str]:
    """本轮能用的正文来源，附带"来自哪里"的说明。

    本轮没检索、但用户说"把上面的内容整理成 HTML"时，退回上一轮已取得的正文。
    """
    current = _with_text(state.get('documents'))
    if current:
        return current, ''
    prior = _with_text(state.get('prior_documents'))
    if prior:
        return prior, '上一轮检索到的正文'
    return [], ''


def _select_sources(state: dict, documents: list[dict] | None = None
                    ) -> tuple[list[dict], bool]:
    """按字符预算挑正文；返回 (入选片段, 是否发生截断)。"""
    if documents is None:
        documents = state.get('documents', [])
    available = _with_text(documents)
    selected, remaining = [], settings.max_total_chars
    truncated = len(available) > settings.max_pages
    for doc in available[:settings.max_pages]:
        limit = min(settings.max_source_chars, remaining)
        if limit <= 0:
            truncated = True
            break
        # 正文不在 state 里，按需从 artifacts 读回来
        full = sources.load_text(doc)
        text = full[:limit]
        truncated = truncated or len(text) < len(full)
        selected.append({'source_id': doc['source_id'], 'title': doc['title'],
                         'text': text,
                         # 截断必须显式可见：否则模型会把"前 3000 字"当全文
                         'truncated': len(text) < len(full),
                         'chars_full': len(full)})
        remaining -= len(text)
    return selected, truncated


_TOOL_RESULT_CHARS = 4000
_TOOL_RESULT_TOTAL = 8000


def tool_results_text(state: dict) -> str:
    """本轮已经调用过的工具返回，供后续作答步骤使用。

    计划常是"先调工具取数据，再作答"两步；第二步若看不见第一步的返回，
    模型就会说"你没提供给我"——这个坑在 MCP 查询类问题上必现。
    """
    parts, used = [], 0
    for index, call in enumerate(state.get('mcp_calls') or [], 1):
        args = json.dumps(call.get('args') or {}, ensure_ascii=False)
        payload = json.dumps(call.get('payload'), ensure_ascii=False)
        room = min(_TOOL_RESULT_CHARS, _TOOL_RESULT_TOTAL - used)
        if room <= 0:
            break
        text = payload[:room]
        used += len(text)
        parts.append(f'[第{index}次工具调用] {call.get("tool")}  参数 {args}\n返回 {text}')
    return '\n\n'.join(parts)
