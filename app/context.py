"""上下文装配：把"该给模型看什么"集中到一处，并按 token 预算裁剪。

在引入这一层之前，每个入口各拼各的：规划器自己截 `[-8:]`、直答再截一次、
联网作答**根本不带历史**、整理报告只看资料。口径分散的后果是预算失控——
一条带来源的联网作答就有 1.4 KB，八条就能吃满窗口，而更早的对话被无声丢弃。

现在的口径：

- **预算按 token，不按条数**。没有引入分词器，用保守估算（见 estimate_tokens），
  偏保守的方向是"少给"，因为预算超了会真的被截断。
- **会话记忆优先于原文**：摘要与关键事实放在 system 里，占固定小额度；
  剩下的额度留给最近几轮原文。
- 各用途给不同预算：规划只要"用户在说什么"，作答要"答什么"。
"""
import re

# 每轮的常用预算（token）。规划比作答小：它只需要判断意图，不需要复述细节。
BUDGETS = {
    'plan': 1800,
    'answer': 3500,
}

_CJK = re.compile(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]')


def estimate_tokens(text: str) -> int:
    """粗估 token 数。

    实测校准：整理报告阶段输入 10000 字符正文 → 约 1.1 万 token，
    即中日韩字符大致 1 token/字；英文与符号约 0.3 token/字符。
    估算偏保守（宁可少塞），避免真实请求超窗。
    """
    if not text:
        return 0
    body = str(text)
    cjk = len(_CJK.findall(body))
    others = len(body) - cjk
    return cjk + int(others * 0.3) + 1


def budget_of(purpose: str) -> int:
    return BUDGETS.get(purpose, BUDGETS['answer'])


def select(history, purpose: str = 'answer') -> list[dict]:
    """从最近往前挑消息，直到用完预算。

    永远至少返回一条：完全不带上文比多花点 token 更糟。
    """
    limit = budget_of(purpose)
    picked, used = [], 0
    for message in reversed(list(history or [])):
        content = message.get('content') or ''
        cost = estimate_tokens(content)
        if picked and used + cost > limit:
            break
        picked.append({'role': message.get('role') or 'user', 'content': content})
        used += cost
        if used >= limit:
            break
    picked.reverse()
    return picked


def memory_text(memory: dict | None) -> str:
    """把会话记忆拼成一段可注入的文本（没有就返回空串）。"""
    if not memory:
        return ''
    parts = []
    summary = (memory.get('summary') or '').strip()
    if summary:
        upto = memory.get('upto') or 0
        head = f'[会话摘要]（此前 {upto} 条对话的压缩结果，非原文）' if upto else '[会话摘要]'
        parts.append(head + '\n' + summary)
    facts = memory.get('facts') or {}
    if facts:
        lines = '\n'.join(f'- {key}：{value}' for key, value in facts.items())
        parts.append('[会话关键事实]\n' + lines)
    return '\n\n'.join(parts)


def system_with_memory(system: str, memory: dict | None) -> str:
    """把会话记忆拼到 system prompt 末尾。"""
    block = memory_text(memory)
    if not block:
        return system
    return f'{system}\n\n{block}\n（以上是更早对话的压缩结果，只作背景，不要逐字复述。）'


def chat_messages(history, question: str, purpose: str = 'answer',
                  prefix: str = '') -> list[dict]:
    """给对话式调用准备的 messages：最近几轮原文 + 本轮问题。

    prefix 用于附加本轮工具结果/资料（放在问题前面，让模型先看数据再看问题）。
    """
    body = question if not prefix else f'{prefix}\n\n{question}'
    return select(history, purpose) + [{'role': 'user', 'content': body}]
