"""动作账本、尝试、产物、工具结果和外部任务。

`commit_plan`、`admit_action`、`settle_action`、`finish_run_owned`、
`pause_at_boundary` 也在这里：它们写动作和步骤，并通过 `self` 调用运行侧的
`_owned`、`save_snapshot`、`mark_inputs_applied`。方法体没有改。
"""
import json
import time
import uuid

from webpage_agent.store.pg_store import _decode_action, _decode_manifest, now

class PgActions:
    """PgActions 方法组。见模块说明。"""

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
        from webpage_agent.governance import execution_control

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
                           decision_intent_version: int | None = None,
                           authorization: dict | None = None) -> dict:
        """**原子地**取得这次工具调用的准入资格。

        同一事务内按固定顺序做四件事：校验租约与控制请求 → 查/建 action →
        建 attempt 并置 in_flight → 交还。锁顺序固定（run → action → attempt），
        避免两个执行者在不同顺序上互等。

        与 `pause` 的先后关系由同一行锁裁决（文档 §7.2）：
        - pause 先提交 → 这里看到 `pause_requested`，**不授予调用资格**；
        - 这里先准入 → 该步视为在途，pause 只禁止后续动作。
        """
        from webpage_agent.governance import execution_control

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
                    await self._attach_grant(conn, data['action_id'], authorization)
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
            await self._attach_grant(conn, action_id, authorization)
            return {'status': 'admitted', 'action_id': action_id,
                    'attempt_id': attempt_id}

    async def _attach_grant(self, conn, action_id: str,
                            authorization: dict | None) -> None:
        """在准入事务里写下放行依据，并预留批准。失败则整笔准入回滚。"""
        if not authorization:
            return
        from webpage_agent.governance import approvals as approvals_module
        await self._run(
            conn,
            'INSERT INTO tool_authorizations '
            '(authorization_id, action_id, tenant_id, user_id, tool_key, '
            ' schema_hash, args_hash, resource_hash, credential_binding, '
            ' credential_subject, credential_revision, policy_revision, '
            ' approval_id, expires_at, status, created_at) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            ('auth_' + uuid.uuid4().hex[:16], action_id,
             str(authorization.get('tenant_id') or ''),
             str(authorization.get('user_id') or ''),
             str(authorization.get('tool_key') or ''),
             str(authorization.get('schema_hash') or ''),
             str(authorization.get('args_hash') or ''),
             str(authorization.get('resource_hash') or ''),
             str(authorization.get('credential_binding') or ''),
             str(authorization.get('credential_subject') or ''),
             int(authorization.get('credential_revision') or 0),
             int(authorization.get('policy_revision') or 0),
             str(authorization.get('approval_id') or ''),
             0.0, 'admitted', now()))
        approval_id = str(authorization.get('approval_id') or '')
        if not approval_id:
            return
        item_key = str(authorization.get('approval_item_key') or 'call')
        row = await self._one(
            conn, 'SELECT * FROM approvals WHERE approval_id=%s FOR UPDATE',
            (approval_id,))
        if row is None or not approvals_module.is_valid(dict(row), now_ts=time.time()):
            raise approvals_module.ApprovalMissing('批准不可用，拒绝发送')
        existing = await self._one(
            conn,
            'SELECT * FROM approval_reservations WHERE approval_id=%s '
            'AND action_id=%s AND manifest_item_key=%s',
            (approval_id, action_id, item_key))
        if existing is not None:
            return
        manifest = approvals_module.parse_manifest(dict(row))
        used = await self._one(
            conn,
            'SELECT COALESCE(SUM(units),0) AS used FROM approval_reservations '
            "WHERE approval_id=%s AND status IN ('reserved','consumed')",
            (approval_id,))
        used_units = int((used['used'] if used else 0) or 0)
        limit = int(manifest.limits.get('max_units') or manifest.max_actions or 0)
        if limit and used_units + 1 > limit:
            raise approvals_module.ApprovalConflict('批准次数已用完')
        await self._run(
            conn,
            'INSERT INTO approval_reservations '
            '(reservation_id, approval_id, action_id, manifest_item_key, units, '
            ' status, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
            ('res_' + uuid.uuid4().hex[:16], approval_id, action_id, item_key,
             1, approvals_module.RESERVED, now()))

    async def settle_action(self, context, *, run_id: str, plan_version: int,
                            step_id: str, action_id: str,
                            attempt_id: str | None, status: str,
                            receipt: dict | None = None,
                            error_type: str | None = None,
                            step_status: str | None = None,
                            state: dict | None = None,
                            next_step_id: str = '',
                            snapshot_schema: int | None = None) -> dict:
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
                                         observation_seq=int((water or {}).get('observation_seq') or 0),
                                         snapshot_schema=snapshot_schema)
            return {'action_id': action_id, 'status': status,
                    'observation_seq': int((water or {}).get('observation_seq') or 0)}

    async def settle_action_accepted(self, context, *, run_id: str,
                                     plan_version: int, step_id: str,
                                     action_id: str, attempt_id: str | None,
                                     receipt: dict,
                                     external: dict,
                                     state: dict | None = None,
                                     next_step_id: str = '',
                                     snapshot_schema: int | None = None) -> dict:
        """原子结算「已受理」：动作回执 + external task + 等待检查点一次提交。

        第八阶段第 4 点：accepted 结算与任务登记必须在**同一数据库事务**里
        保存（提交动作回执、external task、调用关联、等待检查点、`waiting_external`）。
        任何提交断点后，accepted 与外部任务登记要么同时存在、要么同时回滚；
        恢复不会因漏登记而重新提交（重复副作用）。

        动作状态写 `accepted`（不是 succeeded——提交成功但远端业务未完成），
        `phase='accepted'`；attempt 结束但不标错误；runs 置 `waiting_external=1`。
        `external` 是登记所需的字段（remote_task_id / contract_id / version /
        submission_key / item_key / deadline_at / next_poll_at / …）。
        """
        stamp = now()
        async with self._lease() as conn:
            await self._owned(conn, context, run_id=run_id,
                              require_status=('running', 'paused'))
            await self._run(
                conn, "UPDATE actions SET status='accepted', receipt_json=%s, "
                "phase='accepted', updated_at=%s WHERE action_id=%s",
                (json.dumps(receipt or {}, ensure_ascii=False, default=str),
                 stamp, action_id))
            if attempt_id:
                await self._run(
                    conn, 'UPDATE action_attempts SET ended_at=%s, error_type=NULL '
                    'WHERE attempt_id=%s', (stamp, attempt_id))
            # 步骤停在受理阶段，不标 succeeded/ended。
            await self._run(
                conn, "UPDATE steps SET status='accepted', phase='accepted', "
                'output_json=COALESCE(%s, output_json) '
                'WHERE run_id=%s AND plan_version=%s AND step_id=%s',
                (json.dumps(receipt, ensure_ascii=False, default=str),
                 run_id, int(plan_version), step_id))
            # 结果水位 +1（受理也是一次相关结果落库）。
            water = await self._one(
                conn, 'UPDATE runs SET observation_seq=COALESCE(observation_seq,0)+1, '
                'waiting_external=1 '
                'WHERE run_id=%s RETURNING observation_seq', (run_id,))
            # external task 在同一事务登记（复用 conn，不 commit）。
            await self.register_external_task(
                external_task_id=str(external.get('external_task_id') or ''),
                remote_task_id=str(external.get('remote_task_id') or ''),
                tenant_id=str(external.get('tenant_id') or 'default'),
                owner_user_id=str(external.get('owner_user_id') or 'default'),
                server_binding_id=str(external.get('server_binding_id') or ''),
                credential_binding=str(external.get('credential_binding') or ''),
                credential_subject=str(external.get('credential_subject') or ''),
                tool_key=str(external.get('tool_key') or ''),
                contract_id=str(external.get('contract_id') or ''),
                contract_version=int(external.get('contract_version') or 1),
                run_id=run_id, step_id=step_id, action_id=action_id,
                accepted_result_id=str(external.get('accepted_result_id') or ''),
                submission_key=str(external.get('submission_key') or ''),
                item_key=str(external.get('item_key') or ''),
                deadline_at=float(external.get('deadline_at') or 0),
                next_poll_at=float(external.get('next_poll_at') or 0),
                conn=conn)
            if state is not None:
                await self.save_snapshot(run_id, plan_version=plan_version,
                                         state=state, next_step_id=next_step_id,
                                         conn=conn,
                                         observation_seq=int(
                                             (water or {}).get('observation_seq') or 0),
                                         snapshot_schema=snapshot_schema)
            return {'action_id': action_id, 'status': 'accepted',
                    'observation_seq': int((water or {}).get('observation_seq') or 0)}

    async def complete_external_task_and_wake(self, external_task_id: str,
                                             epoch: int, *,
                                             state: str,
                                             output_manifest_ref: str = '',
                                             last_result_id: str = '',
                                             error_code: str = '') -> dict | None:
        """把外部任务推进到终态，并在**同一事务**里唤醒 run。

        第八阶段第 8 点：外部结果与待唤醒记录同事务提交。poller 不直接改写 Pi
        messages——这里只做两件事：① 条件更新 external_tasks 到终态（带 owner/
        epoch/租约门禁）；② 条件更新 runs（requeue 或 needs_reconcile），让获得
        执行权的宿主吸收结果。旧 poller / 重复完成 / 失租分支返回 None，调用方
        不得继续更新快照或唤醒。

        唤醒使用**状态条件更新**：不能复活 cancelled、不能覆盖 paused、不能抢占
        正在运行的新执行者（只有 waiting_external 且无有效租约的 run 才会被
        requeue）。
        """
        stamp = now()
        async with self._lease() as conn:
            task = await self._one(
                conn,
                "UPDATE external_tasks SET state=%s, output_manifest_ref=%s, "
                'last_result_id=%s, error_code=%s, last_observed_at=%s, '
                'poll_owner=%s, poll_lease_expires_at=%s, version=version+1, '
                'updated_at=%s '
                'WHERE external_task_id=%s AND poll_epoch=%s '
                'AND state IN (%s, %s, %s) RETURNING *',
                (state, output_manifest_ref, last_result_id, error_code,
                 time.time(), '', 0, stamp, external_task_id, int(epoch),
                 'accepted', 'running', 'materializing'))
            if task is None:
                # 旧 poller / 重复完成 / 失租 / 状态不符：不写、不唤醒。
                return None
            task = dict(task)
            run_id = str(task.get('run_id') or '')
            if run_id:
                if state == 'completed':
                    # 结果已可靠保存：把 run 放回队列，等宿主吸收结果。只在
                    # waiting_external 且无有效租约（执行者已退出）时唤醒，避免
                    # 抢占正在运行的新执行者、覆盖 paused、复活 cancelled。
                    await self._run(
                        conn,
                        "UPDATE runs SET status='queued', waiting_external=0, "
                        'error=NULL, lease_owner=NULL, lease_expires_at=NULL, '
                        'updated_at=%s WHERE run_id=%s '
                        "AND status='waiting_external' "
                        'AND (lease_owner IS NULL OR lease_expires_at IS NULL '
                        '    OR lease_expires_at < %s)',
                        (stamp, run_id, time.time()))
                elif state == 'failed':
                    # 失败但结果已确认：run 交给人工核实或按失败收尾。
                    await self._run(
                        conn,
                        "UPDATE runs SET status='needs_reconcile', "
                        'waiting_external=0, error=%s, lease_owner=NULL, '
                        'lease_expires_at=NULL, updated_at=%s '
                        "WHERE run_id=%s AND status='waiting_external'",
                        (f'外部任务 {external_task_id} 失败', stamp, run_id))
            return task

    async def request_external_cancel(self, external_task_id: str) -> dict | None:
        """持久化远端取消**意图**（第八阶段第 9 点）。

        只登记意图，不声称已取消——远端是否真的取消要经合同取消能力查询确认。
        幂等：已请求过返回既有行。对已完成/已失败/已取消的任务不再登记。
        """
        async with self._lease() as conn:
            row = await self._one(
                conn,
                "UPDATE external_tasks SET cancel_requested=1, updated_at=%s "
                "WHERE external_task_id=%s AND state IN "
                "('accepted','running','materializing') RETURNING *",
                (now(), external_task_id))
            await conn.commit()
            return dict(row) if row else None

    async def confirm_external_cancel(self, external_task_id: str,
                                      epoch: int, *, confirmed: bool) -> dict | None:
        """按查询结果确认远端已取消（第八阶段第 9 点）。

        只有持当前轮询代次、且任务仍开放时才写 `cancelled`；远端未确认取消则
        保持 `running`（如实保留状态，不声称已取消）。取消与完成的竞争由代次
        门禁裁决：完成已提交则这里写不进（state 不在开放集合）。
        """
        target = 'cancelled' if confirmed else 'running'
        async with self._lease() as conn:
            row = await self._one(
                conn,
                "UPDATE external_tasks SET state=%s, updated_at=%s "
                "WHERE external_task_id=%s AND poll_epoch=%s "
                "AND state IN ('accepted','running','materializing') RETURNING *",
                (target, now(), external_task_id, int(epoch)))
            await conn.commit()
            return dict(row) if row else None

    async def finish_run_owned(self, context, *, run_id: str, status: str,
                               error: str | None = None,
                               state: dict | None = None,
                               expected_intent_version: int | None = None,
                               plan_version: int | None = None,
                               compensated_at: str | None = None,
                               observation_seq: int | None = None,
                               next_route: str = '',
                               require_no_pending_inputs: bool = False,
                               final_message: str | None = None,
                               final_message_key: str = '',
                               session_id: str = '',
                               snapshot_schema: int | None = None) -> bool:
        """带租约与**要求版本**条件地写 run 终态。返回 False = 已失去执行权(不写)。

        收尾也必须过版本门禁（文档 §5 第十一步）：旧版本跑出来的"完成"不能
        盖在新要求上面。若用户在收尾前补了一句要求，这里抛 `IntentChanged`——
        调用方应当回到消费循环，而不是把这个 run 标成结束。

        第七阶段：可选的 `final_message` / `final_message_key` / `session_id` 让
        最终答复在**同一事务**里提交，并用稳定去重键（`(session_id, dedupe_key)`
        唯一索引）保证恢复补收尾不会重复追加最终消息。
        """
        from webpage_agent.governance import execution_control

        async with self._lease() as conn:
            try:
                # 第七阶段收口：终态只允许从 running/paused 进入。已持久化终态
                # （succeeded/failed/cancelled/needs_reconcile 等）不得被迟到
                # 消息覆盖——require_status 让重复收尾抛 LeaseLost。
                row = await self._owned(conn, context, run_id=run_id,
                                        require_status=('running', 'paused'))
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
                    observation_seq=observation_seq, next_route=next_route,
                    snapshot_schema=snapshot_schema)
            # 最终答复与终态同事务提交，按稳定去重键幂等（第七阶段）。
            if final_message is not None and session_id:
                await self._run(
                    conn, 'INSERT INTO messages '
                    '(session_id, role, content, created_at, dedupe_key) '
                    "VALUES (%s, 'assistant', %s, %s, %s) ON CONFLICT DO NOTHING",
                    (session_id, final_message, now(), final_message_key or ''))
            return True

    async def pause_at_boundary(self, context, *, run_id: str, plan_version: int,
                                state: dict, next_step_id: str = '') -> bool:
        """在安全边界暂停：写 paused + 快照，交还租约。返回 False = 已失租。"""
        from webpage_agent.governance import execution_control

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

    # ---------- 工具运行时：通用结果 ----------

    async def commit_tool_result(self, context: dict, staged: dict,
                                 envelope: dict) -> str:
        """登记一条已经落盘的结果。读接口只认这张登记，不认目录里的清单文件。"""
        from webpage_agent.tools import tool_results
        async with self._lease() as conn:
            return await tool_results.commit_result_in_tx(
                conn, context, staged, envelope)

    async def result_manifest(self, result_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM tool_result_manifests WHERE result_id=%s',
                (result_id,))
            return _decode_manifest(dict(row)) if row else None

    async def result_parts(self, result_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                'SELECT * FROM tool_result_parts WHERE result_id=%s ORDER BY ordinal',
                (result_id,))
            return [dict(row) for row in rows]

    # ---------- 工具运行时：外部任务 ----------

    async def register_external_task(self, *, external_task_id: str,
                                     remote_task_id: str, tenant_id: str,
                                     owner_user_id: str, server_binding_id: str,
                                     credential_binding: str, credential_subject: str,
                                     tool_key: str, contract_id: str,
                                     contract_version: int, run_id: str,
                                     step_id: str, action_id: str,
                                     accepted_result_id: str, submission_key: str,
                                     item_key: str = '', deadline_at: float = 0.0,
                                     next_poll_at: float = 0.0,
                                     conn=None) -> dict:
        """登记一个已受理的外部任务。

        唯一约束 `(run_id, action_id, item_key)` 保证**同一个动作只登记一次**：
        重复提交不会变成两个任务（那意味着两份费用）。

        第八阶段第 4 点：`conn` 允许复用外部事务连接（在 `settle_action` 同一
        事务里登记），**不能自行 commit**——事务边界由调用方持有。传入 `conn`
        时只在事务里执行语句；不传时走 `self._lease()` 自己开事务。
        """
        stamp = now()
        sql = ('INSERT INTO external_tasks '
               '(external_task_id, remote_task_id, tenant_id, owner_user_id, '
               ' server_binding_id, credential_binding, credential_subject, '
               ' tool_key, contract_id, contract_version, run_id, step_id, action_id, '
               ' accepted_result_id, submission_key, item_key, state, version, '
               ' next_poll_at, deadline_at, last_observed_at, created_at, updated_at) '
               'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
               'ON CONFLICT (run_id, action_id, item_key) DO NOTHING RETURNING *')
        params = (external_task_id, remote_task_id, tenant_id, owner_user_id,
                  server_binding_id, credential_binding, credential_subject,
                  tool_key, contract_id, int(contract_version), run_id, step_id,
                  action_id, accepted_result_id, submission_key, item_key,
                  'accepted', 1, float(next_poll_at), float(deadline_at),
                  time.time(), stamp, stamp)
        if conn is not None:
            row = await self._one(conn, sql, params)
            if row is None:
                # 已登记过：返回既有行（不新建）
                existing = await self._one(
                    conn,
                    'SELECT * FROM external_tasks WHERE run_id=%s AND action_id=%s '
                    'AND item_key=%s', (run_id, action_id, item_key))
                return dict(existing) if existing else {}
            return dict(row)
        async with self._lease() as conn:
            row = await self._one(conn, sql, params)
            if row is None:
                # 已经登记过：返回既有行（不新建）
                existing = await self._one(
                    conn,
                    'SELECT * FROM external_tasks WHERE run_id=%s AND action_id=%s '
                    'AND item_key=%s', (run_id, action_id, item_key))
                await conn.commit()
                return dict(existing) if existing else {}
            await conn.commit()
            return dict(row)

    async def external_task(self, external_task_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM external_tasks WHERE external_task_id=%s',
                (external_task_id,))
            return dict(row) if row else None

    async def external_tasks_of_run(self, run_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                'SELECT * FROM external_tasks WHERE run_id=%s ORDER BY created_at',
                (run_id,))
            return [dict(row) for row in rows]

    async def due_external_tasks(self, now_ts: float | None = None,
                                 limit: int = 10) -> list[dict]:
        """到期的外部任务（该轮询了）。"""
        current = now_ts if now_ts is not None else time.time()
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                "SELECT * FROM external_tasks WHERE state IN "
                "('accepted','running','materializing') "
                'AND next_poll_at <= %s ORDER BY next_poll_at LIMIT %s',
                (current, int(limit)))
            return [dict(row) for row in rows]

    async def claim_external_task_poll(self, external_task_id: str, owner: str,
                                       lease_seconds: float = 60.0) -> dict | None:
        """领取一次轮询（带代次）。旧 Worker 不能覆盖新结果。

        第八阶段第 6 点：领取时**重新检查开放状态**（accepted/running/
        materializing）与**到期时间**（next_poll_at 已到、deadline 未超），
        并做租约可用性检查。只有仍开放、且上一轮租约已释放/过期才可领取。
        """
        current = time.time()
        async with self._lease() as conn:
            row = await self._one(
                conn,
                "UPDATE external_tasks SET poll_owner=%s, poll_epoch=poll_epoch+1, "
                'poll_lease_expires_at=%s, updated_at=%s '
                "WHERE external_task_id=%s "
                "AND state IN ('accepted','running','materializing') "
                'AND next_poll_at <= %s '
                "AND (poll_owner IS NULL OR poll_owner='' "
                '    OR poll_lease_expires_at < %s) RETURNING *',
                (owner, current + float(lease_seconds), now(), external_task_id,
                 current, current))
            await conn.commit()
            return dict(row) if row else None

    async def finish_external_poll(self, external_task_id: str, epoch: int,
                                   **fields) -> dict | None:
        """结束这一轮查询。代次对不上就什么都不写，避免旧轮询覆盖新结果。

        第八阶段第 6 点：写回时检查 owner、epoch、有效期（poll_lease_expires_at）
        及**允许的状态迁移**。旧 poller（epoch 不符 / 租约已过期）、重复完成、
        失租分支都不得写回——条件不满足返回 None，调用方据此**不得**继续更新
        快照或唤醒 run。
        """
        allowed = ('state', 'version', 'next_poll_at', 'deadline_at',
                   'last_observed_at', 'last_result_id', 'output_manifest_ref',
                   'error_code', 'remote_task_id')
        sets = ['poll_owner=%s', 'poll_lease_expires_at=%s']
        params: list = ['', 0]
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f'{key}=%s')
            params.append(value)
        params.extend([now(), external_task_id, int(epoch), time.time()])
        async with self._lease() as conn:
            row = await self._one(
                conn,
                f"UPDATE external_tasks SET {', '.join(sets)}, updated_at=%s "
                'WHERE external_task_id=%s AND poll_epoch=%s '
                'AND poll_lease_expires_at > %s RETURNING *',
                tuple(params))
            await conn.commit()
            return dict(row) if row else None
