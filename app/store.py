"""SQLite 持久化：会话、任务与事件日志。

设计取舍：

- **标准库 `sqlite3`**，不引入新依赖；
- **对外的仓储接口是异步的**（与 `PostgresStore` 一致）。sqlite3 没有官方异步
  驱动，所以具体执行用 `asyncio.to_thread` 丢到线程池里跑，事件循环不被阻塞。
  实现体本身仍是同步的、单连接 + 线程锁——这对 SQLite 是正确的做法（它本来就是
  单写者），需要高并发就换 PostgreSQL；
- **WAL 模式**，读不阻塞写；
- **事件日志以 SQLite 为准，内存只负责实时投递**：这样进程重启后客户端仍能拿到
  完整的执行过程（见 app/api_hub.py 的 subscribe/去重逻辑）；
- 启动时把库里遗留的 `running` 任务标记为 `interrupted`——进程重启后不可能还有
  任务在跑，否则前端会一直等一个永远不会结束的任务。
"""
import asyncio
import functools
import json
import sqlite3
import threading
import time
import uuid

from app.config import settings
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    owner_user_id TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
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
    lease_expires_at    REAL,
    lease_epoch         INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    -- 已完成补偿的时间（文档 §8.4：撤销是独立动作，要有独立记录，不能只看 run 状态）
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
    lease_expires_at REAL,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe ON background_jobs(kind, dedupe_key);

-- ---------- 请求幂等（POST /api/tasks） ----------
-- 客户端重试（网络抖动、双击、代理重放）不能变成"两个任务、两次部署"。
-- scope_key = hash(tenant|owner|endpoint|client_key)，主键保证只有一个赢家；
-- 存 fingerprint 是为了区分"重试同一请求"和"拿同一个键发了别的内容"。
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
    expires_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_keys_expiry ON request_keys(expires_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 旧库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里按需 ALTER。
# 只做"加列 + 默认值"，SQLite 与 PostgreSQL 语义一致，不需要停机迁移。
_MIGRATIONS = (
    ('sessions', 'tenant_id', "TEXT NOT NULL DEFAULT 'default'"),
    ('sessions', 'owner_user_id', "TEXT NOT NULL DEFAULT 'default'"),
    ('runs', 'compensated_at', 'TEXT'),
)


class Store:
    """会话 / 任务 / 事件 的 SQLite 存储。

    path 传 ':memory:' 可得到完全隔离的临时库（测试用）。
    """

    def __init__(self, path: str | Path = ':memory:'):
        self.path = str(path)
        if self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA synchronous=NORMAL')
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()
        self.interrupted = self._recover_orphans()

    def _migrate(self) -> None:
        """给旧库补列（调用方已持有锁）。"""
        for table, column, decl in _MIGRATIONS:
            rows = self._conn.execute(f'PRAGMA table_info({table})').fetchall()
            if column in {row['name'] for row in rows}:
                continue
            self._conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')

    # ---------- 会话与消息 ----------

    def ensure_session(self, session_id: str, *, tenant_id: str = 'default',
                       owner_user_id: str = 'default') -> None:
        stamp = now()
        with self._lock:
            self._conn.execute(
                'INSERT OR IGNORE INTO sessions '
                '(session_id, tenant_id, owner_user_id, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (session_id, tenant_id, owner_user_id, stamp, stamp))
            self._conn.commit()

    def session(self, session_id: str) -> dict | None:
        """会话归属。接口层用它做对象权限检查（文档 §10）。"""
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        return dict(row) if row else None

    def append_message(self, session_id: str, role: str, content: str, *,
                       tenant_id: str = 'default',
                       owner_user_id: str = 'default') -> None:
        stamp = now()
        with self._lock:
            self._conn.execute(
                'INSERT INTO messages (session_id, role, content, created_at) '
                'VALUES (?, ?, ?, ?)', (session_id, role, content, stamp))
            self._conn.execute(
                'INSERT OR IGNORE INTO sessions '
                '(session_id, tenant_id, owner_user_id, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (session_id, tenant_id, owner_user_id, stamp, stamp))
            self._conn.execute(
                'UPDATE sessions SET updated_at=? WHERE session_id=?', (stamp, session_id))
            self._conn.commit()

    def messages(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT role, content FROM messages WHERE session_id=? ORDER BY id',
                (session_id,)).fetchall()
        return [{'role': r['role'], 'content': r['content']} for r in rows]

    def messages_after(self, session_id: str, after_id: int = 0) -> list[dict]:
        """取 id 大于 after_id 的消息（带 id），供压缩与增量历史使用。"""
        with self._lock:
            rows = self._conn.execute(
                'SELECT id, role, content FROM messages '
                'WHERE session_id=? AND id>? ORDER BY id',
                (session_id, after_id)).fetchall()
        return [{'id': r['id'], 'role': r['role'], 'content': r['content']}
                for r in rows]

    def memory(self, session_id: str) -> dict:
        """会话记忆；没有就是空摘要 + 空事实 + upto=0。"""
        with self._lock:
            row = self._conn.execute(
                'SELECT summary, facts, upto FROM session_memory WHERE session_id=?',
                (session_id,)).fetchone()
        if row is None:
            return {'summary': '', 'facts': {}, 'upto': 0}
        return {'summary': row['summary'],
                'facts': json.loads(row['facts'] or '{}'),
                'upto': row['upto']}

    def set_memory(self, session_id: str, *, summary: str, facts: dict,
                   upto: int) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT INTO session_memory (session_id, summary, facts, upto, updated_at) '
                'VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET '
                'summary=excluded.summary, facts=excluded.facts, '
                'upto=excluded.upto, updated_at=excluded.updated_at',
                (session_id, summary or '',
                 json.dumps(facts or {}, ensure_ascii=False), int(upto), now()))
            self._conn.commit()

    def clear_memory(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute('DELETE FROM session_memory WHERE session_id=?',
                               (session_id,))
            self._conn.commit()

    def list_sessions(self, limit: int = 50, *, tenant_id: str | None = None,
                      owner_user_id: str | None = None) -> list[dict]:
        """会话列表（按最近活跃排序），供前端侧边栏展示。

        title 取该会话第一条用户消息（截断），没有就回退到「新会话」。
        传入 tenant_id / owner_user_id 时只返回该主体名下的会话——单用户阶段
        恒为 default，但查询路径上一开始就得有过滤条件（文档 §10）。
        """
        where, params = [], []
        if tenant_id:
            where.append('s.tenant_id=?')
            params.append(tenant_id)
        if owner_user_id:
            where.append("(s.owner_user_id=? OR s.owner_user_id='default')")
            params.append(owner_user_id)
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self._lock:
            rows = self._conn.execute(
                'SELECT s.session_id, s.created_at, s.updated_at, '
                '  s.tenant_id, s.owner_user_id, '
                '  (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id) '
                '    AS message_count, '
                '  (SELECT content FROM messages m WHERE m.session_id=s.session_id '
                '     AND m.role=\'user\' ORDER BY m.id LIMIT 1) AS first_message '
                f'FROM sessions s{clause} ORDER BY s.updated_at DESC LIMIT ?',
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
        with self._lock:
            self._conn.execute(
                'INSERT INTO tasks (task_id, session_id, message, status, created_at, '
                'updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                (task_id, session_id, message, 'running', stamp, stamp))
            self._conn.commit()

    def update_task(self, task_id: str, **fields) -> None:
        allowed = {'status', 'artifact_id', 'error', 'state'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if 'state' in updates and not isinstance(updates['state'], str):
            updates['state'] = json.dumps(updates['state'], ensure_ascii=False)
        columns = ', '.join(f'{k}=?' for k in updates)
        with self._lock:
            self._conn.execute(
                f'UPDATE tasks SET {columns}, updated_at=? WHERE task_id=?',
                (*updates.values(), now(), task_id))
            self._conn.commit()

    def task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['state'] = json.loads(data['state']) if data['state'] else None
        return data

    def tasks_of(self, session_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            # rowid 是隐式列，可用于排序但不能进索引定义。
            rows = self._conn.execute(
                'SELECT task_id, message, status, created_at FROM tasks '
                'WHERE session_id=? ORDER BY rowid DESC LIMIT ?',
                (session_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def session_tasks(self, session_id: str, limit: int = 50) -> list[dict]:
        """会话里的任务，按时间**正序**——回放执行过程时要按发生顺序。

        与 tasks_of 的区别只在排序方向：那个是给"最近任务"用的。
        """
        with self._lock:
            rows = self._conn.execute(
                'SELECT task_id, message, status, state, created_at FROM tasks '
                'WHERE session_id=? ORDER BY rowid ASC LIMIT ?',
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

        序号必须由**一条语句**算出来。以前是 `SELECT MAX(seq)+1` 再 `INSERT`：
        两个进程同时给同一个 task 写事件时会拿到同一个 seq，主键撞车或者
        ——更糟——SSE 端按 seq 去重，把后到的那条**静默丢掉**。
        这里用 `INSERT ... SELECT ... RETURNING seq`，取值、写入、回读一次完成。
        """
        payload = json.dumps(event, ensure_ascii=False)
        statement = (
            'INSERT INTO events (task_id, seq, payload, created_at) '
            'SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ? FROM events WHERE task_id=? '
            'RETURNING seq')
        params = (task_id, payload, now(), task_id)
        with self._lock:
            try:
                row = self._conn.execute(statement, params).fetchone()
                seq = int(row['seq'])
            except sqlite3.OperationalError:
                # 老版本 SQLite（< 3.35）没有 RETURNING：退回两条语句。
                # 进程内有锁保护，跨进程仍有极小概率撞主键，靠重试兜底。
                seq = self._append_event_legacy(task_id, payload)
            self._conn.commit()
        return seq

    def _append_event_legacy(self, task_id: str, payload: str) -> int:
        for _ in range(5):
            row = self._conn.execute(
                'SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events '
                'WHERE task_id=?', (task_id,)).fetchone()
            seq = row['next_seq']
            try:
                self._conn.execute(
                    'INSERT INTO events (task_id, seq, payload, created_at) '
                    'VALUES (?, ?, ?, ?)', (task_id, seq, payload, now()))
                return seq
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError('事件序号竞争失败')

    def events(self, task_id: str) -> list[tuple[int, dict]]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT seq, payload FROM events WHERE task_id=? ORDER BY seq',
                (task_id,)).fetchall()
        return [(r['seq'], json.loads(r['payload'])) for r in rows]

    # ---------- 中台对象：task / run / plan / step / action ----------
    # 这一层是文档 §3/§6/§8/§9 的落地。旧 tasks 表仍在用（界面查询与事件回放），
    # 新执行链路逐步迁到 run 模型，两边通过 task_id 关联。

    def ensure_task_meta(self, task_id: str, *, session_id: str, goal: str,
                         tenant_id: str = 'default',
                         owner_user_id: str = 'default') -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR IGNORE INTO tasks_meta '
                '(task_id, tenant_id, owner_user_id, goal, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (task_id, tenant_id, owner_user_id, goal, now()))
            self._conn.commit()

    def task_meta(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM tasks_meta WHERE task_id=?', (task_id,)).fetchone()
        return dict(row) if row else None

    def create_run(self, run_id: str, task_id: str, *, budget: dict | None = None,
                   tenant_id: str = 'default', status: str = 'queued') -> None:
        stamp = now()
        with self._lock:
            self._conn.execute(
                'INSERT INTO runs (run_id, task_id, tenant_id, status, '
                'budget_json, consumed_json, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (run_id, task_id, tenant_id, status,
                 json.dumps(budget or {}, ensure_ascii=False), '{}', stamp, stamp))
            self._conn.execute(
                'UPDATE tasks_meta SET current_run_id=? WHERE task_id=?',
                (run_id, task_id))
            self._conn.commit()

    def run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
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
        columns = ', '.join(f'{k}=?' for k in updates)
        with self._lock:
            self._conn.execute(
                f'UPDATE runs SET {columns}, updated_at=? WHERE run_id=?',
                (*updates.values(), now(), run_id))
            self._conn.commit()

    def runs_of(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM runs WHERE task_id=? ORDER BY created_at',
                (task_id,)).fetchall()
        return [_decode_run(dict(r)) for r in rows]

    def claim_run(self, worker_id: str, lease_seconds: float = 60.0,
                  now_ts: float | None = None,
                  run_id: str | None = None) -> dict | None:
        """原子领取一个运行，并递增租约代次。

        - `run_id` 给定时只领这一条：API 进程把自己刚建的 run 领走，Worker 就
          再也领不到它。这是"双执行路径"的根治办法——**只有一种领取方式**，
          就地执行也必须先占住租约。
        - 不给时按入队顺序领：queued、或 running 但租约已过期（Worker 崩溃后
          需要接手，文档 §9.2）、或 reconciling（上轮结果未定，交给账本去拦）。

        `UPDATE ... RETURNING` 让"挑一条 + 占住它"在同一条语句里完成：跨进程
        也不会两个执行者领到同一个 run（PG 侧另有 SKIP LOCKED）。
        """
        stamp = now()
        current = now_ts if now_ts is not None else time.time()
        condition = ("status IN ('queued','running','reconciling') "
                     "AND (lease_expires_at IS NULL OR lease_expires_at < ?)")
        if run_id is not None:
            sql = ("UPDATE runs SET status='running', lease_owner=?, "
                   "lease_expires_at=?, lease_epoch=lease_epoch+1, updated_at=? "
                   f"WHERE run_id=? AND {condition} RETURNING *")
            params = (worker_id, current + lease_seconds, stamp, run_id, current)
        else:
            sql = ("UPDATE runs SET status='running', lease_owner=?, "
                   "lease_expires_at=?, lease_epoch=lease_epoch+1, updated_at=? "
                   f"WHERE run_id=(SELECT run_id FROM runs WHERE {condition} "
                   "ORDER BY created_at LIMIT 1) RETURNING *")
            params = (worker_id, current + lease_seconds, stamp, current)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            self._conn.commit()
        return _decode_run(dict(row)) if row else None

    def release_lease(self, run_id: str, worker_id: str) -> None:
        """执行者主动交还租约。带 owner 条件，旧代次不能覆盖新持有者（fencing）。"""
        with self._lock:
            self._conn.execute(
                'UPDATE runs SET lease_owner=NULL, lease_expires_at=NULL, '
                'updated_at=? WHERE run_id=? AND lease_owner=?',
                (now(), run_id, worker_id))
            self._conn.commit()

    def renew_lease(self, run_id: str, worker_id: str, lease_seconds: float = 60.0,
                    now_ts: float | None = None) -> bool:
        current = now_ts if now_ts is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                'UPDATE runs SET lease_expires_at=?, updated_at=? '
                "WHERE run_id=? AND lease_owner=? AND status='running'",
                (current + lease_seconds, now(), run_id, worker_id))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- 计划与步骤 ----------

    def save_plan(self, run_id: str, version: int, *, goal: str,
                  steps: list[dict], reason: str = '') -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO plans '
                '(run_id, version, goal, steps_json, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (run_id, version, goal,
                 json.dumps(steps, ensure_ascii=False), reason, now()))
            self._conn.commit()

    def plan(self, run_id: str, version: int | None = None) -> dict | None:
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    'SELECT * FROM plans WHERE run_id=? ORDER BY version DESC LIMIT 1',
                    (run_id,)).fetchone()
            else:
                row = self._conn.execute(
                    'SELECT * FROM plans WHERE run_id=? AND version=?',
                    (run_id, version)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['steps'] = json.loads(data.pop('steps_json'))
        return data

    def save_steps(self, run_id: str, plan_version: int, steps: list[dict]) -> None:
        with self._lock:
            for index, step in enumerate(steps):
                self._conn.execute(
                    'INSERT OR REPLACE INTO steps '
                    '(run_id, plan_version, step_id, seq, tool_key, status, '
                    'inputs_json, output_json, action_id) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (run_id, plan_version, step['step_id'], index,
                     step.get('tool_key') or step.get('tool') or '',
                     step.get('status', 'pending'),
                     json.dumps(step.get('inputs') or {}, ensure_ascii=False),
                     None, None))
            self._conn.commit()

    def steps_of(self, run_id: str, plan_version: int | None = None) -> list[dict]:
        with self._lock:
            if plan_version is None:
                row = self._conn.execute(
                    'SELECT MAX(plan_version) AS v FROM steps WHERE run_id=?',
                    (run_id,)).fetchone()
                plan_version = (row['v'] if row else 0) or 0
            rows = self._conn.execute(
                'SELECT * FROM steps WHERE run_id=? AND plan_version=? ORDER BY seq',
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
        columns = ', '.join(f'{k}=?' for k in updates)
        with self._lock:
            self._conn.execute(
                f'UPDATE steps SET {columns} WHERE run_id=? AND plan_version=? '
                'AND step_id=?',
                (*updates.values(), run_id, plan_version, step_id))
            self._conn.commit()

    # ---------- 动作账本（文档 §8） ----------

    def find_action(self, idempotency_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM actions WHERE idempotency_key=?',
                (idempotency_key,)).fetchone()
        return _decode_action(dict(row)) if row else None

    def create_action(self, action_id: str, *, run_id: str, plan_version: int,
                      step_id: str, tool_key: str, args_hash: str,
                      idempotency_key: str, effect: str = 'unknown',
                      authorized_by: str = '') -> dict:
        stamp = now()
        with self._lock:
            self._conn.execute(
                'INSERT INTO actions (action_id, run_id, plan_version, step_id, '
                'tool_key, args_hash, idempotency_key, effect, status, '
                'authorized_by, created_at, updated_at) '
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)",
                (action_id, run_id, plan_version, step_id, tool_key, args_hash,
                 idempotency_key, effect, authorized_by, stamp, stamp))
            self._conn.commit()
        return self._action_row(action_id)

    def action(self, action_id: str) -> dict | None:
        return self._action_row(action_id)

    def _action_row(self, action_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM actions WHERE action_id=?', (action_id,)).fetchone()
        return _decode_action(dict(row)) if row else None

    def update_action(self, action_id: str, **fields) -> None:
        allowed = {'status', 'receipt_json'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if 'receipt_json' in updates and not isinstance(updates['receipt_json'], str):
            updates['receipt_json'] = json.dumps(updates['receipt_json'],
                                                ensure_ascii=False)
        columns = ', '.join(f'{k}=?' for k in updates)
        with self._lock:
            self._conn.execute(
                f'UPDATE actions SET {columns}, updated_at=? WHERE action_id=?',
                (*updates.values(), now(), action_id))
            self._conn.commit()

    def actions_of(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM actions WHERE run_id=? ORDER BY created_at',
                (run_id,)).fetchall()
        return [_decode_action(dict(r)) for r in rows]

    def add_attempt(self, attempt_id: str, action_id: str) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT INTO action_attempts (attempt_id, action_id, started_at) '
                'VALUES (?, ?, ?)', (attempt_id, action_id, now()))
            self._conn.commit()

    def finish_attempt(self, attempt_id: str, *, error_type: str | None = None,
                       result_ref: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                'UPDATE action_attempts SET ended_at=?, error_type=?, result_ref=? '
                'WHERE attempt_id=?', (now(), error_type, result_ref, attempt_id))
            self._conn.commit()

    def attempts_of(self, action_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM action_attempts WHERE action_id=? ORDER BY started_at',
                (action_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 产物登记 ----------

    def add_artifact(self, artifact_id: str, *, run_id: str, kind: str,
                     storage_key: str, mime: str = 'text/html', size: int = 0,
                     sha256: str = '') -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO artifacts (artifact_id, run_id, kind, '
                'storage_key, mime, size, sha256, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (artifact_id, run_id, kind, storage_key, mime, size, sha256, now()))
            self._conn.commit()

    def artifact(self, artifact_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM artifacts WHERE artifact_id=?',
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
        with self._lock:
            blocked = self._conn.execute(
                'SELECT 1 FROM memory_tombstones WHERE scope_type=? AND '
                'scope_id=? AND fact_key=?', (scope_type, scope_id, fact_key)
            ).fetchone()
            if blocked:
                return None
            current = self._conn.execute(
                'SELECT memory_id, version FROM memories WHERE scope_type=? AND '
                'scope_id=? AND subject=? AND predicate=? AND status=? '
                'ORDER BY version DESC LIMIT 1',
                (scope_type, scope_id, subject, predicate, 'active')).fetchone()
            memory_id = 'mem_' + uuid.uuid4().hex[:12]
            version = (current['version'] + 1) if current else 1
            stamp = now()
            self._conn.execute(
                'INSERT INTO memories (memory_id, tenant_id, scope_type, scope_id, '
                'subject, predicate, value, status, version, supersedes, source, '
                'created_by, created_at, updated_at) '
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                (memory_id, tenant_id, scope_type, scope_id, subject, predicate,
                 value, version, current['memory_id'] if current else None,
                 source, created_by, stamp, stamp))
            if current:
                self._conn.execute(
                    "UPDATE memories SET status='superseded', valid_to=?, "
                    'updated_at=? WHERE memory_id=?',
                    (stamp, stamp, current['memory_id']))
            self._conn.commit()
        return memory_id

    def recall(self, scope_type: str, scope_id: str) -> list[dict]:
        """读取该作用域下仍然有效的记忆（被替代/已失效的不返回）。"""
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM memories WHERE scope_type=? AND scope_id=? '
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
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET status='forgotten', valid_to=?, updated_at=? "
                'WHERE scope_type=? AND scope_id=? AND subject=? AND predicate=? '
                "AND status='active'",
                (stamp, stamp, scope_type, scope_id, subject, predicate))
            changed = cur.rowcount
            self._conn.execute(
                'INSERT OR IGNORE INTO memory_tombstones '
                '(tombstone_id, scope_type, scope_id, fact_key, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                ('tmb_' + uuid.uuid4().hex[:10], scope_type, scope_id, fact_key,
                 reason, stamp))
            self._conn.commit()
        return changed

    def is_forgotten(self, scope_type: str, scope_id: str, subject: str,
                     predicate: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                'SELECT 1 FROM memory_tombstones WHERE scope_type=? AND '
                'scope_id=? AND fact_key=?',
                (scope_type, scope_id, f'{subject}|{predicate}')).fetchone()
        return row is not None

    # ---------- 后台任务（outbox） ----------

    def enqueue_job(self, job_id: str, kind: str, dedupe_key: str,
                    payload: dict | None = None) -> bool:
        """按 (kind, dedupe_key) 幂等入队；返回是否真的新入队。"""
        with self._lock:
            cur = self._conn.execute(
                'INSERT OR IGNORE INTO background_jobs '
                '(job_id, kind, dedupe_key, payload_json, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (job_id, kind, dedupe_key,
                 json.dumps(payload or {}, ensure_ascii=False), now(), now()))
            self._conn.commit()
            return cur.rowcount > 0

    def claim_job(self, worker_id: str, lease_seconds: float = 60.0,
                  now_ts: float | None = None) -> dict | None:
        current = now_ts if now_ts is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id FROM background_jobs WHERE status='queued' "
                "AND (lease_expires_at IS NULL OR lease_expires_at < ?) "
                "ORDER BY created_at LIMIT 1", (current,)).fetchone()
            if row is None:
                return None
            job_id = row['job_id']
            self._conn.execute(
                "UPDATE background_jobs SET status='running', lease_owner=?, "
                'lease_expires_at=?, attempts=attempts+1, updated_at=? '
                'WHERE job_id=?',
                (worker_id, current + lease_seconds, now(), job_id))
            self._conn.commit()
        return self._job_row(job_id)

    def job(self, job_id: str) -> dict | None:
        return self._job_row(job_id)

    def _job_row(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM background_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['payload'] = json.loads(data.pop('payload_json') or '{}')
        return data

    def finish_job(self, job_id: str, *, error: str | None = None,
                   retry: bool = False) -> None:
        """收尾一个后台作业。

        `retry=True` 表示"这次失败，但还能再来"：退回 queued，并用退避时间占住
        租约，由下一轮扫描重新领取。以前一次失败就写 failed —— 一次网关抖动
        就把这条作业永久判死，而且没人会知道。
        """
        with self._lock:
            if error and retry:
                row = self._conn.execute(
                    'SELECT attempts FROM background_jobs WHERE job_id=?',
                    (job_id,)).fetchone()
                attempts = int((row['attempts'] if row else 0) or 1)
                delay = min(300.0, 5.0 * (2 ** min(attempts, 6)))
                self._conn.execute(
                    "UPDATE background_jobs SET status='queued', last_error=?, "
                    'lease_owner=NULL, lease_expires_at=?, updated_at=? '
                    'WHERE job_id=?',
                    (error, time.time() + delay, now(), job_id))
            else:
                self._conn.execute(
                    "UPDATE background_jobs SET status=?, last_error=?, updated_at=? "
                    'WHERE job_id=?',
                    ('failed' if error else 'done', error, now(), job_id))
            self._conn.commit()

    # ---------- 请求幂等（POST /api/tasks） ----------
    # "客户端重试"和"用户真的要开第二轮"在服务端看起来一模一样；区别只能由
    # 客户端用一个稳定的键表达。这里做三件事：占位（唯一）、记录回执、过期回收。

    def reserve_request_key(self, scope_key: str, *, client_key: str,
                            tenant_id: str, owner_user_id: str,
                            endpoint: str, fingerprint: str,
                            expires_at: float) -> dict:
        """原子占位：没有就插入，已经有就返回已有行。

        先删同 scope 的过期行，再 `INSERT OR IGNORE`：过期键必须能被复用，
        否则客户端在 TTL 之后重试会被自己的旧记录一直挡着。
        """
        stamp = now()
        with self._lock:
            self._conn.execute(
                'DELETE FROM request_keys WHERE scope_key=? AND expires_at < ?',
                (scope_key, time.time()))
            cur = self._conn.execute(
                'INSERT OR IGNORE INTO request_keys '
                '(scope_key, client_key, tenant_id, owner_user_id, endpoint, '
                ' fingerprint, created_at, expires_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (scope_key, client_key, tenant_id, owner_user_id, endpoint,
                 fingerprint, stamp, expires_at))
            # 只有真正插入成功的那一个请求算"赢家"。靠返回行判断不出来：
            # 后来者读到的是别人刚插入、还没回填响应的行，那必须算"处理中"。
            created = cur.rowcount > 0
            row = self._conn.execute(
                'SELECT * FROM request_keys WHERE scope_key=?',
                (scope_key,)).fetchone()
            self._conn.commit()
        data = _decode_request_key(dict(row)) if row else {}
        data['created'] = created
        return data

    def request_key(self, scope_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM request_keys WHERE scope_key=?',
                (scope_key,)).fetchone()
        return _decode_request_key(dict(row)) if row else None

    def complete_request_key(self, scope_key: str, response: dict, *,
                             task_id: str = '', session_id: str = '',
                             run_id: str = '') -> None:
        """回填这次请求的响应，后续同键请求直接拿它，不再新建任务。"""
        with self._lock:
            self._conn.execute(
                'UPDATE request_keys SET response_json=?, task_id=?, session_id=?, '
                'run_id=? WHERE scope_key=?',
                (json.dumps(response, ensure_ascii=False), task_id or None,
                 session_id or None, run_id or None, scope_key))
            self._conn.commit()

    def release_request_key(self, scope_key: str) -> None:
        """失败时撤销占位，让客户端能用同一个键重试（不能留下"永远处理中"）。"""
        with self._lock:
            self._conn.execute(
                'DELETE FROM request_keys WHERE scope_key=? AND response_json IS NULL',
                (scope_key,))
            self._conn.commit()

    def purge_request_keys(self, now_ts: float | None = None) -> int:
        current = now_ts if now_ts is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                'DELETE FROM request_keys WHERE expires_at < ?', (current,))
            self._conn.commit()
            return cur.rowcount

    # ---------- 维护 ----------

    def stats(self) -> dict:
        """运行态势快照：给 /api/health 与排障用。

        此前队列积压、待核实动作、失败作业都没有出口，只能手工查库。
        """
        queries = {
            'runs_queued': "SELECT COUNT(*) FROM runs WHERE status='queued'",
            'runs_running': "SELECT COUNT(*) FROM runs WHERE status='running'",
            'runs_reconciling': "SELECT COUNT(*) FROM runs "
                                "WHERE status='reconciling'",
            'actions_unknown': "SELECT COUNT(*) FROM actions "
                               "WHERE status='unknown'",
            'jobs_queued': "SELECT COUNT(*) FROM background_jobs "
                           "WHERE status='queued'",
            'jobs_failed': "SELECT COUNT(*) FROM background_jobs "
                           "WHERE status='failed'",
        }
        with self._lock:
            return {name: int(self._conn.execute(sql).fetchone()[0] or 0)
                    for name, sql in queries.items()}

    def _recover_orphans(self) -> int:
        """把**没有任何活租约**的遗留 running 任务标记为 interrupted。

        注意：不能见到 running 就改写。多副本或 `--reload` 时，另一个实例正在跑
        的任务也写着 running，一律改写等于替别人判死；run 的恢复由租约判定
        （见 claim_run / 文档 §9.2），这里只处理"没有对应活 run"的记录。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status='interrupted', updated_at=? "
                "WHERE status='running' AND NOT EXISTS ("
                '  SELECT 1 FROM runs r WHERE r.task_id = tasks.task_id '
                "  AND r.status='running' AND r.lease_expires_at IS NOT NULL "
                '  AND r.lease_expires_at > ?)',
                (now(), time.time()))
            self._conn.commit()
            return cur.rowcount

    def reconcile_orphan_runs(self, owner: str | None = None,
                              now_ts: float | None = None) -> dict:
        """对账**本进程名下的、或租约已过期的**运行（文档 §8.3）。

        以前的实现是"见到 running 就判 unknown 并清租约"。多副本或
        `uvicorn --reload` 一次，它就会把别的进程正在跑的任务判成"结果未知"，
        清掉租约后立刻又能被再领一次——正好制造它想防的重复执行。

        现在只看两种：租约属于自己（本进程崩溃前的遗留），或租约已过期 / 从未
        写入（持有者确实不在了）。处理方式按账本证据分两种：

        - 有 in_flight 动作 → 标 unknown + run=reconciling，**不自动重放**，
          等人工或核实流程确认（远端可能已经执行了）；
        - 只有 prepared（请求根本没发出去）或没有动作 → run 回到 queued，
          由 Worker 用账本安全续跑，不制造假告警。
        """
        current = now_ts if now_ts is not None else time.time()
        stats = {'runs': 0, 'unknown_actions': 0, 'requeued': 0}
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, lease_owner FROM runs WHERE status IN "
                "('running','reconciling') AND ("
                '  lease_owner IS NULL OR lease_owner = ? '
                '  OR lease_expires_at IS NULL OR lease_expires_at < ?)',
                (owner, current)).fetchall()
            for row in rows:
                run_id = row['run_id']
                stats['runs'] += 1
                # 只处理 in_flight：prepared 意味着请求根本没发出去，
                # 可以直接重试；这一点文档 §8.3 的窗口一是分开处理的。
                pending = self._conn.execute(
                    "SELECT action_id, step_id FROM actions "
                    "WHERE run_id=? AND status='in_flight'", (run_id,)).fetchall()
                for action in pending:
                    self._conn.execute(
                        "UPDATE actions SET status='unknown', updated_at=? "
                        'WHERE action_id=?', (now(), action['action_id']))
                    stats['unknown_actions'] += 1
                    self._conn.execute(
                        "UPDATE steps SET status='unknown', error=? "
                        'WHERE run_id=? AND step_id=?',
                        ('进程重启时该调用结果未知，需核实', run_id,
                         action['step_id']))
                target = 'reconciling' if pending else 'queued'
                if target == 'queued':
                    stats['requeued'] += 1
                self._conn.execute(
                    "UPDATE runs SET status=?, lease_owner=NULL, "
                    'lease_expires_at=NULL, updated_at=? WHERE run_id=?',
                    (target, now(), run_id))
            self._conn.commit()
        return stats

    def close(self) -> None:
        with self._lock:
            self._conn.close()


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


def _offload(fn):
    """把同步实现搬到线程里跑，对外提供异步接口。

    `app.persistence.Repository` 只定义一套（异步）接口：SQLite 与 PostgreSQL
    两种实现必须长得一样，调用方不必记住"当前是哪种模式"。SQLite 没有官方
    异步驱动，所以具体执行丢进线程池；`sqlite3` 连接自带一把锁，多线程调用安全。
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
    wrapper.__wrapped_sync__ = fn          # 测试用它断言"确实有同步实现"
    return wrapper


# 给 Store 的**公开方法**套异步外壳。
#
# 为什么用循环而不是手写 56 个 async def 转发：那 56 个转发除了名字什么都不写，
# 真正的信息（借不借线程、怎么提交）一条都不在里面，只会让人觉得"这层有内容"。
# 私有方法（_recover_orphans / _action_row …）与属性（path）不套，保持同步——
# 它们要么在构造期跑，要么根本不做 IO。
import types as _types

_SYNC_METHODS = []
for _name, _member in list(vars(Store).items()):
    if not isinstance(_member, _types.FunctionType) or _name.startswith('_'):
        continue
    _SYNC_METHODS.append(_name)
    setattr(Store, _name, _offload(_member))


def open_store(database_url: str | None = None):
    """按配置返回仓储实现。

    有 DATABASE_URL 就走 PostgreSQL，否则本地 SQLite。两者都满足
    app.persistence.Repository 接口，业务代码不需要关心用的是哪个。
    """
    url = (settings.database_url if database_url is None
           else database_url).strip()
    if url.startswith(('postgresql://', 'postgres://')):
        from app.pg_store import PostgresStore
        return PostgresStore(url)
    return Store(settings.db_path)
