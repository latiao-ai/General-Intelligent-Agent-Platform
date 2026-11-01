"""Ordered, transactional schema upgrades, serialized across all replicas."""
from datetime import datetime, timezone

BASELINE = 'v1_baseline_20261029'
GOVERNANCE = 'v2_production_governance'
RUN_TRACE = 'v3_run_trace'
LATEST = RUN_TRACE
LOCK_ID = 719284631
UPGRADES = ((GOVERNANCE, '''
CREATE TABLE api_rate_windows (
    scope TEXT NOT NULL, window_start BIGINT NOT NULL, requests INTEGER NOT NULL,
    PRIMARY KEY (scope, window_start)
);
CREATE INDEX idx_api_rate_windows_time ON api_rate_windows(window_start);
CREATE TABLE tenant_usage_days (
    tenant_id TEXT NOT NULL, day TEXT NOT NULL, requests BIGINT NOT NULL DEFAULT 0,
    charged_micros BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);
CREATE TABLE provider_charges (
    charge_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, day TEXT NOT NULL,
    run_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    reserved_micros BIGINT NOT NULL, charged_micros BIGINT NOT NULL,
    input_tokens BIGINT, output_tokens BIGINT, status TEXT NOT NULL DEFAULT 'reserved',
    created_at DOUBLE PRECISION NOT NULL, duration_seconds DOUBLE PRECISION,
    trace_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_provider_charges_tenant_day ON provider_charges(tenant_id, day);
'''), (RUN_TRACE, "ALTER TABLE runs ADD COLUMN trace_id TEXT NOT NULL DEFAULT '';"))


async def apply(conn, *, baseline: str, legacy_ids=()) -> list[str]:
    """Caller owns the transaction. No DDL is repeated once its version is recorded."""
    await conn.execute('SET LOCAL lock_timeout = \'30s\'')
    await conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
    await conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations ('
                       'migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
    cur = await conn.execute('SELECT migration_id FROM schema_migrations')
    rows = await cur.fetchall()
    applied = {r['migration_id'] if isinstance(r, dict) else r[0] for r in rows}
    stamp = datetime.now(timezone.utc).isoformat()
    changed = []
    if BASELINE not in applied:
        await conn.execute(baseline)
        for key in (*legacy_ids, BASELINE):
            await conn.execute('INSERT INTO schema_migrations VALUES (%s,%s) '
                               'ON CONFLICT DO NOTHING', (key, stamp))
        changed.append(BASELINE)
    for key, sql in UPGRADES:
        if key not in applied:
            await conn.execute(sql)
            await conn.execute('INSERT INTO schema_migrations VALUES (%s,%s)', (key, stamp))
            changed.append(key)
    return changed


async def require_current(conn) -> None:
    cur = await conn.execute("SELECT to_regclass('schema_migrations')")
    row = await cur.fetchone()
    exists = next(iter(row.values())) if isinstance(row, dict) else row[0]
    if exists:
        cur = await conn.execute('SELECT migration_id FROM schema_migrations WHERE migration_id=%s', (LATEST,))
        if await cur.fetchone():
            return
    raise RuntimeError('Database schema is out of date; run python -m scripts.migrate first')
