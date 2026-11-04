"""服务端绑定、凭据绑定、工具政策和批准。

`server_bindings` 到 `approval_reservations` 原样放在这里。方法体没有改。
"""
import json
import uuid

from webpage_agent.store.pg_store import _decode_credential, _decode_policy, now

class PgGovernance:
    """PgGovernance 方法组。见模块说明。"""

    # ---------- 工具运行时：治理（绑定 / 凭据 / 政策 / 准入授权） ----------

    async def server_bindings(self, tenant_id: str = 'default') -> list[dict]:
        """该租户接了哪些 MCP 服务端。"""
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                'SELECT * FROM mcp_server_bindings WHERE tenant_id=%s '
                'ORDER BY server_id', (tenant_id,))
            return [dict(row) for row in rows]

    async def server_binding(self, tenant_id: str, server_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'SELECT * FROM mcp_server_bindings WHERE tenant_id=%s AND server_id=%s',
                (tenant_id, server_id))
            return dict(row) if row else None

    async def upsert_server_binding(self, *, tenant_id: str, server_id: str,
                                     transport: str = 'streamable_http',
                                     config_ref: str = '', enabled: bool = True,
                                     revision: int = 1) -> dict:
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'INSERT INTO mcp_server_bindings '
                '(binding_id, tenant_id, server_id, transport, config_ref, enabled, '
                ' revision, created_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (tenant_id, server_id) DO UPDATE SET '
                '  transport=EXCLUDED.transport, config_ref=EXCLUDED.config_ref, '
                '  enabled=EXCLUDED.enabled, revision=EXCLUDED.revision '
                'RETURNING *',
                (f'sb_{tenant_id}_{server_id}', tenant_id, server_id, transport,
                 config_ref, 1 if enabled else 0, int(revision), stamp))
            await conn.commit()
            return dict(row)

    async def credential_bindings(self, *, tenant_id: str,
                                  server_binding_id: str = '',
                                  subject_kind: str = '',
                                  subject_id: str = '') -> list[dict]:
        """凭据绑定列表（按主体与服务端过滤）。

        返回的是**元信息**（没有明文密钥）：明文由
        `webpage_agent/core/credentials.py` 按 `credential_ref` 在传输层短时取出。
        """
        sql = 'SELECT * FROM credential_bindings WHERE tenant_id=%s'
        params: list = [tenant_id]
        if server_binding_id:
            sql += ' AND server_binding_id=%s'
            params.append(server_binding_id)
        if subject_kind:
            sql += ' AND subject_kind=%s'
            params.append(subject_kind)
        if subject_id:
            sql += ' AND subject_id=%s'
            params.append(subject_id)
        sql += ' ORDER BY binding_id'
        async with self._lease() as conn:
            rows = await self._all(conn, sql, tuple(params))
            return [_decode_credential(dict(row)) for row in rows]

    async def upsert_credential_binding(self, *, tenant_id: str,
                                         subject_kind: str, subject_id: str,
                                         server_binding_id: str,
                                         credential_ref: str,
                                         scope: dict | None = None,
                                         revision: int = 1,
                                         status: str = 'active',
                                         binding_id: str = '') -> dict:
        """登记一把钥匙。

        `binding_id` 非空时**原地更新那一条**（作用域调整、密钥轮换都用它）：
        每次生成新 id 会在同一服务端上留下两条 `active` 绑定，解析时谁先谁后
        就成了运气。
        """
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'INSERT INTO credential_bindings '
                '(binding_id, tenant_id, subject_kind, subject_id, '
                ' server_binding_id, credential_ref, scope_json, revision, status, '
                ' created_at, updated_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (binding_id) DO UPDATE SET '
                '  credential_ref=EXCLUDED.credential_ref, '
                '  scope_json=EXCLUDED.scope_json, revision=EXCLUDED.revision, '
                '  status=EXCLUDED.status, updated_at=EXCLUDED.updated_at '
                'RETURNING *',
                (binding_id or f'cb_{uuid.uuid4().hex[:16]}', tenant_id, subject_kind,
                 subject_id, server_binding_id, credential_ref,
                 json.dumps(scope or {}, ensure_ascii=False), int(revision),
                 status, stamp, stamp))
            await conn.commit()
            return _decode_credential(dict(row))

    async def tool_policies(self, tenant_id: str = 'default') -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                'SELECT * FROM tool_policies WHERE tenant_id=%s ORDER BY policy_id',
                (tenant_id,))
            return [_decode_policy(dict(row)) for row in rows]

    async def upsert_tool_policy(self, *, tenant_id: str, tool_key: str = '',
                                 role: str = '', user_id: str = '',
                                 resource_scope: str = '', effect: str = 'unknown',
                                 approval_mode: str = 'auto',
                                 limits: dict | None = None, revision: int = 1,
                                 enabled: bool = True) -> dict:
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'INSERT INTO tool_policies '
                '(policy_id, tenant_id, tool_key, role, user_id, resource_scope, '
                ' effect, approval_mode, limits_json, revision, enabled, created_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (tenant_id, tool_key, role, user_id) DO UPDATE SET '
                '  resource_scope=EXCLUDED.resource_scope, effect=EXCLUDED.effect, '
                '  approval_mode=EXCLUDED.approval_mode, limits_json=EXCLUDED.limits_json, '
                '  revision=EXCLUDED.revision, enabled=EXCLUDED.enabled '
                'RETURNING *',
                (f'pol_{uuid.uuid4().hex[:12]}', tenant_id, tool_key, role, user_id,
                 resource_scope, effect, approval_mode,
                 json.dumps(limits or {}, ensure_ascii=False), int(revision),
                 1 if enabled else 0, stamp))
            await conn.commit()
            return _decode_policy(dict(row))

    async def record_authorization(self, *, action_id: str, tenant_id: str,
                                   user_id: str, tool_key: str, schema_hash: str,
                                   args_hash: str, resource_hash: str,
                                   credential_binding: str = '',
                                   credential_subject: str = '',
                                   credential_revision: int = 0,
                                   policy_revision: int = 0,
                                   approval_id: str = '',
                                   expires_at: float = 0.0,
                                   status: str = 'admitted') -> str:
        """记录"这次调用凭什么被放行"。"""
        authorization_id = 'auth_' + uuid.uuid4().hex[:16]
        async with self._lease() as conn:
            await self._run(
                conn,
                'INSERT INTO tool_authorizations '
                '(authorization_id, action_id, tenant_id, user_id, tool_key, '
                ' schema_hash, args_hash, resource_hash, credential_binding, '
                ' credential_subject, credential_revision, policy_revision, '
                ' approval_id, expires_at, status, created_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT DO NOTHING',
                (authorization_id, action_id, tenant_id, user_id, tool_key,
                 schema_hash, args_hash, resource_hash, credential_binding,
                 credential_subject, int(credential_revision),
                 int(policy_revision), approval_id, float(expires_at), status,
                 now()))
            await conn.commit()
        return authorization_id

    # ---------- 工具运行时：批准与预留 ----------

    async def create_approval(self, *, tenant_id: str, owner_user_id: str,
                              requester_user_id: str, run_id: str,
                              manifest: dict, manifest_hash: str,
                              policy_revision: int = 0, expires_at: float = 0.0,
                              limits: dict | None = None) -> dict:
        from webpage_agent.governance import approvals as approvals_module
        approval_id = approvals_module.new_approval_id()
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'INSERT INTO approvals '
                '(approval_id, tenant_id, owner_user_id, requester_user_id, '
                ' approver_user_id, run_id, manifest_json, manifest_hash, '
                ' policy_revision, status, expires_at, limits_json, created_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                (approval_id, tenant_id, owner_user_id, requester_user_id, '',
                 run_id, json.dumps(manifest, ensure_ascii=False, default=str),
                 manifest_hash, int(policy_revision),
                 approvals_module.STATUS_PENDING, float(expires_at),
                 json.dumps(limits or {}, ensure_ascii=False), now()))
            await conn.commit()
            return dict(row)

    async def approval(self, approval_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM approvals WHERE approval_id=%s', (approval_id,))
            return dict(row) if row else None

    async def approve_approval(self, approval_id: str, *, approver_user_id: str,
                               expected_manifest_hash: str = '') -> dict:
        """批准。**内容被改过就拒绝**（manifest_hash 不匹配）。"""
        from webpage_agent.governance import approvals as approvals_module
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM approvals WHERE approval_id=%s FOR UPDATE',
                (approval_id,))
            if row is None:
                raise approvals_module.ApprovalMissing('批准不存在')
            if expected_manifest_hash and \
                    str(row['manifest_hash']) != expected_manifest_hash:
                raise approvals_module.ApprovalConflict('批准内容已变更，需要重新请求')
            status = str(row['status'])
            if status == approvals_module.STATUS_APPROVED:
                updated = row                 # 幂等：重复批准不重复记账
            elif status != approvals_module.STATUS_PENDING:
                raise approvals_module.ApprovalConflict(
                    f'批准当前状态为 {status}，不能批准')
            else:
                updated = await self._one(
                    conn,
                    'UPDATE approvals SET status=%s, approver_user_id=%s, approved_at=%s '
                    'WHERE approval_id=%s RETURNING *',
                    (approvals_module.STATUS_APPROVED, approver_user_id, stamp,
                     approval_id))
            resumed = False
            updated = dict(updated or row)
            run_id = str(updated.get('run_id') or '')
            if run_id:
                run_row = await self._one(
                    conn, 'SELECT status FROM runs WHERE run_id=%s FOR UPDATE',
                    (run_id,))
                if run_row and str(run_row['status']) == 'waiting_approval':
                    await self._run(
                        conn,
                        "UPDATE runs SET status='queued', lease_owner=NULL, "
                        'lease_expires_at=NULL, error=%s, waiting_external=0, '
                        'updated_at=%s WHERE run_id=%s AND status=%s',
                        ('', stamp, run_id, 'waiting_approval'))
                    resumed = True
            await conn.commit()
            updated['resumed'] = resumed
            return updated

    async def revoke_approval(self, approval_id: str, *, approver_user_id: str = '',
                              reject: bool = False) -> dict:
        from webpage_agent.governance import approvals as approvals_module
        stamp = now()
        status = (approvals_module.STATUS_REJECTED if reject
                  else approvals_module.STATUS_REVOKED)
        async with self._lease() as conn:
            row = await self._one(
                conn,
                'UPDATE approvals SET status=%s, approver_user_id=COALESCE(NULLIF(%s, %s), '
                ' approver_user_id), revoked_at=%s WHERE approval_id=%s RETURNING *',
                (status, approver_user_id, '', stamp, approval_id))
            if reject and row and row.get('run_id'):
                await self._run(
                    conn,
                    "UPDATE runs SET status='failed', error=%s, lease_owner=NULL, "
                    'lease_expires_at=NULL, updated_at=%s '
                    "WHERE run_id=%s AND status='waiting_approval'",
                    ('批准被拒绝', stamp, row['run_id']))
            await conn.commit()
            return dict(row) if row else {}

    async def approvals_for(self, *, tenant_id: str, status: str = '',
                            owner_user_id: str = '') -> list[dict]:
        sql = 'SELECT * FROM approvals WHERE tenant_id=%s'
        params: list = [tenant_id]
        if status:
            sql += ' AND status=%s'
            params.append(status)
        if owner_user_id:
            sql += ' AND owner_user_id=%s'
            params.append(owner_user_id)
        sql += ' ORDER BY created_at DESC'
        async with self._lease() as conn:
            rows = await self._all(conn, sql, tuple(params))
            return [dict(row) for row in rows]

    async def consume_approval_reservation(self, *, approval_id: str, action_id: str,
                                           manifest_item_key: str = '') -> bool:
        """确认消费：预留 → consumed。重复调用幂等。"""
        from webpage_agent.governance import approvals as approvals_module
        async with self._lease() as conn:
            cur = await self._run(
                conn,
                'UPDATE approval_reservations SET status=%s '
                'WHERE approval_id=%s AND action_id=%s AND manifest_item_key=%s '
                "AND status <> 'consumed'",
                (approvals_module.CONSUMED, approval_id, action_id,
                 manifest_item_key))
            await conn.commit()
            return bool(cur.rowcount)

    async def release_approval_reservation(self, *, approval_id: str, action_id: str,
                                            manifest_item_key: str = '') -> bool:
        """释放预留（**只允许在确认未执行时**调用）。

        unknown 的动作**不允许**释放：把可能已经花掉的额度放给第二个动作，
        等于让同一笔钱花两次。调用方必须自己判断确定性。
        """
        from webpage_agent.governance import approvals as approvals_module
        async with self._lease() as conn:
            cur = await self._run(
                conn,
                'UPDATE approval_reservations SET status=%s, released_at=%s '
                'WHERE approval_id=%s AND action_id=%s AND manifest_item_key=%s '
                "AND status = 'reserved'",
                (approvals_module.RELEASED, now(), approval_id, action_id,
                 manifest_item_key))
            await conn.commit()
            return bool(cur.rowcount)

    async def approval_reservations(self, approval_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                'SELECT * FROM approval_reservations WHERE approval_id=%s '
                'ORDER BY created_at', (approval_id,))
            return [dict(row) for row in rows]
