"""群聊的单步规划：重放事件日志 → **至多一个**决定。

这是 Bot Mode 里唯一需要"想清楚"的纯逻辑，也是最该被单测钉住的一块：

- 输入是 `bot_room_events` 的**全部**事件 + 房间配置 + 成员表；
- 输出是 `speak(bot, epoch, round, position)` 或 `settle(reason)`；
- 没有内部状态：调度器崩溃后不用恢复"对话进行到哪了"，重放日志就能重新决策。

有界自治（缺一个都会炸）：

| 上限 | 默认 | 作用 |
| --- | --- | --- |
| 轮次 | 3 | 用户每说一次，机器人最多来回三轮 |
| 消息数 | 10 | 房间总发言数上限（全局，含用户消息） |
| 静默 | — | 全员"无补充"→ 立即收束 |
| 提及优先 | — | `@handle` 只让被点名的人先说，不广播 |
| 失败跳过 | — | 某个成员回合失败，视为已发言，不再重试（避免死循环） |

## `epoch` 是什么

`epoch` = 房间里**用户已经说了几次**。用户每说一次就开一个新的讨论纪元：
轮次从 0 重新数，但 `max_messages` 仍然按整个房间算（防止用户无限提问把房间
变成 token 黑洞）。这样"同一房间多轮追问"不需要任何额外的状态列——重放日志
数一下 user 事件就够了。

`position` 在一个纪元的一轮内从 1 递增。`(epoch, round, position)` 一起构成稳定
task id（`room:e{epoch}:r{round}:p{pos}`），同一步重复规划不会跑两遍。
"""
from __future__ import annotations

MESSAGE_KINDS = ('message.user', 'message.member')
SPOKEN_KINDS = ('message.member', 'turn.failed')


def _message_events(events: list[dict]) -> list[dict]:
    return [item for item in (events or [])
            if isinstance(item, dict) and item.get('kind') in MESSAGE_KINDS]


def _int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def epoch_of(events: list[dict]) -> int:
    """当前讨论纪元 = 用户已经说过几次（至少 1，房间总得有个议题）。"""
    count = sum(1 for item in (events or [])
                if isinstance(item, dict) and item.get('kind') == 'message.user')
    return max(1, count)


def round_of(event: dict) -> int:
    return _int((event.get('payload') or {}).get('round'), 0)


def event_epoch(event: dict) -> int:
    return _int((event.get('payload') or {}).get('epoch'), 1)


def _spoken(events: list[dict], epoch: int, round_no: int) -> dict[str, dict]:
    """本纪元本轮里已经发过言（或失败过）的成员：`{bot_id: event}`。"""
    out: dict[str, dict] = {}
    for event in events or []:
        if not isinstance(event, dict) or event.get('kind') not in SPOKEN_KINDS:
            continue
        if event_epoch(event) != epoch or round_of(event) != round_no:
            continue
        bot_id = str((event.get('payload') or {}).get('bot_id') or '')
        if bot_id:
            out[bot_id] = event
    return out


def _mentions(event: dict, known: set[str]) -> list[str]:
    """事件里 `@` 到的、确实是房间成员的 handle（保序去重）。"""
    payload = event.get('payload') or {}
    raw = payload.get('mentions') or []
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            handle = str(item or '').strip().lower()
            if handle and handle in known and handle not in out:
                out.append(handle)
    return out


def _speak(bot: dict, epoch: int, round_no: int, position: int, reason: str) -> dict:
    return {'action': 'speak', 'bot_id': str(bot.get('bot_id') or ''),
            'handle': str(bot.get('handle') or ''),
            'name': str(bot.get('name') or ''),
            'epoch': int(epoch), 'round': int(round_no),
            'position': int(position), 'reason': reason}


def plan_next_task(room: dict, events: list[dict], members: list[dict]) -> dict:
    """算出下一步：谁发言，或者为什么收束。

    `members` 必须是**当前有效成员**（已按 active / 未隐藏过滤），顺序就是轮转顺序。
    返回值不写库，调度器负责落 task + 建 turn。
    """
    status = str((room or {}).get('status') or 'open')
    if status != 'open':
        return {'action': 'settle', 'reason': f'room_{status}'}
    active = [item for item in (members or []) if isinstance(item, dict)
              and str(item.get('bot_id') or '')]
    if not active:
        return {'action': 'settle', 'reason': 'no_members'}
    by_handle = {str(item.get('handle') or '').lower(): item for item in active
                 if str(item.get('handle') or '')}
    by_id = {str(item.get('bot_id') or ''): item for item in active}

    max_rounds = max(1, int((room or {}).get('max_rounds') or 3))
    max_messages = max(1, int((room or {}).get('max_messages') or 10))

    messages = _message_events(events)
    if len(messages) >= max_messages:
        return {'action': 'settle', 'reason': 'message_limit'}

    epoch = epoch_of(events)
    in_epoch = [item for item in messages if event_epoch(item) == epoch]
    member_in_epoch = [item for item in in_epoch if item.get('kind') == 'message.member']
    round_no = max((round_of(item) for item in in_epoch), default=0)
    spoken = _spoken(events, epoch, round_no)

    # 本纪元还没有任何成员发过言：轮到第一位成员开场（用户的问题就是议题）。
    # 开场同样要尊重 `@`，也要**跳过已经失败过的成员**：某个成员第一句话就挂了，
    # 不能每一拍都再点它一次（那会变成一个永远不动的死循环）。
    if not member_in_epoch:
        opening = _mentions(messages[-1], set(by_handle)) if messages else []
        for handle in opening or []:
            bot = by_handle[handle]
            if str(bot.get('bot_id') or '') not in spoken:
                return _speak(bot, epoch, 0, len(spoken) + 1, 'mentioned')
        for bot in active:
            if str(bot.get('bot_id') or '') not in spoken:
                return _speak(bot, epoch, 0, len(spoken) + 1, 'opening')
        return {'action': 'settle', 'reason': 'all_failed'}

    last = messages[-1]
    # ① 提及优先：最后一条消息 @ 了谁，谁先说（本轮说过就跳过）。
    pending = [handle for handle in _mentions(last, set(by_handle))
               if str(by_handle[handle].get('bot_id') or '') not in spoken]
    if pending:
        return _speak(by_handle[pending[0]], epoch, round_no, len(spoken) + 1,
                      'mentioned')

    # ② 轮转：从最后一位发言者之后开始，找这一轮还没说话的人。
    if len(spoken) < len(active):
        order = [str(item.get('bot_id') or '') for item in active]
        last_speaker = ''
        for event in reversed(messages):
            if (event.get('kind') in SPOKEN_KINDS and event_epoch(event) == epoch
                    and round_of(event) == round_no):
                last_speaker = str((event.get('payload') or {}).get('bot_id') or '')
                if last_speaker:
                    break
        start = (order.index(last_speaker) + 1) % len(order) if last_speaker in order else 0
        for offset in range(len(order)):
            bot_id = order[(start + offset) % len(order)]
            if bot_id and bot_id not in spoken:
                return _speak(by_id[bot_id], epoch, round_no, len(spoken) + 1,
                              'rotation')

    # ③ 这一轮全员说完：全静默 → 收束；否则开下一轮（轮次到顶就收束）。
    spoke_events = [event for event in events or []
                    if isinstance(event, dict)
                    and event.get('kind') in SPOKEN_KINDS
                    and event_epoch(event) == epoch
                    and round_of(event) == round_no]
    if spoke_events and all(bool((event.get('payload') or {}).get('silent'))
                            for event in spoke_events):
        return {'action': 'settle', 'reason': 'silent_round'}
    if round_no + 1 >= max_rounds:
        return {'action': 'settle', 'reason': 'round_limit'}
    return _speak(active[0], epoch, round_no + 1, 1, 'next_round')


def task_id_for(room_id: str, epoch: int, round_no: int, position: int) -> str:
    """稳定 task id：同一步重复规划不会跑两遍。"""
    return f'{room_id}:e{int(epoch)}:r{int(round_no)}:p{int(position)}'


def task_event_id(room_id: str, epoch: int, round_no: int, position: int) -> str:
    """成员发言事件的稳定 id（与 task id 同构，天然幂等）。"""
    return f'{room_id}:msg:e{int(epoch)}:r{int(round_no)}:p{int(position)}'
