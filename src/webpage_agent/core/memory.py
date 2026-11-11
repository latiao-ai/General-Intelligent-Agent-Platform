"""会话记忆：把更早的对话压成摘要 + 关键事实。

长会话不可能每轮都把全部原文塞进 prompt。这里做两件事：

1. **滚动摘要**：未摘要的消息超过阈值时，把最老的一段合并进已有摘要；
2. **关键事实**：结构化 kv（目标 / 已否决 / 关键约束…），覆盖式更新，
   注入时优先级高于摘要——它是"硬结论"，摘要只是叙述。

沿用画像那套原则（文件即记忆的延伸）：

- 产物是**人可读的文本**，用户可以在界面里直接改（见 `/api/sessions/{id}/memory`）；
- **原文永远留在 messages 表**，摘要只是"注入视图"，随时可以重新生成；
- 压缩失败不影响任务：继续用旧摘要，绝不因为压缩把一轮对话搞挂。
"""
import json
import re

from webpage_agent.agent import context
from webpage_agent.core.schemas import SessionMemory
from webpage_agent.core.services import structured_call

# 最近这么多条永远保留原文，不进摘要
KEEP_RECENT = 6
# 未摘要条数超过这个就压缩
MAX_MESSAGES = 12
# 或者未摘要部分超过这个 token 数
MAX_TOKENS = 3000

SUMMARY_SYSTEM = (
    '你负责压缩对话历史。把「已有摘要」与「需要合并的对话」合成一份新的记忆，'
    '输出两个字段：'
    'summary：不超过 300 字的中文摘要，必须保留用户的目标、已经完成的关键动作、'
    '已经确认的结论、已被否决的方案、以及尚未解决的问题；丢弃寒暄与重复内容。'
    'facts：从整段对话里提炼的、后续会用到的硬结论，键用简短中文（如"目标""已否决""约束"），'
    '值一句话；没有就留空 {}}。'
    '只写对话里明确出现过的信息，不要推测、不要编造。'
)


def parse_facts(text: str) -> dict:
    """把「键：值」多行文本解析成字典（界面编辑用）。"""
    facts = {}
    for line in (text or '').splitlines():
        line = line.strip().lstrip('-').strip()
        if not line:
            continue
        for sep in ('：', ':'):
            if sep in line:
                key, value = line.split(sep, 1)
                if key.strip():
                    facts[key.strip()] = value.strip()
                break
    return facts


def render_facts(facts: dict) -> str:
    return '\n'.join(f'{key}：{value}' for key, value in (facts or {}).items())


def needs_compaction(messages: list[dict]) -> bool:
    if len(messages) <= MAX_MESSAGES:
        chars = sum(len(m.get('content') or '') for m in messages)
        return context.estimate_tokens('x' * chars) > MAX_TOKENS
    return True


async def compact(store, session_id: str) -> dict:
    """必要时压缩会话记忆，返回最新记忆（含 compacted/folded 供调用方播报）。

    任何异常都退化成"不压缩"，绝不向上抛：压缩是优化，不是任务的前置条件。
    """
    memory = await store.memory(session_id)
    pending = await store.messages_after(session_id, memory.get('upto') or 0)
    if not needs_compaction(pending):
        return {**memory, 'compacted': False}

    fold = pending[:-KEEP_RECENT]
    if not fold:
        return {**memory, 'compacted': False}

    payload = json.dumps({
        '已有摘要': memory.get('summary') or '',
        '已有事实': memory.get('facts') or {},
        '需要合并的对话': [{'role': m['role'], 'content': m['content']}
                          for m in fold],
    }, ensure_ascii=False)
    try:
        result, _ = await structured_call(
            SessionMemory, SUMMARY_SYSTEM, payload,
            model=None, disable_thinking=True)
        data = result.model_dump()
    except Exception:
        return {**memory, 'compacted': False}

    summary = (data.get('summary') or '').strip()
    facts = data.get('facts') or {}
    upto = fold[-1]['id']
    await store.set_memory(session_id, summary=summary, facts=facts, upto=upto)
    return {'summary': summary, 'facts': facts, 'upto': upto,
            'compacted': True, 'folded': len(fold)}


def render_long_term(user_rows: list[dict], workspace_rows: list[dict],
                     max_chars: int = 4000) -> str:
    """把用户级/工作区长期记忆拼成给模型的 <memory> 块。"""
    def lines(rows):
        out = []
        for row in rows or []:
            predicate = str(row.get('predicate') or '').strip()
            value = str(row.get('value') or '').strip()
            if predicate and value:
                out.append(f'- {predicate}：{value}')
        return out
    user_lines = lines(user_rows)
    ws_lines = lines(workspace_rows)
    if not user_lines and not ws_lines:
        return ''
    parts = ['<memory>', '以下是跨会话记忆，只作为背景；当前用户输入和当前事实优先。']
    if user_lines:
        parts.append('用户长期记忆：')
        parts.extend(user_lines)
    if ws_lines:
        parts.append('当前工作区记忆：')
        parts.extend(ws_lines)
    parts.append('</memory>')
    text = '\n'.join(parts)
    return text[:max_chars]


_MEMORY_CUE = re.compile(
    r'(记住|记一下|以后都|以后请|始终|总是|不要|偏好|习惯|'
    r'我的名字|我叫|我喜欢|我不喜欢|remember|always|never)',
    re.IGNORECASE,
)

_MEMORY_EXTRACT_SYSTEM = (
    '你负责从一轮对话中提取**用户长期记忆**。只提取用户明确表达、以后仍然有用的：'
    '身份、长期偏好、固定规则、长期项目事实。不要提取一次性请求、临时天气、'
    '工具报错、搜索结果、密码/Token/API Key 等秘密。'
    '输出 facts：键简短中文，值一句话；最多 6 条；没有就留空。'
)


def has_memory_cue(text: str) -> bool:
    return bool(_MEMORY_CUE.search(str(text or '')))


async def extract_user_memory(store, *, scope_id: str, tenant_id: str,
                              user_message: str, answer: str,
                              created_by: str = 'default') -> int:
    """从一轮对话提取长期记忆并写入 user scope；失败静默。"""
    import json
    from webpage_agent.core.schemas import SessionMemory
    payload = json.dumps({
        '用户': user_message,
        '助手': answer,
    }, ensure_ascii=False)
    try:
        result, _ = await structured_call(
            SessionMemory, _MEMORY_EXTRACT_SYSTEM, payload,
            model=None, disable_thinking=True)
        facts = result.model_dump().get('facts') or {}
    except Exception:                              # noqa: BLE001
        return 0
    count = 0
    for key, value in list(facts.items())[:6]:
        key = str(key).strip()
        value = str(value).strip()
        if not key or not value or len(value) > 500:
            continue
        saved = await store.remember(
            scope_type='user', scope_id=scope_id, subject='用户',
            predicate=key, value=value, source='auto',
            created_by=created_by, tenant_id=tenant_id)
        if saved:
            count += 1
    return count
