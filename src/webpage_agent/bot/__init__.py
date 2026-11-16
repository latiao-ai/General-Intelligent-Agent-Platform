"""Bot Mode：机器人身份、提示词、投递与群聊调度。

设计说明见 `docs/bot-mode.md`。这个包分成四块，边界刻意做窄：

| 模块 | 职责 | 不能做的事 |
| --- | --- | --- |
| `prompts.py` | 人设/花名册/规则/窗口渲染 | 不读库、不发请求（纯函数） |
| `planner.py` | 群聊单步规划 | 不写库（重放日志 → 最多一个决定） |
| `mailbox.py` | 投递收据与 Worker 侧派发 | 不碰模型、不碰 HTTP |
| `rooms.py` | 房间租约与推进 | 不拼提示词（交给 prompts） |

`service.py` 是唯一的集成点：会话行 → 人设/模型/技能/家目录。聊天 turn 与
业务 run 都经 `addons.resolver` 走到它，所以两边行为一致。
"""
from webpage_agent.bot.mailbox import BotMailbox
from webpage_agent.bot.planner import plan_next_task
from webpage_agent.bot.prompts import (
    BOT_CHAT_TITLE, SILENCE_TOKEN, capability_fingerprint, normalize_handle,
    parse_mentions, render_roster)
from webpage_agent.bot.rooms import RoomDriver

__all__ = [
    'BOT_CHAT_TITLE', 'SILENCE_TOKEN', 'BotMailbox', 'RoomDriver',
    'capability_fingerprint', 'normalize_handle', 'parse_mentions',
    'plan_next_task', 'render_roster',
]
