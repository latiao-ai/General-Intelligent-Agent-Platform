# 会话、任务与运行中补充输入：完整设计方案

日期：2026-10-23。状态：待实施设计。本次只新增设计文档，不表示接口、数据库迁移或执行能力已实现。

## 1. 已确认的目标与设计范围

交互采用三条明确规则：

1. 上一任务已经结束后，用户继续发送，创建新 task/run，并通过受控上下文承接历史。
2. 当前目标仍在执行或等待时，默认发送是对该目标的补充，沿用原 task/run。
3. 用户明确点击“新任务”时，首版新建独立会话，再创建任务；原任务不被隐式取消。

本方案面向独立售卖、通过 MCP 接入客户工具的通用中台，不绑定画布或视频业务。保留现有 Python、FastAPI、LangGraph、PostgreSQL、SSE、能力包与动作账本。

本方案统一产品行为和系统合同。`harness-live-input-implementation-plan.md` 是细化施工参考；前一份 `harness-reliability-implementation-plan.md` 提供恢复、租约、暂停和未知结果的基础验收。若旧设计描述与本方案的最终交互约定不同，以本方案为准。

首版不实现自动判断无关话题、多目标并行编排、任意业务回滚、多 Agent、语音或任意附件上传；接口保留后续扩展空间，不为本次引入新框架。

## 2. 当前项目基础与需要改变的行为

本次源码读取确认：

- 已存在 `ExecutionContext`、`Hub.execute_claimed_run()`、`admit_action()`、`settle_action()`、`commit_plan()`、`finish_run_owned()` 和 `run_snapshots`，应扩展这些入口。
- `send()` 仍在 S.running 时调用 interrupt，随后 postTask 新建任务；不支持真正的运行中补充。
- `POST /api/tasks` 每次创建新任务；没有 run_inputs、intent_version 或补充 API。
- 图仍为 plan → run_step 循环；不支持根据补充修订剩余计划。
- `prior_context()` 通过最近任务取产物与正文；同一会话并不自动继承完整计划或动作身份。
- `session_timeline()` 主要根据任务与内存记录寻找 live_task_id；paused 和 waiting 状态也需要明确的“当前目标”记录。

不能只改前端按钮或追加 messages：必须建立输入持久化、要求版本、修订循环、动作准入和收尾的完整闭环。

## 3. 用户可见行为

### 3.1 第一次发送

用户输入 → 建立或验证会话 → 原子创建 task/run 并登记会话当前目标 → 执行者领取 → 读取原要求及已有输入 → 模型规划 → 计划提交 → 工具执行 → 结果结算。

普通新任务只复用用户有权读取的历史、资料和产物引用；旧会话中的历史授权不能自动授予本次写入。

### 3.2 上一任务结束后再次发送

建立新 task/run，保留 session。新任务记录独立目标与执行预算。模型可通过历史和产物引用理解“刚才那个页面”；这不等于复活旧 run。

若存在多个候选产物，不能默认覆盖任意一个：界面选定对象或模型提出必要澄清。新 run 不复用旧 run 的执行租约或重放旧动作；如需编辑旧产物，作为新任务中显式授权的业务操作。

### 3.3 当前目标执行中再次发送

保存为原 run 的输入记录，页面显示“已收到，正在调整”。无需取消旧任务，不创建新执行者；下一决策边界纳入补充。旧版本的新动作被拒绝；在途动作记录真实结果后再处理补充。

“发送成功”不表示改动已完成。界面分别呈现待确认、已收到、正在调整、已纳入计划、需要补充信息、业务完成。

### 3.4 明确新建独立任务

首版“新任务”创建新 session，旧目标保持原状态。新页面不偷偷继承旧任务草稿或选中产物；用户明确引用的资料仍需归属校验。

首版一个会话只关联一个未结束目标；跨会话任务是否可以并发，由现有执行配额控制。并发不足时排队，不以取消旧目标腾位置。

## 4. 领域对象与身份

| 对象 | 定义 | 本次约束 |
| --- | --- | --- |
| session | 一条连续对话与历史范围 | 至多一个 current goal；切换不会取消 |
| task | 持续业务目标 | 执行中补充不新建 |
| run | 目标的一次执行尝试 | 暂停恢复及本轮补充沿用；最终结束后不偷偷复活 |
| input | 用户对指定 run 的一次补充 | 原文与身份不可变，处理状态单独投影 |
| decision | 一次模型决策 | 固定它实际读取的输入版本 |
| plan | 经校验并提交的执行方案 | 新版本追加，旧版本不能覆盖重写 |
| action / attempt | 业务操作身份 / 真实调用尝试 | 补充和计划改版不能重置已发生效果 |

保留三种独立版本：lease_epoch 判断执行资格；intent_version 判断要求是否过时；plan_version 标识具体执行方案。

客户端只提供目标 ID、请求 ID 与用户内容；owner、租户、版本、当前计划、操作身份和授权信息由服务端核实或生成。

## 5. 生命周期与消息路由合同

“是否占用当前目标”不能直接用现有 ACTIVE_RUN_STATUSES 或 TERMINAL_RUN_STATUSES 反推。waiting_input、waiting_approval、paused、needs_reconcile 虽然执行可能退出，业务目标仍未最终结束。

| 状态/条件 | 占用会话当前目标 | 默认发送 | 是否自动推进 |
| --- | --- | --- | --- |
| queued/running/reconciling | 是 | 补充 | 有执行资格且满足安全边界才推进 |
| waiting_input | 是 | 补充 | 输入足够后可按下述受控唤醒继续 |
| paused | 是 | 补充 | 不恢复，用户明确点击继续 |
| waiting_approval | 是 | 补充 | 新方案重新校验批准，不自动扩权 |
| needs_reconcile | 是 | 补充 | 先核实外部效果，不启动依赖的新写入 |
| interrupted | 是 | 补充 | 原恢复规则决定，需要时用户点击继续 |
| cancel 已请求 | 是，直到停止结局确定 | 拒绝全新补充，显示正在停止 | 禁止新业务动作 |
| succeeded/failed/cancelled 且无未核实效果 | 否 | 建新任务 | 独立执行 |

waiting_input 的受控唤醒：输入保存事务仅在没有有效执行者、没有未知动作、无 pause/cancel 的情况下将原 run 置 queued；仍由 claim 领取，不在请求里另开执行协程。有有效执行者时只通知它。paused、approval 与 unknown 不使用这项自动唤醒。

重复请求先验证访问权并查原接受回执，再判断当前是否结束；原请求重放可返回原回执，全新请求才按当前状态拒绝。

现有精确停止命令逻辑应统一到服务层控制路由；首版以停止按钮为主要控制入口。命中既有精确停止表达时走取消流程，不作为普通补充接受；不在前后端各写一套含糊关键词规则，普通业务文本不被随意当停止。

## 6. 数据模型与约束

### 6.1 会话当前目标

在 sessions 增加 active_task_id、active_run_id，可为空。它是路由依据，不是执行事实的替代品；数据库 run 状态和租约仍是执行权威。

创建默认任务时锁会话行，检查 active_run 的实际状态。若已有未结束目标，POST /tasks 返回 409 active_task_exists 并带当前可访问的 task/run ID，不能静默建第二个。

最终收尾在同一事务清理 active 指针，条件必须仍指向本 run，防止旧收尾清掉新目标。needs_reconcile 保留指针。兼容旧库时只在唯一可判定目标时回填；多个候选先要求明确选择或治理，不猜“最新一条”。

### 6.2 运行与输入

建议字段：

```text
runs:
  intent_version BIGINT NOT NULL DEFAULT 1
  processed_intent_version BIGINT NOT NULL DEFAULT 0

run_inputs:
  input_id TEXT PRIMARY KEY
  run_id TEXT NOT NULL
  input_seq BIGINT NOT NULL
  request_id TEXT NOT NULL
  message TEXT NOT NULL
  payload_hash TEXT NOT NULL
  intent_version BIGINT NOT NULL
  created_at ...
  UNIQUE(run_id, request_id)
  UNIQUE(run_id, input_seq)

run_input_processing:
  input_id TEXT PRIMARY KEY
  status TEXT NOT NULL
  processed_plan_version BIGINT
  error_code TEXT
  updated_at ...
```

补充从 input_seq 1 开始；初始任务要求仍保存在 tasks.message，完整要求由原文加顺序补充构成。处理状态可为 accepted、applied、needs_input、closed_unapplied；最后一项用于取消后保留未应用输入的真实结局。

接受输入时分配 seq 并增加 intent_version；processed 只在计划提交时推进。原文不改写、不被摘要覆盖。

plans 增加 intent_version、base_plan_version、decision_id。actions/attempts 增加 decision_intent_version；run_snapshots 保存相关版本和输入覆盖范围，恢复时以最新 runs/input 日志核对。

decision 建议保存最小持久记录：decision_id、run_id、purpose、intent_version、base_plan_version、input_upto_seq、状态、用量和时间。输入大内容通过受控引用关联，不保存凭据或隐藏推理正文。

### 6.3 历史投影与去重

run_inputs 是补充事实源，processing 是可重建投影。时间线按服务端顺序展示补充，明确所属 run。

若为兼容摘要逻辑向 messages 投影，增加 origin_input_id 唯一关联，确保只投影一次。模型上下文按来源去重，不能一边读 messages、一边再拼同一 run_inputs 导致重复。

schema 变更采用可重复迁移；旧库显式补列，不能仅改 CREATE TABLE。旧计划与动作不得批量改 ID 或重新执行。

## 7. API 合同

### 7.1 创建任务

继续使用 `POST /api/tasks` 与现有 Idempotency-Key。含 session_id 时必须检查归属、当前目标和创建冲突。无 session_id 时创建新会话。

新任务创建应原子提交用户消息、task、task_meta、run、会话 active 指针和请求回执，或以同等可恢复事务设计封住创建中断窗口。不要让当前先分多次提交的流程成为两个新任务争抢 active 指针的入口。

### 7.2 发送补充

```http
POST /api/runs/{run_id}/inputs
Content-Type: application/json
```

```json
{"request_id":"稳定随机ID","message":"先不要发布，把标题改成夏日活动"}
```

事务成功返回 202：

```json
{
  "input_id":"inp_...",
  "task_id":"...",
  "run_id":"...",
  "input_seq":1,
  "accepted_intent_version":2,
  "replayed":false,
  "processing":{"status":"accepted","processed_intent_version":1}
}
```

accepted_intent_version 属于原回执，不随重试改变；processing 可返回当前状态。request_id 相同且内容相同重放，内容不同返回 409。校验归属必须早于回执重放，避免借幂等读取他人记录。

新增 `GET /api/runs/{run_id}/inputs?after_seq=...&limit=...` 用于断线回读；默认有界分页。现有 GET run/task 增加当前 plan/intent/processed 版本、待处理输入数和执行控制状态。

### 7.3 时间线与错误

session_timeline 增加 current_task_id/current_run_id 与 input_groups。保留 live_task_id 兼容旧客户端，但不能把 paused 当“没有当前任务”。

| 状态码 | 语义 |
| --- | --- |
| 404 | 目标不可访问或不存在 |
| 409 active_task_exists | 同一会话已有未结束目标 |
| 409 input_request_conflict | 同请求 ID 不同内容 |
| 409 run_finished/run_cancelled/run_not_current | 全新补充目标不可接收 |
| 422 | 内容、ID 或长度不合法 |
| 429 | 速率、积压或总量限制 |
| 503 | 接受事务暂未能成功 |

已结束的原请求重放仍返回原接受结果。接收失败不能自动转发成新任务。

## 8. 并发与事务边界

统一锁顺序：session → task_meta → run → 输入/plan/step/action → event。只需锁 run 的执行路径不反向取得 session 锁；涉及会话创建和最终指针清理的路径从 session 开始。沿用事件 seq 的原子分配规则，禁止形成逆序。

补充接受事务执行：验证归属 → 锁目标 → 幂等检查 → 状态检查 → 插入输入 → 版本递增 → processing → input_received 事件 → 必要时 queued 唤醒 → 提交。内存通知发生在提交以后，失败不影响接受事实。

现有仓储方法通常自己借连接，必须提供事务内辅助函数；不能在一个外层事务中调用另一个独立提交方法，声称它们原子。

模型请求与 MCP 网络等待均不持数据库锁。API 不持有 Worker 租约，合法用户可在不同进程追加输入；只有持租约执行者能消费并提交计划或动作。

## 9. 执行循环与计划修订

### 9.1 决策输入

执行者按一致快照读取原要求、补充范围、当前版本、最近计划、已完成回执、在途/未知效果、产物引用、权限和预算，生成 decision_id。

模型实际读取的版本作为 decision_intent_version，不能读旧内容却贴最新版本。新补充优先于普通历史，关键约束不能被静默截断；容量不足时保留原文并进入等待。

### 9.2 图流程

计划阶段包括读取要求、模型规划、校验、显式提交计划屏障。执行每步后检查是否有新输入；出现 IntentChanged 转入修订分支。步骤走完进入 finalize_gate，门禁发现新输入则回修订，否则才原子结算。

修改 `graph.py` 增加 consume_inputs、revise_plan、persist_plan、finalize_gate。`main.py` 的单个 plan_committed 布尔值改为按计划版本管理，不依赖消费流事件的先后作为持久化屏障。

### 9.3 计划提交

扩展 commit_plan 为 CAS：租约有效、expected_intent_version 等于当前要求、base_plan_version 等于当前计划，才插入下一版。与更新 runs.plan_version、processed_intent_version、input applied、快照和 plan_revised 事件同事务。

现有 ON CONFLICT UPDATE 不能覆盖已提交历史计划。相同计划提交身份与相同内容可幂等重放，不同内容冲突；提交响应丢失后读回，不多加一版。

若规划过程中再次收到补充，旧候选不生效，按最新输入重新规划。可以合并本次快照内多条补充，但保留顺序与全部原文。

### 9.4 已完成操作与剩余步骤

- 已完成动作和输出不可被模型重写。
- 旧未执行步骤标 superseded，保留记录。
- 新步骤 ID 在 run 内唯一，例如 p2_s1，不能覆盖旧 s1。
- 新计划可引用经过归属和成功状态校验的历史步骤输出。
- 同一已有外部效果通过原 action/产物引用复用，不能因计划升级而重新发送。
- 若确实再次生成或覆盖，是新的业务动作，必须经过当前权限与批准检查。
- 不能仅凭参数 hash 相同自动认定两次操作是同一个业务意图。
- 未知效果未核实前，不启动依赖它的新写入。

预算跨修订累计。总超时、请求次数和工具次数不因补充重置。达到修订预算后进入明确等待，保留尚未应用的输入。

## 10. 旧要求操作门禁

admit_action 增加 expected_intent_version，并由已提交计划和受控执行上下文提供；模型不能自报。

事务内必须同时满足：有效租约、无停止控制、计划仍是当前版、决策要求版本等于数据库当前版，且该版已纳入计划。失败抛 IntentChanged，作为控制流回修订，不写成业务失败、不自动补偿。

初版阻断所有过时的新业务调用，不能只凭 readOnlyHint 放过 unknown 或可能收费的调用。核实在途结果走专门受控的查询合同，不靠伪造已处理版本放行。

补充与准入锁同一 run：补充先提交则旧操作不能准入；旧操作先准入则视为在途，允许收取回执。可在实际发送前再检查一次作为优化，但不能承诺消除跨系统发送窗口。

结算与准入规则不同：已准入动作只要执行资格仍合法，结果必须保存，即使 intent_version 已变化。回执带来源版本，不能将 processed 版本回退或擅自继续旧计划。

失去租约的迟到执行者仍禁止提交，由当前执行者核实恢复；要求变化本身不应造成已发生效果丢账。

## 11. 最终回答、收尾与 SSE

流式事件增加 run_id、decision_id、intent_version。新要求提交后，旧 answer_delta 标为调整前的过程；不同决策不能拼成同一条最终回答，旧响应不能覆盖新最终结果。

持久事件至少包括 input_received、intent_update_started、plan_revised、input_needs_clarification。继续使用原 task SSE 与 seq；跨进程有界轮询补发作为可靠回读，不要求本轮引入消息总线。

最终收尾事务检查：租约有效、计划与最新 intent 一致、最新输入已处理、必需步骤确定完成、无未知效果、控制状态允许结束。随后原子更新 run/task/快照、唯一完成消息、完成事件和 session active 指针。

若新补充先提交，收尾返回 IntentChanged；若收尾先提交，全新补充返回 run_finished。两种都可解释，不允许“返回已接收却无消费者”。

完成消息按 run 的唯一结算身份去重。事件不能在事务外先发再写库。

## 12. 前端设计

为当前视图保存 session_id、task_id、run_id、current_goal_status；每条发送保存 request_id、目标、文本和发送状态。

输入框附近显示“补充当前任务”或“发送新任务”的当前模式；保留“新任务”和“停止”入口。paused 显示“补充会保存，点击继续后处理”，needs_reconcile 显示当前需要核实的事实。

运行中 send 改为补充，不 interrupt、不重置步骤区、不替换 task SSE。连续消息按各自 request_id 排序显示。发送限流不删除草稿；网络未知保留待确认，按原 ID 回读或重试。

最终结束后的正常发送才 startNewTurn、postTask 并连接新的 SSE。发现服务端与本地状态不一致时回读路由状态，要求用户明确选择；不自动把失败补充转换成新任务。

切换会话关闭当前观察连接即可，原任务继续；请求结果写回发送时冻结的会话，旧请求不得污染新视图。

## 13. 权限与业务确认

所有输入、回读、事件、计划、历史输出引用都验证租户和用户归属。工具目录按可用权限过滤；执行准入再次授权。

补充消息是意图变化，不是通用批准。新增外部效果、对象或付费参数需按已有授权规则匹配；旧授权是否覆盖新计划必须显式验证。只读用户补充“直接发布”也不能获得发布能力。

即使客户 MCP 自己校验权限，中台仍记录本次凭据主体与操作范围；不要依赖模型从业务文本中提供用户 ID 或凭据。

## 14. 异常、控制与恢复

| 场景 | 处理 |
| --- | --- |
| 输入接受前故障 | 无接受事实，原 request_id 重试 |
| 提交后响应丢失 | 返回原 input 与接受版本 |
| 接受后 Worker 崩溃 | 新执行者由输入日志与最新版本恢复 |
| 新计划提交后响应丢失 | 读回提交身份，不重复修订 |
| 新要求无法理解 | needs_input，旧动作仍被版本门禁阻断 |
| 暂停后补充 | 保存但不恢复，显式继续后先消费 |
| 停止请求与补充并发 | 按事务顺序决定接受，停止后不继续执行 |
| 在途写入结果未知 | needs_reconcile，不盲重发，不自动补偿 |
| 旧 Worker 失租迟到 | 拒绝推进，由现持有者核实 |
| 模型/输入预算超限 | 明确等待或拒绝新请求，已接受日志保留 |

恢复时以数据库最新 intent 校验快照；快照较旧不代表可以继续旧计划。已结束任务保留未应用输入的真实处理结局，不把所有 accepted 批量写 applied。

## 15. 模块与修改点

| 模块 | 修改职责 |
| --- | --- |
| api.py | 输入 schema、POST/GET inputs、创建冲突、状态回读 |
| api_hub.py | append 输入、当前目标路由、统一唤醒、恢复与收尾 |
| persistence.py / pg_store.py | 迁移、原子接受、当前目标、CAS 计划、版本准入、原子完成 |
| execution_control.py | 独立 IntentChanged 与要求版本上下文，保留 LeaseLost/ControlStopped |
| run_inputs.py（新增） | 输入快照、顺序组装、覆盖范围、历史去重 |
| schemas.py / nodes.py | 修订输出、可信历史引用、边界分支 |
| graph.py / main.py | 显式提交屏障、受控修订、收尾门禁、累计预算 |
| ledger.py | 准入决策版本、既有动作身份、来源版本回执 |
| context.py | 当前要求优先、输入引用、超预算可见 |
| events.py / messages.py | 新事件与通用等待/调整回执 |
| web/index.html | 三模式路由、补充显示、版本化回答、原目标连接 |

## 16. 交付顺序与发布策略

1. **协议与基础回归**：确定最终状态、当前目标、停止和唤醒合同；跑通恢复/租约/未知结果基础。
2. **数据库与服务接口**：幂等迁移、会话目标、输入日志、接受事务、回读；补充功能开关保持关闭。
3. **执行门禁**：要求版本进入准入与收尾；暂未修订时安全等待，不能继续旧计划。
4. **修订闭环**：显式计划提交、历史结果引用、CAS、预算、恢复与 applied 状态。
5. **前端接入**：默认补充、明确新任务、网络回读、版本化回答及控制提示。
6. **故障验收后开放**：小范围启用，再开放默认行为；记录未支持业务合同。

升级时先排空旧执行者，再进行迁移并统一升级 API/Worker。旧 Worker 不识别 intent，不能与新 Worker 同池处理可补充任务。禁止开启接收能力后只回滚执行门禁；回退前关闭新输入、保留日志并暂停存在未消费输入的目标。

## 17. 验收矩阵

| 类别 | 必须通过的断言 |
| --- | --- |
| 首次请求 | 单任务单运行、计划先提交、正常结果 |
| 完成后的第二次请求 | 同 session 新 task/run；只承接授权历史与产物 |
| 执行中的第二次请求 | 原 task/run 不变；没有隐式取消 |
| 明确新任务 | 新 session；旧目标不受影响 |
| 两客户端创建 | 会话锁保证只产生一个当前目标 |
| 补充幂等 | 同 ID 只一条输入一次版本增长，不同内容冲突 |
| 跨用户/旧 run | 拒绝且不泄露输入 |
| 新输入先于准入 | 旧 MCP 调用计数零 |
| 旧调用在途 | 保存真实回执，后续不沿旧计划执行 |
| 规划时再次补充 | 旧候选提交失败，只有最新版本计划生效 |
| 修订复用 | 已成功的外部效果不重复，旧输出仍可引用 |
| 最终结算竞争 | 接受必处理或明确拒绝，无悬空输入 |
| 暂停/审批/未知 | 正确保留控制与授权边界，不自动越过 |
| waiting_input 唤醒 | 原 run 原子 queued，一次领取，无第二执行者 |
| 进程终止 | 原身份、顺序和版本恢复，远端写入不盲重发 |
| SSE 断线重连 | 输入事件不丢、不重复展示；旧回答不覆盖新版本 |
| 连续补充 | 全部按序保存，预算累计，不无限创建模型任务 |
| 历史多产物 | 指代不清时要求选择，不误改其它产物 |

测试使用一次性 PostgreSQL、本地假模型与假 MCP，记录远端真实调用次数。并发用 barrier 固定接受、准入和结算窗口；杀进程测试独立服务保留账本。没有 PG_TEST_DSN 而 skipped 不算通过。

## 18. 完整示例与产品取舍

用户先说“生成活动页并发布”：新 task/run，要求 v1、计划 p1。页面生成后，用户补充“先不要发布，标题改成夏日活动”：原 run 记录 input1，要求升至 v2。旧 p1 发布准入被拒绝；新计划 p2 引用已生成页面，修改标题并停在本地。p2 提交后显示“已按补充调整”，全部完成后结束并清理会话当前目标。

之后用户说“把这个页面发布”：因为旧目标已结束，创建同会话的新 task/run，显式引用该页面并按授权规则发布。用户若在制作期间点击“新任务”分析另一份资料，则新建独立会话，原制作继续。

收益是连续协作、真实进度复用、旧决策阻断和可靠恢复。代价是数据库事务、版本与前端状态更复杂；修订会增加模型成本和延迟；已发出操作仍需收取或核实。首版以明确目标和有限修订控制这些代价，不把所有消息自动合并，也不宣称可以撤回所有在途效果。
