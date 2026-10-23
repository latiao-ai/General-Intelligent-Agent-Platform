# Harness 执行可靠性实施方案

日期：2026-10-23。性质：基于当前源码的实施方案，尚未修改或验证执行代码。

目标：补齐恢复、独立 Worker、未知结果、暂停与失租边界。保留 Python、FastAPI、LangGraph、PostgreSQL、多 MCP 与现有动作账本；本轮不增加自主重规划、多 Agent 或新队列。

## 1. 当前问题与验收目标

| 当前证据 | 问题 | 完成标准 |
| --- | --- | --- |
| `app/main.py` 使用 `build_graph().astream()`；`app/graph.py` 的 checkpointer 未接线 | 定义检查点不等于具备图恢复 | 明确唯一恢复来源，在任一步崩溃后能确定计划、输入、动作与下一步 |
| `Hub._finish_run()` 才保存 plan/steps，版本固定 1 | 执行中缺少数据库计划；异常和取消分支不进入正常收尾 | 首次工具执行之前计划已提交；每步确定结果及时提交 |
| `scripts/run_worker.py` 调用异步 get/history/prior_context 缺少 await | 真正领取任务的 Worker 路径可能直接报错 | 新任务与已有计划均由独立进程成功执行 |
| `ToolError` 包装异常；run_step 按类型名判断 unknown | 网络异常的执行确定性在包装后丢失 | 类型化传播确定性，不依赖用户文案或异常类名猜测 |
| pause 只写 `pause_requested`，无执行读取点 | 请求暂停不阻止后续派发 | 暂停与下一步准入存在清晰的事务先后关系 |
| `_guard()` 忽略续租 False；epoch 未贯穿写入 | 失租执行者仍可能推进状态 | 旧 owner/epoch 无法准入新动作或覆盖新执行者 |

成功标准以 PostgreSQL 记录和假 MCP 服务端的实际调用计数为准，不以页面提示或接口返回 200 代替。

## 2. 先确定实现合同

### 2.1 本轮选择数据库账本驱动恢复

以 PostgreSQL 的 plan、step、action、attempt 与运行快照为权威，LangGraph 负责当前进程内的步骤推进。`state.json` 只做诊断和产物旁文件。

暂不把 LangGraph 持久检查点作为另一条恢复入口。不能只给 `build_graph` 传一个 saver 就认为修好：它还涉及异步 saver 生命周期、thread_id、恢复调用方式以及与动作提交的协调。

本轮应删除或明确停用未接线的 `checkpointer()`，修正文档。以后引入持久检查点时，仍必须经过动作账本；不能由图恢复直接重放外部写操作。

### 2.2 状态、控制请求、结果确定性分开

建议增加通用生命周期状态 `paused`。同步修改 `app/outcome.py`、接口、前端状态文案及状态词汇测试。不要把 paused 映射成失败或“任务已完成”。当前 TERMINAL_RUN_STATUSES 也包含等待态，需区分“本次执行已退出”和“目标已最终结束”，调整相关调用点。

| 情形 | run 状态 / 控制 | 下一步 |
| --- | --- | --- |
| 正常执行 | running | 有效租约下准入动作 |
| 正在请求暂停 | running + pause_requested | 已准入动作收尾，禁止准入新动作 |
| 安全暂停 | paused + pause_requested | 显式 resume 才回 queued |
| 在途结果未知 | needs_reconcile | 查询或人工核实，禁止直接重发 |
| 用户取消且无未知动作 | cancelled | 不再自动执行 |
| 用户取消但存在未知动作 | needs_reconcile + cancel_requested_at | 保留停止意图，核实后不能自动继续 |
| 失去执行权 | 旧执行者本地 LeaseLost | 禁止其写 run 终态；由当前执行者或对账处理 |

优先级：失去执行权时禁止写入；结果未知时保留 needs_reconcile；取消阻止后续执行；暂停阻止新动作；全部必需步骤有确定成功回执后才成功。已有部分产物不能覆盖这些控制状态。

### 2.3 不承诺跨系统事务

中台数据库与远端 MCP 无法组成同一个数据库事务。`in_flight` 只证明中台已准入调用，不证明服务端已收到。准入后到实际发送之间崩溃也必须保守核实。

本轮保证可追踪、稳定身份、已成功复用、未知不盲重发。若客户工具支持幂等键或按业务键查询，另通过明确适配器传递；不要向任意 MCP schema 自动塞字段，也不要宣称仅靠本地账本实现远端 exactly-once。

## 3. 阶段 A：先修 Worker 真正领取执行的路径

涉及：`scripts/run_worker.py`、`app/api_hub.py`、`tests/test_core.py` 或新增 `tests/test_worker_execution.py`。

1. 将 `record = hub.get(...)` 改为 await；history、prior_context 也在启动 `_run` 前 await。
2. 把“领取以后构建 record、读取上下文、创建执行任务”的整个过程纳入异常处理。当前部分初始化位于 try 之前，报错可能直接打断 Worker 循环。
3. 空计划明确传 `resume_plan=None`；`{'steps': []} or None` 实际仍返回非空字典。
4. 明确两条分支：没有计划且没有历史动作时允许规划；有已提交计划则恢复该版本。已有动作却缺失计划时禁止自动重新规划，进入待核实。
5. 正常执行、resume 与 Worker 统一调用新增的 `Hub.execute_claimed_run(claimed_run)`，避免三处分别组装上下文和身份。start 仍可就地执行，但也必须走 claim 后同一入口。
6. 记录本次领取的 owner、epoch、有效期。若本进程缓存里已有 live record，必须核对是否同一次领取；不能拿旧 TaskRecord 的活跃协程充当新的执行者。
7. 单个任务出错不退出整个 Worker；进程关停信号仍按关停处理，不吞成单任务失败。
8. 删除脚本注释中已失效的 SQLite 支持说法，项目当前唯一仓储是 PostgreSQL。

先补四个回归：Worker 真正领到任务并完成；已有计划不调用规划器；任务初始化异常后仍能领下一条；API 与 Worker 竞争同一 run 只执行一次。不要只测“队列为空”。

阶段 A 是解阻补丁，最终异常收尾仍要由阶段 B 的租约保护。

## 4. 阶段 B：贯穿租约代次，封住旧执行者

涉及：`app/persistence.py`、`app/pg_store.py`、`app/api_hub.py`、`app/ledger.py`，建议新增 `app/execution_control.py`。

### 4.1 执行上下文

定义受控 `ExecutionContext`：run_id、plan_version、lease_owner、lease_epoch。通过 ContextVar 或显式依赖传递，不把数据库连接放入 AgentState；执行者不能从模型参数中获取这些字段。

`TaskRecord` 保存 epoch 只用于调度和展示，数据库仍是判断依据。

### 4.2 新增仓储事务接口

以下为建议名称，可按现有风格调整：

| 接口 | 原子完成的事情 |
| --- | --- |
| `commit_plan(context, plan, initial_state)` | 校验租约；保存计划、步骤、初始恢复状态、版本与计划事件 |
| `admit_action(context, step, resolved_args)` | 校验租约和控制请求；查找/准备动作；按条件建立 attempt 与 in_flight |
| `settle_action(context, action_id, attempt_id, outcome)` | 校验租约；提交动作/attempt/步骤结果及恢复快照 |
| `pause_at_boundary(context)` | 校验租约；写 paused 与事件，交还租约 |
| `finish_run_owned(context, outcome)` | 校验租约；原子完成 task/run/最终状态及收尾去重记录 |

事务采用一致锁顺序：run → step/action → attempt → 事件/快照。所有执行推进都检查 owner、epoch、有效期和允许状态；旧 epoch 更新必须影响零行并抛 `LeaseLost`。事务锁不能跨模型或 MCP 网络等待。

`renew_lease`、`release_lease` 同样带 epoch；续租还需检查租约未过期，避免旧执行者把已过期租约自行复活。claim 后的关键提交建议采用数据库时间校验有效期。

### 4.3 Guard 与失租处理

1. 续租返回 False：立即撤销本地执行资格，禁止新动作，协作取消本地工作。
2. 数据库短暂不可用：暂停派发；不能“记录日志后照常继续”。在确认仍持有效租约后才恢复准入。
3. 区分 UserCancel、LeaseLost、Shutdown 和 TaskTimeout。不要让 `_run` 的所有 CancelledError 都写成用户取消。
4. 失租后拒绝旧执行者覆盖 run、step、action、TaskRecord 的持久业务状态及完成消息。必要诊断日志可以保留。
5. 远端已经发出的请求无法靠失租撤回。迟到响应不能绕过 epoch 校验提交；交给核实流程恢复证据。

并发验收：A 领取 epoch 1，租约过期后 B 领取 epoch 2；A 不能续租、提交结果、派发下一步、写 failed 或清除 B 的租约。A 已发出的远端操作仍标记为需要核实。

## 5. 阶段 C：先持久化计划，再执行；按步骤恢复

涉及：`app/nodes.py`、`app/main.py`、`app/api_hub.py`、`app/pg_store.py`、`app/schemas.py`、`app/graph.py`。

### 5.1 数据与迁移

建议新增 `run_snapshots` 表，以 run_id 为主键，包含 plan_version、snapshot_schema_version、state_json、next_step_id、artifact_namespace、updated_at。也可放在 runs 的显式列中，但不要继续依赖进程内 state。

快照保存恢复所需的最小完整状态：原始输入/上下文引用、步骤输出、业务 state、稳定产物目录引用。长正文和文件通过产物引用保存；不保存密钥、数据库连接或 asyncio.Task。

按当前 `_apply_schema` 风格增加可重复的 schema 升级，显式补旧表列；新增 CHECK 约束前先检查历史数据。不要仅改 CREATE TABLE 以为旧库会自动补列。

### 5.2 计划提交点

1. `plan()` 返回经校验的完整计划。
2. 增加 `persist_plan` 节点或调用受控计划提交接口。
3. plan、steps、plan_version、初始快照全部事务提交后，才允许进入第一步。
4. 数据库失败时零工具调用；不能只靠 on_event 回调稍后保存。
5. `_finish_run()` 改为收尾汇总，不再承担首次保存计划。账本和恢复读取实际版本，移除到处固定版本 1 的做法。

### 5.3 每步提交点

把当前 ledger.in_flight 的分两次提交改为一个事务；把 succeed/attempt 结算、step output 和快照推进也合并为一个事务。若工具已返回但提交失败，不推进内存游标后继续下一步。

动作稳定身份要包含 tool_key。当前 `key_of()` 接受 tool_key，却未把它放入键中；阶段 B 应顺便校验存量 action 的 tool_key/args_hash 与当前步骤一致。旧动作沿用原键，不批量重新生成身份；新动作可以加键格式版本，通过迁移或双读保证已有回执仍被找到。

### 5.4 恢复算法

1. 读取 run 和原计划，原子领取租约。
2. 校验 plan_version、快照版本和动作归属；读取持久状态。
3. 按原顺序核对动作：succeeded 复用已存结果；prepared 可再次准入；in_flight/unknown 先核实；确定失败按显式恢复策略处理。
4. 发现“成功回执已落库但快照没推进”的旧数据时，用可信回执重建该步输出，不调用远端。
5. 不能仅跳到第一个未完成步骤：必须先重建前序输出，确保 JSON Pointer 引用仍可解析。
6. 保持 run_id、plan_version、action_id 及产物命名空间；不要每次 resume 在 `run_task()` 中重新生成目录 UUID。
7. 最终状态、assistant 完成消息和完成事件按 run 的结算身份去重，避免成功后断流恢复产生两条完成消息。

旧版本执行中断后只有 action 而无计划的数据：不得用当前模型新规划冒充恢复。标 needs_reconcile，保留记录，提供显式核实或另建任务入口。

恢复验收至少覆盖：计划已提交未执行；prepared 未准入；in_flight；成功回执已有而旧快照落后；文件已产生未收尾；最终结算已提交但响应丢失。每个窗口都核对原任务身份和远端计数。

## 6. 阶段 D：未知结果不再靠异常名字判断

涉及：`app/capabilities/base.py`、`app/integrations/mcp_adapter.py`、`app/tools.py`、`app/nodes.py`、`app/ledger.py`、`app/outcome.py`。

### 6.1 类型化错误合同

新增统一工具错误字段：safe_message、error_code、phase、execution_certainty、external_receipt_ref。phase 可为 validate、connect、dispatch、receive、decode；certainty 可为 not_executed、failed_confirmed、unknown。

包装为 ToolError 时必须保留这些字段及原异常链。删除按 `type(exc).__name__` 白名单决定是否 unknown 的做法。底层无法可靠判断请求是否送达时，默认 unknown。

| 场景 | 分类与处理 |
| --- | --- |
| 本地 schema 不合法、权限拒绝，且尚未调用远端 | not_executed，不记成远端执行失败 |
| 服务端返回经适配合同确认的失败回执 | failed_confirmed，保存原回执；如有部分效果继续核实 |
| 发送之后超时、连接断开、结果解析失败 | unknown，保留同一 action/attempt |
| 收到 isError 但工具可能部分写入 | 保留业务错误；不能仅凭 isError 证明没有副作用 |
| 取消、总体超时、进程关停时仍存在 in_flight | unknown；停止后续派发 |
| 纯查询失败 | 可以允许显式重试，但不能把 unknown 的写操作顺带重试 |

“readOnly”不保证调用免费，也不自动授予无限重试。首轮先不增加自动重试机制。

### 6.2 核实与补偿

1. needs_reconcile 不能由普通 resume 清掉。
2. 有服务端幂等查询能力时，按原 action 对应的外部键查询；中台内部身份只由受信适配器映射。
3. 核实成功：保存原效果回执，再决定是否继续；若有 pause/cancel 意图，继续保持停止。
4. 服务端可靠确认未执行：转成可显式重试；仅“查询没找到”不一定证明未执行，需遵守服务端一致性合同。
5. 无法核实：继续 unknown。人工处理记录处理人、证据和原因，不能只有一个“强制成功”开关。
6. unknown 不自动补偿，也不触发重发；先前已成功步骤是否补偿，按现有显式策略独立处理。

回归必须从 MCP 假服务到 adapter、runner、run_step、ledger 全链路走一遍；仅直接调用 `mark_unknown()` 证明不了包装链正确。

## 7. 阶段 E：暂停、取消、恢复的真实边界

涉及：`app/api_hub.py`、`app/nodes.py`、`app/pg_store.py`、`app/api.py`、`app/main.py`、`app/outcome.py`、`app/web/index.html`。

### 7.1 暂停请求和确认分开

pause API 返回“暂停请求已登记”，建议 202 与 `{pause_requested:true,status:...}`；执行器安全退出后再发 paused 事件。前端分别显示“正在暂停，等待当前操作结束”和“已暂停”。

对 queued run，在同一事务直接置 paused，防止 Worker 领取。重复暂停幂等；已结束任务返回当前状态或明确 409。

### 7.2 暂停与准入同锁排序

pause 写入和 `admit_action` 都锁同一 run 行：

- 暂停先提交：该步不得获得调用资格。
- 动作先准入：该步视为在途，允许收取结果；暂停不承诺其尚未发生。

执行器在规划后、每步准入前、每步结算后检查控制状态。读到暂停后调用 `pause_at_boundary`，提交快照、状态与事件，再交还租约。

纯本地规划期间暂停可以等待当前模型请求结束后保存计划，但第一步不得开始。暂停期间继续续租以收取在途结果；达到明确在途截止时间后结果未定，转 needs_reconcile。

### 7.3 恢复与取消

resume 在一个受控事务内检查状态、unknown 动作和控制意图；只有 paused 或允许恢复的 interrupted 才恢复，禁止直接复活 succeeded/cancelled。确定失败的重试另走明确策略；needs_reconcile 必须先核实。

恢复清除 pause_requested 并排队，沿用原计划、版本、动作与目录。并发 resume 只能产生一个有效执行者。

取消阻止后续动作，已发送 MCP 请求是否能取消依赖服务端能力。API 不得把“中台停止等待”展示成“远端撤销成功”。取消和超时分支在有限收尾时间内保存未知结果；若数据库不可用，留下 in_flight 供租约过期后对账，不伪造成功结算。

`main.run_task` 的 finally 不得根据已有产物把 paused、cancelled、needs_reconcile 重算成 succeeded。正常结束也应验证所有必需步骤已确定完成。

### 7.4 工具消失的相邻修复

修改 run_step 中“找不到工具就跳过”。应记录 tool_unavailable，保留原工具身份和计划；根据是否可恢复进入等待/中断或失败，不继续依赖它的步骤。修改现有 `test_run_step_skips_unknown_tool_without_failing`，不能继续用旧测试钉住错误行为。

## 8. 验收矩阵与测试组织

建议拆分 `tests/test_worker_execution.py`、`tests/test_execution_recovery.py`、`tests/test_execution_control.py`、`tests/test_mcp_uncertainty.py`。沿用 unittest 与真实 PostgreSQL；用本地假模型、假 MCP，避免真实收费调用。

| 用例 | 必须断言 |
| --- | --- |
| Worker 领取新任务 | 完成一次；上下文是实际值；没有未 await 协程 |
| API/两个 Worker 抢同一任务 | 同一动作只获得一次有效准入 |
| 旧 epoch 恢复运行 | 新动作、结算、终态和释放租约均被拒绝 |
| 租约续期失败/数据库暂断 | 新动作停止；恢复连接后先重新确认资格 |
| 计划持久化失败 | 远端调用计数为零 |
| prepared 后杀进程 | 新进程可沿原身份执行 |
| 服务端执行写入后断连接 | action unknown；resume 不增加远端写入计数 |
| 服务端已返回、提交前杀进程 | 通过核实恢复；不盲目重发 |
| 成功结算后、客户端收回执前断连接 | 读取原结果；完成消息不重复 |
| 包装后的超时/解析失败 | 依旧 unknown；不能落 failed 后自动再发 |
| pause 在下一步准入前提交 | 下一步调用计数为零 |
| pause 在动作准入后提交 | 只允许该步收尾；后续不派发 |
| queued 时暂停、暂停后重启 | Worker 不领取；保持 paused |
| paused 后两个并发 resume | 原计划与产物目录不变；单执行者 |
| cancel/timeout 遇到在途写入 | 保留待核实；不自动补偿 unknown |
| 必需工具执行前消失 | 后续依赖不执行；不能因有部分文件就成功 |

并发测试用 barrier 或受控事件固定“已准入/已发送/已提交”时点，不依赖任意 sleep。崩溃恢复用子进程真正终止再启动，保留独立假服务的调用账本。

现有测试初始化可能清空表；PG_TEST_DSN 必须指向一次性测试库。验收环境未提供测试库时不能把 skipped 算通过。实施后执行：

```bash
python -W error::RuntimeWarning -m unittest discover -s tests -v
```

运行前按当前项目约定配置一次性 PG_TEST_DSN；同时核对实际运行数、跳过数和独立进程矩阵。阶段 A 即应加入 RuntimeWarning 检查，后续保持。

## 9. 建议提交顺序与交付物

1. **Worker 解阻**：await、初始化异常、真实领取测试，四个基础回归通过。
2. **租约和控制底座**：ExecutionContext、owner/epoch 条件提交、原子动作准入和结算；失租竞态通过。
3. **数据库恢复**：迁移、执行前提交计划、逐步快照、统一恢复入口；杀进程恢复通过。
4. **未知结果**：类型化错误、取消/超时边界、核实门禁；写入后断连接不重发。
5. **暂停与收尾**：paused 状态、事务排序、接口与前端、完成去重；跨进程暂停恢复通过。
6. **验收与文档**：完整矩阵，修正 `docs/platform-acceptance.md` 和 README 的过时结论，记录真实未覆盖项。

部署前暂停新任务进入，让旧执行者排空并完成对账，再统一升级 API 与 Worker；旧 Worker 不具备新 epoch 提交合同，不能与新版混跑处理同一任务池。先执行可重复迁移，再启用新版执行入口。不要为迁移方便删除历史动作、重置未知状态或批量改成成功。

最终交付应包括迁移、代码、回归测试、假服务调用证据、接口变化说明及升级步骤。每个阶段“完成”都要求对应验收通过；新增数据库字段、函数或测试名称本身不算完成。
