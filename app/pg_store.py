"""PostgreSQL 持久化：与 SQLite 实现同一套仓储接口。

设计取舍：

- **连接池**（`psycopg_pool`）而不是"一个进程一条连接 + 一把锁"。每次调用
  借一条连接，用完立即归还；池大小与等待超时都可配（`DB_POOL_SIZE` /
  `DB_POOL_TIMEOUT`）。多线程读写因此能真正并行；
- **并发正确性不靠应用锁，靠数据库**：租约 + `lease_epoch` fencing、
  `FOR UPDATE SKIP LOCKED` 领取、事件序号靠主键冲突重算。应用层的锁一旦
  和事务边界纠缠不清，就会变成"看起来安全、实际把吞吐锁死"；
- **事务边界显式写在每个方法里**（`_lease()` 里提交/回滚），不依赖连接池的
  隐式提交；
- **事件日志以数据库为准，内存只负责实时投递**：这样进程重启后客户端仍能拿到
  完整的执行过程（见 app/api_hub.py 的 subscribe/去重逻辑）；
- 启动时把库里遗留的 `running` 运行交给对账（`reconcile_orphan_runs`）——
  进程重启后不可能还有任务在跑，否则前端会一直等一个永远不会结束的任务。
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    owner_user_id TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
-- 旧库升级：PG 支持 ADD COLUMN IF NOT EXISTS，直接并进建表脚本即可当迁移
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS owner_user_id TEXT NOT NULL DEFAULT 'default';
CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL,
    artifact_id TEXT,
    error       TEXT,
    state       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE TABLE IF NOT EXISTS session_memory (
    session_id TEXT PRIMARY KEY,
    summary    TEXT NOT NULL DEFAULT '',
    facts      TEXT NOT NULL DEFAULT '{}',
    upto       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    task_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);

-- ---------- 中台对象：task / run 分离（文档 §3.1） ----------
-- tasks 是持续目标，runs 是对这个目标的一次执行尝试。
-- 旧表沿用，这里补上"当前运行"和对 run 的引用，避免外部 task_id 与内部产物
-- 目录 ID 语义混用。
CREATE TABLE IF NOT EXISTS tasks_meta (
    task_id        TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL DEFAULT 'default',
    owner_user_id  TEXT NOT NULL DEFAULT 'default',
    goal           TEXT NOT NULL DEFAULT '',
    current_run_id TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    status              TEXT NOT NULL,
    plan_version        INTEGER NOT NULL DEFAULT 0,
    budget_json         TEXT NOT NULL DEFAULT '{}',
    consumed_json       TEXT NOT NULL DEFAULT '{}',
    cancel_requested_at TEXT,
    pause_requested     INTEGER NOT NULL DEFAULT 0,
    lease_owner         TEXT,
    lease_expires_at    DOUBLE PRECISION,
    lease_epoch         INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    compensated_at      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, lease_expires_at);

-- ---------- 计划（文档 §6.1） ----------
CREATE TABLE IF NOT EXISTS plans (
    run_id     TEXT NOT NULL,
    version    INTEGER NOT NULL,
    goal       TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, version)
);

-- ---------- 步骤（文档 §6、§9.1） ----------
CREATE TABLE IF NOT EXISTS steps (
    run_id       TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    step_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    tool_key     TEXT NOT NULL,
    status       TEXT NOT NULL,
    inputs_json  TEXT NOT NULL DEFAULT '{}',
    output_json  TEXT,
    action_id    TEXT,
    started_at   TEXT,
    ended_at     TEXT,
    error        TEXT,
    PRIMARY KEY (run_id, plan_version, step_id)
);

-- ---------- 动作账本（文档 §8） ----------
-- action 是一次业务操作的身份；attempt 是真实发出的每一次调用。
-- 幂等键挂在 action 上，重试只是新增 attempt。
CREATE TABLE IF NOT EXISTS actions (
    action_id       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    plan_version    INTEGER NOT NULL,
    step_id         TEXT NOT NULL,
    tool_key        TEXT NOT NULL,
    args_hash       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    effect          TEXT NOT NULL DEFAULT 'unknown',
    status          TEXT NOT NULL,
    receipt_json    TEXT,
    authorized_by   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_idem ON actions(idempotency_key);

CREATE TABLE IF NOT EXISTS action_attempts (
    attempt_id   TEXT PRIMARY KEY,
    action_id    TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    error_type   TEXT,
    result_ref   TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_action ON action_attempts(action_id);

-- ---------- 产物（文档 §9.1） ----------
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    storage_key TEXT NOT NULL,
    mime        TEXT NOT NULL DEFAULT 'text/html',
    size        INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- ---------- 会话记忆的来源与覆盖边界（文档 §11、§12） ----------
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id    TEXT PRIMARY KEY,
    summary       TEXT NOT NULL DEFAULT '',
    facts         TEXT NOT NULL DEFAULT '{}',
    covered_upto  INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT 'auto',
    generated_ver INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL
);

-- ---------- 长期记忆（文档 §12） ----------
-- 作用域化：user / project / tenant 三级，单用户阶段全部落到 default。
CREATE TABLE IF NOT EXISTS memories (
    memory_id  TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL DEFAULT 'default',
    scope_type TEXT NOT NULL,
    scope_id   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    predicate  TEXT NOT NULL,
    value      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    version    INTEGER NOT NULL DEFAULT 1,
    supersedes TEXT,
    source     TEXT NOT NULL DEFAULT '',
    valid_to   TEXT,
    created_by TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_type, scope_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_fact
    ON memories(scope_type, scope_id, subject, predicate, version);

-- 遗忘用墓碑：不能让旧后台任务从同一来源把已删事实再次生成
CREATE TABLE IF NOT EXISTS memory_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    scope_type   TEXT NOT NULL,
    scope_id     TEXT NOT NULL,
    fact_key     TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tombstones_key
    ON memory_tombstones(scope_type, scope_id, fact_key);

-- ---------- 后台任务（文档 §9.2 outbox） ----------
CREATE TABLE IF NOT EXISTS background_jobs (
    job_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    payload_json    TEXT NOT NULL DEFAULT '{}',
    attempts        INTEGER NOT NULL DEFAULT 0,
    lease_owner     TEXT,
    lease_expires_at DOUBLE PRECISION,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe ON background_jobs(kind, dedupe_key);

-- ---------- 请求幂等（POST /api/tasks） ----------
CREATE TABLE IF NOT EXISTS request_keys (
    scope_key     TEXT PRIMARY KEY,
    client_key    TEXT NOT NULL,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    owner_user_id TEXT NOT NULL DEFAULT 'default',
    endpoint      TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    task_id       TEXT,
    session_id    TEXT,
    run_id        TEXT,
    response_json TEXT,
    created_at    TEXT NOT NULL,
    expires_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_keys_expiry ON request_keys(expires_at);

-- 旧库升级：CREATE TABLE IF NOT EXISTS 不会补列。PG 支持 IF NOT EXISTS，
-- 所以升级语句可以和建表放在一起，启动时幂等执行。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS compensated_at TEXT;

-- ---------- 身份：平台身份 → 本地主体（app/auth.py、文档 §3.1） ----------
-- 本地用户是业务外键的唯一目标；平台标识（openid / sub / 邮箱）只用于"找到它"。
-- 为什么不让业务表直接挂 openid：微信 openid 按 appid 隔离、部分平台的 sub 会
-- 轮换，一旦变了，用户的历史会话、任务、产物就全部对不上人。
CREATE TABLE IF NOT EXISTS app_users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    email        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 一个本地用户可以绑多个平台身份（微信 / 飞书 / 企业 SSO）。
-- (provider, subject) 唯一：同一平台同一主体重复登录只会命中，不会分身账号。
CREATE TABLE IF NOT EXISTS identities (
    provider     TEXT NOT NULL,
    subject      TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    identity_provider_id TEXT NOT NULL DEFAULT '',
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    email        TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'member',
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);
-- 旧库升级：CREATE TABLE IF NOT EXISTS 不补列，ALTER 必须显式执行（且在建索引之前）
ALTER TABLE identities ADD COLUMN IF NOT EXISTS identity_provider_id TEXT NOT NULL DEFAULT '';
-- 老数据的身份源就用原来的 provider，保证存量绑定继续命中同一个账号
UPDATE identities SET identity_provider_id = provider WHERE identity_provider_id = '';

-- 身份绑定的**唯一关系**：企业 + 身份源 + 外部用户 → 中台用户。
-- 老列 (provider, subject) 保留给旧数据：迁移后 provider 与身份源一致。
CREATE UNIQUE INDEX IF NOT EXISTS uq_identities_scope
    ON identities(tenant_id, identity_provider_id, subject)
    WHERE identity_provider_id <> '';

-- 票据兑换记录：(issuer, jti) 唯一 → 同一张票据在多实例并发下也只能成功一次
CREATE TABLE IF NOT EXISTS ticket_redemptions (
    issuer      TEXT NOT NULL,
    jti         TEXT NOT NULL,
    connection_id TEXT NOT NULL DEFAULT '',
    platform_id TEXT NOT NULL DEFAULT '',
    tenant_id   TEXT NOT NULL DEFAULT '',
    expires_at  BIGINT NOT NULL,
    used_at     TEXT NOT NULL,
    auth_session_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (issuer, jti)
);
CREATE INDEX IF NOT EXISTS idx_ticket_redemptions_used
    ON ticket_redemptions(used_at);

-- 运行快照：崩溃恢复的权威依据（state.json 只是诊断旁文件）。
CREATE TABLE IF NOT EXISTS run_snapshots (
    run_id                TEXT PRIMARY KEY,
    plan_version          INTEGER NOT NULL DEFAULT 1,
    snapshot_schema       INTEGER NOT NULL DEFAULT 1,
    state_json            TEXT NOT NULL DEFAULT '{}',
    next_step_id          TEXT NOT NULL DEFAULT '',
    artifact_namespace    TEXT NOT NULL DEFAULT '',
    updated_at            TEXT NOT NULL
);

-- ---------- 运行中补充输入（文档 harness-live-input §4） ----------
-- 三个版本必须分开存：lease_epoch（谁有权推进）、intent_version（用户要求更新到
-- 哪一版）、plan_version（当前采用哪份计划）。混用任何一个都会让"旧要求产生的
-- 操作"重新获得准入资格。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS intent_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS processed_intent_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS intent_version INTEGER NOT NULL DEFAULT 1;
-- 这份计划替代了哪一版计划（0 表示首版），审计"谁被谁取代"用
ALTER TABLE plans ADD COLUMN IF NOT EXISTS base_plan_version INTEGER NOT NULL DEFAULT 0;
-- 每个动作依据的要求版本：审计"这次写入是按哪一版要求做的"
ALTER TABLE actions ADD COLUMN IF NOT EXISTS decision_intent_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_snapshots ADD COLUMN IF NOT EXISTS intent_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_snapshots ADD COLUMN IF NOT EXISTS processed_intent_version INTEGER NOT NULL DEFAULT 0;

-- 补充输入的**不可变日志**：真实来源是这里，不是 messages 表。
-- (run_id, request_id) 唯一 → 客户端重试不会重复加版本；
-- (run_id, input_seq) 唯一 → 顺序由服务端在锁住 run 之后分配，不用 MAX+1 猜。
CREATE TABLE IF NOT EXISTS run_inputs (
    input_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    input_seq       INTEGER NOT NULL,
    request_id      TEXT NOT NULL,
    message         TEXT NOT NULL,
    payload_hash    TEXT NOT NULL DEFAULT '',
    intent_version  INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_inputs_request
    ON run_inputs(run_id, request_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_inputs_seq
    ON run_inputs(run_id, input_seq);

-- ---------- 结果观察与决策（文档 harness-observe-decide §9） ----------
-- 结果水位：每次"相关结果落库"在同一事务 +1。判断决策覆盖到哪一步只能用它，
-- 不能用墙钟时间（跨进程、跨副本的时钟没有可比性）。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS observation_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_snapshots ADD COLUMN IF NOT EXISTS observation_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_snapshots ADD COLUMN IF NOT EXISTS next_route TEXT NOT NULL DEFAULT '';

-- 一次自主决策的完整台账：预留 → 调模型 → 校验 → CAS 提交。
-- 崩溃语义（文档 §9）靠它区分"没请求 / 请求了没响应 / 候选未提交 / 已提交"。
CREATE TABLE IF NOT EXISTS run_decisions (
    decision_id            TEXT PRIMARY KEY,
    run_id                 TEXT NOT NULL,
    decision_key           TEXT NOT NULL,       -- 稳定身份：去重靠它，不靠时间
    reason                 TEXT NOT NULL DEFAULT 'observation',
    status                 TEXT NOT NULL DEFAULT 'reserved',
    intent_version         INTEGER NOT NULL DEFAULT 0,
    base_plan_version      INTEGER NOT NULL DEFAULT 0,
    observation_seq        INTEGER NOT NULL DEFAULT 0,
    decision_kind          TEXT NOT NULL DEFAULT '',
    candidate_plan_hash    TEXT NOT NULL DEFAULT '',
    committed_plan_version INTEGER NOT NULL DEFAULT 0,
    input_snapshot         TEXT NOT NULL DEFAULT '',   -- 给模型看的输入（有界）
    provider_requests      INTEGER NOT NULL DEFAULT 0,
    usage_json             TEXT NOT NULL DEFAULT '{}',
    error_code             TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_decisions_key
    ON run_decisions(run_id, decision_key);
CREATE INDEX IF NOT EXISTS idx_run_decisions_run ON run_decisions(run_id);

-- 处理投影：可以从 run_inputs + plans 重建，因此允许任何新执行者继续处理，
-- 不用一个永久的 processing 标记把崩溃的任务锁死。
CREATE TABLE IF NOT EXISTS run_input_processing (
    input_id              TEXT PRIMARY KEY,
    status                TEXT NOT NULL DEFAULT 'accepted',
    processed_plan_version INTEGER NOT NULL DEFAULT 0,
    error_code            TEXT NOT NULL DEFAULT '',
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_input_processing_status
    ON run_input_processing(status);

-- 中台自己的登录会话。凭证原文只在浏览器，这里只存摘要。
CREATE TABLE IF NOT EXISTS auth_sessions (
    auth_session_id  TEXT PRIMARY KEY,
    token_digest     TEXT NOT NULL UNIQUE,
    user_id          TEXT NOT NULL,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    platform_id      TEXT NOT NULL DEFAULT '',
    connection_id    TEXT NOT NULL DEFAULT '',
    identity_provider_id TEXT NOT NULL DEFAULT '',
    subject          TEXT NOT NULL DEFAULT '',
    issued_at        TEXT NOT NULL,
    expires_at       DOUBLE PRECISION NOT NULL,
    last_seen_at     TEXT NOT NULL,
    revoked_at       TEXT,
    user_agent       TEXT NOT NULL DEFAULT '',
    remote_addr      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);
"""


class TicketAlreadyUsed(RuntimeError):
    """同一张票据被第二次兑换（含并发）。调用方应回 409。"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresStore:
    """PostgreSQL 仓储实现（满足 `app.persistence.Repository`），**全异步**。

    连接用法：`psycopg_pool.AsyncConnectionPool` + 每次调用借一条。以前是
    "一个进程一条连接 + 一把 threading.Lock"，把所有 DB 访问串行化，而且同步
    调用会直接阻塞事件循环；现在借连接、执行、提交全部 await，等 IO 的时候
    事件循环可以继续跑别的协程（SSE 推送、其他任务）。

    并发正确性仍然**不靠应用锁，靠数据库**：租约 + `lease_epoch` fencing、
    `claim_run` 的 `FOR UPDATE SKIP LOCKED`、事件序号用 `pg_advisory_xact_lock`
    按任务串行化。所以多进程、多协程同时用一个库是安全的。

    构造本身不碰 IO（连接池以 `open=False` 创建），真正的初始化在第一次
    `await ready()` 里做：打开池 → 建表 → 对账。这样 `PostgresStore(dsn)` 可以
    在模块导入期构造，服务启动时再用 `await store.ready()` 把失败提前暴露。
    """

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
        """建表。语句是幂等的，每次启动都跑一遍即可当迁移。"""
        async with self._lease(bootstrap=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(SCHEMA)
            await conn.commit()

    # ---------- 身份（app/auth.py） ----------

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
                'SELECT * FROM auth_sessions WHERE token_digest=%s',
                (token_digest,))
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

    async def identities_of(self, user_id: str) -> list[dict]:
        """一个本地用户绑了哪些平台身份（账户合并时用来判断能不能自动并）。"""
        async with self._lease() as conn:
            return await self._all(
                conn,
                'SELECT provider, subject, email, created_at, last_seen_at '
                'FROM identities WHERE user_id=%s ORDER BY created_at',
                (user_id,))

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
                       owner_user_id: str = 'default') -> None:
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO messages (session_id, role, content, created_at) '
                'VALUES (%s, %s, %s, %s)', (session_id, role, content, stamp))
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

    # ---------- 任务 ----------

    async def create_task(self, task_id: str, session_id: str, message: str) -> None:
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO tasks (task_id, session_id, message, status, created_at, '
                'updated_at) VALUES (%s, %s, %s, %s, %s, %s)',
                (task_id, session_id, message, 'running', stamp, stamp))
            await conn.commit()

    async def update_task(self, task_id: str, **fields) -> None:
        allowed = {'status', 'artifact_id', 'error', 'state'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if 'state' in updates and not isinstance(updates['state'], str):
            updates['state'] = json.dumps(updates['state'], ensure_ascii=False)
        columns = ', '.join(f'{k}=%s' for k in updates)
        async with self._lease() as conn:
            await self._run(conn, f'UPDATE tasks SET {columns}, updated_at=%s WHERE task_id=%s',
                (*updates.values(), now(), task_id))
            await conn.commit()

    async def task(self, task_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM tasks WHERE task_id=%s', (task_id,))
        if row is None:
            return None
        data = dict(row)
        data['state'] = json.loads(data['state']) if data['state'] else None
        return data

    async def tasks_of(self, session_id: str, limit: int = 50) -> list[dict]:
        async with self._lease() as conn:
            # rowid 是隐式列，可用于排序但不能进索引定义。
            rows = await self._all(conn, 'SELECT task_id, message, status, created_at FROM tasks '
                'WHERE session_id=%s ORDER BY created_at DESC LIMIT %s',
                (session_id, limit))
        return [dict(r) for r in rows]

    async def session_tasks(self, session_id: str, limit: int = 50) -> list[dict]:
        """会话里的任务，按时间**正序**——回放执行过程时要按发生顺序。

        与 tasks_of 的区别只在排序方向：那个是给"最近任务"用的。
        """
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT task_id, message, status, state, created_at FROM tasks '
                'WHERE session_id=%s ORDER BY created_at ASC LIMIT %s',
                (session_id, limit))
        # state 一起带上：回放时要用它把通用状态派生成展示标签（outcome.label）
        out = []
        for row in rows:
            data = dict(row)
            data['state'] = json.loads(data['state']) if data['state'] else None
            out.append(data)
        return out

    # ---------- 事件日志 ----------

    async def _append_event_in_tx(self, conn, task_id: str, event: dict) -> int:
        """在**调用方的事务里**追加一条事件，返回序号。

        为什么要这一版：补充输入必须在"插入日志 + 升版本 + 追加事件"的同一个事务里
        完成（要么全有、要么全无）。直接调 `append_event()` 会自己借连接、自己提交，
        在外层事务里调用它等于把原子性交给运气。

        锁顺序固定为 **run → 输入/计划/动作 → 事件**：先 `pg_advisory_xact_lock`
        再 INSERT，跨表写法保持一致，避免出现新的死锁顺序。
        """
        payload = json.dumps(event, ensure_ascii=False)
        statement = (
            'INSERT INTO events (task_id, seq, payload, created_at) '
            'SELECT %s, COALESCE(MAX(seq), 0) + 1, %s, %s FROM events '
            'WHERE task_id=%s RETURNING seq')
        for _ in range(8):
            try:
                # 已在外层事务里 → `conn.transaction()` 是 SAVEPOINT：
                # 序号冲突只回滚这一小段，不会连带丢掉调用方的输入日志。
                async with conn.transaction():
                    await self._run(conn,
                                    'SELECT pg_advisory_xact_lock(hashtext(%s))',
                                    (task_id,))
                    row = await self._one(conn, statement,
                                          (task_id, payload, now(), task_id))
                    return int(row['seq'])
            except psycopg.errors.UniqueViolation:
                continue
        raise RuntimeError('事件序号竞争失败')

    async def append_event(self, task_id: str, event: dict) -> int:
        """追加一条事件并返回它的序号（每任务内自增，用于订阅去重）。

        序号必须在**数据库里**算，且同一任务的事件追加要串行：两个事务同时
        算出同一个 seq 时，SSE 端会把后到的那条当重复丢掉——静默丢消息，
        比报错难查得多。

        以前进程内那把大锁顺带把这件事做了，所以看不出问题；换成连接池之后
        并发测试立刻暴露"5 次重试也会被打爆"。现在用 `pg_advisory_xact_lock`
        按 task_id 加锁：只串行化**同一个任务**的追加，不同任务之间完全不
        互相阻塞；冲突重算保留为最后一道保险。
        """
        payload = json.dumps(event, ensure_ascii=False)
        statement = (
            'INSERT INTO events (task_id, seq, payload, created_at) '
            'SELECT %s, COALESCE(MAX(seq), 0) + 1, %s, %s FROM events '
            'WHERE task_id=%s RETURNING seq')
        for _ in range(8):
            async with self._lease() as conn:
                try:
                    # 事务级建议锁：随提交/回滚自动释放，不需要额外的解锁路径
                    await self._run(conn, 'SELECT pg_advisory_xact_lock(hashtext(%s))',
                                 (task_id,))
                    row = await self._one(conn, statement, (task_id, payload, now(), task_id))
                    return int(row['seq'])
                except psycopg.errors.UniqueViolation:
                    # 建议锁下不该再发生；真发生（哈希撞了别人的锁又抢到同一号）
                    # 就重算一次，绝不返回一个可能重复的号
                    await conn.rollback()
                    continue
        raise RuntimeError('事件序号竞争失败')

    async def events(self, task_id: str) -> list[tuple[int, dict]]:
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT seq, payload FROM events WHERE task_id=%s ORDER BY seq',
                (task_id,))
        return [(r['seq'], json.loads(r['payload'])) for r in rows]

    # ---------- 中台对象：task / run / plan / step / action ----------
    # 这一层是文档 §3/§6/§8/§9 的落地。旧 tasks 表仍在用（界面查询与事件回放），
    # 新执行链路逐步迁到 run 模型，两边通过 task_id 关联。

    async def ensure_task_meta(self, task_id: str, *, session_id: str, goal: str,
                         tenant_id: str = 'default',
                         owner_user_id: str = 'default') -> None:
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO tasks_meta '
                '(task_id, tenant_id, owner_user_id, goal, created_at) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (task_id, tenant_id, owner_user_id, goal, now()))
            await conn.commit()

    async def task_meta(self, task_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM tasks_meta WHERE task_id=%s', (task_id,))
        return dict(row) if row else None

    async def create_run(self, run_id: str, task_id: str, *, budget: dict | None = None,
                   tenant_id: str = 'default', status: str = 'queued') -> None:
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO runs (run_id, task_id, tenant_id, status, '
                'budget_json, consumed_json, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (run_id, task_id, tenant_id, status,
                 json.dumps(budget or {}, ensure_ascii=False), '{}', stamp, stamp))
            await self._run(conn, 'UPDATE tasks_meta SET current_run_id=%s WHERE task_id=%s',
                (run_id, task_id))
            await conn.commit()

    async def run(self, run_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM runs WHERE run_id=%s', (run_id,))
        return _decode_run(dict(row)) if row else None

    async def update_run(self, run_id: str, **fields) -> None:
        allowed = {'status', 'plan_version', 'intent_version',
                   'processed_intent_version', 'budget_json', 'consumed_json',
                   'cancel_requested_at', 'pause_requested', 'lease_owner',
                   'lease_expires_at', 'lease_epoch', 'error', 'compensated_at'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        for key in ('budget_json', 'consumed_json'):
            if key in updates and not isinstance(updates[key], str):
                updates[key] = json.dumps(updates[key], ensure_ascii=False)
        columns = ', '.join(f'{k}=%s' for k in updates)
        async with self._lease() as conn:
            await self._run(conn, f'UPDATE runs SET {columns}, updated_at=%s WHERE run_id=%s',
                (*updates.values(), now(), run_id))
            await conn.commit()

    async def runs_of(self, task_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT * FROM runs WHERE task_id=%s ORDER BY created_at',
                (task_id,))
        return [_decode_run(dict(r)) for r in rows]

    async def claim_run(self, worker_id: str, lease_seconds: float = 60.0,
                  now_ts: float | None = None,
                  run_id: str | None = None) -> dict | None:
        """原子领取一个运行，并递增租约代次（PG 侧带 SKIP LOCKED）。

        `run_id` 给定时只领这一条（API 进程就地执行也要先占租约，Worker 才
        领不到它）；不给就按入队顺序领一条。子查询上的
        `FOR UPDATE SKIP LOCKED` 是文档 §9.2 明确要求的：两个 Worker 并发
        领取时，第二个直接跳过被锁住的行，而不是等锁再抢同一条。
        """
        stamp = now()
        current = now_ts if now_ts is not None else time.time()
        condition = ("status IN ('queued','running','reconciling') "
                     "AND (lease_expires_at IS NULL OR lease_expires_at < %s)")
        if run_id is not None:
            sql = ("UPDATE runs SET status='running', lease_owner=%s, "
                   "lease_expires_at=%s, lease_epoch=lease_epoch+1, updated_at=%s "
                   f"WHERE run_id=%s AND {condition} RETURNING *")
            params = (worker_id, current + lease_seconds, stamp, run_id, current)
        else:
            sql = ("UPDATE runs SET status='running', lease_owner=%s, "
                   "lease_expires_at=%s, lease_epoch=lease_epoch+1, updated_at=%s "
                   f"WHERE run_id=(SELECT run_id FROM runs WHERE {condition} "
                   "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                   "RETURNING *")
            params = (worker_id, current + lease_seconds, stamp, current)
        async with self._lease() as conn:
            row = await self._one(conn, sql, params)
            await conn.commit()
        return _decode_run(dict(row)) if row else None

    async def _owned(self, conn, context, *, run_id: str,
                     require_status: tuple | None = None) -> dict | None:
        '''按执行上下文取 run 行并校验租约（owner + epoch + 未过期）。

        返回 None 表示"没这个 run"；返回行表示执行权仍然有效。
        **失去执行权抛 LeaseLost**，不给调用方"当作成功继续"的机会。
        '''
        from app import execution_control

        row = await self._one(conn, 'SELECT * FROM runs WHERE run_id=%s FOR UPDATE',
                              (run_id,))
        if row is None:
            return None
        data = dict(row)
        if context is None:
            return data
        owner = str(data.get('lease_owner') or '')
        epoch = int(data.get('lease_epoch') or 0)
        expires = float(data.get('lease_expires_at') or 0)
        if owner != context.lease_owner or epoch != context.lease_epoch:
            raise execution_control.LeaseLost(
                run_id, f'持有者/代次已变更（库中 {owner or "空"}#{epoch}）')
        if expires and expires < time.time():
            raise execution_control.LeaseLost(run_id, '租约已过期')
        if require_status and data.get('status') not in require_status:
            raise execution_control.LeaseLost(
                run_id, f"状态已变为 {data.get('status')}")
        return data

    async def control_state(self, run_id: str) -> dict:
        '''读控制意图（暂停/取消/要求版本）+ 状态。准入动作前必须看它。

        要求版本也要在这里读：执行者的边界检查靠它判断"计划是不是已经过期"。
        少读这一列，边界检查就会永远认为"版本是 1"，从而把所有动作都拦下。
        '''
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'SELECT status, pause_requested, cancel_requested_at, lease_owner, '
                'lease_epoch, lease_expires_at, intent_version, '
                'processed_intent_version FROM runs WHERE run_id=%s',
                (run_id,))
        return dict(row) if row else {}

    async def save_snapshot(self, run_id: str, *, plan_version: int, state: dict,
                            next_step_id: str = '', artifact_namespace: str = '',
                            conn=None, intent_version: int | None = None,
                            processed_intent_version: int | None = None,
                            observation_seq: int | None = None,
                            next_route: str = '') -> None:
        '''写运行快照（恢复所需的最小完整状态）。

        长正文与文件不进快照：它们走产物引用，快照里只留引用与索引。
        快照里的版本字段只用于**排障与核对**：恢复时以 `runs` 的版本为准，
        因为补充输入会改变 `runs.intent_version`，而快照可能还没来得及更新。
        '''
        payload = json.dumps(state, ensure_ascii=False, default=str)
        sql = ('INSERT INTO run_snapshots (run_id, plan_version, snapshot_schema, '
               'state_json, next_step_id, artifact_namespace, updated_at, '
               'intent_version, processed_intent_version, observation_seq, next_route) '
               'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
               'ON CONFLICT (run_id) DO UPDATE SET plan_version=EXCLUDED.plan_version, '
               'state_json=EXCLUDED.state_json, next_step_id=EXCLUDED.next_step_id, '
               'artifact_namespace=EXCLUDED.artifact_namespace, '
               'updated_at=EXCLUDED.updated_at, '
               'intent_version=EXCLUDED.intent_version, '
               'processed_intent_version=EXCLUDED.processed_intent_version, '
               'observation_seq=EXCLUDED.observation_seq, '
               'next_route=EXCLUDED.next_route')
        args = (run_id, int(plan_version or 1), 1, payload, next_step_id,
                artifact_namespace, now(), int(intent_version or 0),
                int(processed_intent_version or 0), int(observation_seq or 0),
                str(next_route or ''))
        if conn is not None:
            await self._run(conn, sql, args)
            return
        async with self._lease() as own:
            await self._run(own, sql, args)

    async def run_snapshot(self, run_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM run_snapshots WHERE run_id=%s',
                                  (run_id,))
        if row is None:
            return None
        data = dict(row)
        try:
            data['state'] = json.loads(data.get('state_json') or '{}')
        except ValueError:
            data['state'] = {}
        return data

    # ---------- 运行中补充输入（文档 harness-live-input §4） ----------
    # 这里的核心不是"存一条消息"，而是**一次事务**里完成五件事：
    # 校验状态 → 幂等查原回执 → 分配序号插日志 → 升 intent_version → 追加事件。
    # 少任何一步都会出现"版本涨了但没有日志"或"日志在、版本没涨"的分叉状态，
    # 而这两种状态都没法安全恢复。

    # 可以接受补充的状态（文档 §2.2）。已收尾/已取消的 run 一律拒绝：
    # 否则"补充"就成了绕过 resume 门禁复活旧任务的旁路。
    INPUT_ACCEPTING_STATUSES = ('queued', 'running', 'reconciling', 'paused',
                               'waiting_input', 'waiting_approval',
                               'needs_reconcile')

    async def append_run_input(self, *, run_id: str, request_id: str,
                               message: str, payload_hash: str = '',
                               task_id: str = '', max_pending: int = 20,
                               max_chars: int = 4000) -> dict:
        """原子接受一条补充输入。

        返回 `{input_id, run_id, task_id, input_seq, intent_version,
        processed_intent_version, status, replayed, duplicate_message}`。

        抛出的异常都是**可映射的业务错误**，由接口层翻译成 404/409/422/429：

        - `InputRejected`：状态不接受（已结束/已取消/不是 current_run）
        - `InputConflict`：同一个 request_id 换了内容
        - `InputBacklog`：待处理输入超限
        """
        from app import run_inputs as run_inputs_module

        text = (message or '').strip()
        if not text:
            raise run_inputs_module.InputRejected('input_empty', '补充内容不能为空')
        if len(text) > max_chars:
            raise run_inputs_module.InputRejected(
                'input_too_long', f'补充内容过长（上限 {max_chars} 字符）')
        digest = payload_hash or run_inputs_module.payload_hash(text)

        async with self._lease() as conn:
            # 锁 run 行：与"动作准入/计划提交/最终结算"争用的是同一把行锁，
            # 因此"补充先提交还是旧动作先准入"有唯一裁决者（文档 §5 第九步）。
            row = await self._one(
                conn, 'SELECT * FROM runs WHERE run_id=%s FOR UPDATE', (run_id,))
            if row is None:
                raise run_inputs_module.InputRejected('run_missing', '运行不存在')
            run = dict(row)
            owner_task = run['task_id']
            if task_id and task_id != owner_task:
                # 客户端拿着别的任务的 run_id 来发补充：不能转发到新 run
                raise run_inputs_module.InputRejected(
                    'run_not_current', '该运行不属于这个任务')
            meta = await self._one(
                conn, 'SELECT current_run_id FROM tasks_meta WHERE task_id=%s',
                (owner_task,))
            if meta is not None and meta.get('current_run_id'):
                if str(meta['current_run_id']) != str(run_id):
                    raise run_inputs_module.InputRejected(
                        'run_not_current', '该运行已被同一任务的新运行取代')
            status = str(run.get('status') or '')

            # 幂等先行：**先查原回执，再看状态**。否则"响应丢失后重试"会因为任务
            # 刚好收尾而被误报成"未接收"（文档 §3 第二步）。
            previous = await self._one(
                conn, 'SELECT * FROM run_inputs WHERE run_id=%s AND request_id=%s',
                (run_id, request_id))
            if previous is not None:
                item = dict(previous)
                if item.get('payload_hash') and item['payload_hash'] != digest:
                    raise run_inputs_module.InputConflict(
                        '同一个 request_id 提交了不同内容')
                processing = await self._one(
                    conn, 'SELECT * FROM run_input_processing WHERE input_id=%s',
                    (item['input_id'],))
                latest = await self._one(
                    conn, 'SELECT intent_version, processed_intent_version '
                    'FROM runs WHERE run_id=%s', (run_id,))
                return {
                    'input_id': item['input_id'], 'run_id': run_id,
                    'task_id': owner_task, 'input_seq': int(item['input_seq']),
                    'message': item['message'],
                    # 重放不能把"接受版本"改成最新值（文档 §3 第一步）
                    'intent_version': int(item['intent_version']),
                    'processed_intent_version': int(
                        (latest or {}).get('processed_intent_version') or 0),
                    'status': (processing or {}).get('status') or 'accepted',
                    'replayed': True, 'duplicate_message': False,
                }

            if status in ('succeeded', 'failed', 'cancelled', 'interrupted',
                          'partially_succeeded'):
                code = 'run_cancelled' if status == 'cancelled' else 'run_finished'
                raise run_inputs_module.InputRejected(
                    code, f'该运行已经结束（{status}），请另开一轮')
            if status not in self.INPUT_ACCEPTING_STATUSES:
                raise run_inputs_module.InputRejected(
                    'run_not_accepting', f'该运行当前状态为 {status}，不能接受补充')

            pending = await self._one(
                conn, 'SELECT COUNT(*) AS n FROM run_input_processing p '
                'JOIN run_inputs i ON i.input_id=p.input_id '
                "WHERE i.run_id=%s AND p.status='accepted'", (run_id,))
            if int((pending or {}).get('n') or 0) >= max_pending:
                raise run_inputs_module.InputBacklog(
                    f'待处理补充已达上限（{max_pending} 条），等这批处理完再发')

            seq_row = await self._one(
                conn, 'SELECT COALESCE(MAX(input_seq), 0) + 1 AS next_seq '
                'FROM run_inputs WHERE run_id=%s', (run_id,))
            input_seq = int(seq_row['next_seq'])
            version_row = await self._one(
                conn, 'SELECT COALESCE(intent_version, 1) AS v, '
                'COALESCE(processed_intent_version, 0) AS pv '
                'FROM runs WHERE run_id=%s', (run_id,))
            intent_version = int(version_row['v']) + 1
            input_id = 'inp_' + uuid.uuid4().hex[:16]
            stamp = now()
            await self._run(
                conn, 'INSERT INTO run_inputs (input_id, run_id, input_seq, '
                'request_id, message, payload_hash, intent_version, created_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (input_id, run_id, input_seq, request_id, text, digest,
                 intent_version, stamp))
            await self._run(
                conn, 'INSERT INTO run_input_processing (input_id, status, '
                "processed_plan_version, error_code, updated_at) "
                "VALUES (%s, 'accepted', 0, '', %s)", (input_id, stamp))
            await self._run(
                conn, 'UPDATE runs SET intent_version=%s, updated_at=%s '
                'WHERE run_id=%s', (intent_version, stamp, run_id))
            await self._append_event_in_tx(conn, owner_task, {
                'type': 'action', 'icon': '📝', 'label': '收到补充要求',
                'detail': text[:120], 'input_received': True,
                'input_id': input_id, 'input_seq': input_seq,
                'intent_version': intent_version, 'run_id': run_id,
            })
            return {
                'input_id': input_id, 'run_id': run_id, 'task_id': owner_task,
                'input_seq': input_seq, 'message': text,
                'intent_version': intent_version,
                'processed_intent_version': int(version_row['pv']),
                'status': 'accepted', 'replayed': False,
                'duplicate_message': False,
            }

    async def run_inputs(self, run_id: str, *, after_seq: int = 0,
                         limit: int = 50) -> list[dict]:
        """按序号回读补充输入及其处理状态（已接受但还没有消费者的也要看到）。"""
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT i.*, p.status AS processing_status, '
                'p.processed_plan_version, p.error_code, p.updated_at AS processed_at '
                'FROM run_inputs i '
                'LEFT JOIN run_input_processing p ON p.input_id=i.input_id '
                'WHERE i.run_id=%s AND i.input_seq > %s '
                'ORDER BY i.input_seq LIMIT %s',
                (run_id, int(after_seq or 0), int(limit or 50)))
        return [dict(r) for r in rows]

    async def pending_run_inputs(self, run_id: str) -> list[dict]:
        """还没纳入任何计划提交的补充输入（新执行者据此继续处理）。"""
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT i.* FROM run_inputs i '
                'LEFT JOIN run_input_processing p ON p.input_id=i.input_id '
                "WHERE i.run_id=%s AND COALESCE(p.status, 'accepted') = 'accepted' "
                'ORDER BY i.input_seq', (run_id,))
        return [dict(r) for r in rows]

    async def run_intent(self, run_id: str) -> dict:
        """run 的三个版本 + 待处理输入数（一次读，保证互相自洽）。"""
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT COALESCE(intent_version, 1) AS intent_version, '
                'COALESCE(processed_intent_version, 0) AS processed_intent_version, '
                'COALESCE(plan_version, 0) AS plan_version, status '
                'FROM runs WHERE run_id=%s', (run_id,))
            pending = await self._one(
                conn, 'SELECT COUNT(*) AS n FROM run_input_processing p '
                'JOIN run_inputs i ON i.input_id=p.input_id '
                "WHERE i.run_id=%s AND p.status='accepted'", (run_id,))
        data = dict(row or {})
        data['pending_input_count'] = int((pending or {}).get('n') or 0)
        return data

    # ---------- 自主决策与预算（文档 harness-observe-decide §9、§12） ----------

    async def reserve_decision(self, context, *, run_id: str, decision_key: str,
                               reason: str = 'observation', intent_version: int = 0,
                               base_plan_version: int = 0, observation_seq: int = 0,
                               input_snapshot: str = '',
                               max_decisions: int = 12) -> dict:
        """**发请求之前**预留一次决策（含预算检查）。

        返回 `{decision_id, replay: bool, status, ...}`：

        - `replay=True`：同一个 decision_key 已经预留过（进程重启/重试），
          调用方要么复用它的候选，要么按已有状态收尾——不能靠换 ID 绕过预算；
        - 预算不足时抛 `DecisionBudgetExceeded`：保存已完成事实，等人显式继续。

        为什么先预留：模型请求是有成本的外部调用。先记账再请求，才可能做到
        "超时的请求也算消耗"（文档 §12：不能靠只统计成功返回来漏掉超时请求）。
        """
        from app import observation as observation_module

        async with self._lease() as conn:
            row = await self._owned(conn, context, run_id=run_id)
            if row is None:
                raise DecisionBudgetExceeded('运行不存在')
            current_intent = int(row.get('intent_version') or 1)
            if intent_version and int(intent_version) != current_intent:
                # 用户要求变了：旧候选直接作废，不预留
                from app import execution_control
                raise execution_control.IntentChanged(
                    run_id, int(intent_version), current_intent)
            existing = await self._one(
                conn, 'SELECT * FROM run_decisions WHERE run_id=%s AND decision_key=%s',
                (run_id, decision_key))
            if existing is not None:
                data = dict(existing)
                return {'decision_id': data['decision_id'], 'replay': True,
                        'status': data['status'],
                        'decision_kind': data['decision_kind'],
                        'committed_plan_version': int(
                            data.get('committed_plan_version') or 0),
                        'observation_seq': int(data.get('observation_seq') or 0)}
            used = await self._one(
                conn, 'SELECT COUNT(*) AS n FROM run_decisions WHERE run_id=%s',
                (run_id,))
            if int((used or {}).get('n') or 0) >= int(max_decisions or 0):
                raise DecisionBudgetExceeded(
                    f'本次运行已用完 {max_decisions} 次自主决策预算')
            decision_id = 'dec_' + uuid.uuid4().hex[:16]
            stamp = now()
            await self._run(
                conn, 'INSERT INTO run_decisions (decision_id, run_id, decision_key, '
                'reason, status, intent_version, base_plan_version, observation_seq, '
                'input_snapshot, created_at, updated_at) '
                "VALUES (%s, %s, %s, %s, 'reserved', %s, %s, %s, %s, %s, %s)",
                (decision_id, run_id, decision_key, reason, int(intent_version or 0),
                 int(base_plan_version or 0), int(observation_seq or 0),
                 str(input_snapshot or '')[:8000], stamp, stamp))
            # 决策也进事件流：前端要知道"系统在看结果、不是卡住了"
            await self._append_event_in_tx(conn, row['task_id'], {
                'type': 'action', 'icon': '🔎', 'label': '正在根据结果决定下一步',
                'detail': reason, 'decision_reserved': True,
                'decision_id': decision_id, 'run_id': run_id,
                'observation_seq': int(observation_seq or 0),
            })
            return {'decision_id': decision_id, 'replay': False,
                    'status': 'reserved', 'decision_kind': '',
                    'committed_plan_version': 0,
                    'observation_seq': int(observation_seq or 0)}

    async def finish_decision(self, decision_id: str, *, status: str,
                              decision_kind: str = '',
                              candidate_plan_hash: str = '',
                              committed_plan_version: int = 0,
                              provider_requests: int = 0,
                              usage: dict | None = None,
                              error_code: str = '') -> None:
        """记录决策结局（候选保存/提交成功/等待/失败）。"""
        async with self._lease() as conn:
            await self._run(
                conn, 'UPDATE run_decisions SET status=%s, decision_kind=%s, '
                'candidate_plan_hash=%s, committed_plan_version=%s, '
                'provider_requests=provider_requests+%s, usage_json=%s, '
                'error_code=%s, updated_at=%s WHERE decision_id=%s',
                (status, decision_kind, candidate_plan_hash,
                 int(committed_plan_version or 0), int(provider_requests or 0),
                 json.dumps(usage or {}, ensure_ascii=False, default=str),
                 error_code, now(), decision_id))

    async def run_decisions(self, run_id: str, limit: int = 20) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT * FROM run_decisions WHERE run_id=%s '
                'ORDER BY created_at DESC LIMIT %s', (run_id, int(limit)))
        out = []
        for row in rows:
            item = dict(row)
            try:
                item['usage'] = json.loads(item.pop('usage_json') or '{}')
            except ValueError:
                item['usage'] = {}
            out.append(item)
        return out

    async def observation_state(self, run_id: str) -> dict:
        """恢复入口要的那几个数：水位、下一路由、待观察结果数。"""
        async with self._lease() as conn:
            run = await self._one(
                conn, 'SELECT COALESCE(observation_seq,0) AS observation_seq, '
                'COALESCE(plan_version,0) AS plan_version, '
                'COALESCE(intent_version,1) AS intent_version, status, lease_owner '
                'FROM runs WHERE run_id=%s', (run_id,))
            snapshot = await self._one(
                conn, 'SELECT next_route, observation_seq FROM run_snapshots '
                'WHERE run_id=%s', (run_id,))
            pending = await self._one(
                conn, "SELECT COUNT(*) AS n FROM run_decisions WHERE run_id=%s "
                "AND status='reserved'", (run_id,))
        data = dict(run or {})
        data['next_route'] = (snapshot or {}).get('next_route') or ''
        data['pending_decisions'] = int((pending or {}).get('n') or 0)
        return data

    async def consume_budget(self, context, *, run_id: str, key: str,
                             amount: int = 1, limit: int | None = None) -> int:
        """原子累加运行级用量，超过上限抛 `BudgetExceeded`。

        为什么要落库：`state.usage` 只是展示统计，进程重启就没了；而"这次运行
        到底用掉了多少预算"必须跨重启、跨执行者累计（文档 §12）。
        """
        async with self._lease() as conn:
            row = await self._owned(conn, context, run_id=run_id)
            if row is None:
                raise BudgetExceeded('运行不存在')
            # `_owned` 返回的是**原始行**（列名是 *_json），不是解码后的字典：
            # 这里按列名解析，否则每次累加都会把上一种用量覆盖掉（看起来像"只记录了最后一次"）。
            try:
                consumed = json.loads(row.get('consumed_json') or '{}')
            except ValueError:
                consumed = {}
            try:
                budget = json.loads(row.get('budget_json') or '{}')
            except ValueError:
                budget = {}
            used = int(consumed.get(key) or 0)
            cap = limit
            if cap is None:
                cap = budget.get(key)
            if cap is not None and used + int(amount) > int(cap):
                raise BudgetExceeded(
                    f'运行预算用尽（{key}: {used}/{cap}）')
            consumed[key] = used + int(amount)
            await self._run(
                conn, 'UPDATE runs SET consumed_json=%s, updated_at=%s WHERE run_id=%s',
                (json.dumps(consumed, ensure_ascii=False), now(), run_id))
            return consumed[key]

    async def mark_inputs_applied(self, conn, run_id: str, *, intent_version: int,
                                  plan_version: int) -> int:
        """把被这份计划覆盖的输入标成 applied（在计划提交事务里调用）。

        `applied` 只表示"计划已纳入这条要求"，**不表示业务效果已完成**——
        这两件事混用会让用户在计划刚提交时就看到"已完成"。
        """
        cur = await self._run(
            conn, 'UPDATE run_input_processing p SET status=%s, '
            'processed_plan_version=%s, updated_at=%s '
            'FROM run_inputs i WHERE p.input_id=i.input_id AND i.run_id=%s '
            "AND p.status='accepted' AND i.intent_version <= %s",
            ('applied', int(plan_version), now(), run_id, int(intent_version)))
        return int(getattr(cur, 'rowcount', 0) or 0)

    async def release_lease(self, run_id: str, worker_id: str,
                            epoch: int | None = None) -> None:
        """执行者主动交还租约。带 owner（可选 epoch）条件，旧代次不能覆盖新持有者。"""
        condition = 'run_id=%s AND lease_owner=%s'
        params: tuple = (now(), run_id, worker_id)
        if epoch is not None:
            condition += ' AND lease_epoch=%s'
            params += (int(epoch),)
        async with self._lease() as conn:
            await self._run(conn, 'UPDATE runs SET lease_owner=NULL, lease_expires_at=NULL, '
                            f'updated_at=%s WHERE {condition}', params)
            await conn.commit()

    async def renew_lease(self, run_id: str, worker_id: str, lease_seconds: float = 60.0,
                    now_ts: float | None = None, epoch: int | None = None) -> bool:
        """续租。返回 False = **这次执行已经不该继续了**。

        两个条件缺一不可：epoch 与领取时一致（旧执行者不能续别人的租约）、
        租约尚未过期（旧执行者不能把过期租约"复活"，那等于自己发一张新租约）。
        """
        current = now_ts if now_ts is not None else time.time()
        condition = ("run_id=%s AND lease_owner=%s AND status='running' "
                     'AND lease_expires_at IS NOT NULL AND lease_expires_at > %s')
        params: tuple = (current + lease_seconds, now(), run_id, worker_id, current)
        if epoch is not None:
            condition += ' AND lease_epoch=%s'
            params += (int(epoch),)
        async with self._lease() as conn:
            cur = await self._run(
                conn, 'UPDATE runs SET lease_expires_at=%s, updated_at=%s '
                f'WHERE {condition}', params)
            await conn.commit()
            return cur.rowcount > 0

    # ---------- 计划与步骤 ----------

    async def save_plan(self, run_id: str, version: int, *, goal: str,
                  steps: list[dict], reason: str = '') -> None:
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO plans '
                '(run_id, version, goal, steps_json, reason, created_at) '
                'VALUES (%s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (run_id, version) DO UPDATE SET goal=EXCLUDED.goal, '
                'steps_json=EXCLUDED.steps_json, reason=EXCLUDED.reason',
                (run_id, version, goal,
                 json.dumps(steps, ensure_ascii=False), reason, now()))
            await conn.commit()

    async def plan(self, run_id: str, version: int | None = None) -> dict | None:
        async with self._lease() as conn:
            if version is None:
                row = await self._one(conn, 'SELECT * FROM plans WHERE run_id=%s ORDER BY version DESC LIMIT 1',
                    (run_id,))
            else:
                row = await self._one(conn, 'SELECT * FROM plans WHERE run_id=%s AND version=%s',
                    (run_id, version))
        if row is None:
            return None
        data = dict(row)
        data['steps'] = json.loads(data.pop('steps_json'))
        return data

    async def save_steps(self, run_id: str, plan_version: int, steps: list[dict]) -> None:
        async with self._lease() as conn:
            for index, step in enumerate(steps):
                await self._run(conn, 'INSERT INTO steps '
                    '(run_id, plan_version, step_id, seq, tool_key, status, '
                    'inputs_json, output_json, action_id) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) '
                    'ON CONFLICT (run_id, plan_version, step_id) DO UPDATE SET '
                    'tool_key=EXCLUDED.tool_key, status=EXCLUDED.status',
                    (run_id, plan_version, step['step_id'], index,
                     step.get('tool_key') or step.get('tool') or '',
                     step.get('status', 'pending'),
                     json.dumps(step.get('inputs') or {}, ensure_ascii=False),
                     None, None))
            await conn.commit()

    async def steps_of(self, run_id: str, plan_version: int | None = None) -> list[dict]:
        async with self._lease() as conn:
            if plan_version is None:
                row = await self._one(conn, 'SELECT MAX(plan_version) AS v FROM steps WHERE run_id=%s',
                    (run_id,))
                plan_version = (row['v'] if row else 0) or 0
            rows = await self._all(conn, 'SELECT * FROM steps WHERE run_id=%s AND plan_version=%s ORDER BY seq',
                (run_id, plan_version))
        out = []
        for r in rows:
            item = dict(r)
            item['inputs'] = json.loads(item.pop('inputs_json') or '{}')
            item['output'] = json.loads(item.pop('output_json') or 'null')
            out.append(item)
        return out

    async def update_step(self, run_id: str, plan_version: int, step_id: str, **fields) -> None:
        allowed = {'status', 'output', 'action_id', 'error'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if 'output' in updates:
            updates['output_json'] = json.dumps(updates.pop('output'),
                                                ensure_ascii=False)
        if updates.get('status') == 'running':
            updates['started_at'] = now()
        if updates.get('status') in {'succeeded', 'failed', 'blocked', 'unknown',
                                     'cancelled', 'superseded'}:
            updates['ended_at'] = now()
        columns = ', '.join(f'{k}=%s' for k in updates)
        async with self._lease() as conn:
            await self._run(conn, f'UPDATE steps SET {columns} WHERE run_id=%s AND plan_version=%s '
                'AND step_id=%s',
                (*updates.values(), run_id, plan_version, step_id))
            await conn.commit()

    # ---------- 动作账本（文档 §8） ----------

    async def find_action(self, idempotency_key: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM actions WHERE idempotency_key=%s',
                (idempotency_key,))
        return _decode_action(dict(row)) if row else None

    async def create_action(self, action_id: str, *, run_id: str, plan_version: int,
                      step_id: str, tool_key: str, args_hash: str,
                      idempotency_key: str, effect: str = 'unknown',
                      authorized_by: str = '') -> dict:
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO actions (action_id, run_id, plan_version, step_id, '
                'tool_key, args_hash, idempotency_key, effect, status, '
                'authorized_by, created_at, updated_at) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'prepared', %s, %s, %s)",
                (action_id, run_id, plan_version, step_id, tool_key, args_hash,
                 idempotency_key, effect, authorized_by, stamp, stamp))
            await conn.commit()
        return await self.action(action_id)

    async def action(self, action_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM actions WHERE action_id=%s', (action_id,))
        return _decode_action(dict(row)) if row else None

    async def update_action(self, action_id: str, **fields) -> None:
        allowed = {'status', 'receipt_json'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if 'receipt_json' in updates and not isinstance(updates['receipt_json'], str):
            updates['receipt_json'] = json.dumps(updates['receipt_json'],
                                                ensure_ascii=False)
        columns = ', '.join(f'{k}=%s' for k in updates)
        async with self._lease() as conn:
            await self._run(conn, f'UPDATE actions SET {columns}, updated_at=%s WHERE action_id=%s',
                (*updates.values(), now(), action_id))
            await conn.commit()

    # ---------- 执行推进（受租约保护，文档阶段 B/C） ----------

    async def commit_plan(self, context, *, run_id: str, version: int, goal: str,
                          steps: list[dict], state: dict,
                          artifact_namespace: str = '',
                          expected_intent_version: int | None = None,
                          intent_version: int | None = None,
                          base_plan_version: int | None = None) -> dict:
        """把计划、步骤、快照一起提交，并按**用户要求版本**做 CAS。

        一个事务里完成，失败就"零工具调用"：宁可任务失败，也不能出现"远端已经
        跑了两步、库里却没有计划"——那种状态根本没法恢复。

        `expected_intent_version` 是**这份计划的依据版本**。提交时若用户又补了一句
        要求（`runs.intent_version` 变了），这里抛 `IntentChanged`：旧候选计划直接
        作废，绝不能拿"按 v2 生成的计划"去执行、再声称它覆盖了 v3。

        `intent_version` 是这份计划**覆盖到**的版本，提交成功时同步写进
        `runs.processed_intent_version`，并把被它覆盖的输入标成 applied。
        """
        from app import execution_control

        async with self._lease() as conn:
            row = await self._owned(conn, context, run_id=run_id,
                                    require_status=('running', 'queued'))
            if row is None:
                raise execution_control.LeaseLost(run_id, '运行不存在')
            current_intent = int(row.get('intent_version') or 1)
            if (expected_intent_version is not None
                    and int(expected_intent_version) != current_intent):
                raise execution_control.IntentChanged(
                    run_id, int(expected_intent_version), current_intent)
            if (base_plan_version is not None
                    and int(row.get('plan_version') or 0)
                    and int(row.get('plan_version') or 0) != int(base_plan_version)):
                # 计划已经被别人推进过：不能覆盖成更旧的版本
                raise execution_control.IntentChanged(
                    run_id, int(base_plan_version), current_intent)
            covered = int(intent_version if intent_version is not None
                          else current_intent)
            await self._run(
                conn, 'INSERT INTO plans (run_id, version, goal, steps_json, reason, '
                'created_at, intent_version, base_plan_version) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (run_id, version) DO UPDATE SET goal=EXCLUDED.goal, '
                'steps_json=EXCLUDED.steps_json, reason=EXCLUDED.reason, '
                'intent_version=EXCLUDED.intent_version, '
                'base_plan_version=EXCLUDED.base_plan_version',
                (run_id, int(version), goal,
                 json.dumps(steps, ensure_ascii=False), '', now(),
                 covered, int(base_plan_version or 0)))
            # 旧计划里没执行完的步骤标 superseded：审计要能看出"谁被谁取代了"
            if int(version) > 1:
                await self._run(
                    conn, "UPDATE steps SET status='superseded' WHERE run_id=%s "
                    "AND plan_version < %s AND status IN ('pending','running')",
                    (run_id, int(version)))
            for seq, step in enumerate(steps, 1):
                await self._run(
                    conn, 'INSERT INTO steps (run_id, plan_version, step_id, seq, '
                    'tool_key, status, inputs_json) '
                    "VALUES (%s, %s, %s, %s, %s, 'pending', %s) "
                    'ON CONFLICT (run_id, plan_version, step_id) DO NOTHING',
                    (run_id, int(version), step.get('step_id'), seq,
                     step.get('tool_key') or '',
                     json.dumps(step.get('args') or {}, ensure_ascii=False)))
            await self._run(
                conn, 'UPDATE runs SET plan_version=%s, '
                'processed_intent_version=GREATEST(COALESCE(processed_intent_version,0), %s), '
                'updated_at=%s WHERE run_id=%s',
                (int(version), covered, now(), run_id))
            applied = await self.mark_inputs_applied(
                conn, run_id, intent_version=covered, plan_version=int(version))
            await self.save_snapshot(run_id, plan_version=version, state=state,
                                     next_step_id=(steps[0].get('step_id') if steps else ''),
                                     artifact_namespace=artifact_namespace, conn=conn,
                                     intent_version=covered)
            return {'plan_version': int(version), 'intent_version': covered,
                    'applied_inputs': applied}

    async def admit_action(self, context, *, run_id: str, plan_version: int,
                           step_id: str, tool_key: str, args_hash: str,
                           idempotency_key: str, effect: str = 'unknown',
                           action_id: str, attempt_id: str,
                           require_pause_free: bool = True,
                           expected_intent_version: int | None = None,
                           decision_intent_version: int | None = None) -> dict:
        """**原子地**取得这次工具调用的准入资格。

        同一事务内按固定顺序做四件事：校验租约与控制请求 → 查/建 action →
        建 attempt 并置 in_flight → 交还。锁顺序固定（run → action → attempt），
        避免两个执行者在不同顺序上互等。

        与 `pause` 的先后关系由同一行锁裁决（文档 §7.2）：
        - pause 先提交 → 这里看到 `pause_requested`，**不授予调用资格**；
        - 这里先准入 → 该步视为在途，pause 只禁止后续动作。
        """
        from app import execution_control

        async with self._lease() as conn:
            row = await self._owned(conn, context, run_id=run_id,
                                    require_status=('running',))
            if row is None:
                return {'status': 'no_run'}
            if row.get('cancel_requested_at'):
                raise execution_control.ControlStopped('运行已被取消', 'cancelled')
            if require_pause_free and row.get('pause_requested'):
                raise execution_control.ControlStopped('已请求暂停，不再准入新动作',
                                                       'paused')
            # 用户要求版本必须与这份计划的依据版本**相等**才准入（文档 §5 第九步）。
            # 注意这是"新动作"的门禁：已准入动作的回执照样要能结算——
            # 真实发生过的效果不能因为用户改主意就丢掉。
            current_intent = int(row.get('intent_version') or 1)
            if (expected_intent_version is not None
                    and int(expected_intent_version) != current_intent):
                raise execution_control.IntentChanged(
                    run_id, int(expected_intent_version), current_intent)

            existing = await self._one(
                conn, 'SELECT * FROM actions WHERE idempotency_key=%s',
                (idempotency_key,))
            if existing is not None:
                data = _decode_action(dict(existing))
                # 同一个动作重复准入：已成功就复用回执；在途/未知则阻断；
                # **只到 prepared 说明请求还没发出去**（旧版本留下的行，或有人在
                # 准入与派发之间中断）：可以沿同一个动作身份重新准入。
                if data['status'] == 'prepared':
                    restamp = now()
                    await self._run(
                        conn, "UPDATE actions SET status='in_flight', updated_at=%s "
                        'WHERE action_id=%s', (restamp, data['action_id']))
                    await self._run(
                        conn, 'INSERT INTO action_attempts (attempt_id, action_id, '
                        'started_at) VALUES (%s, %s, %s) '
                        'ON CONFLICT (attempt_id) DO NOTHING',
                        (attempt_id, data['action_id'], restamp))
                    await self._run(
                        conn, "UPDATE steps SET status='running', "
                        'started_at=COALESCE(started_at, %s), action_id=%s '
                        'WHERE run_id=%s AND plan_version=%s AND step_id=%s',
                        (restamp, data['action_id'], run_id, int(plan_version),
                         step_id))
                    return {'status': 'admitted', 'action_id': data['action_id'],
                            'attempt_id': attempt_id}
                return {'status': data['status'], 'action': data,
                        'replay': data.get('receipt') if data['status'] == 'succeeded'
                        else None}

            stamp = now()
            await self._run(
                conn, 'INSERT INTO actions (action_id, run_id, plan_version, step_id, '
                'tool_key, args_hash, idempotency_key, effect, status, authorized_by, '
                'created_at, updated_at, decision_intent_version) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'in_flight', '', %s, %s, %s)",
                (action_id, run_id, int(plan_version), step_id, tool_key, args_hash,
                 idempotency_key, effect, stamp, stamp,
                 int(decision_intent_version if decision_intent_version is not None
                     else current_intent)))
            await self._run(
                conn, 'INSERT INTO action_attempts (attempt_id, action_id, started_at) '
                'VALUES (%s, %s, %s) ON CONFLICT (attempt_id) DO NOTHING',
                (attempt_id, action_id, stamp))
            await self._run(
                conn, "UPDATE steps SET status='running', "
                'started_at=COALESCE(started_at, %s), action_id=%s '
                'WHERE run_id=%s AND plan_version=%s AND step_id=%s',
                (stamp, action_id, run_id, int(plan_version), step_id))
            return {'status': 'admitted', 'action_id': action_id,
                    'attempt_id': attempt_id}

    async def settle_action(self, context, *, run_id: str, plan_version: int,
                            step_id: str, action_id: str,
                            attempt_id: str | None, status: str,
                            receipt: dict | None = None,
                            error_type: str | None = None,
                            step_status: str | None = None,
                            state: dict | None = None,
                            next_step_id: str = '') -> dict:
        """原子结算：动作 + attempt + 步骤 + 快照一次提交。

        "工具已返回但提交失败"时**不能推进内存游标继续下一步**：调用方会收到
        异常，任务停在这里，重放/核实时以库里为准。
        """
        stamp = now()
        async with self._lease() as conn:
            await self._owned(conn, context, run_id=run_id,
                              require_status=('running', 'paused'))
            await self._run(
                conn, 'UPDATE actions SET status=%s, receipt_json=%s, updated_at=%s '
                'WHERE action_id=%s',
                (status,
                 json.dumps(receipt or {}, ensure_ascii=False, default=str),
                 stamp, action_id))
            if attempt_id:
                await self._run(
                    conn, 'UPDATE action_attempts SET ended_at=%s, error_type=%s '
                    'WHERE attempt_id=%s', (stamp, error_type, attempt_id))
            if step_status:
                terminal = ('succeeded', 'failed', 'blocked', 'unknown',
                            'cancelled', 'superseded')
                ended = stamp if step_status in terminal else None
                await self._run(
                    conn, 'UPDATE steps SET status=%s, '
                    'ended_at=COALESCE(%s, ended_at), output_json=COALESCE(%s, output_json) '
                    'WHERE run_id=%s AND plan_version=%s AND step_id=%s',
                    (step_status, ended,
                     json.dumps(receipt, ensure_ascii=False, default=str)
                     if receipt else None,
                     run_id, int(plan_version), step_id))
            # 结果水位与结果同事务推进：判断"决策覆盖到哪一步"只能看它，
            # 用墙钟时间会在跨进程时给出错误的新旧关系（文档 §9）。
            water = await self._one(
                conn, 'UPDATE runs SET observation_seq=COALESCE(observation_seq,0)+1 '
                'WHERE run_id=%s RETURNING observation_seq', (run_id,))
            if state is not None:
                await self.save_snapshot(run_id, plan_version=plan_version, state=state,
                                         next_step_id=next_step_id, conn=conn,
                                         observation_seq=int((water or {}).get('observation_seq') or 0))
            return {'action_id': action_id, 'status': status,
                    'observation_seq': int((water or {}).get('observation_seq') or 0)}

    async def finish_run_owned(self, context, *, run_id: str, status: str,
                               error: str | None = None,
                               state: dict | None = None,
                               expected_intent_version: int | None = None,
                               plan_version: int | None = None,
                               compensated_at: str | None = None,
                               observation_seq: int | None = None,
                               next_route: str = '',
                               require_no_pending_inputs: bool = False) -> bool:
        """带租约与**要求版本**条件地写 run 终态。返回 False = 已失去执行权(不写)。

        收尾也必须过版本门禁（文档 §5 第十一步）：旧版本跑出来的"完成"不能
        盖在新要求上面。若用户在收尾前补了一句要求，这里抛 `IntentChanged`——
        调用方应当回到消费循环，而不是把这个 run 标成结束。
        """
        from app import execution_control

        async with self._lease() as conn:
            try:
                row = await self._owned(conn, context, run_id=run_id)
            except execution_control.LeaseLost:
                return False
            if row is None:
                return False
            current_intent = int(row.get('intent_version') or 1)
            if (expected_intent_version is not None
                    and int(expected_intent_version) != current_intent):
                raise execution_control.IntentChanged(
                    run_id, int(expected_intent_version), current_intent)
            if require_no_pending_inputs:
                pending = await self._one(
                    conn, 'SELECT COUNT(*) AS n FROM run_input_processing p '
                    'JOIN run_inputs i ON i.input_id=p.input_id '
                    "WHERE i.run_id=%s AND p.status='accepted'", (run_id,))
                if int((pending or {}).get('n') or 0):
                    # 有已接受却没人处理的补充：不许声称结束（否则它永远没人管）
                    raise execution_control.IntentChanged(
                        run_id, current_intent, current_intent + 1)
            sets = ['status=%s', 'error=%s', 'updated_at=%s',
                    'lease_owner=NULL', 'lease_expires_at=NULL']
            args: list = [status, error, now()]
            if compensated_at is not None:
                sets.append('compensated_at=%s')
                args.append(compensated_at)
            if plan_version is not None:
                sets.append('plan_version=%s')
                args.append(int(plan_version))
            args.append(run_id)
            await self._run(conn, f'UPDATE runs SET {", ".join(sets)} '
                            'WHERE run_id=%s', tuple(args))
            if state is not None:
                await self.save_snapshot(
                    run_id,
                    plan_version=int(plan_version
                                     or (context.plan_version if context else 1)),
                    state=state, conn=conn, intent_version=current_intent,
                    processed_intent_version=int(
                        row.get('processed_intent_version') or 0),
                    observation_seq=observation_seq, next_route=next_route)
            return True

    async def pause_at_boundary(self, context, *, run_id: str, plan_version: int,
                                state: dict, next_step_id: str = '') -> bool:
        """在安全边界暂停：写 paused + 快照，交还租约。返回 False = 已失租。"""
        from app import execution_control

        async with self._lease() as conn:
            try:
                await self._owned(conn, context, run_id=run_id,
                                  require_status=('running',))
            except execution_control.LeaseLost:
                return False
            await self._run(
                conn, 'UPDATE runs SET status=%s, lease_owner=NULL, '
                'lease_expires_at=NULL, updated_at=%s WHERE run_id=%s',
                ('paused', now(), run_id))
            await self.save_snapshot(run_id, plan_version=plan_version, state=state,
                                     next_step_id=next_step_id, conn=conn)
            return True

    async def actions_of(self, run_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT * FROM actions WHERE run_id=%s ORDER BY created_at',
                (run_id,))
        return [_decode_action(dict(r)) for r in rows]

    async def add_attempt(self, attempt_id: str, action_id: str) -> None:
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO action_attempts (attempt_id, action_id, started_at) '
                'VALUES (%s, %s, %s)', (attempt_id, action_id, now()))
            await conn.commit()

    async def finish_attempt(self, attempt_id: str, *, error_type: str | None = None,
                       result_ref: str | None = None) -> None:
        async with self._lease() as conn:
            await self._run(conn, 'UPDATE action_attempts SET ended_at=%s, error_type=%s, result_ref=%s '
                'WHERE attempt_id=%s', (now(), error_type, result_ref, attempt_id))
            await conn.commit()

    async def attempts_of(self, action_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn, 'SELECT * FROM action_attempts WHERE action_id=%s ORDER BY started_at',
                (action_id,))
        return [dict(r) for r in rows]

    # ---------- 产物登记 ----------

    async def add_artifact(self, artifact_id: str, *, run_id: str, kind: str,
                     storage_key: str, mime: str = 'text/html', size: int = 0,
                     sha256: str = '') -> None:
        async with self._lease() as conn:
            await self._run(conn, 'INSERT INTO artifacts (artifact_id, run_id, kind, '
                'storage_key, mime, size, sha256, created_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (artifact_id) DO UPDATE SET size=EXCLUDED.size, '
                'sha256=EXCLUDED.sha256',
                (artifact_id, run_id, kind, storage_key, mime, size, sha256, now()))
            await conn.commit()

    async def artifact(self, artifact_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM artifacts WHERE artifact_id=%s',
                (artifact_id,))
        return dict(row) if row else None

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

    # ---------- 后台任务（outbox） ----------

    async def enqueue_job(self, job_id: str, kind: str, dedupe_key: str,
                    payload: dict | None = None) -> bool:
        """按 (kind, dedupe_key) 幂等入队；返回是否真的新入队。"""
        async with self._lease() as conn:
            cur = await self._run(conn, 'INSERT INTO background_jobs '
                '(job_id, kind, dedupe_key, payload_json, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (job_id, kind, dedupe_key,
                 json.dumps(payload or {}, ensure_ascii=False), now(), now()))
            await conn.commit()
            return cur.rowcount > 0

    async def claim_job(self, worker_id: str, lease_seconds: float = 60.0,
                  now_ts: float | None = None) -> dict | None:
        current = now_ts if now_ts is not None else time.time()
        async with self._lease() as conn:
            row = await self._one(conn, "SELECT job_id FROM background_jobs WHERE status='queued' "
                "AND (lease_expires_at IS NULL OR lease_expires_at < %s) "
                "ORDER BY created_at LIMIT 1", (current,))
            if row is None:
                return None
            job_id = row['job_id']
            await self._run(conn, "UPDATE background_jobs SET status='running', lease_owner=%s, "
                'lease_expires_at=%s, attempts=attempts+1, updated_at=%s '
                'WHERE job_id=%s',
                (worker_id, current + lease_seconds, now(), job_id))
            await conn.commit()
        return await self.job(job_id)

    async def job(self, job_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM background_jobs WHERE job_id=%s', (job_id,))
        if row is None:
            return None
        data = dict(row)
        data['payload'] = json.loads(data.pop('payload_json') or '{}')
        return data

    async def finish_job(self, job_id: str, *, error: str | None = None,
                   retry: bool = False) -> None:
        """收尾一个后台作业。

        `retry=True`：退回 queued 并用退避时间占住租约，下一轮扫描再领。
        一次失败就写 failed 等于把偶发错误当终态，且没人会知道。
        """
        async with self._lease() as conn:
            if error and retry:
                row = await self._one(conn, 'SELECT attempts FROM background_jobs WHERE job_id=%s',
                    (job_id,))
                attempts = int((row['attempts'] if row else 0) or 1)
                delay = min(300.0, 5.0 * (2 ** min(attempts, 6)))
                await self._run(conn, "UPDATE background_jobs SET status='queued', last_error=%s, "
                    'lease_owner=NULL, lease_expires_at=%s, updated_at=%s '
                    'WHERE job_id=%s',
                    (error, time.time() + delay, now(), job_id))
            else:
                await self._run(conn, "UPDATE background_jobs SET status=%s, last_error=%s, updated_at=%s "
                    'WHERE job_id=%s',
                    ('failed' if error else 'done', error, now(), job_id))
            await conn.commit()

    # ---------- 请求幂等（POST /api/tasks） ----------

    async def reserve_request_key(self, scope_key: str, *, client_key: str,
                            tenant_id: str, owner_user_id: str,
                            endpoint: str, fingerprint: str,
                            expires_at: float) -> dict:
        """原子占位。过期行先删再插，保证 TTL 之后同一个键可以复用。"""
        stamp = now()
        async with self._lease() as conn:
            await self._run(conn, 'DELETE FROM request_keys WHERE scope_key=%s AND expires_at < %s',
                (scope_key, time.time()))
            cur = await self._run(conn, 'INSERT INTO request_keys '
                '(scope_key, client_key, tenant_id, owner_user_id, endpoint, '
                ' fingerprint, created_at, expires_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (scope_key, client_key, tenant_id, owner_user_id, endpoint,
                 fingerprint, stamp, expires_at))
            # 并发请求里只有一个插入成功；其它的读到同一行但没有响应 → 处理中
            created = cur.rowcount > 0
            row = await self._one(conn, 'SELECT * FROM request_keys WHERE scope_key=%s',
                (scope_key,))
            await conn.commit()
        data = _decode_request_key(dict(row)) if row else {}
        data['created'] = created
        return data

    async def request_key(self, scope_key: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM request_keys WHERE scope_key=%s',
                (scope_key,))
        return _decode_request_key(dict(row)) if row else None

    async def complete_request_key(self, scope_key: str, response: dict, *,
                             task_id: str = '', session_id: str = '',
                             run_id: str = '') -> None:
        async with self._lease() as conn:
            await self._run(conn, 'UPDATE request_keys SET response_json=%s, task_id=%s, '
                'session_id=%s, run_id=%s WHERE scope_key=%s',
                (json.dumps(response, ensure_ascii=False), task_id or None,
                 session_id or None, run_id or None, scope_key))
            await conn.commit()

    async def release_request_key(self, scope_key: str) -> None:
        async with self._lease() as conn:
            await self._run(conn, 'DELETE FROM request_keys WHERE scope_key=%s AND response_json IS NULL',
                (scope_key,))
            await conn.commit()

    async def purge_request_keys(self, now_ts: float | None = None) -> int:
        current = now_ts if now_ts is not None else time.time()
        async with self._lease() as conn:
            cur = await self._run(conn, 'DELETE FROM request_keys WHERE expires_at < %s', (current,))
            await conn.commit()
            return cur.rowcount

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
        }
        out = {}
        async with self._lease() as conn:
            for name, sql in queries.items():
                row = await self._one(conn, sql)
                out[name] = int(row['n'] if row else 0)
        return out

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


class DecisionBudgetExceeded(RuntimeError):
    """自主决策预算用尽：保存已完成事实，等人显式继续（不是失败）。"""

    def __init__(self, message: str = '决策预算用尽') -> None:
        super().__init__(message)
        self.safe_message = message


class BudgetExceeded(RuntimeError):
    """运行级预算（provider 请求数 / 工具调用数 …）用尽。"""

    def __init__(self, message: str = '运行预算用尽') -> None:
        super().__init__(message)
        self.safe_message = message


def _decode_run(row: dict) -> dict:
    row['budget'] = json.loads(row.pop('budget_json') or '{}')
    row['consumed'] = json.loads(row.pop('consumed_json') or '{}')
    return row


def _decode_action(row: dict) -> dict:
    row['receipt'] = json.loads(row.pop('receipt_json') or 'null')
    return row


def _decode_request_key(row: dict) -> dict:
    row['response'] = json.loads(row.pop('response_json') or 'null')
    return row
