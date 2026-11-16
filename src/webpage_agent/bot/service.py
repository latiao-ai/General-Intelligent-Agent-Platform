"""机器人的解析层：会话行 → 这一轮真正生效的人设、模型、技能、记忆与家目录。

这是 Bot Mode 的**唯一集成点**：聊天 turn 与业务 run 都经过
`webpage_agent.addons.resolver.resolve()`，所以机器人能力只在这里接一次，
不会出现"聊天里是机器人、定时任务里是通用助手"这种两套行为。

三件事在这里定死：

1. **人设按会话注入**：只有会话行带 `bot_id` 才有机器人提示；gate 是
   `session["bot_id"] != ''` 这个稳定纯函数，普通任务一个字都不会变。
2. **每轮重新解析，不缓存**：人设/模型/技能随时可改，下一轮必须生效。
   `sessions.capability_epoch` 只用来"感知变化"，不是缓存的开关。
3. **家目录按机器人隔离**：`bots/<bot_id>/` 在用户私有目录内，技能物化、文件
   产出、脚本工作目录都落在那里——同一用户的两个机器人不会读到对方的技能目录。
"""
from __future__ import annotations

import logging
from pathlib import Path

from webpage_agent.bot import prompts
from webpage_agent.core import sandbox

logger = logging.getLogger(__name__)

# 机器人提示块在系统提示里的位置：放在最前面。人设是最稳的前缀，
# 让后面易变的会话记忆、工具清单尽量少影响它。
BOT_PROMPT_HEADER = '\n\n'


def bot_home(tenant_id: str, owner_user_id: str, bot_id: str) -> Path:
    """机器人自己的目录：`<用户私有根>/bots/<bot_id>`。

    放在用户私有根**里面**而不是旁边：文件工具的越界检查按私有根算，
    放到外面会让机器人的 read_file 一调就被沙箱拒绝。
    """
    root = sandbox.provision(tenant_id, owner_user_id)
    return root / 'bots' / sandbox.segment(bot_id)


def ensure_bot_home(tenant_id: str, owner_user_id: str, bot_id: str) -> str:
    folder = bot_home(tenant_id, owner_user_id, bot_id)
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise sandbox.SandboxError('机器人目录不能是符号链接')
    return str(folder)


async def _roster(store, tenant_id: str, owner_user_id: str) -> list[dict]:
    try:
        return await store.list_bot_rows(tenant_id=tenant_id,
                                          owner_user_id=owner_user_id,
                                          include_hidden=False)
    except Exception:                              # noqa: BLE001
        logger.warning('读取机器人花名册失败', exc_info=True)
        return []


async def resolve_session_bot(store, session_row: dict | None, *,
                              tenant_id: str = '', owner_user_id: str = '',
                              materialize: bool = True) -> dict:
    """解析会话对应的机器人上下文。没有 `bot_id` 时返回 `{}`。

    返回字段（调用方按需取用）：

    - `bot`：身份行；`epoch`：能力指纹；`prompt_block`：人设 + 协作规则；
    - `model` / `skill_ids` / `connector_ids`：该机器人的默认值；
    - `room`：房间行（只在房间会话里有）；
    - `room_members`：房间成员的展示信息（渲染规则用）。
    """
    row = session_row or {}
    bot_id = str(row.get('bot_id') or '')
    if not bot_id:
        return {}
    tenant = tenant_id or str(row.get('tenant_id') or 'default')
    owner = owner_user_id or str(row.get('owner_user_id') or 'default')
    bot = await store.bot_row(bot_id, tenant_id=tenant, owner_user_id=owner)
    if bot is None:
        # 身份行被删了（或跨主体）：不猜、不回退成"通用助手"，直接不注入人设。
        logger.warning('会话 %s 引用的机器人 %s 不存在', row.get('session_id'), bot_id)
        return {'missing_bot_id': bot_id}
    roster = await _roster(store, tenant, owner)
    epoch = prompts.capability_fingerprint(bot, roster)
    session_id = str(row.get('session_id') or '')
    stored_epoch = str(row.get('capability_epoch') or '')
    if session_id and materialize:
        # 只读预览（GET 会话选择）不写库、不建目录：一次查询不该产生副作用。
        try:
            await store.set_session_capability_epoch(session_id, epoch)
        except Exception:                          # noqa: BLE001
            logger.debug('写能力纪元失败 session=%s', session_id, exc_info=True)

    blocks = [prompts.persona_block(bot, roster=roster, epoch=epoch)]
    notice = prompts.capability_notice(stored_epoch, epoch)
    if notice:
        blocks.append(notice)

    room = None
    room_members: list[dict] = []
    if str(row.get('session_kind') or '') == 'bot_room':
        room = await store.room_row(str(row.get('room_id') or ''))
        if room:
            wanted = [str(item) for item in (room.get('members') or [])]
            by_id = {str(item.get('bot_id') or ''): item for item in roster}
            room_members = [by_id[item] for item in wanted if item in by_id]
            blocks.append(prompts.room_rules(room, room_members))

    return {
        'bot': bot,
        'bot_id': bot_id,
        'epoch': epoch,
        'prompt_block': BOT_PROMPT_HEADER + '\n\n'.join(blocks),
        'model': str(bot.get('model') or ''),
        'skill_ids': [str(item) for item in (bot.get('skill_ids') or [])],
        'connector_ids': [str(item) for item in (bot.get('connector_ids') or [])],
        'room': room,
        'room_members': room_members,
        'home': (ensure_bot_home(tenant, owner, bot_id) if materialize
                 else str(bot_home(tenant, owner, bot_id))),
    }


