"""彻底删除一个用户的所有信息（purge）。设计见 docs/local-account-auth.md §16。

与旧的 `console_delete_user()` 的区别：旧实现只删 app_users / identities /
auth_sessions 三张表，刻意保留历史会话与消息当审计证据。现在需求是「删除用户
就是删除他的所有信息」，所以这里按 会话 → 任务 → run → 动作/产物 → 归属表 →
身份表 的顺序，在一个事务里清空。

两条硬规则：
1. 每条 DELETE 都必须带 user_id 或收集到的 ID 条件，禁止 TRUNCATE、禁止
   按 tenant_id 删——那会连坐同租户的其他人。
2. 只保留一条 `console_user_purge` 审计事件，且里面的 user_id 存哈希前 16 位，
   不含昵称/邮箱/subject/消息正文。既留追责线索，又不留可反查到人的数据。

文件（工作目录、产物、OAuth 凭据）不能在 DB 事务里回滚，所以放在 COMMIT 之后删，
失败只告警并把路径写进审计，供人工补齐。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def now() -> str:
    """与 pg_store.now() 同口径；不反向 import pg_store，避免循环。"""
    return datetime.now(timezone.utc).isoformat()

# 按用户 ID 直接删除的表。列名都来自活库 information_schema，不是用户输入。
_USER_TABLES: tuple[tuple[str, str], ...] = (
    ('user_workspaces', 'owner_user_id'),
    ('request_keys', 'owner_user_id'),
    ('tool_policies', 'user_id'),
    ('tool_authorizations', 'user_id'),
    ('tool_result_manifests', 'owner_user_id'),
    ('tasks_meta', 'owner_user_id'),
    ('external_tasks', 'owner_user_id'),
    ('session_turns', 'owner_user_id'),
    ('scheduled_task_runs', 'owner_user_id'),
    ('scheduled_tasks', 'owner_user_id'),
    ('experts', 'owner_user_id'),
    # 机器人（docs/bot-mode.md）：身份、分区、投递收据、房间都挂 owner_user_id。
    # 房间的子表（事件 / 每步任务）没有 owner 列，在 `_delete_owned` 里按房间先删。
    ('bots', 'owner_user_id'),
    ('bot_sections', 'owner_user_id'),
    ('bot_deliveries', 'owner_user_id'),
    ('bot_rooms', 'owner_user_id'),
    ('memories', 'created_by'),
    ('runs', 'owner_user_id'),
)


class PgUserPurge:
    """PgUserPurge 方法组，挂在 `PostgresStore` 上。"""

    async def purge_preview(self, user_id: str) -> dict:
        """删除确认框要显示的条数。只读，不改任何数据。"""
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'SELECT '
                ' (SELECT count(*) FROM sessions WHERE owner_user_id=%s) AS sessions, '
                ' (SELECT count(*) FROM messages m JOIN sessions s '
                '   ON s.session_id=m.session_id WHERE s.owner_user_id=%s) AS messages, '
                ' (SELECT count(*) FROM tasks t JOIN sessions s '
                '   ON s.session_id=t.session_id WHERE s.owner_user_id=%s) AS tasks, '
                ' (SELECT count(*) FROM runs WHERE owner_user_id=%s) AS runs, '
                ' (SELECT count(*) FROM artifacts a JOIN runs r '
                '   ON r.run_id=a.run_id WHERE r.owner_user_id=%s) AS artifacts, '
                ' (SELECT count(*) FROM auth_sessions WHERE user_id=%s) AS logins, '
                ' (SELECT count(*) FROM identities WHERE user_id=%s) AS identities, '
                ' (SELECT count(*) FROM tool_result_manifests '
                '   WHERE owner_user_id=%s) AS tool_results',
                (user_id,) * 8)
        data = {k: int(v or 0) for k, v in (row or {}).items()}
        data['total'] = sum(data.values())
        return data

    async def purge_user(self, *, user_id: str, actor_id: str = '',
                         reason: str = '') -> dict:
        """彻底清除。前置条件只有两条：不能删自己、不能删最后一个超管。

        不再要求「先停用」：超管对任何用户都有删除权，停用只是一个可逆的
        中间态，多一道前置反而挡住了正常清理。防误删由前端确认框承担。
        """
        if user_id == actor_id:
            raise ValueError('不能删除自己')
        stamp = now()
        stats: dict[str, int] = {}
        async with self._lease() as conn:
            await self._run(
                conn, 'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                ('console-role',))
            user = await self._one(
                conn,
                'SELECT user_id, status FROM app_users '
                'WHERE user_id=%s FOR UPDATE', (user_id,))
            if user is None:
                raise LookupError('用户不存在')

            bound = await self._all(
                conn, "SELECT role FROM identities WHERE user_id=%s", (user_id,))
            if any(str(r.get('role') or '') == 'platform_admin' for r in bound):
                left = await self._one(
                    conn,
                    "SELECT count(DISTINCT user_id) AS n FROM identities "
                    "WHERE role='platform_admin' AND user_id<>%s", (user_id,))
                if int((left or {}).get('n') or 0) == 0:
                    raise ValueError('这是最后一个超管，删掉就没人能再进控制台了')

            ids = await self._collect_ids(conn, user_id)
            file_paths = await self._collect_paths(conn, user_id, ids)
            await self._delete_children(conn, ids, stats)
            await self._delete_owned(conn, user_id, stats)
            await self._delete_identity(conn, user_id, stats)

            await self._audit(conn, {
                'type': 'console_user_purge', 'operator_id': actor_id,
                'user_ref': hashlib.sha256(user_id.encode()).hexdigest()[:16],
                'reason': (reason or '')[:200], 'counts': stats,
                'file_paths': file_paths[:200]}, stamp)
            await conn.commit()

        removed, failed = self._remove_files(file_paths)
        return {'user_id': user_id, 'purged': stats,
                'deleted_rows': sum(stats.values()),
                'files_removed': removed, 'files_failed': failed,
                'audit_ref': hashlib.sha256(user_id.encode()).hexdigest()[:16]}

    # ---------- 单个会话的删除（侧栏「删除任务」） ----------

    async def delete_session(self, session_id: str) -> dict:
        """删掉一条会话：库里的行、磁盘上的产物，一起清掉。

        与 `purge_user` 的差别只有范围：这里是**一个 session_id**，所以一条
        「按 owner 删」的语句都不能有——同一个用户名下还有别的任务要留着。

        文件不在事务里删（文件系统不会回滚），提交成功之后才动磁盘；删失败
        只告警并把路径带回去，让人能手工补。

        实现上先收 ID 再删：删掉 sessions 行之后，任务/运行就再也反查不到了。
        """
        stats: dict[str, int] = {}
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT session_id FROM sessions WHERE session_id=%s FOR UPDATE',
                (session_id,))
            if row is None:
                raise LookupError('会话不存在')
            ids = await self._session_ids(conn, session_id)
            paths = await self._session_paths(conn, ids)
            await self._delete_children(conn, ids, stats)
            await conn.commit()

        removed, failed = self._remove_files(paths)
        return {'session_id': session_id,
                'deleted_rows': sum(stats.values()),
                'counts': stats,
                'files_removed': removed,
                'files_failed': failed}

    async def _session_ids(self, conn, session_id: str) -> dict[str, list[str]]:
        """这条会话牵涉到的全部 ID（与 `_collect_ids` 同形状，供 `_delete_children` 用）。"""
        async def values(sql: str, params=()):
            rows = await self._all(conn, sql, params)
            return [str(next(iter(r.values()))) for r in rows if next(iter(r.values()))]

        tasks = await values(
            'SELECT task_id FROM tasks WHERE session_id=%s', (session_id,))
        runs = await values(
            'SELECT run_id FROM runs WHERE session_id=%s', (session_id,))
        if tasks:
            runs += await values(
                'SELECT run_id FROM runs WHERE task_id = ANY(%s)', (tasks,))
            runs += await values(
                'SELECT current_run_id FROM tasks_meta WHERE task_id = ANY(%s) '
                'AND current_run_id IS NOT NULL', (tasks,))
        runs = sorted(set(runs))
        turns = await values(
            'SELECT turn_id FROM session_turns WHERE session_id=%s', (session_id,))
        actions = await values(
            'SELECT action_id FROM actions WHERE run_id = ANY(%s)', (runs,)) if runs else []
        results = await values(
            'SELECT result_id FROM tool_result_manifests '
            'WHERE run_id = ANY(%s)', (runs,)) if runs else []
        approvals = await values(
            'SELECT approval_id FROM approvals WHERE run_id = ANY(%s)', (runs,)) if runs else []
        return {'sessions': [session_id],
                'tasks': sorted(set(tasks)),
                'runs': runs,
                'turns': turns,
                'actions': actions,
                'results': results,
                'approvals': approvals}

    async def _session_paths(self, conn, ids: dict[str, list[str]]) -> list[str]:
        """这条会话在磁盘上留下的东西：产物文件、工具结果、任务产物目录。

        **不碰工作空间目录**：空间是用户自己的容器，里面可能还有别的任务的文件，
        按会话删除时把整个空间端掉就是数据灾难。这里只删"这一条会话产出的"。
        """
        from webpage_agent.core import sandbox

        paths: list[str] = []
        if ids['runs']:
            rows = await self._all(
                conn, 'SELECT storage_key FROM artifacts WHERE run_id = ANY(%s)',
                (ids['runs'],))
            paths += [str(r['storage_key']) for r in rows if r.get('storage_key')]
        if ids['results']:
            rows = await self._all(
                conn, 'SELECT storage_key FROM tool_result_parts '
                      'WHERE result_id = ANY(%s)', (ids['results'],))
            paths += [str(r['storage_key']) for r in rows if r.get('storage_key')]
        # 任务产物目录没有登记表，目录名就是 artifact_id / task_id。名字先过
        # `sandbox.task_dir` 的校验：非法值（空、带路径分隔符）直接跳过，绝不拼。
        rows = await self._all(
            conn,
            'SELECT t.task_id, t.artifact_id, '
            '       COALESCE(m.tenant_id, %s) AS tenant_id, '
            '       COALESCE(m.owner_user_id, %s) AS owner_user_id '
            'FROM tasks t LEFT JOIN tasks_meta m ON m.task_id=t.task_id '
            'WHERE t.session_id = ANY(%s)',
            ('default', 'default', ids['sessions']))
        for row in rows:
            for name in (row.get('artifact_id'), row.get('task_id')):
                try:
                    paths.append(str(sandbox.task_dir(
                        str(name or ''),
                        str(row.get('tenant_id') or 'default'),
                        str(row.get('owner_user_id') or 'default'))))
                except sandbox.SandboxError:
                    continue
        return sorted(set(paths))

    async def _collect_ids(self, conn, user_id: str) -> dict[str, list[str]]:
        """删之前一次性收集所有关联 ID，删掉父行后就查不到了。"""
        async def values(sql: str, params=()):
            rows = await self._all(conn, sql, params)
            return [str(next(iter(r.values()))) for r in rows if next(iter(r.values()))]

        sessions = await values(
            'SELECT session_id FROM sessions WHERE owner_user_id=%s', (user_id,))
        tasks = await values(
            'SELECT task_id FROM tasks_meta WHERE owner_user_id=%s', (user_id,))
        if sessions:
            tasks += await values(
                'SELECT task_id FROM tasks WHERE session_id = ANY(%s)',
                (sessions,))
        tasks = sorted(set(tasks))
        runs = await values(
            'SELECT current_run_id FROM tasks_meta WHERE owner_user_id=%s '
            'AND current_run_id IS NOT NULL', (user_id,))
        runs += await values(
            'SELECT run_id FROM runs WHERE owner_user_id=%s', (user_id,))
        if tasks:
            runs += await values(
                'SELECT run_id FROM runs WHERE task_id = ANY(%s)', (tasks,))
        runs = sorted(set(runs))
        turns = await values(
            'SELECT turn_id FROM session_turns WHERE owner_user_id=%s', (user_id,))
        if sessions:
            turns += await values(
                'SELECT turn_id FROM session_turns WHERE session_id = ANY(%s)',
                (sessions,))
        turns = sorted(set(turns))
        actions = await values(
            'SELECT action_id FROM actions WHERE run_id = ANY(%s)', (runs,)) if runs else []
        results = await values(
            'SELECT result_id FROM tool_result_manifests WHERE owner_user_id=%s',
            (user_id,))
        if runs:
            results += await values(
                'SELECT result_id FROM tool_result_manifests '
                'WHERE run_id = ANY(%s)', (runs,))
        results = sorted(set(results))
        approvals = await values(
            'SELECT approval_id FROM approvals WHERE owner_user_id=%s '
            'OR requester_user_id=%s OR approver_user_id=%s',
            (user_id, user_id, user_id))
        if runs:
            approvals += await values(
                'SELECT approval_id FROM approvals WHERE run_id = ANY(%s)', (runs,))
        approvals = sorted(set(approvals))
        return {'sessions': sessions, 'tasks': tasks, 'runs': runs,
                'turns': turns, 'actions': actions, 'results': results,
                'approvals': approvals}

    async def _collect_paths(self, conn, user_id: str,
                             ids: dict[str, list[str]]) -> list[str]:
        """收集要删的文件路径。只收路径，不在事务里做文件 IO。"""
        paths: list[str] = []
        rows = await self._all(
            conn, 'SELECT path FROM user_workspaces WHERE owner_user_id=%s',
            (user_id,))
        paths += [str(r['path']) for r in rows if r.get('path')]
        # 机器人自己的家目录（workspaces/<tenant>/<user>/bots/<bot_id>）不在
        # user_workspaces 里，按身份行逐个补上；路径校验仍由 `_remove_files` 兜。
        try:
            from webpage_agent.bot import service as bot_service
            bot_rows = await self._all(
                conn, 'SELECT bot_id, tenant_id FROM bots WHERE owner_user_id=%s',
                (user_id,))
            for row in bot_rows:
                paths.append(str(bot_service.bot_home(
                    str(row.get('tenant_id') or 'default'), user_id,
                    str(row.get('bot_id') or ''))))
        except Exception:                          # noqa: BLE001
            pass
        if ids['runs']:
            rows = await self._all(
                conn, 'SELECT storage_key FROM artifacts WHERE run_id = ANY(%s)',
                (ids['runs'],))
            paths += [str(r['storage_key']) for r in rows if r.get('storage_key')]
        if ids['results']:
            rows = await self._all(
                conn, 'SELECT storage_key FROM tool_result_parts '
                      'WHERE result_id = ANY(%s)', (ids['results'],))
            paths += [str(r['storage_key']) for r in rows if r.get('storage_key')]
        # OAuth 凭据文件按 <tenant>__<user_id>__<server>.json 命名，不属于 DB 表。
        secrets_dir = self._project_root() / 'secrets' / 'oauth'
        if secrets_dir.is_dir():
            for p in secrets_dir.glob(f'*__{user_id}__*.json'):
                paths.append(str(p))
        return sorted(set(paths))

    async def _del(self, conn, table: str, column: str,
                   values: list[str], stats: dict) -> None:
        if not values:
            return
        cur = await self._run(
            conn, f'DELETE FROM {table} WHERE {column} = ANY(%s)', (values,))
        stats[table] = stats.get(table, 0) + int(cur.rowcount or 0)

    async def _delete_children(self, conn, ids: dict[str, list[str]],
                               stats: dict) -> None:
        """叶子 → 中间：先删引用了 session/task/run/turn/action 的行。"""
        s, t, r = ids['sessions'], ids['tasks'], ids['runs']
        turn, action = ids['turns'], ids['actions']
        result, approval = ids['results'], ids['approvals']
        for table, column, values in (
            ('messages', 'session_id', s),
            ('session_memory', 'session_id', s),
            ('session_summaries', 'session_id', s),
            ('session_turn_events', 'turn_id', turn),
            ('tool_result_parts', 'result_id', result),
            ('action_attempts', 'action_id', action),
            ('approval_reservations', 'action_id', action),
            ('approval_reservations', 'approval_id', approval),
            ('tool_authorizations', 'action_id', action),
            ('steps', 'run_id', r),
            ('actions', 'run_id', r),
            ('plans', 'run_id', r),
            ('run_decisions', 'run_id', r),
            ('run_inputs', 'run_id', r),
            ('run_snapshots', 'run_id', r),
            ('artifacts', 'run_id', r),
            ('provider_charges', 'run_id', r),
            ('tool_result_manifests', 'run_id', r),
            ('events', 'task_id', t),
            ('tasks_meta', 'task_id', t),
            ('approvals', 'run_id', r),
            ('external_tasks', 'run_id', r),
            ('request_keys', 'session_id', s),
            ('request_keys', 'task_id', t),
            ('request_keys', 'run_id', r),
            ('scheduled_task_runs', 'session_id', s),
            ('scheduled_task_runs', 'task_id', t),
            ('scheduled_task_runs', 'run_id', r),
            ('session_turns', 'turn_id', turn),
            ('session_turns', 'session_id', s),
            ('tasks', 'task_id', t),
            ('tasks', 'session_id', s),
            ('runs', 'run_id', r),
            ('runs', 'task_id', t),
            ('sessions', 'session_id', s),
        ):
            await self._del(conn, table, column, values, stats)

    async def _delete_owned(self, conn, user_id: str, stats: dict) -> None:
        """按用户 ID 删剩余归属数据；带 OR 的几张表单独处理。"""
        # 房间子表没有 owner 列：必须在删 bot_rooms 之前按房间清掉，否则留下孤儿。
        for table in ('bot_room_events', 'bot_room_tasks'):
            cur = await self._run(
                conn, f'DELETE FROM {table} WHERE room_id IN ('
                      'SELECT room_id FROM bot_rooms WHERE owner_user_id=%s)',
                (user_id,))
            stats[table] = stats.get(table, 0) + int(cur.rowcount or 0)
        for table, column in _USER_TABLES:
            cur = await self._run(
                conn, f'DELETE FROM {table} WHERE {column}=%s', (user_id,))
            stats[table] = stats.get(table, 0) + int(cur.rowcount or 0)
        for table, sql in (
            ('approvals', 'DELETE FROM approvals WHERE requester_user_id=%s '
                          'OR approver_user_id=%s'),
            ('memories', "DELETE FROM memories WHERE subject=%s"),
            ('credential_bindings', "DELETE FROM credential_bindings "
                                    "WHERE subject_kind='user' AND subject_id=%s"),
            ('external_tasks', 'DELETE FROM external_tasks '
                               'WHERE credential_subject=%s'),
        ):
            params = (user_id, user_id) if table == 'approvals' else (user_id,)
            cur = await self._run(conn, sql, params)
            stats[table] = stats.get(table, 0) + int(cur.rowcount or 0)

    async def _delete_identity(self, conn, user_id: str, stats: dict) -> None:
        # 消息平台：配置、运行态、队列、登录态、审计都按 owner_user_id 归属。
        # im_pairing.user_id 是外部 IM 用户编号，不是中台账号，不能按它删。
        for table in ('im_channels', 'im_runtime', 'im_inbox', 'im_outbox',
                      'im_login_state', 'im_events', 'im_sessions',
                      'im_pairing'):
            cur = await self._run(
                conn, f'DELETE FROM {table} WHERE owner_user_id=%s', (user_id,))
            stats[table] = stats.get(table, 0) + int(cur.rowcount or 0)
        # sessions 兜底：万一有行的 owner_user_id 是后来补写、没被 collect 到。
        cur = await self._run(
            conn, 'DELETE FROM sessions WHERE owner_user_id=%s', (user_id,))
        stats['sessions'] = stats.get('sessions', 0) + int(cur.rowcount or 0)
        for table in ('auth_sessions', 'identities', 'local_accounts', 'app_users'):
            cur = await self._run(
                conn, f'DELETE FROM {table} WHERE user_id=%s', (user_id,))
            stats[table] = stats.get(table, 0) + int(cur.rowcount or 0)

    @staticmethod
    def _project_root() -> Path:
        # .../main_branch/src/webpage_agent/store/pg_user_purge.py
        # parents: [0]=store [1]=webpage_agent [2]=src [3]=main_branch
        return Path(__file__).resolve().parents[3]

    @classmethod
    def _remove_files(cls, paths: list[str]) -> tuple[int, list[str]]:
        """DB 已提交后才调用。逐路径校验根目录，绝不按通配符批量删。"""
        from webpage_agent.core.config import settings

        root = cls._project_root()
        # 根目录跟配置走：ARTIFACT_DIR / WORKSPACE_ROOT 换过位置时，写死的
        # `main_branch/artifacts` 会把合法路径全判成越界——删除看起来成功，
        # 实际一个字节都没删。
        allowed = (Path(settings.workspace_root), Path(settings.artifact_dir),
                   root / 'secrets' / 'oauth')
        removed, failed = 0, []
        for raw in paths:
            try:
                path = Path(raw)
                if not path.is_absolute():
                    path = root / path
                resolved = path.resolve()
                if not any(str(resolved).startswith(str(a.resolve()) + os.sep)
                           for a in allowed):
                    failed.append(raw)
                    continue
                if resolved.is_dir():
                    shutil.rmtree(resolved)
                elif resolved.is_file():
                    resolved.unlink()
                else:
                    continue
                removed += 1
            except OSError as exc:
                logger.warning('purge 删除文件失败 path=%s err=%s', raw, exc)
                failed.append(raw)
        return removed, failed
