"""本地账号（第 4 种登录方式）的持久层。设计见 docs/local-account-auth.md。

只管凭据：密码哈希、失败计数、锁定、最近登录。**账号状态不在这里**——
`app_users.status` 是唯一真相，`principal_from_session()` 会在每次请求时检查，
停用后旧 cookie 立即失效。身份绑定仍然写 `identities`，身份源固定为 `local`，
所以控制台的用户列表 / 活跃度 / 角色 / 停用 / 删除全都自动覆盖本地账号。

一个必须记住的坑：`identities` 主键是 `(provider, subject)`，**跨租户全局唯一**。
一期因此只允许 `tenant_id='default'`，同名用户名在别的租户会撞主键。将来要支持
多租户，得把 subject 编码成 `tenant:username` 并做一次数据迁移，不能只改唯一索引。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

DEFAULT_ALGO = 'scrypt-16384-8-1'


def now() -> str:
    """与 pg_store.now() 同口径（UTC ISO 字符串）。

    这里不 import pg_store：它被 pg_store 作为 mixin 导入，反向依赖会成环。
    """
    return datetime.now(timezone.utc).isoformat()


class PgLocalAccounts:
    """PgLocalAccounts 方法组，挂在 `PostgresStore` 上（见 pg_store.py）。"""

    async def create_local_account(self, *, username: str, password_hash: str,
                                   password_algo: str = DEFAULT_ALGO,
                                   display_name: str = '', role: str = 'member',
                                   tenant_id: str = 'default',
                                   identity_provider_id: str = 'local',
                                   must_change_password: bool = False,
                                   created_by: str = '') -> dict:
        """建号：一个事务里写 app_users + identities + local_accounts。

        注册/管理端建号**不能**走 `login_platform_identity()`——那个方法会顺手插
        一条 `auth_sessions`，建号接口不该发会话。首登并发靠 advisory lock 串行化，
        同一用户名只会有一个账号。
        """
        stamp = now()
        async with self._lease() as conn:
            await self._run(
                conn, 'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                (json.dumps(['local-account', tenant_id, username]),))
            exists = await self._one(
                conn,
                'SELECT 1 AS ok FROM local_accounts '
                'WHERE tenant_id=%s AND username=%s', (tenant_id, username))
            if exists:
                raise FileExistsError('用户名已存在')
            bound = await self._one(
                conn,
                'SELECT 1 AS ok FROM identities WHERE provider=%s '
                'AND subject=%s', (identity_provider_id, username))
            if bound:
                # 账号行被删过、身份绑定残留：不能静默插第二个身份，也不能
                # 直接复用（旧身份可能指向已停用的 user_id），让调用方先清理。
                raise ValueError('该用户名的身份绑定未清理干净')

            user_id = 'u_' + uuid.uuid4().hex[:16]
            await self._run(
                conn,
                'INSERT INTO app_users '
                '(user_id, display_name, email, status, created_at, updated_at) '
                'VALUES (%s,%s,%s,%s,%s,%s)',
                (user_id, display_name or username, '', 'active', stamp, stamp))
            inserted = await self._one(
                conn,
                'INSERT INTO identities '
                '(provider, subject, user_id, tenant_id, identity_provider_id, '
                ' email, role, created_at, last_seen_at) '
                'VALUES (%s,%s,%s,%s,%s,\'\',%s,%s,%s) '
                'ON CONFLICT (provider, subject) DO NOTHING RETURNING user_id',
                (identity_provider_id, username, user_id, tenant_id,
                 identity_provider_id, role, stamp, stamp))
            if inserted is None:
                raise ValueError('该用户名的身份绑定未清理干净')
            await self._run(
                conn,
                'INSERT INTO local_accounts '
                '(username, tenant_id, user_id, password_hash, password_algo, '
                ' password_updated_at, must_change_password, created_at, '
                ' updated_at, created_by) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (username, tenant_id, user_id, password_hash, password_algo,
                 stamp, 1 if must_change_password else 0, stamp, stamp,
                 created_by or ''))
            await self._audit(conn, {
                'type': 'console_local_account_create', 'operator_id': created_by,
                'user_id': user_id, 'username': username,
                'tenant_id': tenant_id, 'role': role,
                'must_change_password': bool(must_change_password)}, stamp)
            await conn.commit()
        return {'user_id': user_id, 'username': username,
                'tenant_id': tenant_id, 'role': role}

    async def local_account_bind_to_user(self, *, user_id: str, username: str,
                                         password_hash: str,
                                         password_algo: str = DEFAULT_ALGO,
                                         tenant_id: str = 'default',
                                         identity_provider_id: str = 'local',
                                         actor_id: str = '',
                                         revoke_sessions: bool = True) -> dict:
        """给一个**已存在**的中台账号补一条本地登录凭据。

        这是「超管对任意用户重置密码」的落点。中台不保管 BeeFTV / 短信 /
        mock-idp 的密码，无法改上游；能做的是给同一个 user_id 开一个由中台
        自己校验的登录方式。因此这里**不新建 app_users**：角色、租户、历史
        会话与数据全部保留，用新密码从「本地账号」入口登进的是同一个人。

        新身份行的 role 取该用户已有身份里的最高角色，否则一个超管换个入口
        登录会悄悄降成 member。tenant_id 必须与登录侧查询用的租户一致，
        不然 `login_platform_identity()` 会另外建一个账号。
        """
        stamp = now()
        async with self._lease() as conn:
            await self._run(
                conn, 'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                (json.dumps(['local-account', tenant_id, username]),))
            user = await self._one(
                conn,
                'SELECT user_id, status FROM app_users '
                'WHERE user_id=%s FOR UPDATE', (user_id,))
            if user is None:
                raise LookupError('用户不存在')
            taken = await self._one(
                conn, 'SELECT user_id FROM local_accounts WHERE username=%s',
                (username,))
            if taken:
                raise FileExistsError('登录账号已被占用')
            bound = await self._one(
                conn,
                'SELECT 1 AS ok FROM identities WHERE provider=%s '
                'AND subject=%s', (identity_provider_id, username))
            if bound:
                raise ValueError('该登录账号的身份绑定未清理干净')

            rows = await self._all(
                conn, 'SELECT role FROM identities WHERE user_id=%s', (user_id,))
            names = {str(r.get('role') or '') for r in rows}
            role = ('platform_admin' if 'platform_admin' in names
                    else 'operator' if 'operator' in names else 'member')
            inserted = await self._one(
                conn,
                'INSERT INTO identities '
                '(provider, subject, user_id, tenant_id, identity_provider_id, '
                ' email, role, created_at, last_seen_at) '
                "VALUES (%s,%s,%s,%s,%s,'',%s,%s,%s) "
                'ON CONFLICT (provider, subject) DO NOTHING RETURNING user_id',
                (identity_provider_id, username, user_id, tenant_id,
                 identity_provider_id, role, stamp, stamp))
            if inserted is None:
                raise ValueError('该登录账号的身份绑定未清理干净')
            await self._run(
                conn,
                'INSERT INTO local_accounts '
                '(username, tenant_id, user_id, password_hash, password_algo, '
                ' password_updated_at, must_change_password, created_at, '
                ' updated_at, created_by) '
                'VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s)',
                (username, tenant_id, user_id, password_hash, password_algo,
                 stamp, stamp, stamp, actor_id or ''))
            revoked = 0
            if revoke_sessions:
                cur = await self._run(
                    conn,
                    'UPDATE auth_sessions SET revoked_at=%s '
                    'WHERE user_id=%s AND revoked_at IS NULL', (stamp, user_id))
                revoked = int(cur.rowcount or 0)
            await self._audit(conn, {
                'type': 'console_local_account_bind', 'operator_id': actor_id,
                'user_id': user_id, 'username': username,
                'tenant_id': tenant_id, 'role': role,
                'revoked_sessions': revoked}, stamp)
            await conn.commit()
        return {'user_id': user_id, 'username': username, 'role': role,
                'created': True, 'revoked_sessions': revoked}

    async def local_account_for_login(self, username: str,
                                      tenant_id: str = 'default') -> dict | None:
        """登录时一次性取齐凭据 + 账号状态。没有账号返回 None。"""
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'SELECT a.username, a.tenant_id, a.user_id, a.password_hash, '
                '       a.password_algo, a.failed_count, a.locked_until, '
                '       a.must_change_password, u.status, u.display_name '
                'FROM local_accounts a JOIN app_users u ON u.user_id=a.user_id '
                'WHERE a.tenant_id=%s AND a.username=%s',
                (tenant_id, username))
        return dict(row) if row else None

    async def local_account_note_failure(self, username: str, *, threshold: int,
                                         seconds: int) -> dict:
        """连续失败计数；到阈值就锁一段时间。成功登录时清零。"""
        stamp, epoch = now(), time.time()
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'UPDATE local_accounts '
                'SET failed_count=failed_count+1, '
                '    locked_until=CASE WHEN failed_count+1 >= %s '
                '                     THEN %s ELSE locked_until END, '
                '    updated_at=%s '
                'WHERE username=%s '
                'RETURNING failed_count, locked_until',
                (threshold, epoch + seconds, stamp, username))
            await conn.commit()
        return dict(row) if row else {}

    async def local_account_note_success(self, username: str) -> None:
        stamp = now()
        async with self._lease() as conn:
            await self._run(
                conn,
                'UPDATE local_accounts SET failed_count=0, locked_until=0, '
                'last_login_at=%s, updated_at=%s WHERE username=%s',
                (stamp, stamp, username))
            await conn.commit()

    async def local_account_set_password(self, *, username: str,
                                         password_hash: str,
                                         password_algo: str = DEFAULT_ALGO,
                                         actor_id: str = '',
                                         revoke_sessions: bool = True) -> bool:
        """改密 / 重置密码。改完把该用户全部会话撤销，旧 cookie 立即失效。"""
        stamp = now()
        async with self._lease() as conn:
            cur = await self._run(
                conn,
                'UPDATE local_accounts SET password_hash=%s, password_algo=%s, '
                'password_updated_at=%s, must_change_password=0, '
                'failed_count=0, locked_until=0, updated_at=%s '
                'WHERE username=%s',
                (password_hash, password_algo, stamp, stamp, username))
            updated = int(cur.rowcount or 0)
            if not updated:
                await conn.rollback()
                return False
            revoked = 0
            if revoke_sessions:
                row = await self._one(
                    conn, 'SELECT user_id FROM local_accounts WHERE username=%s',
                    (username,))
                cur = await self._run(
                    conn,
                    'UPDATE auth_sessions SET revoked_at=%s '
                    'WHERE user_id=%s AND revoked_at IS NULL',
                    (stamp, (row or {}).get('user_id', '')))
                revoked = int(cur.rowcount or 0)
            await self._audit(conn, {
                'type': 'console_local_account_password', 'operator_id': actor_id,
                'username': username, 'revoked_sessions': revoked}, stamp)
            await conn.commit()
        return True

    async def local_account_unlock(self, username: str,
                                   actor_id: str = '') -> bool:
        stamp = now()
        async with self._lease() as conn:
            cur = await self._run(
                conn,
                'UPDATE local_accounts SET failed_count=0, locked_until=0, '
                'updated_at=%s WHERE username=%s', (stamp, username))
            updated = int(cur.rowcount or 0)
            if updated:
                await self._audit(conn, {
                    'type': 'console_local_account_unlock',
                    'operator_id': actor_id, 'username': username}, stamp)
            await conn.commit()
        return bool(updated)

    async def local_account_set_flag(self, username: str, *,
                                     must_change_password: bool) -> bool:
        stamp = now()
        async with self._lease() as conn:
            cur = await self._run(
                conn,
                'UPDATE local_accounts SET must_change_password=%s, '
                'updated_at=%s WHERE username=%s',
                (1 if must_change_password else 0, stamp, username))
            await conn.commit()
        return bool(cur.rowcount or 0)

    async def local_account_list(self, *, query: str = '', limit: int = 100,
                                 offset: int = 0) -> dict:
        """管理端列表。绝不返回 password_hash。"""
        like = '%' + query.strip().lower() + '%'
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                'SELECT a.username, a.tenant_id, a.user_id, a.created_at, '
                '       a.last_login_at, a.locked_until, a.failed_count, '
                '       a.must_change_password, u.status, u.display_name '
                'FROM local_accounts a JOIN app_users u ON u.user_id=a.user_id '
                'WHERE a.username LIKE %s OR lower(u.display_name) LIKE %s '
                'ORDER BY a.created_at DESC LIMIT %s OFFSET %s',
                (like, like, max(1, min(limit, 500)), max(0, offset)))
            total = await self._one(
                conn, 'SELECT count(*) AS n FROM local_accounts')
        return {'accounts': [dict(r) for r in rows],
                'total': int((total or {}).get('n') or 0)}

    async def local_account_by_user(self, user_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'SELECT username, tenant_id, user_id FROM local_accounts '
                'WHERE user_id=%s', (user_id,))
        return dict(row) if row else None

    async def local_account_delete_for_user(self, conn, user_id: str) -> int:
        """被 purge 调用；在调用方的事务里执行，不自己 commit。"""
        cur = await self._run(
            conn, 'DELETE FROM local_accounts WHERE user_id=%s', (user_id,))
        return int(cur.rowcount or 0)

    async def local_account_count(self) -> int:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT count(*) AS n FROM local_accounts')
        return int((row or {}).get('n') or 0)
