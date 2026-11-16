"""Worker 的机器人节拍：泵投递 + 推进群聊。

挂在 `scripts/run_worker.py` 的主循环里，和定时任务 tick 同层。为什么放在 Worker
而不是 API：**调度不能绑在 UI 上**——用户关掉浏览器之后，队友之间的消息、房间
里的下一轮发言仍然要发生（docs/bot-mode.md §执行层）。

一拍只做两件事，各自有界：

1. `BotMailbox.pump()`：把排队的投递变成目标机器人的 turn，收敛已完成的收据；
2. `RoomDriver.tick()`：给每个讨论中的房间推进一步（最多一个成员发言）。

任何一步抛异常都只影响这一步：投递留在 `queued`（收据还在），房间下一拍重试。
"""
from __future__ import annotations

import logging

from webpage_agent.core.config import settings

logger = logging.getLogger(__name__)


async def tick(store, worker_id: str) -> dict:
    """跑一拍。返回统计，供 Worker 日志与测试断言。"""
    stats = {'delivered': 0, 'settled': 0, 'replied': 0, 'spoken': 0,
             'rooms_settled': 0, 'errors': 0}
    if not settings.bot_mode_enabled:
        return stats

    from webpage_agent.bot.mailbox import BotMailbox
    try:
        mail = await BotMailbox(store, max_hops=settings.bot_max_hops).pump(
            limit=settings.bot_delivery_batch)
        stats['delivered'] += int(mail.get('claimed') or 0)
        stats['settled'] += (int(mail.get('settled') or 0)
                             + int(mail.get('failed') or 0))
        stats['replied'] += int(mail.get('replied') or 0)
    except Exception:                              # noqa: BLE001
        logger.exception('机器人投递泵失败')
        stats['errors'] += 1

    from webpage_agent.bot.rooms import RoomDriver
    try:
        rooms = await RoomDriver(store, worker_id=worker_id).tick(
            limit=settings.bot_room_batch)
        stats['spoken'] += int(rooms.get('spoken') or 0)
        stats['rooms_settled'] += int(rooms.get('settled') or 0)
    except Exception:                              # noqa: BLE001
        logger.exception('群聊调度失败')
        stats['errors'] += 1
    return stats
