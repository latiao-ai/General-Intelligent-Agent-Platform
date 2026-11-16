"""群聊调度器：租约 + 栅栏 + 单步推进。

调度**不绑在 UI 上**：它跑在 Worker 的主循环里，关掉浏览器照样推进。每个房间
同一时刻只有一个调度者（`bot_rooms.lease_owner + lease_generation`），接管时代次
加一，旧副本的写入被条件更新挡掉。

一次 `step()` 只做一件事，顺序固定：

1. 领租约（拿不到就等下一拍，不抢）；
2. 收敛正在跑的成员回合：跑完 → 追加 `message.member`；失败 → `turn.failed`；
   还没跑完 → 本拍结束（单步不变量：一个房间同时最多一个成员在说话）；
3. 重放事件 → `planner.plan_next_task()`；
4. 有决定 → 落 task + 在该成员自己的房间会话里建一轮 turn；没决定 → 收束。

崩溃恢复：`running` task 的回合如果状态不明，`step()` 会把它标成
`indeterminate` 并追加 `turn.failed`，**不自动重跑**——宁可少说一句，也不要
同一个成员把同一段话说两遍。
"""
from __future__ import annotations

import logging
import uuid

from webpage_agent.bot import planner, prompts

logger = logging.getLogger(__name__)

# 房间租约：拿住 60 秒。一拍最多起一个成员回合，远比这短；留足余量给慢模型。
ROOM_LEASE_SECONDS = 60.0
# 一拍最多推进多少个房间。房间再多也不能饿死 Worker 的其他职责。
ROOM_TICK_LIMIT = 3


class RoomDriver:
    """群聊调度器。`worker_id` 用作租约持有者身份。"""

    def __init__(self, store, *, worker_id: str = ''):
        self.store = store
        self.worker_id = worker_id or f'room:{uuid.uuid4().hex[:6]}'

    async def tick(self, *, limit: int = ROOM_TICK_LIMIT) -> dict:
        """一整拍：遍历讨论中的房间，各推进一步。"""
        stats = {'rooms': 0, 'spoken': 0, 'settled': 0, 'waiting': 0,
                 'leased': 0, 'failed': 0}
        rooms = await self.store.open_room_rows(limit=max(1, int(limit)))
        for room in rooms:
            stats['rooms'] += 1
            try:
                result = await self.step(str(room.get('room_id') or ''))
            except Exception:                      # noqa: BLE001
                logger.exception('房间 %s 推进失败', room.get('room_id'))
                stats['failed'] += 1
                continue
            action = str(result.get('action') or '')
            if action == 'speak':
                stats['spoken'] += 1
            elif action == 'settle':
                stats['settled'] += 1
            elif action == 'waiting':
                stats['waiting'] += 1
            elif action == 'lease_held':
                stats['leased'] += 1
        return stats

    async def step(self, room_id: str) -> dict:
        """推进一个房间一步。返回 `{action, ...}`（可被接口直接回给前端）。"""
        if not room_id:
            return {'action': 'skip', 'reason': 'no_room'}
        room = await self.store.room_row(room_id)
        if room is None:
            return {'action': 'skip', 'reason': 'room_missing'}
        if str(room.get('status') or 'open') != 'open':
            return {'action': 'skip', 'reason': f'room_{room.get("status")}'}

        generation = await self.store.claim_room_lease(
            room_id, owner=self.worker_id, ttl=ROOM_LEASE_SECONDS)
        if generation is None:
            return {'action': 'lease_held', 'reason': 'another_driver'}

        # ① 收敛上一拍派出去的成员回合。它在跑就等，不叠第二个。
        pending = await self._settle_tasks(room_id)
        if pending is not None:
            return {'action': 'waiting', 'task_id': pending}

        # ② 重放日志 → 单步计划。
        events = await self.store.room_events(room_id)
        members = await self._members(room)
        plan = planner.plan_next_task(room, events, members)
        if plan.get('action') == 'settle':
            await self._settle_room(room, plan.get('reason') or 'settled')
            return {'action': 'settle', 'reason': plan.get('reason') or 'settled'}

        bot_id = str(plan.get('bot_id') or '')
        member = next((item for item in members
                       if str(item.get('bot_id') or '') == bot_id), {})
        if not member:
            await self._settle_room(room, 'no_members')
            return {'action': 'settle', 'reason': 'no_members'}

        epoch = int(plan.get('epoch') or 1)
        round_no = int(plan.get('round') or 0)
        position = int(plan.get('position') or 1)
        task_id = planner.task_id_for(room_id, epoch, round_no, position)
        task, created = await self.store.create_room_task(
            task_id=task_id, room_id=room_id, epoch=epoch, round_no=round_no,
            position=position, bot_id=bot_id)
        if not created:
            # 这一步已经派过（或正在跑）：不再重复建 turn。
            return {'action': 'duplicate', 'task_id': task_id,
                    'status': str(task.get('status') or '')}

        session = await self.store.ensure_bot_session(
            bot_id=bot_id, tenant_id=str(room.get('tenant_id') or 'default'),
            owner_user_id=str(room.get('owner_user_id') or 'default'),
            session_kind='bot_room',
            title=f'{prompts.ROOM_SESSION_TITLE_PREFIX}{room.get("name") or room_id}',
            room_id=room_id)
        session_id = str(session.get('session_id') or '')
        if not session_id:
            await self.store.settle_room_task(task_id, status='failed',
                                              reason='no_session')
            await self._append_failure(room_id, task_id, bot_id, epoch, round_no,
                                       position, member, 'no_session')
            return {'action': 'failed', 'reason': 'no_session'}

        transcript = prompts.room_transcript(
            events, handles={str(item.get('bot_id') or ''): str(item.get('handle') or '')
                             for item in members})
        text = prompts.room_turn_text(room, transcript, epoch=epoch,
                                      round_no=round_no, position=position)
        try:
            turn = await self._create_room_turn(
                session_id=session_id, tenant_id=str(room.get('tenant_id') or 'default'),
                owner_user_id=str(room.get('owner_user_id') or 'default'),
                task_id=task_id, message=text)
        except Exception as exc:                   # noqa: BLE001
            logger.warning('房间 %s 建回合失败：%s', room_id, type(exc).__name__)
            await self.store.settle_room_task(task_id, status='failed',
                                              reason='turn_create_failed')
            await self._append_failure(room_id, task_id, bot_id, epoch, round_no,
                                       position, member, 'turn_create_failed')
            return {'action': 'failed', 'reason': 'turn_create_failed'}

        await self.store.bind_room_task_turn(
            task_id, turn_id=str(turn.get('turn_id') or ''), session_id=session_id)
        await self.store.update_room_row(room_id, round_no=round_no)
        logger.info('房间 %s 派发 %s（%s，epoch=%s round=%s pos=%s）', room_id,
                    bot_id, plan.get('reason'), epoch, round_no, position)
        return {'action': 'speak', 'task_id': task_id, 'bot_id': bot_id,
                'bot_name': member.get('name') or bot_id,
                'handle': member.get('handle') or '',
                'epoch': epoch, 'round': round_no, 'position': position,
                'reason': plan.get('reason') or '',
                'turn_id': str(turn.get('turn_id') or ''),
                'session_id': session_id}

    # ---------- 内部 ----------

    async def _members(self, room: dict) -> list[dict]:
        """房间成员的有效身份行（按 `members_json` 的顺序，过滤失效的）。"""
        wanted = [str(item) for item in (room.get('members') or [])]
        rows = await self.store.list_bot_rows(
            tenant_id=str(room.get('tenant_id') or 'default'),
            owner_user_id=str(room.get('owner_user_id') or 'default'))
        by_id = {str(row.get('bot_id') or ''): row for row in rows
                 if str(row.get('status') or 'active') == 'active'}
        return [by_id[bot_id] for bot_id in wanted if bot_id in by_id]

    async def _settle_tasks(self, room_id: str) -> str | None:
        """把已完成的成员回合发布回房间。返回还在跑 task 的 id（有就本拍结束）。"""
        for task in await self.store.open_room_tasks(room_id):
            task_id = str(task.get('task_id') or '')
            turn_id = str(task.get('turn_id') or '')
            if not turn_id:
                # 建 turn 之前就崩了：标 indeterminate，不再自动重来。
                await self.store.settle_room_task(task_id, status='indeterminate',
                                                  reason='turn_missing')
                await self._append_failure(
                    room_id, task_id, str(task.get('bot_id') or ''),
                    int(task.get('epoch') or 1), int(task.get('round_no') or 0),
                    int(task.get('position') or 1),
                    await self.store.bot_row(str(task.get('bot_id') or '')) or {},
                    'turn_missing')
                return None
            turn = await self.store.turn(turn_id)
            if turn is None:
                await self.store.settle_room_task(task_id, status='indeterminate',
                                                  reason='turn_missing')
                return None
            status = str(turn.get('status') or '')
            if status not in ('succeeded', 'failed', 'cancelled'):
                return task_id
            if status == 'succeeded':
                await self._publish(room_id, task, turn)
                await self.store.settle_room_task(task_id, status='settled')
            else:
                member = await self.store.bot_row(str(task.get('bot_id') or '')) or {}
                await self._append_failure(
                    room_id, task_id, str(task.get('bot_id') or ''),
                    int(task.get('epoch') or 1), int(task.get('round_no') or 0),
                    int(task.get('position') or 1), member,
                    str(turn.get('error') or status))
                await self.store.settle_room_task(task_id, status='failed',
                                                  reason=status)
        return None

    async def _publish(self, room_id: str, task: dict, turn: dict) -> None:
        """成员回合成功 → 追加一条 `message.member`（迟到的结果也要能落地）。"""
        bot_id = str(task.get('bot_id') or '')
        member = await self.store.bot_row(bot_id) or {}
        publication = prompts.room_publication(str(turn.get('answer') or ''))
        epoch = int(task.get('epoch') or 1)
        round_no = int(task.get('round_no') or 0)
        position = int(task.get('position') or 1)
        await self.store.append_room_event(
            room_id,
            event_id=planner.task_event_id(room_id, epoch, round_no, position),
            kind='message.member',
            actor=f'bot:{bot_id}',
            payload={'bot_id': bot_id, 'handle': member.get('handle') or '',
                     'name': member.get('name') or bot_id,
                     'text': publication['text'], 'silent': publication['silent'],
                     'mentions': prompts.parse_mentions(publication['text']),
                     'epoch': epoch, 'round': round_no, 'position': position,
                     'task_id': str(task.get('task_id') or ''),
                     'turn_id': str(task.get('turn_id') or '')})

    async def _append_failure(self, room_id: str, task_id: str, bot_id: str,
                              epoch: int, round_no: int, position: int,
                              member: dict, reason: str) -> None:
        await self.store.append_room_event(
            room_id,
            event_id=f'{task_id}:failed',
            kind='turn.failed',
            actor=f'bot:{bot_id}' if bot_id else 'system',
            payload={'bot_id': bot_id, 'handle': (member or {}).get('handle') or '',
                     'task_id': task_id, 'reason': str(reason or '')[:300],
                     'epoch': int(epoch), 'round': int(round_no),
                     'position': int(position), 'silent': True})

    async def _settle_room(self, room: dict, reason: str) -> None:
        room_id = str(room.get('room_id') or '')
        await self.store.append_room_event(
            room_id, event_id=f'{room_id}:settled:{reason}',
            kind='room.settled', actor='system',
            payload={'reason': str(reason or '')})
        await self.store.update_room_row(room_id, status='settled')
        logger.info('房间 %s 收束：%s', room_id, reason)

    async def _create_room_turn(self, *, session_id: str, tenant_id: str,
                                owner_user_id: str, task_id: str,
                                message: str) -> dict:
        from webpage_agent.api.hub_messages import request_hash
        import hashlib

        turn_id = 'rm' + hashlib.sha256(task_id.encode('utf-8')).hexdigest()[:10]
        return await self.store.create_turn(
            turn_id, session_id, request_id=f'room:{task_id}',
            request_hash=request_hash(message, ''), message=message,
            kind='chat', tenant_id=tenant_id, owner_user_id=owner_user_id,
            owner_role='member')
