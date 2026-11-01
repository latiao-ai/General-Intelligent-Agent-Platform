"""Shared rate limits and conservative tenant billing, backed by PostgreSQL."""
import hashlib
import math
import time
import uuid
from contextvars import ContextVar
from contextlib import asynccontextmanager
import logging
from datetime import datetime, timezone

from webpage_agent.core.config import settings

logger = logging.getLogger(__name__)
_scope = ContextVar('provider_scope', default=None)


class QuotaExceeded(RuntimeError):
    safe_message = '租户本日调用或费用额度已用完，请联系管理员。'

    def __init__(self):
        super().__init__(self.safe_message)


def bind(store, tenant_id, run_id=''):
    return _scope.set((store, tenant_id or 'default', run_id))


def unbind(token):
    _scope.reset(token)


async def reserve(*, provider='llm', model=''):
    if not settings.tenant_budget_enabled:
        return None
    scope = _scope.get()
    if scope is None:
        raise RuntimeError('Provider request has no trusted tenant billing scope')
    if provider == 'llm':
        cost = (settings.llm_context_window * settings.llm_input_price_micros_per_million
                + settings.llm_output_tokens * settings.llm_output_price_micros_per_million + 999_999) // 1_000_000
    else:
        cost = settings.search_request_cost_micros
    store, tenant, run_id = scope
    charge_id = uuid.uuid4().hex
    from webpage_agent.observe.telemetry import trace_id, span_id
    await store.reserve_provider_charge(
        charge_id, tenant_id=tenant, run_id=run_id, provider=provider,
        model=model or settings.llm_model, cost_micros=cost,
        max_requests=settings.tenant_daily_provider_requests,
        max_cost_micros=settings.tenant_daily_cost_micros,
        trace_id=trace_id.get())
    parent_span = span_id.get()
    token = span_id.set(uuid.uuid4().hex[:16])
    logger.info('provider_span_start charge=%s provider=%s model=%s parent_span=%s', charge_id, provider, model or settings.llm_model, parent_span)
    return store, charge_id, time.monotonic(), token


async def finish(reservation, *, usage=None, succeeded=False):
    if reservation is None:
        return
    store, charge_id, started, span_token = reservation
    # Failed/cancelled requests can still be billed. Missing usage keeps the upper bound.
    tokens = None
    if succeeded and isinstance(usage, dict):
        a, b = usage.get('input_tokens'), usage.get('output_tokens')
        if type(a) is int and type(b) is int and a >= 0 and b >= 0:
            tokens = a, b
    actual = None
    if tokens:
        actual = (tokens[0] * settings.llm_input_price_micros_per_million
                  + tokens[1] * settings.llm_output_price_micros_per_million + 999_999) // 1_000_000
    try:
        await store.settle_provider_charge(charge_id, actual_micros=actual,
            tokens=tokens, duration=time.monotonic() - started,
            status='succeeded' if succeeded else 'uncertain')
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


class GovernanceStore:
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
                                      max_cost_micros, trace_id=''):
        if min(cost_micros, max_cost_micros) < 0 or max_requests < 1:
            raise ValueError('Invalid tenant billing limits')
        day = datetime.now(timezone.utc).date().isoformat()
        async with self._lease() as conn:
            # Conditional UPSERT serializes all requests for this tenant/day.
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
                'reserved_micros,charged_micros,created_at,trace_id) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (charge_id, tenant_id, day, run_id, provider, model,
                 cost_micros, cost_micros, time.time(), trace_id))

    async def settle_provider_charge(self, charge_id, *, actual_micros=None,
                                     tokens=None, duration=0, status='uncertain'):
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
                'duration_seconds=%s,status=%s WHERE charge_id=%s',
                (charged, tokens[0] if tokens else None, tokens[1] if tokens else None,
                 duration, status, charge_id))

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
