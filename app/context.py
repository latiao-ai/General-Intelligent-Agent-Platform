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

# 用途决定需要哪些材料（文档 §11）。不同用途给不同预算，不要一份预算打天下。
PURPOSES = ('plan', 'replan', 'transform', 'final_answer', 'summarize',
            'memory_extract')

# 各用途的预算（token）——**唯一口径**。
# plan 只要判断意图；final_answer 要能答清楚；transform/summarize 处理大块
# 内容，预算最大。以前这里还有第二张 BUDGETS 表给 select() 用，两张表各写各的，
# 数值当时碰巧一样，改一处就悄悄漂了。现在 BUDGETS 由这张表派生。
PURPOSE_BUDGETS = {
    'plan': 1800,
    'replan': 2200,
    'final_answer': 3500,
    'transform': 6000,
    'summarize': 6000,
    'memory_extract': 3000,
}

# 调用点的历史用途 → 预算用途。chat_messages 传的是 'answer'，
# build 传的是 'final_answer'，两者必须映射到同一个数。
PURPOSE_ALIASES = {'answer': 'final_answer', 'compose': 'transform'}

BUDGETS = dict(PURPOSE_BUDGETS)
BUDGETS.update({'answer': PURPOSE_BUDGETS['final_answer']})

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
    resolved = PURPOSE_ALIASES.get(purpose, purpose)
    return PURPOSE_BUDGETS.get(resolved, PURPOSE_BUDGETS['final_answer'])


def select(history, purpose: str = 'answer') -> list[dict]:
    """从最近往前挑消息，直到用完预算。

    永远至少返回一条：完全不带上文比多花点 token 更糟。

    这里**委托给 build()**，而不是自己再实现一遍截断逻辑：两套实现就是两个
    口径，迟早分叉（历史上就分叉过）。生产路径（规划、作答）都经由
    `select`/`chat_messages` 走到 `build`，预算报告因此是真实的。
    """
    return build(purpose=purpose, history=history)['messages']


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


# 固定优先级：规则与权限 → 当前请求 → 状态与待核实 → 工具 → 证据 → 记忆 → 历史
_PRIORITY = ('rules', 'request', 'state', 'tools', 'evidence', 'memory', 'history')


def build(*, purpose: str = 'final_answer', rules: str = '', request: str = '',
          state: str = '', tools: str = '', evidence: str = '',
          history=None, memory: dict | None = None) -> dict:
    """按用途装配模型输入，并返回**预算报告**（文档 §11）。

    报告里写清每个板块用了多少、有没有被截断——"结果被截断却说成全量"是
    文档点名要避免的失败模式，所以截断必须在报告里显式可见，交给调用方决定
    是缩小候选、分页还是明确告知模型。

    核心指令与最小 schema 已经超预算时**不做静默截断**：直接标记
    `over_budget=True`，让上层选择缩小范围或暂停。
    """
    budget = budget_of(purpose)
    sections = {'rules': rules or '', 'request': request or '',
                'state': state or '', 'tools': tools or '',
                'evidence': evidence or ''}
    report = {'purpose': purpose, 'budget': budget, 'sections': {},
              'truncated': [], 'over_budget': False}

    used = 0
    for name in _PRIORITY:
        if name in ('memory', 'history'):
            continue
        text = sections.pop(name, '')
        cost = estimate_tokens(text)
        report['sections'][name] = cost
        used += cost

    # 记忆与历史按剩余额度填充，从最老往前砍
    memory_block = memory_text(memory)
    memory_cost = estimate_tokens(memory_block)
    report['sections']['memory'] = memory_cost
    used += memory_cost

    remaining = max(0, budget - used)
    picked, spent = [], 0
    for message in reversed(list(history or [])):
        cost = estimate_tokens(message.get('content') or '')
        if picked and spent + cost > remaining:
            report['truncated'].append('history')
            break
        picked.append({'role': message.get('role') or 'user',
                       'content': message.get('content') or ''})
        spent += cost
        if spent >= remaining:
            break
    picked.reverse()
    # 只要还有历史没被装进去，就是截断过——哪怕原因是单条就吃满了预算
    if len(picked) < len(list(history or [])) and 'history' not in report['truncated']:
        report['truncated'].append('history')
    report['sections']['history'] = spent
    report['sections']['total'] = used + spent

    if report['sections'].get('rules', 0) + report['sections'].get('tools', 0) > budget:
        # 规则和工具 schema 已经撑爆预算：不能静默截断关键约束
        report['over_budget'] = True

    return {'purpose': purpose, 'messages': picked,
            'memory_block': memory_block, 'sections': sections,
            'report': report}


def chat_messages(history, question: str, purpose: str = 'answer',
                  prefix: str = '') -> list[dict]:
    """给对话式调用准备的 messages：最近几轮原文 + 本轮问题。

    prefix 用于附加本轮工具结果/资料（放在问题前面，让模型先看数据再看问题）。
    """
    body = question if not prefix else f'{prefix}\n\n{question}'
    return select(history, purpose) + [{'role': 'user', 'content': body}]
