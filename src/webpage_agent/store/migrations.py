"""有序、事务化、跨副本串行的结构升级通道。

**铁律（增删列之前先读这一段）**

`SCHEMA`（定义在 `webpage_agent.store.pg_store`）是**全新库的完整快照**，只为
"从零建库"服务：`apply()` 只在 `v1_baseline` 还没登记时执行它一次，此后**再也
不会**执行。由此推出三条硬规则：

1. **任何针对存量库的结构变更，都必须作为一条 `UPGRADES` 条目写进来**，并带上
   自己的版本号。改完 `SCHEMA` 不算数。
2. **不要**指望往 `SCHEMA` 里加 `ALTER TABLE ... ADD COLUMN` 能升级旧库。它看着
   像迁移，实际只在全新库建表阶段顺带跑一次；对已记录基线的库永远不会生效，
   而且不报错、不告警——那一列就是永远不存在，直到某次读写碰到它才炸。
3. 往 `SCHEMA` 里补列的唯一正当用途是"让新库建表语句保持完整快照"。这种情形
   （新库有列、旧库没有，且没有对应 `UPGRADES`）由 `verify_schema()` 当场拦下。

历史背景：本项目早期确实靠 `SCHEMA` 里那批 `ADD COLUMN IF NOT EXISTS` 升级过
旧库（PG 支持 `IF NOT EXISTS`，并进建表脚本当时是有效的）。问题在于一旦有人把它
当成常规迁移机制，后面新增的列就会静默丢失，而 `require_current()` 只看版本号
是否登记、查不出列缺失。所以现在规则写死在这里，同时让启动/部署阶段真的去比对
活库结构（`verify_schema()`）。
"""
from datetime import datetime, timezone

BASELINE = 'v1_baseline_20261029'
GOVERNANCE = 'v2_production_governance'
RUN_TRACE = 'v3_run_trace'
# 用途与估算标记单独一个版本：改 SCHEMA 里的 DO 块不会作用到已经记过基线的库。
USAGE_DETAIL = 'v4_provider_usage_detail'
# 引擎归属：runs 增加 runtime_kind / runtime_version / runtime_state_schema。
RUNTIME_OWNERSHIP = 'v5_runtime_ownership'
# 最终答复去重：messages 增 dedupe_key（部分唯一索引），恢复补收尾幂等。
FINAL_ANSWER_DEDUPE = 'v6_final_answer_dedupe'
# 外部任务取消意图：external_tasks 增 cancel_requested（区分本地停止/远端取消请求/
# 远端已确认取消）。第八阶段第 9 点。
EXTERNAL_CANCEL_INTENT = 'v7_external_cancel_intent'
# 会话 turn：独立聊天 turn 记录（第十一阶段）。请求指纹、顺序、状态、引擎身份、
# 租约、执行代次、关联业务 run。
SESSION_TURNS = 'v8_session_turns'
# 会话 turn 追问与歧义：source_turn_id（追问链）、candidates_json（歧义候选）、
# clarification（追问文案）、execution_generation（执行代次，区分 lease_epoch 的
# 领取代次——每次重新领取都 +1，用于「过期 running 重领」与「同会话串行」判定）。
TURN_FOLLOWUP = 'v9_turn_followup'
# 会话 turn 可信主体：tenant_id / owner_user_id（供只读查询工具从 turn 本身取得
# 当前用户身份，不依赖业务 run 碰巧提供）。第十一阶段 A 组第 5 点。
TURN_PRINCIPAL = 'v10_turn_principal'
# 新建 run 的列默认改为 pi。已有行保持原 runtime_kind（存量仍是 langgraph）。
RUNTIME_DEFAULT_PI = 'v11_runtime_default_pi'
# 会话 turn 上的角色。接纳时已经传入 owner_role，领取后要原样交给查询执行记录。
TURN_OWNER_ROLE = 'v12_turn_owner_role'
LATEST = TURN_OWNER_ROLE
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
-- 全新库的 SCHEMA 已经建过这张完整的表。这里必须 IF NOT EXISTS：
-- 否则基线建表成功后，v2 再 CREATE 会直接撞车，升级通道走不完。
-- 已执行过 v2 的存量库不会重跑这段，缺的列由 v4 补。
CREATE TABLE IF NOT EXISTS provider_charges (
    charge_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, day TEXT NOT NULL,
    run_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    reserved_micros BIGINT NOT NULL, charged_micros BIGINT NOT NULL,
    input_tokens BIGINT, output_tokens BIGINT, status TEXT NOT NULL DEFAULT 'reserved',
    created_at DOUBLE PRECISION NOT NULL, duration_seconds DOUBLE PRECISION,
    trace_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_provider_charges_tenant_day ON provider_charges(tenant_id, day);
'''), (RUN_TRACE, "ALTER TABLE runs ADD COLUMN trace_id TEXT NOT NULL DEFAULT '';"),
    (USAGE_DETAIL, '''
ALTER TABLE provider_charges ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT '';
ALTER TABLE provider_charges ADD COLUMN IF NOT EXISTS tokens_estimated BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_provider_charges_run ON provider_charges(run_id);
'''), (RUNTIME_OWNERSHIP, '''
ALTER TABLE runs ADD COLUMN IF NOT EXISTS runtime_kind TEXT NOT NULL DEFAULT 'langgraph';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS runtime_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS runtime_state_schema INTEGER NOT NULL DEFAULT 1;
'''), (FINAL_ANSWER_DEDUPE, '''
ALTER TABLE messages ADD COLUMN IF NOT EXISTS dedupe_key TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_dedupe
    ON messages(session_id, dedupe_key) WHERE dedupe_key <> '';
'''), (EXTERNAL_CANCEL_INTENT, '''
ALTER TABLE external_tasks ADD COLUMN IF NOT EXISTS cancel_requested INTEGER NOT NULL DEFAULT 0;
'''), (SESSION_TURNS, '''
CREATE TABLE IF NOT EXISTS session_turns (
    turn_id            TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    turn_seq           INTEGER NOT NULL,
    request_id         TEXT NOT NULL,
    request_hash       TEXT NOT NULL DEFAULT '',
    message            TEXT NOT NULL,
    target_task_id     TEXT NOT NULL DEFAULT '',
    target_run_id      TEXT NOT NULL DEFAULT '',
    kind               TEXT NOT NULL DEFAULT 'chat',
    status             TEXT NOT NULL DEFAULT 'accepted',
    engine_kind        TEXT NOT NULL DEFAULT '',
    engine_version     INTEGER NOT NULL DEFAULT 0,
    lease_owner        TEXT NOT NULL DEFAULT '',
    lease_expires_at   DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease_epoch        INTEGER NOT NULL DEFAULT 0,
    answer             TEXT NOT NULL DEFAULT '',
    error              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_turns_request
    ON session_turns(session_id, request_id);
CREATE INDEX IF NOT EXISTS idx_session_turns_session
    ON session_turns(session_id, turn_seq);
CREATE INDEX IF NOT EXISTS idx_session_turns_claim
    ON session_turns(status, lease_expires_at);
'''), (TURN_FOLLOWUP, '''
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS source_turn_id TEXT NOT NULL DEFAULT '';
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS candidates_json TEXT NOT NULL DEFAULT '';
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS clarification TEXT NOT NULL DEFAULT '';
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS execution_generation INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_session_turns_claim2
    ON session_turns(session_id, status, lease_expires_at);
'''), (TURN_PRINCIPAL, '''
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS owner_user_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_session_turns_principal
    ON session_turns(tenant_id, owner_user_id);
'''), (RUNTIME_DEFAULT_PI, '''
ALTER TABLE runs ALTER COLUMN runtime_kind SET DEFAULT 'pi';
'''), (TURN_OWNER_ROLE, '''
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS owner_role TEXT NOT NULL DEFAULT 'member';
'''))


class SchemaDrift(RuntimeError):
    """活库结构与 `SCHEMA` 快照不一致：有表或列没被任何迁移建出来。"""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        super().__init__(
            '数据库结构与迁移脚本不一致，缺 %s。' % '、'.join(self.missing)
            + '原因通常是有人只改了 SCHEMA 却没写进 UPGRADES——'
              'SCHEMA 只服务全新库，对已记录基线的库永不执行。'
              '请把这条变更作为新的 UPGRADES 条目加上自己的版本号后重跑 '
              'python -m scripts.migrate')


# 建表语句体里，以这些词开头的是表级约束，不是列定义
_CONSTRAINT_WORDS = ('PRIMARY', 'UNIQUE', 'CONSTRAINT', 'FOREIGN',
                     'CHECK', 'EXCLUDE', 'LIKE')

_CREATE_TABLE = r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w."]+)\s*\('
_ADD_COLUMN = (r'ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([\w."]+)\s+'
               r'ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([\w"]+)')


def _unquote(name: str) -> str:
    return name.strip().strip('"').split('.')[-1]


def _balanced(text: str, open_index: int) -> tuple[str, int]:
    """返回 `(` 起那段括号里的内容与收尾位置。类型里的括号（varchar(8)）也在这里被吃掉。"""
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return text[open_index + 1:index], index + 1
    raise ValueError('SCHEMA 里的括号不配对')


def _split_top_level(body: str) -> list[str]:
    """按顶层逗号切分列定义（`NUMERIC(10,2)` 里的逗号不能切）。"""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        if char == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
            continue
        current.append(char)
    if ''.join(current).strip():
        parts.append(''.join(current))
    return parts


def parse_schema(baseline: str) -> dict[str, set[str]]:
    """把 `SCHEMA` 文本解析成 `{表: {列}}`，即"全新库应有的完整快照"。

    建表语句和 `ALTER TABLE ... ADD COLUMN` 合并计算：对新库来说，那些 ALTER
    就是**列的主要来源**（`runs.intent_version` 之类只在 ALTER 里出现），
    不是重复定义的兜底。
    """
    import re

    text = re.sub(r'--[^\n]*', '', baseline)
    columns: dict[str, set[str]] = {}
    for match in re.finditer(_CREATE_TABLE, text, re.I):
        body, _ = _balanced(text, match.end() - 1)
        table = _unquote(match.group(1))
        bucket = columns.setdefault(table, set())
        for part in _split_top_level(body):
            tokens = part.split()
            if not tokens:
                continue
            if tokens[0].upper() in _CONSTRAINT_WORDS:
                continue
            bucket.add(_unquote(tokens[0]))
    for match in re.finditer(_ADD_COLUMN, text, re.I):
        columns.setdefault(_unquote(match.group(1)), set()).add(
            _unquote(match.group(2)))
    return columns


async def verify_schema(conn, baseline: str) -> dict:
    """把 `SCHEMA` 快照与活库实测结构对齐，缺表/缺列直接抛 `SchemaDrift`。

    这是那条"改 SCHEMA 等于白改"的静默失效的护栏：只要有列写进了 `SCHEMA`
    却没有任何 `UPGRADES` 把它带给存量库，这里就会指着 `表.列` 报出来。
    只比较"快照有、库里没有"，库里的额外列（`UPGRADES` 加进来的，如
    `provider_charges`）不算问题。
    """
    expected = parse_schema(baseline)
    cur = await conn.execute(
        'SELECT table_name, column_name FROM information_schema.columns '
        'WHERE table_schema = current_schema()')
    present: dict[str, set[str]] = {}
    for row in await cur.fetchall():
        table, column = ((row['table_name'], row['column_name'])
                         if isinstance(row, dict) else (row[0], row[1]))
        present.setdefault(table, set()).add(column)
    missing = []
    for table, wanted in sorted(expected.items()):
        if table not in present:
            missing.append(f'{table}（整表缺失）')
            continue
        missing.extend(f'{table}.{column}'
                       for column in sorted(wanted - present[table]))
    if missing:
        raise SchemaDrift(missing)
    return {'tables': len(expected),
            'columns': sum(len(columns) for columns in expected.values())}


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
    # 升完立刻核对：只改了 SCHEMA 的列在这里就会暴露，而不是等某天读写才炸
    await verify_schema(conn, baseline)
    return changed


async def require_current(conn) -> None:
    """启动自检：版本号已登记**且**活库结构与 SCHEMA 快照一致才算当前。"""
    cur = await conn.execute("SELECT to_regclass('schema_migrations')")
    row = await cur.fetchone()
    exists = next(iter(row.values())) if isinstance(row, dict) else row[0]
    current = False
    if exists:
        cur = await conn.execute('SELECT migration_id FROM schema_migrations WHERE migration_id=%s', (LATEST,))
        current = await cur.fetchone() is not None
    if not current:
        raise RuntimeError('Database schema is out of date; run python -m scripts.migrate first')
    # 版本号只说明"迁移脚本自己认为跑完了"，说明不了列真的在。这里再对一次结构。
    from webpage_agent.store.pg_store import SCHEMA
    await verify_schema(conn, SCHEMA)
