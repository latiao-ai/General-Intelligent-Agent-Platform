"""机器人的提示词与身份：人设、花名册、协作规则、能力纪元、房间上下文。

对应 `docs/bot-mode.md`。这个模块**纯函数**：不碰数据库、不发请求。所有需要
读库的东西由调用方（`addons/resolver.py`、`bot/mailbox.py`）取好传进来。

为什么要单拎出来：这些字符串决定"机器人是谁、能看见谁、能不能互相说话"，
是 Bot Mode 里最容易被悄悄改坏的一层。纯函数才能被单测钉住——花名册多一行、
`@` 解析错一个字符，都会让机器人之间开始互相刷屏（见 docs 里的反 ping-pong）。
"""
from __future__ import annotations

import hashlib
import json
import re

# 规范会话的标题常量。**注意**：本项目不靠标题解析会话（`sessions` 有显式的
# `session_kind` + 唯一索引），这个常量只用于展示与"人可读的一致性"：
# 导出、日志、另一个客户端看到的都是同一个名字。
BOT_CHAT_TITLE = 'Bot Chat'
ROOM_SESSION_TITLE_PREFIX = 'Group: '

# 静默令牌：房间里"没有补充"时，成员可以只回这一句。调度器据此提前收束，
# 而不是逼着每个人把同一句话换个说法再说一遍。
SILENCE_TOKEN = '（无补充）'
_SILENCE_ALIASES = frozenset({
    SILENCE_TOKEN, '（无补充）', '(无补充)', '无补充', '（无更多补充）', 'pass',
    'PASS', '无', '（已达成结论）',
})

# 花名册一行给模型的长度上限（含 handle / 标题 / 描述）。
ROSTER_LINE_MAX = 160
# 花名册最多列多少个队友：机器人再多也不该把提示词撑爆。
ROSTER_MAX = 40

# 房间上下文窗口：条数 + 字符数双上限（与 Hermes 的 200 条/32k 同思路，
# 这里更保守——本项目的模型上下文预算还要留给技能索引与工具声明）。
ROOM_WINDOW_MESSAGES = 24
ROOM_WINDOW_CHARS = 12000
ROOM_MESSAGE_MAX_CHARS = 2000

# 一次性回复回合的无人值守策略：没有人看着的时候，危险动作默认拒绝。
# 这是 Bot 自动化的安全底线（Hermes 的 `approvals.single_query_mode: deny`）。
UNATTENDED_APPROVAL = 'deny'

_HANDLE_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,23}$')
_MENTION_RE = re.compile(r'@([A-Za-z0-9_-]{1,32})')


def normalize_handle(raw: str, *, name: str = '') -> str:
    """把用户输入的名字收敛成合法 handle（小写、字母数字、`-` `_`）。

    非法字符不报错，直接丢掉：创建机器人时更好的体验是"你输入的「代码 审查」
    变成了 `code-shencha`"，而不是弹一个红框让人猜规则。中文名会退化成
    `bot-<hash>` 形式，保证非空且稳定。
    """
    text = str(raw or '').strip().lower()
    text = re.sub(r'[^a-z0-9_-]+', '-', text).strip('-_')
    text = re.sub(r'-{2,}', '-', text)
    if _HANDLE_RE.match(text):
        return text
    source = str(raw or name or '').strip()
    if not source:
        return ''
    digest = hashlib.sha256(source.encode('utf-8')).hexdigest()[:6]
    return f'bot-{digest}'


def is_valid_handle(handle: str) -> bool:
    return bool(_HANDLE_RE.match(str(handle or '')))


def parse_mentions(text: str) -> list[str]:
    """从一句话里取出所有 `@handle`（保序去重、统一小写）。"""
    seen: list[str] = []
    for match in _MENTION_RE.finditer(str(text or '')):
        handle = match.group(1).lower()
        if handle not in seen:
            seen.append(handle)
    return seen


def is_silence(text: str) -> bool:
    """这句话是不是"没有补充"的静默令牌。"""
    normalized = ' '.join(str(text or '').split())
    if not normalized:
        return True
    if normalized in _SILENCE_ALIASES:
        return True
    # 允许"（无补充）"前后带标点/空白
    stripped = normalized.strip('。.!！,，、 ')
    return stripped in _SILENCE_ALIASES


def _one_line(text: str, limit: int) -> str:
    value = ' '.join(str(text or '').split())
    return value if len(value) <= limit else value[:limit - 1] + '…'


def render_roster(bots: list[dict], *, self_bot_id: str = '') -> str:
    """队友花名册：一行一个，`@handle — title — description`。

    只列**同一主体名下、启用中**的机器人（调用方负责过滤），排除自己：
    告诉模型"你可以给自己发消息"只会诱发无意义的自问自答。
    """
    lines: list[str] = []
    for bot in bots or []:
        if not isinstance(bot, dict):
            continue
        bot_id = str(bot.get('bot_id') or '')
        if bot_id and bot_id == self_bot_id:
            continue
        if str(bot.get('status') or 'active') != 'active' or bot.get('hidden'):
            continue
        handle = str(bot.get('handle') or '').strip()
        if not handle:
            continue
        title = str(bot.get('title') or '').strip()
        description = str(bot.get('description') or '').strip()
        parts = [f'- @{handle}']
        if title:
            parts.append(f'— {_one_line(title, 40)}')
        if description:
            parts.append(f'— {_one_line(description, 90)}')
        line = ' '.join(parts)
        lines.append(line[:ROSTER_LINE_MAX])
        if len(lines) >= ROSTER_MAX:
            break
    return '\n'.join(lines)


def messaging_rules() -> str:
    """`message_bot` 的使用规则。**只在规范会话里注入**（gate 见 bot/service.py）。

    反 ping-pong 规则（"只找一个最相关的队友，不要群发"）写在这里，和 Hermes 的
    `## Messaging other agents` 同一思路：平台级环路守卫只能挡消息平台入站，
    机器人之间的往返只能靠提示词 + 有界转述深度（`max_hops`）一起兜。
    """
    return (
        '## 与其他机器人协作\n'
        '- 你有一份队友花名册（见下）。需要别人专业能力时，调用 `message_bot` '
        '把请求发给**一个**最相关的队友，不要群发。\n'
        '- 消息是**异步**的：调用后立刻拿到"已受理"，答复会在之后作为一条新消息'
        '出现在这个会话里，不要在同一轮里反复发同一条消息催。\n'
        '- 转发别人给你的内容时，保留原话里的关键事实与出处，不要替对方编造结论。\n'
        '- 用户直接问你就能答的事，不要转给队友；转手一次就是一次额外的等待与费用。\n'
        '- 收到队友的回复时，先用一句话告诉用户"谁回复了什么"，再决定要不要继续处理。'
    )


def capability_fingerprint(bot: dict, roster: list[dict]) -> str:
    """能力面指纹（12 hex）：人设、模型、技能、连接器、队友花名册。

    本项目每轮都会重建系统提示（没有跨轮 prompt 缓存），所以这个指纹不是
    "要不要重建 prompt"的开关，而是**变更检测 + 可审计**：指纹写进提示词，
    也写进 `sessions.capability_epoch`；两者不一致时提示模型"能力面已变化，
    旧索引不要沿用"（见 `capability_notice`）。

    漏算一项的后果和 Hermes 一样：装了技能/换了模型，机器人却按老能力行事。
    新增"会改变能力面"的字段时**必须**加进这里。
    """
    payload = {
        'soul': str(bot.get('soul') or ''),
        'model': str(bot.get('model') or ''),
        'skills': sorted(str(x) for x in (bot.get('skill_ids') or ())),
        'connectors': sorted(str(x) for x in (bot.get('connector_ids') or ())),
        'roster': [
            [str(item.get('bot_id') or ''), str(item.get('handle') or ''),
             str(item.get('title') or ''), str(item.get('description') or '')]
            for item in (roster or []) if isinstance(item, dict)
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    return digest.hexdigest()[:12]


def capability_notice(stored: str, current: str) -> str:
    """指纹变了才出现的一段提示；没变就是空串（不占 token）。"""
    if not current or str(stored or '') in ('', current):
        return ''
    return ('\n\n【能力面已更新】你的技能/模型/队友自上次对话后发生了变化，'
            '请按当前工具与技能清单行事，不要沿用旧判断。')


def persona_block(bot: dict, *, roster: list[dict], epoch: str) -> str:
    """机器人的身份段：我是谁、我负责什么、队友是谁、怎么说话。

    顺序是刻意的：先身份（stable）→ 再人设（用户可改）→ 再花名册（易变）
    → 最后规则。稳定内容在前，模型读起来更不容易把人设和协作规则混在一起。
    """
    name = str(bot.get('name') or '').strip() or '未命名机器人'
    handle = str(bot.get('handle') or '').strip()
    title = str(bot.get('title') or '').strip()
    description = str(bot.get('description') or '').strip()
    soul = str(bot.get('soul') or '').strip()
    head = f'【你是 {name}' + (f'（@{handle}）' if handle else '') + '】'
    if title:
        head += f' — {title}'
    lines = [head]
    if description:
        lines.append(description)
    if soul:
        lines.append('')
        lines.append(soul)
    roster_text = render_roster(roster, self_bot_id=str(bot.get('bot_id') or ''))
    if roster_text:
        lines.append('')
        lines.append('## 你的队友')
        lines.append(roster_text)
    lines.append('')
    lines.append(messaging_rules())
    lines.append('')
    lines.append(f'Capability epoch: {epoch}')
    return '\n'.join(lines)


def room_rules(room: dict, members: list[dict]) -> str:
    """房间回合的规则段：只发言一轮、不要抢话、说清依据、可以弃权。"""
    name = str(room.get('name') or '').strip() or '未命名房间'
    topic = str(room.get('topic') or '').strip()
    roster = ', '.join(
        f'{item.get("name") or item.get("handle")}(@{item.get("handle")})'
        for item in members if isinstance(item, dict) and item.get('handle'))
    lines = [f'【群聊：{name}】' + (f' 议题：{topic}' if topic else '')]
    if roster:
        lines.append(f'成员：{roster}。')
    lines.append(
        '你在讨论里**只发一轮言**：给出你的判断、依据或下一步，不要复述别人已经说过的话，'
        '也不要假装还在等谁。\n'
        f'如果这一轮你没有新信息，只回复「{SILENCE_TOKEN}」，不要为了凑字数而发言。\n'
        '不要 `@` 全体成员；需要某个人接话时才 `@handle` 点名一个人。\n'
        '回答控制在 200 字以内，除非用户明确要求展开。'
    )
    return '\n'.join(lines)


def room_transcript(events: list[dict], *, handles: dict[str, str],
                    limit_messages: int = ROOM_WINDOW_MESSAGES,
                    limit_chars: int = ROOM_WINDOW_CHARS) -> str:
    """把房间事件渲染成模型可读的对话记录（有界窗口）。

    窗口是**双上限**：条数 + 字符数。只按条数截，一条超长消息就能把上下文撑爆；
    只按字符截，又会在大量短消息时丢掉最近的几轮。单条超长时截断并标注。
    """
    msgs = [item for item in (events or [])
            if isinstance(item, dict)
            and str(item.get('kind') or '') in ('message.user', 'message.member')]
    omitted = max(0, len(msgs) - max(0, int(limit_messages)))
    if omitted:
        msgs = msgs[-int(limit_messages):]
    rendered: list[str] = []
    used = 0
    for item in msgs:
        payload = item.get('payload') or {}
        actor = str(item.get('actor') or '')
        if actor.startswith('bot:'):
            who = '@' + (handles.get(actor[4:], actor[4:]) or 'bot')
        else:
            who = '用户'
        text = ' '.join(str(payload.get('text') or '').split())
        if not text:
            continue
        if len(text) > ROOM_MESSAGE_MAX_CHARS:
            text = text[:ROOM_MESSAGE_MAX_CHARS] + f'…（原消息 {len(text)} 字，已截断）'
        line = f'[{who}] {text}'
        if used + len(line) > limit_chars:
            omitted += 1
            continue
        used += len(line)
        rendered.append(line)
    head = [f'（以下是房间最近的对话，省略了更早的 {omitted} 条）'] if omitted else []
    return '\n'.join(head + rendered)


def routine_turn_text(name: str, output: str) -> str:
    """例行任务结果交付给机器人时的那一轮输入（不是用户说的话）。

    Hermes 的做法值得照抄：明确告诉模型"这是定时任务的输出、不是用户发的"，
    否则它会用聊天口吻回一句"你好，有什么可以帮你"，把真正的结果丢掉。
    """
    body = str(output or '').strip() or '（本次运行没有产出文本）'
    title = str(name or '').strip() or '定时任务'
    return (
        f'[Cronjob "{title}" output — scheduled job, not the user. '
        'Review it, act on anything that needs action, and summarize for the chat.]\n\n'
        f'{body}'
    )


def routine_mirror_prefix(name: str, when: str = '') -> str:
    """投递到会话列表/侧栏时的镜像标签（Hermes 的 `[Cron delivery: …]`）。"""
    title = str(name or '').strip() or '定时任务'
    suffix = f'（{when}）' if str(when or '').strip() else ''
    return f'[Cron delivery: {title}{suffix}]'


def dm_turn_text(sender: dict, body: str, *, hop: int = 0, max_hops: int = 2) -> str:
    """队友私信变成目标机器人会话里的一轮输入。

    署名是必须的：模型看不到"这行是谁发的"就只能靠猜，结果会把队友的请求
    当成用户本人说的（"你刚才不是让我改代码吗"）。
    """
    handle = str(sender.get('handle') or 'bot').strip()
    name = str(sender.get('name') or handle).strip()
    text = str(body or '').strip()
    head = f'Message from 🤖 {name} (@{handle}):'
    if int(hop) > 0:
        head += f'\n（这是一条转述，已经转手 {int(hop)} 次；最多允许 {int(max_hops)} 次。）'
    return f'{head}\n\n{text}'


def reply_turn_text(sender: dict, answer: str, *, hop: int, max_hops: int) -> str:
    """队友的答复回到发送方会话时的那一轮输入。

    和 `dm_turn_text` 一样必须署名，同时告诉模型"这是一次异步答复"：它上一轮
    已经把请求发出去了，这一轮要做的不是再发一遍，而是把结果接住/转述给用户。
    """
    handle = str(sender.get('handle') or 'bot').strip()
    name = str(sender.get('name') or handle).strip()
    body = str(answer or '').strip() or '（对方没有给出文本答复）'
    tail = ''
    if int(hop) >= int(max_hops):
        tail = ('\n\n（这是这条链上的最后一跳：不要再给队友发消息，'
                '直接把结论告诉用户。）')
    return (f'Reply from 🤖 {name} (@{handle}) — 这是你之前请求的答复，'
            f'不是用户发的新消息：\n\n{body}{tail}')


def relay_limit_note(sender: dict, answer: str, *, hop: int, max_hops: int) -> str:
    """超过转述深度上限时，只落一条消息、不再起回合（防机器人互相刷屏）。"""
    handle = str(sender.get('handle') or 'bot').strip()
    name = str(sender.get('name') or handle).strip()
    body = str(answer or '').strip() or '（对方没有给出文本答复）'
    return (f'[队友答复 · 已达转述上限 {int(hop)}/{int(max_hops)}，不再自动回复]\n'
            f'🤖 {name} (@{handle})：{body}')


def room_publication(answer: str) -> dict:
    """一次成员发言的发布形状：文本 + 是否静默 + 是否要求结束讨论。"""
    text = ' '.join(str(answer or '').split())
    silent = is_silence(text)
    return {'text': text or SILENCE_TOKEN, 'silent': silent}


def room_turn_text(room: dict, transcript: str, *, epoch: int, round_no: int,
                   position: int) -> str:
    """房间回合的输入正文：本次发言机会 + 有界对话记录 + 只说一轮。

    记录放在**用户消息**里而不是系统提示里：每个成员都有自己的房间会话，
    会话历史只包含"它自己说过的话 + 它被叫到时看到的记录"，把记录放进提示词
    会让模型分不清"这是别人说的"还是"这是系统设定"。
    """
    name = str(room.get('name') or '').strip() or '房间'
    topic = str(room.get('topic') or '').strip()
    lines = [f'【群聊发言机会 · {name} · 第 {int(epoch)} 个议题 · 第 {int(round_no)} 轮'
             f' · 第 {int(position)} 位】']
    if topic and int(round_no) == 0:
        lines.append(f'议题：{topic}')
    if transcript:
        lines.append('')
        lines.append(transcript)
    lines.append('')
    lines.append('现在轮到你发言。只输出你这一轮要说的话；没有新信息就只回'
                 f'「{SILENCE_TOKEN}」。')
    return '\n'.join(lines)
