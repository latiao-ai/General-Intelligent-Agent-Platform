"""连接池、借还和启动对账。

`PostgresStore` 的连接方法原样放在这里：`__init__`、`_make_pool`、`ready`、
`close`、`pool_stats`、`path`、`_apply_schema`，以及 `_lease` / `_one` /
`_all` / `_run`。启动对账 `_recover_orphans`、`reconcile_orphan_runs` 和
健康计数 `stats` 跟连接生命周期放在一起。方法体没有改。

`_lease`、`_one`、`_all`、`_run` 覆盖 `GovernanceStore` 的四个抽象方法。
`logger` 仍使用原来的名字 `webpage_agent.store.pg_store`。
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from webpage_agent.core.config import settings
from webpage_agent.store.pg_schema import MIGRATIONS, SCHEMA
from webpage_agent.store.pg_store import now

logger = logging.getLogger('webpage_agent.store.pg_store')

class PgConnection:
    """PgConnection 方法组。见模块说明。"""

    def __init__(self, dsn: str, *, pool_size: int | None = None) -> None:
        self.dsn = dsn
        self._pool_size = pool_size
        self._pool = self._make_pool()
        self._open_lock: asyncio.Lock | None = None
        self._opened = False
        self.interrupted = 0

    def _make_pool(self) -> AsyncConnectionPool:
        size = self._pool_size if self._pool_size is not None else settings.db_pool_size
        return AsyncConnectionPool(
            self.dsn,
            min_size=1,
            max_size=max(1, size),
            timeout=settings.db_pool_timeout,
            kwargs={'row_factory': dict_row, 'autocommit': False},
            name='agent-platform',
            open=False,                 # 不在 __init__ 里做 IO：见 ready()
        )

    async def ready(self) -> None:
        """打开连接池、建表、对账（幂等）。

        启动方（API lifespan / Worker / 脚本）应当显式 await 一次：连不上库时
        失败发生在启动阶段，而不是等到第一个用户请求才炸。
        """
        if self._opened:
            return
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        async with self._open_lock:
            if self._opened:
                return
            if getattr(self._pool, 'closed', False):
                # 之前 close() 过：psycopg 的池不能复用，只能重建一个。
                # 不重建的话，同一实例被 close 后再用会抛 PoolClosed。
                self._pool = self._make_pool()
            await self._pool.open(wait=True, timeout=max(5.0, settings.db_pool_timeout))
            try:
                await self._apply_schema()
                self.interrupted = await self._recover_orphans()
            except BaseException:
                # 建表/对账失败就别留着半开的池
                await self._pool.close()
                raise
            self._opened = True

    @asynccontextmanager
    async def _lease(self, *, bootstrap: bool = False):
        """借一条连接：块内正常结束提交，出异常回滚，无论如何归还。

        事务边界写在这里，而不是依赖连接池的隐式提交——账本、事件这类写入
        必须明确"要么提交、要么回滚"。
        """
        if not bootstrap:
            await self.ready()
        conn = await self._pool.getconn()
        try:
            yield conn
            if not conn.autocommit:
                await conn.commit()
        except BaseException:
            try:
                await conn.rollback()
            except Exception:                       # noqa: BLE001
                # 连接已经坏了：回滚失败不掩盖原始异常，交给池回收
                pass
            raise
        finally:
            await self._pool.putconn(conn)

    # ---------- 单语句小工具：把 await 收在一处 ----------
    # psycopg 的异步游标连 fetchone/fetchall 都要 await，写开就是两层嵌套；
    # 这层薄封装让每个方法体保持"一条 SQL 一行"的可读性。

    @staticmethod
    async def _one(conn, sql: str, params=()):
        cur = await conn.execute(sql, params)
        return await cur.fetchone()

    @staticmethod
    async def _all(conn, sql: str, params=()):
        cur = await conn.execute(sql, params)
        return await cur.fetchall()

    @staticmethod
    async def _run(conn, sql: str, params=()):
        """执行语句并返回游标（要 rowcount 时用它）。"""
        return await conn.execute(sql, params)

    def pool_stats(self) -> dict:
        """连接池现状：给 /api/health 与排障看（不涉及任何业务语义）。"""
        stats = self._pool.get_stats()
        return {'min_size': stats.get('pool_min'),
                'max_size': stats.get('pool_max'),
                'size': stats.get('pool_size'),
                'available': stats.get('pool_available'),
                'requests_waiting': stats.get('requests_waiting'),
                'usage_ms': stats.get('usage_ms')}

    async def _apply_schema(self) -> None:
        from webpage_agent.store import migrations
        async with self._lease(bootstrap=True) as conn:
            if settings.db_auto_migrate:
                await migrations.apply(conn, baseline=SCHEMA, legacy_ids=MIGRATIONS)
            else:
                await migrations.require_current(conn)

    # ---------- 维护 ----------

    async def stats(self) -> dict:
        """运行态势快照：给 /api/health 与排障用。"""
        queries = {
            'runs_queued': "SELECT COUNT(*) AS n FROM runs WHERE status='queued'",
            'runs_running': "SELECT COUNT(*) AS n FROM runs WHERE status='running'",
            'runs_reconciling': "SELECT COUNT(*) AS n FROM runs "
                                "WHERE status='reconciling'",
            'actions_unknown': "SELECT COUNT(*) AS n FROM actions "
                               "WHERE status='unknown'",
            'jobs_queued': "SELECT COUNT(*) AS n FROM background_jobs "
                           "WHERE status='queued'",
            'jobs_failed': "SELECT COUNT(*) AS n FROM background_jobs "
                           "WHERE status='failed'",
            'approvals_pending': "SELECT COUNT(*) AS n FROM approvals "
                                 "WHERE status='pending'",
            'external_open': "SELECT COUNT(*) AS n FROM external_tasks "
                             "WHERE state IN ('accepted','running','materializing')",
        }
        out = {}
        async with self._lease() as conn:
            for name, sql in queries.items():
                row = await self._one(conn, sql)
                out[name] = int(row['n'] if row else 0)
        return out

    # ---------- 服务心跳 ----------
    # API 与 Worker 是两个进程，"API 活着"不等于"有人在干活"。Worker 每次
    # 心跳写一行，接纳层与监控据此判断执行器是否在线。

    async def beat(self, service: str, instance_id: str, *,
                   host: str = '', pid: int = 0, meta: str = '{}') -> None:
        """写/续一次心跳。同一 instance_id 只保留一行，不堆积历史。"""
        stamp = time.time()
        async with self._lease() as conn:
            await self._run(conn,
                'INSERT INTO service_heartbeats '
                '(service, instance_id, beat_at, started_at, host, pid, meta_json) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (service, instance_id) DO UPDATE SET '
                'beat_at=EXCLUDED.beat_at, host=EXCLUDED.host, '
                'pid=EXCLUDED.pid, meta_json=EXCLUDED.meta_json',
                (service, instance_id, stamp, stamp, host, int(pid), meta))
            await conn.commit()

    async def live_instances(self, service: str,
                             max_age_seconds: float) -> list[dict]:
        """还活着（最后一次心跳在 max_age 之内）的实例。"""
        cutoff = time.time() - float(max_age_seconds)
        async with self._lease() as conn:
            rows = await self._all(conn,
                'SELECT instance_id, beat_at, started_at, host, pid, meta_json '
                'FROM service_heartbeats WHERE service=%s AND beat_at >= %s '
                'ORDER BY beat_at DESC', (service, cutoff))
        return [dict(r) for r in rows]

    async def live_count(self, service: str, max_age_seconds: float) -> int:
        cutoff = time.time() - float(max_age_seconds)
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT COUNT(*) AS n FROM service_heartbeats '
                'WHERE service=%s AND beat_at >= %s', (service, cutoff))
        return int((row or {}).get('n') or 0)

    async def purge_heartbeats(self, max_age_seconds: float) -> int:
        """清掉早已失联的实例行。不能留着：否则"看着有人"会变成一种假象。"""
        cutoff = time.time() - float(max_age_seconds)
        async with self._lease() as conn:
            cur = await self._run(conn,
                'DELETE FROM service_heartbeats WHERE beat_at < %s', (cutoff,))
            await conn.commit()
            return int(cur.rowcount or 0)

    async def turn_backlog(self) -> dict:
        """turn 积压态势：等多久了、卡在哪。

        `oldest_accepted_age` 是接纳后一直没人领的最长等待秒数——它是
        "消息发出去没回复"最直接的量化指标。
        """
        async with self._lease() as conn:
            accepted = await self._one(conn,
                "SELECT COUNT(*) AS n FROM session_turns "
                "WHERE status IN ('accepted','retry')")
            running = await self._one(conn,
                "SELECT COUNT(*) AS n FROM session_turns WHERE status='running'")
            oldest = await self._one(conn,
                "SELECT created_at FROM session_turns "
                "WHERE status IN ('accepted','retry') "
                'ORDER BY created_at LIMIT 1')
        age = 0.0
        raw = (oldest or {}).get('created_at')
        if raw:
            try:
                age = max(0.0, time.time() - datetime.fromisoformat(raw).timestamp())
            except (TypeError, ValueError):
                age = 0.0
        return {'turns_accepted': int((accepted or {}).get('n') or 0),
                'turns_running': int((running or {}).get('n') or 0),
                'oldest_accepted_age': round(age, 1)}

    async def _recover_orphans(self) -> int:
        """只把**没有活租约**的遗留 running 任务标记为 interrupted。

        多副本下不能见到 running 就改写：那会把别人正在跑的任务替它判死。
        """
        async with self._lease(bootstrap=True) as conn:
            cur = await self._run(conn, "UPDATE tasks SET status='interrupted', updated_at=%s "
                "WHERE status='running' AND NOT EXISTS ("
                '  SELECT 1 FROM runs r WHERE r.task_id = tasks.task_id '
                "  AND r.status='running' AND r.lease_expires_at IS NOT NULL "
                '  AND r.lease_expires_at > %s)',
                (now(), time.time()))
            await conn.commit()
            return cur.rowcount

    async def reconcile_orphan_runs(self, owner: str | None = None,
                              now_ts: float | None = None) -> dict:
        """只对账**自己名下的、或租约已过期的**运行（文档 §8.3）。

        以前是"见到 running 就判 unknown 并清租约"：多副本或 --reload 会把
        别的进程正在跑的任务判成结果未知，清掉租约后立刻又被领一次，正好
        制造它想防的重复执行。
        """
        current = now_ts if now_ts is not None else time.time()
        stats = {'runs': 0, 'unknown_actions': 0, 'requeued': 0}
        async with self._lease() as conn:
            rows = await self._all(conn, "SELECT run_id FROM runs WHERE status IN ('running','reconciling') "
                'AND (lease_owner IS NULL OR lease_owner = %s '
                'OR lease_expires_at IS NULL OR lease_expires_at < %s)',
                (owner, current))
            for row in rows:
                run_id = row['run_id']
                stats['runs'] += 1
                pending = await self._all(conn, "SELECT action_id, step_id FROM actions "
                    "WHERE run_id=%s AND status='in_flight'", (run_id,))
                for action in pending:
                    await self._run(conn, "UPDATE actions SET status='unknown', updated_at=%s "
                        'WHERE action_id=%s', (now(), action['action_id']))
                    stats['unknown_actions'] += 1
                    await self._run(conn, "UPDATE steps SET status='unknown', error=%s "
                        'WHERE run_id=%s AND step_id=%s',
                        ('进程重启时该调用结果未知，需核实', run_id,
                         action['step_id']))
                target = 'reconciling' if pending else 'queued'
                if target == 'queued':
                    stats['requeued'] += 1
                await self._run(conn, 'UPDATE runs SET status=%s, lease_owner=NULL, '
                    'lease_expires_at=NULL, updated_at=%s WHERE run_id=%s',
                    (target, now(), run_id))
            await conn.commit()
        return stats

    async def close(self) -> None:
        """关闭连接池（幂等）。进程退出、测试收尾都走这里。"""
        pool = getattr(self, '_pool', None)
        if pool is None or not self._opened:
            return
        try:
            # 有界等待：池的 worker 可能正在退避重连，无限等会卡住进程退出
            await asyncio.wait_for(pool.close(), timeout=5)
        except (asyncio.TimeoutError, Exception):   # noqa: BLE001
            logger.warning('关闭连接池超时/失败，已强制标记关闭')
        self._opened = False

    # ---------- 兼容接口（SQLite 实现里有、PG 侧不需要的） ----------

    @property
    def path(self) -> str:
        return self.dsn
