"""机器人广场（Bot 广场）的仓储方法：条目与安装登记。

对应 `docs/bot-plaza.md`。这个模块只做"行怎么读写"，**不做**权限判断、
不复制机器人身份、不发网络请求——那些在 `api/plaza_api.py` 与
`api/console_api.py`。

三条不变量（都靠数据库）：

1. **条目是快照，不是指针**。`bot_plaza_items` 存发布那一刻的人设与能力，
   `source_bot_id` 只是"它从哪来"的可追溯信息，**不参与渲染**。投稿人之后改
   自己的机器人，广场上已被别人看到的版本不会跟着变——否则用户点「使用」拿到的
   东西和页面上写的不是一回事。
2. **一人一条目只装一次**。`UNIQUE (tenant_id, owner_user_id, item_id)`；
   `claim_install()` 先 `INSERT ... ON CONFLICT DO NOTHING` 再回读，所以并发点两次
   「使用」只会产生一个机器人，不会建出两份同名副本。
3. **安装次数只在首次登记时 +1**。`claim_install()` 返回"这次是不是新插入的"，
   计数由调用方据此决定加不加——重复点击不刷量，也不需要在应用层加锁。

状态机（`status`）：

```
draft ──提交──▶ pending ──通过──▶ published ──下架──▶ offline
                   │                   ▲                 │
                   └──驳回──▶ rejected ─┴──重新提交───────┘
```

`published` 是唯一会被用户看到的取值；`rejected` 保留驳回理由（`review_note`），
让投稿人知道改哪；`offline` 与 `published` 之间可以反复切换，历史条目不删。
"""
from __future__ import annotations

import json

from webpage_agent.bot.plaza import SCOPES, STATUSES, VISIBLE_STATUS
from webpage_agent.store.pg_store import now

# 条目里可写的字段。item_id / created_at / 计数类（install_count / view_count）
# 不在白名单：计数只能由 bump_* 改，否则"谁装了"与"装了几次"会各说各话。
PLAZA_FIELDS = ('slug', 'source_bot_id', 'publisher_user_id', 'publisher_tenant',
                'name', 'handle', 'title', 'description', 'soul', 'model',
                'skill_ids', 'connector_ids', 'icon', 'color', 'shape', 'image',
                'category', 'tags', 'readme', 'starters', 'scope', 'status',
                'featured', 'position', 'review_note', 'reviewed_by',
                'reviewed_at', 'version')
JSON_COLUMNS = {'skill_ids': 'skill_ids_json', 'connector_ids': 'connector_ids_json',
                'tags': 'tags_json', 'starters': 'starters_json'}
INT_COLUMNS = ('featured', 'position', 'version')

# 用户可见的状态。别的地方（接口、控制台）引用 `bot/plaza.py` 里的同一份常量，
# 避免各写一份字符串。这里重新导出，是为了让仓储层的调用方不必多导一个模块。
__all__ = ['PgPlaza', 'decode_item', 'decode_install', 'PLAZA_FIELDS',
           'VISIBLE_STATUS', 'STATUSES', 'SCOPES']


def _json_list(value) -> str:
    return json.dumps(list(value or ()), ensure_ascii=False)


def decode_item(row) -> dict | None:
    """把条目行解成接口可直接用的 dict（json 列反序列化，计数转 int）。"""
    if row is None:
        return None
    item = dict(row)
    for key, column in JSON_COLUMNS.items():
        raw = item.pop(column, None)
        if raw is None:
            item[key] = []
            continue
        if isinstance(raw, (list, tuple)):
            item[key] = list(raw)
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = []
        item[key] = parsed if isinstance(parsed, list) else []
    item['featured'] = bool(item.get('featured'))
    for key in ('position', 'version', 'install_count', 'view_count'):
        item[key] = int(item.get(key) or 0)
    return item


def decode_install(row) -> dict | None:
    return dict(row) if row is not None else None


class PgPlaza:
    """广场条目与安装登记的 PostgreSQL 实现。"""

    # ---------- 条目 ----------

    async def list_plaza_items(self, *, statuses: tuple[str, ...] = (VISIBLE_STATUS,),
                               viewer_tenant: str = '', category: str = '',
                               q: str = '', publisher_tenant: str | None = None,
                               publisher_user_id: str | None = None,
                               source_bot_id: str | None = None,
                               order: str = 'featured',
                               limit: int = 100,
                               offset: int = 0) -> list[dict]:
        """列条目。

        `viewer_tenant` 非空时**只**返回"公开的 + 本租户私有上架的"——广场是产品
        概念，不该让 A 企业误以为 B 企业内部的机器人也能用。控制台传空串看全部。
        """
        clauses = []
        params: list = []
        if statuses:
            clauses.append('status = ANY(%s)')
            params.append(list(statuses))
        if viewer_tenant:
            clauses.append("(scope = 'public' OR publisher_tenant = %s)")
            params.append(viewer_tenant)
        if publisher_tenant is not None:
            clauses.append('publisher_tenant = %s')
            params.append(publisher_tenant)
        if publisher_user_id is not None:
            clauses.append('publisher_user_id = %s')
            params.append(publisher_user_id)
        if source_bot_id is not None:
            clauses.append('source_bot_id = %s')
            params.append(source_bot_id)
        if category:
            clauses.append('category = %s')
            params.append(category)
        if q:
            like = f'%{q}%'
            clauses.append('(name ILIKE %s OR handle ILIKE %s OR title ILIKE %s '
                           'OR description ILIKE %s OR tags_json ILIKE %s)')
            params.extend([like] * 5)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        if order == 'new':
            ranking = 'created_at DESC'
        elif order == 'popular':
            ranking = 'install_count DESC, created_at DESC'
        else:
            ranking = 'featured DESC, position ASC, install_count DESC, created_at DESC'
        params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
        async with self._lease() as conn:
            rows = await self._all(
                conn,
                f'SELECT * FROM bot_plaza_items{where} '
                f'ORDER BY {ranking} LIMIT %s OFFSET %s', tuple(params))
        return [decode_item(row) or {} for row in rows]

    async def plaza_item(self, item_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(conn, 'SELECT * FROM bot_plaza_items WHERE item_id=%s',
                                  (item_id,))
        return decode_item(row)

    async def plaza_item_by_handle(self, slug: str) -> dict | None:
        """按 slug 精确查（投稿时判重）。模糊查会把"是别人 slug 的前缀"也判成占用。"""
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT * FROM bot_plaza_items WHERE slug=%s', (slug,))
        return decode_item(row)

    async def plaza_item_by_source(self, *, source_bot_id: str,
                                   publisher_tenant: str,
                                   publisher_user_id: str) -> dict | None:
        """同一个机器人**同一个投稿人**只维护一条条目：再次投稿是更新而不是再发一份。"""
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT * FROM bot_plaza_items WHERE source_bot_id=%s '
                      'AND publisher_tenant=%s AND publisher_user_id=%s '
                      'ORDER BY created_at DESC LIMIT 1',
                (source_bot_id, publisher_tenant, publisher_user_id))
        return decode_item(row)

    async def create_plaza_item(self, *, item_id: str, created_at: str,
                                updated_at: str, **fields) -> dict:
        columns = ['item_id', 'created_at', 'updated_at']
        values: list = [item_id, created_at, updated_at]
        for key in PLAZA_FIELDS:
            column = JSON_COLUMNS.get(key, key)
            if key in JSON_COLUMNS:
                values.append(_json_list(fields.get(key)))
            elif key in INT_COLUMNS:
                # featured 也是 0/1 整数列，跟着这里走（布尔 True → 1）。
                values.append(int(fields.get(key) or 0))
            else:
                values.append(str(fields.get(key) or ''))
            columns.append(column)
        placeholders = ', '.join(['%s'] * len(columns))
        async with self._lease() as conn:
            row = await self._one(
                conn, f'INSERT INTO bot_plaza_items ({", ".join(columns)}) '
                      f'VALUES ({placeholders}) RETURNING *', tuple(values))
        return decode_item(row) or {}

    async def update_plaza_item(self, item_id: str, **fields) -> dict | None:
        sets: list[str] = []
        values: list = []
        for key in PLAZA_FIELDS:
            if key not in fields:
                continue
            column = JSON_COLUMNS.get(key, key)
            value = fields[key]
            if key in JSON_COLUMNS:
                values.append(_json_list(value))
            elif key in INT_COLUMNS:
                values.append(int(value or 0))
            else:
                values.append(str(value or ''))
            sets.append(f'{column}=%s')
        if not sets:
            return await self.plaza_item(item_id)
        sets.append('updated_at=%s')
        values.extend([now(), item_id])
        async with self._lease() as conn:
            row = await self._one(
                conn, f'UPDATE bot_plaza_items SET {", ".join(sets)} '
                      'WHERE item_id=%s RETURNING *', tuple(values))
        return decode_item(row)

    async def delete_plaza_item(self, item_id: str) -> bool:
        """硬删条目。装过它的人**不受影响**：副本早就是他们自己的机器人了。"""
        async with self._lease() as conn:
            cur = await self._run(
                conn, 'DELETE FROM bot_plaza_items WHERE item_id=%s', (item_id,))
            return bool(getattr(cur, 'rowcount', 0))

    async def bump_plaza_counter(self, item_id: str, column: str, delta: int = 1) -> int:
        """`install_count` / `view_count` 自增。列名走白名单，不拼用户输入。"""
        if column not in ('install_count', 'view_count'):
            raise ValueError('只能自增 install_count / view_count')
        async with self._lease() as conn:
            row = await self._one(
                conn, f'UPDATE bot_plaza_items SET {column} = {column} + %s '
                      'WHERE item_id=%s RETURNING ' + column, (int(delta), item_id))
        if row is None:
            return 0
        return int((row['value'] if isinstance(row, dict) else row[0]) or 0)

    # ---------- 安装登记 ----------

    async def claim_plaza_install(self, *, install_id: str, item_id: str,
                                  tenant_id: str, owner_user_id: str,
                                  item_version: int = 1) -> bool:
        """登记"这个人装了这条目"。**返回是否是这次插入的**（计数只用它决定）。"""
        stamp = now()
        async with self._lease() as conn:
            row = await self._one(
                conn, 'INSERT INTO bot_plaza_installs '
                      '(install_id, item_id, bot_id, tenant_id, owner_user_id, '
                      ' item_version, created_at, updated_at) '
                      'VALUES (%s,%s,%s,%s,%s,%s,%s,%s) '
                      'ON CONFLICT (tenant_id, owner_user_id, item_id) DO NOTHING '
                      'RETURNING install_id',
                (install_id, item_id, '', tenant_id, owner_user_id,
                 int(item_version or 1), stamp, stamp))
        return row is not None

    async def bind_plaza_install_bot(self, install_id: str, *, bot_id: str,
                                     item_version: int = 1) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(
                conn, 'UPDATE bot_plaza_installs SET bot_id=%s, item_version=%s, '
                      'updated_at=%s WHERE install_id=%s RETURNING *',
                (bot_id, int(item_version or 1), now(), install_id))
        return decode_install(row)

    async def plaza_install_for(self, *, item_id: str, tenant_id: str,
                                owner_user_id: str) -> dict | None:
        async with self._lease() as conn:
            row = await self._one(
                conn, 'SELECT * FROM bot_plaza_installs WHERE item_id=%s '
                      'AND tenant_id=%s AND owner_user_id=%s',
                (item_id, tenant_id, owner_user_id))
        return decode_install(row)

    async def list_plaza_installs(self, *, tenant_id: str,
                                  owner_user_id: str) -> list[dict]:
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT * FROM bot_plaza_installs WHERE tenant_id=%s '
                      'AND owner_user_id=%s ORDER BY created_at DESC',
                (tenant_id, owner_user_id))
        return [decode_install(row) or {} for row in rows]

    async def drop_plaza_install(self, *, item_id: str, tenant_id: str,
                                 owner_user_id: str) -> bool:
        async with self._lease() as conn:
            cur = await self._run(
                conn, 'DELETE FROM bot_plaza_installs WHERE item_id=%s '
                      'AND tenant_id=%s AND owner_user_id=%s',
                (item_id, tenant_id, owner_user_id))
            return bool(getattr(cur, 'rowcount', 0))

    async def plaza_install_counts(self, item_ids: list[str]) -> dict[str, int]:
        """按条目聚合实际安装行数。控制台用它核对 `install_count` 有没有跑偏。"""
        if not item_ids:
            return {}
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT item_id, COUNT(*) AS n FROM bot_plaza_installs '
                      'WHERE item_id = ANY(%s) GROUP BY item_id', (list(item_ids),))
        out: dict[str, int] = {}
        for row in rows:
            item = dict(row)
            out[str(item.get('item_id'))] = int(item.get('n') or 0)
        return out
