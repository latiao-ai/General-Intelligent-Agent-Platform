"""定时任务的仓储方法（`scheduled_tasks` / `scheduled_task_runs`）。

设计要点（详见 `docs/scheduled-tasks.md`）：

- **物化 `next_run_at`**：tick 只按 `(enabled, next_run_at)` 索引取到期行，
  不每次扫全表算规则；
- **认领带租约**：`claim_due_schedules` 用 `FOR UPDATE SKIP LOCKED` + `lease_epoch`，
  多副本同时 tick 时同一个任务只会被一个副本拿走（照 `external_tasks` 的形状）；
- **fencing**：调度器推进 `next_run_at` 必须带 `lease_owner + lease_epoch`，
  被接管的旧副本写不进去（否则会把新副本刚推进的下一次踩回去）；
- **occurrence 唯一**：`(schedule_id, due_at)` 唯一索引，是"同一时刻只触发一次"的
  最后一道闸门，不依赖进程内状态；
- 没有外键：`schedule_id`/`run_id` 的完整性由应用层保证（本仓库全库如此）。
"""
import json

from webpage_agent.store.pg_store import now

# 用户可改的字段。调度器内部状态（next_run_at / lease_* / last_*）不在白名单里：
# 让接口能改下一次触发时间，等于让人手写一个"永远不触发"的任务。
USER_FIELDS = ('name', 'prompt', 'enabled', 'schedule_type', 'rrule', 'scheduled_at',
               'valid_from', 'valid_until', 'connector_ids_json', 'delivery_json')
# 调度器可以推进的字段（都过租约门禁）。
TICK_FIELDS = ('next_run_at', 'next_attempt_at', 'last_run_at', 'last_status',
               'last_error_code', 'last_error_message', 'consecutive_failures',
               'deferred_count')
OCCURRENCE_FIELDS = ('status', 'task_id', 'run_id', 'session_id', 'error_code',
                     'error_message', 'summary', 'delivery_status',
                     'delivery_attempts', 'started_at', 'finished_at')
OPEN_STATUSES = ('queued', 'running')


def _decode_schedule(row: dict | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    data['connector_ids'] = json.loads(data.pop('connector_ids_json') or '[]')
    data['delivery'] = json.loads(data.pop('delivery_json') or '{}')
    data['enabled'] = bool(data.get('enabled'))
    return data


def _decode_occurrence(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


class PgSchedules:
    """PgSchedules 方法组。见模块说明。"""

    # ---------- 定义 ----------

    async def create_schedule(self, **fields) -> dict:
        """建一条任务。`next_run_at` 由调用方（接口层）算好再传进来。"""
        stamp = now()
        columns = ('schedule_id', 'tenant_id', 'owner_user_id', 'owner_role',
                   'platform_id', 'platform_subject', 'name', 'prompt', 'enabled',
                   'schedule_type', 'rrule', 'scheduled_at', 'valid_from',
                   'valid_until', 'next_run_at', 'next_attempt_at',
                   'connector_ids_json', 'delivery_json', 'created_at', 'updated_at')
        values = (fields['schedule_id'], fields.get('tenant_id', 'default'),
                  fields.get('owner_user_id', 'default'),
                  fields.get('owner_role', 'member'),
                  fields.get('platform_id', ''), fields.get('platform_subject', ''),
                  fields['name'], fields['prompt'], bool(fields.get('enabled', True)),
                  fields.get('schedule_type', 'recurring'), fields.get('rrule', ''),
                  fields.get('scheduled_at'), fields.get('valid_from'),
                  fields.get('valid_until'), fields.get('next_run_at'),
                  float(fields.get('next_attempt_at') or 0.0),
                  json.dumps(fields.get('connector_ids') or [], ensure_ascii=False),
                  json.dumps(fields.get('delivery') or {}, ensure_ascii=False),
                  stamp, stamp)
        placeholders = ', '.join(['%s'] * len(columns))
        async with self._lease() as conn:
            row = await self._one(conn,
                f'INSERT INTO scheduled_tasks ({", ".join(columns)}) '
                f'VALUES ({placeholders}) RETURNING *', values)
            await conn.commit()
        return _decode_schedule(dict(row)) or {}

    async def schedule(self, schedule_id: str, *, tenant_id: str | None = None,
                       owner_user_id: str | None = None) -> dict | None:
        """按 id 取一条。给了主体就校验归属——不区分"不存在"和"不是你的"。"""
        sql = 'SELECT * FROM scheduled_tasks WHERE schedule_id=%s'
        params: list = [schedule_id]
        if tenant_id is not None:
            sql += ' AND tenant_id=%s'
            params.append(tenant_id)
        if owner_user_id is not None:
            sql += ' AND owner_user_id=%s'
            params.append(owner_user_id)
        async with self._lease() as conn:
            row = await self._one(conn, sql, tuple(params))
        return _decode_schedule(dict(row) if row else None)

    async def list_schedules(self, *, tenant_id: str, owner_user_id: str,
                             status: str = 'all', limit: int = 200) -> list[dict]:
        where = ['tenant_id=%s', 'owner_user_id=%s']
        params: list = [tenant_id, owner_user_id]
        if status == 'active':
            where.append('enabled')
        elif status == 'paused':
            where.append('NOT enabled')
        async with self._lease() as conn:
            rows = await self._all(conn,
                'SELECT * FROM scheduled_tasks WHERE ' + ' AND '.join(where)
                + ' ORDER BY created_at DESC LIMIT %s', (*params, limit))
        return [_decode_schedule(dict(row)) or {} for row in rows]

    async def update_schedule(self, schedule_id: str, *, tenant_id: str,
                              owner_user_id: str, **fields) -> dict | None:
        """改用户可见字段。改完 `next_run_at` 由调用方按新规则重算再写。"""
        updates = {key: value for key, value in fields.items() if key in USER_FIELDS}
        if not updates:
            return await self.schedule(schedule_id, tenant_id=tenant_id,
                                       owner_user_id=owner_user_id)
        assignments = ', '.join(f'{key}=%s' for key in updates)
        values = list(updates.values()) + [now(), schedule_id, tenant_id, owner_user_id]
        async with self._lease() as conn:
            row = await self._one(conn,
                f'UPDATE scheduled_tasks SET {assignments}, updated_at=%s '
                'WHERE schedule_id=%s AND tenant_id=%s AND owner_user_id=%s '
                'RETURNING *', tuple(values))
            await conn.commit()
        return _decode_schedule(dict(row) if row else None)

    async def reschedule(self, schedule_id: str, *, tenant_id: str,
                         owner_user_id: str, next_run_at: float | None) -> None:
        """重算下一次触发时间（改规则/启停/改有效期时用）。"""
        async with self._lease() as conn:
            await self._run(conn,
                'UPDATE scheduled_tasks SET next_run_at=%s, next_attempt_at=0, '
                'deferred_count=0, updated_at=%s '
                'WHERE schedule_id=%s AND tenant_id=%s AND owner_user_id=%s',
                (next_run_at, now(), schedule_id, tenant_id, owner_user_id))
            await conn.commit()

    async def delete_schedule(self, schedule_id: str, *, tenant_id: str,
                              owner_user_id: str) -> bool:
        """删任务，连同它的 occurrence 记录一起删（同一事务）。

        不连带删会让运行记录变成永远查不到的孤儿行；`background_jobs` 里已排队的
        投递作业不用清——消费时找不到 occurrence 就直接当完成。
        """
        async with self._lease() as conn:
            cur = await self._run(conn,
                'DELETE FROM scheduled_tasks WHERE schedule_id=%s '
                'AND tenant_id=%s AND owner_user_id=%s',
                (schedule_id, tenant_id, owner_user_id))
            deleted = cur.rowcount > 0
            if deleted:
                await self._run(conn,
                    'DELETE FROM scheduled_task_runs WHERE schedule_id=%s',
                    (schedule_id,))
            await conn.commit()
            return deleted

    # ---------- 认领（调度器） ----------

    async def claim_due_schedules(self, worker_id: str, *, now_ts: float,
                                  limit: int, lease_seconds: float) -> list[dict]:
        """原子认领到期的任务；认领后租约在手，别人 tick 会跳过。

        认领**不推进** `next_run_at`：要不要跑、跑完推进到哪，取决于
        会话是否空闲、是否错过窗口等判断，那是调度器的事。
        """
        cte_params: list = [float(now_ts), float(now_ts), float(now_ts), int(limit)]
        update_params: list = [worker_id, float(now_ts) + lease_seconds, now()]
        sql = (
            "WITH candidate AS ("
            "  SELECT schedule_id FROM scheduled_tasks"
            "  WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= %s"
            "    AND next_attempt_at <= %s"
            "    AND (lease_expires_at <= %s OR lease_owner='')"
            "  ORDER BY next_run_at"
            "  FOR UPDATE SKIP LOCKED"
            "  LIMIT %s"
            ") "
            "UPDATE scheduled_tasks s SET lease_owner=%s, lease_expires_at=%s, "
            "  lease_epoch=s.lease_epoch+1, updated_at=%s "
            "FROM candidate c WHERE s.schedule_id=c.schedule_id RETURNING s.*"
        )
        async with self._lease() as conn:
            rows = await self._all(conn, sql, tuple(cte_params + update_params))
            await conn.commit()
        return [_decode_schedule(dict(row)) or {} for row in rows]

    async def finish_schedule_tick(self, schedule_id: str, *, lease_owner: str,
                                   lease_epoch: int, **fields) -> bool:
        """带租约门禁地推进一次 tick 的结果。

        `next_run_at` / `next_attempt_at` / `last_*` 只有当前持有者写得进去；
        被接管的旧副本在这里拿到 `False`，不会把新副本的状态踩回去。
        """
        updates = {key: value for key, value in fields.items() if key in TICK_FIELDS}
        updates.setdefault('lease_owner', '')
        updates.setdefault('lease_expires_at', 0)
        updates['updated_at'] = now()
        assignments = ', '.join(f'{key}=%s' for key in updates)
        values = list(updates.values()) + [schedule_id, lease_owner, int(lease_epoch)]
        async with self._lease() as conn:
            cur = await self._run(conn,
                f'UPDATE scheduled_tasks SET {assignments} '
                'WHERE schedule_id=%s AND lease_owner=%s AND lease_epoch=%s',
                tuple(values))
            await conn.commit()
            return cur.rowcount > 0

    async def claim_schedule_manually(self, schedule_id: str, *, worker_id: str,
                                      lease_seconds: float) -> dict | None:
        """手动"立即运行"：不看到期时间，只要求当前没有别人持有租约。

        与定时触发共用同一条执行路径，区别只在入口：手动触发不推进
        `next_run_at`（否则用户点一下就把今晚的定时任务顶掉了）。
        """
        import time

        stamp = now()
        current = time.time()
        async with self._lease() as conn:
            row = await self._one(conn,
                'UPDATE scheduled_tasks SET lease_owner=%s, lease_expires_at=%s, '
                'lease_epoch=lease_epoch+1, updated_at=%s '
                "WHERE schedule_id=%s AND (lease_expires_at <= %s OR lease_owner='') "
                'RETURNING *',
                (worker_id, current + lease_seconds, stamp, schedule_id, current))
            await conn.commit()
        return _decode_schedule(dict(row) if row else None)

    # ---------- 运行记录 ----------

    async def record_occurrence(self, **fields) -> dict:
        """按 `(schedule_id, due_at)` 幂等写一条 occurrence。

        重复触发同一时刻时更新而不是插入——`uq_scheduled_task_runs_due` 是
        "同一 occurrence 只跑一次"的最后一道闸门，这里不靠应用层判断。
        """
        stamp = now()
        columns = ('occurrence_id', 'schedule_id', 'tenant_id', 'owner_user_id',
                   'due_at', 'status', 'task_id', 'run_id', 'session_id',
                   'error_code', 'error_message', 'summary', 'delivery_status',
                   'delivery_attempts', 'started_at', 'finished_at',
                   'created_at', 'updated_at')
        values = (fields['occurrence_id'], fields['schedule_id'],
                  fields.get('tenant_id', 'default'),
                  fields.get('owner_user_id', 'default'), fields['due_at'],
                  fields.get('status', 'queued'), fields.get('task_id', ''),
                  fields.get('run_id', ''), fields.get('session_id', ''),
                  fields.get('error_code', ''), fields.get('error_message', ''),
                  fields.get('summary', ''), fields.get('delivery_status', ''),
                  int(fields.get('delivery_attempts') or 0),
                  fields.get('started_at'), fields.get('finished_at'),
                  stamp, stamp)
        updates = ', '.join(f'{column}=EXCLUDED.{column}' for column in columns[5:]
                            if column not in ('created_at', 'delivery_attempts'))
        placeholders = ', '.join(['%s'] * len(columns))
        async with self._lease() as conn:
            row = await self._one(conn,
                f'INSERT INTO scheduled_task_runs ({", ".join(columns)}) '
                f'VALUES ({placeholders}) '
                f'ON CONFLICT (schedule_id, due_at) DO UPDATE SET {updates} '
                'RETURNING *', values)
            await conn.commit()
        return _decode_occurrence(dict(row)) or {}

    async def occurrence(self, occurrence_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM scheduled_task_runs WHERE occurrence_id=%s',
                (occurrence_id,))
        return _decode_occurrence(dict(row) if row else None)

    async def occurrence_for_due(self, schedule_id: str, due_at: float) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM scheduled_task_runs WHERE schedule_id=%s AND due_at=%s',
                (schedule_id, float(due_at)))
        return _decode_occurrence(dict(row) if row else None)

    async def open_occurrence(self, schedule_id: str) -> dict | None:
        """该任务是否还有没结束的 occurrence（= 上一轮还在跑）。"""
        async with self._lease() as conn:
            row = await self._one(conn,
                "SELECT * FROM scheduled_task_runs WHERE schedule_id=%s "
                "AND status IN (%s,%s) ORDER BY due_at LIMIT 1",
                (schedule_id, *OPEN_STATUSES))
        return _decode_occurrence(dict(row) if row else None)

    async def open_occurrences(self, *, limit: int = 50) -> list[dict]:
        """所有没结束的 occurrence（含 run_id），供 tick 收敛。"""
        async with self._lease() as conn:
            rows = await self._all(conn,
                "SELECT * FROM scheduled_task_runs WHERE status IN (%s,%s) "
                'ORDER BY created_at LIMIT %s', (*OPEN_STATUSES, limit))
        return [_decode_occurrence(dict(row)) or {} for row in rows]

    async def update_occurrence(self, occurrence_id: str, **fields) -> bool:
        updates = {key: value for key, value in fields.items()
                   if key in OCCURRENCE_FIELDS}
        if not updates:
            return False
        updates['updated_at'] = now()
        assignments = ', '.join(f'{key}=%s' for key in updates)
        values = list(updates.values()) + [occurrence_id]
        async with self._lease() as conn:
            cur = await self._run(conn,
                f'UPDATE scheduled_task_runs SET {assignments} '
                'WHERE occurrence_id=%s', tuple(values))
            await conn.commit()
            return cur.rowcount > 0

    async def list_occurrences(self, schedule_id: str, *, limit: int = 50) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn,
                'SELECT * FROM scheduled_task_runs WHERE schedule_id=%s '
                'ORDER BY due_at DESC LIMIT %s', (schedule_id, limit))
        return [_decode_occurrence(dict(row)) or {} for row in rows]

    async def note_schedule_result(self, schedule_id: str, *, status: str,
                                   error_code: str = '',
                                   error_message: str = '',
                                   succeeded: bool = False) -> None:
        """把一次触发的最终结果写回任务行，供列表展示与成败计数。

        这是**管理动作**：写的人不是持有调度租约的那一方（收敛阶段可能在任何
        副本上跑），所以不带 `lease_owner/epoch` 条件——照 `external_tasks._wake_run`
        的同样理由：它只写 `last_status/last_*` 这类展示字段，不碰 `next_run_at`，
        不存在与调度推进互相覆盖的问题。
        """
        async with self._lease() as conn:
            await self._run(conn,
                'UPDATE scheduled_tasks SET last_status=%s, last_error_code=%s, '
                'last_error_message=%s, '
                'consecutive_failures=CASE WHEN %s THEN 0 '
                'ELSE consecutive_failures + 1 END, updated_at=%s '
                'WHERE schedule_id=%s',
                (status, error_code, error_message, bool(succeeded), now(),
                 schedule_id))
            await conn.commit()

    async def set_session_connectors(self, session_id: str,
                                     connector_ids: list[str] | None) -> None:
        """记下这次触发用的连接器（`None` = 不限制，沿用全部已配置工具）。

        为什么要在派发后写会话行：执行时 `api_hub` 是按**会话**读连接器选择的，
        不是按任务读；不写就会静默退化成"所有工具都能用"。
        """
        if not session_id or connector_ids is None:
            return
        async with self._lease() as conn:
            await self._run(conn,
                'UPDATE sessions SET connector_ids_json=%s WHERE session_id=%s',
                (json.dumps([str(item) for item in connector_ids], ensure_ascii=False),
                 session_id))
            await conn.commit()
