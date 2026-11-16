"""例行任务（Routines）：定时任务的结果投递回归属机器人自己的会话。

和 Hermes 的 `deliver=bot-chat` 是同一个语义：结果不是发一条 webhook 就完了，
而是**变成这个机器人的一轮新输入**——它读到自己的日报/巡检结果，决定要不要
跟进、要不要提醒用户，顺便形成一份"工作日志"。

幂等：`request_id = cron:<occurrence_id>`。调度器的 `collect()` 可能被多个
Worker 同时调用（`open_occurrences` 不排他），同一 occurrence 只会有一行 turn。

失败**不投回合**，只落一条消息：一次失败的定时任务没必要再花一次模型调用，
但它必须在机器人的会话里留痕，否则用户只会觉得"这个机器人什么也没干"。
"""
from __future__ import annotations

import logging

from webpage_agent.bot import prompts

logger = logging.getLogger(__name__)


async def deliver_routine(store, *, schedule: dict, occurrence: dict,
                          summary: str, status: str) -> dict | None:
    """把一次 occurrence 的结果交给归属机器人。没有 `bot_id` 时什么都不做。"""
    bot_id = str((schedule or {}).get('bot_id') or '')
    if not bot_id:
        return None
    occurrence_id = str((occurrence or {}).get('occurrence_id') or '')
    if not occurrence_id:
        return None
    tenant = str(schedule.get('tenant_id') or 'default')
    owner = str(schedule.get('owner_user_id') or 'default')
    bot = await store.bot_row(bot_id, tenant_id=tenant, owner_user_id=owner)
    if bot is None or str(bot.get('status') or '') != 'active':
        logger.warning('例行任务 %s 的机器人 %s 不可用，跳过投递',
                       schedule.get('schedule_id'), bot_id)
        return None
    session = await store.ensure_bot_session(
        bot_id=bot_id, tenant_id=tenant, owner_user_id=owner,
        session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
    session_id = str(session.get('session_id') or '')
    if not session_id:
        return None

    name = str(schedule.get('name') or '')
    text = str(summary or '').strip()
    if status == 'succeeded':
        message = prompts.routine_turn_text(name, text)
        turn = await _create_turn(
            store, session_id=session_id, tenant=tenant, owner=owner,
            request_id=f'cron:{occurrence_id}', message=message)
        logger.info('例行任务 %s 已投递到机器人 %s 的会话（turn=%s）',
                    schedule.get('schedule_id'), bot_id, turn.get('turn_id'))
        return {'session_id': session_id, 'turn_id': turn.get('turn_id'),
                'kind': 'turn'}

    # 失败/需要人工：只落消息，不起回合。
    from datetime import datetime
    when = ''
    due = occurrence.get('due_at')
    try:
        if due:
            when = datetime.fromtimestamp(float(due)).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OSError):
        when = ''
    head = prompts.routine_mirror_prefix(name, when)
    body = (f'{head} 执行未成功（{status}）。\n'
            f'{text or "（没有更多说明）"}')
    await store.append_message(
        session_id, 'user', body, tenant_id=tenant, owner_user_id=owner,
        dedupe_key=f'cron:{occurrence_id}')
    # 把这一条的时间戳推到最前，机器人面板的预览才是最新的。
    await store.touch_session(session_id)
    logger.info('例行任务 %s 失败，已在机器人 %s 的会话里留痕',
                schedule.get('schedule_id'), bot_id)
    return {'session_id': session_id, 'kind': 'message'}


async def _create_turn(store, *, session_id: str, tenant: str, owner: str,
                       request_id: str, message: str) -> dict:
    import hashlib

    from webpage_agent.api.hub_messages import request_hash

    turn_id = 'cr' + hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:10]
    return await store.create_turn(
        turn_id, session_id, request_id=request_id,
        request_hash=request_hash(message, ''), message=message,
        kind='chat', tenant_id=tenant, owner_user_id=owner, owner_role='member')
