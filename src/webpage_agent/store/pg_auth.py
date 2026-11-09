"""身份、票据、登录会话、会话消息，以及长期记忆。

`bind_identity` 到 `list_sessions`，再加上 `remember` / `recall` / `forget` /
`is_forgotten`，原样从 `PostgresStore` 移来。方法体没有改。
"""
import json
import time
import uuid

import psycopg

from webpage_agent.store.pg_store import TicketAlreadyUsed, TurnRequestConflict, now

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

    # ---------- 会话 turn（第十一阶段） ----------

    async def create_turn(self, turn_id: str, session_id: str, *,
                          request_id: str, request_hash: str, message: str,
                          target_task_id: str = '', target_run_id: str = '',
                          kind: str = 'chat', tenant_id: str = 'default',
                          owner_user_id: str = 'default',
                          owner_role: str = 'member',
                          source_turn_id: str = '',
                          candidates: list | None = None,
                          clarification: str = '') -> dict:
        """用户消息 + 待执行 turn + 接纳事件同事务提交。

        幂等：`(session_id, request_id)` 唯一。同一 request_id 同一 request_hash
        返回原 turn（replayed=True）；不同 request_hash 抛 `TurnRequestConflict`。
        返回 `{turn_id, message_id, status, replayed, ...}`。

        第十一阶段：`source_turn_id` 关联追问链（歧义追问后下一条回答能继续澄清），
        `candidates`/`clarification` 保存歧义候选关联（控制去重与持久追问）。
        """
        stamp = now()
        async with self._lease() as conn:
            # 会话归属：不存在则创建（归属当前主体），存在则校验归属
            await self._run(conn, 'INSERT INTO sessions '
                '(session_id, tenant_id, owner_user_id, created_at, updated_at) '
                'VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (session_id, tenant_id, owner_user_id, stamp, stamp))
            sess = await self._one(conn, 'SELECT * FROM sessions WHERE session_id=%s FOR UPDATE',
                                   (session_id,))
            if (sess['tenant_id'], sess['owner_user_id']) != (tenant_id, owner_user_id):
                raise PermissionError('会话不属于当前主体')
            # 幂等：同 request_id 已存在
            existing = await self._one(conn,
                'SELECT * FROM session_turns WHERE session_id=%s AND request_id=%s',
                (session_id, request_id))
            if existing:
                if existing['request_hash'] != request_hash:
                    raise TurnRequestConflict(request_id)
                return {'turn_id': existing['turn_id'],
                        'status': existing['status'], 'replayed': True,
                        'message_id': 0, 'kind': existing['kind'],
                        'target_task_id': existing['target_task_id'],
                        'target_run_id': existing['target_run_id'],
                        'turn_seq': int(existing.get('turn_seq') or 0)}
            # 分配 turn_seq（锁住会话后 MAX+1，不用猜）
            seq_row = await self._one(conn,
                'SELECT COALESCE(MAX(turn_seq),0) AS n FROM session_turns WHERE session_id=%s',
                (session_id,))
            turn_seq = int((seq_row or {}).get('n') or 0) + 1
            await self._run(conn, 'INSERT INTO session_turns '
                '(turn_id, session_id, turn_seq, request_id, request_hash, message, '
                ' target_task_id, target_run_id, kind, status, source_turn_id, '
                ' candidates_json, clarification, execution_generation, '
                ' tenant_id, owner_user_id, owner_role, created_at, updated_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (turn_id, session_id, turn_seq, request_id, request_hash, message,
                 target_task_id, target_run_id, kind, 'accepted',
                 source_turn_id or '',
                 json.dumps(candidates or [], ensure_ascii=False),
                 clarification or '', 0, tenant_id, owner_user_id,
                 str(owner_role or 'member'), stamp, stamp))
            # 用户消息（同事务）
            await self._run(conn, 'INSERT INTO messages (session_id, role, content, created_at, dedupe_key) '
                'VALUES (%s,%s,%s,%s,%s)',
                (session_id, 'user', message, stamp, f'turn:{turn_id}'))
            msg_row = await self._one(conn,
                'SELECT id FROM messages WHERE session_id=%s AND dedupe_key=%s',
                (session_id, f'turn:{turn_id}'))
            await self._run(conn, 'UPDATE sessions SET updated_at=%s WHERE session_id=%s',
                            (stamp, session_id))
            await conn.commit()
        return {'turn_id': turn_id, 'message_id': int((msg_row or {}).get('id') or 0),
                'status': 'accepted', 'replayed': False, 'kind': kind,
                'target_task_id': target_task_id, 'target_run_id': target_run_id,
                'turn_seq': turn_seq}

    async def turn(self, turn_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM session_turns WHERE turn_id=%s',
                                  (turn_id,))
        return dict(row) if row else None

    async def chat_history(self, session_id: str, exclude_turn_id: str) -> list[dict]:
        """聊天上下文：当前 turn 之前的 user/assistant 消息（按完成消息水位截断）。

        第十一阶段 A 组第 3 点：只读当前 turn 用户消息**之前**已完成的消息，不读入
        **之后已接纳但尚未执行的用户输入**。以当前 turn 用户消息的 `id` 为上界
        （`dedupe_key = turn:{exclude_turn_id}`），保证同会话按接纳顺序构造上下文；
        不同会话由 `session_id` 天然隔离。
        """
        async with self._lease() as conn:
            rows = await self._all(conn,
                'SELECT role, content FROM messages WHERE session_id=%s '
                'AND id < COALESCE((SELECT MIN(id) FROM messages '
                '   WHERE session_id=%s AND dedupe_key=%s), 9223372036854775807) '
                'ORDER BY id',
                (session_id, session_id, f'turn:{exclude_turn_id}'))
        return [{'role': r['role'], 'content': r['content']} for r in rows
                if r['role'] in ('user', 'assistant')]

    async def turns_of(self, session_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn,
                'SELECT * FROM session_turns WHERE session_id=%s ORDER BY turn_seq',
                (session_id,))
        return [dict(r) for r in rows]

    async def pending_turns(self, limit: int = 50) -> list[dict]:
        """跨会话领取待执行的聊天 turn（不同会话可有界并发）。

        `accepted`（待执行）或 `running` 且租约已过期（执行者失联，重新领取）。
        按 turn_seq 正序，只取未过期的；不区分 session，由调用方做并发额度控制。
        """
        now_ts = time.time()
        async with self._lease() as conn:
            rows = await self._all(conn,
                "SELECT * FROM session_turns WHERE status IN ('accepted','retry') "
                "OR (status='running' AND lease_expires_at > 0 AND lease_expires_at < %s) "
                'ORDER BY created_at LIMIT %s',
                (now_ts, int(limit or 50)))
        return [dict(r) for r in rows]

    async def claim_turn(self, owner: str, ttl: float,
                         *, session_id: str = '') -> dict | None:
        """原子领取一个聊天 turn（第十一阶段第 2 点）。

        同一会话只能有一个**有效执行中**的聊天 turn：领取前先检查该会话是否
        已有未过期的 running turn，有则跳过（保证同会话串行）。不同会话可并发。

        支持过期 running 重新领取：租约已过期的 running turn 视为执行者失联，
        重新领取并递增 `execution_generation`（执行代次，区分 lease_epoch 的
        领取代次）与 `lease_epoch`。`session_id` 给定时只领该会话，否则跨会话
        领一条（有界并发由调用方的额度控制）。
        """
        now_ts = time.time()
        async with self._lease() as conn:
            if session_id:
                await self._run(conn,
                    "SELECT pg_advisory_xact_lock(hashtext('turn:' || %s))",
                    (session_id,))
                # 同会话串行：已有未过期 running turn 时不再领
                active = await self._one(conn,
                    "SELECT 1 FROM session_turns WHERE session_id=%s AND status='running' "
                    'AND lease_expires_at > %s LIMIT 1', (session_id, now_ts))
                if active is not None:
                    return None
                row = await self._one(conn,
                    "SELECT * FROM session_turns WHERE session_id=%s AND "
                    "(status IN ('accepted','retry') OR "
                    " (status='running' AND lease_expires_at>0 AND lease_expires_at<%s)) "
                    'ORDER BY turn_seq LIMIT 1 FOR UPDATE SKIP LOCKED',
                    (session_id, now_ts))
            else:
                # 跨会话领取：按 created_at 正序。同会话串行仍要保证，所以先取
                # 候选列表再逐条尝试（用 advisory lock 保证同会话互斥）。
                candidates = await self._all(conn,
                    "SELECT turn_id, session_id FROM session_turns WHERE "
                    "(status IN ('accepted','retry') OR "
                    " (status='running' AND lease_expires_at>0 AND lease_expires_at<%s)) "
                    'ORDER BY created_at LIMIT 200', (now_ts,))
                row = None
                for cand in candidates:
                    sid = cand['session_id']
                    await self._run(conn,
                        "SELECT pg_advisory_xact_lock(hashtext('turn:' || %s))",
                        (sid,))
                    active = await self._one(conn,
                        "SELECT 1 FROM session_turns WHERE session_id=%s AND status='running' "
                        'AND lease_expires_at > %s LIMIT 1', (sid, now_ts))
                    if active is not None:
                        continue
                    row = await self._one(conn,
                        "SELECT * FROM session_turns WHERE turn_id=%s AND "
                        "(status IN ('accepted','retry') OR "
                        " (status='running' AND lease_expires_at>0 AND lease_expires_at<%s)) "
                        'FOR UPDATE SKIP LOCKED',
                        (cand['turn_id'], now_ts))
                    if row is not None:
                        break
            if row is None:
                return None
            await self._run(conn,
                'UPDATE session_turns SET status=%s, lease_owner=%s, '
                'lease_expires_at=%s, lease_epoch=lease_epoch+1, '
                'execution_generation=execution_generation+1, updated_at=%s '
                'WHERE turn_id=%s',
                ('running', owner, now_ts + ttl, now(), row['turn_id']))
            await conn.commit()
        out = dict(row)
        out['status'] = 'running'
        out['lease_owner'] = owner
        out['lease_epoch'] = int(out.get('lease_epoch') or 0) + 1
        out['execution_generation'] = int(out.get('execution_generation') or 0) + 1
        return out

    async def renew_turn_lease(self, turn_id: str, owner: str, ttl: float,
                               *, lease_epoch: int) -> bool:
        """聊天 turn 长执行续租（第十一阶段第 2 点）。

        返回 False = 已失租（owner/epoch 不符或租约已过期），执行者应停止。
        与 run 的 renew_lease 同口径：epoch 与领取时一致、租约未过期才续。
        """
        current = time.time()
        async with self._lease() as conn:
            cur = await self._run(
                conn, "UPDATE session_turns SET lease_expires_at=%s, updated_at=%s "
                "WHERE turn_id=%s AND lease_owner=%s AND lease_epoch=%s "
                "AND status='running' AND lease_expires_at > %s",
                (current + ttl, now(), turn_id, owner, int(lease_epoch), current))
            await conn.commit()
            return cur.rowcount > 0

    async def settle_turn(self, turn_id: str, *, status: str,
                          answer: str = '', error: str = '',
                          lease_owner: str = '', lease_epoch: int = 0,
                          execution_generation: int = 0,
                          require_running: bool = True) -> bool:
        """结算 turn（第十一阶段第 2/3 点）：原子提交答复 + 终态 + 事件 + 消息。

        带租约门禁（owner + epoch + 代次 + running 状态 + 租约有效期）：过期执行者
        写回无效。**失租返回 False，调用方据此停止，不能再发布成功结果**。

        第十一阶段 A 组第 2 点：结算事务同时检查 owner、epoch、running 状态和租约
        有效期；失租后拒绝答复写入。

        最终 assistant 消息、turn 终态在同一事务提交，用稳定去重键
        `(session_id, turn-answer:{turn_id})` 保证「答复提交前崩溃」「提交后
        响应丢失」均可恢复、不丢答复、不重复答复。
        """
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM session_turns WHERE turn_id=%s FOR UPDATE',
                (turn_id,))
            if row is None:
                return False
            now_ts = time.time()
            if lease_owner:
                # 租约门禁：owner + epoch + 代次 + running 状态 + 租约未过期。
                # 失租（任一不符）→ 不写答复、不写终态。
                cond = ('turn_id=%s AND lease_owner=%s AND lease_epoch=%s '
                        'AND execution_generation=%s')
                params: list = [turn_id, lease_owner, lease_epoch,
                                execution_generation]
                if require_running:
                    cond += " AND status='running' AND lease_expires_at > %s"
                    params.append(now_ts)
                cur = await self._run(conn,
                    'UPDATE session_turns SET status=%s, answer=%s, error=%s, '
                    'lease_expires_at=0, updated_at=%s WHERE ' + cond,
                    (status, answer, error, now(), *params))
                if not cur.rowcount:
                    # 失租（owner/epoch/代次/running/租约任一不符）：不写答复
                    return False
            else:
                await self._run(conn,
                    'UPDATE session_turns SET status=%s, answer=%s, error=%s, '
                    'updated_at=%s WHERE turn_id=%s',
                    (status, answer, error, now(), turn_id))
            # 最终答复消息（同事务、稳定去重键）
            if answer:
                await self._run(conn,
                    'INSERT INTO messages (session_id, role, content, created_at, dedupe_key) '
                    'VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                    (row['session_id'], 'assistant', answer, now(),
                     f'turn-answer:{turn_id}'))
            await self._run(conn,
                'UPDATE sessions SET updated_at=%s WHERE session_id=%s',
                (now(), row['session_id']))
            await conn.commit()
        return True

    async def finish_turn_message(self, session_id: str, content: str,
                                  *, dedupe_key: str = '') -> None:
        """写聊天答复消息（assistant），带去重键。"""
        await self.append_message(session_id, 'assistant', content,
                                  dedupe_key=dedupe_key or '')

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
