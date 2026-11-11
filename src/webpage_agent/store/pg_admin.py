"""管理控制台的读模型：跨租户的用户、活跃度、审计。

这里的查询**故意不走 `policies.ensure_owner`**：控制台的语义就是"一个人看全部"。
所以这一层的安全性完全由调用方（`webpage_agent/api/console_api.py`）的准入检查
承担——任何新增方法都必须假设它返回的是全量数据，不要在这里拼租户过滤来自我安慰。

四条约定：

1. **只读为主**。写操作只有 `set_user_role` 与 `set_policy_enabled`，前者自带锁与
   "最后一个超管"保护；
2. **活跃度按 `sessions.owner_user_id` 归集**，不按 `tasks_meta`。后者虽有
   `owner_user_id` 列，但只有部分链路会写（存量库里大量任务没有对应行），用它算
   会把活跃度算少。`provider_charges` 只有 `run_id`，要按人算必须 join 回
   `runs → tasks → sessions`——这条链最贵，所以只对**当页**用户算；
3. **时间按 UTC 日期切**。库里存的是 `datetime.now(timezone.utc).isoformat()`，
   按天聚合直接 `substr(..., 1, 10)`，不必转型成 date 再截断；
4. **审计读 `events` 表里 `task_id` 以 `audit_` 开头的行**。没有独立审计表是有意的：
   `events` 已经有保留期清理（`retention_events_days`），再建一张只会变成没人清理的
   隐私负担。代价是查询要按前缀过滤，`LIKE 'audit\\_%'` 里的反斜杠别丢——`_` 是
   通配符，不转义会把 `auditX...` 也匹配进来。
"""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from webpage_agent.store.pg_store import now

# 角色强弱。展示用"这个人最高是什么"，而不是逐租户罗列。
_ROLE_RANK = {'member': 1, 'operator': 2, 'platform_admin': 3}
# 可**授予**的角色只有两档：普通用户与超管。`operator`（本企业运维）是遗留角色，
# 只为了不把存量账号锁在门外才继续识别，控制台不再授予它。
ROLES = ('member', 'platform_admin')
LEGACY_ROLES = tuple(_ROLE_RANK)
# 算"失败"的任务终态。成功与进行中的不算。
_FAILED_TASK_STATES = ('failed', 'cancelled', 'blocked', 'interrupted', 'error')


def _top_role(roles) -> str:
    best = 'member'
    for role in roles:
        if _ROLE_RANK.get(role, 0) > _ROLE_RANK.get(best, 0):
            best = role
    return best


def _utc_day(back: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0, back))).strftime('%Y-%m-%d')


def _login_iso(value) -> str:
    """把 `auth_sessions.last_seen_at` 统一成 ISO 文本。

    这一列是 TEXT，写进去的是 `datetime.now(...).isoformat()`；但早期版本塞过 epoch
    秒。前端 `new Date()` 两种都吃，可"3 秒前"和"1970 年"差得离谱，所以在这里按
    "先当 ISO 解析、失败再当 epoch"的顺序抹平，解析不了的当没有。
    """
    if value is None:
        return ''
    text = str(value).strip()
    if not text:
        return ''
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        pass
    try:
        seconds = float(text)
    except ValueError:
        return ''
    if seconds <= 0:
        return ''
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _json_field_like(field: str, value: str) -> str:
    """构造匹配 `{"field": "value"` 的 LIKE 模式。

    `_audit()` 用 `json.dumps` 的默认分隔符，所以键值之间是 `": "`（带空格）。
    三个字符必须转义：`\\` 是 LIKE 的转义符本身，`%` `_` 是通配符——`user_id`
    长得就像 `u_2f6e...`，不转义 `_` 会把任意单字符都匹配上。
    """
    escaped = (value.replace('\\', '\\\\')
               .replace('%', '\\%')
               .replace('_', '\\_'))
    return f'%"{field}": "{escaped}"%'


def _event_row(row) -> dict:
    """把 `events` 行摊平成审计条目。payload 坏了也不能让整页 500。"""
    item = dict(row)
    try:
        payload = json.loads(item.pop('payload') or '{}')
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {'value': payload}
    return {'id': item.get('task_id'), 'created_at': item.get('created_at'), **payload}


class PgAdmin:
    """PgAdmin 方法组。见模块说明。"""

    # ---------- 用户 ----------

    async def console_users(self, *, tenant_id: str = '', query: str = '',
                            status: str = '', role: str = '',
                            limit: int = 50, offset: int = 0) -> dict:
        """跨租户用户列表 + 当页活跃度。

        顺序是"先筛人、再聚合"，不是反过来：反着写会让 `messages` 与
        `provider_charges` 的 join 在用户增长后变成全表扫。
        """
        where, params = [], []
        if tenant_id:
            where.append('i.tenant_id=%s')
            params.append(tenant_id)
        if status:
            where.append('u.status=%s')
            params.append(status)
        if role:
            where.append('i.role=%s')
            params.append(role)
        if query:
            where.append('(u.user_id ILIKE %s OR u.display_name ILIKE %s '
                         'OR u.email ILIKE %s)')
            like = f'%{query}%'
            params.extend([like, like, like])
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))

        async with self._lease() as conn:
            total_row = await self._one(
                conn,
                'SELECT count(DISTINCT u.user_id) AS n FROM app_users u '
                f'JOIN identities i ON i.user_id=u.user_id {clause}', tuple(params))
            rows = await self._all(
                conn,
                'SELECT DISTINCT u.user_id, u.display_name, u.email, u.status, '
                f'u.created_at FROM app_users u '
                f'JOIN identities i ON i.user_id=u.user_id {clause} '
                'ORDER BY u.user_id LIMIT %s OFFSET %s',
                tuple(params) + (limit, offset))
            users = [dict(row) for row in rows]
            ids = [row['user_id'] for row in users]
            if not ids:
                return {'total': int((total_row or {}).get('n') or 0), 'users': []}
            roles = await self._all(
                conn,
                'SELECT user_id, tenant_id, role, provider, identity_provider_id '
                'FROM identities WHERE user_id = ANY(%s)',
                (ids,))
            activity = await self._activity_for(conn, ids)

        scope: dict[str, dict] = {}
        for row in roles:
            item = scope.setdefault(
                row['user_id'], {'tenants': [], 'roles': [], 'platforms': []})
            if row['tenant_id'] not in item['tenants']:
                item['tenants'].append(row['tenant_id'])
            item['roles'].append(row['role'])
            # "子平台"就是身份源：beeftv / mock-idp-a / mock-idp-b 这些。
            # 优先取 identity_provider_id（绑定了具体 IdP 记录时才有），退回 provider。
            platform = row['identity_provider_id'] or row['provider']
            if platform and platform not in item['platforms']:
                item['platforms'].append(platform)

        for user in users:
            meta = scope.get(user['user_id'],
                             {'tenants': [], 'roles': [], 'platforms': []})
            stats = activity.get(user['user_id'], {})
            user['tenants'] = sorted(meta['tenants'])
            user['platforms'] = sorted(meta['platforms'])
            user['role'] = _top_role(meta['roles'])
            # 「最近活跃」优先会话时间；只登录过、没开过会话的人退回登录时间，
            # 否则列表里会是一片「—」，看起来像这个账号从没被人用过。
            chat_at = stats.get('last_active_at') or ''
            login_at = stats.get('last_login_at') or ''
            user['last_active_at'] = chat_at or login_at
            user['last_login_at'] = login_at
            user['last_active_source'] = 'chat' if chat_at else ('login' if login_at else '')
            for field in ('sessions', 'messages', 'tasks', 'failed_tasks',
                          'input_tokens', 'output_tokens', 'charged_micros'):
                user[field] = int(stats.get(field) or 0)
        return {'total': int((total_row or {}).get('n') or 0), 'users': users}

    async def _activity_for(self, conn, ids: list[str]) -> dict[str, dict]:
        """一批用户的会话 / 消息 / 任务 / token / 花费。四条 SQL，按 user_id 合并。"""
        out: dict[str, dict] = {uid: {} for uid in ids}

        for row in await self._all(
                conn,
                'SELECT owner_user_id AS user_id, count(*) AS sessions, '
                '       max(updated_at) AS last_active_at '
                'FROM sessions WHERE owner_user_id = ANY(%s) GROUP BY 1', (ids,)):
            out.setdefault(row['user_id'], {}).update(
                sessions=int(row['sessions'] or 0),
                last_active_at=row['last_active_at'] or '')
        for row in await self._all(
                conn,
                'SELECT user_id, max(last_seen_at) AS last_seen FROM auth_sessions '
                'WHERE user_id = ANY(%s) GROUP BY 1', (ids,)):
            out.setdefault(row['user_id'], {})['last_login_at'] = _login_iso(
                row['last_seen'])
        for row in await self._all(
                conn,
                'SELECT s.owner_user_id AS user_id, count(m.id) AS messages '
                'FROM sessions s JOIN messages m ON m.session_id=s.session_id '
                'WHERE s.owner_user_id = ANY(%s) GROUP BY 1', (ids,)):
            out.setdefault(row['user_id'], {})['messages'] = int(row['messages'] or 0)
        for row in await self._all(
                conn,
                'SELECT s.owner_user_id AS user_id, count(*) AS tasks, '
                '       count(*) FILTER (WHERE t.status = ANY(%s)) AS failed_tasks '
                'FROM sessions s JOIN tasks t ON t.session_id=s.session_id '
                'WHERE s.owner_user_id = ANY(%s) GROUP BY 1',
                (list(_FAILED_TASK_STATES), ids)):
            out.setdefault(row['user_id'], {}).update(
                tasks=int(row['tasks'] or 0),
                failed_tasks=int(row['failed_tasks'] or 0))
        for row in await self._all(
                conn,
                'SELECT s.owner_user_id AS user_id, '
                '       COALESCE(sum(p.input_tokens), 0) AS input_tokens, '
                '       COALESCE(sum(p.output_tokens), 0) AS output_tokens, '
                '       COALESCE(sum(p.charged_micros), 0) AS charged_micros '
                'FROM sessions s JOIN tasks t ON t.session_id=s.session_id '
                'JOIN runs r ON r.task_id=t.task_id '
                'JOIN provider_charges p ON p.run_id=r.run_id '
                'WHERE s.owner_user_id = ANY(%s) GROUP BY 1', (ids,)):
            out.setdefault(row['user_id'], {}).update(
                input_tokens=int(row['input_tokens'] or 0),
                output_tokens=int(row['output_tokens'] or 0),
                charged_micros=int(row['charged_micros'] or 0))
        return out

    async def console_user(self, user_id: str, *, days: int = 14) -> dict | None:
        """单个用户的档案：绑定身份、活跃度曲线、会话、最近任务、本人相关审计。

        `days` 只影响活跃度曲线，其余是"最近 N 条"的定长截取。
        """
        days = max(1, min(90, int(days)))
        start = _utc_day(days - 1)
        async with self._lease() as conn:
            user = await self._one(
                conn,
                'SELECT user_id, display_name, email, status, created_at, updated_at '
                'FROM app_users WHERE user_id=%s', (user_id,))
            if user is None:
                return None
            identities = await self._all(
                conn,
                'SELECT tenant_id, identity_provider_id, provider, subject, email, '
                '       role, created_at, last_seen_at FROM identities '
                'WHERE user_id=%s ORDER BY last_seen_at DESC', (user_id,))
            sessions = await self._all(
                conn,
                'SELECT session_id, tenant_id, created_at, updated_at '
                'FROM sessions WHERE owner_user_id=%s ORDER BY updated_at DESC LIMIT 20',
                (user_id,))
            tasks = await self._all(
                conn,
                'SELECT t.task_id, t.session_id, t.status, t.created_at, '
                '       substr(t.message, 1, 120) AS message '
                'FROM sessions s JOIN tasks t ON t.session_id=s.session_id '
                'WHERE s.owner_user_id=%s ORDER BY t.created_at DESC LIMIT 20', (user_id,))
            live = await self._one(
                conn,
                'SELECT count(*) AS n FROM auth_sessions '
                'WHERE user_id=%s AND revoked_at IS NULL AND expires_at>%s',
                (user_id, time.time()))
            last_login = await self._one(
                conn,
                'SELECT max(last_seen_at) AS last_seen FROM auth_sessions WHERE user_id=%s',
                (user_id,))
            message_days = await self._all(
                conn,
                'SELECT substr(m.created_at,1,10) AS day, count(*) AS messages '
                'FROM messages m JOIN sessions s ON s.session_id=m.session_id '
                'WHERE s.owner_user_id=%s AND substr(m.created_at,1,10) >= %s GROUP BY 1',
                (user_id, start))
            task_days = await self._all(
                conn,
                'SELECT substr(t.created_at,1,10) AS day, count(*) AS tasks '
                'FROM tasks t JOIN sessions s ON s.session_id=t.session_id '
                'WHERE s.owner_user_id=%s AND substr(t.created_at,1,10) >= %s GROUP BY 1',
                (user_id, start))
            activity = await self._all(
                conn,
                'SELECT task_id, payload, created_at FROM events '
                'WHERE task_id LIKE %s AND payload LIKE %s '
                'ORDER BY created_at DESC LIMIT 50',
                ('audit\\_%', _json_field_like('user_id', user_id)))
        return {'user': dict(user),
                'identities': [dict(row) for row in identities],
                'sessions': [dict(row) for row in sessions],
                'tasks': [dict(row) for row in tasks],
                'active_logins': int((live or {}).get('n') or 0),
                'last_login_at': _login_iso((last_login or {}).get('last_seen')),
                'activity': self._daily_series(start, days, message_days, task_days),
                'audit': [_event_row(row) for row in activity]}

    @staticmethod
    def _daily_series(start: str, days: int, message_days, task_days) -> list[dict]:
        """把按天分组的碎片补成连续日期，前端直接画，不用自己造空格子。"""
        first = datetime.strptime(start, '%Y-%m-%d')
        series = [{'day': (first + timedelta(days=index)).strftime('%Y-%m-%d'),
                   'messages': 0, 'tasks': 0} for index in range(days)]
        by_day = {row['day']: row for row in series}
        for rows, field in ((message_days, 'messages'), (task_days, 'tasks')):
            for row in rows:
                target = by_day.get(row['day'])
                if target is not None:
                    target[field] = int(row[field] or 0)
        return series

    # ---------- 概览 ----------

    async def console_overview(self, *, days: int = 14) -> dict:
        """活跃度概览：总量、按天趋势、租户分布。

        `days` 只影响趋势曲线，总量永远是"现在这一刻"的快照。
        趋势数组**按日期补齐空格子**，前端不用自己造 0。
        """
        days = max(1, min(90, int(days)))
        start = _utc_day(days - 1)
        async with self._lease() as conn:
            users = await self._one(
                conn,
                'SELECT count(*) AS total, '
                "       count(*) FILTER (WHERE status='active') AS active, "
                "       count(*) FILTER (WHERE status<>'active') AS disabled, "
                '       count(*) FILTER (WHERE substr(created_at,1,10) >= %s) AS new_recent '
                'FROM app_users', (start,))
            tenants = await self._all(
                conn,
                'SELECT tenant_id, count(DISTINCT user_id) AS users '
                'FROM identities GROUP BY 1 ORDER BY 2 DESC', ())
            session_days = await self._all(
                conn,
                # join `app_users`：删掉的账号其历史会话仍在库里（见
                # `console_delete_user`），不 join 就会把它们算进"今日活跃"，
                # 用户列表和活跃数字对不上。
                'SELECT substr(s.updated_at,1,10) AS day, '
                '       count(DISTINCT s.owner_user_id) AS active_users, '
                '       count(DISTINCT s.session_id) AS sessions '
                'FROM sessions s JOIN app_users u ON u.user_id=s.owner_user_id '
                'WHERE substr(s.updated_at,1,10) >= %s GROUP BY 1', (start,))
            message_days = await self._all(
                conn,
                'SELECT substr(created_at,1,10) AS day, count(*) AS messages '
                'FROM messages WHERE substr(created_at,1,10) >= %s GROUP BY 1', (start,))
            task_days = await self._all(
                conn,
                'SELECT substr(created_at,1,10) AS day, count(*) AS tasks, '
                '       count(*) FILTER (WHERE status = ANY(%s)) AS failed '
                'FROM tasks WHERE substr(created_at,1,10) >= %s GROUP BY 1',
                (list(_FAILED_TASK_STATES), start))
            charge_days = await self._all(
                conn,
                'SELECT day, count(*) AS requests, '
                '       COALESCE(sum(charged_micros),0) AS charged_micros, '
                '       COALESCE(sum(input_tokens),0) AS input_tokens, '
                '       COALESCE(sum(output_tokens),0) AS output_tokens '
                'FROM provider_charges WHERE day >= %s GROUP BY 1', (start,))

        first = datetime.strptime(start, '%Y-%m-%d')
        series = []
        for index in range(days):
            day = (first + timedelta(days=index)).strftime('%Y-%m-%d')
            series.append({'day': day, 'active_users': 0, 'sessions': 0,
                           'messages': 0, 'tasks': 0, 'failed': 0,
                           'requests': 0, 'charged_micros': 0})
        by_day = {row['day']: row for row in series}

        def take(rows, *fields):
            for row in rows:
                target = by_day.get(row['day'])
                if target is None:
                    continue
                for field in fields:
                    target[field] = int(row[field] or 0)

        take(session_days, 'active_users', 'sessions')
        take(message_days, 'messages')
        take(task_days, 'tasks', 'failed')
        take(charge_days, 'requests', 'charged_micros')

        return {'totals': dict(users or {}),
                'tenants': [dict(row) for row in tenants],
                'days': days,
                'trend': series}

    # ---------- 连接器与技能 ----------

    async def console_connectors(self) -> dict:
        """全部租户的服务端绑定，按租户分组。

        `config_ref` 可能是凭据引用，**不返回**；页面只需要"接了没有、开着没有、
        有没有配钥匙"。
        """
        async with self._lease() as conn:
            bindings = await self._all(
                conn,
                'SELECT binding_id, tenant_id, server_id, transport, enabled, '
                '       revision FROM mcp_server_bindings '
                'ORDER BY tenant_id, server_id', ())
            credentialed = await self._all(
                conn,
                'SELECT tenant_id, server_binding_id FROM credential_bindings '
                "WHERE status='active'", ())
        keys = {(row['tenant_id'], row['server_binding_id']) for row in credentialed}
        by_tenant: dict[str, list] = {}
        for row in bindings:
            by_tenant.setdefault(row['tenant_id'], []).append({
                'server_id': row['server_id'],
                'transport': row['transport'],
                'enabled': bool(row['enabled']),
                'revision': int(row['revision'] or 0),
                'credentialed': (row['tenant_id'], row['binding_id']) in keys,
            })
        return {'tenants': [{'tenant_id': key, 'bindings': value}
                            for key, value in sorted(by_tenant.items())],
                'total': len(bindings)}

    async def console_policies(self) -> dict:
        """全部租户的工具政策，按租户分组。"""
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                'SELECT policy_id, tenant_id, tool_key, role, user_id, effect, '
                '       approval_mode, enabled, revision FROM tool_policies '
                'ORDER BY tenant_id, tool_key', ())
        by_tenant: dict[str, list] = {}
        for row in rows:
            by_tenant.setdefault(row['tenant_id'], []).append({
                'policy_id': row['policy_id'],
                'tool_key': row['tool_key'],
                'role': row['role'] or '',
                'user_id': row['user_id'] or '',
                'effect': row['effect'] or 'unknown',
                'approval_mode': row['approval_mode'] or 'auto',
                'enabled': bool(row['enabled']),
            })
        return {'tenants': [{'tenant_id': key, 'policies': value}
                            for key, value in sorted(by_tenant.items())],
                'total': len(rows)}

    async def set_policy_enabled(self, *, policy_id: str, enabled: bool,
                                 actor_id: str = '') -> dict:
        """开关一条工具政策。

        控制台上唯一允许直接改的治理项：新增绑定要凭据、要定作用域，
        那是运维脚本的活；"暂停某个工具"才是纯管理动作。
        """
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'UPDATE tool_policies SET enabled=%s, revision=revision+1 '
                'WHERE policy_id=%s '
                'RETURNING policy_id, tenant_id, tool_key, enabled, revision',
                (1 if enabled else 0, policy_id))
            if row is None:
                raise LookupError('政策不存在')
            await self._audit(conn, {
                'type': 'console_policy_toggle', 'operator_id': actor_id,
                'policy_id': policy_id, 'tenant_id': row['tenant_id'],
                'tool_key': row['tool_key'], 'enabled': bool(enabled)}, stamp)
        return dict(row)

    # ---------- 角色 ----------

    async def set_user_role(self, *, user_id: str, role: str, tenant_id: str = '',
                            actor_id: str = '', reason: str = '') -> dict:
        """授予 / 回收角色。`tenant_id` 为空表示改这个用户的**全部**身份。

        两个保护写在同一把 advisory 锁里，因为它们都是全表不变量：

        - **不能把最后一个 `platform_admin` 降级**，否则控制台永久锁死、只能改库；
        - **不能降级自己**，避免管理员手滑把自己踢出去后要找运维。

        角色是**每次请求现读** `identities` 的（见 `pg_auth.auth_session` 的 join），
        所以改完立即生效，不用撤销会话。
        """
        if role not in ROLES:
            raise ValueError(f'未知角色：{role}')
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                            ('console-role',))
            if not await self._one(conn, 'SELECT 1 FROM app_users WHERE user_id=%s',
                                   (user_id,)):
                raise LookupError('用户不存在')
            scope_sql = 'user_id=%s AND tenant_id=%s' if tenant_id else 'user_id=%s'
            args = (user_id, tenant_id) if tenant_id else (user_id,)
            previous = await self._all(
                conn,
                f'SELECT tenant_id, role FROM identities WHERE {scope_sql} '
                'ORDER BY tenant_id', args)
            if not previous:
                raise LookupError('用户还没绑定身份，先让对方登录一次')
            if role != 'platform_admin' and user_id == actor_id:
                raise ValueError('不能降级自己，请让另一位管理员操作')
            updated = await self._run(
                conn, f'UPDATE identities SET role=%s WHERE {scope_sql}',
                (role,) + args)
            if role != 'platform_admin':
                left = await self._one(
                    conn,
                    "SELECT count(*) AS n FROM identities WHERE role='platform_admin'", ())
                if int((left or {}).get('n') or 0) == 0:
                    raise ValueError('至少要保留一个 platform_admin，否则没人能再进控制台')
            await self._audit(conn, {
                'type': 'console_user_role', 'operator_id': actor_id,
                'user_id': user_id, 'tenant_id': tenant_id or '*',
                'previous': [{'tenant_id': row['tenant_id'], 'role': row['role']}
                             for row in previous],
                'role': role, 'reason': reason}, stamp)
        return {'user_id': user_id, 'role': role, 'tenant_id': tenant_id or '*',
                'updated': int(updated.rowcount or 0)}

    async def console_set_user_status(self, *, user_id: str, status: str,
                                      actor_id: str = '') -> dict:
        """停用 / 启用一个账号（全局，不分租户）。

        和 `pg_auth.set_tenant_user_status` 的区别：那个是**租户运维**视角，会
        拒绝"用户还关联着别的租户"的情况；这里是**平台超管**视角，停用就是全平台
        停用，所以不设那道限制，但必须由 `console_access` 判成 `all` 的调用方进来。

        停用与踢会话在同一个事务里：否则旧 cookie 还能用到会话自然过期。
        """
        if status not in ('active', 'disabled'):
            raise ValueError('状态只能是 active 或 disabled')
        if status == 'disabled' and user_id == actor_id:
            raise ValueError('不能停用自己')
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                            (f'console-status:{user_id}',))
            current = await self._one(
                conn, 'SELECT status FROM app_users WHERE user_id=%s FOR UPDATE',
                (user_id,))
            if current is None:
                raise LookupError('用户不存在')
            await self._run(
                conn, 'UPDATE app_users SET status=%s, updated_at=%s WHERE user_id=%s',
                (status, stamp, user_id))
            revoked = 0
            if status == 'disabled':
                cur = await self._run(
                    conn,
                    'UPDATE auth_sessions SET revoked_at=%s '
                    'WHERE user_id=%s AND revoked_at IS NULL', (stamp, user_id))
                revoked = int(cur.rowcount or 0)
            await self._audit(conn, {
                'type': 'console_user_status', 'operator_id': actor_id,
                'user_id': user_id, 'previous_status': current['status'],
                'status': status, 'revoked_sessions': revoked}, stamp)
        return {'user_id': user_id, 'status': status, 'revoked_sessions': revoked}

    async def console_delete_user(self, *, user_id: str, actor_id: str = '',
                                  reason: str = '') -> dict:
        """彻底删除一个账号。

        删的是**账号本身**：`auth_sessions`（凭证）、`identities`（身份绑定）、
        `app_users`（账号行）。历史会话 / 消息 / 任务**保留**——它们是使用记录，
        删掉等于销毁审计证据，而"删账号"要解决的是"这个人不能再登录"。

        代价说清楚：删完之后 `sessions.owner_user_id` 指向一个不存在的用户。
        用户列表不再出现他，概览的活跃用户数也已 join `app_users` 过滤，
        所以数字仍然自洽；但这些历史数据无法再归属到具体的人。

        三条保护与 `set_user_role` 同一把 advisory 锁：
        不能删自己、不能删最后一个超管、账号必须存在。
        """
        if user_id == actor_id:
            raise ValueError('不能删除自己')
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                            ('console-role',))
            user = await self._one(
                conn,
                'SELECT user_id, display_name, email, status FROM app_users '
                'WHERE user_id=%s FOR UPDATE', (user_id,))
            if user is None:
                raise LookupError('用户不存在')
            identities = await self._all(
                conn,
                'SELECT tenant_id, role, provider, identity_provider_id, subject '
                'FROM identities WHERE user_id=%s', (user_id,))
            if any(row['role'] == 'platform_admin' for row in identities):
                left = await self._one(
                    conn,
                    "SELECT count(DISTINCT user_id) AS n FROM identities "
                    "WHERE role='platform_admin' AND user_id<>%s", (user_id,))
                if int((left or {}).get('n') or 0) == 0:
                    raise ValueError('这是最后一个超管，删掉就没人能再进控制台了')
            stats = await self._one(
                conn,
                'SELECT (SELECT count(*) FROM sessions WHERE owner_user_id=%s) AS sessions, '
                '       (SELECT count(*) FROM messages m JOIN sessions s '
                '          ON s.session_id=m.session_id WHERE s.owner_user_id=%s) AS messages, '
                '       (SELECT count(*) FROM tasks t JOIN sessions s '
                '          ON s.session_id=t.session_id WHERE s.owner_user_id=%s) AS tasks, '
                '       (SELECT count(*) FROM auth_sessions '
                '          WHERE user_id=%s AND revoked_at IS NULL) AS live_sessions',
                (user_id, user_id, user_id, user_id))
            await self._run(conn, 'DELETE FROM auth_sessions WHERE user_id=%s', (user_id,))
            await self._run(conn, 'DELETE FROM identities WHERE user_id=%s', (user_id,))
            await self._run(conn, 'DELETE FROM app_users WHERE user_id=%s', (user_id,))
            await self._audit(conn, {
                'type': 'console_user_delete', 'operator_id': actor_id,
                'user_id': user_id,
                'display_name': user['display_name'] or '',
                'email': user['email'] or '',
                'previous_status': user['status'],
                'identities': [{'tenant_id': row['tenant_id'], 'role': row['role'],
                                'platform': row['identity_provider_id'] or row['provider'],
                                'subject': row['subject']} for row in identities],
                'retained': {key: int((stats or {}).get(key) or 0)
                             for key in ('sessions', 'messages', 'tasks')},
                'revoked_sessions': int((stats or {}).get('live_sessions') or 0),
                'reason': reason}, stamp)
        return {'user_id': user_id, 'deleted': True,
                'identities': len(identities),
                'revoked_sessions': int((stats or {}).get('live_sessions') or 0),
                'retained': {key: int((stats or {}).get(key) or 0)
                             for key in ('sessions', 'messages', 'tasks')}}

    # ---------- 审计 ----------

    async def console_audit(self, *, limit: int = 100, kind: str = '',
                            user_id: str = '') -> list[dict]:
        """管理动作审计，最新的在前。写入方是 `pg_auth.set_tenant_user_status`
        与本模块（见模块说明第 4 条）。

        `user_id` 用来筛"针对这个人的动作"，供用户详情页用；已删除账号的审计
        仍然查得到，因为它只是 payload 里的一个字符串。
        """
        limit = max(1, min(500, int(limit)))
        # 前缀写成**参数**而不是字面量：psycopg 只认 %s 占位符，SQL 里出现裸的
        # `%` 会被当成占位符开头直接报错（`LIKE 'audit\_%'` 就是这么炸的）。
        sql = 'SELECT task_id, payload, created_at FROM events WHERE task_id LIKE %s'
        params: list = ['audit\\_%']
        if kind:
            sql += ' AND payload LIKE %s'
            params.append(f'%"type": "{kind}"%')
        if user_id:
            sql += ' AND payload LIKE %s'
            params.append(_json_field_like('user_id', user_id))
        sql += ' ORDER BY created_at DESC, task_id DESC LIMIT %s'
        params.append(limit)
        async with self._lease() as conn:
            rows = await self._all(conn, sql, tuple(params))
        return [_event_row(row) for row in rows]

    async def _audit(self, conn, payload: dict, stamp: str) -> None:
        await self._run(
            conn,
            'INSERT INTO events (task_id, seq, payload, created_at) VALUES (%s,%s,%s,%s)',
            ('audit_' + uuid.uuid4().hex, 1,
             json.dumps(payload, ensure_ascii=False, default=str), stamp))
