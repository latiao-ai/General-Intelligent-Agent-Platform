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
# 编号沿用已发布的 v12_turn_owner_role（与未发布的 v12_session_turn_role 同一列，
# 不另起一个 v12，避免存量库对同一列记两套版本号）。
TURN_OWNER_ROLE = 'v12_turn_owner_role'
# 用户级工作目录：前端「选择工作空间」选中的本地目录，按租户+用户落一行。
USER_WORKSPACE = 'v13_user_workspace'
# 会话级连接器选择：每条 turn 记住用户在当前会话选中的连接器，供工具注入与审计。
CONNECTOR_SELECTION = 'v14_connector_selection'
# 会话级选择也落到 sessions 行：业务 run 执行时按会话读取，而不是只看某条 turn。
SESSION_CONNECTOR_SELECTION = 'v15_session_connector_selection'
# 会话的已验证平台主体：聊天/查询没有业务 run，MCP stdio 仍需要它做登录来源校验。
SESSION_PLATFORM_SUBJECT = 'v16_session_platform_subject'
# 聊天/查询 turn 的逐字草稿：前端轮询 timeline 时能边生成边显示。
TURN_DRAFT = 'v17_turn_draft'
# 会话 turn 的独立事件流（SSE）：answer_delta 等按自增游标持久化。
TURN_EVENTS = 'v18_turn_events'
# 登录平台登记表：把"中台支持哪些平台的账号密码登录"从环境变量搬进库，控制台可增删。
AUTH_PLATFORMS = 'v19_auth_platforms'
# 定时任务：scheduled_tasks / scheduled_task_runs 两张表（docs/scheduled-tasks.md）。
# 下一步是 v22：投递 supersede 与每任务日预算。
SCHEDULED_TASKS = 'v20_scheduled_tasks'
# 服务心跳：service_heartbeats 一张表。API 与 Worker 是两个进程，过去没有
# 任何证据能说明 Worker 还活着，Worker 挂掉后消息永久停在 accepted，用户
# 看到的是"发出去没回复"。这张表让接纳层能当场拒绝、让监控能告警。
SERVICE_HEARTBEATS = 'v21_service_heartbeats'
# 专家 · 技能 · 连接器：会话级选择列 + 自定义专家表（docs/addons.md）。
# 技能不落库：磁盘（SKILLS_DIR）是唯一事实来源，建索引表只会带来双写不一致。
ADDONS = 'v22_addons'
# 本地账号登录：local_accounts 一张表（docs/local-account-auth.md）。
# 存量库靠下面 UPGRADES 里逐字相同的 DDL 建出来；只改 SCHEMA 快照不算升级。
LOCAL_ACCOUNTS = 'v23_local_accounts'
# 机器人（Bot Mode，docs/bot-mode.md）：sessions 的身份列 + bots / bot_sections /
# bot_deliveries / bot_rooms / bot_room_events / bot_room_tasks 六张表 +
# scheduled_tasks.bot_id。存量库靠下面 UPGRADES 里逐字相同的 DDL 建出来。
BOT_MODE = 'v24_bot_mode'
# 积分：credit_grants（充值/赠送/人工调整流水，append-only）与 credit_orders
# （购买订单，支付通道未接前一律 pending，不落账）。消耗不单独记账，由
# provider_charges 按用户实时聚合——计费链路本来就有那份数据，再抄一份到流水
# 只会出现两份对不上的账。存量库靠下面 UPGRADES 里逐字相同的 DDL 建出来。
CREDITS = 'v25_credits'
# 消息平台（IM 网关，docs/messaging-platforms.md）：渠道配置、运行态快照、入站队列、
# 幂等表、配对、会话映射、出站队列、iLink 登录态与审计事件。运行态放库里而不是
# 进程内：API 只读、Worker 单写，两个进程看到的是同一份状态。
MESSAGING = 'v26_messaging'
# 机器人广场（docs/bot-plaza.md）：bot_plaza_items（可被一键启用的条目快照）+
# bot_plaza_installs（谁装了哪个条目，install → bot 的幂等指针）。条目是**快照**
# 而不是指向用户机器人的指针：投稿人之后改自己的人设，不能悄悄改掉广场上的版本。
# 存量库靠下面 UPGRADES 里逐字相同的 DDL 建出来。
BOT_PLAZA = 'v27_bot_plaza'
# 消息级技能胶囊（docs/skill-chip.md）：messages 增 content_blocks，保存结构化
# 内容块（text / resource_link）。回显时前端据此把技能胶囊渲染成只读 chip，
# 不再解析消息正文里的 `@skill:` 标记。存量库靠下面 UPGRADES 里的 DDL 补列。
MESSAGE_CONTENT_BLOCKS = 'v28_message_content_blocks'
# 会话锚点：runs 增 session_id（业务 run 由 tasks 回填、turn 记录由 session_turns
# 回填），并把「一个会话只能有一个未结束业务目标」这条应用约定升级成部分唯一
# 索引——原来是靠「必须先锁 session 行」维持的，谁绕过 create_goal 直接插 runs
# 就静默失效。
SESSION_RUN_ANCHOR = 'v29_session_run_anchor'
# 回退 v29 里过严的唯一索引：「一会话一活跃 run」并不成立——一个目标允许多次
# 执行尝试（恢复/补偿都会 create_run），旧 run 未必已终态，硬约束会拒掉合法重试。
# 改为 create_goal / create_run 共用的应用层守卫。
DROP_OVER_STRICT_GOAL_INDEX = 'v30_drop_over_strict_goal_index'
# 消息平台多实例（一个机器人一个 IM 应用）：`im_channels` / `im_runtime` 的主键各加
# 一维 `instance_id`。同一个 owner 下，同一个渠道因此可以有多份配置，各自一套凭据、
# 一套运行态——这正是「飞书里挂五个机器人」需要的形状：五个企业自建应用，五份
# AppID/Secret，各自一条回调。
#
# `instance_id` 默认 '' 表示**存量那一份**：所有查询都把它当默认值，老代码不传就是
# 它，行为一字不变。队列与审计表先补列不参与查询——这一片只有默认实例在跑，等
# 真正允许开第二个实例时再让出站/入站带上实例（否则消息会用错凭据发出去）。
IM_INSTANCES = 'v31_im_instances'
IM_EVENT_INSTANCE = 'v32_im_event_instance'
# 会话级身份：`im_sessions.bot_id` 是「这个群/这段私聊归哪个机器人」的**覆盖值**，
# 空串表示跟随渠道默认（`im_channels.bot_id`）。加这一列是因为「渠道绑定一个
# 机器人」只够一个群：一个飞书应用被拉进十个群，常常希望客服群和内部群各自
# 不同人格。凭据是应用级的，人格是会话级的，两者本来就不是一回事。
#
# 同时给会话列表加索引：消息页的「会话身份」卡按最近活跃倒序取前 N 条。
IM_SESSION_BOT = 'v33_im_session_bot'
LATEST = IM_SESSION_BOT
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
'''), (USER_WORKSPACE, '''
CREATE TABLE IF NOT EXISTS user_workspaces (
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    path TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_user_id)
);
'''), (CONNECTOR_SELECTION, '''
ALTER TABLE session_turns
    ADD COLUMN IF NOT EXISTS connector_ids_json TEXT NOT NULL DEFAULT '[]';
'''), (SESSION_CONNECTOR_SELECTION, '''
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS connector_ids_json TEXT;
'''), (SESSION_PLATFORM_SUBJECT, '''
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS platform_subject TEXT;
'''), (TURN_DRAFT, '''
ALTER TABLE session_turns
    ADD COLUMN IF NOT EXISTS draft_answer TEXT NOT NULL DEFAULT '';
'''), (TURN_EVENTS, '''
CREATE TABLE IF NOT EXISTS session_turn_events (
    id          BIGSERIAL PRIMARY KEY,
    turn_id     TEXT NOT NULL,
    event_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_turn_events_turn
    ON session_turn_events(turn_id, id);
'''), (AUTH_PLATFORMS, '''
CREATE TABLE IF NOT EXISTS auth_platforms (
    platform_id  TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    login_mode   TEXT NOT NULL DEFAULT 'password_http',
    enabled      BOOLEAN NOT NULL DEFAULT true,
    is_default   BOOLEAN NOT NULL DEFAULT false,
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    identity_provider_id TEXT NOT NULL DEFAULT '',
    base_url     TEXT NOT NULL DEFAULT '',
    config       TEXT NOT NULL DEFAULT '{}',
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL DEFAULT '',
    updated_by   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auth_platforms_enabled ON auth_platforms(enabled);
'''), (SCHEDULED_TASKS, '''
-- 与 SCHEMA 快照里同名表逐字一致：新库由基线建，存量库靠这一条升级。
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    schedule_id        TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    owner_user_id      TEXT NOT NULL DEFAULT 'default',
    owner_role         TEXT NOT NULL DEFAULT 'member',
    platform_id        TEXT NOT NULL DEFAULT '',
    platform_subject   TEXT NOT NULL DEFAULT '',
    name               TEXT NOT NULL,
    prompt             TEXT NOT NULL,
    enabled            BOOLEAN NOT NULL DEFAULT true,
    schedule_type      TEXT NOT NULL DEFAULT 'recurring',
    rrule              TEXT NOT NULL DEFAULT '',
    scheduled_at       TEXT,
    valid_from         TEXT,
    valid_until        TEXT,
    next_run_at        DOUBLE PRECISION,
    next_attempt_at    DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_run_at        DOUBLE PRECISION,
    last_status        TEXT NOT NULL DEFAULT '',
    last_error_code    TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    deferred_count     INTEGER NOT NULL DEFAULT 0,
    connector_ids_json TEXT NOT NULL DEFAULT '[]',
    delivery_json      TEXT NOT NULL DEFAULT '{}',
    lease_owner        TEXT NOT NULL DEFAULT '',
    lease_expires_at   DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease_epoch        BIGINT NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
    ON scheduled_tasks(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_owner
    ON scheduled_tasks(tenant_id, owner_user_id, created_at);
CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    occurrence_id  TEXT PRIMARY KEY,
    schedule_id    TEXT NOT NULL,
    tenant_id      TEXT NOT NULL DEFAULT 'default',
    owner_user_id  TEXT NOT NULL DEFAULT 'default',
    due_at         DOUBLE PRECISION NOT NULL,
    status         TEXT NOT NULL DEFAULT 'queued',
    task_id        TEXT NOT NULL DEFAULT '',
    run_id         TEXT NOT NULL DEFAULT '',
    session_id     TEXT NOT NULL DEFAULT '',
    error_code     TEXT NOT NULL DEFAULT '',
    error_message  TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    delivery_status TEXT NOT NULL DEFAULT '',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    started_at     DOUBLE PRECISION,
    finished_at    DOUBLE PRECISION,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_task_runs_due
    ON scheduled_task_runs(schedule_id, due_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_schedule
    ON scheduled_task_runs(schedule_id, created_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_open
    ON scheduled_task_runs(schedule_id) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_session
    ON scheduled_task_runs(session_id);
'''), (SERVICE_HEARTBEATS, '''
-- 与 SCHEMA 快照里同名表逐字一致。存量库靠这一条建出来。
CREATE TABLE IF NOT EXISTS service_heartbeats (
    service     TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    beat_at     DOUBLE PRECISION NOT NULL,
    started_at  DOUBLE PRECISION NOT NULL,
    host        TEXT NOT NULL DEFAULT '',
    pid         BIGINT NOT NULL DEFAULT 0,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (service, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_service_heartbeats_beat
    ON service_heartbeats(service, beat_at);
'''), (ADDONS, '''
-- 会话级选择：WorkBuddy 的 addon_selection 对应列。`{}` = 没选任何东西。
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS addon_selection TEXT NOT NULL DEFAULT '{}';
-- 自定义专家。内置专家是代码目录（addons/catalog.py），不进库：
-- 往每张库里灌一批固定行会让升级时"用户改过的内置专家"无从判断。
CREATE TABLE IF NOT EXISTS experts (
    expert_id          TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    owner_user_id      TEXT NOT NULL DEFAULT 'default',
    name               TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    system_prompt      TEXT NOT NULL DEFAULT '',
    skill_ids_json     TEXT NOT NULL DEFAULT '[]',
    connector_ids_json TEXT NOT NULL DEFAULT '[]',
    model              TEXT NOT NULL DEFAULT '',
    icon               TEXT NOT NULL DEFAULT '🧩',
    color              TEXT NOT NULL DEFAULT '#eef4ff',
    tag                TEXT NOT NULL DEFAULT '自定义',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experts_owner
    ON experts(tenant_id, owner_user_id, created_at);
'''), (LOCAL_ACCOUNTS, '''
-- 与 SCHEMA 快照里的 local_accounts 逐字一致。存量库靠这一条建出来。
CREATE TABLE IF NOT EXISTS local_accounts (
    username             TEXT NOT NULL,
    tenant_id            TEXT NOT NULL DEFAULT 'default',
    user_id              TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    password_algo        TEXT NOT NULL DEFAULT 'scrypt-16384-8-1',
    password_updated_at  TEXT NOT NULL,
    failed_count         INTEGER NOT NULL DEFAULT 0,
    locked_until         DOUBLE PRECISION NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    last_login_at        TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    created_by           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, username)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_accounts_user
    ON local_accounts(user_id);
'''), (BOT_MODE, '''
-- 与 SCHEMA 快照里的 Bot Mode 段落逐字一致。存量库靠这一条建出来。
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS room_id TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS session_kind TEXT NOT NULL DEFAULT 'task';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS hidden INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS capability_epoch TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_bot_chat
    ON sessions(bot_id) WHERE session_kind = 'bot_chat';
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_bot_room
    ON sessions(bot_id, room_id) WHERE session_kind = 'bot_room';
CREATE INDEX IF NOT EXISTS idx_sessions_bot
    ON sessions(bot_id, session_kind);

CREATE TABLE IF NOT EXISTS bots (
    bot_id             TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    owner_user_id      TEXT NOT NULL DEFAULT 'default',
    handle             TEXT NOT NULL DEFAULT '',
    name               TEXT NOT NULL,
    title              TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    soul               TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL DEFAULT '',
    skill_ids_json     TEXT NOT NULL DEFAULT '[]',
    connector_ids_json TEXT NOT NULL DEFAULT '[]',
    section_id         TEXT NOT NULL DEFAULT '',
    icon               TEXT NOT NULL DEFAULT '🤖',
    color              TEXT NOT NULL DEFAULT '#eef4ff',
    shape              TEXT NOT NULL DEFAULT 'circle',
    image              TEXT NOT NULL DEFAULT '',
    pinned             INTEGER NOT NULL DEFAULT 0,
    hidden             INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active',
    ui_json            TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bots_handle
    ON bots(owner_user_id, handle);
CREATE INDEX IF NOT EXISTS idx_bots_owner
    ON bots(tenant_id, owner_user_id, created_at);

CREATE TABLE IF NOT EXISTS bot_sections (
    section_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    owner_user_id TEXT NOT NULL DEFAULT 'default',
    name          TEXT NOT NULL,
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_sections_name
    ON bot_sections(owner_user_id, name);

CREATE TABLE IF NOT EXISTS bot_deliveries (
    delivery_id   TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    owner_user_id TEXT NOT NULL DEFAULT 'default',
    from_bot      TEXT NOT NULL DEFAULT '',
    to_bot        TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'dm',
    room_id       TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    hop           INTEGER NOT NULL DEFAULT 0,
    max_hops      INTEGER NOT NULL DEFAULT 2,
    status        TEXT NOT NULL DEFAULT 'queued',
    turn_id       TEXT NOT NULL DEFAULT '',
    session_id    TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_deliveries_inbox
    ON bot_deliveries(to_bot, status);
CREATE INDEX IF NOT EXISTS idx_bot_deliveries_turn
    ON bot_deliveries(turn_id);

CREATE TABLE IF NOT EXISTS bot_rooms (
    room_id          TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    owner_user_id    TEXT NOT NULL DEFAULT 'default',
    name             TEXT NOT NULL,
    topic            TEXT NOT NULL DEFAULT '',
    members_json     TEXT NOT NULL DEFAULT '[]',
    max_rounds       INTEGER NOT NULL DEFAULT 3,
    max_messages     INTEGER NOT NULL DEFAULT 10,
    round_no         INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'open',
    next_seq         INTEGER NOT NULL DEFAULT 1,
    lease_owner      TEXT NOT NULL DEFAULT '',
    lease_expires_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease_generation BIGINT NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_rooms_owner
    ON bot_rooms(tenant_id, owner_user_id, created_at);

CREATE TABLE IF NOT EXISTS bot_room_events (
    room_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    event_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    actor        TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (room_id, seq),
    UNIQUE (room_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_room_events_kind
    ON bot_room_events(room_id, kind);

CREATE TABLE IF NOT EXISTS bot_room_tasks (
    task_id    TEXT PRIMARY KEY,
    room_id    TEXT NOT NULL,
    epoch      INTEGER NOT NULL DEFAULT 1,
    round_no   INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    bot_id     TEXT NOT NULL,
    turn_id    TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'running',
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (room_id, epoch, round_no, position)
);
CREATE INDEX IF NOT EXISTS idx_bot_room_tasks_open
    ON bot_room_tasks(room_id, status);
CREATE INDEX IF NOT EXISTS idx_bot_room_tasks_turn
    ON bot_room_tasks(turn_id);

ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT '';
'''), (CREDITS, '''
CREATE TABLE IF NOT EXISTS credit_grants (
    grant_id      TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    amount_micros BIGINT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'grant',
    note          TEXT NOT NULL DEFAULT '',
    operator_id   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_grants_user
    ON credit_grants(tenant_id, user_id, created_at);

CREATE TABLE IF NOT EXISTS credit_orders (
    order_id    TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    package_id  TEXT NOT NULL,
    points      BIGINT NOT NULL,
    price_cents INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    paid_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_credit_orders_user
    ON credit_orders(tenant_id, user_id, created_at);
'''), (MESSAGING, '''
-- 与 SCHEMA 快照里的消息平台段落逐字一致。存量库靠这一条建出来。
CREATE TABLE IF NOT EXISTS im_channels (
    tenant_id          TEXT NOT NULL,
    owner_user_id      TEXT NOT NULL,
    channel_id         TEXT NOT NULL,
    enabled            INTEGER NOT NULL DEFAULT 0,
    config_json        TEXT NOT NULL DEFAULT '{}',
    bot_id             TEXT NOT NULL DEFAULT '',
    dm_policy          TEXT NOT NULL DEFAULT 'pair',
    group_policy       TEXT NOT NULL DEFAULT 'mention',
    allowed_users_json TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS im_runtime (
    tenant_id     TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',
    error_code    TEXT NOT NULL DEFAULT '',
    fingerprint   TEXT NOT NULL DEFAULT '',
    worker_id     TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS im_inbox (
    inbox_id      TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_at       DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (platform, tenant_id, owner_user_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_im_inbox_due
    ON im_inbox(status, next_at);

CREATE TABLE IF NOT EXISTS im_processed (
    platform   TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    message_id TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (platform, tenant_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_im_processed_time
    ON im_processed(created_at);

CREATE TABLE IF NOT EXISTS im_pairing (
    tenant_id     TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    platform      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    chat_id       TEXT NOT NULL DEFAULT '',
    code_hash     TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    expires_at    DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_user_id, platform, user_id)
);

CREATE TABLE IF NOT EXISTS im_sessions (
    tenant_id     TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    chat_type     TEXT NOT NULL DEFAULT 'dm',
    session_id    TEXT NOT NULL,
    generation    INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_user_id, platform, chat_id)
);

CREATE TABLE IF NOT EXISTS im_outbox (
    outbox_id        TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    owner_user_id    TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    platform_chat_id TEXT NOT NULL,
    sent_key         TEXT NOT NULL,
    text             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_at          DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_error       TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (tenant_id, owner_user_id, channel_id, sent_key)
);
CREATE INDEX IF NOT EXISTS idx_im_outbox_due
    ON im_outbox(status, next_at);

CREATE TABLE IF NOT EXISTS im_login_state (
    tenant_id     TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    state_json    TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS im_events (
    event_id      BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    direction     TEXT NOT NULL DEFAULT '',
    chat_id_hash  TEXT NOT NULL DEFAULT '',
    message_id    TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT '',
    error_code    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_im_events_channel
    ON im_events(tenant_id, owner_user_id, channel_id, event_id DESC);
'''), (BOT_PLAZA, '''
-- 与 SCHEMA 快照里的 Bot 广场段落逐字一致。存量库靠这一条建出来。
-- 条目存的是**发布那一刻的快照**（人设/技能/连接器/模型），不是指向 bots 的指针：
-- 投稿人之后再改自己的机器人，不应该悄悄改掉别人已经看到的版本；
-- 要更新广场版本，重新投稿一次（version+1，重新进入评审）。
CREATE TABLE IF NOT EXISTS bot_plaza_items (
    item_id            TEXT PRIMARY KEY,
    slug               TEXT NOT NULL DEFAULT '',
    source_bot_id      TEXT NOT NULL DEFAULT '',
    publisher_user_id  TEXT NOT NULL DEFAULT '',
    publisher_tenant   TEXT NOT NULL DEFAULT 'default',
    name               TEXT NOT NULL,
    handle             TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    soul               TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL DEFAULT '',
    skill_ids_json     TEXT NOT NULL DEFAULT '[]',
    connector_ids_json TEXT NOT NULL DEFAULT '[]',
    icon               TEXT NOT NULL DEFAULT '🤖',
    color              TEXT NOT NULL DEFAULT '#eef4ff',
    shape              TEXT NOT NULL DEFAULT 'circle',
    image              TEXT NOT NULL DEFAULT '',
    category           TEXT NOT NULL DEFAULT 'general',
    tags_json          TEXT NOT NULL DEFAULT '[]',
    readme             TEXT NOT NULL DEFAULT '',
    starters_json      TEXT NOT NULL DEFAULT '[]',
    scope              TEXT NOT NULL DEFAULT 'tenant',
    status             TEXT NOT NULL DEFAULT 'draft',
    featured           INTEGER NOT NULL DEFAULT 0,
    position           INTEGER NOT NULL DEFAULT 0,
    install_count      INTEGER NOT NULL DEFAULT 0,
    view_count         INTEGER NOT NULL DEFAULT 0,
    review_note        TEXT NOT NULL DEFAULT '',
    reviewed_by        TEXT NOT NULL DEFAULT '',
    reviewed_at        TEXT NOT NULL DEFAULT '',
    version            INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_plaza_slug
    ON bot_plaza_items(slug);
CREATE INDEX IF NOT EXISTS idx_bot_plaza_listing
    ON bot_plaza_items(status, scope, position, created_at);
CREATE INDEX IF NOT EXISTS idx_bot_plaza_publisher
    ON bot_plaza_items(publisher_tenant, publisher_user_id, created_at);

-- 安装登记：一个用户对同一条目只装一次。install 行是"装过没有"的唯一事实，
-- 不靠 bots.ui_json 里的标记——那样查一次列表要扫全部机器人行。
CREATE TABLE IF NOT EXISTS bot_plaza_installs (
    install_id    TEXT PRIMARY KEY,
    item_id       TEXT NOT NULL,
    bot_id        TEXT NOT NULL DEFAULT '',
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    owner_user_id TEXT NOT NULL DEFAULT 'default',
    item_version  INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (tenant_id, owner_user_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_plaza_installs_item
    ON bot_plaza_installs(item_id);
'''), (MESSAGE_CONTENT_BLOCKS, '''
-- 消息级技能胶囊：结构化内容块落库（回显只读 chip 用），正文仍是 content 纯文本。
ALTER TABLE messages ADD COLUMN IF NOT EXISTS content_blocks TEXT NOT NULL DEFAULT '';
'''), (SESSION_RUN_ANCHOR, '''
-- 会话锚点：让「这个会话还有没有没结束的目标」变成一次索引扫描。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS session_id TEXT;

-- 业务 run：发起它的任务挂在哪个会话下。
UPDATE runs r SET session_id = t.session_id
  FROM tasks t
 WHERE t.task_id = r.task_id AND r.session_id IS NULL;

-- turn 内部执行记录：task_id 就是 run_id，没有 tasks 行，只能从 turn 反查。
UPDATE runs r SET session_id = st.session_id
  FROM session_turns st
 WHERE st.turn_id = r.run_id AND r.session_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, status);
'''), (DROP_OVER_STRICT_GOAL_INDEX, '''
-- v29 在这里建过一个「一会话一活跃 run」的部分唯一索引，方向是错的：
-- 一个目标（tasks 行）允许多次执行尝试——hub_recovery / hub_compensation 都会
-- 对同一 task 再 create_run，而旧 run 常常还没到终态。硬约束把这些合法的重试
-- 一起拒了。这条不变量改由应用层维持：PgRuns._reject_other_open_goal()，
-- create_goal 与 create_run 共用同一判定。
DROP INDEX IF EXISTS uq_runs_one_active_goal_per_session;
'''), (IM_INSTANCES, '''
-- 配置面：加一维实例。PK 必须一起换掉，否则第二个实例的 upsert 会命中老行，
-- 把别人的 AppID 配置覆盖成自己的。
ALTER TABLE im_channels ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE im_channels DROP CONSTRAINT IF EXISTS im_channels_pkey;
ALTER TABLE im_channels ADD PRIMARY KEY (tenant_id, owner_user_id, channel_id, instance_id);

-- 运行态：每实例一份快照。合成一份会让两个实例互相把对方的状态覆盖成 connected。
ALTER TABLE im_runtime ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE im_runtime DROP CONSTRAINT IF EXISTS im_runtime_pkey;
ALTER TABLE im_runtime ADD PRIMARY KEY (tenant_id, owner_user_id, channel_id, instance_id);

-- 队列 / 配对 / 会话：只补列，**这一片不参与查询**（只有默认实例在跑）。
-- 但要先把位置留出来：出站队列不带实例的话，多实例一开就会用 A 应用的凭据
-- 把 B 应用的消息发出去。
ALTER TABLE im_outbox ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE im_inbox ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE im_pairing ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE im_sessions ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_im_outbox_instance
    ON im_outbox(channel_id, instance_id, status);
'''), (IM_EVENT_INSTANCE, '''
-- 审计事件也得分清是谁收发的：同一个「飞书」在两个实例下都有一条流，
-- 不记实例的话，在 A 机器人的事件卡里会看到 B 机器人的收发记录。
ALTER TABLE im_events ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
DROP INDEX IF EXISTS idx_im_events_channel;
CREATE INDEX IF NOT EXISTS idx_im_events_channel
    ON im_events(tenant_id, owner_user_id, channel_id, instance_id, event_id DESC);
'''), (IM_SESSION_BOT, '''
-- 会话级身份覆盖：'' = 跟随渠道默认机器人。非空时，这个群/这段私聊由它作答。
ALTER TABLE im_sessions ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT '';

-- 映射的主键也要带上实例。企业微信的 ChatId 是 corp 级的、飞书同一批人拉的两个
-- 应用也可能给出同一个 chat_id：只按 (渠道, 会话) 去重的话，两个机器人会共用
-- 一条映射，消息每来一次就把身份换一次（A→B→A），每一句都开新会话。
ALTER TABLE im_sessions DROP CONSTRAINT IF EXISTS im_sessions_pkey;
ALTER TABLE im_sessions ADD PRIMARY KEY (tenant_id, owner_user_id, platform, instance_id, chat_id);

-- 「会话身份」卡按最近活跃倒序取，走这个索引，避免全表扫。
CREATE INDEX IF NOT EXISTS idx_im_sessions_owner
    ON im_sessions(tenant_id, owner_user_id, platform, instance_id, updated_at DESC);
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
    # 结构升级是运维动作，不该被会话级 statement_timeout 砍断：给大表建索引、
    # 回填数据都可能超过 30 秒，中途被掐会留下半套结构。等锁仍受 30 秒约束——
    # 那把锁是别的事务占着业务表，继续等下去没有意义。
    await conn.execute('SET LOCAL statement_timeout = 0')
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
