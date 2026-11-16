"""机器人（Bot Mode）的仓储方法：身份、规范会话、投递收据、房间与租约。

对应 `docs/bot-mode.md`。这个模块只做"行怎么读写"，**不做**任何提示词拼装、
不提政策判断、不发网络请求——那些在 `webpage_agent.bot` 与 `api/bots_api.py`。

四条不变量（都靠数据库，不靠进程内状态）：

1. **一个机器人一个规范会话**：`uq_sessions_bot_chat` 唯一索引。没有存 session-id
   指针，`ensure_bot_session()` 先 `INSERT ... ON CONFLICT DO NOTHING` 再回读，
   所以并发/重建/换客户端都收敛到同一行。
2. **投递幂等**：`delivery_id` 由发送方按"哪个回合、发给谁、说了什么"算出来，
   重试复用同一个 id；`INSERT ... ON CONFLICT DO NOTHING` 让重复投递变成一次。
3. **同一会话串行**：由 `session_turns` 的领取协议保证（见 `store/pg_auth.py`），
   这里只负责把投递变成一行 turn。
4. **房间只有一个调度者**：`bot_rooms.lease_owner + lease_generation` 栅栏，
   过期可被接管，接管时代次加一，旧副本的写入被条件更新挡掉。
"""
from __future__ import annotations

import json
import time

from webpage_agent.store.pg_store import now

# 机器人行里用户可改的字段。bot_id / tenant_id / owner_user_id / created_at 不在
# 白名单里：让接口改归属等于越权搬家。
BOT_FIELDS = ('handle', 'name', 'title', 'description', 'soul', 'model',
              'skill_ids', 'connector_ids', 'section_id', 'icon', 'color',
              'shape', 'image', 'pinned', 'hidden', 'status', 'ui')
JSON_COLUMNS = {'skill_ids': 'skill_ids_json', 'connector_ids': 'connector_ids_json',
                'ui': 'ui_json'}
# 房间事件里允许的 kind。未知 kind 一律拒绝：事件日志是重放决策的唯一输入，
# 允许乱写等于允许伪造历史。
ROOM_EVENT_KINDS = frozenset({
    'room.opened', 'message.user', 'message.member', 'turn.failed',
    'room.settled', 'room.closed', 'room.notice',
})


def _json(value, default):
    """读 JSON 文本列。坏数据不炸整张表：降级成默认值。"""
    if value is None or value == '':
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def decode_bot(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out['skill_ids'] = _json(out.pop('skill_ids_json', None), [])
    out['connector_ids'] = _json(out.pop('connector_ids_json', None), [])
    out['ui'] = _json(out.pop('ui_json', None), {})
    out['pinned'] = bool(out.get('pinned'))
    out['hidden'] = bool(out.get('hidden'))
    return out


def decode_room(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out['members'] = [str(x) for x in _json(out.pop('members_json', None), [])]
    return out


def decode_event(row: dict) -> dict:
    out = dict(row)
    out['payload'] = _json(out.pop('payload_json', None), {})
    return out


class PgBots:
    """机器人 / 分区 / 投递 / 房间的 PostgreSQL 实现。"""

    # ---------- 机器人身份 ----------

    async def list_bot_rows(self, *, tenant_id: str, owner_user_id: str,
                            include_archived: bool = False,
                            include_hidden: bool = True) -> list[dict]:
        """花名册：机器人行 + 规范会话 + 最近一条消息预览。

        一次查询解决，不做 N+1：桌面端每次刷新花名册都要这四样东西。
        `last_preview` 取规范会话里最新一条消息，列表行直接可显示。
        """
        clauses = ['b.tenant_id=%s', 'b.owner_user_id=%s']
        params: list = [tenant_id, owner_user_id]
        if not include_archived:
            clauses.append("b.status='active'")
        if not include_hidden:
            clauses.append('b.hidden=0')
        sql = f"""
            SELECT b.*, s.session_id AS chat_session_id,
                   s.updated_at AS chat_updated_at,
                   (SELECT m.content FROM messages m
                     WHERE m.session_id = s.session_id ORDER BY m.id DESC LIMIT 1)
                     AS last_preview,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.session_id = s.session_id) AS message_count
              FROM bots b
              LEFT JOIN sessions s
                ON s.bot_id = b.bot_id AND s.session_kind = 'bot_chat'
             WHERE {' AND '.join(clauses)}
             ORDER BY b.pinned DESC, b.created_at ASC
        """
        async with self._lease() as conn:
            rows = await self._all(conn, sql, tuple(params))
        out = []
        for row in rows:
            item = decode_bot(row) or {}
            preview = str(item.get('last_preview') or '').strip()
            item['last_preview'] = (preview[:80] + '…') if len(preview) > 80 else preview
            item['message_count'] = int(item.get('message_count') or 0)
            out.append(item)
        return out

    async def bot_row(self, bot_id: str, *, tenant_id: str | None = None,
                      owner_user_id: str | None = None) -> dict | None:
        """按 id 取机器人；给了主体就一并校验归属（不存在与越权都返回 None）。"""
        clauses = ['bot_id=%s']
        params: list = [bot_id]
        if tenant_id is not None:
            clauses.append('tenant_id=%s')
            params.append(tenant_id)
        if owner_user_id is not None:
            clauses.append('owner_user_id=%s')
            params.append(owner_user_id)
        async with self._lease() as conn:
            row = await self._one(
                conn, f'SELECT * FROM bots WHERE {" AND ".join(clauses)}',
                tuple(params))
        return decode_bot(row)

    async def bot_row_by_handle(self, handle: str, *, tenant_id: str,
                                owner_user_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT * FROM bots WHERE owner_user_id=%s AND tenant_id=%s '
                      'AND lower(handle)=lower(%s) AND status=%s',
                (owner_user_id, tenant_id, handle, 'active'))
        return decode_bot(row)

    async def create_bot_row(self, *, bot_id: str, tenant_id: str,
                             owner_user_id: str, created_at: str,
                             updated_at: str, **fields) -> dict:
        columns = ['bot_id', 'tenant_id', 'owner_user_id', 'created_at', 'updated_at']
        values: list = [bot_id, tenant_id, owner_user_id, created_at, updated_at]
        for key in BOT_FIELDS:
            column = JSON_COLUMNS.get(key, key)
            if key in ('skill_ids', 'connector_ids'):
                values.append(json.dumps(list(fields.get(key) or ()), ensure_ascii=False))
            elif key == 'ui':
                values.append(json.dumps(dict(fields.get(key) or {}), ensure_ascii=False))
            elif key in ('pinned', 'hidden'):
                values.append(1 if fields.get(key) else 0)
            else:
                values.append(str(fields.get(key) or ''))
            columns.append(column)
        placeholders = ', '.join(['%s'] * len(columns))
        async with self._lease() as conn:
            row = await self._one(
                conn, f'INSERT INTO bots ({", ".join(columns)}) '
                      f'VALUES ({placeholders}) RETURNING *', tuple(values))
            await conn.commit()
        return decode_bot(row) or {}

    async def update_bot_row(self, bot_id: str, *, tenant_id: str,
                             owner_user_id: str, **fields) -> dict | None:
        """按键更新（只更新出现过的字段），返回更新后的行或 None。"""
        sets: list[str] = []
        values: list = []
        for key in BOT_FIELDS:
            if key not in fields:
                continue
            column = JSON_COLUMNS.get(key, key)
            value = fields[key]
            if key in ('skill_ids', 'connector_ids'):
                values.append(json.dumps(list(value or ()), ensure_ascii=False))
            elif key == 'ui':
                values.append(json.dumps(dict(value or {}), ensure_ascii=False))
            elif key in ('pinned', 'hidden'):
                values.append(1 if value else 0)
            else:
                values.append(str(value or ''))
            sets.append(f'{column}=%s')
        if not sets:
            return await self.bot_row(bot_id, tenant_id=tenant_id,
                                      owner_user_id=owner_user_id)
        sets.append('updated_at=%s')
        values.extend([now(), bot_id, tenant_id, owner_user_id])
        async with self._lease() as conn:
            row = await self._one(
                conn, f'UPDATE bots SET {", ".join(sets)} '
                      'WHERE bot_id=%s AND tenant_id=%s AND owner_user_id=%s '
                      'RETURNING *', tuple(values))
            await conn.commit()
        return decode_bot(row)

    async def delete_bot_row(self, bot_id: str, *, tenant_id: str,
                             owner_user_id: str) -> bool:
        """硬删身份行；会话与消息按外键无关地保留为档案（不级联删历史）。

        归档用 `update_bot_row(status='archived')`——那才是产品语义。
        这里只给"建错了、立刻删掉"用。
        """
        async with self._lease() as conn:
            cur = await self._run(
                conn, 'DELETE FROM bots WHERE bot_id=%s AND tenant_id=%s '
                      'AND owner_user_id=%s', (bot_id, tenant_id, owner_user_id))
            await conn.commit()
            return bool(getattr(cur, 'rowcount', 0))

    # ---------- 分区 ----------

    async def list_bot_sections(self, *, tenant_id: str,
                                owner_user_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT * FROM bot_sections WHERE tenant_id=%s '
                      'AND owner_user_id=%s ORDER BY position ASC, created_at ASC',
                (tenant_id, owner_user_id))
        return [dict(row) for row in rows]

    async def upsert_bot_section(self, *, section_id: str, tenant_id: str,
                                 owner_user_id: str, name: str,
                                 position: int) -> dict:
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(
                conn, 'INSERT INTO bot_sections '
                      '(section_id, tenant_id, owner_user_id, name, position, '
                      ' created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s) '
                      'ON CONFLICT (section_id) DO UPDATE '
                      'SET name=EXCLUDED.name, position=EXCLUDED.position, '
                      '    updated_at=EXCLUDED.updated_at '
                      'WHERE bot_sections.tenant_id=EXCLUDED.tenant_id '
                      'AND bot_sections.owner_user_id=EXCLUDED.owner_user_id '
                      'RETURNING *',
                (section_id, tenant_id, owner_user_id, name, int(position),
                 stamp, stamp))
            await conn.commit()
        return dict(row) if row else {}

    async def delete_bot_section(self, section_id: str, *, tenant_id: str,
                                 owner_user_id: str) -> bool:
        """删分区只删分区行；成员归属一起清空（否则它们会挂在一个不存在的分区上）。"""
        async with self._lease() as conn:
            cur = await self._run(
                conn, 'DELETE FROM bot_sections WHERE section_id=%s AND tenant_id=%s '
                      'AND owner_user_id=%s', (section_id, tenant_id, owner_user_id))
            deleted = bool(getattr(cur, 'rowcount', 0))
            if deleted:
                await self._run(
                    conn, 'UPDATE bots SET section_id=%s, updated_at=%s '
                          'WHERE section_id=%s AND tenant_id=%s AND owner_user_id=%s',
                    ('', now(), section_id, tenant_id, owner_user_id))
            await conn.commit()
        return deleted

    # ---------- 会话：规范会话与房间会话 ----------

    async def ensure_bot_session(self, *, bot_id: str, tenant_id: str,
                                 owner_user_id: str, session_kind: str,
                                 title: str, room_id: str = '') -> dict:
        """取或建机器人会话。**幂等**：唯一索引 + ON CONFLICT DO NOTHING。

        这就是"标题即身份"的等价物：调用方只给 (bot, kind, room) 这三个
        可重建的键，不保存 session-id 指针。会话被归档/删库后，下一次点开
        机器人会自然建出新的一行，不需要迁移任何引用。
        """
        if session_kind not in ('bot_chat', 'bot_room'):
            raise ValueError(f'不支持的机器人会话类型 {session_kind}')
        session_id = f'bot_{bot_id}_{session_kind}' if session_kind == 'bot_chat' \
            else f'bot_{bot_id}_room_{room_id}'
        stamp = now()
        async with self._lease() as conn:
            await self._run(
                conn, 'INSERT INTO sessions (session_id, tenant_id, owner_user_id, '
                      'bot_id, room_id, session_kind, title, hidden, created_at, '
                      'updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s,%s) '
                      'ON CONFLICT DO NOTHING',
                (session_id, tenant_id, owner_user_id, bot_id, room_id,
                 session_kind, title, stamp, stamp))
            row = await self._one(
                conn, "SELECT * FROM sessions WHERE bot_id=%s AND session_kind=%s "
                      "AND (CASE WHEN %s='' THEN TRUE ELSE room_id=%s END) "
                      "ORDER BY created_at ASC LIMIT 1",
                (bot_id, session_kind, room_id, room_id))
            await conn.commit()
        return dict(row) if row else {}

    async def bot_session(self, bot_id: str, session_kind: str = 'bot_chat') -> dict | None:
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT * FROM sessions WHERE bot_id=%s AND session_kind=%s',
                (bot_id, session_kind))
        return dict(row) if row else None

    async def set_session_capability_epoch(self, session_id: str, epoch: str) -> None:
        """记录本轮系统提示的能力指纹（变了才重建提示，见 bot/prompts.py）。"""
        async with self._lease() as conn:
            await self._run(
                conn, 'UPDATE sessions SET capability_epoch=%s WHERE session_id=%s',
                (str(epoch or ''), session_id))
            await conn.commit()

    # ---------- 投递收据（机器人之间的邮箱） ----------

    async def create_delivery(self, *, delivery_id: str, tenant_id: str,
                              owner_user_id: str, from_bot: str, to_bot: str,
                              body: str, kind: str = 'dm', hop: int = 0,
                              max_hops: int = 2, room_id: str = '') -> tuple[dict, bool]:
        """落一条投递收据。返回 `(row, created)`；重复 delivery_id 返回原行。"""
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(
                conn, 'INSERT INTO bot_deliveries (delivery_id, tenant_id, '
                      'owner_user_id, from_bot, to_bot, kind, room_id, body, hop, '
                      'max_hops, status, created_at, updated_at) '
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s) "
                      'ON CONFLICT (delivery_id) DO NOTHING RETURNING *',
                (delivery_id, tenant_id, owner_user_id, from_bot, to_bot, kind,
                 room_id, body, int(hop), int(max_hops), stamp, stamp))
            created = row is not None
            if not created:
                row = await self._one(
                    conn, 'SELECT * FROM bot_deliveries WHERE delivery_id=%s',
                    (delivery_id,))
            await conn.commit()
        return (dict(row) if row else {}), created

    async def delivery(self, delivery_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT * FROM bot_deliveries WHERE delivery_id=%s',
                (delivery_id,))
        return dict(row) if row else None

    async def list_deliveries(self, *, bot_id: str = '', tenant_id: str = '',
                              owner_user_id: str = '', status: str = '',
                              limit: int = 50) -> list[dict]:
        clauses, params = [], []
        if bot_id:
            clauses.append('to_bot=%s')
            params.append(bot_id)
        if tenant_id:
            clauses.append('tenant_id=%s')
            params.append(tenant_id)
        if owner_user_id:
            clauses.append('owner_user_id=%s')
            params.append(owner_user_id)
        if status:
            clauses.append('status=%s')
            params.append(status)
        clause = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        async with self._lease() as conn:
            rows = await self._all(
                conn, f'SELECT * FROM bot_deliveries{clause} '
                      'ORDER BY created_at DESC LIMIT %s',
                (*params, max(1, min(200, int(limit)))))
        return [dict(row) for row in rows]

    async def claim_pending_deliveries(self, *, limit: int = 5) -> list[dict]:
        """取还没人处理的投递（`queued`）。

        这里**不加租约**：真正防双跑的是"同一 (session, request_id) 只能有一行
        turn"这条唯一约束（见 `bot/mailbox.py`）。重复领取最多多调一次
        `create_turn`，它会被幂等挡回 `replayed=True`，然后把收据推到 claimed。
        """
        async with self._lease() as conn:
            rows = await self._all(
                conn, "SELECT * FROM bot_deliveries WHERE status='queued' "
                      'ORDER BY created_at ASC LIMIT %s',
                (max(1, min(50, int(limit))),))
        return [dict(row) for row in rows]

    async def open_deliveries(self, *, statuses: tuple[str, ...] = ('claimed',
                                                                   'ambiguous'),
                              limit: int = 20) -> list[dict]:
        """已建过 turn、还没结算的收据（供 pump 收敛）。"""
        allowed = tuple(str(item) for item in statuses)
        if not allowed:
            return []
        placeholders = ', '.join(['%s'] * len(allowed))
        async with self._lease() as conn:
            rows = await self._all(
                conn, f'SELECT * FROM bot_deliveries WHERE status IN ({placeholders}) '
                      'ORDER BY created_at ASC LIMIT %s',
                (*allowed, max(1, min(100, int(limit)))))
        return [dict(row) for row in rows]

    async def mark_delivery(self, delivery_id: str, *, status: str,
                            turn_id: str | None = None,
                            session_id: str | None = None,
                            reason: str | None = None,
                            result: str | None = None,
                            attempts: int | None = None) -> dict | None:
        sets = ['status=%s', 'updated_at=%s']
        values: list = [status, now()]
        for column, value in (('turn_id', turn_id), ('session_id', session_id),
                              ('reason', reason), ('result', result),
                              ('attempts', attempts)):
            if value is not None:
                sets.append(f'{column}=%s')
                values.append(value)
        values.append(delivery_id)
        async with self._lease() as conn:
            row = await self._one(
                conn, f'UPDATE bot_deliveries SET {", ".join(sets)} '
                      'WHERE delivery_id=%s RETURNING *', tuple(values))
            await conn.commit()
        return dict(row) if row else None

    async def deliveries_for_turn(self, turn_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT * FROM bot_deliveries WHERE turn_id=%s', (turn_id,))
        return [dict(row) for row in rows]

    async def settle_delivery_by_turn(self, turn_id: str, *, ok: bool,
                                      answer: str = '', error: str = '') -> list[dict]:
        """某个 turn 跑完了：把它的收据从 claimed 推到终态，返回被结算的行。

        `settled` = 目标机器人真的给出了答复；`failed` = 回合失败。
        条件更新（`status IN ('claimed','ambiguous')`）保证同一张收据只被结算
        一次：重复调用拿到空列表，调用方据此不重复回送答复。

        `ambiguous` 也在这里结算——它表示"目标回合状态不明"，此时手里既然已经
        拿到了终态（成功或失败），就可以定案；真正**不重放**的约束在投递侧
        （`bot/mailbox.py` 不会为 ambiguous 再建一行 turn）。
        """
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'UPDATE bot_deliveries SET status=%s, result=%s, reason=%s, '
                      'updated_at=%s WHERE turn_id=%s '
                      "AND status IN ('claimed','ambiguous') RETURNING *",
                ('settled' if ok else 'failed', str(answer or '')[:8000],
                 '' if ok else str(error or '')[:500], now(), turn_id))
            await conn.commit()
        return [dict(row) for row in rows]

    # ---------- 房间 ----------

    async def list_room_rows(self, *, tenant_id: str, owner_user_id: str,
                             limit: int = 50) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT * FROM bot_rooms WHERE tenant_id=%s '
                      'AND owner_user_id=%s ORDER BY created_at DESC LIMIT %s',
                (tenant_id, owner_user_id, max(1, min(200, int(limit)))))
        return [decode_room(row) or {} for row in rows]

    async def room_row(self, room_id: str, *, tenant_id: str | None = None,
                       owner_user_id: str | None = None) -> dict | None:
        clauses = ['room_id=%s']
        params: list = [room_id]
        if tenant_id is not None:
            clauses.append('tenant_id=%s')
            params.append(tenant_id)
        if owner_user_id is not None:
            clauses.append('owner_user_id=%s')
            params.append(owner_user_id)
        async with self._lease() as conn:
            row = await self._one(
                conn, f'SELECT * FROM bot_rooms WHERE {" AND ".join(clauses)}',
                tuple(params))
        return decode_room(row)

    async def create_room_row(self, *, room_id: str, tenant_id: str,
                              owner_user_id: str, name: str, members: list[str],
                              topic: str = '', max_rounds: int = 3,
                              max_messages: int = 10) -> dict:
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(
                conn, 'INSERT INTO bot_rooms (room_id, tenant_id, owner_user_id, '
                      'name, topic, members_json, max_rounds, max_messages, '
                      'round_no, status, next_seq, created_at, updated_at) '
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,'open',1,%s,%s) RETURNING *",
                (room_id, tenant_id, owner_user_id, name, topic,
                 json.dumps(list(members), ensure_ascii=False),
                 int(max_rounds), int(max_messages), stamp, stamp))
            await conn.commit()
        return decode_room(row) or {}

    async def update_room_row(self, room_id: str, *, status: str | None = None,
                              round_no: int | None = None,
                              members: list[str] | None = None) -> dict | None:
        sets, values = ['updated_at=%s'], [now()]
        if status is not None:
            sets.append('status=%s')
            values.append(status)
        if round_no is not None:
            sets.append('round_no=%s')
            values.append(int(round_no))
        if members is not None:
            sets.append('members_json=%s')
            values.append(json.dumps(list(members), ensure_ascii=False))
        values.append(room_id)
        async with self._lease() as conn:
            row = await self._one(
                conn, f'UPDATE bot_rooms SET {", ".join(sets)} '
                      'WHERE room_id=%s RETURNING *', tuple(values))
            await conn.commit()
        return decode_room(row)

    async def append_room_event(self, room_id: str, *, event_id: str, kind: str,
                                actor: str, payload: dict | None = None,
                                expected_seq: int | None = None) -> dict | None:
        """追加一条房间事件，返回事件行；**幂等**（同 event_id 返回原行）。

        序号在 `SELECT ... FOR UPDATE` 的行锁里分配：一个房间同一时刻只有一个
        写者能拿到 next_seq。`expected_seq` 给调度器用（CAS 语义）：对不上就
        返回 None，调用方放弃这一拍，下一拍重放日志重新决策。
        """
        if kind not in ROOM_EVENT_KINDS:
            raise ValueError(f'未知的房间事件类型 {kind}')
        stamp = now()
        async with self._lease() as conn:
            existing = await self._one(
                conn, 'SELECT * FROM bot_room_events WHERE room_id=%s AND event_id=%s',
                (room_id, event_id))
            if existing is not None:
                await conn.commit()
                return decode_event(existing)
            row = await self._one(
                conn, 'SELECT next_seq FROM bot_rooms WHERE room_id=%s FOR UPDATE',
                (room_id,))
            if row is None:
                await conn.commit()
                return None
            seq = int(row['next_seq'])
            if expected_seq is not None and int(expected_seq) != seq:
                await conn.commit()
                return None
            await self._run(
                conn, 'INSERT INTO bot_room_events (room_id, seq, event_id, kind, '
                      'actor, payload_json, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (room_id, seq, event_id, kind, actor,
                 json.dumps(payload or {}, ensure_ascii=False), stamp))
            await self._run(
                conn, 'UPDATE bot_rooms SET next_seq=%s, updated_at=%s WHERE room_id=%s',
                (seq + 1, stamp, room_id))
            await conn.commit()
        return {'room_id': room_id, 'seq': seq, 'event_id': event_id, 'kind': kind,
                'actor': actor, 'payload': payload or {}, 'created_at': stamp}

    async def room_events(self, room_id: str, *, after_seq: int = 0,
                          limit: int = 500) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT * FROM bot_room_events WHERE room_id=%s AND seq>%s '
                      'ORDER BY seq ASC LIMIT %s',
                (room_id, int(after_seq), max(1, min(2000, int(limit)))))
        return [decode_event(row) for row in rows]

    # ---------- 房间任务（每步一个决定） ----------

    async def create_room_task(self, *, task_id: str, room_id: str, epoch: int,
                               round_no: int, position: int,
                               bot_id: str) -> tuple[dict, bool]:
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(
                conn, 'INSERT INTO bot_room_tasks (task_id, room_id, epoch, round_no, '
                      'position, bot_id, status, created_at, updated_at) '
                      "VALUES (%s,%s,%s,%s,%s,%s,'running',%s,%s) "
                      'ON CONFLICT (task_id) DO NOTHING RETURNING *',
                (task_id, room_id, int(epoch), int(round_no), int(position),
                 bot_id, stamp, stamp))
            created = row is not None
            if not created:
                row = await self._one(
                    conn, 'SELECT * FROM bot_room_tasks WHERE task_id=%s', (task_id,))
            await conn.commit()
        return (dict(row) if row else {}), created

    async def bind_room_task_turn(self, task_id: str, *, turn_id: str,
                                  session_id: str) -> None:
        async with self._lease() as conn:
            await self._run(
                conn, 'UPDATE bot_room_tasks SET turn_id=%s, session_id=%s, '
                      'updated_at=%s WHERE task_id=%s',
                (turn_id, session_id, now(), task_id))
            await conn.commit()

    async def settle_room_task(self, task_id: str, *, status: str,
                               reason: str = '') -> dict | None:
        async with self._lease() as conn:
            row = await self._one(
                conn, 'UPDATE bot_room_tasks SET status=%s, reason=%s, updated_at=%s '
                      'WHERE task_id=%s RETURNING *',
                (status, reason, now(), task_id))
            await conn.commit()
        return dict(row) if row else None

    async def open_room_tasks(self, room_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn, "SELECT * FROM bot_room_tasks WHERE room_id=%s "
                      "AND status IN ('running','indeterminate') ORDER BY created_at ASC",
                (room_id,))
        return [dict(row) for row in rows]

    async def room_tasks(self, room_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT * FROM bot_room_tasks WHERE room_id=%s '
                      'ORDER BY created_at ASC', (room_id,))
        return [dict(row) for row in rows]

    # ---------- 房间租约 ----------

    async def claim_room_lease(self, room_id: str, *, owner: str,
                               ttl: float = 60.0) -> int | None:
        """取房间调度权，返回新的 `lease_generation`；拿不到返回 None。

        规则（与 runs 的租约同口径）：自己的、已过期、或没人持有的可以拿；
        别人的且未过期的不能拿。每次成功领取代次加一，旧副本的写被条件更新挡掉。
        """
        stamp = time.time()
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT lease_owner, lease_expires_at, lease_generation '
                      'FROM bot_rooms WHERE room_id=%s FOR UPDATE', (room_id,))
            if row is None:
                await conn.commit()
                return None
            current_owner = str(row.get('lease_owner') or '')
            expires = float(row.get('lease_expires_at') or 0)
            if current_owner and current_owner != owner and expires > stamp:
                await conn.commit()
                return None
            generation = int(row.get('lease_generation') or 0) + 1
            await self._run(
                conn, 'UPDATE bot_rooms SET lease_owner=%s, lease_expires_at=%s, '
                      'lease_generation=%s, updated_at=%s WHERE room_id=%s',
                (owner, stamp + float(ttl), generation, now(), room_id))
            await conn.commit()
        return generation

    async def renew_room_lease(self, room_id: str, *, owner: str,
                               generation: int, ttl: float = 60.0) -> bool:
        async with self._lease() as conn:
            cur = await self._run(
                conn, 'UPDATE bot_rooms SET lease_expires_at=%s '
                      'WHERE room_id=%s AND lease_owner=%s AND lease_generation=%s',
                (time.time() + float(ttl), room_id, owner, int(generation)))
            await conn.commit()
            return bool(getattr(cur, 'rowcount', 0))

    async def release_room_lease(self, room_id: str, *, owner: str,
                                 generation: int) -> bool:
        """释放调度权。还有 running 任务时拒绝（否则没人接手完成它们）。"""
        async with self._lease() as conn:
            open_rows = await self._one(
                conn, "SELECT COUNT(*) AS n FROM bot_room_tasks WHERE room_id=%s "
                      "AND status='running'", (room_id,))
            if int((open_rows or {}).get('n') or 0) > 0:
                await conn.commit()
                return False
            cur = await self._run(
                conn, 'UPDATE bot_rooms SET lease_owner=%s, lease_expires_at=0 '
                      'WHERE room_id=%s AND lease_owner=%s AND lease_generation=%s',
                ('', room_id, owner, int(generation)))
            await conn.commit()
            return bool(getattr(cur, 'rowcount', 0))

    async def open_room_rows(self, *, limit: int = 5) -> list[dict]:
        """还在讨论中的房间（调度器的候选集）。租约在 claim 时再判。"""
        async with self._lease() as conn:
            rows = await self._all(
                conn, "SELECT * FROM bot_rooms WHERE status='open' "
                      'ORDER BY updated_at ASC LIMIT %s',
                (max(1, min(50, int(limit))),))
        return [decode_room(row) or {} for row in rows]
