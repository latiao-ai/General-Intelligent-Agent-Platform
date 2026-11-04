"""任务、目标准入和事件日志。

`create_goal` 到 `task_meta` 原样放在这里。运行侧只通过
`self._append_event_in_tx` 调用事件写入，不必和 run 方法待在同一个类里。
跟 run、计划放在一起会让 `pg_runs.py` 超过 700 行，所以单独成一个 mixin。
方法体没有改。
"""
import json
import time
import uuid

import psycopg

from webpage_agent.core.config import settings
from webpage_agent.observe import telemetry
from webpage_agent.store.pg_store import SessionGoalConflict, now

class PgTasks:
    """PgTasks 方法组。见模块说明。"""

    # ---------- 任务 ----------

    async def create_goal(self, task_id: str, run_id: str, session_id: str,
                          message: str, *, tenant_id: str, owner_user_id: str,
                          owner_role: str = 'member', scene: dict | None = None,
                          request_spec: dict | None = None, platform_id: str = '',
                          platform_subject: str = '') -> dict:
        """会话准入、任务、运行、消息及请求键关联同事务提交。

        请求键先锁、会话后锁。事务提交前不会派发任务；崩溃只可能全部回滚或
        留下带 task/run 的可重放请求，不能留下已执行但查不到任务的占位。
        """
        from webpage_agent.store.idempotency import IdempotencyConflict, IdempotencyInFlight

        stamp = now()
        async with self._lease() as conn:
            if request_spec:
                scope = request_spec['scope']
                await self._run(conn, 'SELECT pg_advisory_xact_lock(hashtext(%s))',
                                ('request:' + scope,))
                row = await self._one(conn, 'SELECT * FROM request_keys '
                                      'WHERE scope_key=%s FOR UPDATE', (scope,))
                if row and float(row['expires_at']) < time.time():
                    await self._run(conn, 'DELETE FROM request_keys WHERE scope_key=%s',
                                    (scope,))
                    row = None
                if row:
                    if row['fingerprint'] != request_spec['fingerprint']:
                        raise IdempotencyConflict('该幂等键已用于不同内容的请求')
                    if row.get('task_id'):
                        return {'task_id': row['task_id'], 'run_id': row['run_id'],
                                'session_id': row['session_id'], 'replayed': True}
                    # 旧版本的孤立占位无法证明没有执行过，不可盲目重建任务。
                    raise IdempotencyInFlight('旧请求占位尚未关联任务，需要核实后重试')
                else:
                    await self._run(conn, 'INSERT INTO request_keys '
                        '(scope_key, client_key, tenant_id, owner_user_id, endpoint, '
                        'fingerprint, created_at, expires_at) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                        (scope, request_spec['client_key'], tenant_id, owner_user_id,
                         'tasks.create', request_spec['fingerprint'], stamp,
                         time.time() + settings.idempotency_ttl_seconds))

            await self._run(conn, 'INSERT INTO sessions '
                '(session_id, tenant_id, owner_user_id, created_at, updated_at) '
                'VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (session_id, tenant_id, owner_user_id, stamp, stamp))
            session = await self._one(conn, 'SELECT * FROM sessions '
                                      'WHERE session_id=%s FOR UPDATE', (session_id,))
            if (session['tenant_id'], session['owner_user_id']) != (tenant_id, owner_user_id):
                raise PermissionError('会话不属于当前主体')
            current = await self._one(conn,
                'SELECT r.task_id, r.run_id FROM tasks t JOIN tasks_meta m '
                'ON m.task_id=t.task_id JOIN runs r ON r.run_id=m.current_run_id '
                "WHERE t.session_id=%s AND r.status NOT IN "
                "('succeeded','partially_succeeded','failed','cancelled') "
                'ORDER BY r.created_at LIMIT 1', (session_id,))
            if current:
                raise SessionGoalConflict(current['task_id'], current['run_id'])
            await self._run(conn, 'INSERT INTO tasks '
                '(task_id,session_id,message,status,created_at,updated_at) '
                "VALUES (%s,%s,%s,'queued',%s,%s)",
                (task_id, session_id, message, stamp, stamp))
            await self._run(conn, 'INSERT INTO tasks_meta '
                '(task_id,tenant_id,owner_user_id,goal,current_run_id,created_at,scene_context_json) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (task_id, tenant_id, owner_user_id, message, run_id, stamp,
                 json.dumps(scene or {}, ensure_ascii=False)))
            await self._run(conn, 'INSERT INTO runs '
                '(run_id,task_id,tenant_id,status,owner_user_id,owner_role,platform_id,platform_subject,created_at,updated_at,trace_id) '
                "VALUES (%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s,%s)",
                (run_id, task_id, tenant_id, owner_user_id, owner_role, platform_id, platform_subject, stamp, stamp,
                 telemetry.trace_id.get() or uuid.uuid4().hex))
            await self._run(conn, 'INSERT INTO messages (session_id,role,content,created_at) '
                "VALUES (%s,'user',%s,%s)", (session_id, message, stamp))
            await self._run(conn, 'UPDATE sessions SET updated_at=%s WHERE session_id=%s',
                            (stamp, session_id))
            if request_spec:
                await self._run(conn, 'UPDATE request_keys SET task_id=%s,run_id=%s,session_id=%s '
                    'WHERE scope_key=%s', (task_id, run_id, session_id, request_spec['scope']))
        return {'task_id': task_id, 'run_id': run_id, 'session_id': session_id,
                'replayed': False}

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
                    await self._run(conn, "SELECT pg_notify('agent_task_events', %s)",
                                    (task_id,))
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

    async def scene_context(self, task_id: str) -> dict:
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'SELECT scene_context_json FROM tasks_meta WHERE task_id=%s',
                (task_id,))
        if not row:
            return {}
        raw = row.get('scene_context_json') or ''
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    async def task_meta(self, task_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM tasks_meta WHERE task_id=%s', (task_id,))
        return dict(row) if row else None
