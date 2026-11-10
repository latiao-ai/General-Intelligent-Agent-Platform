"""Pi 工具循环上的产品行为：确认边界、无进展停手、完成条件核对。

这些规则以前挂在图的 plan / observe / verify 节点上。Pi 不跑那张图，
但画布和 LibTV 仍然需要同样的产品结果：

- 提议和提交不能在同一轮里连着执行（用户确认边界）；
- 同样的调用反复出现、没有新结果时停手，而不是空转；
- 模型声称做完时，声明过的产物和部署回执仍要核对。
"""
from __future__ import annotations

import os

from webpage_agent.agent.plan_validate import check_confirmation_boundary
from webpage_agent.observe.observation import (
    completion_state,
    criteria_of,
    progress_pair,
)


def confirmation_block(history: list[dict], tool, catalog) -> str:
    """下一个工具若会越过用户确认边界，返回要停下来告诉用户的原因。"""
    if tool is None or not getattr(tool, 'server_id', ''):
        return ''
    steps = list(history or [])
    steps.append({
        'tool': getattr(tool, 'name', '') or '',
        'step_id': f'c{len(steps) + 1}',
        'args': {},
    })
    try:
        issues = check_confirmation_boundary(steps, catalog or [])
    except Exception:  # noqa: BLE001 — 目录不完整时不拦正常调用
        return ''
    for issue in issues or []:
        if issue.get('code') == 'confirmation_bypasses_user':
            return str(issue.get('detail') or '需要用户确认后才能继续')
    return ''


def repeat_key(tool_name: str, args: dict | None) -> str:
    """同样的工具、同样的参数算同一次进展指纹（结果要等调用后才知道）。"""
    return progress_pair(tool_key=tool_name or '', args=args or {}, result={})


def repeat_block(counts: dict, key: str, limit: int) -> str:
    """同一指纹已经达到容忍次数时，返回停手原因。调用方在成功后自行加一。"""
    if not key:
        return ''
    if int(counts.get(key) or 0) >= max(1, int(limit or 1)):
        return '同样的调用没有新进展，已停止以免空转'
    return ''


def note_repeat(counts: dict, key: str) -> None:
    if key:
        counts[key] = int(counts.get(key) or 0) + 1


def completion_block(state: dict | None) -> str:
    """模型给出最终答复之后，核对产物和部署回执。没有条件就不拦。"""
    state = state or {}
    criteria = criteria_of(state.get('plan') if isinstance(state.get('plan'), dict) else {},
                           state)
    if not criteria:
        return ''
    report = completion_state(criteria, state=state, files_exist=os.path.isfile)
    unmet = report.get('unmet') or []
    if not unmet:
        return ''
    reasons = []
    for item in unmet:
        text = str(item.get('reason') or item.get('detail') or '完成条件未满足')
        if text not in reasons:
            reasons.append(text)
    return '；'.join(reasons)
