"""消息平台（IM 网关）的仓储方法。

对应 `docs/messaging-platforms.md`。这个模块只做「行怎么读写」，**不做**协议
解析、不拼提示词、不发网络请求——那些在 `webpage_agent.messaging`。

四条不变量都靠数据库，不靠进程内状态：

1. **配置面与运行面分开**：`im_channels` 是用户配置，`im_runtime` 是 Worker 写的
   运行态快照。API 只读运行态，永远不在这里写 `connected`。
2. **入站幂等**：`im_inbox` 按 `(platform, tenant, owner, message_id)` 唯一，
   `im_processed` 按 `(platform, tenant, message_id)` 唯一，重复投递只处理一次。
3. **入站/出站都是队列**：回调渠道先落 `im_inbox` 再返回 200（快速 ACK），
   Worker 领取执行；出站进 `im_outbox`，`(tenant, owner, channel, sent_key)` 唯一，
   重试不刷屏。
4. **配对即授权**：批准后不改写用户白名单，`im_pairing.status='approved'` 就是
   授权事实，撤销时直接改状态——两处存同一件事会不一致。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

from webpage_agent.store.pg_store import now

# 用户可改的配置字段。tenant/owner/channel_id 不在白名单里：让接口改归属等于越权搬家。
CHANNEL_FIELDS = ('enabled', 'config', 'bot_id', 'dm_policy', 'group_policy',
                  'allowed_users')
JSON_COLUMNS = {'config': 'config_json', 'allowed_users': 'allowed_users_json'}

PAIRING_TTL_SECONDS = 3600
PAIRING_MAX_PENDING = 3
PAIRING_COOLDOWN_SECONDS = 600
PAIRING_MAX_ATTEMPTS = 5


def _json(value, default):
    if value is None or value == '':
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def decode_channel(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out['config'] = _json(out.pop('config_json', None), {})
    out['allowed_users'] = [str(item) for item in
                            _json(out.pop('allowed_users_json', None), [])]
    out['enabled'] = bool(out.get('enabled'))
    return out


def decode_inbox(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out['payload'] = _json(out.pop('payload_json', None), {})
    return out


class PgMessaging:
    """消息平台配置 / 运行态 / 队列 / 配对 / 会话映射的 PostgreSQL 实现。"""

    # ---------- 渠道配置 ----------

    async def im_list_channels(self, *, tenant_id: str,
                               owner_user_id: str) -> list[dict]:
        """该 owner 的全部实例。同一渠道可能有多行（每个机器人一套）。"""
        async with self._lease() as conn:
            rows = await self._all(conn,
                'SELECT * FROM im_channels WHERE tenant_id=%s AND owner_user_id=%s '
                'ORDER BY channel_id, instance_id', (tenant_id, owner_user_id))
        return [decode_channel(row) for row in rows]

    async def im_channel(self, channel_id: str, *, tenant_id: str,
                         owner_user_id: str, instance_id: str = '') -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM im_channels WHERE tenant_id=%s AND owner_user_id=%s '
                'AND channel_id=%s AND instance_id=%s',
                (tenant_id, owner_user_id, channel_id, instance_id))
        return decode_channel(row)

    async def im_upsert_channel(self, channel_id: str, *, tenant_id: str,
                                owner_user_id: str, instance_id: str = '',
                                **fields) -> dict:
        """按白名单字段写入；缺省字段保持原值（partial update 语义）。

        `instance_id` 是定位坐标，不参与白名单——它决定改的是**哪一份**配置，
        不是配置内容。
        """
        stamp = now()
        values: dict = {}
        for key, value in fields.items():
            if key not in CHANNEL_FIELDS or value is None:
                continue
            if key == 'enabled':
                values[key] = 1 if value else 0
            elif key in JSON_COLUMNS:
                values[JSON_COLUMNS[key]] = _dumps(value)
            else:
                values[key] = value
        async with self._lease() as conn:
            existing = await self._one(conn,
                'SELECT channel_id FROM im_channels WHERE tenant_id=%s '
                'AND owner_user_id=%s AND channel_id=%s AND instance_id=%s',
                (tenant_id, owner_user_id, channel_id, instance_id))
            if existing is None:
                columns = ['tenant_id', 'owner_user_id', 'channel_id', 'instance_id',
                           'created_at', 'updated_at', *values.keys()]
                params = [tenant_id, owner_user_id, channel_id, instance_id, stamp, stamp,
                          *values.values()]
                marks = ','.join(['%s'] * len(columns))
                await self._run(conn,
                    f'INSERT INTO im_channels ({",".join(columns)}) VALUES ({marks})',
                    tuple(params))
            elif values:
                sets = ', '.join(f'{key}=%s' for key in values)
                await self._run(conn,
                    f'UPDATE im_channels SET {sets}, updated_at=%s '
                    'WHERE tenant_id=%s AND owner_user_id=%s AND channel_id=%s '
                    'AND instance_id=%s',
                    (*values.values(), stamp, tenant_id, owner_user_id, channel_id,
                     instance_id))
            row = await self._one(conn,
                'SELECT * FROM im_channels WHERE tenant_id=%s AND owner_user_id=%s '
                'AND channel_id=%s AND instance_id=%s',
                (tenant_id, owner_user_id, channel_id, instance_id))
        return decode_channel(row) or {}

    async def im_channel_owner(self, channel_id: str, *,
                               tenant_id: str, instance_id: str = '') -> dict | None:
        """同一租户内这个渠道的这个实例是否已被某个账号配置（实例凭据是部署级的）。"""
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM im_channels WHERE tenant_id=%s AND channel_id=%s '
                'AND instance_id=%s ORDER BY created_at LIMIT 1',
                (tenant_id, channel_id, instance_id))
        return decode_channel(row) if row else None

    async def im_enabled_channels(self) -> list[dict]:
        """Worker 用：所有租户/用户里启用的渠道，按归属分组启动。"""
        async with self._lease() as conn:
            rows = await self._all(conn,
                'SELECT * FROM im_channels WHERE enabled=1 '
                'ORDER BY tenant_id, owner_user_id, channel_id, instance_id')
        return [decode_channel(row) for row in rows]

    async def im_delete_instance(self, instance_id: str, *, tenant_id: str,
                                 owner_user_id: str) -> int:
        """删掉一个机器人实例的全部配置与运行态，返回删掉的渠道行数。

        默认实例（`''`）**不许删**：它就是存量那一份配置，删掉等于把这条渠道
        从部署里摘掉，那是「停用」该做的事，不是「删机器人」。
        """
        key = str(instance_id or '')
        if not key:
            raise ValueError('默认实例不能删除')
        async with self._lease() as conn:
            cur = await self._run(conn,
                'DELETE FROM im_channels WHERE tenant_id=%s AND owner_user_id=%s '
                'AND instance_id=%s', (tenant_id, owner_user_id, key))
            removed = int(cur.rowcount or 0)
            await self._run(conn,
                'DELETE FROM im_runtime WHERE tenant_id=%s AND owner_user_id=%s '
                'AND instance_id=%s', (tenant_id, owner_user_id, key))
            # 没发出去的出站消息一起清：实例没了 Worker 不会再为它建 Transport，
            # 这些行只会永远躺在队列里。已发/失败的留着当排障记录。
            await self._run(conn,
                'DELETE FROM im_outbox WHERE tenant_id=%s AND owner_user_id=%s '
                "AND instance_id=%s AND status IN ('queued','retry')",
                (tenant_id, owner_user_id, key))
        return removed

    # ---------- 运行态（Worker 单写，API 只读） ----------

    async def im_set_runtime(self, channel_id: str, *, tenant_id: str,
                             owner_user_id: str, state: str, detail: str = '',
                             error_code: str = '', fingerprint: str = '',
                             worker_id: str = '', instance_id: str = '') -> None:
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn,
                'INSERT INTO im_runtime (tenant_id, owner_user_id, channel_id, '
                ' instance_id, state, detail, error_code, fingerprint, worker_id, '
                ' updated_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (tenant_id, owner_user_id, channel_id, instance_id) '
                'DO UPDATE SET '
                'state=excluded.state, detail=excluded.detail, '
                'error_code=excluded.error_code, fingerprint=excluded.fingerprint, '
                'worker_id=excluded.worker_id, updated_at=excluded.updated_at',
                (tenant_id, owner_user_id, channel_id, instance_id, state, detail,
                 error_code, fingerprint, worker_id, stamp))

    async def im_runtime(self, channel_id: str, *, tenant_id: str,
                         owner_user_id: str, instance_id: str = '') -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM im_runtime WHERE tenant_id=%s AND owner_user_id=%s '
                'AND channel_id=%s AND instance_id=%s',
                (tenant_id, owner_user_id, channel_id, instance_id))
        return dict(row) if row else None

    async def im_runtime_all(self) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT * FROM im_runtime')
        return [dict(row) for row in rows]

    async def im_runtime_list(self, *, tenant_id: str,
                              owner_user_id: str) -> list[dict]:
        """一个账号下所有实例的运行态。实例列表用它，避免按渠道逐个查。"""
        async with self._lease() as conn:
            rows = await self._all(conn,
                'SELECT * FROM im_runtime WHERE tenant_id=%s AND owner_user_id=%s',
                (tenant_id, owner_user_id))
        return [dict(row) for row in rows]

    async def im_clear_runtime(self, channel_id: str, *, tenant_id: str,
                               owner_user_id: str, instance_id: str = '') -> None:
        async with self._lease() as conn:
            await self._run(conn,
                'DELETE FROM im_runtime WHERE tenant_id=%s AND owner_user_id=%s '
                'AND channel_id=%s AND instance_id=%s',
                (tenant_id, owner_user_id, channel_id, instance_id))

    # ---------- 幂等 ----------

    async def im_mark_seen(self, platform: str, tenant_id: str, message_id: str,
                           *, ttl_seconds: float = 86400) -> bool:
        """同一平台消息只返回一次 True。顺手清理 TTL 外的旧行（有索引，很便宜）。"""
        if not message_id:
            return True
        stamp = time.time()
        async with self._lease() as conn:
            inserted = await self._one(conn,
                'INSERT INTO im_processed (platform, tenant_id, message_id, created_at) '
                'VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING message_id',
                (platform, tenant_id, message_id, stamp))
            await self._run(conn,
                'DELETE FROM im_processed WHERE created_at < %s',
                (stamp - float(ttl_seconds),))
        return inserted is not None

    # ---------- 入站队列 ----------

    async def im_inbox_add(self, *, platform: str, tenant_id: str,
                           owner_user_id: str, message_id: str,
                           payload: dict, instance_id: str = '') -> bool:
        """回调渠道先把入站消息落库再 ACK。重复消息返回 False。

        `instance_id` 记下这条消息是**哪个实例**收回来的：Worker 领取时要据此
        选回哪个应用，选错就是用 A 应用的凭据回 B 应用的消息。
        """
        stamp = now()
        inbox_id = uuid.uuid4().hex
        async with self._lease() as conn:
            row = await self._one(conn,
                'INSERT INTO im_inbox (inbox_id, platform, tenant_id, owner_user_id, '
                ' instance_id, message_id, payload_json, status, attempts, next_at, '
                ' created_at, updated_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,%s) '
                'ON CONFLICT (platform, tenant_id, owner_user_id, message_id) DO NOTHING '
                'RETURNING inbox_id',
                (inbox_id, platform, tenant_id, owner_user_id, instance_id, message_id,
                 _dumps(payload), 'queued', stamp, stamp))
        return row is not None

    async def im_inbox_claim(self, limit: int = 5,
                             stale_seconds: float = 900) -> list[dict]:
        stamp = time.time()
        cutoff = datetime.fromtimestamp(
            stamp - max(30.0, float(stale_seconds)), timezone.utc).isoformat()
        async with self._lease() as conn:
            rows = await self._all(conn,
                'WITH due AS ('
                '  SELECT inbox_id FROM im_inbox '
                "  WHERE (status IN ('queued','retry') AND next_at <= %s) "
                "     OR (status='processing' AND updated_at < %s) "
                '  ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED'
                ') '
                'UPDATE im_inbox SET status=%s, attempts=attempts+1, updated_at=%s '
                'WHERE inbox_id IN (SELECT inbox_id FROM due) RETURNING *',
                (stamp, cutoff, max(1, int(limit)), 'processing', now()))
        return [decode_inbox(row) for row in rows]

    async def im_inbox_requeue(self, inbox_id: str, *,
                               delay_seconds: float = 15,
                               max_age_seconds: float = 86400) -> bool:
        """渠道暂时没就绪：把消息放回队列，不消耗失败次数。

        超过 24 小时还不处理就返回 False（调用方把它标失败），避免一条永远
        没人消费的消息在库里躺着。
        """
        now_ts = time.time()
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT created_at FROM im_inbox WHERE inbox_id=%s', (inbox_id,))
            if row is None:
                return False
            try:
                created = datetime.fromisoformat(str(row.get('created_at') or ''))
                age = now_ts - created.timestamp()
            except (TypeError, ValueError):
                age = 0.0
            if age > float(max_age_seconds):
                return False
            await self._run(conn,
                "UPDATE im_inbox SET status='retry', next_at=%s, updated_at=%s "
                'WHERE inbox_id=%s',
                (now_ts + max(1.0, float(delay_seconds)), now(), inbox_id))
        return True

    async def im_inbox_finish(self, inbox_id: str, *, ok: bool,
                              retry: bool = False, error_code: str = '',
                              delay_seconds: float = 0) -> None:
        stamp = time.time()
        if ok:
            status, next_at = 'done', 0
        elif retry:
            status, next_at = 'retry', stamp + max(1.0, delay_seconds)
        else:
            status, next_at = 'failed', 0
        async with self._lease() as conn:
            await self._run(conn,
                'UPDATE im_inbox SET status=%s, next_at=%s, updated_at=%s '
                'WHERE inbox_id=%s', (status, next_at, now(), inbox_id))

    async def im_inbox_backlog(self, *, tenant_id: str, owner_user_id: str,
                               instance_id: str | None = None) -> int:
        """待处理入站条数。`instance_id=None` 统计全部实例，给了就只算那一个。"""
        clauses = ['tenant_id=%s', 'owner_user_id=%s',
                   "status IN ('queued','retry','processing')"]
        params: list = [tenant_id, owner_user_id]
        if instance_id is not None:
            clauses.append('instance_id=%s')
            params.append(str(instance_id))
        async with self._lease() as conn:
            row = await self._one(conn,
                f'SELECT COUNT(*) AS n FROM im_inbox WHERE {" AND ".join(clauses)}',
                tuple(params))
        return int((row or {}).get('n') or 0)

    # ---------- 配对 ----------

    async def im_pairing_pending(self, *, tenant_id: str,
                                 owner_user_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn,
                "SELECT * FROM im_pairing WHERE tenant_id=%s AND owner_user_id=%s "
                "AND status='pending' AND expires_at > %s ORDER BY created_at",
                (tenant_id, owner_user_id, time.time()))
        return [dict(row) for row in rows]

    async def im_pairing_approved(self, *, tenant_id: str, owner_user_id: str,
                                  platform: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn,
                "SELECT * FROM im_pairing WHERE tenant_id=%s AND owner_user_id=%s "
                "AND platform=%s AND status='approved' ORDER BY updated_at DESC",
                (tenant_id, owner_user_id, platform))
        return [dict(row) for row in rows]

    async def im_pairing_approved_users(self, *, tenant_id: str,
                                        owner_user_id: str,
                                        platform: str) -> set[str]:
        rows = await self.im_pairing_approved(
            tenant_id=tenant_id, owner_user_id=owner_user_id, platform=platform)
        return {str(row.get('user_id') or '') for row in rows if row.get('user_id')}

    async def im_pairing_issue(self, *, tenant_id: str, owner_user_id: str,
                               platform: str, user_id: str, chat_id: str,
                               code_hash: str, instance_id: str = '') -> tuple[bool, str]:
        """登记一个待批二维码请求。返回 (ok, reason)。

        限流照抄 Hermes 的量级：每平台最多 3 个待批、同一用户 10 分钟一次。
        超过上限不是错误——静默丢弃比给攻击者反馈更好，所以调用方拿到 reason
        只写日志。
        """
        stamp = now()
        now_ts = time.time()
        async with self._lease() as conn:
            pending = await self._one(conn,
                "SELECT COUNT(*) AS n FROM im_pairing WHERE tenant_id=%s "
                "AND owner_user_id=%s AND platform=%s AND status='pending' "
                'AND expires_at > %s',
                (tenant_id, owner_user_id, platform, now_ts))
            if int((pending or {}).get('n') or 0) >= PAIRING_MAX_PENDING:
                return False, 'too_many_pending'
            recent = await self._one(conn,
                'SELECT updated_at FROM im_pairing WHERE tenant_id=%s '
                'AND owner_user_id=%s AND platform=%s AND user_id=%s',
                (tenant_id, owner_user_id, platform, user_id))
            if recent is not None:
                try:
                    from datetime import datetime
                    last = datetime.fromisoformat(str(recent['updated_at']))
                    if (datetime.now(last.tzinfo).timestamp()
                            - last.timestamp()) < PAIRING_COOLDOWN_SECONDS:
                        return False, 'cooldown'
                except (TypeError, ValueError):
                    pass
            await self._run(conn,
                'INSERT INTO im_pairing (tenant_id, owner_user_id, platform, '
                ' instance_id, user_id, chat_id, code_hash, status, attempts, '
                ' expires_at, created_at, updated_at) '
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',0,%s,%s,%s) "
                'ON CONFLICT (tenant_id, owner_user_id, platform, user_id) DO UPDATE SET '
                'instance_id=excluded.instance_id, chat_id=excluded.chat_id, '
                'code_hash=excluded.code_hash, '
                "status='pending', attempts=0, expires_at=excluded.expires_at, "
                'updated_at=excluded.updated_at',
                (tenant_id, owner_user_id, platform, instance_id, user_id, chat_id,
                 code_hash, now_ts + PAIRING_TTL_SECONDS, stamp, stamp))
        return True, ''

    async def im_pairing_approve(self, *, tenant_id: str, owner_user_id: str,
                                 code_hash: str) -> dict | None:
        """按配对码批准。错误码只累计 attempts，达上限即作废（防暴力猜码）。"""
        now_ts = time.time()
        async with self._lease() as conn:
            # 一张表里可能同时有多个待批用户；逐个比对码。
            rows = await self._all(conn,
                "SELECT * FROM im_pairing WHERE tenant_id=%s AND owner_user_id=%s "
                "AND status='pending' AND expires_at > %s ORDER BY created_at",
                (tenant_id, owner_user_id, now_ts))
            matched = next((item for item in rows
                            if str(item.get('code_hash') or '') == code_hash), None)
            if matched is None:
                await self._run(conn,
                    "UPDATE im_pairing SET attempts=attempts+1, updated_at=%s "
                    "WHERE tenant_id=%s AND owner_user_id=%s AND status='pending' "
                    'AND attempts < %s',
                    (now(), tenant_id, owner_user_id, PAIRING_MAX_ATTEMPTS))
                await self._run(conn,
                    "UPDATE im_pairing SET status='expired', updated_at=%s "
                    "WHERE tenant_id=%s AND owner_user_id=%s AND status='pending' "
                    'AND attempts >= %s',
                    (now(), tenant_id, owner_user_id, PAIRING_MAX_ATTEMPTS))
                return None
            await self._run(conn,
                "UPDATE im_pairing SET status='approved', expires_at=0, "
                'attempts=0, updated_at=%s WHERE tenant_id=%s AND owner_user_id=%s '
                'AND platform=%s AND user_id=%s',
                (now(), tenant_id, owner_user_id, matched['platform'],
                 matched['user_id']))
        return dict(matched)

    async def im_pairing_revoke(self, *, tenant_id: str, owner_user_id: str,
                                platform: str, user_id: str) -> bool:
        async with self._lease() as conn:
            cur = await self._run(conn,
                'DELETE FROM im_pairing WHERE tenant_id=%s AND owner_user_id=%s '
                'AND platform=%s AND user_id=%s',
                (tenant_id, owner_user_id, platform, user_id))
        return bool(cur.rowcount)

    # ---------- 会话映射 ----------

    async def im_session(self, *, tenant_id: str, owner_user_id: str,
                         platform: str, chat_id: str,
                         instance_id: str = '') -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM im_sessions WHERE tenant_id=%s AND owner_user_id=%s '
                'AND platform=%s AND instance_id=%s AND chat_id=%s',
                (tenant_id, owner_user_id, platform, instance_id, chat_id))
        return dict(row) if row else None

    async def im_session_bind(self, *, tenant_id: str, owner_user_id: str,
                              platform: str, chat_id: str, chat_type: str,
                              session_id: str, generation: int = 1,
                              bot_id: str | None = None,
                              instance_id: str = '') -> dict:
        """记下「这个 IM 会话 ↔ 哪条 Agent 会话」，以及它是从哪个实例进来的。

        `instance_id` 必须记：这段会话的有效身份要按 **它所属实例** 的渠道配置
        解析（两个飞书应用各绑各的机器人）。不记的话，实例 B 的群会按实例 A 的
        默认机器人算出一个"有效身份"，卡片上写的人和回话的人就不是一个人了。

        `bot_id=None` 表示**不要动**这一行已有的身份覆盖（`/new` 就走这条，
        换个会话不该顺手把用户手工指定的机器人清掉）；传空串才是「恢复跟随
        渠道默认」。
        """
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn,
                'INSERT INTO im_sessions (tenant_id, owner_user_id, platform, '
                ' instance_id, chat_id, chat_type, session_id, generation, bot_id, '
                ' created_at, updated_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,\'\'),%s,%s) '
                'ON CONFLICT (tenant_id, owner_user_id, platform, instance_id, chat_id) '
                'DO UPDATE SET '
                'chat_type=excluded.chat_type, session_id=excluded.session_id, '
                'generation=excluded.generation, '
                'bot_id=COALESCE(%s, im_sessions.bot_id), '
                'updated_at=excluded.updated_at',
                (tenant_id, owner_user_id, platform, instance_id, chat_id, chat_type,
                 session_id, int(generation), bot_id, stamp, stamp, bot_id))
            row = await self._one(conn,
                'SELECT * FROM im_sessions WHERE tenant_id=%s AND owner_user_id=%s '
                'AND platform=%s AND instance_id=%s AND chat_id=%s',
                (tenant_id, owner_user_id, platform, instance_id, chat_id))
        return dict(row) if row else {}

    async def im_session_set_bot(self, *, tenant_id: str, owner_user_id: str,
                                 platform: str, chat_id: str, bot_id: str,
                                 instance_id: str = '') -> bool:
        """只改身份覆盖，不碰会话映射（`/bind` `/unbind` 用）。

        行不存在返回 False：那种情况该由 bridge 建行，凭空造一条指向不存在
        session 的映射只会让下次消息落到一条空会话上。
        """
        async with self._lease() as conn:
            cur = await self._run(conn,
                'UPDATE im_sessions SET bot_id=%s, updated_at=%s WHERE tenant_id=%s '
                'AND owner_user_id=%s AND platform=%s AND instance_id=%s AND chat_id=%s',
                (bot_id, now(), tenant_id, owner_user_id, platform, instance_id,
                 chat_id))
        return bool(cur.rowcount)

    async def im_session_touch(self, *, tenant_id: str, owner_user_id: str,
                               platform: str, chat_id: str,
                               instance_id: str = '') -> None:
        """把 `updated_at` 推到当前时间：控制面按它排「最近活跃」。

        每条消息一次小 UPDATE。不这么做的话，这一列只在换会话/改绑定时变，
        卡片上写「最近活跃」却显示三天前的换绑时间，是在骗人。
        """
        async with self._lease() as conn:
            await self._run(conn,
                'UPDATE im_sessions SET updated_at=%s WHERE tenant_id=%s '
                'AND owner_user_id=%s AND platform=%s AND instance_id=%s '
                'AND chat_id=%s',
                (now(), tenant_id, owner_user_id, platform, instance_id, chat_id))

    async def im_session_bot_id(self, session_id: str) -> str | None:
        """这条 Agent 会话现在挂的是哪个机器人；会话已被清理时返回 None。

        返回 None 与返回 ``''`` 必须分开：前者是「查不到」，后者是「明确绑的是
        通用助手」。合并成一个值会让 bridge 在会话被清理后误判成身份变化。
        """
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT bot_id FROM sessions WHERE session_id=%s', (session_id,))
        if row is None:
            return None
        return str(row.get('bot_id') or '')

    async def im_sessions_list(self, *, tenant_id: str, owner_user_id: str,
                               platform: str | None = None,
                               instance_id: str | None = None,
                               limit: int = 30) -> list[dict]:
        """最近的 IM 会话映射，供「会话身份」卡使用。

        按 `updated_at` 倒序（每条消息都会摸一下这个时间），不是按创建时间：
        用户要找的通常是「刚才在聊的那个群」。
        """
        clauses = ['tenant_id=%s', 'owner_user_id=%s']
        params: list = [tenant_id, owner_user_id]
        if platform is not None:
            clauses.append('platform=%s')
            params.append(platform)
        if instance_id is not None:
            clauses.append('instance_id=%s')
            params.append(instance_id)
        async with self._lease() as conn:
            rows = await self._all(conn,
                f'SELECT * FROM im_sessions WHERE {" AND ".join(clauses)} '
                'ORDER BY updated_at DESC LIMIT %s',
                (*params, max(1, min(200, int(limit)))))
        return [dict(row) for row in rows]

    async def im_session_bump(self, *, tenant_id: str, owner_user_id: str,
                              platform: str, chat_id: str,
                              session_id: str, instance_id: str = '') -> dict:
        """`/new`：同一 IM 会话换一条 Agent 会话，历史留在旧 session 行里。"""
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn,
                'UPDATE im_sessions SET generation=generation+1, session_id=%s, '
                'updated_at=%s WHERE tenant_id=%s AND owner_user_id=%s '
                'AND platform=%s AND instance_id=%s AND chat_id=%s',
                (session_id, stamp, tenant_id, owner_user_id, platform, instance_id,
                 chat_id))
            row = await self._one(conn,
                'SELECT * FROM im_sessions WHERE tenant_id=%s AND owner_user_id=%s '
                'AND platform=%s AND instance_id=%s AND chat_id=%s',
                (tenant_id, owner_user_id, platform, instance_id, chat_id))
        return dict(row) if row else {}

    async def im_create_session(self, session_id: str, *, tenant_id: str,
                                owner_user_id: str, bot_id: str,
                                title: str) -> None:
        """建 Agent 会话行。`im_chat` 对普通侧栏隐藏，只能从消息平台进。

        `ON CONFLICT DO NOTHING` 是**故意的**，不要改成 upsert `bot_id`：
        换绑机器人后，如果就地改写这条会话的身份，旧对话里带着上一个人格的
        记忆、技能与工作目录会继续被新人格读到（"它记得不该记得的事"）。正确
        做法是换一代新会话——见 `ImBridge._session` 的身份守卫。
        """
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn,
                'INSERT INTO sessions (session_id, tenant_id, owner_user_id, '
                ' bot_id, session_kind, title, hidden, created_at, updated_at) '
                "VALUES (%s,%s,%s,%s,'im_chat',%s,1,%s,%s) ON CONFLICT DO NOTHING",
                (session_id, tenant_id, owner_user_id, bot_id, title, stamp, stamp))

    # ---------- 出站队列 ----------

    async def im_outbox_add(self, *, tenant_id: str, owner_user_id: str,
                            channel_id: str, platform_chat_id: str,
                            sent_key: str, text: str,
                            instance_id: str = '') -> bool:
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(conn,
                'INSERT INTO im_outbox (outbox_id, tenant_id, owner_user_id, channel_id, '
                ' instance_id, platform_chat_id, sent_key, text, status, attempts, '
                ' next_at, last_error, created_at, updated_at) '
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'queued',0,0,'',%s,%s) "
                'ON CONFLICT (tenant_id, owner_user_id, channel_id, sent_key) '
                'DO NOTHING RETURNING outbox_id',
                (uuid.uuid4().hex, tenant_id, owner_user_id, channel_id, instance_id,
                 platform_chat_id, sent_key, text, stamp, stamp))
        return row is not None

    async def im_outbox_claim(self, channel_id: str, *, tenant_id: str,
                              owner_user_id: str, limit: int = 5,
                              stale_seconds: float = 300,
                              instance_id: str = '') -> list[dict]:
        """领取要发的消息。**必须按实例领**：同渠道的两个实例共用 channel_id，
        不筛实例就会用 A 的 Transport 发 B 的消息（消息发到别的应用里去）。"""
        stamp = time.time()
        cutoff = datetime.fromtimestamp(
            stamp - max(30.0, float(stale_seconds)), timezone.utc).isoformat()
        async with self._lease() as conn:
            rows = await self._all(conn,
                'WITH due AS ('
                '  SELECT outbox_id FROM im_outbox '
                '  WHERE tenant_id=%s AND owner_user_id=%s AND channel_id=%s '
                '    AND instance_id=%s '
                "    AND ((status IN ('queued','retry') AND next_at <= %s) "
                "         OR (status='sending' AND updated_at < %s)) "
                '  ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED'
                ') '
                "UPDATE im_outbox SET status='sending', attempts=attempts+1, "
                'updated_at=%s WHERE outbox_id IN (SELECT outbox_id FROM due) '
                'RETURNING *',
                (tenant_id, owner_user_id, channel_id, instance_id, stamp, cutoff,
                 max(1, int(limit)), now()))
        return [dict(row) for row in rows]

    async def im_outbox_finish(self, outbox_id: str, *, ok: bool,
                               retry: bool = False, error: str = '',
                               delay_seconds: float = 0) -> None:
        stamp = time.time()
        if ok:
            status, next_at = 'sent', 0
        elif retry:
            status, next_at = 'retry', stamp + max(1.0, delay_seconds)
        else:
            status, next_at = 'failed', 0
        async with self._lease() as conn:
            await self._run(conn,
                'UPDATE im_outbox SET status=%s, next_at=%s, last_error=%s, '
                'updated_at=%s WHERE outbox_id=%s',
                (status, next_at, (error or '')[:400], now(), outbox_id))

    async def im_outbox_pending(self, *, tenant_id: str, owner_user_id: str,
                                channel_id: str,
                                instance_id: str | None = None) -> int:
        """还有多少条没发出去。`instance_id=None` 统计全部实例。"""
        clauses = ['tenant_id=%s', 'owner_user_id=%s', 'channel_id=%s',
                   "status IN ('queued','retry','sending')"]
        params: list = [tenant_id, owner_user_id, channel_id]
        if instance_id is not None:
            clauses.append('instance_id=%s')
            params.append(str(instance_id))
        async with self._lease() as conn:
            row = await self._one(conn,
                f'SELECT COUNT(*) AS n FROM im_outbox WHERE {" AND ".join(clauses)}',
                tuple(params))
        return int((row or {}).get('n') or 0)

    # ---------- iLink 登录态 ----------

    async def im_login_get(self, channel_id: str, *, tenant_id: str,
                           owner_user_id: str) -> dict:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT state_json, updated_at FROM im_login_state '
                'WHERE tenant_id=%s AND owner_user_id=%s AND channel_id=%s',
                (tenant_id, owner_user_id, channel_id))
        if row is None:
            return {}
        data = _json(row.get('state_json'), {})
        data['updated_at'] = row.get('updated_at') or ''
        return data

    async def im_login_set(self, channel_id: str, *, tenant_id: str,
                           owner_user_id: str, state: dict) -> None:
        async with self._lease() as conn:
            await self._run(conn,
                'INSERT INTO im_login_state (tenant_id, owner_user_id, channel_id, '
                ' state_json, updated_at) VALUES (%s,%s,%s,%s,%s) '
                'ON CONFLICT (tenant_id, owner_user_id, channel_id) DO UPDATE SET '
                'state_json=excluded.state_json, updated_at=excluded.updated_at',
                (tenant_id, owner_user_id, channel_id, _dumps(state), now()))

    async def im_login_clear(self, channel_id: str, *, tenant_id: str,
                             owner_user_id: str) -> None:
        async with self._lease() as conn:
            await self._run(conn,
                'DELETE FROM im_login_state WHERE tenant_id=%s AND owner_user_id=%s '
                'AND channel_id=%s', (tenant_id, owner_user_id, channel_id))

    # ---------- 审计事件 ----------

    async def im_event_add(self, *, tenant_id: str, owner_user_id: str,
                           channel_id: str, direction: str, chat_id_hash: str,
                           message_id: str = '', status: str = '',
                           error_code: str = '', instance_id: str = '') -> None:
        async with self._lease() as conn:
            await self._run(conn,
                'INSERT INTO im_events (tenant_id, owner_user_id, channel_id, '
                ' instance_id, direction, chat_id_hash, message_id, status, '
                ' error_code, created_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (tenant_id, owner_user_id, channel_id, instance_id, direction,
                 chat_id_hash, message_id, status, error_code, now()))

    async def im_events_list(self, *, tenant_id: str, owner_user_id: str,
                             channel_id: str = '', limit: int = 50,
                             instance_id: str | None = None) -> list[dict]:
        clauses = ['tenant_id=%s', 'owner_user_id=%s']
        params: list = [tenant_id, owner_user_id]
        if channel_id:
            clauses.append('channel_id=%s')
            params.append(channel_id)
        if instance_id is not None:
            clauses.append('instance_id=%s')
            params.append(str(instance_id))
        async with self._lease() as conn:
            rows = await self._all(conn,
                f'SELECT * FROM im_events WHERE {" AND ".join(clauses)} '
                'ORDER BY event_id DESC LIMIT %s',
                (*params, max(1, min(200, int(limit)))))
        return [dict(row) for row in rows]

    async def im_event_cleanup(self, *, keep_seconds: float = 604800) -> int:
        """按时间水位清理审计事件。默认保留 7 天。"""
        from datetime import datetime, timezone
        cutoff = datetime.fromtimestamp(
            time.time() - max(0.0, float(keep_seconds)),
            timezone.utc).isoformat()
        async with self._lease() as conn:
            cur = await self._run(conn,
                'DELETE FROM im_events WHERE created_at < %s', (cutoff,))
            removed = int(cur.rowcount or 0)
            # 终态的入站/出站记录也一起清：它们只用于排障，留着会无限增长。
            for table in ('im_inbox', 'im_outbox'):
                cur = await self._run(conn,
                    f'DELETE FROM {table} WHERE status IN '
                    "('done','failed','sent') AND updated_at < %s", (cutoff,))
                removed += int(cur.rowcount or 0)
        return removed


    # ---------- 网关单实例锁 ----------
    # PostgreSQL 的会话级 advisory lock：连接不关，锁就不放。Worker 可能多副本，
    # 但同一条 iLink 长轮询只能有一个消费者——否则消息被两边抢着领、回复发两遍。
    # 只让抢到锁的那个 Worker 启动消息网关；其余进程控制面显示 gateway_stopped。

    async def im_take_leader_lock(self, lock_id: int):
        """抢到返回连接对象（必须一直持有），抢不到返回 None。

        用**池外**的独立连接：池大小可能是 1，从池里借一条然后一直不还，
        后续所有查询都会排队等这条连接——那等于自己把自己锁死。
        """
        import psycopg
        await self.ready()
        conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
        try:
            cur = await conn.execute(
                'SELECT pg_try_advisory_lock(%s) AS ok', (int(lock_id),))
            row = await cur.fetchone()
            acquired = bool(row[0]) if row else False
            if not acquired:
                await conn.close()
                return None
            return conn
        except BaseException:
            await conn.close()
            raise

    async def im_release_leader_lock(self, conn, lock_id: int) -> None:
        if conn is None:
            return
        try:
            await conn.execute('SELECT pg_advisory_unlock(%s)', (int(lock_id),))
        finally:
            await conn.close()
