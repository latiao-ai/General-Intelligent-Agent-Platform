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
import json
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

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
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresStore:
    """PostgreSQL 仓储实现（满足 `app.persistence.Repository`）。

    连接用法：**连接池 + 每次调用借一条**。以前是"一个进程一条连接 + 一把
    threading.Lock"，把所有 DB 访问串行化——单 Worker 够用，但 SSE 写入、
    后台作业、接口查询会互相排队，长事务还会把整个进程的读写一起拖住。
    现在每次调用从池里借一条（`_lease()`），事务边界仍然显式写在方法里：
    正常提交、异常回滚、最后一定归还。

    并发正确性不靠这把锁，靠数据库：租约 + `lease_epoch` 做 fencing，
    `claim_run` 用 `FOR UPDATE SKIP LOCKED`，事件序号靠主键冲突重算。
    所以多进程、多线程同时用一个库是安全的（这点有并发用例覆盖）。
    """

    def __init__(self, dsn: str, *, pool_size: int | None = None) -> None:
        self.dsn = dsn
        size = pool_size if pool_size is not None else settings.db_pool_size
        self._pool = ConnectionPool(
            dsn,
            min_size=1,                 # 留一条热连接：启动即失败，而不是等第一次查询
            max_size=max(1, size),
            timeout=settings.db_pool_timeout,
            kwargs={'row_factory': dict_row, 'autocommit': False},
            name='agent-platform',
            open=True,
        )
        # 抛错就别假装启动成功：连不上库时要立刻在启动日志里看到原因
        self._pool.wait(timeout=max(5.0, settings.db_pool_timeout))
        self._apply_schema()
        self.interrupted = self._recover_orphans()

    @contextmanager
    def _lease(self):
        """借一条连接：块内正常结束提交，出异常回滚，无论如何归还。

        事务边界写在这里，而不是依赖连接池的隐式提交——账本、事件这类写入
        必须明确"要么提交、要么回滚"，不能靠上下文管理器的心情。
        """
        conn = self._pool.getconn()
        try:
            yield conn
            if not conn.autocommit:
                conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:                       # noqa: BLE001
                # 连接已经坏了：回滚失败不掩盖原始异常，交给池回收
                pass
            raise
        finally:
            self._pool.putconn(conn)

    def pool_stats(self) -> dict:
        """连接池现状：给 /api/health 与排障看（不涉及任何业务语义）。"""
        stats = self._pool.get_stats()
        return {'min_size': stats.get('pool_min'),
                'max_size': stats.get('pool_max'),
                'size': stats.get('pool_size'),
                'available': stats.get('pool_available'),
                'requests_waiting': stats.get('requests_waiting'),
                'usage_ms': stats.get('usage_ms')}

    def _apply_schema(self) -> None:
        """建表。语句是幂等的，每次启动都跑一遍即可当迁移。"""
        with self._lease() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()

    # ---------- 会话与消息 ----------

    def ensure_session(self, session_id: str, *, tenant_id: str = 'default',
                       owner_user_id: str = 'default') -> None:
        stamp = now()
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO sessions '
                '(session_id, tenant_id, owner_user_id, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (session_id, tenant_id, owner_user_id, stamp, stamp))
            conn.commit()

    def session(self, session_id: str) -> dict | None:
        """会话归属。接口层用它做对象权限检查（文档 §10）。"""
        with self._lease() as conn:
            row = conn.execute(
                'SELECT * FROM sessions WHERE session_id=%s', (session_id,)).fetchone()
        return dict(row) if row else None

    def append_message(self, session_id: str, role: str, content: str, *,
                       tenant_id: str = 'default',
                       owner_user_id: str = 'default') -> None:
        stamp = now()
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO messages (session_id, role, content, created_at) '
                'VALUES (%s, %s, %s, %s)', (session_id, role, content, stamp))
            conn.execute(
                'INSERT INTO sessions '
                '(session_id, tenant_id, owner_user_id, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (session_id, tenant_id, owner_user_id, stamp, stamp))
            conn.execute(
                'UPDATE sessions SET updated_at=%s WHERE session_id=%s', (stamp, session_id))
            conn.commit()

    def messages(self, session_id: str) -> list[dict]:
        with self._lease() as conn:
            rows = conn.execute(
                'SELECT role, content FROM messages WHERE session_id=%s ORDER BY id',
                (session_id,)).fetchall()
        return [{'role': r['role'], 'content': r['content']} for r in rows]

    def messages_after(self, session_id: str, after_id: int = 0) -> list[dict]:
        """取 id 大于 after_id 的消息（带 id），供压缩与增量历史使用。"""
        with self._lease() as conn:
            rows = conn.execute(
                'SELECT id, role, content FROM messages '
                'WHERE session_id=%s AND id>%s ORDER BY id',
                (session_id, after_id)).fetchall()
        return [{'id': r['id'], 'role': r['role'], 'content': r['content']}
                for r in rows]

    def memory(self, session_id: str) -> dict:
        """会话记忆；没有就是空摘要 + 空事实 + upto=0。"""
        with self._lease() as conn:
            row = conn.execute(
                'SELECT summary, facts, upto FROM session_memory WHERE session_id=%s',
                (session_id,)).fetchone()
        if row is None:
            return {'summary': '', 'facts': {}, 'upto': 0}
        return {'summary': row['summary'],
                'facts': json.loads(row['facts'] or '{}'),
                'upto': row['upto']}

    def set_memory(self, session_id: str, *, summary: str, facts: dict,
                   upto: int) -> None:
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO session_memory (session_id, summary, facts, upto, updated_at) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT(session_id) DO UPDATE SET '
                'summary=excluded.summary, facts=excluded.facts, '
                'upto=excluded.upto, updated_at=excluded.updated_at',
                (session_id, summary or '',
                 json.dumps(facts or {}, ensure_ascii=False), int(upto), now()))
            conn.commit()

    def clear_memory(self, session_id: str) -> None:
        with self._lease() as conn:
            conn.execute('DELETE FROM session_memory WHERE session_id=%s',
                               (session_id,))
            conn.commit()

    def list_sessions(self, limit: int = 50, *, tenant_id: str | None = None,
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
        with self._lease() as conn:
            rows = conn.execute(
                'SELECT s.session_id, s.created_at, s.updated_at, '
                '  (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id) '
                '    AS message_count, '
                '  (SELECT content FROM messages m WHERE m.session_id=s.session_id '
                '     AND m.role=\'user\' ORDER BY m.id LIMIT 1) AS first_message '
                f'FROM sessions s{clause} ORDER BY s.updated_at DESC LIMIT %s',
                (*params, limit)).fetchall()
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

    def create_task(self, task_id: str, session_id: str, message: str) -> None:
        stamp = now()
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO tasks (task_id, session_id, message, status, created_at, '
                'updated_at) VALUES (%s, %s, %s, %s, %s, %s)',
                (task_id, session_id, message, 'running', stamp, stamp))
            conn.commit()

    def update_task(self, task_id: str, **fields) -> None:
        allowed = {'status', 'artifact_id', 'error', 'state'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if 'state' in updates and not isinstance(updates['state'], str):
            updates['state'] = json.dumps(updates['state'], ensure_ascii=False)
        columns = ', '.join(f'{k}=%s' for k in updates)
        with self._lease() as conn:
            conn.execute(
                f'UPDATE tasks SET {columns}, updated_at=%s WHERE task_id=%s',
                (*updates.values(), now(), task_id))
            conn.commit()

    def task(self, task_id: str) -> dict | None:
        with self._lease() as conn:
            row = conn.execute(
                'SELECT * FROM tasks WHERE task_id=%s', (task_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['state'] = json.loads(data['state']) if data['state'] else None
        return data

    def tasks_of(self, session_id: str, limit: int = 50) -> list[dict]:
        with self._lease() as conn:
            # rowid 是隐式列，可用于排序但不能进索引定义。
            rows = conn.execute(
                'SELECT task_id, message, status, created_at FROM tasks '
                'WHERE session_id=%s ORDER BY created_at DESC LIMIT %s',
                (session_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def session_tasks(self, session_id: str, limit: int = 50) -> list[dict]:
        """会话里的任务，按时间**正序**——回放执行过程时要按发生顺序。

        与 tasks_of 的区别只在排序方向：那个是给"最近任务"用的。
        """
        with self._lease() as conn:
            rows = conn.execute(
                'SELECT task_id, message, status, state, created_at FROM tasks '
                'WHERE session_id=%s ORDER BY created_at ASC LIMIT %s',
                (session_id, limit)).fetchall()
        # state 一起带上：回放时要用它把通用状态派生成展示标签（outcome.label）
        out = []
        for row in rows:
            data = dict(row)
            data['state'] = json.loads(data['state']) if data['state'] else None
            out.append(data)
        return out

    # ---------- 事件日志 ----------

    def append_event(self, task_id: str, event: dict) -> int:
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
            with self._lease() as conn:
                try:
                    # 事务级建议锁：随提交/回滚自动释放，不需要额外的解锁路径
                    conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                                 (task_id,))
                    row = conn.execute(
                        statement, (task_id, payload, now(), task_id)).fetchone()
                    return int(row['seq'])
                except psycopg.errors.UniqueViolation:
                    # 建议锁下不该再发生；真发生（哈希撞了别人的锁又抢到同一号）
                    # 就重算一次，绝不返回一个可能重复的号
                    conn.rollback()
                    continue
        raise RuntimeError('事件序号竞争失败')

    def events(self, task_id: str) -> list[tuple[int, dict]]:
        with self._lease() as conn:
            rows = conn.execute(
                'SELECT seq, payload FROM events WHERE task_id=%s ORDER BY seq',
                (task_id,)).fetchall()
        return [(r['seq'], json.loads(r['payload'])) for r in rows]

    # ---------- 中台对象：task / run / plan / step / action ----------
    # 这一层是文档 §3/§6/§8/§9 的落地。旧 tasks 表仍在用（界面查询与事件回放），
    # 新执行链路逐步迁到 run 模型，两边通过 task_id 关联。

    def ensure_task_meta(self, task_id: str, *, session_id: str, goal: str,
                         tenant_id: str = 'default',
                         owner_user_id: str = 'default') -> None:
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO tasks_meta '
                '(task_id, tenant_id, owner_user_id, goal, created_at) '
                'VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (task_id, tenant_id, owner_user_id, goal, now()))
            conn.commit()

    def task_meta(self, task_id: str) -> dict | None:
        with self._lease() as conn:
            row = conn.execute(
                'SELECT * FROM tasks_meta WHERE task_id=%s', (task_id,)).fetchone()
        return dict(row) if row else None

    def create_run(self, run_id: str, task_id: str, *, budget: dict | None = None,
                   tenant_id: str = 'default', status: str = 'queued') -> None:
        stamp = now()
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO runs (run_id, task_id, tenant_id, status, '
                'budget_json, consumed_json, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (run_id, task_id, tenant_id, status,
                 json.dumps(budget or {}, ensure_ascii=False), '{}', stamp, stamp))
            conn.execute(
                'UPDATE tasks_meta SET current_run_id=%s WHERE task_id=%s',
                (run_id, task_id))
            conn.commit()

    def run(self, run_id: str) -> dict | None:
        with self._lease() as conn:
            row = conn.execute(
                'SELECT * FROM runs WHERE run_id=%s', (run_id,)).fetchone()
        return _decode_run(dict(row)) if row else None

    def update_run(self, run_id: str, **fields) -> None:
        allowed = {'status', 'plan_version', 'budget_json', 'consumed_json',
                   'cancel_requested_at', 'pause_requested', 'lease_owner',
                   'lease_expires_at', 'lease_epoch', 'error', 'compensated_at'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        for key in ('budget_json', 'consumed_json'):
            if key in updates and not isinstance(updates[key], str):
                updates[key] = json.dumps(updates[key], ensure_ascii=False)
        columns = ', '.join(f'{k}=%s' for k in updates)
        with self._lease() as conn:
            conn.execute(
                f'UPDATE runs SET {columns}, updated_at=%s WHERE run_id=%s',
                (*updates.values(), now(), run_id))
            conn.commit()

    def runs_of(self, task_id: str) -> list[dict]:
        with self._lease() as conn:
            rows = conn.execute(
                'SELECT * FROM runs WHERE task_id=%s ORDER BY created_at',
                (task_id,)).fetchall()
        return [_decode_run(dict(r)) for r in rows]

    def claim_run(self, worker_id: str, lease_seconds: float = 60.0,
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
        with self._lease() as conn:
            row = conn.execute(sql, params).fetchone()
            conn.commit()
        return _decode_run(dict(row)) if row else None

    def release_lease(self, run_id: str, worker_id: str) -> None:
        """执行者主动交还租约。带 owner 条件，旧代次不能覆盖新持有者（fencing）。"""
        with self._lease() as conn:
            conn.execute(
                'UPDATE runs SET lease_owner=NULL, lease_expires_at=NULL, '
                'updated_at=%s WHERE run_id=%s AND lease_owner=%s',
                (now(), run_id, worker_id))
            conn.commit()

    def renew_lease(self, run_id: str, worker_id: str, lease_seconds: float = 60.0,
                    now_ts: float | None = None) -> bool:
        current = now_ts if now_ts is not None else time.time()
        with self._lease() as conn:
            cur = conn.execute(
                'UPDATE runs SET lease_expires_at=%s, updated_at=%s '
                "WHERE run_id=%s AND lease_owner=%s AND status='running'",
                (current + lease_seconds, now(), run_id, worker_id))
            conn.commit()
            return cur.rowcount > 0

    # ---------- 计划与步骤 ----------

    def save_plan(self, run_id: str, version: int, *, goal: str,
                  steps: list[dict], reason: str = '') -> None:
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO plans '
                '(run_id, version, goal, steps_json, reason, created_at) '
                'VALUES (%s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (run_id, version) DO UPDATE SET goal=EXCLUDED.goal, '
                'steps_json=EXCLUDED.steps_json, reason=EXCLUDED.reason',
                (run_id, version, goal,
                 json.dumps(steps, ensure_ascii=False), reason, now()))
            conn.commit()

    def plan(self, run_id: str, version: int | None = None) -> dict | None:
        with self._lease() as conn:
            if version is None:
                row = conn.execute(
                    'SELECT * FROM plans WHERE run_id=%s ORDER BY version DESC LIMIT 1',
                    (run_id,)).fetchone()
            else:
                row = conn.execute(
                    'SELECT * FROM plans WHERE run_id=%s AND version=%s',
                    (run_id, version)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['steps'] = json.loads(data.pop('steps_json'))
        return data

    def save_steps(self, run_id: str, plan_version: int, steps: list[dict]) -> None:
        with self._lease() as conn:
            for index, step in enumerate(steps):
                conn.execute(
                    'INSERT INTO steps '
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
            conn.commit()

    def steps_of(self, run_id: str, plan_version: int | None = None) -> list[dict]:
        with self._lease() as conn:
            if plan_version is None:
                row = conn.execute(
                    'SELECT MAX(plan_version) AS v FROM steps WHERE run_id=%s',
                    (run_id,)).fetchone()
                plan_version = (row['v'] if row else 0) or 0
            rows = conn.execute(
                'SELECT * FROM steps WHERE run_id=%s AND plan_version=%s ORDER BY seq',
                (run_id, plan_version)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item['inputs'] = json.loads(item.pop('inputs_json') or '{}')
            item['output'] = json.loads(item.pop('output_json') or 'null')
            out.append(item)
        return out

    def update_step(self, run_id: str, plan_version: int, step_id: str, **fields) -> None:
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
        with self._lease() as conn:
            conn.execute(
                f'UPDATE steps SET {columns} WHERE run_id=%s AND plan_version=%s '
                'AND step_id=%s',
                (*updates.values(), run_id, plan_version, step_id))
            conn.commit()

    # ---------- 动作账本（文档 §8） ----------

    def find_action(self, idempotency_key: str) -> dict | None:
        with self._lease() as conn:
            row = conn.execute(
                'SELECT * FROM actions WHERE idempotency_key=%s',
                (idempotency_key,)).fetchone()
        return _decode_action(dict(row)) if row else None

    def create_action(self, action_id: str, *, run_id: str, plan_version: int,
                      step_id: str, tool_key: str, args_hash: str,
                      idempotency_key: str, effect: str = 'unknown',
                      authorized_by: str = '') -> dict:
        stamp = now()
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO actions (action_id, run_id, plan_version, step_id, '
                'tool_key, args_hash, idempotency_key, effect, status, '
                'authorized_by, created_at, updated_at) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'prepared', %s, %s, %s)",
                (action_id, run_id, plan_version, step_id, tool_key, args_hash,
                 idempotency_key, effect, authorized_by, stamp, stamp))
            conn.commit()
        return self.action(action_id)

    def action(self, action_id: str) -> dict | None:
        with self._lease() as conn:
            row = conn.execute(
                'SELECT * FROM actions WHERE action_id=%s', (action_id,)).fetchone()
        return _decode_action(dict(row)) if row else None

    def update_action(self, action_id: str, **fields) -> None:
        allowed = {'status', 'receipt_json'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if 'receipt_json' in updates and not isinstance(updates['receipt_json'], str):
            updates['receipt_json'] = json.dumps(updates['receipt_json'],
                                                ensure_ascii=False)
        columns = ', '.join(f'{k}=%s' for k in updates)
        with self._lease() as conn:
            conn.execute(
                f'UPDATE actions SET {columns}, updated_at=%s WHERE action_id=%s',
                (*updates.values(), now(), action_id))
            conn.commit()

    def actions_of(self, run_id: str) -> list[dict]:
        with self._lease() as conn:
            rows = conn.execute(
                'SELECT * FROM actions WHERE run_id=%s ORDER BY created_at',
                (run_id,)).fetchall()
        return [_decode_action(dict(r)) for r in rows]

    def add_attempt(self, attempt_id: str, action_id: str) -> None:
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO action_attempts (attempt_id, action_id, started_at) '
                'VALUES (%s, %s, %s)', (attempt_id, action_id, now()))
            conn.commit()

    def finish_attempt(self, attempt_id: str, *, error_type: str | None = None,
                       result_ref: str | None = None) -> None:
        with self._lease() as conn:
            conn.execute(
                'UPDATE action_attempts SET ended_at=%s, error_type=%s, result_ref=%s '
                'WHERE attempt_id=%s', (now(), error_type, result_ref, attempt_id))
            conn.commit()

    def attempts_of(self, action_id: str) -> list[dict]:
        with self._lease() as conn:
            rows = conn.execute(
                'SELECT * FROM action_attempts WHERE action_id=%s ORDER BY started_at',
                (action_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 产物登记 ----------

    def add_artifact(self, artifact_id: str, *, run_id: str, kind: str,
                     storage_key: str, mime: str = 'text/html', size: int = 0,
                     sha256: str = '') -> None:
        with self._lease() as conn:
            conn.execute(
                'INSERT INTO artifacts (artifact_id, run_id, kind, '
                'storage_key, mime, size, sha256, created_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) '
                'ON CONFLICT (artifact_id) DO UPDATE SET size=EXCLUDED.size, '
                'sha256=EXCLUDED.sha256',
                (artifact_id, run_id, kind, storage_key, mime, size, sha256, now()))
            conn.commit()

    def artifact(self, artifact_id: str) -> dict | None:
        with self._lease() as conn:
            row = conn.execute(
                'SELECT * FROM artifacts WHERE artifact_id=%s',
                (artifact_id,)).fetchone()
        return dict(row) if row else None

    # ---------- 长期记忆（文档 §12） ----------

    def remember(self, *, scope_type: str, scope_id: str, subject: str,
                 predicate: str, value: str, source: str = '',
                 created_by: str = 'default',
                 tenant_id: str = 'default') -> str | None:
        """写入一条记忆。同键有旧值则升版本并替代旧值。

        返回 None 表示这次写入被墓碑挡住（用户已明确忘记过这个事实）。
        """
        fact_key = f'{subject}|{predicate}'
        with self._lease() as conn:
            blocked = conn.execute(
                'SELECT 1 FROM memory_tombstones WHERE scope_type=%s AND '
                'scope_id=%s AND fact_key=%s', (scope_type, scope_id, fact_key)
            ).fetchone()
            if blocked:
                return None
            current = conn.execute(
                'SELECT memory_id, version FROM memories WHERE scope_type=%s AND '
                'scope_id=%s AND subject=%s AND predicate=%s AND status=%s '
                'ORDER BY version DESC LIMIT 1',
                (scope_type, scope_id, subject, predicate, 'active')).fetchone()
            memory_id = 'mem_' + uuid.uuid4().hex[:12]
            version = (current['version'] + 1) if current else 1
            stamp = now()
            conn.execute(
                'INSERT INTO memories (memory_id, tenant_id, scope_type, scope_id, '
                'subject, predicate, value, status, version, supersedes, source, '
                'created_by, created_at, updated_at) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s)",
                (memory_id, tenant_id, scope_type, scope_id, subject, predicate,
                 value, version, current['memory_id'] if current else None,
                 source, created_by, stamp, stamp))
            if current:
                conn.execute(
                    "UPDATE memories SET status='superseded', valid_to=%s, "
                    'updated_at=%s WHERE memory_id=%s',
                    (stamp, stamp, current['memory_id']))
            conn.commit()
        return memory_id

    def recall(self, scope_type: str, scope_id: str) -> list[dict]:
        """读取该作用域下仍然有效的记忆（被替代/已失效的不返回）。"""
        with self._lease() as conn:
            rows = conn.execute(
                'SELECT * FROM memories WHERE scope_type=%s AND scope_id=%s '
                "AND status='active' ORDER BY updated_at DESC",
                (scope_type, scope_id)).fetchall()
        return [dict(r) for r in rows]

    def forget(self, *, scope_type: str, scope_id: str, subject: str,
               predicate: str, reason: str = '') -> int:
        """显式遗忘：写墓碑 + 把现有事实置为 forgotten。

        墓碑的作用是阻止后台任务从同一来源再次生成这条事实——
        只删不拦，下一次摘要又会把它捡回来。
        """
        fact_key = f'{subject}|{predicate}'
        stamp = now()
        with self._lease() as conn:
            cur = conn.execute(
                "UPDATE memories SET status='forgotten', valid_to=%s, updated_at=%s "
                'WHERE scope_type=%s AND scope_id=%s AND subject=%s AND predicate=%s '
                "AND status='active'",
                (stamp, stamp, scope_type, scope_id, subject, predicate))
            changed = cur.rowcount
            conn.execute(
                'INSERT INTO memory_tombstones '
                '(tombstone_id, scope_type, scope_id, fact_key, reason, created_at) '
                'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                ('tmb_' + uuid.uuid4().hex[:10], scope_type, scope_id, fact_key,
                 reason, stamp))
            conn.commit()
        return changed

    def is_forgotten(self, scope_type: str, scope_id: str, subject: str,
                     predicate: str) -> bool:
        with self._lease() as conn:
            row = conn.execute(
                'SELECT 1 FROM memory_tombstones WHERE scope_type=%s AND '
                'scope_id=%s AND fact_key=%s',
                (scope_type, scope_id, f'{subject}|{predicate}')).fetchone()
        return row is not None

    # ---------- 后台任务（outbox） ----------

    def enqueue_job(self, job_id: str, kind: str, dedupe_key: str,
                    payload: dict | None = None) -> bool:
        """按 (kind, dedupe_key) 幂等入队；返回是否真的新入队。"""
        with self._lease() as conn:
            cur = conn.execute(
                'INSERT INTO background_jobs '
                '(job_id, kind, dedupe_key, payload_json, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (job_id, kind, dedupe_key,
                 json.dumps(payload or {}, ensure_ascii=False), now(), now()))
            conn.commit()
            return cur.rowcount > 0

    def claim_job(self, worker_id: str, lease_seconds: float = 60.0,
                  now_ts: float | None = None) -> dict | None:
        current = now_ts if now_ts is not None else time.time()
        with self._lease() as conn:
            row = conn.execute(
                "SELECT job_id FROM background_jobs WHERE status='queued' "
                "AND (lease_expires_at IS NULL OR lease_expires_at < %s) "
                "ORDER BY created_at LIMIT 1", (current,)).fetchone()
            if row is None:
                return None
            job_id = row['job_id']
            conn.execute(
                "UPDATE background_jobs SET status='running', lease_owner=%s, "
                'lease_expires_at=%s, attempts=attempts+1, updated_at=%s '
                'WHERE job_id=%s',
                (worker_id, current + lease_seconds, now(), job_id))
            conn.commit()
        return self.job(job_id)

    def job(self, job_id: str) -> dict | None:
        with self._lease() as conn:
            row = conn.execute(
                'SELECT * FROM background_jobs WHERE job_id=%s', (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['payload'] = json.loads(data.pop('payload_json') or '{}')
        return data

    def finish_job(self, job_id: str, *, error: str | None = None,
                   retry: bool = False) -> None:
        """收尾一个后台作业。

        `retry=True`：退回 queued 并用退避时间占住租约，下一轮扫描再领。
        一次失败就写 failed 等于把偶发错误当终态，且没人会知道。
        """
        with self._lease() as conn:
            if error and retry:
                row = conn.execute(
                    'SELECT attempts FROM background_jobs WHERE job_id=%s',
                    (job_id,)).fetchone()
                attempts = int((row['attempts'] if row else 0) or 1)
                delay = min(300.0, 5.0 * (2 ** min(attempts, 6)))
                conn.execute(
                    "UPDATE background_jobs SET status='queued', last_error=%s, "
                    'lease_owner=NULL, lease_expires_at=%s, updated_at=%s '
                    'WHERE job_id=%s',
                    (error, time.time() + delay, now(), job_id))
            else:
                conn.execute(
                    "UPDATE background_jobs SET status=%s, last_error=%s, updated_at=%s "
                    'WHERE job_id=%s',
                    ('failed' if error else 'done', error, now(), job_id))
            conn.commit()

    # ---------- 请求幂等（POST /api/tasks） ----------

    def reserve_request_key(self, scope_key: str, *, client_key: str,
                            tenant_id: str, owner_user_id: str,
                            endpoint: str, fingerprint: str,
                            expires_at: float) -> dict:
        """原子占位。过期行先删再插，保证 TTL 之后同一个键可以复用。"""
        stamp = now()
        with self._lease() as conn:
            conn.execute(
                'DELETE FROM request_keys WHERE scope_key=%s AND expires_at < %s',
                (scope_key, time.time()))
            cur = conn.execute(
                'INSERT INTO request_keys '
                '(scope_key, client_key, tenant_id, owner_user_id, endpoint, '
                ' fingerprint, created_at, expires_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (scope_key, client_key, tenant_id, owner_user_id, endpoint,
                 fingerprint, stamp, expires_at))
            # 并发请求里只有一个插入成功；其它的读到同一行但没有响应 → 处理中
            created = cur.rowcount > 0
            row = conn.execute(
                'SELECT * FROM request_keys WHERE scope_key=%s',
                (scope_key,)).fetchone()
            conn.commit()
        data = _decode_request_key(dict(row)) if row else {}
        data['created'] = created
        return data

    def request_key(self, scope_key: str) -> dict | None:
        with self._lease() as conn:
            row = conn.execute(
                'SELECT * FROM request_keys WHERE scope_key=%s',
                (scope_key,)).fetchone()
        return _decode_request_key(dict(row)) if row else None

    def complete_request_key(self, scope_key: str, response: dict, *,
                             task_id: str = '', session_id: str = '',
                             run_id: str = '') -> None:
        with self._lease() as conn:
            conn.execute(
                'UPDATE request_keys SET response_json=%s, task_id=%s, '
                'session_id=%s, run_id=%s WHERE scope_key=%s',
                (json.dumps(response, ensure_ascii=False), task_id or None,
                 session_id or None, run_id or None, scope_key))
            conn.commit()

    def release_request_key(self, scope_key: str) -> None:
        with self._lease() as conn:
            conn.execute(
                'DELETE FROM request_keys WHERE scope_key=%s AND response_json IS NULL',
                (scope_key,))
            conn.commit()

    def purge_request_keys(self, now_ts: float | None = None) -> int:
        current = now_ts if now_ts is not None else time.time()
        with self._lease() as conn:
            cur = conn.execute(
                'DELETE FROM request_keys WHERE expires_at < %s', (current,))
            conn.commit()
            return cur.rowcount

    # ---------- 维护 ----------

    def stats(self) -> dict:
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
        with self._lease() as conn:
            for name, sql in queries.items():
                row = conn.execute(sql).fetchone()
                out[name] = int(row['n'] if row else 0)
        return out

    def _recover_orphans(self) -> int:
        """只把**没有活租约**的遗留 running 任务标记为 interrupted。

        多副本下不能见到 running 就改写：那会把别人正在跑的任务替它判死。
        """
        with self._lease() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='interrupted', updated_at=%s "
                "WHERE status='running' AND NOT EXISTS ("
                '  SELECT 1 FROM runs r WHERE r.task_id = tasks.task_id '
                "  AND r.status='running' AND r.lease_expires_at IS NOT NULL "
                '  AND r.lease_expires_at > %s)',
                (now(), time.time()))
            conn.commit()
            return cur.rowcount

    def reconcile_orphan_runs(self, owner: str | None = None,
                              now_ts: float | None = None) -> dict:
        """只对账**自己名下的、或租约已过期的**运行（文档 §8.3）。

        以前是"见到 running 就判 unknown 并清租约"：多副本或 --reload 会把
        别的进程正在跑的任务判成结果未知，清掉租约后立刻又被领一次，正好
        制造它想防的重复执行。
        """
        current = now_ts if now_ts is not None else time.time()
        stats = {'runs': 0, 'unknown_actions': 0, 'requeued': 0}
        with self._lease() as conn:
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE status IN ('running','reconciling') "
                'AND (lease_owner IS NULL OR lease_owner = %s '
                'OR lease_expires_at IS NULL OR lease_expires_at < %s)',
                (owner, current)).fetchall()
            for row in rows:
                run_id = row['run_id']
                stats['runs'] += 1
                pending = conn.execute(
                    "SELECT action_id, step_id FROM actions "
                    "WHERE run_id=%s AND status='in_flight'", (run_id,)).fetchall()
                for action in pending:
                    conn.execute(
                        "UPDATE actions SET status='unknown', updated_at=%s "
                        'WHERE action_id=%s', (now(), action['action_id']))
                    stats['unknown_actions'] += 1
                    conn.execute(
                        "UPDATE steps SET status='unknown', error=%s "
                        'WHERE run_id=%s AND step_id=%s',
                        ('进程重启时该调用结果未知，需核实', run_id,
                         action['step_id']))
                target = 'reconciling' if pending else 'queued'
                if target == 'queued':
                    stats['requeued'] += 1
                conn.execute(
                    'UPDATE runs SET status=%s, lease_owner=NULL, '
                    'lease_expires_at=NULL, updated_at=%s WHERE run_id=%s',
                    (target, now(), run_id))
            conn.commit()
        return stats

    def close(self) -> None:
        """关闭连接池（幂等）。进程退出、测试收尾都走这里。"""
        pool = getattr(self, '_pool', None)
        if pool is None:
            return
        try:
            pool.close()
            pool.wait(timeout=5)
        except Exception:                           # noqa: BLE001
            pass

    # ---------- 兼容接口（SQLite 实现里有、PG 侧不需要的） ----------

    @property
    def path(self) -> str:
        return self.dsn


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
