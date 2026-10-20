# 通用智能体中台 · 代码评审（2026-10-20）

> **修复状态**：本报告的 P0 全部 8 条 + P1-1/2/3/4/6/7 + P2-1/3 + 通用性提示词分包  
> 已落地；**批次 2 三项也已补齐**：P1-8 创建任务幂等键、补偿步骤（§8.4）、
> 多 MCP Server 路由。逐条对照见 `docs/platform-acceptance.md` 末两节。  
> 仍未做：retention/GC、metrics 看板（批次 3）。测试 176 → 189 → 211 条，全绿。

评审对象：`webpage-agent-demo/`（176 个用例全绿，4.0s；PG 用例 6 个 skip）  
评审方式：通读 `app/` 全部 31 个模块 + `scripts/` + `tests/test_core.py`，逐条对照  
`docs/agent-platform-design.md` 与 `docs/platform-acceptance.md`，并实际跑测试。

---

## 结论

中台的**骨架是立得住的**：task/run 分离、计划合同（step_id / depends_on / literal-ref）、  
动作账本三个崩溃窗口、租约与 fencing、SSE 游标、仓储接口 + PG 实现，这些不是"演示级"的  
东西，方向对，抽象也对。

问题集中&#x5728;**"验收清单已经打勾，但生产路径没接上"**：两套执行路径并存（API 就地跑 vs  
Worker 队列）、两套上下文预算（一套只被测试调用）、续跑在最需要它的场景（重启后）不可用、  
以及多进程下的领取与事件序号不是原子的。这些是会**造成重复外部副作用**的问题，优先级高于  
任何功能扩展。

下面按 P0（正确性）/ P1（架构一致性）/ P2（可运维）分级，每条给证据位置与修法。

---

## P0 · 正确性缺陷

### P0-1 双执行路径：Worker 会领走 API 正在跑的 run，导致同一任务执行两次

**证据**

- `app/api_hub.py:207` — `create_run(..., status='running')`，随后 `asyncio.create_task(self._run(...))`  
  就地执行，**从不写 `lease_owner` / `lease_expires_at`**。
- `app/store.py:496-511`、`app/pg_store.py:497-512` — 领取条件：  
  `status IN ('queued','running') AND (lease_expires_at IS NULL OR lease_expires_at < now)`。  
  租约为 NULL 的 running 行**完全命中**。
- `scripts/run_worker.py:48-59` — Worker 进程里 `hub.tasks` 没有该记录 → `hub.get()` 从库里  
  恢复 → `create_task(hub._run(..., resume_plan=steps))`，**再跑一遍**。

**影响**：部署类写操作重复执行（`deploy_site` 无幂等键，会真的建出第二个站点）；模型费用  
翻倍；两条进程各自向同一 task 追加事件，seq 交叉、SSE 端按 seq 去重会丢事件。只要按 README  
同时起 API 与 `run_worker`，这个问题就存在。

**修法**（二选一，建议后者）

1. `create_run` 时顺手写入 `lease_owner='api:<pid>'` + 未来 10 分钟的 `lease_expires_at`，  
   让 claim 领不到；
2. 彻底走队列：`Hub.start` 只 `create_run(status='queued')` 就返回，由 Worker 领取执行。  
   这才是文档 §9 的本意（"避免请求进程同时承担上百秒的长任务"）。API 进程只保留  
   `Hub.stop_session` 这类轻量控制。

---

### P0-2 后台摘要作业 100% 失败，且失败被静默吞掉

**证据**：`scripts/run_worker.py:79-91`，`drain_jobs()` 是同步函数，从 `async main_loop`  
里被直接调用（此时事件循环正在运行），第 85 行 `asyncio.run(...)` 必然抛  
`RuntimeError: asyncio.run() cannot be called from a running event loop`，被  
`except Exception` 捕获后 `finish_job(error='RuntimeError')`。

**影响**：outbox 里的 `session_summary` 从来没成功过，而且失败即"完成"，不会重试；  
与此同时 `app/api_hub.py:266` 仍在线压缩一次 —— 等于"既没挪到后台，后台那条还是坏的"。

**修法**：`drain_jobs` 改 `async def`，`await memory_module.compact(...)`；失败要  
`retry_count += 1` 并支持退避，不能一次失败就终结。补一条 Worker 用例（现有  
`WorkerTests` 没有覆盖 jobs 路径）。

---

### P0-3 「截断声明」永远到不了模型

**证据**：`app/capabilities/answers.py:86-94`

```python
messages = context.chat_messages(..., f'问题：{question}\n\n资料：\n{material}', ...)  # 86-88
truncated_notes = [...]                                    # 89-91
if truncated_notes:
    material += '\n\n[注意] ...'                            # 92-94  ← 拼在 messages 之后
```

**影响**：模型把"前 3000 字"当全文，输出"资料已完整覆盖"，而这正是文档 §11 点名要避免的  
失败模式。验收清单里"联网作答会写明原文 X 字仅取前 Y"这一条，与实际行为不符。

**修法**：把 note 拼进 `material` 之后再构造 `messages`；加一条断言  
`messages[-1]['content']` 含 `仅取前` 的测试。

---

### P0-4 两轮修不好，仍然执行一份残缺计划

**证据**：`app/nodes.py:215-253`。循环两轮后若 `issues` 仍非空，只判断 `if not steps:`  
——而 `validate_plan` 是把合法步骤**过滤后返回**的，`steps` 通常非空，于是带着已知错误  
进入执行。被丢弃步骤的 `step_id` 仍可能被后续步骤 `ref` 引用，`resolve_inputs` 解析成  
`None` 直接传给工具。

**影响**：与文档 §6.3「不能像当前 `_valid_steps` 一样丢掉未知工具后直接执行剩余步骤」  
直接冲突；验收清单标 ✅，实际是"部分执行 + 静默降级"。

**修法**：`issues` 非空 → 停在 `waiting_input`（或 `failed`），把 issues 原文给用户；  
要自动恢复就补真正的 `replan` 节点，而不是复用同一个"回喂修复"。

---

### P0-5 多进程下事件序号与领取都不是原子的

**证据**

- `app/store.py:409-421`、`app/pg_store.py:410-422`：`SELECT MAX(seq)+1` 再 `INSERT`。  
  PG 版同样如此，且用的是单连接 + 进程内锁（跨进程无效）。
- `app/pg_store.py:497-512`：`SELECT run_id` 再 `UPDATE`，**没有  
  `FOR UPDATE SKIP LOCKED`**（文档 §9.2 明确要求）。

**影响**：两个 Worker 并发 → 同一 `(task_id, seq)` 两条事件；SSE 消费端按 seq 去重，  
后到的那条被**静默丢弃**。同一 run 也可能被两个 Worker 同时领到，回到 P0-1 的重复执行。

**修法**：events 加 `UNIQUE(task_id, seq)`，seq 用 `runs.event_seq` 原子自增  
（`UPDATE ... RETURNING`）；claim 改 `UPDATE runs SET ... WHERE run_id = (SELECT ...
FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`。现测 `test_claim_is_exclusive_and_epoch_increments`  
只覆盖了单进程，需要在两个连接上各领一次才有意义。

---

### P0-6 续跑在最需要的场景不可用

**证据**

- `app/api.py:211-216` → `Hub.resume`（`app/api_hub.py:378-397`）第一句就是  
  `record = self.tasks.get(run['task_id'])`，**没有回库兜底**（`Hub.get` 有）。
- 进程重启后 `self.tasks` 为空 → 返回 `None` → 接口 409「没有可续跑的计划」。
- 启动对账把 run 置为 `reconciling`（`app/store.py:894`），而 `claim_run` 只领  
  `queued`/`running` → **没有任何路径会自动接手**。

**影响**：崩溃恢复链路名义上存在，实际上"重启后既不能 API 续跑，也不会被 Worker 捡起"，  
只能人工改库。这与验收清单里 ✅ 的"断点续跑"不相称。

**修法**：`resume` 用 `hub.get()`（回库）；恢复扫描器按 `status='reconciling' AND
lease_expires_at IS NULL` 取出，先按账本核实（succeeded 复用 / unknown 挂起等人确认 /  
prepared 重试）再决定续跑。`reconciling` 应可被领取，或单独用一个 `recovery` 状态。

---

### P0-7 导入即对账，会误伤正在运行的 run（多副本/--reload 场景）

**证据**：`app/api.py:36` 模块级 `RECOVERY = hub.reconcile()`；  
`app/store.py:864-899` 只按 `status='running'` 取，**不看租约归属与是否过期**，  
一律 `in_flight → unknown`、`run → reconciling` 且清空 `lease_owner/lease_expires_at`。

**影响**：`uvicorn --reload` 一次、或起第二个副本，就会把别的进程正在跑的任务判成  
"结果未知"并清掉租约，于是立刻可被再次领取 → 又一次重复执行；用户还会看到一条假的  
"结果不确定，需要核实"。

**修法**：对账移到 `lifespan`；只处理 `lease_owner = 自己` 或 `lease_expires_at < now`  
的 run；`unknown` 只给真正 in_flight 且租约过期的那部分。

---

### P0-8 归属校验只覆盖了一半接口，会话 ID 可被冒用

**证据**：带 `principal` 的只有 `/api/runs/{id}`、`/api/artifacts/{id}`。以下全部无校验：

| 接口                                         | 位置               | 后果                     |
| ------------------------------------------ | ---------------- | ---------------------- |
| `POST /api/tasks`                          | `api.py:68`      | 可向任意 `session_id` 追加消息 |
| `GET /api/tasks/{id}`                      | `api.py:75`      | 读任意任务 state（含正文元信息）    |
| `GET /api/tasks/{id}/events`               | `api.py:90`      | 订阅任意任务全过程              |
| `GET /api/tasks/{id}/artifact`             | `api.py:170`     | 下载任意产物                 |
| `GET /api/sessions/{id}/messages`          | `api.py:307`     | **读任意会话全文**            |
| `/api/sessions`                            | `api.py:301`     | 列出全部会话标题               |
| `/api/profile`、`/api/sessions/{id}/memory` | `api.py:254-298` | 读写他人画像/记忆              |

`session_id` 由客户端传入且是 12 位十六进制，知道 ID 即可接管上下文。文档 §10 要求  
"订阅 SSE、下载产物、读取画像全部执行对象权限"，目前只做了一小半。

**修法**：`/api/tasks*`、`/api/sessions*` 统一加 `Depends(current_principal)`；  
`sessions`/`tasks` 表补 `owner_user_id, tenant_id`（表里已有 `tenant_id`，缺 owner），  
查询层强制过滤；`profile` 按用户作用域落盘（`profile/<user>.md`）。

---

## P1 · 架构一致性（验收清单标了 ✅，生产未生效）

| #    | 问题                         | 证据                                                                                                                   | 说明                                                                                                  |
| ---- | -------------------------- | -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| P1-1 | **`context.build()` 是死代码** | 只有 `tests/test_core.py:719-746` 调用；生产用 `nodes.py:198` 的 `context.select` 和 `answers.py` 的 `chat_messages`            | 验收清单"按用途的完整预算 ✅"名不副实。同时存在两套预算表（`BUDGETS` / `PURPOSE_BUDGETS`），口径已经分叉。要么把 `build()` 接进规划/作答，要么删掉并改清单 |
| P1-2 | **部署按钮绕过执行链**              | `api.py:150-167` 直接 `deploy_task()`                                                                                  | 无账本 action、无策略检查、无事件 seq、暂停语义失效。文档 §13 明确"不能保留绕过执行器的独立写入口"                                          |
| P1-3 | **取消接口假装成功**               | `api.py:219-228`：`hub.cancel()` 返回 False 也照写 `cancelled` 并返回 True                                                    | Worker 进程里的任务无法跨进程取消（`cancel` 只查 `self.tasks`），run 却已被标成 cancelled → 界面说停了，实际还在跑                    |
| P1-4 | **续跑丢上下文**                 | `api_hub.py:396`、`run_worker.py:59`：`self._run(record, [], {}, ...)`                                                 | history / prior / memory 全空，续跑出来的报告接不上上文                                                            |
| P1-5 | **"刚才那份"仍是猜的**             | `api_hub.py:151-161` 扫最近 20 个任务取第一个有产物的                                                                              | 文档 §11 明确反对这个做法；应改为"当前会话产物列表 + 多份时让用户选"                                                             |
| P1-6 | **状态机两套词汇**                | `TERMINAL_STATUS`（`api_hub.py:30`）缺 `waiting_input` / `waiting_approval` / `partially_succeeded`；task 状态与 run 状态各说各话 | 一旦要做"等人确认"，前端与查询会立刻分裂                                                                               |
| P1-7 | **预算只记不限**                 | `usage` 全量采集，但 `MAX_STEPS=8`（`nodes.py:23`）、`asyncio.timeout(300)`（`main.py:73`）、`recursion_limit=30` 都是硬编码          | 文档 §7 要求"步数/调用次数/重规划次数/费用"可配置且可继承。另外 `attempt_timeout=180` × 2 次尝试 > 总预算 300s，网关慢时外层的超时会先砍断内层重试     |
| P1-8 | **创建任务无幂等**                | `POST /api/tasks` 无幂等键                                                                                               | 双击/网络重试 = 两个 run、两份部署。文档 §13 要求创建/恢复/取消带幂等标识                                                        |

---

## P2 · 可运维缺口

1. **全仓没有 `logging`**（`grep -r "logging\." app/` 为空）。排障只能翻 `events` 表，  
   拿不到"哪个 run / step / action 慢、失败率多少"。
2. **没有 metrics / 健康检查**：队列积压、unknown 动作数、模型延迟、上下文截断率都没有出口  
   （`/api/recovery` 只是启动快照）。
3. **没有 lifespan**：`app = FastAPI(...)` 无生命周期钩子，`hub.close()` 从不调用，  
   SQLite 连接与 WAL 不收尾；`uvicorn --reload` 反复起停会留下 -wal/-shm。
4. **数据无保留策略**：`events` 表与 `artifacts/` 只增不减，没有 TTL / GC 脚本  
   （`scripts/` 里只有 serve/migrate/list_tools/worker/retry_deploy）。
5. **`Hub.tasks` 无界缓存**：`Hub.get()` 把每个查过的任务永久留在内存。
6. **测试结构**：`tests/test_core.py` 2949 行 / 176 用例单文件。4 秒跑完是好事，但定位失败  
   成本高，建议按 api / hub / ledger / capabilities 拆 5 个文件。
7. **无并发上限**：单进程可以同时跑任意多个 `asyncio.Task`，上游没有队列与背压。

---

## 通用性：内核仍绑定网页业务

这是"通用中台"最实质的一条。

1. **规划提示词是业务硬编码**：`app/nodes.py:25-58` 的 `PLANNER_SYSTEM` 里写死了  
   `web_search → fetch_pages → compose_report → render_html`、"资料转 HTML 用  
   `render_sources_html`"、部署判定规则。**新增一个工单/数据库能力，必须改内核提示词**，  
   与文档 §4「业务提示词移到能力包」不一致（验收清单未列此项，属于遗漏）。  
   建议：`Tool` 加 `prompt_rules: str`，由各能力包自带；规划器 system = 通用规则 + 各能力  
   规则拼接；要按关键词裁剪（工具多时只拼候选工具所属能力包的规则）。
2. **终态判定认识业务**：`nodes.py:275-286` 的 `final_status` 判断  
   `answer / deployment / artifact`。建议每个能力包提供 `finalize(state) -> status`，  
   内核只做"按优先级询问各包"。
3. **`AgentState` 仍是业务字段**：`documents / report / deployment / sources_page` 直接  
   写在 TypedDict 里。文档 §6.2 要求"工具只读显式输入 + 受控 ExecutionContext"，  
   目前工具仍可读写整个 state（`tool.run(args, state)`）。
4. **工具注册是硬编码列表**：`tools.py:68` 的 `BUILTIN_TOOLS` + `_LOCAL_META` 两份手工表，  
   加能力要改两处；`Tool.capability` 字段已经有了，但没有发现机制。  
   建议：能力包内用 `@register(capability='web', effect='read')` 装饰器注册，  
   `tools.catalog()` 扫包汇总。

---

## 建议路线

### 批次 1（1–2 天，先止血，全部是 P0）

| 项    | 动作                                              | 验收                                                          |
| ---- | ----------------------------------------------- | ----------------------------------------------------------- |
| P0-1 | `Hub.start` 改为只入队；`create_run(status='queued')` | 同时起 API + worker 跑同一任务，远端只被调用一次                             |
| P0-2 | `drain_jobs` 改 async                            | `--jobs-only` 跑一次，`background_jobs.status='done'`，会话记忆真的被压缩 |
| P0-3 | note 先拼再建 messages                              | 新用例断言 user 内容含「仅取前」                                         |
| P0-4 | issues 非空 → `waiting_input`，不执行                 | 模拟未知工具，任务停在等待而不是带着坏计划跑                                      |
| P0-6 | `resume` 回库 + reconciling 可被恢复                  | 杀进程 → 重启 → `POST /api/runs/{id}/resume` 成功                  |
| P0-7 | 对账移到 lifespan 且看租约                              | 起两个副本，互相不误伤                                                 |

### 批次 2（3–5 天）

P0-5（seq/claim 原子化）、P0-8（全接口归属校验）、P1-1（删或接 `context.build`）、  
P1-2（部署走执行链）、P1-3/P1-4、P1-7（预算配置化）、`logging` + lifespan + `/api/recovery`  
扩成实时统计。

### 批次 3（按需，有实测数据再做）

能力包注册化 + 提示词分包（通用性）；P1-5 产物引用选择；并行步骤；retention/GC；  
metrics 看板；语义工具召回（当前工具数远未到需要）。

---

## 给验收清单的三处修订建议

避免"文档比代码乐观"——这是评审里最值得改的习惯：

| 条目                 | 现在                      | 实际                            |
| ------------------ | ----------------------- | ----------------------------- |
| 「按用途的完整预算 ✅」       | 指向 `context.build`      | 生产未使用，仅测试覆盖                   |
| 「计划结构化校验 + 有限修复 ✅」 | 指向 `validate_plan` + 两轮 | 两轮后仍带 issues 执行残缺计划           |
| 「业务提示词移到能力包」（未列出）  | —                       | `PLANNER_SYSTEM` 仍写死检索链路与部署规则 |

---

*本评审未修改任何业务代码。批次 1 的六项都已定位到具体行，可直接开工。*
