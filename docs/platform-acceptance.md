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
| 计划结构化校验 + 有限修复 | ✅ | `nodes.validate_plan` + `plan()` 两轮 | 未知工具/重复 ID/前向依赖都报错 |
| 移除研究型兜底与静默跳过 | ✅ | `nodes.fallback_steps` | 兜底只降级为直接作答 |
| 多 MCP Server 路由 | ⬜ | — | 当前配置仍是单 Server（`MCP_SERVER_ID`），接入第二个需要 Server 配置表 |

## 阶段 B：可靠执行与恢复

| 文档要求 | 状态 | 实现位置 | 验证方式 |
|---|---|---|---|
| 动作账本 + attempt | ✅ | `actions` / `action_attempts`、`app/ledger.py` | `test_succeeded_action_is_replayed_not_resent` |
| 崩溃窗口一（prepared 未发送） | ✅ | `reconcile_orphan_runs` 只处理 in_flight | `test_reconcile_marks_inflight_actions_unknown`（prepared 保持可重试） |
| 崩溃窗口二（已发送未回执） | ✅ | `ledger.begin` 返回 blocked | `test_unknown_action_blocks_instead_of_resending` |
| 崩溃窗口三（已成功未推进） | ✅ | 账本复用回执 | `test_succeeded_action_is_replayed_not_resent` |
| 暂停 / 取消 / 恢复 | ✅ | `Hub.pause/cancel/resume`、`/api/runs/{id}/*` | `test_pause_only_affects_active_runs`、`test_resume_reuses_saved_plan` |
| 断点续跑沿用已存计划 | ✅ | `plan()` 的 `resume_plan` 分支 | 续跑不重新规划，已成功动作不重发 |
| LangGraph 持久化检查点 | ⚠️ | `graph.checkpointer()` | **未安装 `langgraph-checkpoint-sqlite`**，当前退回 `InMemorySaver`；恢复能力由账本承担，不依赖检查点 |
| 有限重新规划（replan 节点） | ⬜ | — | 当前用"校验失败回喂修复"两轮代替独立 replan 节点 |

## 阶段 C：多用户运行底座

| 文档要求 | 状态 | 实现位置 | 验证方式 |
|---|---|---|---|
| 身份与对象权限缝 | ⚠️ | `app/policies.py` | 跨租户返回 404 有测试；**未接入真实认证**，请求头只当身份声明 |
| 租约与 fencing | ✅ | `Store.claim_run`、`lease_epoch` | `test_claim_is_exclusive_and_epoch_increments` |
| 独立 Worker | ✅ | `scripts/run_worker.py` | 支持 `--once` / `--jobs-only`；SQLite 阶段限单 Worker |
| SSE 游标 | ✅ | `Last-Event-ID` / `?after=` | 实测 `after=2` 正确跳发；事件帧带 `id:` 行 |
| outbox 与后台任务 | ✅ | `background_jobs` + `dedupe_key` | `test_jobs_are_deduped`、Worker 消费 |
| 仓储接口层 | ✅ | `app/persistence.py`（Protocol） | `test_sqlite_store_satisfies_repository_protocol`；`check_repository` 能列出缺失方法 |
| PostgreSQL 迁移 | ⚠️ | `scripts/migrate_to_postgres.py` | DDL 翻译有单测；**未在真实 PostgreSQL 上验证**，文件头列出已知差异 |
| PostgresStore 实现 | ⬜ | — | 本机无 PG 实例，写一个跑不起来的实现只是制造"看起来支持了"的假象，故不写 |

## 阶段 D：上下文与长期记忆

| 文档要求 | 状态 | 实现位置 | 验证方式 |
|---|---|---|---|
| 按用途的完整预算 | ✅ | `context.build(purpose=...)` | 6 种用途各有预算与报告 |
| 截断必须可见 | ✅ | `context.build` 报告 + `_select_sources` | `test_history_is_trimmed_and_flagged`；联网作答会写明"原文 X 字仅取前 Y" |
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

1. **PostgreSQL 实机**：本机无 PG 实例。迁移脚本只做了 DDL 翻译的单元测试，
   搬数据与行数校验逻辑未经真实运行。
2. **真实多用户登录**：没有身份提供方。`current_principal()` 只读请求头且不
   校验签名；生产必须替换为经过验证的认证依赖。
3. **LangGraph 检查点持久化**：缺 `langgraph-checkpoint-sqlite`，当前是内存实现。
   图位置在进程重启后丢失，恢复依赖 `runs/steps/actions` 账本。
4. **多 MCP Server 路由**：只有一个 Server 配置，同名工具跨 Server 的去重与
   路由未实现。
5. **补偿步骤（§8.4）**：取消不撤销已完成动作，撤销需要独立补偿步骤，未实现。

## 测试

`python -m unittest tests.test_core` → **170 个通过**。
