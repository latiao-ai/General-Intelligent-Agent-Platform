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

    # ---------- 维护 ----------

    def _recover_orphans(self) -> int:
        """把库里遗留的 running 任务标记为 interrupted。

        进程重启后不可能还有任务在跑；不处理的话前端会永远等一个不会结束的任务。
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
