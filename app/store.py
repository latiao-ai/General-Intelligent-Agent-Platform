"""SQLite 持久化：会话、任务与事件日志。

设计取舍：

- **标准库 `sqlite3`**，不引入新依赖；
- 单连接 + 线程锁，同步调用。SQLite 本地写入是亚毫秒级，demo 规模完全够用；
  真正高并发时应改为 `asyncio.to_thread` 或换专门的数据库；
- **WAL 模式**，读不阻塞写；
- **事件日志以 SQLite 为准，内存只负责实时投递**：这样进程重启后客户端仍能拿到
  完整的执行过程（见 app/api_hub.py 的 subscribe/去重逻辑）；
- 启动时把库里遗留的 `running` 任务标记为 `interrupted`——进程重启后不可能还有
  任务在跑，否则前端会一直等一个永远不会结束的任务。
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            self._conn.commit()
        self.interrupted = self._recover_orphans()

    # ---------- 会话与消息 ----------

    def ensure_session(self, session_id: str) -> None:
        stamp = now()
        with self._lock:
            self._conn.execute(
                'INSERT OR IGNORE INTO sessions (session_id, created_at, updated_at) '
                'VALUES (?, ?, ?)', (session_id, stamp, stamp))
            self._conn.commit()

    def append_message(self, session_id: str, role: str, content: str) -> None:
        stamp = now()
        with self._lock:
            self._conn.execute(
                'INSERT INTO messages (session_id, role, content, created_at) '
                'VALUES (?, ?, ?, ?)', (session_id, role, content, stamp))
            self._conn.execute(
                'INSERT OR IGNORE INTO sessions (session_id, created_at, updated_at) '
                'VALUES (?, ?, ?)', (session_id, stamp, stamp))
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

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """会话列表（按最近活跃排序），供前端侧边栏展示。

        title 取该会话第一条用户消息（截断），没有就回退到「新会话」。
        """
        with self._lock:
            rows = self._conn.execute(
                'SELECT s.session_id, s.created_at, s.updated_at, '
                '  (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id) '
                '    AS message_count, '
                '  (SELECT content FROM messages m WHERE m.session_id=s.session_id '
                '     AND m.role=\'user\' ORDER BY m.id LIMIT 1) AS first_message '
                'FROM sessions s ORDER BY s.updated_at DESC LIMIT ?',
                (limit,)).fetchall()
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
                'SELECT task_id, message, status, created_at FROM tasks '
                'WHERE session_id=? ORDER BY rowid ASC LIMIT ?',
                (session_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 事件日志 ----------

    def append_event(self, task_id: str, event: dict) -> int:
        """追加一条事件并返回它的序号（每任务内自增，用于订阅去重）。"""
        payload = json.dumps(event, ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                'SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE task_id=?',
                (task_id,)).fetchone()
            seq = row[0]
            self._conn.execute(
                'INSERT INTO events (task_id, seq, payload, created_at) VALUES (?, ?, ?, ?)',
                (task_id, seq, payload, now()))
            self._conn.commit()
        return seq

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
                   'lease_expires_at', 'lease_epoch', 'error'}
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
                  now_ts: float | None = None) -> dict | None:
        """原子领取一个排队中的 run，并递增租约代次。

        lease_epoch 是 fencing 令牌：旧 Worker 拿着过期的代次写状态会被拒，
        避免"暂停后恢复，老的协程又把状态改回去"。
        """
        stamp = now()
        current = now_ts if now_ts is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                # queued 的可以领；running 但租约已过期的也要能领——
                # 那正是 Worker 崩溃后需要接手的运行（文档 §9.2）。
                "SELECT run_id FROM runs WHERE status IN ('queued','running') "
                "AND (lease_expires_at IS NULL OR lease_expires_at < ?) "
                "ORDER BY created_at LIMIT 1", (current,)).fetchone()
            if row is None:
                return None
            run_id = row['run_id']
            self._conn.execute(
                "UPDATE runs SET status='running', lease_owner=?, lease_expires_at=?, "
                "lease_epoch=lease_epoch+1, updated_at=? WHERE run_id=?",
                (worker_id, current + lease_seconds, stamp, run_id))
            self._conn.commit()
        return self.run(run_id)

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
        return self.action(action_id)

    def action(self, action_id: str) -> dict | None:
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
        return self.job(job_id)

    def job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM background_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['payload'] = json.loads(data.pop('payload_json') or '{}')
        return data

    def finish_job(self, job_id: str, *, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE background_jobs SET status=?, last_error=?, updated_at=? "
                'WHERE job_id=?',
                ('failed' if error else 'done', error, now(), job_id))
            self._conn.commit()

    # ---------- 维护 ----------

    def _recover_orphans(self) -> int:
        """把库里遗留的 running 任务标记为 interrupted。

        注意：这只是**兼容旧表**的兜底。中台下 run 的恢复由租约判定
        （见 Store.claim_run / 文档 §9.2），不能靠"每个 API 实例启动就改写全部
        运行态"——多实例时会互相踩。新链路请用 runs 表。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status='interrupted', updated_at=? WHERE status='running'",
                (now(),))
            self._conn.commit()
            return cur.rowcount

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
