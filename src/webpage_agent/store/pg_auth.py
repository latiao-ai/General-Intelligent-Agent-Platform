"""身份、票据、登录会话、会话消息，以及长期记忆。

`bind_identity` 到 `list_sessions`，再加上 `remember` / `recall` / `forget` /
`is_forgotten`，原样从 `PostgresStore` 移来。方法体没有改。
"""
import json
import time
import uuid

import psycopg

from webpage_agent.store.pg_store import TicketAlreadyUsed, now

class PgAuth:
    """PgAuth 方法组。见模块说明。"""

    # ---------- 身份（webpage_agent/api/auth.py） ----------

    async def bind_identity(self, provider: str, subject: str, *,
                            email: str = '', display_name: str = '',
                            tenant_id: str = 'default', role: str = 'member',
                            identity_provider_id: str = '') -> dict:
        """平台身份 → 本地主体：命中就返回，没有就建号。

        首登并发靠 `ON CONFLICT ... RETURNING` 收敛：两个请求同时进来时由数据库
        决定谁赢，输的一方拿到赢家的 `user_id` 并返回，不会出现两个账号各自持有
        一半历史。因此**返回值必须取自 RETURNING，不能用本地生成的 user_id**。

        常见路径（已绑定过）只有一次 SELECT，不写库。
        """
        stamp = now()
        # 身份源的稳定编号是隔离键：两个平台各自有个 user-123，必须落到两个账号
        idp = (identity_provider_id or provider).strip()
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'SELECT user_id, tenant_id, role FROM identities '
                'WHERE tenant_id=%s AND identity_provider_id=%s AND subject=%s',
                (tenant_id, idp, subject))
            if row is not None:
                # last_seen 只用于运营统计，失败不该影响登录，所以不参与返回判断
                await self._run(
                    conn,
                    'UPDATE identities SET last_seen_at=%s '
                    'WHERE tenant_id=%s AND identity_provider_id=%s AND subject=%s',
                    (stamp, tenant_id, idp, subject))
                await conn.commit()
                return dict(row)

            user_id = 'u_' + uuid.uuid4().hex[:16]
            await self._run(
                conn,
                'INSERT INTO app_users '
                '(user_id, display_name, email, status, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING',
                (user_id, display_name, email, 'active', stamp, stamp))
            row = await self._one(
                conn,
                'INSERT INTO identities '
                '(provider, subject, user_id, tenant_id, identity_provider_id, '
                ' email, role, created_at, last_seen_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (tenant_id, identity_provider_id, subject) '
                '  WHERE identity_provider_id <> \'\' '
                '  DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at '
                'RETURNING user_id, tenant_id, role',
                (idp, subject, user_id, tenant_id, idp, email, role,
                 stamp, stamp))
            await conn.commit()
            return dict(row)

    async def redeem_ticket(self, *, issuer: str, jti: str, expires_at: int,
                            connection_id: str, platform_id: str,
                            tenant_id: str, identity_provider_id: str,
                            subject: str, session_id: str,
                            session_digest: str, session_expires_at: float,
                            email: str = '', display_name: str = '',
                            user_agent: str = '', remote_addr: str = ''
                            ) -> dict:
        """**一次事务里**完成：登记票据已用 → 绑定身份 → 建中台登录会话。

        为什么必须同一个事务：中途失败若留下"票据已登记但没会话"，平台重试会被
        当成重复兑换而永远登不进来；若留下"会话已建但票据没登记"，同一张票据就能
        被反复兑换。两种半成品都不可接受。

        并发由数据库裁决：`(issuer, jti)` 是主键，第二个事务插入冲突 → 抛
        `TicketAlreadyUsed`，不会建出第二个会话。
        """
        stamp = now()
        async with self._lease() as conn:
            try:
                await self._run(
                    conn,
                    'INSERT INTO ticket_redemptions '
                    '(issuer, jti, connection_id, platform_id, tenant_id, '
                    ' expires_at, used_at, auth_session_id) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                    (issuer, jti, connection_id, platform_id, tenant_id,
                     int(expires_at), stamp, session_id))
            except psycopg.errors.UniqueViolation as exc:
                await conn.rollback()
                raise TicketAlreadyUsed(f'票据 {jti} 已被兑换过') from exc

            idp = (identity_provider_id or connection_id).strip()
            row = await self._one(
                conn,
                'SELECT user_id, tenant_id, role FROM identities '
                'WHERE tenant_id=%s AND identity_provider_id=%s AND subject=%s',
                (tenant_id, idp, subject))
            if row is None:
                user_id = 'u_' + uuid.uuid4().hex[:16]
                await self._run(
                    conn,
                    'INSERT INTO app_users '
                    '(user_id, display_name, email, status, created_at, updated_at) '
                    'VALUES (%s, %s, %s, %s, %s, %s) '
                    'ON CONFLICT (user_id) DO NOTHING',
                    (user_id, display_name, email, 'active', stamp, stamp))
                row = await self._one(
                    conn,
                    'INSERT INTO identities '
                    '(provider, subject, user_id, tenant_id, identity_provider_id, '
                    ' email, role, created_at, last_seen_at) '
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id, identity_provider_id, subject) "
                    "  WHERE identity_provider_id <> '' "
                    '  DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at '
                    'RETURNING user_id, tenant_id, role',
                    (idp, subject, user_id, tenant_id, idp, email, 'member',
                     stamp, stamp))
            else:
                await self._run(
                    conn,
                    'UPDATE identities SET last_seen_at=%s '
                    'WHERE tenant_id=%s AND identity_provider_id=%s AND subject=%s',
                    (stamp, tenant_id, idp, subject))

            await self._run(
                conn,
                'INSERT INTO auth_sessions '
                '(auth_session_id, token_digest, user_id, tenant_id, platform_id, '
                ' connection_id, identity_provider_id, subject, issued_at, '
                ' expires_at, last_seen_at, user_agent, remote_addr) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                (session_id, session_digest, row['user_id'], tenant_id,
                 platform_id, connection_id, idp, subject, stamp,
                 float(session_expires_at), stamp, user_agent[:200],
                 remote_addr[:64]))
            return {'user_id': row['user_id'], 'tenant_id': row['tenant_id'],
                    'role': row.get('role') or 'member',
                    'auth_session_id': session_id}

    async def auth_session(self, token_digest: str) -> dict | None:
        """按凭证摘要取中台登录会话（**库里没有明文凭证**）。"""
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'SELECT s.*, COALESCE(i.role, \'member\') AS role FROM auth_sessions s '
                'LEFT JOIN identities i ON i.user_id=s.user_id AND i.tenant_id=s.tenant_id '
                'AND i.identity_provider_id=s.identity_provider_id AND i.subject=s.subject '
                'WHERE s.token_digest=%s', (token_digest,))
            if row is None:
                return None
            await self._run(
                conn,
                'UPDATE auth_sessions SET last_seen_at=%s '
                'WHERE auth_session_id=%s', (now(), row['auth_session_id']))
            return dict(row)

    async def revoke_auth_session(self, auth_session_id: str) -> bool:
        async with self._lease() as conn:
            cur = await self._run(
                conn,
                'UPDATE auth_sessions SET revoked_at=%s '
                'WHERE auth_session_id=%s AND revoked_at IS NULL',
                (now(), auth_session_id))
            return bool(cur.rowcount)

    async def revoke_user_sessions(self, user_id: str) -> int:
        """停用用户时一并踢掉：否则旧会话还能用到过期。"""
        async with self._lease() as conn:
            cur = await self._run(
                conn,
                'UPDATE auth_sessions SET revoked_at=%s '
                'WHERE user_id=%s AND revoked_at IS NULL', (now(), user_id))
            return int(cur.rowcount or 0)

    async def purge_auth_sessions(self, before_ts: float | None = None) -> int:
        """清理过期会话与旧票据记录（保留期内用于排障）。"""
        current = before_ts if before_ts is not None else time.time()
        async with self._lease() as conn:
            cur = await self._run(
                conn,
                'DELETE FROM auth_sessions WHERE expires_at < %s',
                (current - 86400,))
            await self._run(
                conn,
                'DELETE FROM ticket_redemptions WHERE expires_at < %s',
                (int(current) - 86400 * 7,))
            return int(cur.rowcount or 0)

    async def user(self, user_id: str) -> dict | None:
        async with self._lease() as conn:
            return await self._one(
                conn,
                'SELECT user_id, display_name, email, status, created_at '
                'FROM app_users WHERE user_id=%s', (user_id,))

    # ---------- 会话与消息 ----------

    async def ensure_session(self, session_id: str, *, tenant_id: str = 'default',
                       owner_user_id: str = 'default') -> None:
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO sessions '
                '(session_id, tenant_id, owner_user_id, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (session_id, tenant_id, owner_user_id, stamp, stamp))
            await conn.commit()

    async def session(self, session_id: str) -> dict | None:
        """会话归属。接口层用它做对象权限检查（文档 §10）。"""
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM sessions WHERE session_id=%s', (session_id,))
        return dict(row) if row else None

    async def append_message(self, session_id: str, role: str, content: str, *,
                       tenant_id: str = 'default',
                       owner_user_id: str = 'default',
                       dedupe_key: str = '') -> None:
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO messages (session_id, role, content, created_at, dedupe_key) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (session_id, role, content, stamp, dedupe_key or ''))
            await self._run(conn, 'INSERT INTO sessions '
                '(session_id, tenant_id, owner_user_id, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (session_id, tenant_id, owner_user_id, stamp, stamp))
            await self._run(conn, 'UPDATE sessions SET updated_at=%s WHERE session_id=%s', (stamp, session_id))
            await conn.commit()

    async def messages(self, session_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT role, content FROM messages WHERE session_id=%s ORDER BY id',
                (session_id,))
        return [{'role': r['role'], 'content': r['content']} for r in rows]

    async def messages_after(self, session_id: str, after_id: int = 0) -> list[dict]:
        """取 id 大于 after_id 的消息（带 id），供压缩与增量历史使用。"""
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT id, role, content FROM messages '
                'WHERE session_id=%s AND id>%s ORDER BY id',
                (session_id, after_id))
        return [{'id': r['id'], 'role': r['role'], 'content': r['content']}
                for r in rows]

    async def memory(self, session_id: str) -> dict:
        """会话记忆；没有就是空摘要 + 空事实 + upto=0。"""
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT summary, facts, upto FROM session_memory WHERE session_id=%s',
                (session_id,))
        if row is None:
            return {'summary': '', 'facts': {}, 'upto': 0}
        return {'summary': row['summary'],
                'facts': json.loads(row['facts'] or '{}'),
                'upto': row['upto']}

    async def set_memory(self, session_id: str, *, summary: str, facts: dict,
                   upto: int) -> None:
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO session_memory (session_id, summary, facts, upto, updated_at) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT(session_id) DO UPDATE SET '
                'summary=excluded.summary, facts=excluded.facts, '
                'upto=excluded.upto, updated_at=excluded.updated_at',
                (session_id, summary or '',
                 json.dumps(facts or {}, ensure_ascii=False), int(upto), now()))
            await conn.commit()

    async def clear_memory(self, session_id: str) -> None:
        async with self._lease() as conn:
            await self._run(conn, 'DELETE FROM session_memory WHERE session_id=%s',
                               (session_id,))
            await conn.commit()

    async def list_sessions(self, limit: int = 50, *, tenant_id: str | None = None,
                      owner_user_id: str | None = None) -> list[dict]:
        """会话列表（按最近活跃排序），供前端侧边栏展示。

        title 取该会话第一条用户消息（截断），没有就回退到「新会话」。
        传入 tenant_id / owner_user_id 时只返回该主体名下的会话。
        """
        where, params = [], []
        if tenant_id:
            where.append('s.tenant_id=%s')
            params.append(tenant_id)
        if owner_user_id:
            where.append("(s.owner_user_id=%s OR s.owner_user_id='default')")
            params.append(owner_user_id)
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT s.session_id, s.created_at, s.updated_at, '
                '  (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id) '
                '    AS message_count, '
                '  (SELECT content FROM messages m WHERE m.session_id=s.session_id '
                '     AND m.role=\'user\' ORDER BY m.id LIMIT 1) AS first_message '
                f'FROM sessions s{clause} ORDER BY s.updated_at DESC LIMIT %s',
                (*params, limit))
        out = []
        for r in rows:
            first = (r['first_message'] or '').strip()
            title = (first[:24] + '…') if len(first) > 24 else (first or '新会话')
            out.append({'session_id': r['session_id'],
                        'title': title,
                        'message_count': r['message_count'],
                        'created_at': r['created_at'],
                        'updated_at': r['updated_at']})
        return out

    # ---------- 长期记忆（文档 §12） ----------

    async def remember(self, *, scope_type: str, scope_id: str, subject: str,
                 predicate: str, value: str, source: str = '',
                 created_by: str = 'default',
                 tenant_id: str = 'default') -> str | None:
        """写入一条记忆。同键有旧值则升版本并替代旧值。

        返回 None 表示这次写入被墓碑挡住（用户已明确忘记过这个事实）。
        """
        fact_key = f'{subject}|{predicate}'
        async with self._lease() as conn:
            blocked = await self._one(conn, 'SELECT 1 FROM memory_tombstones WHERE scope_type=%s AND '
                'scope_id=%s AND fact_key=%s', (scope_type, scope_id, fact_key))
            if blocked:
                return None
            current = await self._one(conn, 'SELECT memory_id, version FROM memories WHERE scope_type=%s AND '
                'scope_id=%s AND subject=%s AND predicate=%s AND status=%s '
                'ORDER BY version DESC LIMIT 1',
                (scope_type, scope_id, subject, predicate, 'active'))
            memory_id = 'mem_' + uuid.uuid4().hex[:12]
            version = (current['version'] + 1) if current else 1
            stamp = now()
            await self._run(conn, 'INSERT INTO memories (memory_id, tenant_id, scope_type, scope_id, '
                'subject, predicate, value, status, version, supersedes, source, '
                'created_by, created_at, updated_at) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s)",
                (memory_id, tenant_id, scope_type, scope_id, subject, predicate,
                 value, version, current['memory_id'] if current else None,
                 source, created_by, stamp, stamp))
            if current:
                await self._run(conn, "UPDATE memories SET status='superseded', valid_to=%s, "
                    'updated_at=%s WHERE memory_id=%s',
                    (stamp, stamp, current['memory_id']))
            await conn.commit()
        return memory_id

    async def recall(self, scope_type: str, scope_id: str) -> list[dict]:
        """读取该作用域下仍然有效的记忆（被替代/已失效的不返回）。"""
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT * FROM memories WHERE scope_type=%s AND scope_id=%s '
                "AND status='active' ORDER BY updated_at DESC",
                (scope_type, scope_id))
        return [dict(r) for r in rows]

    async def forget(self, *, scope_type: str, scope_id: str, subject: str,
               predicate: str, reason: str = '') -> int:
        """显式遗忘：写墓碑 + 把现有事实置为 forgotten。

        墓碑的作用是阻止后台任务从同一来源再次生成这条事实——
        只删不拦，下一次摘要又会把它捡回来。
        """
        fact_key = f'{subject}|{predicate}'
        stamp = now()
        async with self._lease() as conn:
            cur = await self._run(conn, "UPDATE memories SET status='forgotten', valid_to=%s, updated_at=%s "
                'WHERE scope_type=%s AND scope_id=%s AND subject=%s AND predicate=%s '
                "AND status='active'",
                (stamp, stamp, scope_type, scope_id, subject, predicate))
            changed = cur.rowcount
            await self._run(conn, 'INSERT INTO memory_tombstones '
                '(tombstone_id, scope_type, scope_id, fact_key, reason, created_at) '
                'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                ('tmb_' + uuid.uuid4().hex[:10], scope_type, scope_id, fact_key,
                 reason, stamp))
            await conn.commit()
        return changed

    async def is_forgotten(self, scope_type: str, scope_id: str, subject: str,
                     predicate: str) -> bool:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT 1 FROM memory_tombstones WHERE scope_type=%s AND '
                'scope_id=%s AND fact_key=%s',
                (scope_type, scope_id, f'{subject}|{predicate}'))
        return row is not None
