# 中台改造验收清单

对照 `docs/agent-platform-design.md` 逐项说明实现状态。

**图例**：✅ 已实现且验证 ／ ⚠️ 已实现但**本环境无法验证** ／ ⬜ 未做（文档明确后置）

最后更新：执行可靠性改造（`docs/harness-reliability-implementation-plan.md`
阶段 A–E）落地之后。那一轮把 Worker 领取、租约代次、计划先行提交、
逐步快照恢复、类型化执行确定性与暂停边界全部补齐，并新增 4 个测试文件。

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
| 动作账本 + attempt | ✅ | `actions` / `action_attempts`、`app/ledger.py`（只有 `admit` / `settle` 两个写操作，各一个事务） | `ActionLedgerTests`（6 条）、`test_plan_is_committed_before_the_first_tool_call` |
| 崩溃窗口一（prepared 未发送） | ✅ | `store.admit_action` 对已存在的 `prepared` 行**重新准入并新建 attempt** | `test_prepared_action_is_retried_with_the_same_identity`（沿同一 action_id 重做） |
| 崩溃窗口二（已发送未回执） | ✅ | 账本 `admit` 返回 blocked → 步骤 `unknown`、run `needs_reconcile` | `test_unknown_action_blocks_a_blind_resend`、`test_timeout_is_unknown_and_blocks_a_resend` |
| 崩溃窗口三（已成功未推进） | ✅ | 账本复用回执；全部步骤已结算时直接用快照/回执收尾，不跑图 | `test_succeeded_action_is_reused_after_a_crash`、`test_succeeded_action_is_replayed_not_resent` |
| 计划先于执行提交 | ✅ | `main.run_task` 收到 `plan` 节点更新即 `commit_plan`（plan + steps + 初始快照，一个事务） | `test_plan_commit_failure_means_zero_remote_calls`（提交失败 → 远端调用计数为 0） |
| 逐步快照 | ✅ | `run_snapshots` + `settle_action` 同事务写入（含 `next_step_id`） | `test_commit_failure_after_the_tool_returns_does_not_advance`（结算失败不推进游标） |
| Worker 单一路径 | ✅ | `Hub.execute_claimed_run` 是唯一执行入口；`scripts/run_worker.run_once` 只负责领取与护栏 | `test_worker_claims_and_executes_a_run_once`（上下文 owner/epoch 是库里的真实值）、`test_worker_has_no_unawaited_coroutines`（`-W error::RuntimeWarning`） |
| 执行权代次（fencing） | ✅ | `ExecutionContext` + `_owned()` 条件校验，`renew_lease(epoch=)` / `release_lease(epoch=)` / `finish_run_owned` | `test_stale_epoch_cannot_advance_the_run`（准入/结算/终态/续租全被拒）、`test_renewal_failure_stops_before_the_next_step` |
| 暂停 / 取消 / 恢复 | ✅ | `Hub.pause` 返回 `{ok,status,pause_requested}`；`paused` 是独立终态；`resume` 只接受 `paused/interrupted/queued` 且存在未知动作时拒绝 | `test_pause_committed_before_admission_blocks_the_call`、`test_pause_after_admission_only_finishes_the_current_step`、`test_resume_after_pause_continues_from_the_saved_plan`、`test_resume_refuses_a_finished_run` |
| 断点续跑沿用已存计划 | ✅ | `plan()` 的 `resume_plan` 分支 + `Hub.resume` 回库 | 重启后仍可续跑，且带历史/上轮产物/记忆（`test_resume_works_after_restart`） |
| 补偿步骤（§8.4） | ✅ | `PlanStep.compensate` + `nodes._run_compensations` + `POST /api/runs/{id}/compensate` | 失败时逆序撤销已成功的写操作并记账（`test_failure_rolls_back_succeeded_writes_in_reverse`）；撤销失败报 `partially_succeeded`；结果未定的动作**不自动撤销**（`test_unknown_action_is_not_compensated_blindly`）；手工补偿新建独立 run，原 run 记 `compensated_at` |
| 取消与撤销分离 | ✅ | 取消只停后续步骤；撤销走独立补偿运行 | `Hub.compensate` 拒绝在运行中/已补偿的运行上执行 |
| 启动对账不误伤在跑的 run | ✅ | `reconcile_orphan_runs(owner=...)`、lifespan | 只处理自己名下或租约已过期的运行；无 in_flight 动作时回到 `queued` 等 Worker 续跑（`test_reconcile_spares_runs_held_by_another_instance`） |
| LangGraph 持久化检查点 | ✅（有意不用） | `graph.checkpointer()` 返回 `None` | 图重放会绕开动作账本、把远端调用再发一遍；恢复的唯一依据是 plan / run_snapshots / actions / steps |
| 有限重新规划（replan 节点） | ⬜ | — | 当前用"校验失败回喂修复"两轮代替独立 replan 节点；修不好即停机等补充 |

## 阶段 C：多用户运行底座

| 文档要求 | 状态 | 实现位置 | 验证方式 |
|---|---|---|---|
| 身份与对象权限缝 | ⚠️ | `app/policies.py` | 跨租户返回 404 有测试；**未接入真实认证**，请求头只当身份声明 |
| 对象权限覆盖全部接口 | ✅ | `app/api.py` 的 `_owned_task/_owned_session/_owned_run` | 任务/事件/产物/会话/记忆/画像全部按主体校验，`sessions` 与 `tasks_meta` 落 `owner_user_id`（`test_other_user_cannot_read_a_session`） |
| 创建任务请求幂等 | ✅ | `request_keys` 表 + `app/idempotency.py` + `Idempotency-Key` 头 | 同键同内容只建一个任务（`test_retry_with_same_key_returns_the_same_task`）；同键换内容 409；键按 主体+接口 作用域化、按 TTL 过期 |
| 画像按用户作用域 | ✅ | `profile.use()` + `users/<user>.md` | 两个用户的画像互不覆盖（`test_profile_is_scoped_per_user`） |
| 租约与 fencing | ✅ | `PgStore.claim_run`（`FOR UPDATE SKIP LOCKED`，`lease_epoch+1`） | `test_claim_is_exclusive_and_epoch_increments`、`test_two_workers_claiming_at_once_execute_once`；就地执行也占租约 |
| 执行期间续租 | ✅ | `Hub._guard`（**带 epoch 续租**，续租失败即停派并标记失租） | `test_renewal_failure_stops_before_the_next_step` |
| 独立 Worker | ✅ | `scripts/run_worker.py` | 支持 `--once` / `--jobs-only`；`test_execution_error_does_not_kill_the_worker`（一条失败不带走循环） |
| SSE 游标 | ✅ | `Last-Event-ID` / `?after=` | 实测 `after=2` 正确跳发；事件帧带 `id:` 行 |
| 事件序号原子自增 | ✅ | `append_event` 单语句 `INSERT … RETURNING seq`（PG 冲突回滚重算） | 多写入者不会再算出同一个 seq（旧实现会撞主键或被 SSE 去重静默丢弃） |
| outbox 与后台任务 | ✅ | `background_jobs` + `dedupe_key`，`drain_jobs` 为 async | `test_jobs_drain_is_idempotent`、`test_summary_job_actually_compacts_memory`、`test_failed_job_is_requeued_with_backoff` |
| 仓储接口层 | ✅ | `app/persistence.py`（Protocol + 全 `async def`）；SQLite 实现已删除 | `AsyncDriverTests`（协议异步、实现是协程函数、PG 用异步池）、`check_repository()` |
| PostgreSQL 迁移 | ✅ | `scripts/migrate_to_postgres.py`（历史 SQLite → PG 的一次性脚本，实现已删、脚本保留） | 已在本地 PostgreSQL 18.6 实测：21 会话 / 139 消息 / 71 任务 / 1069 事件全部搬入，行数校验一致 |
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

### 执行可靠性改造的实机验证（2026-10-23）

全部走真实 HTTP + 真实模型 + 真实 PostgreSQL（`agent_platform`），核对库里的行：

| 动作 | 实机结果 |
|---|---|
| `POST /api/tasks`「你好」 | `answered` / `succeeded`；`runs.lease_owner` 已清空、`lease_epoch=1`；`plans` + `steps(s1 local:answers:direct succeeded)` + `actions(status=succeeded, effect=read, receipt 带回 answer)` + `run_snapshots(720 字节)` 齐备 |
| 把该 run 置 `paused` 后 `POST /api/runs/{id}/resume` | 领取代次到 **2**；日志出现"按快照收尾（步骤已全部结算）"；状态回 `succeeded`，**动作数仍为 1**（没有重发任何调用） |
| 研究类任务 `POST /api/runs/{id}/pause` | 接口如实返回 `{ok:true, status:"running", pause_requested:true}`（还在跑）；十余秒后 run 落 `paused`、租约交还、`run_snapshots.next_step_id=s1`、`step_index=0` |
| 紧接 `POST /api/runs/{id}/resume` | 代次到 2，从第 1 步继续跑完：`steps(s1 web:search, s2 files:write)` 都 succeeded，终态 `succeeded`，产物是 `workspace/postgresql-连接池要点报告.md`（outcome.kind=artifact, effects.verified=true） |
| 执行中 `POST /api/tasks/{id}/cancel` | run 落 `cancelled`、`cancel_requested_at` 有值、租约交还、**零动作**（取消发生在规划阶段，没有产生任何调用） |

## 明确未验证清单（不假装完成）

1. ~~PostgreSQL 实机~~ **已补齐**：本机 PostgreSQL 18.6 上完成迁移与实测，
   见 `PostgresParityTests`（设置 `PG_TEST_DSN` 运行）。
2. **真实多用户登录**：没有身份提供方。`current_principal()` 只读请求头且不
   校验签名；生产必须替换为经过验证的认证依赖。
3. ~~LangGraph 检查点持久化~~ **有意不做**：`checkpointer()` 明确返回 `None`。
   图重放会绕开动作账本，把远端写请求再发一遍；恢复只认 plan / run_snapshots /
   actions / steps。要用检查点，必须先证明它的恢复路径也走账本。
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

## 执行可靠性改造（计划 A–E）验收

`docs/harness-reliability-implementation-plan.md` 的五个阶段与验收矩阵落点。
用例全部跑在**真实 PostgreSQL** 上，远端与模型是受控假实现（不联网、不花钱），
时点用 `asyncio.Event` 固定而不是 sleep 猜时序。

| 阶段 | 交付 | 用例 |
|---|---|---|
| A Worker 解阻 | `execute_claimed_run` 单一入口；`run_once` 只领取 + 护栏；漏 await 的问题消灭 | `test_worker_execution.py`（6，含一条**子进程**启动/退出）：领取执行一次、两 Worker 抢同一条、异常不带走循环、空计划不是续跑、无未 await 协程 |
| B 租约与控制底座 | `ExecutionContext`（run_id/plan_version/owner/epoch）+ `_owned()` 条件提交；续租/释放/终态都带 epoch | `test_execution_control.py`（9）：旧代次准入/结算/终态/续租全被拒、续租失败停派、暂停三种时序、取消、工具消失 |
| C 数据库恢复 | 计划先于调用提交（`commit_plan` 一个事务）；`settle_action` 同事务写快照；恢复算法按动作推断起点；全部结算完只做收尾 | `test_execution_recovery.py`（7） |
| D 未知结果 | `app/errors.py` 类型化确定性（`not_executed` / `failed_confirmed` / `unknown`），适配器在最了解现场的一层标注 | `test_mcp_uncertainty.py`（6） |
| E 暂停与收尾 | `paused` 独立终态；`pause` 返回库里的真实结果；暂停快照可恢复；`resume` 只接受停下状态且拒绝未知动作 | `test_execution_control.py` 的暂停组、`test_mcp_uncertainty.py::test_resume_refuses_while_a_result_is_unknown` |

**诚实说明本环境没覆盖到的**：

- **没有做"子进程真被杀掉"的矩阵**。杀进程的每种窗口都用"往库里写它留下的行"
  来构造（`prepared` / `in_flight` / `unknown` / `succeeded` 四种），走的仍是
  真实执行入口。这能验证恢复逻辑，但**不能**证明"内核在任意指令处被 kill 之后
  库状态一定落在这四种之一"——那需要反复 kill 的模糊测试。
- **没有多进程多副本长时间混跑**。fencing 是被单进程内两个 Hub（各自的连接池）
  验证的；跨机器时钟、网络分区不在覆盖范围。
- **没有真实 MCP 服务端参与这组用例**，远端是假实现。业务失败/超时/连接错误的
  判定走的是真实适配器代码路径（`_make_mcp_runner` → `MCPToolFailure` →
  `ToolError` → 账本），但没有真实网络。
- **暂停的响应时延没有 SLO**：执行者只在"下一步准入前"检查暂停，一个很长的工具
  调用期间不会被打断（这是有意的：中途打断会留下结果未定的写操作）。

## 测试

```bash
# PG_TEST_DSN 必须指向一次性测试库（库名含 test），用例会 TRUNCATE 全部业务表
PG_TEST_DSN=postgresql://…/agent_platform_test \
  python -W error::RuntimeWarning -m unittest discover -s tests -v
# 实测：Ran 336 tests, OK（22 秒）
```

`-W error::RuntimeWarning` 是刻意的：漏 `await` 不会报错，只会静默失效（把协程
对象当值传下去，既不生效也不抛），只有把它提升成错误才能守住"Worker 真的在
等结果"。这条检查从阶段 A 起进入验收命令。

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
| §10 | **登录：平台签票 → 中台换会话** | `app/auth_connections.py`（可信平台登记表：issuer/audience/算法/公钥/kid/企业与平台白名单/身份源）、`app/ticket_auth.py`（票据→可信身份，固定 RS256、拒绝 alg=none 与 HS256 混淆、`(iss,jti)` 唯一、有效期与时钟误差、越权企业/平台拒绝）、`app/auth.py`（兑换=验票+一次性登记+绑定+建会话，同一事务；会话凭证只存 SHA-256 摘要）、`/api/auth/exchange`、`/api/me`、`DELETE /api/auth/session`。身份唯一关系改为**企业+身份源+外部用户 → 中台 user_id**（老数据按 provider 回填，中台 ID 不变）。`AUTH_MODE=ticket` 时业务接口只认会话，改请求头无效；退出撤销会话，SSE 周期复查。 | `tests/test_ticket_auth.py`（29）、`tests/test_auth_exchange.py`（18）、`tests/test_auth_isolation.py`（9）；实测两个模拟平台、三个用户走真实 HTTP |
| §9 | **仓储接口全异步（psycopg 异步驱动）** | `Repository` 协议全部 `async def`；PG 侧换 `AsyncConnectionPool`，等 DB 时事件循环不被阻塞；SQLite 侧用 `asyncio.to_thread` 提供同一套异步接口（它没有官方异步驱动）。`check_repository()` 现在同时校验"方法齐备"与"实现是协程函数"。启动路径（API lifespan / Worker）先 `await store.ready()`：连不上库在启动阶段就失败，而不是等第一个请求。构造不碰 IO（池以 `open=False` 创建） | `AsyncDriverTests`（4 条：协议异步、两实现异步、PG 用异步池、写库期间事件循环不阻塞）；实测 5 个任务并发跑完，池涨到 4、每个任务 `events.seq` 连续 |
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
