# Harness 运行中补充输入实施方案

日期：2026-10-23。状态：实施设计，未修改执行代码、未运行验收。

目标：借鉴 BeefTV 的三项机制——区分新任务与当前任务补充；补充先持久化；阻止依据旧要求产生的新操作。沿用本项目 FastAPI、LangGraph、PostgreSQL、MCP、SSE 与动作账本。

## 1. 先确认当前起点与依赖

本次读取的工作区已包含 `app/execution_control.py`、`Hub.execute_claimed_run()`、仓储 `admit_action/settle_action/commit_plan` 和 `run_snapshots`，不同于前一轮分析时的旧版本。本方案在这些入口扩展，不重复搭建。函数存在不代表并发、崩溃矩阵已通过，实施前仍应跑前一份可靠性方案的验收。

当前与新能力直接冲突的行为：

- `app/web/index.html` 的 `send()`：`if (S.running) await interrupt()`，随后 `postTask()` 新建任务。
- `POST /api/tasks` 与 `Hub.start()` 每次建立新的 task/run。
- `app/graph.py` 仍是 `plan → run_step ↺ → END`，没有消费补充与修改剩余计划的分支。
- `ExecutionContext` 的 lease_epoch 代表执行者代次，不能拿它代替用户要求版本。
- `ActionLedger` 的幂等身份与 plan_version 有关；重规划若简单重建全计划，会重开动作身份。

本方案必须同时完成“接收、持久化、消费、执行门禁、收尾门禁”，只新增一个消息接口无法实现运行中调整。

## 2. 对外行为先定清楚

### 2.1 输入类型由接口明确表达

| 用户动作 | 后端入口 | 任务身份 |
| --- | --- | --- |
| 开始一个新目标 | 现有 `POST /api/tasks` | 新 task/run |
| 给当前任务补充要求 | 新增 `POST /api/runs/{run_id}/inputs` | 原 task/run，不另建执行者 |
| 暂停、取消 | 现有专用控制入口 | 原 run，按控制规则执行 |
| 新建独立会话 | 现有新会话交互，再创建任务 | 新 session/task/run |

首版不要靠模型或关键词自动判定“补充还是新任务”。当前任务运行时，默认发送是补充；用户明确点击“新任务/新会话”才另建。旧的“打断后发起新任务”可保留为明确按钮。

对于同一会话的默认工作流，首版采用单个可交互活跃目标，服务端也检查冲突；其它客户端不能绕过前端同时创建第二个默认目标。若以后支持并行任务，要让用户明确选择目标，消息始终携带 run_id。不要通过“会话里最新一条任务”猜归属。

### 2.2 补充与停止状态

| run 当前状态 | 新补充处理 |
| --- | --- |
| queued/running | 接收；阻止旧计划准入新动作；边界处处理 |
| paused/waiting_input | 接收；保持暂停或等待，用户显式继续时消费 |
| waiting_approval | 接收；旧批准与新要求是否兼容需重新验证，不自动消费旧批准 |
| needs_reconcile | 可记录新要求；先核实在途旧效果，不启动新的业务写操作 |
| cancel_requested/已取消 | 拒绝，不能借补充恢复执行 |
| succeeded/failed/最终结束 | 拒绝并返回已结束；前端提示明确开启新一轮 |
| 不是该 task 的 current_run_id | 拒绝旧目标输入，不能偷偷转发到新 run |

“已收到”只代表数据库提交；“已纳入”代表基于新要求的计划已提交；“已完成”才是业务目标完成。三种文案不能混用。

## 3. 机制一：区分新任务与补充输入

### 第一步：定义新接口

新增 `RunInputRequest`，建议放 `app/api.py` 或专用 schema 模块：

```json
{
  "request_id": "客户端生成并持久保存的随机ID",
  "message": "改成竖屏，结尾不要品牌名称"
}
```

首版只做文本，不顺便开放任意文件路径、工具权限或模型凭据。后续接附件时只接已授权产物引用。request_id 有长度和字符格式限制；message 沿用现有长度限制。

新增：

- `POST /api/runs/{run_id}/inputs`：接收补充，事务提交后返回 202。
- `GET /api/runs/{run_id}/inputs`：按 after_seq 分页回读接受与处理状态。
- run/task 查询增加 intent_version、processed_intent_version、pending_input_count。

响应示例：

```json
{
  "input_id": "inp_...",
  "task_id": "...",
  "run_id": "...",
  "input_seq": 2,
  "intent_version": 3,
  "processed_intent_version": 1,
  "status": "accepted",
  "replayed": false
}
```

202 不启动第二个 Worker，也不调用 cancel。请求重试返回原 input 与接受版本；另附当前处理状态也可以，但不能把原接受版本改成最新值。

### 第二步：在 Hub 中建立独立入口

新增 `Hub.append_run_input(run_id, request_id, message, principal)`，只调用事务仓储，不进入 `Hub.start()`，不追加一个普通新 task。

授权先验证租户、用户、task/run/session 归属与 current_run_id；无法读取的对象统一按当前项目规则返回 404。客户自报 session_id、tenant_id、权限或版本均不能扩大范围。

对同 request_id 的重试，先在完成归属验证后查原回执：相同内容返回原记录，即使 run 已收尾；内容变化返回 409 input_request_conflict。全新请求再检查可接收状态。避免“响应丢失后重试，却因任务完成而误报未接收”。

### 第三步：改前端发送分支

修改 `app/web/index.html`：

1. 保存当前 run_id；现有 create_task 响应已有该字段，前端需要保留。
2. running 时走 `postRunInput()`，删除默认发送中的 `interrupt()`。
3. 保持原 taskId 与 EventSource，不调用 `startNewTurn()` 清除原任务视图。
4. 每条补充独立保存 request_id、run_id、原文和发送状态；按钮只限制重复提交当前这条。
5. 网络失败标“接收待确认”，保留同一个 request_id 查询或原样重发；不能换 ID，也不能退回创建新任务。
6. 只有最终结束拒绝时提示用户“任务已结束，是否另开一轮”；不要自动另开并执行。
7. 发送时冻结 session/run 归属；切会话后的旧请求结果写回原会话状态，不能污染当前界面。

验收：第一条任务运行中补充两次，数据库仍是一条 task、一条 run；没有 cancel_requested；原 SSE 继续显示状态。

## 4. 机制二：补充先持久化，再让执行者消费

### 第四步：新增数据结构

建议在 `app/pg_store.py` 的幂等 schema 升级中增加：

| 对象 | 字段 | 用途 |
| --- | --- | --- |
| runs | intent_version，默认 1 | 数据库已接受的最新要求版本 |
| runs | processed_intent_version，默认 0 | 已提交有效计划覆盖的要求版本 |
| run_inputs | input_id、run_id、input_seq、request_id、message、payload_hash、intent_version、created_at | 不可变输入日志 |
| run_input_processing | input_id、status、processed_plan_version、error_code、updated_at | 可重建的处理投影 |
| plans | intent_version、base_plan_version | 计划依据及替代关系 |
| actions/attempts | decision_intent_version | 动作依据的要求版本，便于审计 |
| run_snapshots | intent_version、processed_intent_version | 恢复时核对；数据库 runs 版本优先 |

`run_inputs` 的唯一约束至少包括 `(run_id, request_id)` 与 `(run_id, input_seq)`。input_seq 在锁住 run 后分配；不能无锁 MAX+1。原始新任务 message 保留在现有 task 中，补充从 seq 1 开始；原任务初始 intent_version 为 1，第一次补充升到 2。

processing 状态建议 accepted、applied、needs_input；accepted 可由任一新执行者继续处理，不用一个永久 processing 标记把崩溃任务锁死。applied 只表示计划已经纳入，不表示业务效果已完成。

### 第五步：实现一次原子接受事务

建议新增 `Repository.append_run_input(...)`，在一个连接和事务中完成：

1. 校验对象归属，锁定 run，核实其 current_run_id 与状态。
2. 查 request_id 幂等记录；相同内容返回原回执，不再次加版本。
3. 对新请求分配 input_seq，插入不可变日志。
4. intent_version 增加 1，processed_intent_version 不动。
5. 插入 processing 投影 accepted。
6. 在同一事务追加 input_received 事件，带 input_id、input_seq 和 intent_version。
7. 提交后才返回 202，再尽力通知进程内执行者。

不要在这个事务外先写消息再升版本，也不要先推 SSE 再落库。不要持锁等待模型或 MCP。

现有 append_event 自己开启连接，不能直接在外层事务调用它以冒充原子性；应提取 `_append_event_in_tx(conn, ...)` 复用现有序号锁规则。事件写入的锁顺序统一为 run → 输入/计划/动作 → 事件，避免新的死锁顺序。

### 第六步：消费者依赖数据库，通知只做加速

新输入 API 不需要持执行租约；它作为授权用户修改要求。执行者在已有边界查询最新版本，API 与 Worker 可以在不同进程。

可以用现有 guard 的周期唤醒检查，但写操作安全性不能依赖轮询是否及时。真正的阻断由后面 `admit_action` 的事务比较实现。

崩溃窗口行为：

- 提交前崩溃：没有接收回执；客户端同 ID 重试。
- 提交后响应丢失：同 ID 重试拿原 input，版本不再增加。
- 接受后 Worker 崩溃：新执行者读取未消费输入，不依赖内存队列。
- 新计划已提交后崩溃：processed 版本与 applied 投影已原子提交，不重复重规划。

消息历史以输入日志为真实来源，读取会话时间线时合并展示。若需要向 messages 表投影，增加 origin_input_id 唯一关联并事务去重，避免另存一条普通用户消息后在规划上下文中重复拼入。

验收：跨进程 API 接收、Worker 消费；重试、重启后输入顺序与内容完整，只有一次版本增长和一次输入事件。

## 5. 机制三：让旧要求产生的操作失效

### 第七步：先分清三个版本

| 版本 | 代表什么 |
| --- | --- |
| lease_epoch | 哪个执行者有权推进 |
| intent_version | 用户要求更新到了哪一版 |
| plan_version | 当前采用哪份执行计划 |

三者必须分别保存与校验。新补充不需要重新领取租约；同一执行者可以依据新要求提交新计划。新要求也不自动撤销已经执行的操作。

### 第八步：为每次模型决策冻结输入版本

建议新增 `app/run_inputs.py`，提供 `build_decision_input()` 等纯组装逻辑；在 nodes 的规划、修订和最终回答入口使用。

模型输入至少包括：原目标、按序补充、最近有效计划、真实已完成动作与输出、在途/未知动作、已发生的外部效果、当前权限与可用工具、预算。

一次读取形成一致的输入快照，记录 decision_intent_version。不要读完补充后再无锁读取“最新版本”贴上去：内容与版本必须相互对应。

processed_intent_version 不能在“开始调用模型”时更新。只有基于该版本的计划通过校验并事务提交后，才更新。输入很长时保留完整原文引用；新补充和关键约束不得被普通聊天摘要静默替代。装不下时进入等待，不截断后假装已理解。

### 第九步：扩展动作准入事务

在现有 `ActionLedger.admit()` / `PostgresStore.admit_action()` 增加 expected_intent_version 参数，来自已提交计划，禁止由模型自行填写。

准入事务锁住 run 后同时检查：

```text
租约 owner/epoch/有效期仍合法
并且没有 pause/cancel 阻断
并且 expected_intent_version == runs.intent_version
并且该计划是当前可执行计划，已消费版本相符
```

失败抛专门的 `IntentChanged` 控制异常，不能包装成普通 ToolError、记录成远端失败或触发补偿。

初版对所有旧版本的新业务工具调用都拦截，不仅 write：unknown 分类可能实际有副作用；旧查询结果也可能被当作最新证据。已在途查询可以收回结果并标来源版本。核实未知效果需要专门受控的查询路径，不能为放行它伪造“模型已读最新要求”。

最重要的非对称性：

- **新动作准入必须版本相等。**
- **已准入动作的回执结算只要租约等执行条件合法，就应保存，不能因为用户版本更新而丢掉真实效果。**
- 旧结果标记来源版本，更新事实账本；不能把 processed_intent_version 一并推进，不能激活旧计划后续步骤。

补充事务与准入事务争用同一 run 行，因此边界明确：新补充先提交，旧动作零准入；旧动作先准入，则视为在途，补充只约束后续。数据库事务之外已经准入但尚未发出的窄窗口无法完全消除，不能承诺新消息到达就撤销一切。

### 第十步：增加受控的修订计划循环

现有 LangGraph 是固定计划执行，不能照搬 BeefTV 的 steer 参数。建议加入 `consume_inputs/revise_plan` 节点：

```text
plan → persist_plan → run_step
run_step → 还有步骤：run_step
run_step → 要求版本变化：consume_inputs → revise_plan → persist_plan
run_step → 暂停/待核实：退出本次执行，保留状态
run_step → 全部完成：finalize_gate
finalize_gate → 有新输入：consume_inputs
finalize_gate → 版本仍一致：原子结算
```

persist_plan 使用图中显式节点或同等同步屏障，不能仅依赖消费 updates 事件后才落库。扩展当前 `commit_plan` 为 CAS：expected_intent_version、expected_base_plan_version 与租约都匹配才提交新计划。

`revise_plan` 本轮只解决“补充导致的剩余计划调整”，不扩展成任意自主重规划。模型输出包含新剩余步骤、已有结果引用和需要澄清的问题；沿用 validate_plan 再增加以下校验：

1. 已执行动作不可重写或删除账本记录。
2. 原计划未执行的旧步骤标 superseded，保留审计。
3. 新步骤使用 run 内唯一稳定 ID，例如 p2_s1；不能覆盖旧 s1 的输出。
4. 新步骤可引用成功的旧步骤输出；校验器显式接受一组受信历史输出，不要求伪造旧步骤重新执行。
5. 依赖未知操作的步骤不执行，先待核实。
6. 改要求不自动扩大工具权限或既有批准范围。
7. 同一预期外部效果若已完成，必须引用原 action/产物；不能因 plan_version 增长重发。
8. 如果确实要再次生成、覆盖或发布，则作为新的业务动作按权限/批准规则处理，不能按“工具+参数相同”自动判为同一效果或自动授权。

重规划不重置总超时、调用预算与已消耗量。配置每次运行最大修订调用数；超过预算时保存未消费输入并进入 waiting_input 等待显式继续，不能把它标 applied。

如果生成计划过程中又收到输入，旧候选计划不提交；读取最新快照再修订。并发连续输入可合并到一次下一轮决策，但按序保留全部原文。同一个 Worker 串行消费，不为每条补充各开规划任务。

### 第十一步：封住回答与结束的旧版本窗口

不仅 MCP 写操作需要门禁，`answer_directly/answer_with_sources` 等产生用户可见回答的模型调用也要记录决策版本。旧回答可以作为历史过程记录，但不能被标为已满足新要求。

现有 answer_delta 没有版本字段。增加 decision_id、intent_version；前端按决策分段展示。新输入 accepted 后，旧段落标记“调整前的输出”，不能把新旧两次回答拼成一条连续最终答案。已经展示的文字不能假装从未发生。

扩展 `finish_run_owned()`：在与 append_run_input 相同的 run 行锁内检查最新版本已处理、计划是当前版本、无未知动作及未完成必需步骤，才保存最终状态、消息和事件。

若新输入先提交，旧结束操作失败并回到消费循环；若最终结算先提交，全新补充得到 run_finished。接口不得出现“已接受但执行器恰好结束，从此没人处理”的悬空输入。

## 6. 事件、前端、审批与异常合同

新增持久事件：

| 事件 | 展示含义 |
| --- | --- |
| input_received | 补充已保存，等待纳入 |
| intent_update_started | 正在根据补充调整 |
| plan_revised/input_applied | 新计划已提交，显示采用的要求版本 |
| input_needs_clarification | 已读但缺必要信息，显示具体问题 |

事件必须带 run_id、input_id 或输入范围、intent_version；plan_revised 带 plan_version。继续使用现有 task SSE 与 seq 回放，不为每个补充另开事件流。跨副本即时通知未实现时，以有界数据库轮询/补发保证更新，并把通知延迟与数据库接受状态分开。

错误合同建议：404 目标不可访问；409 input_request_conflict/run_not_current/run_finished/run_cancelled；422 内容不合法；429 输入积压超限；503 数据库暂不可用。只有事务提交才算 accepted。

输入数量/体积设置可配置上限，在接受事务内检查，防止无限积压。请求限流不授权丢弃已经接受的消息；超限拒绝新请求并保留原输入框草稿。

补充不是批准。比如原要求“生成本地页面”，补充“直接发布”不能借一次普通 append 操作自动获得额外工具权限。既有明确授权是否覆盖，交给授权合同验证；模型只提出待批准动作。新 intent_version 使旧批准需要重新匹配或显式继承，不能无条件复用。

## 7. 与暂停、取消、失租、恢复的衔接

- 新输入不是取消：不调用现有 interrupt，不撤销已生成文件，不自动删除线上对象。
- 暂停期间可以保存补充，但不自动恢复；resume 后先消费新输入，再尝试旧计划步骤。
- 取消先提交时拒绝全新补充；补充先提交、随后取消时保留 accepted 日志和取消结局，不声称已应用。
- unknown 在途效果先核实。新要求可以记录，后续依赖操作必须停止；核实成功也不清除 pause/cancel。
- lease_epoch 变化后新 Worker 读取 run_inputs 与当前版本；恢复快照版本落后时，以数据库输入日志重建，不把版本回退。
- 补充准备或新规划失败时，旧计划继续被 intent_version 阻断；进入 waiting_input/failed 等明确状态，不能当补充没发生而恢复旧动作。
- 原任务多轮补充仍沿用原 run；若最终任务已结束，用户明确新开一轮才建立新 task/run。

## 8. 文件修改清单

| 文件 | 实施内容 |
| --- | --- |
| `app/api.py` | 补充 request/response、POST/GET 输入接口、归属与错误映射 |
| `app/api_hub.py` | append_run_input、状态读回、恢复消费、统一结算门禁 |
| `app/persistence.py` | 新输入仓储接口、带版本的计划提交与动作准入合同 |
| `app/pg_store.py` | 幂等迁移、输入接受事务、CAS 计划提交、动作准入和最终结算检查 |
| `app/execution_control.py` | 独立 IntentChanged，保持租约代次与要求版本分离 |
| `app/run_inputs.py`（新增） | 按序组装输入、决策快照与消费辅助逻辑 |
| `app/schemas.py` | AgentState 的 intent/plan/decision 字段与修订计划结构 |
| `app/nodes.py` | 边界分支、consume_inputs/revise_plan、历史输出引用校验 |
| `app/graph.py` | 修订循环、计划提交屏障、收尾检查分支 |
| `app/main.py` | 去掉“一次计划提交”的固定假设、预算跨修订累计、快照恢复 |
| `app/ledger.py` | 准入传决策版本，保存原动作身份与已执行效果引用 |
| `app/context.py` | 为当前要求、顺序补充和已执行事实分配预算 |
| `app/events.py`、`app/messages.py` | 新输入和调整事件、待核实/等待的通用回执 |
| `app/web/index.html` | 新任务与补充分支、run 归属、发送状态与分版本回答 |

注意 `app/main.py` 当前 `plan_committed` 布尔值只适合一次规划，应改按 plan_version 管理；否则第二份计划可能没有经过持久提交就开始执行。

## 9. 分阶段提交与验收

### 提交 1：输入日志与接口

完成数据迁移、接受事务、幂等和读回，功能开关关闭时维持旧前端。接口未有消费者前不要向用户开放。

验收：同 ID 并发只增加一次版本；同 ID 不同内容 409；两条不同 ID 按事务顺序编号；跨用户/旧 run 拒绝；接受失败零事件、零版本增长。

### 提交 2：旧操作阻断与恢复

接入 IntentChanged、动作准入版本检查及快照字段。尚未接修订模型时，可安全进入 waiting_input；不能以“已自动调整”发布。

验收：新输入先于动作准入提交，旧 MCP 调用计数为零；已准入旧操作能保存回执但不能推进后续；Worker 重启后仍受新版本阻断。

### 提交 3：修订与结算闭环

接入受控修订节点、已有结果引用、计划 CAS、applied 事务、最终结算门禁与累计预算。

验收：已完成部署不因新计划重发；未执行的旧发布被阻断；规划中连续两条补充后，只有覆盖最新版本的计划能执行；accepted 与完成并发无悬空消息。

### 提交 4：前端开放与端到端验收

完成 send 分支、同 ID 重试、版本化显示及暂停取消提示。先在配置开启的单测试环境使用。

至少执行这些确定性用例：

1. 正常运行中补充：原 task/run 不变，原流不中断。
2. 当前模型慢响应时补充：返回的旧写指令不执行。
3. 远端写操作在途时补充：保存真实结果，后续依据新要求。
4. 读取接口确认补充已接受，再重启 Worker：输入仍被处理。
5. POST 响应丢失、同 ID 重试：一条输入、一次版本增长。
6. 新计划提交响应丢失：恢复复用已提交计划。
7. 补充与最终结算竞争：只能“接受并处理”或“结束后拒绝”，不能接受后遗失。
8. 两个浏览器同时补充：输入按服务端顺序保留，单执行者消费。
9. 暂停后补充：不自动恢复；恢复后先处理补充。
10. unknown 后补充：不重发旧写入，不启动依赖新写入。
11. 权限不足时补充“直接发布”：不能扩大权限。
12. 切会话、旧请求迟到：输入和结果仍归原 run。
13. 多次修订：既有动作身份、产物和总预算不重置。
14. 补充与上下文超限：明确拒绝或等待，不静默丢掉约束。
15. 旧流式回答迟到：不覆盖新版本最终结果。

测试使用真实一次性 PostgreSQL、本地假模型和假 MCP。通过 barrier 精确控制准入、提交和返回时点；记录服务端真实调用计数，避免靠 sleep 判断竞态。必须覆盖跨进程和杀进程恢复，不能只测内存版 Hub。

## 10. 最小完整产品示例

原请求：“生成一个活动页面并发布。”

1. 接受版本 1；计划 p1 开始，页面已生成，发布尚未准入。
2. 用户补充：“先不要发布，把标题改成夏日活动。”
3. 输入日志和版本 2 原子提交，返回“已收到”。
4. p1 的发布以要求版本 1 准入，数据库发现当前版本 2，拒绝调用 MCP。
5. 执行者读取原目标、补充和已生成页面事实，提交 p2：修改页面，保留本地产物，不发布。
6. 提交 p2 时仍核对版本 2；成功才标 input applied，并显示“已按新要求调整”。
7. 按 p2 执行，最终结算仍比较版本；没有后续补充才结束。

如果第 2 步到达前发布已经准入，就必须收取或核实原发布结果，并如实说明可能已经发布。是否下线是另一项业务动作，按用户授权和工具能力处理，不能把“补充成功”宣传成自动撤回。

最终上线标准：新输入可靠归属、已接受消息不会丢失、旧决策无法准入新操作、已发生效果不丢账、同一动作不因计划修订重复执行。完整闭环通过后再开启前端默认补充模式。
