# 中台改造验收清单

对照 `docs/agent-platform-design.md` 逐项说明实现状态。

**图例**：✅ 已实现且验证 ／ ⚠️ 已实现但**本环境无法验证** ／ ⬜ 未做（文档明确后置）

最后更新：随 `1a19e52` 之后的产物登记提交更新。

---

## 阶段 A：通用工具与计划合同

| 文档要求 | 状态 | 实现位置 | 验证方式 |
|---|---|---|---|
| 能力包拆分（web/reports/publishing/answers） | ✅ | `app/capabilities/` | `tools.py` 699→217 行；目录里 5 个包归属清晰 |
| 工具保留完整描述（§2 点名"应优先修复"） | ✅ | `mcp_adapter.list_server_tools` | 服务端 `title`/`description`/`annotations` 全保留 |
| 工具稳定身份与动作性质 | ✅ | `Tool.tool_key` / `Tool.effect` | `delete_site→destructive`、`list_sites→read` |
| task / run 分离 | ✅ | `tasks_meta` / `runs` | `test_finishing_a_run_persists_plan_and_steps` |
| 步骤 ID 与依赖 | ✅ | `PlanStep.step_id` / `depends_on` | `test_step_ids_assigned_and_dependencies_validated` 等 |
| 输入绑定（literal / ref） | ✅ | `nodes.resolve_inputs` | 只认受限 JSON Pointer，不做表达式求值 |
| 计划结构化校验 + 有限修复 | ✅ | `nodes.validate_plan` + `plan()` 两轮 | 未知工具/重复 ID/前向依赖都报错；**两轮仍有错误就停在 `waiting_input`，不执行残缺计划**（`test_bad_plan_stops_instead_of_executing`） |
| 移除研究型兜底与静默跳过 | ✅ | `nodes.fallback_steps` | 兜底只降级为直接作答 |
| 业务提示词归能力包 | ✅ | `Tool.prompt_rules` + `nodes.planner_system` | 内核只留通用规则，检索/成稿/作答/发布四段规则各自随能力包走；无部署工具时提示词里不出现发布规则（`test_planner_prompt_is_split_by_capability`） |
| 多 MCP Server 路由 | ✅ | `app/mcp_servers.py` + `mcp_adapter` 按 server 参数化 | `MCP_SERVERS` 配多个服务端；多服务端时工具名 `mcp__<id>__<tool>`、单服务端保持 `mcp__<tool>`；单服务端掉线不影响其他（`test_discovery_is_isolated_per_server`） |

## 阶段 B：可靠执行与恢复

**唯一执行路径**：就地执行（API 进程）与 Worker 走同一条 `claim_run`，都要先占租约。
`test_inline_execution_holds_a_lease_so_worker_cannot_steal` 守着这条——
以前 API 用 `create_run(status='running')` 却不写租约，Worker 会把正在跑的 run
当孤儿再领一次，同一任务执行两遍。

| 文档要求 | 状态 | 实现位置 | 验证方式 |
|---|---|---|---|
| 动作账本 + attempt | ✅ | `actions` / `action_attempts`、`app/ledger.py` | `test_succeeded_action_is_replayed_not_resent` |
| 崩溃窗口一（prepared 未发送） | ✅ | `reconcile_orphan_runs` 只处理 in_flight | `test_reconcile_marks_inflight_actions_unknown`（prepared 保持可重试） |
| 崩溃窗口二（已发送未回执） | ✅ | `ledger.begin` 返回 blocked | `test_unknown_action_blocks_instead_of_resending` |
| 崩溃窗口三（已成功未推进） | ✅ | 账本复用回执 | `test_succeeded_action_is_replayed_not_resent` |
| 暂停 / 取消 / 恢复 | ✅ | `Hub.pause` / `cancel_run` / `resume`、`/api/runs/{id}/*` | 取消落 `cancel_requested_at` 由执行者轮询处置；已结束的运行返回 409 而不是假装成功（`test_cross_process_cancel_writes_a_flag_and_is_honest`） |
| 断点续跑沿用已存计划 | ✅ | `plan()` 的 `resume_plan` 分支 + `Hub.resume` 回库 | 重启后仍可续跑，且带历史/上轮产物/记忆（`test_resume_works_after_restart`） |
| 补偿步骤（§8.4） | ✅ | `PlanStep.compensate` + `nodes._run_compensations` + `POST /api/runs/{id}/compensate` | 失败时逆序撤销已成功的写操作并记账（`test_failure_rolls_back_succeeded_writes_in_reverse`）；撤销失败报 `partially_succeeded`；结果未定的动作**不自动撤销**（`test_unknown_action_is_not_compensated_blindly`）；手工补偿新建独立 run，原 run 记 `compensated_at` |
| 取消与撤销分离 | ✅ | 取消只停后续步骤；撤销走独立补偿运行 | `Hub.compensate` 拒绝在运行中/已补偿的运行上执行 |
| 启动对账不误伤在跑的 run | ✅ | `reconcile_orphan_runs(owner=...)`、lifespan | 只处理自己名下或租约已过期的运行；无 in_flight 动作时回到 `queued` 等 Worker 续跑（`test_reconcile_spares_runs_held_by_another_instance`） |
| LangGraph 持久化检查点 | ⚠️ | `graph.checkpointer()` | **未安装 `langgraph-checkpoint-sqlite`**，当前退回 `InMemorySaver`；恢复能力由账本承担，不依赖检查点 |
| 有限重新规划（replan 节点） | ⬜ | — | 当前用"校验失败回喂修复"两轮代替独立 replan 节点；修不好即停机等补充 |

## 阶段 C：多用户运行底座

| 文档要求 | 状态 | 实现位置 | 验证方式 |
|---|---|---|---|
| 身份与对象权限缝 | ⚠️ | `app/policies.py` | 跨租户返回 404 有测试；**未接入真实认证**，请求头只当身份声明 |
| 对象权限覆盖全部接口 | ✅ | `app/api.py` 的 `_owned_task/_owned_session/_owned_run` | 任务/事件/产物/会话/记忆/画像全部按主体校验，`sessions` 与 `tasks_meta` 落 `owner_user_id`（`test_other_user_cannot_read_a_session`） |
| 创建任务请求幂等 | ✅ | `request_keys` 表 + `app/idempotency.py` + `Idempotency-Key` 头 | 同键同内容只建一个任务（`test_retry_with_same_key_returns_the_same_task`）；同键换内容 409；键按 主体+接口 作用域化、按 TTL 过期 |
| 画像按用户作用域 | ✅ | `profile.use()` + `users/<user>.md` | 两个用户的画像互不覆盖（`test_profile_is_scoped_per_user`） |
| 租约与 fencing | ✅ | `Store.claim_run`（`UPDATE … RETURNING`）／`PgStore.claim_run`（`FOR UPDATE SKIP LOCKED`） | `test_claim_is_exclusive_and_epoch_increments`；就地执行也占租约 |
| 执行期间续租 | ✅ | `Hub._guard` | 长任务不会因租约过期被另一个执行者接手 |
| 独立 Worker | ✅ | `scripts/run_worker.py` | 支持 `--once` / `--jobs-only`；SQLite 阶段限单 Worker |
| SSE 游标 | ✅ | `Last-Event-ID` / `?after=` | 实测 `after=2` 正确跳发；事件帧带 `id:` 行 |
| 事件序号原子自增 | ✅ | `append_event` 单语句 `INSERT … RETURNING seq`（PG 冲突回滚重算） | 多写入者不会再算出同一个 seq（旧实现会撞主键或被 SSE 去重静默丢弃） |
| outbox 与后台任务 | ✅ | `background_jobs` + `dedupe_key`，`drain_jobs` 为 async | `test_jobs_drain_is_idempotent`、`test_summary_job_actually_compacts_memory`、`test_failed_job_is_requeued_with_backoff` |
| 仓储接口层 | ✅ | `app/persistence.py`（Protocol） | `test_sqlite_store_satisfies_repository_protocol`；`check_repository` 能列出缺失方法 |
| PostgreSQL 迁移 | ✅ | `scripts/migrate_to_postgres.py` | 已在本地 PostgreSQL 18.6 实测：21 会话 / 139 消息 / 71 任务 / 1069 事件全部搬入，行数校验一致 |
| PostgresStore 实现 | ✅ | `app/pg_store.py` | 满足同一 Repository 接口；`PostgresParityTests` 在真实 PG 上跑通 6 组关键读写 |

## 阶段 D：上下文与长期记忆

| 文档要求 | 状态 | 实现位置 | 验证方式 |
|---|---|---|---|
| 按用途的完整预算 | ✅ | `context.build(purpose=...)` | `select()` / `chat_messages()` 都经由 `build()`，**生产路径就是这一套**；`BUDGETS` 由 `PURPOSE_BUDGETS` 派生，不再有两张表 |
| 截断必须可见 | ✅ | `context.build` 报告 + `_select_sources` | `test_history_is_trimmed_and_flagged`；截断说明**先拼进正文再构造 messages**（`test_truncation_note_reaches_the_model`） |
| 核心约束超预算不静默截断 | ✅ | `report['over_budget']` | `test_oversized_rules_are_reported_not_silently_cut` |
| 产物引用按需读取 | ✅ | `artifacts` 表 + `GET /api/artifacts/{id}` | 实测产物登记（kind/size/sha256）并可按引用读取 |
| 记忆作用域（user/project/tenant） | ✅ | `memories` 表 | `test_scopes_do_not_leak` |
| 记忆版本与替代 | ✅ | `Store.remember` | 同键写入升版本、旧值置 superseded |
| 遗忘与墓碑 | ✅ | `memory_tombstones` | `test_forget_blocks_regeneration_from_same_source` |

## 阶段 E：按实测扩展

⬜ 全部未做。文档明确要求"根据工具数量、积压和成本，再增加语义召回 / pgvector /
并行步骤 / 消息队列"，当前没有实测数据支撑，因此不做。

---

## 实测里程碑（文档 §16）

以"同一套内核完成一个 HTML 发布任务和一个完全不涉及网页的 MCP 任务"为验收：

| 任务 | 结果 |
|---|---|
| 「服务器上部署了几个站点，最早那个的访问地址是什么」 | `answered`，`documents=0`、无产物，正确给出站点与地址 |
| 「把 SQLite FTS5 的资料原样保存成网页」 | `local_ready`，产物登记 `art_*`，kind=sources、245823 字节、sha256 可查 |

一次会话后数据库状态：`runs: {succeeded: 2}`、`outbox 作业: 2`、
`GET /api/recovery → {"runs":0,"unknown_actions":0}`。

## 明确未验证清单（不假装完成）

1. ~~PostgreSQL 实机~~ **已补齐**：本机 PostgreSQL 18.6 上完成迁移与实测，
   见 `PostgresParityTests`（设置 `PG_TEST_DSN` 运行）。
2. **真实多用户登录**：没有身份提供方。`current_principal()` 只读请求头且不
   校验签名；生产必须替换为经过验证的认证依赖。
3. **LangGraph 检查点持久化**：缺 `langgraph-checkpoint-sqlite`，当前是内存实现。
   图位置在进程重启后丢失，恢复依赖 `runs/steps/actions` 账本。
4. **真实 MCP 服务端联调**：多 Server 的路由、命名、失败隔离都有用例覆盖，但
   本环境没有第二个真实 MCP 服务端，`deploy_tool` / 参数形态仍按原服务端契约。
5. **补偿的远端语义**：补偿依赖"存在对应的撤销工具"。服务端没有撤销工具时，
   补偿只能是空（计划里留空），系统不会假装撤回过。
6. **规模化**：连接池已落地（见下），但**没有**高可用承诺——多副本缺跨实例实时
   事件推送，池大小是静态配置，借不到连接直接报错。
7. **运维**：有结构化日志、`/api/health`（含连接池态势）与 `/api/recovery`，
   但**没有** Prometheus 指标出口、告警、历史数据清理策略。
8. **执行中重规划 / 并行步骤**：未实现。现有设计是"一次只走一步"，账本顺序、
   补偿逆序、事件序号都建立在这个前提上；动它要先重设计这三条不变量。

## 测试

```bash
python -m unittest tests.test_core                       # 219 通过（SQLite，无 PG 时跳过 10 条）

# PG_TEST_DSN 必须指向一次性测试库（库名需含 test），用例会 TRUNCATE 全部表
PG_TEST_DSN=postgresql://…/agent_platform_test python -m unittest \
    tests.test_core.PostgresParityTests tests.test_core.PostgresPoolTests
# 以上合计 229 通过：6 条仓储一致性 + 4 条连接池并发
```

## 本轮（2026-10-20 评审后）修复清单

对应 `docs/platform-review-2026-10-20.md` 的批次 1 与部分批次 2：

| 编号 | 问题 | 现在 |
|---|---|---|
| P0-1 | 双执行路径，Worker 会重跑 API 正在跑的任务 | 就地执行也走 `claim_run` 并写租约；`test_inline_execution_holds_a_lease_so_worker_cannot_steal` |
| P0-2 | 后台摘要作业 100% 失败（`asyncio.run` 在运行中的循环里） | `drain_jobs` 改 async；失败按 attempts 退避重排，不再一次终结 |
| P0-3 | 截断声明拼在 messages 之后，永远到不了模型 | 先拼 material 再建 messages |
| P0-4 | 两轮修不好仍执行残缺计划 | 停在 `waiting_input`，把校验错误原文交给用户 |
| P0-5 | `MAX(seq)+1` 与 `SELECT→UPDATE` 领取非原子 | `INSERT … RETURNING seq`；`UPDATE … RETURNING`（PG 加 `SKIP LOCKED`） |
| P0-6 | 重启后 `resume` 必 409、`reconciling` 无人接手 | `resume` 回库 + 占租约 + 带上历史/产物/记忆；`reconciling` 可被领取 |
| P0-7 | 导入即对账，误伤别的副本 | 对账移入 lifespan，只处理自己名下或租约过期的运行 |
| P0-8 | 归属校验只覆盖一半接口 | 全部接口按主体校验；`sessions` 落 `owner_user_id`；画像按用户分文件 |
| P1-1 | `context.build` 是死代码、两套预算表分叉 | `select`/`chat_messages` 都走 `build`；预算表合并 |
| P1-2 | 部署按钮绕过执行链 | 部署变成"建 run + 一步计划"走执行器（账本/事件/取消都生效） |
| P1-3 | 取消接口假装成功 | 返回 409；跨进程取消靠 `cancel_requested_at` |
| P1-4 | 续跑丢上下文 | 续跑带 history / prior / memory |
| P1-6 | 状态机两套词汇 | `TERMINAL_STATUS` 与 `RUN_STATUS_OF` 统一，补 waiting_* / partially_succeeded |
| P1-7 | 预算只记不限、硬编码 | `MAX_STEPS` / `TASK_TIMEOUT` / `GRAPH_RECURSION_LIMIT` / `RUN_LEASE_SECONDS` 全部可配 |
| P2-1 | 全仓没有 logging | `app/logging_setup.py`，hub/worker/api 关键路径打点 |
| P2-3 | 没有 lifespan，连接从不收尾 | `FastAPI(lifespan=...)`，退出时 `hub.close()` |
| 通用性 | 规划提示词写死网页业务 | `Tool.prompt_rules` 由能力包自带，规划器按候选工具拼接 |

**仍未做**（批次 3）：retention/GC、metrics 看板、并行步骤、多租户真实认证。

## 批次 2 补充（2026-10-20 第二轮）

评审报告里标为"批次 2/3 剩余"的三项已经落地：

| 编号 | 要求 | 现在 | 验证 |
|---|---|---|---|
| P1-8 | `POST /api/tasks` 幂等键 | `Idempotency-Key` 头或 body `idempotency_key`；`request_keys` 表按 `主体+接口+键` 占位，同键同内容返回原响应（`idempotent_replay: true`），同键换内容 409，TTL 过期可复用；建任务失败会撤销占位 | `IdempotencyTests`、`ApiIdempotencyTests`（共 9 条） |
| §8.4 | 补偿步骤 | 计划可给每步声明 `compensate`；步骤失败时逆序撤销已成功的写操作，补偿有自己的账本动作与事件；撤销失败 → `partially_succeeded`；手工撤销走 `POST /api/runs/{id}/compensate`（独立 run，原 run 记 `compensated_at`）；`ref` 绑定可把站点 ID 传给撤销工具 | `CompensationTests`（7 条） |
| §5.4 | 多 MCP Server 路由 | `MCP_SERVERS` 配置多个服务端（transport/url/token/stdio），并发发现、失败隔离；工具名带服务端前缀并记录 `server_id`；旧计划里的 `mcp__<tool>` 可按尾部唯一匹配、或按 `tool_key` 尾部回退 | `McpRoutingTests`（6 条） |
| §5.4 | **部署工具不再是一条旁路** | `deploy_site` 就是 `default` 服务端声明的那个工具：身份 `mcp:<server>:<tool>`，走统一 MCP 通道（同一份账本、同样进 `mcp_calls`、同样的结果未定判定），平台只挂适配器（调用前的开关/产物闸门 + 调用后的回执归一）。清单里只有它一份，`mcp__deploy_site` 旧名仍能解析；服务端掉线时留占位工具明确报错；`DEPLOY_ENABLED=false` 时它整体不在清单里，没法绕过闸门调原始工具 | `DeployGateTests`（9 条） |

| §4 | **能力包注册化：加能力 = 加一个文件** | `@tool(...)` 由能力包自己声明（`CAPABILITY` / `PROMPT_RULES` / 描述 / 参数 / effect / tool_key 都在包内），`app/capabilities/registry.py` 扫目录 import 即注册，内核 `app/tools.py` 只剩汇总，且**不再 import 任何业务包**。注册即校验（effect 白名单、参数必须是 object schema、`tool_key` 必须 `local:` 且含能力名），`tool_key` 与原手工表**逐字保持一致**（已写进账本，不能因换机制而改身份） | `CapabilityRegistryTests`（5 条，含"写一个模块就多一个工具、内核零改动"） |
| §9 | **PostgreSQL 连接池** | 换掉"单连接 + 一把 `threading.Lock`"（那是把吞吐锁死，不是安全）：`psycopg_pool` 池化，每次调用 `_lease()` 借一条，事务边界显式（提交/回滚/归还），池大小与等待超时用 `DB_POOL_SIZE`/`DB_POOL_TIMEOUT` 配，`/api/health` 暴露 `db_pool`。去掉进程锁后暴露了一个真缺陷：`append_event` 的序号竞争在 8 线程下会打爆 5 次重试 → 改用 `pg_advisory_xact_lock(hashtext(task_id))` 按任务串行化，冲突重算留作兜底 | `PostgresPoolTests`（4 条：8 线程并发 seq 连续不重复、读写混跑、失败回滚不吃连接、池状态可观测） |
| §7 | **终态词汇收口：状态机 / outcome / 展示标签三层分开** | `tasks.status` / `runs.status` 只存 11 个通用值（`queued` `running` `reconciling` `waiting_input` `waiting_approval` `succeeded` `partially_succeeded` `needs_reconcile` `failed` `cancelled` `interrupted`）；本轮产出写进 `state.outcome = {kind, produced[], effects[], answered, unverified}`；`answered` / `local_ready` / `deployed` / `deploy_needs_check` / `compensated` 降级成 `outcome.label()` 派生出来的展示标签，接口同时返回 `status`（标签）与 `run_status`（通用值）。工具自报的 `status` 只当阶段小结，不再写进 run 终态。历史数据用 `scripts.migrate_status_vocabulary` 迁移（生产库已跑：66 行）。越界状态由 `check_run_status` 记 error 并退化，不再像旧的 `RUN_STATUS_OF.get(status, status)` 那样静默写库 | `StatusVocabularyTests`（9 条） |

设计取舍写在这里，避免以后被"看起来更简单"的做法改回去：

- **补偿不做成"把 run 标成已撤销"**。撤销本身是有副作用的调用，必须有独立
  账本动作和事件流；标一个状态字段等于假装撤销过。
- **结果未定（unknown）的动作不自动补偿**。那等于基于猜测再发一次写请求；
  应保持 unknown，等人核实后再走手工补偿。
- **单服务端不强制改成带前缀的名字**。多服务端才加前缀，是为了让已保存的
  计划继续可用；`find_tool` 仍保留"去前缀后唯一匹配"的回退。
- **幂等只覆盖创建任务**。事件追加、部署、补偿都不收幂等键：它们已经有
  更精确的机制（事件序号、账本 action、`compensated_at`）。
- **状态机词汇不许出现业务词**。`answered` / `local_ready` / `deployed` 都只是
  展示标签：写进 `tasks.status` 就等于每加一类能力就要加一个状态值，而
  `RUN_STATUS_OF.get(status, status)` 那种兜底还会让"忘了映射"的新词静默落库。
  接第 N 个能力时，差异应当体现在 `outcome`（数据）与 `tool_key`（身份）上。
- **"结果待确认"不是失败**。写请求可能真的成功了、只是回执看不懂，所以
  `needs_reconcile` 是独立终态：既不谎称成功，也不报失败，而是要求核实。
