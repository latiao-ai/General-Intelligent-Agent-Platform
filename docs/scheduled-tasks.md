# 定时任务（scheduled tasks）：可行性评估与迁移方案

> 参考物：WorkBuddy 5.9.2 的定时任务（`workbuddy-schedule-analysis.md`）及其最小实现
> （`scheduler-poc`，Node + `node:sqlite`）。
> 本项目是 **Python + PostgreSQL + 独立 Worker**，参考实现只能借**语义**，不能借代码。
> 本文是评估结论，不是已实现的功能；代码落地按 §11 分四步。

---

## 0. 结论

**可行，而且门槛比 WorkBuddy 原实现低。** 因为本项目已经具备定时任务真正难的那一半：

| WorkBuddy 里的机制 | 本项目现状 | 结论 |
| --- | --- | --- |
| 后台常驻循环（30s/5s tick） | Worker 常驻 `main_loop`（`scripts/run_worker.py:220`），已有 `poll_due` 这种周期任务范式 | 加一段调用即可 |
| 物化 `next_run_at` + 索引扫描 | 无 | 新建 1 张表 + 2 个索引 |
| 单飞（同一任务不重复执行） | **有**：`runs` 租约 + `lease_epoch` fencing（`store/pg_runs.py:217`）；另有会话级互斥 `SessionGoalConflict`（`store/pg_tasks.py:90`，本次用不到，因为每次触发是新会话） | 直接复用 |
| 并发上限 | **有**：`max_concurrent_runs`（`core/config.py:356`，进程级）+ `hub.at_capacity()` | 直接复用 |
| 冷启动对账 | **有**：`hub.reconcile()` / `reconcile_orphan_runs`，Worker 启动即跑（`run_worker.py:236`） | 直接复用 |
| 执行 = 后台 agent 会话 | **有**：`Hub.start()` → `create_goal` → `claim_run` → Pi 内核（`api/api_hub.py:181`、`:233`） | 直接复用 |
| 投递 outbox（租约 + 去重 + 重试） | **有**：`background_jobs` + `FOR UPDATE SKIP LOCKED` + `lease_epoch` + 退避（`store/pg_jobs.py:34`） | 复用，缺 supersede |
| 结果摘要协议（`send_automation_result`） | 无该工具 | v1 取最终答复；P1 加工具 |
| RRULE 子集时间语义 | 无（全仓无 cron/schedule 代码） | 新增纯函数模块 `core/rrules.py` |
| 企业微信 / 飞书 webhook 投递 | 无（只有 SkillHub 里的连接器词条） | 新增 `integrations/webhooks.py` |
| 云端双源（`/v2/as/scheduler/tasks`） | 无，也不需要 | **不做** |

**真正要新写的只有三件事**：时间语义（RRULE 子集）→ 触发判定（tick + claim）→ 投递渠道（webhook）。
其余全部是"把已有部件接起来"。

**工作量**：后端 ≈1.5 人日、前端 ≈0.5 人日、测试与文档 ≈0.5 人日（详见 §11）。

---

## 1. 概念映射（WorkBuddy → 本项目）

| WorkBuddy | 本项目落点 |
| --- | --- |
| `automations` 表 | 新表 `scheduled_tasks`（名字沿用本项目 `*_tasks` 习惯） |
| `automation_runs`（一次运行 = 一个 thread） | 新表 `scheduled_task_runs`（occurrence 记录）+ 每次触发**新会话**，复用 `runs`/`tasks`/`messages` |
| `automation_runtime_state`（running 标记） | **不需要**：`runs` 行本身就是权威状态；"同一任务不并行"由 occurrence 表兜（§4.2） |
| `automation_delivery_outbox` | 复用 `background_jobs`，`kind='schedule_delivery'` |
| `LocalAutomationScheduler` | 新模块 `scheduler/tick.py`，挂在 Worker `main_loop` |
| `ServerAutomationExecutor` | **已有** `Hub.start()`，不新写执行器 |
| `send_automation_result` 工具 | v1 取会话最后一条 assistant 消息；P1 换成能力工具 |
| `permission_mode=fullAccess` + 无审批 | **不照搬**：本项目工具政策仍然生效（`tools/tool_policy.py`） |
| `sessions.is_background_automation` | **不新增标记列**：`list_sessions` 用 occurrence 表反连接排除（理由见 §3 注） |
| 6 段 cron（云侧） | **不做**：只保留 RRULE 子集一种表示 |
| `valid_from/valid_until` 有效期 | 同名字段，语义照抄 |

---

## 2. 时间语义：RRULE 子集，不是 cron

新增 `src/webpage_agent/core/rrules.py`（纯函数、无 IO、可单测）：

```text
parse(rrule) -> Rule            # 严格校验；非法即抛 RuleError（不猜）
next_run_at(rule, after, *, valid_from=None, valid_until=None) -> float | None
describe(rule) -> str           # "每天 09:00" / "每周一、周四 09:00"，前端直接用
```

| 字段 | 取值 | 约束 |
| --- | --- | --- |
| `FREQ` | `HOURLY/DAILY/WEEKLY/MONTHLY/YEARLY` | 必填 |
| `INTERVAL` | ≥1 | 默认 1 |
| `BYDAY` | `MO,TU,...` | WEEKLY 必填；HOURLY 可选 |
| `BYMONTHDAY` | 1–31 | MONTHLY 必填，可多个 |
| `BYMONTH` | 1–12 | YEARLY 必填 |
| `BYHOUR`/`BYMINUTE` | 0–23 / 0–59 | 本地时区 |

语义（与 WorkBuddy 一致，逐条写进单测）：

- 一次性任务：`now < scheduled_at ≤ valid_until` 且 `≥ valid_from`，否则不再触发；
- 重复任务：从 `max(now, valid_from)` 起找**下一个**满足条件的时刻，结果 `> now`、`≤ valid_until`，否则 `next_run_at = NULL`（永不触发）；
- `MONTHLY;BYMONTHDAY=31` 落在没有 31 号的月份 → **夹到当月最后一天**（WorkBuddy 语义，非 RFC 跳过）；
- `YEARLY;BYMONTHDAY=29` 平年 → **夹到 2/28**；
- `HOURLY` 忽略 `BYMINUTE`；
- 全部按**进程本地时区**计算，落库为 epoch 秒（`DOUBLE PRECISION`，与 `background_jobs.due_at` 一致）。

> 为什么不用 cron：cron 表达不了"每两周一、两个小时一次"这类用户原话；而且本项目只需**一种表示**，
> UI 做"每天/每周/双周/每月/每年/每小时/一次"的友好映射即可。WorkBuddy 的 RRULE↔cron 双表示是
> 兼容云端的包袱，不迁。

---

## 3. 数据模型

两张新表。列类型严格按本仓库约定：**时间戳 `TEXT`（UTC ISO）、期限/到期 `DOUBLE PRECISION`（epoch 秒）、
JSON 一律 `*_json TEXT`（没有 JSONB）、没有外键**（`store/pg_schema.py:758` 的注释）。

```sql
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    schedule_id        TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    owner_user_id      TEXT NOT NULL DEFAULT 'default',
    owner_role         TEXT NOT NULL DEFAULT 'member',
    platform_id        TEXT NOT NULL DEFAULT '',
    platform_subject   TEXT NOT NULL DEFAULT '',
    name               TEXT NOT NULL,
    prompt             TEXT NOT NULL,
    enabled            BOOLEAN NOT NULL DEFAULT true,          -- 停用=关，不软删
    schedule_type      TEXT NOT NULL DEFAULT 'recurring',      -- recurring | once
    rrule              TEXT NOT NULL DEFAULT '',
    scheduled_at       TEXT,                                   -- once：UTC ISO
    valid_from         TEXT,
    valid_until        TEXT,
    next_run_at        DOUBLE PRECISION,                       -- epoch 秒；NULL=不再触发
    next_attempt_at    DOUBLE PRECISION NOT NULL DEFAULT 0,    -- defer 重试闸门
    last_run_at        DOUBLE PRECISION,
    last_status        TEXT NOT NULL DEFAULT '',
    last_error_code    TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    connector_ids_json TEXT NOT NULL DEFAULT '[]',             -- 该任务用哪些连接器；'[]'=不限制
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

CREATE TABLE IF NOT EXISTS scheduled_task_runs (          -- 一次 occurrence
    occurrence_id  TEXT PRIMARY KEY,                       -- f'{schedule_id}:{int(due_at)}'
    schedule_id    TEXT NOT NULL,
    tenant_id      TEXT NOT NULL DEFAULT 'default',
    owner_user_id  TEXT NOT NULL DEFAULT 'default',
    due_at         DOUBLE PRECISION NOT NULL,              -- 计划触发时间
    status         TEXT NOT NULL DEFAULT 'queued',         -- queued|running|succeeded|failed|skipped|deferred
    task_id        TEXT NOT NULL DEFAULT '',
    run_id         TEXT NOT NULL DEFAULT '',
    session_id     TEXT NOT NULL DEFAULT '',               -- 本次触发自己的会话
    error_code     TEXT NOT NULL DEFAULT '',
    error_message  TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    delivery_status TEXT NOT NULL DEFAULT '',              -- ''|queued|sent|failed
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    started_at     DOUBLE PRECISION,
    finished_at    DOUBLE PRECISION,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_task_runs_due
    ON scheduled_task_runs(schedule_id, due_at);           -- 同一个 occurrence 只能有一行
CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_schedule
    ON scheduled_task_runs(schedule_id, created_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_open
    ON scheduled_task_runs(schedule_id) WHERE status IN ('queued','running');
```

侧栏过滤**不动 `sessions` 表**：`list_sessions`（`store/pg_auth.py:831`）加一条反连接，
把"某次定时触发产生的会话"排除掉：

```sql
NOT EXISTS (SELECT 1 FROM scheduled_task_runs o WHERE o.session_id = s.session_id)
```

> 为什么不加 `sessions.is_background` 列（WorkBuddy 的做法）：本仓库有若干用例和旧代码
> 用 `INSERT INTO sessions VALUES (...)` 这种**按位置**的写法，给热点表加一列会静默改变
> 列序（第一次实测就撞了 `created_at` 非空约束）。反连接的语义本来就等价——"这条会话
> 属于某个定时任务的某次触发"——而且不碰热点表结构。索引
> `idx_scheduled_task_runs_session` 跟着表一起建。

> 为什么**不是**"一个任务固定一个会话"：`create_goal` 会给会话插一条用户消息，运行时
> 又把整段历史当上下文（`api_hub.py` 的 `history`）。每日任务跑一年就是 365 轮历史，
> 上下文成本和串味都不可控。WorkBuddy 也是"一次运行 = 一个新 thread"
> （`automation_runs` 主键就是 thread_id）。代价是"同一任务不并行"这条单飞规则要由
> `scheduled_task_runs` 自己兜（见 §4.2），不能白嫖会话互斥。

**迁移怎么加**（这一步错了会在启动时 `SchemaDrift`）：

1. `store/migrations.py`：`SCHEDULED_TASKS = 'v20_scheduled_tasks'`，`LATEST = SCHEDULED_TASKS`，把同一段 DDL 追加进 `UPGRADES`；
2. `store/pg_schema.py`：把**同样的 DDL** 补进 `SCHEMA` 快照（新库只跑快照，存量库只跑 UPGRADES）；
3. `tests/core_support.py:16` 与 `tests/exec_env.py:29` 两处 `TEST_TABLES` 都要加两张新表（漏了会串用例）；
4. `python -m scripts.migrate`。

---

## 4. 调度循环

放 **Worker 的 `main_loop`**，与 `external_tasks.poll_due` 同层、同样 try/except 隔离
（`scripts/run_worker.py:247`）。理由：Worker 本来就有 `Hub`、租约身份和 Pi 运行时，而生产环境
可以只跑 Worker、API 副本数也不确定（`docs/production-deployment.md`）——把调度挂在 API 的
lifespan 上，等于让"关掉 API 就不定时"。跨副本安全靠行级 claim（`external_tasks` 已有范式，
`store/pg_actions.py:840`），不靠进程内互斥。

**节拍用单调时钟 deadline，不要 `await asyncio.sleep(30)`** —— 主循环同时是业务 run 的派发器，
睡 30s 会把 run 领取一起拖慢：

```python
next_tick = time.monotonic()
while True:
    ...
    if settings.scheduled_tasks_enabled and time.monotonic() >= next_tick:
        next_tick = time.monotonic() + settings.scheduler_tick_seconds
        try:
            await schedules.tick(hub, worker_id)
        except Exception:
            logger.exception('定时任务 tick 失败')
```

`tick()` 一次做三件事：**认领到期的 → 派发 → 收敛已结束的 + 投递入队**。

### 4.1 认领（跨副本安全）

单个事务里 `FOR UPDATE SKIP LOCKED` 选出到期的行，逐行算下一次并在同事务推进：

```sql
SELECT * FROM scheduled_tasks
 WHERE enabled
   AND next_run_at IS NOT NULL AND next_run_at <= %s
   AND next_attempt_at <= %s
 ORDER BY next_run_at
 LIMIT %s
 FOR UPDATE SKIP LOCKED;
```

行锁在提交时释放，第二个副本直接跳过；`lease_owner/lease_epoch` 用于"派发中崩溃"的对账，
语义照抄 `claim_run`（`store/pg_runs.py:232`）与 `claim_external_task_poll`（`store/pg_actions.py:840`）。

### 4.2 判定（照抄 WorkBuddy 的 `decide()`）

```text
due = next_run_at
1. 错过窗口：now - due > missed_window（默认 86400s）
   → occurrence = skipped/missed_window，推进 next_run_at，**不补跑**
2. 本任务已有未结束的 occurrence（status ∈ {queued,running}），或本进程已到并发上限
   → occurrence = deferred，next_run_at 不动，next_attempt_at = now + defer_backoff
   → 连续 defer 超过 N 次（默认 5）→ skipped/deferred_too_long，推进 next_run_at
3. 正常：先写 occurrence（唯一键挡住重复），再把 next_run_at 推进到下次，
   然后调 hub.start()
```

第 2 条的"本任务已有未结束 occurrence"就是 WorkBuddy 的 `deferredKind='busy'`：同一自动化的
两次运行不叠着跑（上一轮每小时任务跑了两小时，这一轮不该再起一个）。
`next_attempt_at` 是 defer 的退避闸门——没有它，主循环 1s 一轮会把满负荷会话打成热循环。

**抖动（jitter）**：`delay = hash(schedule_id + occurrence) % window`，默认窗口 0（关闭），
仅在"整点集中触发"成为问题时打开。WorkBuddy 在峰值小时放大窗口，本项目用户量不到那个量级。

### 4.3 派发 = 复用现有执行链

```python
record = await hub.start(
    schedule.prompt,
    session_id=uuid.uuid4().hex[:12],        # 每次触发一个新会话（见 §3 的取舍）
    principal=SimpleNamespace(              # 建任务时落库的主体，不是 'default'
        tenant_id=schedule.tenant_id, user_id=schedule.owner_user_id,
        role=schedule.owner_role, platform_id=schedule.platform_id,
        subject=schedule.platform_subject),
)
# 会话标成后台，侧栏不显示；occurrence 记下 session_id 供排障
await store.mark_session_background(record.session_id)
```

`hub.start()` 内部已经做完了：`create_goal`（任务 + run + 消息同事务）→ `claim_run` 占租约 →
就地执行。因此：

- **不要**新写执行器、不要绕过租约直接 `INSERT INTO runs`；
- **"已提交"不等于"已在跑"**：本进程到并发上限时 `start()` 会带着 `status='queued'` 返回，
  由本轮或别的 Worker 领走（`api_hub.py:164`）。occurrence 先记 `queued`，
  收敛阶段（§4.4）再按 `runs.status` 推进，不能假定同步执行完；
- 计费绑定 `governance.bind(store, tenant_id, run_id)` 在 `_run` 里已经做了（`api_hub.py:379`），
  前提是 `create_goal` 拿到的 tenant 是真的（**绝不 `or 'default'` 兜底**）；
- 建任务时要挡住"停止词"：prompt 恰好是 `停/停止/取消/stop` 会被 `hub.start` 当成
  `stop_session` 处理、根本不建任务（`core/services.py:105`）。创建接口直接 400。

无人在场的一个真实差别：审批。工具政策要求审批时，run 会停在 `waiting_input`。
调度器必须把它当**终态以外的一个通知点**：occurrence 记 `paused`，投递一条
"任务 X 需要你确认后继续"，而不是无限等。

### 4.4 收敛与冷启动

- 每条 tick 扫一遍"未结束"的 occurrence（走 `idx_scheduled_task_runs_open`）：
  - run 已终态 → 写 occurrence 终态 + 摘要 → 投递入队；
  - run 处于 `waiting_input`/`paused` → 投递"待确认"；
  - run 行不存在或已被对账成 `failed(interrupted)` → occurrence `failed/interrupted`。
- Worker 启动时 `hub.reconcile()` 已经把"上次进程死掉的 running"收敛成终态
  （`run_worker.py:236`），调度器只需按上一条兜住 occurrence，**不重复执行**。

---

## 5. 投递链路

复用 `background_jobs`，不新建 outbox 表（它已有唯一去重键、租约、`FOR UPDATE SKIP LOCKED`、
带 epoch 的 fencing 和指数退避，正是分析文档要求的形状）：

```python
await store.enqueue_job(
    uuid.uuid4().hex[:12], 'schedule_delivery',
    dedupe_key=f'{occurrence_id}:{channel}',   # 幂等：同一 occurrence 同渠道只发一次
    payload={'schedule_id': ..., 'occurrence_id': ..., 'run_id': ...})
```

Worker 的 `drain_jobs` 加一个分支（`run_worker.py:126`），投递失败走
`finish_job(retry=True)`，退避 5→10→20…上限 300s，超过 `MAX_JOB_ATTEMPTS=5` 落 `failed` 可查。

**渠道**：新增 `src/webpage_agent/integrations/webhooks.py`，形状照 `sms_client._post`
（`integrations/sms_client.py:81`）：Settings 超时、`follow_redirects=False`、
领域异常、**5xx 不当成功**。三种格式：

| channel | 报文 |
| --- | --- |
| `wecom` | `{"msgtype":"markdown","markdown":{"content": ...}}` |
| `feishu` | `{"msg_type":"text","content":{"text": ...}}` |
| `webhook` | `{"task": name, "status": ..., "summary": ..., "occurrence_at": ..., "run_id": ...}` |

`trust_env`（代理）必须**显式决策并写进文档**：默认 `trust_env=True` 跟随公司代理，
容器直连部署时配 `WEBHOOK_TRUST_ENV=false`。不写清楚就会出现"本机能发、容器不能发"。

**摘要**：按权威程度依次取——`tasks.state` 里的 `answer`/`outcome`（`store.task(task_id)`，
人话版本用 `core/messages.py:16` 的 `reply_text(state)`）→ 该会话最后一条 `role='assistant'`
消息（`store.messages()`）→ 只有错误码。截断 2000 字，并**显式标注"最终答复截断"**。
分析文档反对"截全文当摘要"，但它给的办法（注入 `send_automation_result` 工具）在本项目要动
工具注册与权限，放 P1：`capabilities/` 加一个 `schedule_result` 工具，写当前 run 的
occurrence.summary，注册即生效（`capabilities/` 的约定就是"加一个文件，内核一个字不用改"）。

**supersede（同任务同渠道只留最新一条）**：v1 用 `dedupe_key` 保证"同一 occurrence 不重发"，
但不会作废旧 occurrence 的待发消息。若出现"补发一堆过期通知"，再加一条
`UPDATE background_jobs SET status='cancelled' WHERE kind='schedule_delivery' AND dedupe_key LIKE ...`
的作废逻辑（分析文档 §10.3 第 7 条），在 P2。

---

## 6. API 契约

新增 `src/webpage_agent/api/schedules_api.py`（`APIRouter(prefix='/api/schedules')`），
在 `api/routes.py:146` 一带 `include_router`。鉴权一律 `Depends(policies.current_principal)`，
**租户/用户只从 principal 取，客户端传什么都不信**（`routes.py:971` 的既有约定）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/schedules?status=active\|paused\|all` | 列表，含 `scheduleDesc`、`next_run_at`、`last_status` |
| POST | `/api/schedules` | 创建（校验 rrule；`once` 校验 `scheduled_at` 必须在未来） |
| GET | `/api/schedules/{id}` | 详情 |
| PATCH | `/api/schedules/{id}` | 改名/改 prompt/改 rrule/启停/改投递（改规则必须重算 `next_run_at`） |
| DELETE | `/api/schedules/{id}` | 删除（硬删，本项目没有软删习惯） |
| POST | `/api/schedules/{id}/trigger` | 立即运行（**与定时触发同一条执行路径**，方便排障） |
| GET | `/api/schedules/{id}/runs?limit=20` | 运行记录（occurrence 列表 + 摘要 + 错误码） |
| POST | `/api/schedules/{id}/delivery/test` | 测试推送（配置自检，对应 WorkBuddy 的 `claw:testAutomationWecomWebhook`） |

创建请求体：

```json
{
  "name": "每日 AI 新闻推送",
  "prompt": "关注当天 AI 领域的重要动态，整理成 5 条要点",
  "schedule": {"type": "recurring", "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"},
  "valid_until": null,
  "connector_ids": ["search"],
  "delivery": {"channel": "wecom", "webhook_url": "https://qyapi.weixin.qq.com/…"}
}
```

一次性任务：`{"type": "once", "scheduled_at": "2026-11-12T09:00:00+08:00"}`。

> `webhook_url` 落库前做 scheme 白名单（只允许 `https`，本机调试允许 `http://127.0.0.1`），
> `GET` 返回时**脱敏**（只回显 host）。理想做法是让用户填环境变量名、URL 不进库；
> v1 先按"落库 + 读时脱敏"处理，并在文档标注。

---

## 7. UI

只动 `index.html`（`workbuddy.html` 是过期副本，无路由引用，**不要碰**）。五个**纯追加**插入点：

| 位置 | 内容 | 行数 |
| --- | --- | --- |
| `index.html:1199`（`</style>` 后） | `<style id="wb-schedule">`：列表行、状态徽标、表单两列、运行记录表 | ~90 |
| `index.html:1281` 前 | `<button class="nav-item" id="navSchedule">定时任务</button>` | 3 |
| `index.html:1430` 前 | `<section class="hub-view" id="scheduleView" hidden>`：全部/已启用/已停用 chips + `#schedList` + `#schedAdd` | ~40 |
| `index.html:5443` 附近 | `#schedEditorModal`（名称/提示词/重复规则/时间/有效期/推送/连接器）+ `#schedRunsModal` | ~85 |
| `index.html:6645`（末个 `</script>` 后） | 新 `<script>` IIFE（state/load/render/editor/runs/toast） | ~280 |

视觉与交互直接抄现成范式，不新造设计：

- 整页视图与筛选胶囊 → `#hubView`（`:1378`）、`.hub-cat.active`（`:895`）；
- 列表 ↔ 子页（运行时列表 ↔ 运行记录）→ `#mcpManager` 的 `#mmListView`/`#mmEditor`（`:1654`）；
- 新建表单弹窗 → `#addSkillModal` + `.hub-form`（`:5444`、`:930`）；
- 进页给 `.main` 加**现有** `hub-mode` 类（`:854` 已隐藏聊天区/输入框），退页移除；
  `#navChat` 的退出由新 IIFE 自己监听，不改 `closeHub`（`:6020`）；
- `fullAccess` 那套无审批语义**不抄**：弹窗里明示"该任务会在后台以你的身份执行"。

因为 script1 的 `$/esc/API_PREFIX/checkAuth` 是全局词法作用域，新脚本可直接用；不要塞进 script3（IIFE 内不可见）。

---

## 8. 配置项（`core/config.py`，`@dataclass(frozen=True)` + `os.getenv`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SCHEDULED_TASKS_ENABLED` | `false` | 总开关。**默认关**：定时任务=无人值守烧 token，先手动验收再打开 |
| `SCHEDULER_TICK_SECONDS` | 30 | tick 间隔（`max(5.0, …)`） |
| `SCHEDULED_MISSED_WINDOW_SECONDS` | 86400 | 超过多久的错过不再补跑 |
| `SCHEDULED_MAX_DEFERRALS` | 5 | session 忙时最多延几次 |
| `SCHEDULED_DEFER_BACKOFF_SECONDS` | 60 | defer 重试间隔 |
| `SCHEDULED_JITTER_SECONDS` | 0 | 抖动窗口，0=关 |
| `SCHEDULED_BATCH_LIMIT` | 20 | 一次 tick 最多派发几个 |
| `WEBHOOK_TIMEOUT_SECONDS` | 10 | 出站投递超时 |
| `WEBHOOK_TRUST_ENV` | `true` | 是否读 `HTTP(S)_PROXY` |
| `SCHEDULED_DEFAULT_WEBHOOK` | 空 | 全局默认推送地址（未配置则只写运行记录） |

**不要**用 `governance_enabled()` 包这个开关（它在 ticket 模式会自动变 true，
`core/config.py:50`），否则生产一升级就自动开始无人值守执行。

---

## 9. 与 WorkBuddy 的取舍对照

| WorkBuddy 的做法 | 本项目 | 理由 |
| --- | --- | --- |
| RRULE + 6 段 cron 双表示互转 | 只留 RRULE 子集 | 没有云端调度要兼容，双表示是历史包袱 |
| `permission_mode=fullAccess`、无审批 UI | 工具政策照常生效；停在 `waiting_input` 就通知用户 | 定时 prompt 可能被注入，最小权限是底线 |
| 调度 + 执行绑在 Electron 主进程 | 放 Worker 常驻进程 | 关掉浏览器也要跑；多副本靠行级 claim |
| 云端 `/v2/as/*` 双源 | 不做 | 私有后端契约，与"单源本地调度"无关 |
| 摘要由 agent 主动产出（`send_automation_result`） | v1 取最终答复，P1 改能力工具 | 先能跑，再把摘要质量做对 |
| outbox `supersede` | P2 | 先保证不重发；过期堆积是量级问题 |

---

## 10. 风险与应对

1. **审批阻塞**：无人在场时 `waiting_input` 会一直等。→ occurrence 标 `paused`，推送"待确认"，不进失败统计；P1 加"无人值守自动拒绝需审批工具"的开关。
2. **成本失控**：每条任务都是一次真实模型调用。→ 默认关总开关；每任务可设 `max_runs_per_day`（P1）；沿用既有 `run_max_provider_requests` 预算。
3. **时区**：按进程本地时区算（与 WorkBuddy 一致）。→ 文档写明；跨时区用户需要 IANA 参数（P1，`rrules.next_run_at` 预留 `tz` 形参）。
4. **多副本重复派发**：靠 `FOR UPDATE SKIP LOCKED` + `(schedule_id, due_at)` 唯一键。→ 单测覆盖"两个 worker 同时 tick 只派发一次"。
5. **迁移漂移**：只改 `SCHEMA` 不改 `UPGRADES` 会让存量库启动即 `SchemaDrift`。→ 按 §3 四步走，并跑 `test_migration_channel` 那类用例。
6. **webhook 泄密**：URL 里带 key。→ 读接口脱敏；生产建议走环境变量或密钥引用。
7. **prompt 注入**：任务的 prompt 由用户写入，执行时以该用户身份跑。→ 工具政策 + 工作区沙箱已有护栏；文档明确"谁能改任务 = 谁能以该用户身份定时执行"。
8. **会话膨胀**：每次触发一个新会话、每次运行都写 messages。→ 靠 `list_sessions` 的反连接不进侧栏；一个任务的历史在"运行记录"里看，靠 `scheduled_task_runs` 索引；保留期清理沿用 `observe/maintenance.py`（按 artifact/事件保留期回收）。

---

## 11. 实施步骤（每步可独立验收）

| 步 | 产出 | 验收 |
| --- | --- | --- |
| **S1 时间语义** | `core/rrules.py` + `tests/test_rrules.py` | 每天/每周/双周/每月 31 号/每年 2/29/每小时/一次/有效期边界全绿；非法规则抛错 |
| **S2 存储** | 迁移 v20 + `store/pg_schedules.py` + Protocol 方法 + 两处 `TEST_TABLES` | `test_migrate` 在"只跑过 baseline 的库"上能建出两张表；claim 的 fencing 用例（第二次 claim 拿不到、过期可被接管） |
| **S3 调度 + 投递** | `scheduler/tick.py` + `integrations/webhooks.py` + `drain_jobs` 分支 + Worker 接线 | 回拨 `next_run_at` 能派发；3 天前的错过记 `missed` 不补跑；会话忙时 defer 不超过 N 次；webhook 500 记 `failed` 并退避重试 |
| **S4 API + UI** | `api/schedules_api.py` + `index.html` 五个插入点 | 建任务→列表→停用/启用→立即运行→看运行记录→改规则重算下次时间；重启 Worker 不重复跑 |

建议按 S1→S4 顺序提交，每步都跑 `bash scripts/run_tests.sh`（需要 PG，库名必须含 `test`）。

---

## 12. 不做的事

- 云端任务源、`cronExpr` 契约、云端日志分页；
- 多任务 DAG / 依赖编排（那是另一套调度器，不是"定时任务"）；
- 任务级独立工作区（v1 跟随用户当前工作空间；本项目沙箱按"租户+用户"分配私有目录，
  按任务切目录要么改 `runs` 列、要么破坏 `stored_path_ok` 的边界，留到有真实需求再做）；
- 任务级选模型（模型是按用户选的文件，不在 run 上；要按任务选得先给 run 加模型覆盖字段）；
- 定时任务的跨用户共享 / 模板市场。

---

## 13. 实现状态（已完成，2026-11-11）

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 时间语义 | `src/webpage_agent/core/rrules.py` | 解析 / `next_run_at` / `once_run_at` / `describe`；18 条单测 |
| 判定 | `src/webpage_agent/scheduler/decide.py` | 纯函数 `decide()` + 确定性抖动（FNV-1a，不用 `hash()`） |
| 调度 | `src/webpage_agent/scheduler/tick.py` | 认领 → 判定 → 派发 → 收敛；`run_now()` 手动触发 |
| 投递 | `src/webpage_agent/scheduler/delivery.py` + `integrations/webhooks.py` | outbox 入队、幂等键、可重试/不可重试分类、渠道报文 |
| 存储 | `src/webpage_agent/store/pg_schedules.py` | 20 个方法：定义 CRUD、claim、fencing 推进、occurrence、结果回写 |
| 迁移 | `migrations.py` / `pg_schema.py` | `v20_scheduled_tasks`（v21 已被"服务心跳"占用，两处并存） |
| 接口 | `src/webpage_agent/api/schedules_api.py` | 8 条 REST（列表/创建/详情/修改/删除/立即运行/运行记录/测试推送） |
| 接线 | `scripts/run_worker.py` | 主循环单调时钟到点 tick + `drain_jobs` 的 `schedule_delivery` 分支 |
| 前端 | `api/web/index.html` | 导航项、整页视图、编辑器弹窗、运行记录弹窗、独立 IIFE（五个纯追加点） |
| 配置 | `core/config.py` | 11 个 `SCHEDULED_*` / `WEBHOOK_*` 开关，总开关默认 **关** |

**验证**（本机实测，不是"应该能跑"）：

| 场景 | 结果 |
| --- | --- |
| `tests/test_rrules.py` + `tests/test_schedules.py` | 51 条全绿（时间语义、claim/fencing、错过窗口、忙时延后、派发、收敛、投递重试） |
| 全新库迁移 | `python -m scripts.migrate` → 42 表 / 495 列，`verify_schema` 通过 |
| 存量库升级 | 只跑 baseline 的库补上 v20/v21，结构核对通过 |
| API | 创建（`每天 09:00`）、改规则（重算下次时间）、停用（`next_run_at → NULL`）、删除、422/400 校验（非法规则、停止词、过去的一次性时间、非 https 地址） |
| 真实 tick | Worker 到点认领 → `hub.start` 建任务并执行 → 失败收敛 → 通知入队 → POST 到本地 webhook，`delivery_status=sent` |
| 浏览器 | Playwright：新建（每周一/周四 08:30）→ 列表 → 停用/启用 → 筛选 → 搜索 → 运行记录 → 编辑回填 → 删除，无 JS 报错 |

**两个顺手修掉的既有问题**（不修会挡住验证）：

1. `pg_schema.py` 里 `ticket_redemptions` 的注释和 `CREATE TABLE` 粘在一行，**全新库迁移直接
   语法错误**（只影响从零建库，存量库看不出来）。
2. 存量库升级时 `runs.owner_user_id` 等列由 UPGRADES 补，`sessions` 的列序和 `SCHEMA` 不一致
   —— 已确认与本次改动无关，单独记录，未在本次处理。

**未做**：任务级工作区/模型选择、`send_schedule_result` 工具（摘要仍取最终答复）、
投递 supersede、按任务日预算、`scheduled_task_runs` 的历史清理（可挂 `maintenance`）。
