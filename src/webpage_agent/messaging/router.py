"""会话键、鉴权与命令。

鉴权**默认拒绝**：没有白名单、没有配对批准、策略又是关闭的，消息直接丢弃。
消息入口是最危险的面——它能让任何陌生人驱动一个带工具、带凭据的 Agent。

授权顺序（与 Hermes 的语义一致，但只保留本项目需要的四档）：

    会话类型策略（group: disabled/mention；dm: disabled/allowlist/pair/open）
    → 平台白名单（含 "*"）
    → 已配对名单
    → 默认 IGNORE

群聊额外要求"被 @"（`group_policy='mention'`）：群里每句话都触发 Agent 会把
token 烧穿，也会变成噪音源。
"""
from __future__ import annotations

from dataclasses import dataclass

from webpage_agent.messaging.models import Inbound

ALLOW = 'allow'
PAIR = 'pair'
IGNORE = 'ignore'
DECLINE = 'decline'

# 允许的配置值。未知值一律按 ignore 处理（配置写错不能变成放行）。
DM_POLICIES = ('open', 'allowlist', 'pair', 'ignore', 'disabled')
GROUP_POLICIES = ('mention', 'open', 'allowlist', 'ignore', 'disabled')

HELP_TEXT = (
    '可用命令：\n'
    '/help  看这段说明\n'
    '/new   开一段新对话（历史仍保留在本机）\n'
    '/stop  打断当前回复（如果正在生成）\n'
    '/who   看这个会话现在由哪个机器人回答\n'
    '/bind  看可选的机器人；`/bind 名字` 让这个会话由它回答\n'
    '/unbind 恢复跟随渠道默认机器人'
)


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str = ''

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW


def session_key(event: Inbound) -> str:
    """一个 IM 会话对应一条 Agent 会话；群聊按 thread 再分。"""
    return f'{event.platform}:{event.tenant}:{event.chat_id}:{event.thread_id or ""}'


def _normalized(value: str, allowed: tuple[str, ...], fallback: str) -> str:
    text = str(value or '').strip().lower()
    return text if text in allowed else fallback


def _listed(user_id: str, names: set[str]) -> bool:
    return bool(user_id) and (user_id in names or '*' in names)


def authorize(event: Inbound, *, dm_policy: str, group_policy: str,
              allowed_users, paired_users) -> Decision:
    """纯函数：消息 + 策略 → 决定。单测覆盖每一档。"""
    allowed = {str(item) for item in (allowed_users or []) if str(item)}
    paired = {str(item) for item in (paired_users or []) if str(item)}
    known = allowed | paired

    if event.chat_type in ('group', 'channel'):
        policy = _normalized(group_policy, GROUP_POLICIES, 'mention')
        if policy in ('disabled', 'ignore'):
            return Decision(IGNORE, 'group_disabled')
        if policy == 'mention' and not event.mentioned:
            return Decision(IGNORE, 'not_mentioned')
        if policy == 'open':
            return Decision(ALLOW, 'group_open')
        if _listed(event.user_id, known):
            return Decision(ALLOW, 'group_listed')
        # 群里不发配对码：那是公开场合，发码等于把入口暴露给所有人。
        return Decision(IGNORE, 'group_not_listed')

    policy = _normalized(dm_policy, DM_POLICIES, 'pair')
    if policy in ('disabled', 'ignore'):
        return Decision(IGNORE, 'dm_disabled')
    if policy == 'open':
        return Decision(ALLOW, 'dm_open')
    if _listed(event.user_id, known):
        return Decision(ALLOW, 'dm_listed')
    if policy == 'pair':
        return Decision(PAIR, 'dm_unpaired')
    return Decision(IGNORE, 'dm_not_listed')


def command_action(command: str) -> str:
    """把 `/new` 之类映射成动作名；未知命令返回空串（交给 Agent 当普通文本）。"""
    name = str(command or '').strip().lower()
    return {'help': 'help', 'new': 'new', 'stop': 'stop',
            'who': 'who', 'bind': 'bind', 'unbind': 'unbind'}.get(name, '')
