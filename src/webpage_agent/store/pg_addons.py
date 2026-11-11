"""专家与会话级选择的仓储方法（`experts` 表 + `sessions.addon_selection`）。

对应 WorkBuddy 的 `experts/` 目录与 `sessions.addon_selection` 列：

- **专家落库**：自定义专家要能被不同前端实例读到、能被归属检查保护，所以进库；
  内置专家是代码里的只读目录（`addons/catalog.py`），不落库——升级不会撞行。
- **会话选择落库**：WorkBuddy 把它存在会话行上（不是消息里），因为"选了什么"是
  会话配置，影响之后的每一轮。同理，本项目也写在 `sessions.addon_selection`。

刻意**不做**的两件事：

- 不在这里校验技能/连接器是否真的存在：解析层负责"点了但已失效"的降级，
  写选择时不该因为一个技能被卸载就拒绝整次保存（那会让用户连取消都点不了）。
- 不合并 `session_turns.connector_ids_json`：那条是"这条消息带了什么"的审计记录，
  语义不同，合并会让历史 turn 的真相被改写。
"""
from __future__ import annotations

import json

from webpage_agent.store.pg_store import now

# 专家行里用户可改的字段。expert_id / tenant_id / owner_user_id / created_at 不在
# 白名单里：让接口改归属等于越权搬家。
EXPERT_FIELDS = ('name', 'description', 'system_prompt', 'skill_ids', 'connector_ids',
                 'model', 'icon', 'color', 'tag')
# 库里存 JSON 文本的列（项目全库统一用 TEXT + json.dumps，不用 jsonb/array：
# 读出来就是 str，与其它模块的解码方式一致）。
JSON_COLUMNS = {'skill_ids': 'skill_ids_json', 'connector_ids': 'connector_ids_json'}


def _decode_expert(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


class PgAddons:
    """专家 CRUD 与会话 addon_selection 读写。"""

    # ---------- 会话选择 ----------

    async def session_addons(self, session_id: str) -> dict:
        """读会话的 addon_selection；没有或坏了都当 `{}`（= 没选任何东西）。"""
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT addon_selection FROM sessions WHERE session_id=%s',
                (session_id,))
        if not row:
            return {}
        raw = row['addon_selection']
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw or '{}')
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def set_session_addons(self, session_id: str, selection: dict) -> dict | None:
        """整体覆盖 addon_selection（合并语义在 `addons.resolver.merge_selection`）。

        返回写入后的选择；会话不存在返回 None（调用方转 404）。
        """
        payload = json.dumps(selection if isinstance(selection, dict) else {},
                             ensure_ascii=False)
        async with self._lease() as conn:
            row = await self._one(conn,
                'UPDATE sessions SET addon_selection=%s, updated_at=%s '
                'WHERE session_id=%s RETURNING addon_selection',
                (payload, now(), session_id))
            await conn.commit()
        if not row:
            return None
        try:
            data = json.loads(row['addon_selection'] or '{}')
        except (TypeError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    # ---------- 专家 ----------

    async def list_expert_rows(self, *, tenant_id: str, owner_user_id: str,
                               limit: int = 200) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(conn,
                'SELECT * FROM experts WHERE tenant_id=%s AND owner_user_id=%s '
                'ORDER BY created_at DESC LIMIT %s',
                (tenant_id, owner_user_id, max(1, min(500, int(limit)))))
        return [_decode_expert(row) or {} for row in rows]

    async def expert_row(self, expert_id: str, *, tenant_id: str,
                         owner_user_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT * FROM experts WHERE expert_id=%s AND tenant_id=%s '
                'AND owner_user_id=%s',
                (expert_id, tenant_id, owner_user_id))
        return _decode_expert(row)

    async def create_expert_row(self, *, expert_id: str, tenant_id: str,
                                owner_user_id: str, created_at: str,
                                updated_at: str, **fields) -> dict:
        columns = ['expert_id', 'tenant_id', 'owner_user_id', 'created_at', 'updated_at']
        values: list = [expert_id, tenant_id, owner_user_id, created_at, updated_at]
        for key in EXPERT_FIELDS:
            column = JSON_COLUMNS.get(key)
            if column is None:
                column = key
                values.append(str(fields.get(key) or ''))
            else:
                values.append(json.dumps(list(fields.get(key) or ()), ensure_ascii=False))
            columns.append(column)
        placeholders = ', '.join(['%s'] * len(columns))
        async with self._lease() as conn:
            row = await self._one(conn,
                f'INSERT INTO experts ({", ".join(columns)}) '
                f'VALUES ({placeholders}) RETURNING *', tuple(values))
            await conn.commit()
        return _decode_expert(row) or {}

    async def update_expert_row(self, expert_id: str, *, tenant_id: str,
                                owner_user_id: str, **fields) -> dict | None:
        sets, values = [], []
        for key in EXPERT_FIELDS:
            if key not in fields:
                continue
            column = JSON_COLUMNS.get(key, key)
            if key in JSON_COLUMNS:
                values.append(json.dumps(list(fields.get(key) or ()), ensure_ascii=False))
            else:
                values.append(str(fields.get(key) or ''))
            sets.append(f'{column}=%s')
        if 'updated_at' in fields:
            sets.append('updated_at=%s')
            values.append(str(fields['updated_at']))
        if not sets:
            return await self.expert_row(expert_id, tenant_id=tenant_id,
                                         owner_user_id=owner_user_id)
        values += [expert_id, tenant_id, owner_user_id]
        async with self._lease() as conn:
            row = await self._one(conn,
                f'UPDATE experts SET {", ".join(sets)} WHERE expert_id=%s '
                'AND tenant_id=%s AND owner_user_id=%s RETURNING *', tuple(values))
            await conn.commit()
        return _decode_expert(row)

    async def delete_expert_row(self, expert_id: str, *, tenant_id: str,
                                owner_user_id: str) -> bool:
        async with self._lease() as conn:
            row = await self._one(conn,
                'DELETE FROM experts WHERE expert_id=%s AND tenant_id=%s '
                'AND owner_user_id=%s RETURNING expert_id',
                (expert_id, tenant_id, owner_user_id))
            await conn.commit()
        return bool(row)
