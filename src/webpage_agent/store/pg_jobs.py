"""后台作业和请求幂等键。

`enqueue_job` 到 `purge_request_keys` 原样放在这里。方法体没有改。
"""
import json
import time

from webpage_agent.store.pg_store import _decode_request_key, now

class PgJobs:
    """PgJobs 方法组。见模块说明。"""

    # ---------- 后台任务（outbox） ----------

    async def enqueue_job(self, job_id: str, kind: str, dedupe_key: str,
                    payload: dict | None = None,
                    due_at: float | None = None) -> bool:
        """按 (kind, dedupe_key) 幂等入队；返回是否真的新入队。

        `due_at` 是**调度到期时间**（Unix 秒），与租约期限分开：
        退避的重试要占住"什么时候再轮"，而不是"谁的租约"——把两者塞进同一列，
        旧 Worker 与新 Worker 都会以为对方还活着。
        """
        async with self._lease() as conn:
            cur = await self._run(conn, 'INSERT INTO background_jobs '
                '(job_id, kind, dedupe_key, payload_json, due_at, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
                (job_id, kind, dedupe_key,
                 json.dumps(payload or {}, ensure_ascii=False),
                 float(due_at if due_at is not None else 0.0), now(), now()))
            await conn.commit()
            return cur.rowcount > 0

    async def claim_job(self, worker_id: str, lease_seconds: float = 60.0,
                  now_ts: float | None = None,
                  kind: str = '') -> dict | None:
        """**原子**领取一个到期作业。

        旧实现是「SELECT 一行 → UPDATE 那一行」：两个 Worker 同时跑这段时，
        第二个会拿到**同一条**作业（第一个还没提交，第二个就选中了它），
        于是同一个作业被做两遍——对「给这个会话压缩摘要」这种幂等作业还好，
        对「轮询一次外部任务」就是**重复查询**，对「物化一次产物」就是**重复下载**。

        现在用文档 §10 E2 要求的形状：`FOR UPDATE SKIP LOCKED` 选候选行，
        同一事务里更新 owner/代次/期限并 RETURNING。第二个 Worker 会直接
        **跳过**被锁住的行去领下一条，而不是等锁再抢同一条。

        领取条件：到期（`due_at <= now`）且
        - `queued`（还没人领）；或
        - `running` 但**租约已过期**（领取它的 Worker 死了，作业要能被接手）。

        `lease_epoch` 每次领取 +1：它是 fencing token。旧 Worker 醒来后带着
        旧代次来收尾时，条件更新影响 0 行——**它不能覆盖新 Worker 的结果**。
        """
        current = now_ts if now_ts is not None else time.time()
        # 参数顺序必须与 SQL 里的占位符顺序**严格一致**：CTE 的占位符在前，
        # UPDATE 的在后。（曾经把 kind 的参数追加到末尾，结果 CTE 拿到的是
        #  worker_id —— 过滤条件整个错位。）
        cte_params: list = [current, current]
        kind_clause = ''
        if kind:
            kind_clause = ' AND j.kind=%s'
            cte_params.append(kind)
        update_params: list = [worker_id, current + lease_seconds, now()]
        sql = (
            "WITH candidate AS ("
            "  SELECT job_id FROM background_jobs j"
            "  WHERE j.due_at <= %s"
            "    AND (j.status='queued'"
            "         OR (j.status='running' AND j.lease_expires_at < %s))"
            + kind_clause +
            "  ORDER BY j.due_at, j.created_at"
            "  FOR UPDATE SKIP LOCKED"
            "  LIMIT 1"
            ") "
            "UPDATE background_jobs j SET status='running', lease_owner=%s, "
            "  lease_epoch=j.lease_epoch+1, lease_expires_at=%s, "
            "  attempts=j.attempts+1, updated_at=%s "
            "FROM candidate c WHERE j.job_id=c.job_id RETURNING j.*"
        )
        async with self._lease() as conn:
            row = await self._one(conn, sql, tuple(cte_params + update_params))
            await conn.commit()
        if row is None:
            return None
        data = dict(row)
        data['payload'] = json.loads(data.pop('payload_json') or '{}')
        return data

    async def job(self, job_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM background_jobs WHERE job_id=%s', (job_id,))
        if row is None:
            return None
        data = dict(row)
        data['payload'] = json.loads(data.pop('payload_json') or '{}')
        return data

    async def finish_job(self, job_id: str, *, error: str | None = None,
                   retry: bool = False, worker_id: str = '',
                   epoch: int | None = None) -> bool:
        """收尾一个后台作业。返回 False = "已经不是我的了"。

        `retry=True`：退回 queued 并用退避时间占住租约，下一轮扫描再领。
        一次失败就写 failed 等于把偶发错误当终态，且没人会知道。

        **带 owner + 代次条件**（给了 `worker_id` 时）：作业被接手后，旧 Worker
        醒来时带着旧代次来收尾，必须**写不进去**。否则会出现"新 Worker 已经
        成功收尾，旧 Worker 又把它标成 failed"的经典覆盖 bug——
        文档 §10 E2 明确要求"旧 Worker 无法覆盖新结果"。

        没给 `worker_id` 时不加条件（兼容既有调用点），但那时调用方自己
        负责保证没有并发收尾。
        """
        condition = ''
        params: list = []
        if worker_id:
            condition = ' AND lease_owner=%s'
            params.append(worker_id)
            if epoch is not None:
                condition += ' AND lease_epoch=%s'
                params.append(int(epoch))
        async with self._lease() as conn:
            if error and retry:
                row = await self._one(conn, 'SELECT attempts FROM background_jobs WHERE job_id=%s',
                    (job_id,))
                attempts = int((row['attempts'] if row else 0) or 1)
                delay = min(300.0, 5.0 * (2 ** min(attempts, 6)))
                cur = await self._run(conn, "UPDATE background_jobs SET status='queued', "
                    'last_error=%s, due_at=%s, lease_owner=NULL, lease_expires_at=NULL, '
                    f'updated_at=%s WHERE job_id=%s{condition}',
                    (error, time.time() + delay, now(), job_id, *params))
            else:
                cur = await self._run(conn, "UPDATE background_jobs SET status=%s, last_error=%s, "
                    f'lease_owner=NULL, lease_expires_at=NULL, updated_at=%s '
                    f'WHERE job_id=%s{condition}',
                    ('failed' if error else 'done', error, now(), job_id, *params))
            await conn.commit()
            return bool(cur.rowcount)

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
