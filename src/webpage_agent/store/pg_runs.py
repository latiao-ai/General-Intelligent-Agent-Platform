"""运行、租约、快照、补充输入、计划和步骤、自主决策。

运行级预算的 `consume_budget` / `refund_budget` 在 `pg_budget.py`：它们只通过
`self._owned` 跟这里协作。方法体没有改。
"""
import json
import time
import uuid

from webpage_agent.observe import telemetry
from webpage_agent.core.errors import DecisionBudgetExceeded
from webpage_agent.store.pg_store import _decode_run, now

class PgRuns:
    """PgRuns 方法组。见模块说明。"""

    async def create_run(self, run_id: str, task_id: str, *, budget: dict | None = None,
                   tenant_id: str = 'default', status: str = 'queued',
                   owner_user_id: str = 'default', owner_role: str = 'member',
                   platform_id: str | None = None,
                   runtime_kind: str | None = None) -> None:
        # 引擎归属在**创建时**确定：默认走全局 default_runtime_kind（只影响新建），
        # 显式传入则用传入值。恢复时不走这里，读库里已存的归属。
        from webpage_agent.agent import runtime_identity
        kind = runtime_identity.normalize_kind(
            runtime_kind or runtime_identity.default_kind())
        if not kind:
            raise runtime_identity.UnknownRuntimeKind(
                run_id, runtime_kind or '')
        version = runtime_identity.version_for_kind(kind)
        schema = runtime_identity.schema_for_kind(kind)
        stamp = now()
        async with self._lease() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('retention:'+task_id,))
            await self._run(conn, 'INSERT INTO runs (run_id, task_id, tenant_id, status, '
                'budget_json, consumed_json, owner_user_id, owner_role, platform_id, platform_subject, '
                'runtime_kind, runtime_version, runtime_state_schema, '
                'created_at, updated_at, trace_id) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '
                "COALESCE(%s, (SELECT platform_id FROM runs WHERE task_id=%s ORDER BY created_at LIMIT 1), ''), "
                "COALESCE((SELECT platform_subject FROM runs WHERE task_id=%s ORDER BY created_at LIMIT 1), ''), "
                '%s, %s, %s, %s, %s, %s)',
                (run_id, task_id, tenant_id, status,
                 json.dumps(budget or {}, ensure_ascii=False), '{}',
                 str(owner_user_id or 'default'), str(owner_role or 'member'),
                 platform_id, task_id, task_id,
                 kind, int(version), int(schema),
                 stamp, stamp, telemetry.trace_id.get() or uuid.uuid4().hex))
            await self._run(conn, 'UPDATE tasks_meta SET current_run_id=%s WHERE task_id=%s',
                (run_id, task_id))
            await conn.commit()

    async def run(self, run_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM runs WHERE run_id=%s', (run_id,))
        return _decode_run(dict(row)) if row else None

    async def create_turn_run(self, *, run_id: str, tenant_id: str,
                              owner_user_id: str, lease_owner: str,
                              lease_epoch: int, lease_ttl: float,
                              owner_role: str = 'member',
                              budget: dict | None = None) -> dict | None:
        """为会话 turn（chat/query）建一条内部执行记录（run 行）。

        第十二阶段缺口修复（评审点 1）：普通查询没有业务 run，但仍有用户、租户、
        turn 与费用归属。这里为 turn_id 建一条 `runs` 行作为**内部查询执行记录**，
        让只读工具经 `governed_call` 走完整治理链（授权 → 预算 → 准入 → 真实调用 →
        结算），而不是靠关闭网关检查或无条件放行「无 run 调用」。

        租约直接复用 turn 领取时的 `lease_owner`/`lease_epoch`（与 `session_turns`
        一致），并设成「运行中 + 租约未过期」：
        - `_owned`（admit_action 的 require_status=('running',)）据此放行；
        - `claim_run` 的领取条件 `lease_expires_at < now` 保证租约未过期时**不会**
          被 Worker 业务领取池领走（内部记录不进业务调度）。
        """
        stamp = now()
        expires = time.time() + float(lease_ttl)
        async with self._lease() as conn:
            await self._run(conn,
                'INSERT INTO runs (run_id, task_id, tenant_id, status, '
                'budget_json, consumed_json, owner_user_id, owner_role, '
                'lease_owner, lease_expires_at, lease_epoch, '
                'runtime_kind, runtime_version, runtime_state_schema, '
                'created_at, updated_at, trace_id) '
                "VALUES (%s, %s, %s, 'running', %s, '{}', %s, %s, "
                "%s, %s, %s, 'pi', 1, 2, %s, %s, %s) "
                'ON CONFLICT (run_id) DO NOTHING',
                (run_id, run_id, tenant_id,
                 json.dumps(budget or {}, ensure_ascii=False), owner_user_id,
                 str(owner_role or 'member'),
                 lease_owner, expires, int(lease_epoch or 0),
                 stamp, stamp, uuid.uuid4().hex))
            await conn.commit()
        return await self.run(run_id)

    async def settle_turn_run(self, *, run_id: str, status: str,
                              lease_owner: str, lease_epoch: int) -> bool:
        """收尾内部查询执行记录：turn 结算后置终态，清租约，不留 running 僵尸行。

        带租约门禁（owner + epoch）：过期执行者不能收尾。返回是否成功。
        """
        stamp = now()
        async with self._lease() as conn:
            cur = await self._run(conn,
                'UPDATE runs SET status=%s, lease_expires_at=0, updated_at=%s '
                'WHERE run_id=%s AND lease_owner=%s AND lease_epoch=%s',
                (status, stamp, run_id, lease_owner, int(lease_epoch or 0)))
            await conn.commit()
        return bool(cur.rowcount)

    async def runtime_identity(self, run_id: str) -> dict:
        """读并校验 run 的引擎归属（恢复/分流入口的唯一权威来源）。

        返回 `{kind, version, state_schema}`；未知引擎抛 `UnknownRuntimeKind`，
        不兼容版本抛 `IncompatibleRuntimeVersion`。**不回退默认引擎**。
        """
        from webpage_agent.agent import runtime_identity
        row = await self.run(run_id)
        if row is None:
            raise runtime_identity.UnknownRuntimeKind(run_id, '<missing>')
        kind = runtime_identity.normalize_kind(str(row.get('runtime_kind') or ''))
        if not kind:
            # 空值是迁移前的存量行（v5 才加上这列，缺省回填过 langgraph）。
            # 不认识的非空值必须阻断，不能静默改成 pi 再跑一遍。
            stored = str(row.get('runtime_kind') or '')
            if stored in ('', None):
                kind = runtime_identity.KIND_LANGGRAPH
            else:
                raise runtime_identity.UnknownRuntimeKind(run_id, stored)
        version = int(row.get('runtime_version') or 0)
        schema = int(row.get('runtime_state_schema') or 0)
        expected_version = runtime_identity.version_for_kind(kind)
        expected_schema = runtime_identity.schema_for_kind(kind)
        if version and version != expected_version:
            raise runtime_identity.IncompatibleRuntimeVersion(
                run_id, kind, 'runtime_version', version, expected_version)
        if schema and schema != expected_schema:
            raise runtime_identity.IncompatibleRuntimeVersion(
                run_id, kind, 'runtime_state_schema', schema, expected_schema)
        return {'kind': kind, 'version': version, 'state_schema': schema}

    async def adopt_pi_runtime(self, run_id: str, *, lease_owner: str,
                               lease_epoch: int) -> bool:
        """把一条还没有副作用的存量 langgraph run 改挂到 Pi。

        只在当前租约仍属于这个执行者时改。已经是 pi、或租约对不上，返回 False。
        """
        from webpage_agent.agent import runtime_identity
        async with self._lease() as conn:
            cur = await self._run(
                conn,
                "UPDATE runs SET runtime_kind=%s, runtime_version=%s, "
                'runtime_state_schema=%s, updated_at=%s '
                "WHERE run_id=%s AND runtime_kind=%s "
                'AND lease_owner=%s AND lease_epoch=%s',
                (runtime_identity.KIND_PI,
                 runtime_identity.PI_RUNTIME_VERSION,
                 runtime_identity.PI_STATE_SCHEMA,
                 now(), run_id, runtime_identity.KIND_LANGGRAPH,
                 lease_owner, int(lease_epoch or 0)))
            await conn.commit()
            return bool(cur.rowcount)

    async def update_run(self, run_id: str, **fields) -> None:
        allowed = {'status', 'plan_version', 'intent_version',
                   'processed_intent_version', 'budget_json', 'consumed_json',
                   'cancel_requested_at', 'pause_requested', 'lease_owner',
                   'lease_expires_at', 'lease_epoch', 'error', 'compensated_at',
                   'waiting_external', 'owner_user_id', 'owner_role'}
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

    async def update_run_owned(self, run_id: str, *, lease_owner: str,
                               lease_epoch: int, **fields) -> bool:
        """只在租约仍属于这个执行者时改 run。旧 Worker 不能盖住新执行者。"""
        allowed = {'status', 'error', 'waiting_external', 'pause_requested',
                   'cancel_requested_at'}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return False
        columns = ', '.join(f'{key}=%s' for key in updates)
        async with self._lease() as conn:
            cur = await self._run(
                conn, f'UPDATE runs SET {columns}, updated_at=%s '
                'WHERE run_id=%s AND lease_owner=%s AND lease_epoch=%s',
                (*updates.values(), now(), run_id, lease_owner, int(lease_epoch)))
            await conn.commit()
            return cur.rowcount > 0

    async def requeue_run_owned(self, run_id: str, *, lease_owner: str,
                                lease_epoch: int) -> bool:
        """把**自己刚领到、但没能执行**的 run 放回队列，同时释放租约。

        与 `update_run_owned` 分开的原因：`lease_owner` / `lease_expires_at`
        不在它的白名单里（那是有意的——执行者不该随手改自己的租约），而"放回
        队列"恰恰要清掉这两列。门禁照旧：只有当前 owner + epoch 对得上才动，
        别人已经接手的话返回 False，一个字段都不碰。
        """
        async with self._lease() as conn:
            cur = await self._run(
                conn, "UPDATE runs SET status='queued', lease_owner=NULL, "
                'lease_expires_at=NULL, updated_at=%s '
                'WHERE run_id=%s AND lease_owner=%s AND lease_epoch=%s',
                (now(), run_id, lease_owner, int(lease_epoch)))
            await conn.commit()
            return cur.rowcount > 0

    async def claim_run(self, worker_id: str, lease_seconds: float = 60.0,
                  now_ts: float | None = None,
                  run_id: str | None = None) -> dict | None:
        """原子领取一个运行，并递增租约代次（PG 侧带 SKIP LOCKED）。

        `run_id` 给定时只领这一条（API 进程就地执行也要先占租约，Worker 才
        领不到它）；不给就按入队顺序领一条。子查询上的
        `FOR UPDATE SKIP LOCKED` 是文档 §9.2 明确要求的：两个 Worker 并发
        领取时，第二个直接跳过被锁住的行，而不是等锁再抢同一条。

        这里仍是「每个进程各算各的」：并发上限不跨副本，重租户可以占满一个
        进程。改领取条件做集群级公平调度是另一量级的改动，这一轮不动。
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
        from webpage_agent.governance import execution_control

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

    async def verify_lease(self, run_id: str, *, lease_owner: str,
                           lease_epoch: int) -> None:
        '''校验当前执行者仍持有租约（owner + epoch 一致且未过期）。

        第七阶段收口：检查点/快照等**非动作**写入也要过租约门禁，否则过期执行者
        仍能覆盖新执行者的快照。失租/代次不符抛 `LeaseLost`。
        '''
        from webpage_agent.governance import execution_control
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT lease_owner, lease_epoch, lease_expires_at '
                'FROM runs WHERE run_id=%s', (run_id,))
        if row is None:
            raise execution_control.LeaseLost(run_id, '运行不存在')
        if str(row.get('lease_owner') or '') != str(lease_owner) \
                or int(row.get('lease_epoch') or 0) != int(lease_epoch):
            raise execution_control.LeaseLost(
                run_id, f'持有者/代次已变更（库中 {row.get("lease_owner") or "空"}'
                        f'#{row.get("lease_epoch") or 0}）')
        expires = float(row.get('lease_expires_at') or 0)
        if expires and expires < time.time():
            raise execution_control.LeaseLost(run_id, '租约已过期')

    async def save_snapshot(self, run_id: str, *, plan_version: int, state: dict,
                            next_step_id: str = '', artifact_namespace: str = '',
                            conn=None, intent_version: int | None = None,
                            processed_intent_version: int | None = None,
                            observation_seq: int | None = None,
                            next_route: str = '',
                            snapshot_schema: int | None = None) -> None:
        '''写运行快照（恢复所需的最小完整状态）。

        长正文与文件不进快照：它们走产物引用，快照里只留引用与索引。
        快照里的版本字段只用于**排障与核对**：恢复时以 `runs` 的版本为准，
        因为补充输入会改变 `runs.intent_version`，而快照可能还没来得及更新。

        `snapshot_schema` 是快照状态格式的代次：LangGraph 用 1，Pi 用自己的
        schema 代次（第七阶段）。之前写死 1，导致 Pi 快照无法标注版本；
        现在显式传入，UPSERT 也同步更新，避免旧 schema 值残留。
        '''
        schema = int(snapshot_schema if snapshot_schema is not None else 1)
        payload = json.dumps(state, ensure_ascii=False, default=str)
        sql = ('INSERT INTO run_snapshots (run_id, plan_version, snapshot_schema, '
               'state_json, next_step_id, artifact_namespace, updated_at, '
               'intent_version, processed_intent_version, observation_seq, next_route) '
               'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
               'ON CONFLICT (run_id) DO UPDATE SET plan_version=EXCLUDED.plan_version, '
               'snapshot_schema=EXCLUDED.snapshot_schema, '
               'state_json=EXCLUDED.state_json, next_step_id=EXCLUDED.next_step_id, '
               'artifact_namespace=EXCLUDED.artifact_namespace, '
               'updated_at=EXCLUDED.updated_at, '
               'intent_version=EXCLUDED.intent_version, '
               'processed_intent_version=EXCLUDED.processed_intent_version, '
               'observation_seq=EXCLUDED.observation_seq, '
               'next_route=EXCLUDED.next_route')
        args = (run_id, int(plan_version or 1), schema, payload, next_step_id,
                artifact_namespace, now(), int(intent_version or 0),
                int(processed_intent_version or 0), int(observation_seq or 0),
                str(next_route or ''))
        if conn is not None:
            await self._run(conn, sql, args)
            return
        async with self._lease() as own:
            await self._run(own, sql, args)

    async def save_snapshot_owned(self, run_id: str, *, plan_version: int,
                                  state: dict, lease_owner: str,
                                  lease_epoch: int,
                                  next_step_id: str = '',
                                  artifact_namespace: str = '',
                                  intent_version: int | None = None,
                                  processed_intent_version: int | None = None,
                                  observation_seq: int | None = None,
                                  next_route: str = '',
                                  snapshot_schema: int | None = None) -> bool:
        """把快照写入与租约校验放进**同一事务**，返回 False = 已失租（不写）。

        第七阶段 `_persist_snapshot` 之前是「先 `verify_lease` 再独立
        `save_snapshot`」两步：两步之间租约可能被接管，过期执行者仍能把快照
        写进库里覆盖新执行者的进度。这里把「校验 owner + epoch + 未过期」与
        「UPSERT 快照」合进一个事务，条件不满足就一行都不写、返回 False。

        实现：锁住 run 行（`FOR UPDATE`）读 owner/epoch/expires，校验通过再
        写快照，同一事务提交。失租/状态不符返回 False，不抛 LeaseLost——
        调用方（检查点确认）据此回 ok=False 让 Node 停止，而不是炸掉整条桥接。
        """
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT lease_owner, lease_epoch, lease_expires_at '
                'FROM runs WHERE run_id=%s FOR UPDATE', (run_id,))
            if row is None:
                return False
            if str(row.get('lease_owner') or '') != str(lease_owner) \
                    or int(row.get('lease_epoch') or 0) != int(lease_epoch):
                return False
            expires = float(row.get('lease_expires_at') or 0)
            if expires and expires < time.time():
                return False
            await self.save_snapshot(
                run_id, plan_version=plan_version, state=state,
                next_step_id=next_step_id, artifact_namespace=artifact_namespace,
                conn=conn, intent_version=intent_version,
                processed_intent_version=processed_intent_version,
                observation_seq=observation_seq, next_route=next_route,
                snapshot_schema=snapshot_schema)
            return True

    async def run_snapshot(self, run_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM run_snapshots WHERE run_id=%s',
                                  (run_id,))
        if row is None:
            return None
        data = dict(row)
        try:
            data['state'] = json.loads(data.get('state_json') or '{}')
        except ValueError as exc:
            # 第七阶段收口：损坏的 state_json 必须可被识别，不能吞成 {} 冒充空快照。
            raise RuntimeError(
                f'run {run_id} 的快照 state_json 损坏：{exc}') from exc
        data['schema'] = int(data.get('snapshot_schema') or 0)
        return data

    # ---------- 运行中补充输入 ----------
    # 这里的核心不是"存一条消息"，而是**一次事务**里完成五件事：
    # 校验状态 → 幂等查原回执 → 分配序号插日志 → 升 intent_version → 追加事件。
    # 少任何一步都会出现"版本涨了但没有日志"或"日志在、版本没涨"的分叉状态，
    # 而这两种状态都没法安全恢复。

    # 可以接受补充的状态（文档 §2.2）。已收尾/已取消的 run 一律拒绝：
    # 否则"补充"就成了绕过 resume 门禁复活旧任务的旁路。
    #
    # `needs_reconcile` **不在**这里：它既不能 resume，也没有任何执行者会来取补充，
    # 于是输入只会躺进黑洞。真踩过：画布上那次 submit 被服务端拒绝后 run 停在
    # needs_reconcile，用户连发三条「你好」全部落库成 run_inputs 却永远不执行，
    # 界面看起来就是"这个画布发不出消息了"。宁可当场拒绝并说清原因。
    INPUT_ACCEPTING_STATUSES = ('queued', 'running', 'reconciling', 'paused',
                               'waiting_input', 'waiting_approval')

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
        from webpage_agent.agent import run_inputs as run_inputs_module

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
                if status == 'needs_reconcile':
                    # 说得具体一点：这不是"状态不对"，而是有一次写入结果没核实。
                    raise run_inputs_module.InputRejected(
                        'run_needs_reconcile',
                        '这条运行有结果未定的写操作，必须先核实才能继续；'
                        '请先确认那次写入是否已经发生，或在侧栏新建会话继续')
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

    # ---------- 自主决策与预算 ----------

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
        async with self._lease() as conn:
            row = await self._owned(conn, context, run_id=run_id)
            if row is None:
                raise DecisionBudgetExceeded('运行不存在')
            current_intent = int(row.get('intent_version') or 1)
            if intent_version and int(intent_version) != current_intent:
                # 用户要求变了：旧候选直接作废，不预留
                from webpage_agent.governance import execution_control
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
