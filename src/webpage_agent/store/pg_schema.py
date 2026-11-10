"""PostgreSQL 基线 DDL（`SCHEMA`）和已登记的迁移编号（`MIGRATIONS`）。

从 `pg_store` 原样移出，再由 `pg_store` 以同名导出。语句和说明都没有改。
基线字符串单独就超过 600 行，留在 `pg_store.py` 会让那个文件过 400 行。
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    owner_user_id TEXT NOT NULL DEFAULT 'default',
    connector_ids_json TEXT,
    platform_subject TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
-- 旧库升级：PG 支持 ADD COLUMN IF NOT EXISTS，直接并进建表脚本即可当迁移
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS owner_user_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS connector_ids_json TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS platform_subject TEXT;
-- 用户级工作目录：前端「选择工作空间」选中的本地目录。
-- 存量库由 v13_user_workspace 迁移建表，这里只是让新库快照保持完整。
CREATE TABLE IF NOT EXISTS user_workspaces (
    tenant_id     TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    path          TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dedupe_key TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_dedupe
    ON messages(session_id, dedupe_key) WHERE dedupe_key <> '';
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL,
    artifact_id TEXT,
    error       TEXT,
    state       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE TABLE IF NOT EXISTS session_memory (
    session_id TEXT PRIMARY KEY,
    summary    TEXT NOT NULL DEFAULT '',
    facts      TEXT NOT NULL DEFAULT '{}',
    upto       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    task_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);

-- ---------- 中台对象：task / run 分离（文档 §3.1） ----------
-- tasks 是持续目标，runs 是对这个目标的一次执行尝试。
-- 旧表沿用，这里补上"当前运行"和对 run 的引用，避免外部 task_id 与内部产物
-- 目录 ID 语义混用。
CREATE TABLE IF NOT EXISTS tasks_meta (
    task_id        TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL DEFAULT 'default',
    owner_user_id  TEXT NOT NULL DEFAULT 'default',
    goal           TEXT NOT NULL DEFAULT '',
    current_run_id TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    status              TEXT NOT NULL,
    plan_version        INTEGER NOT NULL DEFAULT 0,
    budget_json         TEXT NOT NULL DEFAULT '{}',
    consumed_json       TEXT NOT NULL DEFAULT '{}',
    cancel_requested_at TEXT,
    pause_requested     INTEGER NOT NULL DEFAULT 0,
    lease_owner         TEXT,
    lease_expires_at    DOUBLE PRECISION,
    lease_epoch         INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    compensated_at      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    runtime_kind        TEXT NOT NULL DEFAULT 'pi',
    runtime_version     INTEGER NOT NULL DEFAULT 1,
    runtime_state_schema INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, lease_expires_at);

-- ---------- 计划（文档 §6.1） ----------
CREATE TABLE IF NOT EXISTS plans (
    run_id     TEXT NOT NULL,
    version    INTEGER NOT NULL,
    goal       TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, version)
);

-- ---------- 步骤（文档 §6、§9.1） ----------
CREATE TABLE IF NOT EXISTS steps (
    run_id       TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    step_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    tool_key     TEXT NOT NULL,
    status       TEXT NOT NULL,
    inputs_json  TEXT NOT NULL DEFAULT '{}',
    output_json  TEXT,
    action_id    TEXT,
    started_at   TEXT,
    ended_at     TEXT,
    error        TEXT,
    PRIMARY KEY (run_id, plan_version, step_id)
);

-- ---------- 动作账本（文档 §8） ----------
-- action 是一次业务操作的身份；attempt 是真实发出的每一次调用。
-- 幂等键挂在 action 上，重试只是新增 attempt。
CREATE TABLE IF NOT EXISTS actions (
    action_id       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    plan_version    INTEGER NOT NULL,
    step_id         TEXT NOT NULL,
    tool_key        TEXT NOT NULL,
    args_hash       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    effect          TEXT NOT NULL DEFAULT 'unknown',
    status          TEXT NOT NULL,
    receipt_json    TEXT,
    authorized_by   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_idem ON actions(idempotency_key);

CREATE TABLE IF NOT EXISTS action_attempts (
    attempt_id   TEXT PRIMARY KEY,
    action_id    TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    error_type   TEXT,
    result_ref   TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_action ON action_attempts(action_id);

-- ---------- 产物（文档 §9.1） ----------
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    storage_key TEXT NOT NULL,
    mime        TEXT NOT NULL DEFAULT 'text/html',
    size        INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- ---------- 会话记忆的来源与覆盖边界（文档 §11、§12） ----------
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id    TEXT PRIMARY KEY,
    summary       TEXT NOT NULL DEFAULT '',
    facts         TEXT NOT NULL DEFAULT '{}',
    covered_upto  INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT 'auto',
    generated_ver INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL
);

-- ---------- 长期记忆（文档 §12） ----------
-- 作用域化：user / project / tenant 三级，单用户阶段全部落到 default。
CREATE TABLE IF NOT EXISTS memories (
    memory_id  TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL DEFAULT 'default',
    scope_type TEXT NOT NULL,
    scope_id   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    predicate  TEXT NOT NULL,
    value      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    version    INTEGER NOT NULL DEFAULT 1,
    supersedes TEXT,
    source     TEXT NOT NULL DEFAULT '',
    valid_to   TEXT,
    created_by TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_type, scope_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_fact
    ON memories(scope_type, scope_id, subject, predicate, version);

-- 遗忘用墓碑：不能让旧后台任务从同一来源把已删事实再次生成
CREATE TABLE IF NOT EXISTS memory_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    scope_type   TEXT NOT NULL,
    scope_id     TEXT NOT NULL,
    fact_key     TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tombstones_key
    ON memory_tombstones(scope_type, scope_id, fact_key);

-- ---------- 后台任务（文档 §9.2 outbox） ----------
CREATE TABLE IF NOT EXISTS background_jobs (
    job_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    payload_json    TEXT NOT NULL DEFAULT '{}',
    attempts        INTEGER NOT NULL DEFAULT 0,
    lease_owner     TEXT,
    lease_expires_at DOUBLE PRECISION,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe ON background_jobs(kind, dedupe_key);

-- ---------- 请求幂等（POST /api/tasks） ----------
CREATE TABLE IF NOT EXISTS request_keys (
    scope_key     TEXT PRIMARY KEY,
    client_key    TEXT NOT NULL,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    owner_user_id TEXT NOT NULL DEFAULT 'default',
    endpoint      TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    task_id       TEXT,
    session_id    TEXT,
    run_id        TEXT,
    response_json TEXT,
    created_at    TEXT NOT NULL,
    expires_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_keys_expiry ON request_keys(expires_at);

-- 旧库升级：CREATE TABLE IF NOT EXISTS 不会补列。PG 支持 IF NOT EXISTS，
-- 所以升级语句可以和建表放在一起，启动时幂等执行。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS compensated_at TEXT;

-- ---------- 身份：平台身份 → 本地主体（webpage_agent/api/auth.py、文档 §3.1） ----------
-- 本地用户是业务外键的唯一目标；平台标识（openid / sub / 邮箱）只用于"找到它"。
-- 为什么不让业务表直接挂 openid：微信 openid 按 appid 隔离、部分平台的 sub 会
-- 轮换，一旦变了，用户的历史会话、任务、产物就全部对不上人。
CREATE TABLE IF NOT EXISTS app_users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    email        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 一个本地用户可以绑多个平台身份（微信 / 飞书 / 企业 SSO）。
-- (provider, subject) 唯一：同一平台同一主体重复登录只会命中，不会分身账号。
CREATE TABLE IF NOT EXISTS identities (
    provider     TEXT NOT NULL,
    subject      TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    identity_provider_id TEXT NOT NULL DEFAULT '',
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    email        TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'member',
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);
-- 旧库升级：CREATE TABLE IF NOT EXISTS 不补列，ALTER 必须显式执行（且在建索引之前）
ALTER TABLE identities ADD COLUMN IF NOT EXISTS identity_provider_id TEXT NOT NULL DEFAULT '';
-- 老数据的身份源就用原来的 provider，保证存量绑定继续命中同一个账号
UPDATE identities SET identity_provider_id = provider WHERE identity_provider_id = '';

-- 身份绑定的**唯一关系**：企业 + 身份源 + 外部用户 → 中台用户。
-- 老列 (provider, subject) 保留给旧数据：迁移后 provider 与身份源一致。
CREATE UNIQUE INDEX IF NOT EXISTS uq_identities_scope
    ON identities(tenant_id, identity_provider_id, subject)
    WHERE identity_provider_id <> '';

-- 票据兑换记录：(issuer, jti) 唯一 → 同一张票据在多实例并发下也只能成功一次
CREATE TABLE IF NOT EXISTS ticket_redemptions (
    issuer      TEXT NOT NULL,
    jti         TEXT NOT NULL,
    connection_id TEXT NOT NULL DEFAULT '',
    platform_id TEXT NOT NULL DEFAULT '',
    tenant_id   TEXT NOT NULL DEFAULT '',
    expires_at  BIGINT NOT NULL,
    used_at     TEXT NOT NULL,
    auth_session_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (issuer, jti)
);
CREATE INDEX IF NOT EXISTS idx_ticket_redemptions_used
    ON ticket_redemptions(used_at);

-- 运行快照：崩溃恢复的权威依据（state.json 只是诊断旁文件）。
CREATE TABLE IF NOT EXISTS run_snapshots (
    run_id                TEXT PRIMARY KEY,
    plan_version          INTEGER NOT NULL DEFAULT 1,
    snapshot_schema       INTEGER NOT NULL DEFAULT 1,
    state_json            TEXT NOT NULL DEFAULT '{}',
    next_step_id          TEXT NOT NULL DEFAULT '',
    artifact_namespace    TEXT NOT NULL DEFAULT '',
    updated_at            TEXT NOT NULL
);

-- ---------- 运行中补充输入 ----------
-- 三个版本必须分开存：lease_epoch（谁有权推进）、intent_version（用户要求更新到
-- 哪一版）、plan_version（当前采用哪份计划）。混用任何一个都会让"旧要求产生的
-- 操作"重新获得准入资格。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS intent_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS processed_intent_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS intent_version INTEGER NOT NULL DEFAULT 1;
-- 这份计划替代了哪一版计划（0 表示首版），审计"谁被谁取代"用
ALTER TABLE plans ADD COLUMN IF NOT EXISTS base_plan_version INTEGER NOT NULL DEFAULT 0;
-- 每个动作依据的要求版本：审计"这次写入是按哪一版要求做的"
ALTER TABLE actions ADD COLUMN IF NOT EXISTS decision_intent_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_snapshots ADD COLUMN IF NOT EXISTS intent_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_snapshots ADD COLUMN IF NOT EXISTS processed_intent_version INTEGER NOT NULL DEFAULT 0;

-- 补充输入的**不可变日志**：真实来源是这里，不是 messages 表。
-- (run_id, request_id) 唯一 → 客户端重试不会重复加版本；
-- (run_id, input_seq) 唯一 → 顺序由服务端在锁住 run 之后分配，不用 MAX+1 猜。
CREATE TABLE IF NOT EXISTS run_inputs (
    input_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    input_seq       INTEGER NOT NULL,
    request_id      TEXT NOT NULL,
    message         TEXT NOT NULL,
    payload_hash    TEXT NOT NULL DEFAULT '',
    intent_version  INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_inputs_request
    ON run_inputs(run_id, request_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_inputs_seq
    ON run_inputs(run_id, input_seq);

-- ---------- 结果观察与决策 ----------
-- 结果水位：每次"相关结果落库"在同一事务 +1。判断决策覆盖到哪一步只能用它，
-- 不能用墙钟时间（跨进程、跨副本的时钟没有可比性）。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS observation_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_snapshots ADD COLUMN IF NOT EXISTS observation_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_snapshots ADD COLUMN IF NOT EXISTS next_route TEXT NOT NULL DEFAULT '';

-- 一次自主决策的完整台账：预留 → 调模型 → 校验 → CAS 提交。
-- 崩溃语义（文档 §9）靠它区分"没请求 / 请求了没响应 / 候选未提交 / 已提交"。
CREATE TABLE IF NOT EXISTS run_decisions (
    decision_id            TEXT PRIMARY KEY,
    run_id                 TEXT NOT NULL,
    decision_key           TEXT NOT NULL,       -- 稳定身份：去重靠它，不靠时间
    reason                 TEXT NOT NULL DEFAULT 'observation',
    status                 TEXT NOT NULL DEFAULT 'reserved',
    intent_version         INTEGER NOT NULL DEFAULT 0,
    base_plan_version      INTEGER NOT NULL DEFAULT 0,
    observation_seq        INTEGER NOT NULL DEFAULT 0,
    decision_kind          TEXT NOT NULL DEFAULT '',
    candidate_plan_hash    TEXT NOT NULL DEFAULT '',
    committed_plan_version INTEGER NOT NULL DEFAULT 0,
    input_snapshot         TEXT NOT NULL DEFAULT '',   -- 给模型看的输入（有界）
    provider_requests      INTEGER NOT NULL DEFAULT 0,
    usage_json             TEXT NOT NULL DEFAULT '{}',
    error_code             TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_decisions_key
    ON run_decisions(run_id, decision_key);
CREATE INDEX IF NOT EXISTS idx_run_decisions_run ON run_decisions(run_id);

-- 处理投影：可以从 run_inputs + plans 重建，因此允许任何新执行者继续处理，
-- 不用一个永久的 processing 标记把崩溃的任务锁死。
CREATE TABLE IF NOT EXISTS run_input_processing (
    input_id              TEXT PRIMARY KEY,
    status                TEXT NOT NULL DEFAULT 'accepted',
    processed_plan_version INTEGER NOT NULL DEFAULT 0,
    error_code            TEXT NOT NULL DEFAULT '',
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_input_processing_status
    ON run_input_processing(status);

-- 中台自己的登录会话。凭证原文只在浏览器，这里只存摘要。
CREATE TABLE IF NOT EXISTS auth_sessions (
    auth_session_id  TEXT PRIMARY KEY,
    token_digest     TEXT NOT NULL UNIQUE,
    user_id          TEXT NOT NULL,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    platform_id      TEXT NOT NULL DEFAULT '',
    connection_id    TEXT NOT NULL DEFAULT '',
    identity_provider_id TEXT NOT NULL DEFAULT '',
    subject          TEXT NOT NULL DEFAULT '',
    issued_at        TEXT NOT NULL,
    expires_at       DOUBLE PRECISION NOT NULL,
    last_seen_at     TEXT NOT NULL,
    revoked_at       TEXT,
    user_agent       TEXT NOT NULL DEFAULT '',
    remote_addr      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);

-- ============ 工具运行时（《智能体中台说明书》） ============
-- 迁移编号：schema_version 记录已执行的迁移集合，迁移必须在同一发布代际只跑一次
-- 或保证可重入。下面的语句都写成幂等形式，因此重复执行是安全的。

-- ---------- M1：通用结果（信封 v2 + 分块） ----------
-- 结果与它的分块是**受保护数据**：只存引用与元数据，原始内容落在受控存储。
-- 全局内容 hash 去重**不**隐式共享跨租户读取权：读权限由 result_id 的归属决定。
CREATE TABLE IF NOT EXISTS tool_result_manifests (
    result_id           TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    owner_user_id       TEXT NOT NULL DEFAULT 'default',
    run_id              TEXT NOT NULL DEFAULT '',
    step_id             TEXT NOT NULL DEFAULT '',
    action_id           TEXT NOT NULL DEFAULT '',
    attempt_id          TEXT NOT NULL DEFAULT '',
    tool_key            TEXT NOT NULL DEFAULT '',
    schema_hash         TEXT NOT NULL DEFAULT '',
    contract_version    TEXT NOT NULL DEFAULT '',
    transport_status    TEXT NOT NULL DEFAULT 'ok',
    business_status     TEXT NOT NULL DEFAULT 'not_applicable',
    verification_status TEXT NOT NULL DEFAULT 'not_applicable',
    raw_ref             TEXT NOT NULL DEFAULT '',
    structured_ref      TEXT NOT NULL DEFAULT '',
    summary_json        TEXT NOT NULL DEFAULT '{}',
    completeness_json   TEXT NOT NULL DEFAULT '{}',
    schema_version      INTEGER NOT NULL DEFAULT 2,
    size_bytes          BIGINT NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_result_manifests_run ON tool_result_manifests(run_id);
CREATE INDEX IF NOT EXISTS idx_result_manifests_owner
    ON tool_result_manifests(tenant_id, owner_user_id);

CREATE TABLE IF NOT EXISTS tool_result_parts (
    part_id      TEXT PRIMARY KEY,
    result_id    TEXT NOT NULL,
    ordinal      INTEGER NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/json',
    kind         TEXT NOT NULL DEFAULT 'json',
    mime         TEXT NOT NULL DEFAULT '',
    storage_key  TEXT NOT NULL DEFAULT '',
    link_ref     TEXT NOT NULL DEFAULT '',
    size_bytes   BIGINT NOT NULL DEFAULT 0,
    sha256       TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    visibility   TEXT NOT NULL DEFAULT 'owner',
    UNIQUE (result_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_result_parts_result ON tool_result_parts(result_id);

-- ---------- M2：工具治理（服务端绑定 / 凭据绑定 / 政策 / 准入授权） ----------
CREATE TABLE IF NOT EXISTS mcp_server_bindings (
    binding_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    server_id     TEXT NOT NULL,
    transport     TEXT NOT NULL DEFAULT 'streamable_http',
    config_ref    TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    revision      INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    UNIQUE (tenant_id, server_id)
);

-- 只存**密钥引用**，不存明文。明文由凭据解析器在传输层短时取出。
CREATE TABLE IF NOT EXISTS credential_bindings (
    binding_id        TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    subject_kind      TEXT NOT NULL DEFAULT 'tenant',
    subject_id        TEXT NOT NULL DEFAULT 'default',
    server_binding_id TEXT NOT NULL DEFAULT '',
    credential_ref    TEXT NOT NULL DEFAULT '',
    scope_json        TEXT NOT NULL DEFAULT '{}',
    revision          INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credential_bindings_subject
    ON credential_bindings(tenant_id, subject_kind, subject_id);

-- 政策：scope 只用预定义字段/集合，不执行任意规则代码。
CREATE TABLE IF NOT EXISTS tool_policies (
    policy_id     TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    tool_key      TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT '',
    user_id       TEXT NOT NULL DEFAULT '',
    resource_scope TEXT NOT NULL DEFAULT '',
    effect        TEXT NOT NULL DEFAULT 'unknown',
    approval_mode TEXT NOT NULL DEFAULT 'auto',
    limits_json   TEXT NOT NULL DEFAULT '{}',
    revision      INTEGER NOT NULL DEFAULT 1,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    UNIQUE (tenant_id, tool_key, role, user_id)
);

-- 准入授权：记录"这次调用凭什么被放行"。唯一 action + 有效准入身份，
-- 保留审计历史（重试新增行，不覆盖旧行）。
CREATE TABLE IF NOT EXISTS tool_authorizations (
    authorization_id  TEXT PRIMARY KEY,
    action_id         TEXT NOT NULL,
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    user_id           TEXT NOT NULL DEFAULT 'default',
    tool_key          TEXT NOT NULL DEFAULT '',
    schema_hash       TEXT NOT NULL DEFAULT '',
    args_hash         TEXT NOT NULL DEFAULT '',
    resource_hash     TEXT NOT NULL DEFAULT '',
    credential_binding TEXT NOT NULL DEFAULT '',
    credential_subject TEXT NOT NULL DEFAULT '',
    credential_revision INTEGER NOT NULL DEFAULT 0,
    policy_revision   INTEGER NOT NULL DEFAULT 0,
    approval_id       TEXT NOT NULL DEFAULT '',
    expires_at        DOUBLE PRECISION NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'admitted',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_auth_action ON tool_authorizations(action_id);
CREATE INDEX IF NOT EXISTS idx_tool_auth_credential
    ON tool_authorizations(tenant_id, credential_binding);

-- ---------- M3：结构化批准 ----------
CREATE TABLE IF NOT EXISTS approvals (
    approval_id      TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    owner_user_id    TEXT NOT NULL DEFAULT 'default',
    requester_user_id TEXT NOT NULL DEFAULT 'default',
    approver_user_id TEXT NOT NULL DEFAULT '',
    run_id           TEXT NOT NULL DEFAULT '',
    manifest_json    TEXT NOT NULL DEFAULT '{}',
    manifest_hash    TEXT NOT NULL DEFAULT '',
    policy_revision  INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',
    expires_at       DOUBLE PRECISION NOT NULL DEFAULT 0,
    limits_json      TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    approved_at      TEXT,
    revoked_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_pending
    ON approvals(tenant_id, status);

-- 预留：按 (approval_id, action_id, manifest_item_key) 唯一。
-- 额度在锁住批准行之后原子计算并预留，两个并发动作不能超用同一份批准。
CREATE TABLE IF NOT EXISTS approval_reservations (
    reservation_id   TEXT PRIMARY KEY,
    approval_id      TEXT NOT NULL,
    action_id        TEXT NOT NULL,
    manifest_item_key TEXT NOT NULL DEFAULT '',
    units            INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'reserved',
    created_at       TEXT NOT NULL,
    released_at      TEXT,
    UNIQUE (approval_id, action_id, manifest_item_key)
);
CREATE INDEX IF NOT EXISTS idx_approval_reservations_approval
    ON approval_reservations(approval_id);

-- ---------- M4：外部任务（异步长任务合同） ----------
CREATE TABLE IF NOT EXISTS external_tasks (
    external_task_id  TEXT PRIMARY KEY,
    remote_task_id    TEXT NOT NULL DEFAULT '',
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    owner_user_id     TEXT NOT NULL DEFAULT 'default',
    server_binding_id TEXT NOT NULL DEFAULT '',
    credential_binding TEXT NOT NULL DEFAULT '',
    credential_subject TEXT NOT NULL DEFAULT '',
    tool_key          TEXT NOT NULL DEFAULT '',
    contract_id       TEXT NOT NULL DEFAULT '',
    contract_version  INTEGER NOT NULL DEFAULT 1,
    run_id            TEXT NOT NULL DEFAULT '',
    step_id           TEXT NOT NULL DEFAULT '',
    action_id         TEXT NOT NULL DEFAULT '',
    accepted_result_id TEXT NOT NULL DEFAULT '',
    submission_key    TEXT NOT NULL DEFAULT '',
    item_key          TEXT NOT NULL DEFAULT '',
    state             TEXT NOT NULL DEFAULT 'accepted',
    version           INTEGER NOT NULL DEFAULT 1,
    next_poll_at      DOUBLE PRECISION NOT NULL DEFAULT 0,
    deadline_at       DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_observed_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_result_id    TEXT NOT NULL DEFAULT '',
    output_manifest_ref TEXT NOT NULL DEFAULT '',
    error_code        TEXT NOT NULL DEFAULT '',
    poll_owner        TEXT NOT NULL DEFAULT '',
    poll_epoch        INTEGER NOT NULL DEFAULT 0,
    poll_lease_expires_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_external_tasks_submission
    ON external_tasks(run_id, action_id, item_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_external_tasks_remote
    ON external_tasks(tenant_id, server_binding_id, credential_subject, remote_task_id)
    WHERE remote_task_id <> '';
CREATE INDEX IF NOT EXISTS idx_external_tasks_due
    ON external_tasks(state, next_poll_at);

-- ---------- M5：作业与状态扩展 ----------
-- background_jobs 增加 due_at / lease_epoch：调度到期时间与领取代次必须分开，
-- 退避的重试不能把到期时间塞进 lease_expires_at（那会让旧 Worker 与新 Worker
-- 都以为对方还活着）。
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS due_at DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS lease_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0;
-- 领取作业要按 (status, due_at) 找候选行：没有这个索引时，作业一多就是全表扫，
-- 而且 `FOR UPDATE SKIP LOCKED` 的候选集也会变慢。
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON background_jobs(status, due_at);

-- run 增加 waiting_external 生命周期：外部任务已受理未完成，**不是**完成。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS waiting_external INTEGER NOT NULL DEFAULT 0;
-- run 记录**执行主体**：工具调用的授权按主体判定，主体必须来自库里已提交的
-- 行，而不是请求头里的自报字段或模型输出。没有它时 Worker 无法知道
-- "这次调用该用哪把钥匙"。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS owner_user_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS owner_role TEXT NOT NULL DEFAULT 'member';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS platform_id TEXT NOT NULL DEFAULT '';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS platform_subject TEXT NOT NULL DEFAULT '';
-- step/action 增加受理阶段：提交拿到了回执，但该逻辑步骤尚未完成。
ALTER TABLE steps ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT '';
ALTER TABLE actions ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT '';
ALTER TABLE actions ADD COLUMN IF NOT EXISTS external_task_id TEXT NOT NULL DEFAULT '';
-- 已验证的现场上下文（当前项目 / 选中对象）。浏览器自报的 JSON 不能直接当它用。
ALTER TABLE tasks_meta ADD COLUMN IF NOT EXISTS scene_context_json TEXT NOT NULL DEFAULT '';

-- run 的引擎归属：runtime_kind / 运行时实现版本 / 状态格式版本。
-- v5 给存量行回填过 langgraph（历史标记，不再有对应引擎）；
-- v11 把列默认改成 pi。已存在的行不会被这条 ALTER 改写。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS runtime_kind TEXT NOT NULL DEFAULT 'pi';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS runtime_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS runtime_state_schema INTEGER NOT NULL DEFAULT 1;

-- 最终答复的去重键：恢复补收尾时按 (session_id, dedupe_key) 幂等，
-- 避免「进程在终态与最终消息之间崩溃」后恢复重复追加最终答复。
ALTER TABLE messages ADD COLUMN IF NOT EXISTS dedupe_key TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_dedupe
    ON messages(session_id, dedupe_key) WHERE dedupe_key <> '';

-- 费用明细的完整快照。v2 里那张表是给「已经记过基线、还没有这张表」的存量库建的，
-- 而且建完就不再执行。用途和估算标记必须写在这里，verify_schema 才认得出
-- 「只改了升级脚本、快照没跟上」。全新库先建这张完整的表，v2 用 IF NOT EXISTS
-- 让开；已经有旧表的库不重跑 v2，靠 v4_provider_usage_detail 补列和按 run 的索引。
CREATE TABLE IF NOT EXISTS provider_charges (
    charge_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, day TEXT NOT NULL,
    run_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    reserved_micros BIGINT NOT NULL, charged_micros BIGINT NOT NULL,
    input_tokens BIGINT, output_tokens BIGINT, status TEXT NOT NULL DEFAULT 'reserved',
    created_at DOUBLE PRECISION NOT NULL, duration_seconds DOUBLE PRECISION,
    trace_id TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    tokens_estimated BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_provider_charges_tenant_day ON provider_charges(tenant_id, day);
-- 按 run 汇总是运营的主路径。只有 (tenant_id, day) 时，这条查询是全表扫。
CREATE INDEX IF NOT EXISTS idx_provider_charges_run ON provider_charges(run_id);

-- ---------- M6：迁移版本记录 ----------
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id  TEXT PRIMARY KEY,
    applied_at    TEXT NOT NULL
);

-- ---------- 会话 turn（第十一阶段：独立聊天 turn，不把每句话建成业务任务） ----------
-- 一次聊天/查询/控制是一轮 turn；业务任务通过 turn 关联（target_task_id/run_id）。
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
    source_turn_id     TEXT NOT NULL DEFAULT '',
    candidates_json    TEXT NOT NULL DEFAULT '',
    clarification      TEXT NOT NULL DEFAULT '',
    connector_ids_json TEXT NOT NULL DEFAULT '[]',
    draft_answer       TEXT NOT NULL DEFAULT '',
    execution_generation INTEGER NOT NULL DEFAULT 0,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    owner_user_id      TEXT NOT NULL DEFAULT 'default',
    owner_role         TEXT NOT NULL DEFAULT 'member',
    answer             TEXT NOT NULL DEFAULT '',
    error              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_turns_request
    ON session_turns(session_id, request_id);
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS connector_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS draft_answer TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_session_turns_session
    ON session_turns(session_id, turn_seq);
CREATE INDEX IF NOT EXISTS idx_session_turns_claim
    ON session_turns(status, lease_expires_at);
"""
# 时间单位约定：
# `background_jobs.due_at` / `lease_expires_at` 与 `external_tasks.*_at` 一律用
# **Unix 秒（DOUBLE PRECISION）**，与既有 `runs.lease_expires_at` 同口径，
# 因此可以直接和 `time.time()` 比较。**不要**把这些列拿去和 PG 的 `now()`
# 比——那会把两种单位混在一起（本项目早年的坑就是这么来的）。
# 新增的调度期限如果以后改用 TIMESTAMPTZ，必须先做显式转换再切换。


# 迁移编号：
# 以下是旧版本登记编号，只在 v1 基线迁移时补记。
# 新的结构变更必须追加到 webpage_agent.store.migrations.UPGRADES，不修改已执行的基线。
MIGRATIONS = (
    'm1_tool_result_manifests',
    'm2_tool_governance',
    'm3_approvals',
    'm4_external_tasks',
    'm5_job_and_state_extensions',
    'm6_schema_migrations',
)
