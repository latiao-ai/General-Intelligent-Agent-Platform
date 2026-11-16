"""积分钱包：余额、进账流水、消耗聚合。

## 为什么消耗不单独记一张表

计费链路本来就把每次模型调用的实付金额写进了 `provider_charges`
（`charged_micros`，单位百万分之一元）。再在积分侧抄一份"扣了多少"，
就有两份账要维护：哪次结算失败、哪次重试多写一条，余额立刻对不上，
而对不上的时候没人能判断哪一份是错的。

所以这里只落**进账**（`credit_grants`：购买、赠送、人工调整），消耗一律
从 `provider_charges` 现算：

    余额 = 进账合计 − 已用合计

按人的归集链是 `provider_charges.run_id → runs.task_id → tasks.session_id →
sessions.owner_user_id`。链上任何一环缺失（存量库里确实有）就归不到人，
那笔消耗会从"这个人的使用情况"里消失——这是有意的取舍：宁可把余额算高，
也不要把别人的花费算到某个人头上。

## 单位

对外的"积分"是展示单位，1 积分 = ¥0.01 = 10 000 micros（与购买套餐
1000 积分 / ¥10 对齐）。库里一律存 micros 整数，避免浮点累计误差。

## 时间

`credit_grants.created_at` 存 ISO 文本（与库内其它 `created_at` 同口径）；
`provider_charges.created_at` 存 Unix 秒，按天聚合时用
`to_timestamp(...) AT TIME ZONE 'UTC'` 换算，不要直接和 `now()` 比。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from webpage_agent.store.pg_store import now

# 1 积分 = ¥0.01。改这个数会让所有历史余额的展示口径一起变，
# 所以它不是配置项，是数据字典的一部分。
MICROS_PER_POINT = 10_000

# 消耗归集：一次 join 拿到人，下面所有查询共用。
_USAGE_FROM = (
    'FROM provider_charges p '
    'JOIN runs r ON r.run_id = p.run_id '
    'JOIN tasks t ON t.task_id = r.task_id '
    'JOIN sessions s ON s.session_id = t.session_id '
)
_USAGE_DAY = "to_char(to_timestamp(p.created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD')"


def points(micros) -> float:
    """micros → 积分（两位小数）。前端直接显示，不做二次四舍五入。"""
    return round(int(micros or 0) / MICROS_PER_POINT, 2)


def _day_from(days: int) -> str:
    """窗口起点（UTC 日期文本），与 `_USAGE_DAY` 同格式。"""
    back = max(1, int(days)) - 1
    return (datetime.now(timezone.utc) - timedelta(days=back)).strftime('%Y-%m-%d')


class PgCredits:
    """积分方法组。见模块说明。"""

    async def _granted_for(self, conn, tenant_id: str, user_ids: list[str]) -> dict:
        """一批用户的进账合计与最近一次进账时间。"""
        rows = await self._all(
            conn,
            'SELECT user_id, COALESCE(sum(amount_micros), 0) AS amount_micros, '
            '       max(created_at) AS last_grant_at '
            'FROM credit_grants WHERE tenant_id=%s AND user_id = ANY(%s) GROUP BY 1',
            (tenant_id, list(user_ids)))
        return {row['user_id']: row for row in rows}

    async def _used_for(self, conn, tenant_id: str, user_ids: list[str],
                        day_from: str) -> dict:
        """一批用户在窗口内的消耗合计、调用次数、token。"""
        rows = await self._all(
            conn,
            'SELECT s.owner_user_id AS user_id, '
            '       COALESCE(sum(p.charged_micros), 0) AS used_micros, '
            '       count(*) AS calls, '
            '       COALESCE(sum(p.input_tokens), 0) AS input_tokens, '
            '       COALESCE(sum(p.output_tokens), 0) AS output_tokens, '
            '       max(p.created_at) AS last_used_ts ' + _USAGE_FROM +
            ' WHERE s.tenant_id=%s AND s.owner_user_id = ANY(%s) AND ' + _USAGE_DAY + ' >= %s '
            'GROUP BY 1',
            (tenant_id, list(user_ids), day_from))
        return {row['user_id']: row for row in rows}

    async def _daily_for(self, conn, tenant_id: str, user_ids: list[str],
                         day_from: str) -> dict:
        """按人按天拆开的消耗，给管理后台画趋势。"""
        rows = await self._all(
            conn,
            'SELECT s.owner_user_id AS user_id, ' + _USAGE_DAY + ' AS day, '
            '       COALESCE(sum(p.charged_micros), 0) AS used_micros, '
            '       count(*) AS calls ' + _USAGE_FROM +
            ' WHERE s.tenant_id=%s AND s.owner_user_id = ANY(%s) AND ' + _USAGE_DAY + ' >= %s '
            'GROUP BY 1, 2 ORDER BY 2',
            (tenant_id, list(user_ids), day_from))
        out: dict[str, list[dict]] = {}
        for row in rows:
            out.setdefault(row['user_id'], []).append({
                'day': row['day'],
                'points': points(row['used_micros']),
                'calls': int(row['calls'] or 0),
            })
        return out

    async def credit_balances(self, tenant_id: str, user_ids: list[str]) -> dict:
        """{user_id: {balance, granted, used, ...}}。`used` 是**全部历史**消耗。"""
        ids = [str(uid) for uid in user_ids if uid]
        if not ids:
            return {}
        async with self._lease() as conn:
            granted = await self._granted_for(conn, tenant_id, ids)
            used = await self._used_for(conn, tenant_id, ids, '0000-00-00')
        out = {}
        for uid in ids:
            g = int((granted.get(uid) or {}).get('amount_micros') or 0)
            u = int((used.get(uid) or {}).get('used_micros') or 0)
            out[uid] = {
                'user_id': uid,
                'balance_micros': g - u,
                'balance': points(g - u),
                'granted': points(g),
                'used': points(u),
                'last_grant_at': str((granted.get(uid) or {}).get('last_grant_at') or ''),
            }
        return out

    async def credit_balance(self, tenant_id: str, user_id: str) -> dict:
        detail = (await self.credit_balances(tenant_id, [user_id])).get(user_id)
        return detail or {'user_id': user_id, 'balance_micros': 0, 'balance': 0.0,
                          'granted': 0.0, 'used': 0.0, 'last_grant_at': ''}

    async def credit_events(self, tenant_id: str, user_id: str,
                            limit: int = 20) -> list[dict]:
        """这个人的变动时间线：进账逐条，消耗按天并成一条。

        逐条列消耗没有意义——一次任务会打出几十条 charge——所以按天合并，
        但保留当天的调用次数，够回答"这笔是干什么用的"。
        """
        limit = max(1, min(200, int(limit)))
        async with self._lease() as conn:
            grants = await self._all(
                conn,
                'SELECT grant_id, amount_micros, kind, note, operator_id, created_at '
                'FROM credit_grants WHERE tenant_id=%s AND user_id=%s '
                'ORDER BY created_at DESC LIMIT %s', (tenant_id, user_id, limit))
            usage = await self._all(
                conn,
                'SELECT ' + _USAGE_DAY + " AS day, COALESCE(sum(p.charged_micros), 0) AS used_micros, "
                '       count(*) AS calls ' + _USAGE_FROM +
                ' WHERE s.tenant_id=%s AND s.owner_user_id=%s '
                'GROUP BY 1 ORDER BY 1 DESC LIMIT %s', (tenant_id, user_id, limit))
        events = [{
            'at': str(row['created_at'] or ''),
            'kind': str(row['kind'] or 'grant'),
            'points': points(row['amount_micros']),
            'note': str(row['note'] or ''),
            'operator_id': str(row['operator_id'] or ''),
            'ref': str(row['grant_id'] or ''),
        } for row in grants]
        events.extend({
            'at': str(row['day'] or '') + 'T23:59:59+00:00',
            'kind': 'consume',
            'points': -points(row['used_micros']),
            'note': f"当日调用 {int(row['calls'] or 0)} 次",
            'operator_id': '',
            'ref': str(row['day'] or ''),
        } for row in usage if int(row['used_micros'] or 0))
        events.sort(key=lambda item: item['at'], reverse=True)
        return events[:limit]

    async def credit_overview(self, tenant_id: str, days: int = 7) -> dict:
        """管理后台用：每个用户一行，含余额、窗口内消耗、每日趋势、最近变动。"""
        days = max(1, min(90, int(days)))
        start = _day_from(days)
        async with self._lease() as conn:
            users = await self._all(
                conn,
                'SELECT u.user_id, u.display_name, u.email, u.status FROM app_users u '
                'WHERE EXISTS (SELECT 1 FROM identities i '
                'WHERE i.user_id=u.user_id AND i.tenant_id=%s) '
                'ORDER BY u.user_id', (tenant_id,))
            ids = [row['user_id'] for row in users]
            if not ids:
                return {'days': days, 'total': 0, 'users': []}
            granted = await self._granted_for(conn, tenant_id, ids)
            used_all = await self._used_for(conn, tenant_id, ids, '0000-00-00')
            used_win = await self._used_for(conn, tenant_id, ids, start)
            daily = await self._daily_for(conn, tenant_id, ids, start)

        rows = []
        for user in users:
            uid = user['user_id']
            g = int((granted.get(uid) or {}).get('amount_micros') or 0)
            ua = int((used_all.get(uid) or {}).get('used_micros') or 0)
            win = used_win.get(uid) or {}
            # 「最近变动」取进账与消耗里更晚的那个，用户问的就是这个时间点。
            last_grant = str((granted.get(uid) or {}).get('last_grant_at') or '')
            last_used_ts = win.get('last_used_ts')
            last_used = ('' if last_used_ts is None else
                         datetime.fromtimestamp(float(last_used_ts), timezone.utc).isoformat())
            rows.append({
                'user_id': uid,
                'display_name': str(user.get('display_name') or ''),
                'email': str(user.get('email') or ''),
                'status': str(user.get('status') or ''),
                'balance': points(g - ua),
                'granted': points(g),
                'used_total': points(ua),
                'used_window': points(win.get('used_micros')),
                'calls_window': int(win.get('calls') or 0),
                'tokens_window': int(win.get('input_tokens') or 0) + int(win.get('output_tokens') or 0),
                'last_change_at': max(last_grant, last_used),
                'last_grant_at': last_grant,
                'daily': daily.get(uid, []),
            })
        return {'days': days, 'total': len(rows), 'users': rows}

    async def credit_tenants(self) -> list[str]:
        """有身份登记的全部租户，供跨租户控制台按租户逐个聚合。"""
        async with self._lease() as conn:
            rows = await self._all(
                conn, 'SELECT DISTINCT tenant_id FROM identities ORDER BY tenant_id')
        return [str(row['tenant_id']) for row in rows if row['tenant_id']]

    async def add_credit_grant(self, *, tenant_id: str, user_id: str, points_delta: float,
                               kind: str = 'grant', note: str = '',
                               operator_id: str = '') -> dict:
        """人工充值/调整。`points_delta` 可为负；余额不允许被调成负数。

        用 advisory lock 把同一用户的并发调整串起来：两个人同时给同一个人
        加积分时，第二个要看到第一个的结果再判断会不会变负。
        """
        amount = int(round(float(points_delta) * MICROS_PER_POINT))
        if amount == 0:
            raise ValueError('调整数量不能为 0')
        async with self._lease() as conn:
            await self._run(conn, 'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                            (f'credit:{tenant_id}:{user_id}',))
            current = await self._granted_for(conn, tenant_id, [user_id])
            used = await self._used_for(conn, tenant_id, [user_id], '0000-00-00')
            granted = int((current.get(user_id) or {}).get('amount_micros') or 0)
            spent = int((used.get(user_id) or {}).get('used_micros') or 0)
            if granted + amount - spent < 0:
                raise ValueError('调整后余额会变成负数')
            grant_id = uuid.uuid4().hex
            await self._run(
                conn,
                'INSERT INTO credit_grants (grant_id,tenant_id,user_id,amount_micros,kind,'
                'note,operator_id,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (grant_id, tenant_id, user_id, amount, kind, note, operator_id, now()))
        return {'grant_id': grant_id, 'balance': points(granted + amount - spent),
                'delta': points(amount)}

    async def create_credit_order(self, *, tenant_id: str, user_id: str, package_id: str,
                                  points: int, price_cents: int) -> dict:
        """落一张待支付订单。**不加积分**：支付通道确认后才该到账。"""
        order_id = uuid.uuid4().hex
        async with self._lease() as conn:
            await self._run(
                conn,
                'INSERT INTO credit_orders (order_id,tenant_id,user_id,package_id,points,'
                "price_cents,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,'pending',%s)",
                (order_id, tenant_id, user_id, package_id, int(points),
                 int(price_cents), now()))
        return {'order_id': order_id, 'status': 'pending'}
