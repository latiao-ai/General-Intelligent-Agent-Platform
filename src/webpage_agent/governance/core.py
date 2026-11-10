"""Shared rate limits and conservative tenant billing, backed by PostgreSQL."""
import hashlib
import math
import time
import uuid
from abc import ABC, abstractmethod
from contextvars import ContextVar
from contextlib import AbstractAsyncContextManager, asynccontextmanager
import logging
from datetime import datetime, timezone

from webpage_agent.core.config import settings
from webpage_agent.core.errors import BudgetExceeded

logger = logging.getLogger(__name__)
# bind() 写入 (store, tenant_id, run_id)；没绑定时是 None。
# 不标类型的话，default=None 会把变量收成 ContextVar[None]，set 元组就对不上。
_scope: ContextVar['tuple[GovernanceStore, str, str] | None'] = ContextVar(
    'provider_scope', default=None)


class QuotaExceeded(RuntimeError):
    safe_message = '租户本日调用或费用额度已用完，请联系管理员。'

    def __init__(self):
        super().__init__(self.safe_message)


def bind(store, tenant_id, run_id=''):
    return _scope.set((store, tenant_id or 'default', run_id))


def unbind(token):
    _scope.reset(token)


def _measured_tokens(usage):
    """只有明确的非负整数输入/输出才算实测。缺字段不能当成 0，否则失败请求会显示成没花钱。"""
    if not isinstance(usage, dict):
        return None
    incoming, outgoing = usage.get('input_tokens'), usage.get('output_tokens')
    if type(incoming) is int and type(outgoing) is int and incoming >= 0 and outgoing >= 0:
        return incoming, outgoing
    return None


def _reservation_micros(provider: str) -> int:
    if provider == 'llm':
        return (settings.llm_context_window * settings.llm_input_price_micros_per_million
                + settings.llm_output_tokens * settings.llm_output_price_micros_per_million
                + 999_999) // 1_000_000
    return settings.search_request_cost_micros


def _active_model(model: str | None) -> str:
    from webpage_agent.core.model_choice import active
    return model or active(getattr(settings, 'llm_model', '') or '')


async def reserve(*, provider='llm', model='', purpose=''):
    scope = _scope.get()
    # 日额度关掉时，没绑运行的直接调用仍不落账，避免单测和本地试跑写进共享库。
    # 绑了运行就要记账：token 闸和按次查询只读 provider_charges，额度开关不该把用量一起关掉。
    if scope is None:
        if settings.tenant_budget_enabled:
            raise RuntimeError('Provider request has no trusted tenant billing scope')
        return None
    store, tenant, run_id = scope
    charge_id = uuid.uuid4().hex
    from webpage_agent.observe.telemetry import trace_id, span_id
    await store.reserve_provider_charge(
        charge_id, tenant_id=tenant, run_id=run_id, provider=provider,
        model=_active_model(model), cost_micros=_reservation_micros(provider),
        max_requests=settings.tenant_daily_provider_requests,
        max_cost_micros=settings.tenant_daily_cost_micros,
        trace_id=trace_id.get(), purpose=purpose or '',
        enforce_quota=settings.tenant_budget_enabled)
    parent_span = span_id.get()
    token = span_id.set(uuid.uuid4().hex[:16])
    logger.info('provider_span_start charge=%s provider=%s model=%s purpose=%s parent_span=%s',
                charge_id, provider, _active_model(model), purpose or '', parent_span)
    return store, charge_id, time.monotonic(), token, provider


async def finish(reservation, *, usage=None, succeeded=False):
    if reservation is None:
        return
    store, charge_id, started, span_token, provider = reservation
    # 失败、取消、超时仍按预留金额保留。有实测 token 就记实测；拿不到才按配置上界估算，
    # 并用 tokens_estimated 分开。不标的话，最贵的失败在 SUM 里是 NULL，闸门完全看不见。
    # 搜索与抓取没有 token，不能拿模型窗口去填，否则对账会把检索算成一次满窗口调用。
    measured = _measured_tokens(usage)
    estimated = False
    tokens = measured
    if tokens is None and provider == 'llm':
        tokens = (settings.llm_context_window, settings.llm_output_tokens)
        estimated = True
    actual = None
    if succeeded and measured is not None:
        actual = (measured[0] * settings.llm_input_price_micros_per_million
                  + measured[1] * settings.llm_output_price_micros_per_million
                  + 999_999) // 1_000_000
    try:
        await store.settle_provider_charge(charge_id, actual_micros=actual,
            tokens=tokens, duration=time.monotonic() - started,
            status='succeeded' if succeeded else 'uncertain', estimated=estimated)
        logger.info('provider_span_end charge=%s succeeded=%s duration_seconds=%.6f',
                    charge_id, succeeded, time.monotonic() - started)
    finally:
        from webpage_agent.observe.telemetry import span_id
        span_id.reset(span_token)



@asynccontextmanager
async def metered(**kwargs):
    reservation = await reserve(**kwargs)
    success = False
    try:
        yield
        success = True
    finally:
        await finish(reservation, succeeded=success)


class GovernanceStore(ABC):
    """额度、限流和用量查询。

    借连接、取行、执行语句不写在这个类里：那是具体仓储的事，目前只有
    `PostgresStore`。下面四个方法是这些查询的依赖，声明在基类上，子类必须实现。
    这样方法体能通过类型检查，也不会再假装基类自己有一套连接实现。
    """

    @abstractmethod
    def _lease(self, *, bootstrap: bool = False) -> AbstractAsyncContextManager[object]:
        """借一条连接：正常结束提交，异常回滚。`bootstrap` 为真时跳过就绪检查。"""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    async def _one(conn, sql: str, params=()):
        """执行语句并取第一行；没有行时返回 None。"""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    async def _all(conn, sql: str, params=()):
        """执行语句并取全部行。"""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    async def _run(conn, sql: str, params=()):
        """执行语句。要 rowcount 时用返回的游标。"""
        raise NotImplementedError

    async def rate_limit(self, scope: str, limit: int, *, now_ts=None) -> tuple[bool, int]:
        current = time.time() if now_ts is None else now_ts
        window = int(current // 60) * 60
        # Store no IP addresses or user identifiers in the limiter table.
        key = hashlib.sha256(scope.encode()).hexdigest()
        async with self._lease() as conn:
            row = await self._one(conn,
                'INSERT INTO api_rate_windows VALUES (%s,%s,1) '
                'ON CONFLICT (scope,window_start) DO UPDATE '
                'SET requests=api_rate_windows.requests+1 '
                'WHERE api_rate_windows.requests < %s RETURNING requests',
                (key, window, limit))
        return row is not None, max(1, math.ceil(window + 60 - current))

    async def reserve_provider_charge(self, charge_id, *, tenant_id, run_id,
                                      provider, model, cost_micros, max_requests,
                                      max_cost_micros, trace_id='', purpose='',
                                      enforce_quota=True):
        if min(cost_micros, max_cost_micros) < 0 or max_requests < 1:
            raise ValueError('Invalid tenant billing limits')
        day = datetime.now(timezone.utc).date().isoformat()
        async with self._lease() as conn:
            if enforce_quota:
                # Conditional UPSERT serializes all requests for this tenant/day.
                # 日额度关掉时不写这张表：token 明细不该把已经明确不做的日上限又打开。
                row = await self._one(conn,
                    'INSERT INTO tenant_usage_days (tenant_id,day,requests,charged_micros) '
                    'SELECT %s,%s,1,%s WHERE %s <= %s '
                    'ON CONFLICT (tenant_id,day) DO UPDATE '
                    'SET requests=tenant_usage_days.requests+1, '
                    'charged_micros=tenant_usage_days.charged_micros+EXCLUDED.charged_micros '
                    'WHERE tenant_usage_days.requests < %s AND '
                    'tenant_usage_days.charged_micros+EXCLUDED.charged_micros <= %s '
                    'RETURNING requests',
                    (tenant_id, day, cost_micros, cost_micros, max_cost_micros,
                     max_requests, max_cost_micros))
                if row is None:
                    raise QuotaExceeded()
            await self._run(conn,
                'INSERT INTO provider_charges (charge_id,tenant_id,day,run_id,provider,model,'
                'reserved_micros,charged_micros,created_at,trace_id,purpose) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (charge_id, tenant_id, day, run_id, provider, model,
                 cost_micros, cost_micros, time.time(), trace_id, purpose or ''))

    async def settle_provider_charge(self, charge_id, *, actual_micros=None,
                                     tokens=None, duration=0, status='uncertain',
                                     estimated=False):
        async with self._lease() as conn:
            row = await self._one(conn,
                "SELECT * FROM provider_charges WHERE charge_id=%s FOR UPDATE", (charge_id,))
            if row is None or row['status'] != 'reserved':
                return
            charged = row['reserved_micros'] if actual_micros is None else max(0, actual_micros)
            await self._run(conn,
                'UPDATE tenant_usage_days SET charged_micros=charged_micros+%s '
                'WHERE tenant_id=%s AND day=%s',
                (charged-row['charged_micros'], row['tenant_id'], row['day']))
            await self._run(conn,
                'UPDATE provider_charges SET charged_micros=%s,input_tokens=%s,output_tokens=%s,'
                'duration_seconds=%s,status=%s,tokens_estimated=%s WHERE charge_id=%s',
                (charged, tokens[0] if tokens else None, tokens[1] if tokens else None,
                 duration, status, bool(estimated), charge_id))

    async def billing_stats(self):
        async with self._lease() as conn:
            row = await self._one(conn,
                'SELECT count(*) AS provider_requests, '
                'COALESCE(sum(charged_micros),0) AS provider_charged_micros, '
                'COALESCE(sum(input_tokens),0) AS provider_input_tokens, '
                'COALESCE(sum(output_tokens),0) AS provider_output_tokens, '
                'count(*) FILTER (WHERE status=\'reserved\') AS provider_unsettled_requests '
                'FROM provider_charges')
            return {key: int(value) for key, value in row.items()}

    async def run_usage(self, run_id: str) -> dict:
        """按用途汇总一次运行。普通 SELECT，不加锁。

        查询接口若把行锁住，会堵住正在结算的那次请求。要的是当前读得到的账。
        """
        async with self._lease() as conn:
            rows = await self._all(
                conn, _USAGE_SELECT + ' WHERE run_id=%s GROUP BY 1 ORDER BY 1',
                (run_id,))
        grouped = [_usage_row(row) for row in rows]
        return {'run_id': run_id, 'by_purpose': grouped, 'total': _usage_total(grouped)}

    async def tenant_usage(self, *, tenant_id: str = '', day_from: str = '',
                           day_to: str = '') -> dict:
        """按租户和日期区间汇总，给运营对账与导出。同样不加锁。"""
        clauses: list[str] = []
        params: list[str] = []
        if tenant_id:
            clauses.append('tenant_id=%s')
            params.append(tenant_id)
        if day_from:
            clauses.append('day>=%s')
            params.append(day_from)
        if day_to:
            clauses.append('day<=%s')
            params.append(day_to)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        async with self._lease() as conn:
            rows = await self._all(
                conn, _USAGE_SELECT + where + ' GROUP BY 1 ORDER BY 1', tuple(params))
        grouped = [_usage_row(row) for row in rows]
        return {'tenant_id': tenant_id, 'day_from': day_from, 'day_to': day_to,
                'by_purpose': grouped, 'total': _usage_total(grouped)}

    async def enforce_run_token_budget(self, run_id: str) -> int:
        """下一次模型请求前，用已经记账的量决定还能不能再发。返回计入的 token 数。

        已结算行加的是输入加输出（实测或估算）。仍是 reserved 的行还没有 token，
        但请求已经在别的执行者手上：只读已结算会把它漏掉，于是按单次上界
        （窗口 + 输出上限）计一行。搜索行没有 token，不进这个和。

        不加锁，也不写 runs。结算发生在请求结束之后，手上没有 owner 与 epoch，
        写 runs 就是没有门禁的越权写。总量用 SUM 现算。
        """
        ceiling = settings.llm_context_window + settings.llm_output_tokens
        async with self._lease() as conn:
            row = await self._one(conn, _TOKEN_SPEND_SQL, ('reserved', run_id, 'llm'))
        used = (int(row['input_tokens']) + int(row['output_tokens'])
                + int(row['reserved_requests']) * ceiling)
        limit = int(settings.run_max_tokens)
        if used >= limit:
            raise BudgetExceeded(f'本次运行的 token 已用 {used}，上限 {limit}')
        return used


# 按用途分组。GROUP BY 1 跟着 SELECT 里的 COALESCE，避免空用途被拆成两堆。
# 语句里没有 FOR UPDATE：按 run / 按租户查询都不能挡住正在写的结算。
_USAGE_SELECT = (
    "SELECT COALESCE(purpose, '') AS purpose, COUNT(*) AS requests, "
    'COALESCE(SUM(input_tokens),0) AS input_tokens, '
    'COALESCE(SUM(output_tokens),0) AS output_tokens, '
    'COUNT(*) FILTER (WHERE tokens_estimated) AS estimated_requests '
    'FROM provider_charges'
)

_TOKEN_SPEND_SQL = (
    'SELECT COALESCE(SUM(input_tokens),0) AS input_tokens, '
    'COALESCE(SUM(output_tokens),0) AS output_tokens, '
    "COUNT(*) FILTER (WHERE status=%s) AS reserved_requests "
    'FROM provider_charges WHERE run_id=%s AND provider=%s'
)


def _usage_row(row) -> dict:
    return {'purpose': row['purpose'] or '',
            'requests': int(row['requests']),
            'input_tokens': int(row['input_tokens']),
            'output_tokens': int(row['output_tokens']),
            'estimated_requests': int(row['estimated_requests'])}


def _usage_total(rows: list[dict]) -> dict:
    keys = ('requests', 'input_tokens', 'output_tokens', 'estimated_requests')
    return {key: sum(row[key] for row in rows) for key in keys}
