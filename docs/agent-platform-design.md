# 通用 Agent 执行中台设计方案

日期：2026-10-19。状态：设计提案，尚未实施。

## 1. 定位与设计结论

中台接受用户目标，生成可校验的计划，从已接入的能力中选择工具，通过 MCP Server 或本地适配器执行，观察结果并继续、调整或暂停任务，最后返回可核实的结果。

网页搜索、生成 HTML、部署是一个业务能力包。执行内核不应要求任务一定有网页来源、报告、HTML 或访问地址。以后接入工单、文件处理、数据库查询等能力，应主要增加工具配置和必要适配器。

继续使用 Python、FastAPI、SSE、LangGraph。先以模块化单体落地，本地开发保留 SQLite；正式多用户部署采用 PostgreSQL，并将 API 与执行 Worker 分成两个运行角色，仍共用一个代码库。初期用数据库任务表领取工作，暂不强制引入 Redis、独立消息队列、向量数据库或多 Agent。

第一版按顺序执行步骤，但数据模型记录依赖；后续有明确需求时再开放独立步骤并行。任务恢复、调用记录、参数绑定、权限隔离应在并行执行之前完成。

## 2. 当前代码基础与具体缺口

| 位置 | 当前实现 | 建议变化 |
|---|---|---|
| `app/graph.py` | `plan → run_step` 循环，compile 未配置 checkpointer | 保留 LangGraph，增加校验、观察、重新规划、等待与恢复节点 |
| `app/nodes.py` | 规划提示词包含大量检索与部署规则；无效工具被过滤；执行时未知工具可跳过 | 提取业务规则；计划整体校验；关键步骤无效就修复或暂停 |
| `app/schemas.py` | 步骤只有 tool、args、why；状态包含 documents/report/deployment | 增加步骤 ID、输入绑定、预期结果、计划版本；业务结果迁入通用输出 |
| `app/tools.py` | 本地工具和单个远端 MCP 的动态工具；远端名加 `mcp__` 前缀 | 建立统一注册表，工具名包含 server_id；独立业务能力包 |
| `app/mcp_adapter.py` | 全局环境变量定位一个 Server；每次调用新建连接、列举工具；结果偏向 JSON 部署对象 | 按 Server 配置连接；保留描述、输出 schema 和多种返回块；部署逻辑单独适配 |
| `app/main.py` | 每轮创建内部 ID；逐节点写 state.json；整个任务固定 300 秒超时 | 由运行服务传入 run_id；使用持久化图；区分步骤超时、等待和总体预算 |
| `app/api_hub.py` | 进程内 asyncio.Task 与订阅队列；SQLite 保存事件和最终状态 | 拆为任务服务、Worker 调度与事件流服务；数据库为权威状态 |
| `app/store.py` | SQLite、线程锁、会话/消息/任务/事件/摘要；初始化时恢复孤儿任务 | 增加迁移和仓储；恢复由调度器按租约判断，不能每个 API 实例启动时改写全部运行任务 |
| `app/context.py` | 历史消息的粗略 token 预算 | 覆盖规则、工具、状态、证据、历史、当前请求的完整预算 |
| `app/memory.py` | 在线滚动摘要，覆盖式 facts | 有来源的摘要；显式纠错、删除；普通整理异步化 |
| `app/profile.py` | 一份全局 Markdown 画像 | 按用户/项目存储；Markdown 保留为导入导出格式 |
| `app/api.py` | 任务、会话、画像、部署接口，没有身份归属依赖 | 注入身份，统一对象权限；部署按钮走通用执行链 |
| `app/web/index.html` | 聊天、阶段流、任务回放 | 保留交互基础，增加计划版本、步骤状态、等待与恢复呈现 |

当前中断后可查历史、文件和事件，并不等于可以可靠续跑。当前通用 MCP 工具确实会按 inputSchema 校验参数，但本地工具尚无统一的同等级校验入口；计划整体的数据依赖也没有校验。

另外，发现阶段只保留远端工具 name 和 inputSchema，实际 description 未传给规划器。工具名称不能完整表达能力，这应优先修复。

## 3. 核心对象与边界

### 3.1 身份、会话、任务、运行

- tenant：组织或工作空间。单人阶段可固定为 default，但字段和过滤路径保留。
- user：真实登录用户。身份由服务端验证，客户端不能任意指定。
- project：一组共享资源、工具绑定、约定及权限。
- session：对话容器；可以关联多个任务。
- task：一个持续目标，如“生成活动页面并发布”。
- run：目标的一次执行尝试，记录图运行、计划版本、预算和执行状态。
- step：计划中的逻辑步骤，ID 在该计划版本内唯一。
- action：一次具体业务操作的稳定身份。相同操作的安全重试沿用同一 action_id。
- attempt：一次真实调用尝试。每次发送请求都单独记录。

建议先完成 task/run 分离，避免当前外部 task_id 与内部产物目录 ID 的语义混用。旧任务可一对一迁成 task + run，旧目录通过 artifact 存储引用继续访问，无需移动原文件。

继续一个暂停运行使用同一 run_id；用户发起独立新要求创建新 task/run；运行中修改目标则写入新指令事件，停止领取旧计划新步骤，待在途动作确定后发布新计划版本。旧版本和执行结果保留。

### 3.2 六类数据

1. 原始记录：用户消息、计划变更、工具调用和回执。
2. 执行状态：步骤、动作、等待原因、检查点。
3. 产物与大结果：文件、完整 JSON、正文和工具内容块。
4. 会话摘要：帮助理解历史的派生视图。
5. 长期记忆：用户偏好、项目约定、稳定事实。
6. 外部业务事实：由业务系统确定，必要时重新查询。

模型提出的“部署成功”不能代替工具回执。摘要不能代替执行状态。长期记忆中的旧授权不能自动授权新操作。

## 4. 模块划分

建议划分为八个模块，在同一工程中通过接口调用：

| 模块 | 责任 | 主要接口 |
|---|---|---|
| TaskService | 建任务、追加指令、暂停、取消、恢复 | create_task / append_instruction / resume |
| ToolRegistry | Server 注册、工具发现、版本与权限过滤 | refresh / search / get_definition |
| Planner | 目标理解、工具选择、生成计划、有限修复 | create_plan / revise_plan |
| Executor | 解析输入、校验、调用、记录动作、恢复 | execute_step / reconcile_action |
| PolicyService | 身份、资源范围、工具许可、授权匹配 | authorize / check_action |
| ContextBuilder | 按用途组装完整模型输入 | build_for_plan / build_for_observe |
| MemoryService | 偏好、事实、来源、纠错、遗忘 | retrieve / remember / forget |
| ArtifactService | 文件与大结果持久化、授权读取 | put / get / verify_hash |

EventService 提供横切能力：事务内记事件、按游标回放、实时通知。模型调用仍通过现有 services.py 封装，增加调用用途、模型版本、用量和超时记录。

## 5. 多 MCP Server 与统一工具体系

### 5.1 Server 配置

一条 Server 配置建议包含：

```json
{
  "server_id": "publishing",
  "tenant_id": "tenant_1",
  "transport": "streamable_http",
  "endpoint": "https://mcp.example.com/mcp",
  "credential_ref": "secret:tenant_1/publishing",
  "enabled": true,
  "connect_timeout_seconds": 10,
  "call_timeout_seconds": 90,
  "concurrency_limit": 4
}
```

以上数值是初始配置示例，不是通用性能结论。密钥通过 credential_ref 读取，不进入规划上下文或事件正文。用户个人账号授权时，凭据绑定还应包含 user_id，连接隔离到相应主体。

本地开发继续支持 stdio；生产 stdio 由管理员配置命令、运行目录和资源限制，不接受模型生成任意命令。SSE 与 Streamable HTTP 保留现有传输适配能力。可配置端点由管理员接入，不能由模型随意创建连接目标。

### 5.2 工具定义

中台内的稳定键使用 `mcp:publishing:deploy_site`、`local:html:render`；若模型工具 API 对名称字符有限制，再生成短别名并保存映射。

工具定义需要保留：server_id、原始名称、title、description、input_schema、output_schema、annotations、schema_hash、发现时间、可用状态，以及本地治理元数据。

本地元数据包括 capability_tags、effect_type、timeout_policy、retry_policy、credential_binding、result_adapter、reconcile_strategy、policy_version。effect_type 可分 read、write、destructive、unknown。缺少行为信息时保持 unknown，不根据名字猜测可重试性。

远端 annotations 是参考信息，需结合可信 Server 配置与适配验证。只有经确认的业务幂等能力才能支持自动重试写操作。依据：[MCP Tools 规范](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。

### 5.3 发现和选择

Server 接入时发现并保存工具完整描述；后台按配置刷新，支持时响应工具列表变化通知。缓存按 tenant、Server、凭据主体与工具版本隔离；Server 故障应有短期失败缓存和可见状态，避免每轮重复等待。

少量工具时，规划器可读取全部有权工具。工具增多后，先按项目绑定、权限、标签、关键词选择候选，再让模型读取候选的完整 schema。初始候选数量可设为 10–20，评测后调整。

规划器找不到能力时可请求扩大检索，或明确等待接入；不能把“能力缺失”退回网页搜索。工具发现失败要区分无权限、不可用和确实不存在。

计划引用工具版本。执行前再检查当前权限和 schema_hash；发生不兼容变化时修复计划，不静默换成另一个工具，也不静默跳过。

### 5.4 连接与统一返回

第一版可以按调用建连接，但不必每次重新发现全量工具。多 Server 和并发增长后再按 Server/凭据主体管理连接生命周期，具备关闭、断线重建、并发上限与空闲释放。

通用返回包装为：

```json
{
  "action_id": "act_1",
  "attempt_id": "attempt_1",
  "tool_key": "mcp:publishing:deploy_site",
  "execution_status": "succeeded",
  "data": {"site_id": "site_1", "url": "https://example.com/site_1"},
  "content_refs": [],
  "artifact_refs": [],
  "external_receipt": {"site_id": "site_1"},
  "error": null,
  "verification_status": "verified"
}
```

execution_status 与 verification_status 分开：工具返回成功，但业务结果尚未核实，不能合成“已验证”。验证策略来自工具适配器与可执行后置条件。

保留原始 MCP 结果；支持 structuredContent、多个文本块、图片、音频、资源链接等。没有 JSON 不应被通用适配器判为部署失败。声明 outputSchema 时验证结构化结果；失败时保留回执，对有副作用的动作不能因为解析失败就重发。

部署专用 result_url、base64 组装、哈希验证移到 publishing 能力包。通用 MCP 客户端不应给任意带 files 字段的工具自动注入当前 HTML。

## 6. 计划与步骤输入输出

### 6.1 计划结构

```json
{
  "task_id": "task_1",
  "run_id": "run_1",
  "version": 1,
  "goal": "生成活动页面并发布到指定服务器",
  "completion_criteria": ["HTML 产物可读取", "部署回执包含站点标识"],
  "steps": [
    {
      "step_id": "s1",
      "tool_key": "local:html:render",
      "tool_schema_hash": "schema_hash_a",
      "depends_on": [],
      "inputs": {"title": {"literal": "活动页"}},
      "why": "生成可发布文件",
      "postconditions": [{"type": "artifact_exists", "output": "artifact_id"}]
    },
    {
      "step_id": "s2",
      "tool_key": "mcp:publishing:deploy_site",
      "tool_schema_hash": "schema_hash_b",
      "depends_on": ["s1"],
      "inputs": {
        "artifact_id": {"ref": {"step_id": "s1", "pointer": "/artifact_id"}}
      },
      "why": "发布页面",
      "postconditions": [{"type": "field_present", "pointer": "/site_id"}]
    }
  ]
}
```

示例中的 artifact_id 是中台发布适配器的输入。若原始远端 schema 要求 files，则由该适配器读取授权产物、校验哈希并生成 files；不能假装远端原生支持 artifact_id。业务适配器和原始工具应分别注册，明确其参数合同。

### 6.2 输入绑定

支持三种明确输入：literal 字面量、前序步骤输出 ref、用户授权产物引用 artifact_ref。引用使用受限 JSON Pointer 解析；禁止 eval、任意 Python/模板表达式和隐式文件路径读取。

规划时校验引用目标、依赖顺序和可推断类型；执行时解析出最终参数，再对完整实参进行 schema 校验。后续工具若需要自然语言处理前序结果，可单独安排受预算约束的模型转换步骤，保留其输入来源和结构化输出。

每个工具只能读取显式输入及受控 ExecutionContext，例如身份、凭据句柄、产物读取接口；不再随意修改整个 AgentState。兼容期使用适配器将旧工具的 state 增量转换为通用结果。

### 6.3 计划校验与修复

校验内容：工具存在且有权限；步骤与引用合法；无环；必要输入具备；参数/绑定类型合理；不超过预算；业务后置条件可执行；计划版本及工具版本有效。

不能像当前 _valid_steps 一样丢掉未知工具后直接执行剩余步骤。首次校验失败可将结构化错误交给模型修复，初始最多两轮；仍失败进入 waiting_input 或 failed，给出具体缺口。

预期条件由受控验证器执行，模型可选择已注册验证类型，不能生成任意验证代码。复杂任务的目标是否满足，可由模型辅助判断，但执行成功、资源创建等结论必须有证据。

## 7. 执行闭环、状态与重新规划

推荐图节点职责：load_context 加载身份、状态与引用；select_tools 选候选；plan 生成计划；validate_plan 校验；prepare_step 解析参数和策略；execute_step 调用；observe 校验结果；replan 调整剩余计划；finalize 返回结果。等待用户输入或授权时持久化挂起。

第一版依次执行满足依赖的步骤。常规成功只进行程序判定，不必每一步调用模型。只有输入歧义、结果改变后续路线、需要内容转换或确定失败需要新方案时才调用模型。

运行状态建议：queued、planning、running、waiting_input、waiting_approval、paused、reconciling、succeeded、partially_succeeded、failed、cancelled。取消请求另存 cancel_requested_at，避免在途操作未确定就宣称全部取消。

步骤状态建议：pending、ready、running、succeeded、failed、blocked、unknown、cancelled、superseded。动作调用状态另行记录 prepared、in_flight、succeeded、failed、unknown。

重新规划生成新版本，记录原因、基准事件与沿用结果。已完成步骤不可改写；旧计划未执行步骤标为 superseded。若修改动作目标或最终参数，创建新 action_id 并重新匹配授权。

设置可配置限制：计划最大步数、模型调用次数、重新规划次数、总费用、单步超时和活跃执行期限。等待用户的时间不占单次模型调用或工具超时；运行恢复时继承已消费预算，不因重试归零。现有 8 步、300 秒限制转为可配置策略。

普通读操作遇到暂时故障可退避重试；业务拒绝或参数错误返回结构化原因；写操作超时则先核实。未知结果的写操作不能通过换工具或重新规划绕开核实。

## 8. 调用账本与断点恢复

### 8.1 检查点与账本的分工

LangGraph 持久化检查点保存图执行位置与运行状态。以 run_id 作为图 thread_id；同一个 run 的恢复复用它，同一 session 的新目标使用新的 run_id。SQLite 用于开发持久化，PostgreSQL 用于正式部署，启动时管理持久化组件生命周期。依据：[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。

checkpointer 是图状态权威来源；actions/attempts 是外部调用事实的权威来源；runs 和 steps 表是业务状态与界面查询视图，通过版本化、幂等更新同步。检查点与账本可能不在同一事务，必须设计重放核对，而不是假设它们原子提交。

### 8.2 每个动作的执行协议

1. 解析最终参数、校验权限，生成规范化参数哈希。
2. 事务写 prepared 动作、关联步骤和执行意图事件；稳定记录 action_id、目标、工具版本、幂等键及授权引用。
3. Worker 确认租约与执行版本，写入 attempt、标记 in_flight，再发送远端调用。数据库事务不跨远端网络等待。
4. 返回后保存原始结果及其引用、回执、动作状态、步骤结果和事件；大结果先写不可变对象，再提交引用。事务未引用的对象由后台清理。
5. 图节点返回并保存检查点。图恢复再次进入节点时先查 action：已成功则重用结果；unknown 则核实；从未发送且策略允许才发起调用。

同一逻辑动作安全重试沿用幂等键，新增 attempt。键应基于稳定动作身份和最终参数版本；参数变化不能继续复用旧键。只有远端实际支持时才把键传入对应参数或约定通道，不能随意增加 schema 不接受的字段。MCP 请求 ID 不能代替业务幂等键。

### 8.3 三个崩溃窗口

| 崩溃时刻 | 恢复行为 |
|---|---|
| prepared 尚未发送 | 确认可领取、授权仍有效后执行 |
| 已发送，但回执未落库 | 标记 unknown，通过幂等键或业务查询核实 |
| 已保存成功，但图检查点未推进 | 读取动作结果，不重复远端调用，补推进检查点 |

远端没有幂等和核实能力时，无法保证写操作仅执行一次。该步骤应保持 unknown，要求外部核实或人工处理；系统必须如实报告结果不确定。

### 8.4 取消、暂停与补偿

暂停：停止派发新动作；在途动作尽量收取结果。取消：停止后续步骤，并尝试协议/Server 支持的取消；本地协程退出不证明远端未执行。

取消后仍可能收到成功回执，要保存并展示实际副作用。撤销已完成动作通过独立补偿步骤实现，具有单独权限和结果记录，不等同于取消任务。

## 9. 存储、调度与多进程

### 9.1 建议表结构

所有业务行携带 tenant_id，必要时携带 owner_user_id、project_id、created_at、updated_at；外键/唯一约束包含租户范围，避免只靠应用约定关联数据。检查点、对象存储和缓存同样执行范围限制。

| 表 | 核心字段/约束 |
|---|---|
| users / memberships / projects | 用户、租户成员关系、项目授权 |
| sessions / messages | 所属主体、message_id、role、content_ref、时间 |
| tasks | 目标、会话、创建者、当前运行 |
| runs | task_id、status、version、预算、cancel_requested_at、lease_owner、lease_expires_at、lease_epoch |
| plans | run_id、version、goal、steps_json、reason；唯一(run_id, version) |
| steps | run_id、plan_version、step_id、status、input_ref、output_ref、action_id |
| actions | action_id、tool_key、参数哈希、幂等键、状态、回执、授权引用 |
| action_attempts | attempt_id、action_id、开始/结束、错误类型、原始结果引用 |
| events | run_id、seq、type、payload_ref；唯一(run_id, seq) |
| artifacts | artifact_id、run_id、kind、storage_key、mime、size、sha256、来源与保留策略 |
| mcp_servers / tool_definitions | Server、凭据引用、工具及 schema 版本 |
| authorization_grants | 主体、动作/资源范围、参数约束、期限、来源指令 |
| session_summaries | 会话、summary、facts、覆盖到的消息/事件 ID、来源、生成版本 |
| memories / memory_sources | 作用范围、事实、版本、状态、时效、来源关系 |
| memory_tombstones | 删除范围、归一化事实键、来源禁用规则、删除时间 |
| background_jobs | 类型、dedupe_key、状态、重试、租约；唯一去重键 |
| graph checkpoints | 由选定 LangGraph 持久化组件管理 |

events 的 seq 在数据库事务中分配，不能依赖多个进程各自计算 MAX(seq)+1。可在 runs 上维护事件计数并原子递增。

本地 Store 通过迁移版本新增表，不破坏旧任务读取。正式上线前把 SQL 与业务逻辑隔离为仓储接口，并准备 SQLite 到 PostgreSQL 的迁移脚本。增加索引与约束优先于为每张表创建独立服务。

### 9.2 Worker 与租约

API 创建 queued 运行即返回，Worker 从数据库原子领取，续租并执行。PostgreSQL 可用行锁与 SKIP LOCKED 领取；SQLite 开发模式限定单 Worker。每次领取递增 lease_epoch；所有状态推进检查版本及租约代次，阻止旧 Worker 覆盖新状态。

租约过期后由恢复 Worker 先读取在途动作并核实，再决定续跑。不能看到 running 就重新调用。租约/fencing 只能约束中台写入，不能取消远端已收到的请求；仍依赖账本和远端幂等。

同一会话的改变目标/新任务提交可先串行处理。来自 API、其他客户端和 Worker 的冲突由数据库版本检查解决，不能只依赖前端按钮或进程内锁。

先用数据库 background_jobs 处理摘要、索引、恢复核实与事件派生。业务事件和派生 job 在同一事务写入，实现 outbox；消费者按 dedupe_key 幂等。未来接入消息队列时，队列只负责唤醒，数据库仍保存权威任务和状态。

### 9.3 SSE

事件先落库再通知。SSE 支持 id 游标与 Last-Event-ID，从最后确认 seq 补发，客户端按 run_id + seq 去重。用户身份必须在订阅和回放时校验。

多进程第一版可由 API 按游标短轮询数据库，将新增事件推到 SSE；之后增加 PostgreSQL 通知或 Redis 作为唤醒机制。通知可丢，事件日志可补。逐字草稿允许非持久化，但结束事件包含最终答案引用，断线重连可恢复最终结果。

## 10. 多用户、工具权限与授权

接口通过认证依赖获得 tenant_id、user_id、项目访问范围；请求中的对象 ID 只作定位，不作授权。读任务、下载产物、读取画像、订阅 SSE、恢复运行和调用工具全部执行对象权限。

工具可见性和远端资源访问分两层：规划器只看到可用工具；执行器在解析最终目标后再次校验资源范围，并以正确用户/服务账号凭据调用 Server。凭据缓存、连接池和工具目录均按身份作用域隔离。

明确用户请求可作为授权来源。例如“生成页面并部署”可以覆盖本任务的发布动作，不必每一步重复询问。仅当动作超出已授权范围、目标存在歧义或本地策略要求时等待确认。

确认记录绑定 run_id、plan_version、action_id、最终参数哈希、目标资源、有效期；参数或目标实质变化需重新判断。服务器端策略决定是否允许，不能只在工具描述中写“删除前确认”然后交给模型自觉遵守。

工具说明和返回内容是能力信息或证据，不可提升为应用指令。模型不能修改策略、注入凭据或覆盖租户字段。开发单用户身份可固定映射，但外部多用户入口启用前必须完成此层。

## 11. 上下文管理

ContextBuilder 输入结构化对象，输出最终模型消息和预算报告。用途至少区分 plan、replan、transform、final_answer、summarize、memory_extract，每种用途提供必要而不同的数据。

固定优先级：应用规则和权限边界；当前有效请求；执行目标、约束、当前步骤及待核实动作；相关工具定义；直接依赖结果；相关记忆与摘要；最近对话与可选证据。

每次模型调用预算包含 system、工具 schema、消息包装、当前请求、状态、资料与历史；按模型能力配置窗口与输出预留，使用匹配分词器或保守估算，并记录实际 usage 校准。核心指令和最小必要 schema 已超预算时，应缩小候选、分解任务或暂停，不能静默截断关键约束。

大结果保存完整内容和引用，给模型提供摘要、字段投影、分页或相关片段。必须明确是否截断、筛选条件、页码与已知总量；不再用简单前 4000 字符让模型猜完整数据。

前序步骤明确输出的 site_id、artifact_id 等直接绑定下游参数，避免让模型从摘要反推。用户说“部署刚才那份”时，从可访问产物中解析明确引用；存在多份且无法确定则请求选择，不固定回溯最近 20 个任务并取第一个 HTML。

摘要记录覆盖边界、来源与生成版本；已有摘要生效后，每轮均从边界加载未覆盖消息，而非仅在本轮发生压缩时才裁剪。当前 compact 路径会读到本轮已追加的用户消息，改造时也应确保当前请求只注入一次。

## 12. 长期记忆

优先存储明确用户偏好、项目约定、确认事实；业务即时状态放在工具系统与动作记录中。模型生成候选，应用负责范围、来源、覆盖规则与权限。

记录字段：memory_id、tenant_id、scope_type(user/project/tenant)、scope_id、type、subject、predicate、value、status、version、supersedes、valid_from、valid_to、expires_at、created_by。来源另表关联消息、事件、工具回执与证据类型。

明确“记住/纠正/忘记”同步提交，提交成功后再回复。普通候选提取与摘要作为后台工作，不阻塞每次任务规划。更新保留版本；模型推测只作为候选；不同作用范围可并存，冲突不可无条件按时间覆盖。

读取先限定租户与权限，再加载必要偏好和项目约定，结合结构化键与关键词检索相关事实，过滤失效与被替代记录。确认语义检索有收益后再加 pgvector；初期不要求 embedding。

遗忘流程写入 tombstone，失效记忆、检索索引、缓存、相关摘要，并阻止旧后台任务从相同来源再次生成。尚未执行的模型调用重建上下文；已发送的模型请求无法收回，应通过记忆版本标记禁止旧结果再次提取被删除事实。原始事件删除和备份保留另设明确策略，不能把“取消注入”说成物理清除全部数据。

USER.md 可继续作为用户可读的导出文件或导入入口，不作为多用户权威存储。

## 13. API、前端与观测

建议增量 API：

| 接口 | 用途 |
|---|---|
| POST /api/tasks | 创建目标与首个 queued run，返回 task_id/run_id/session_id |
| POST /api/tasks/{id}/instructions | 追加要求；指明作用于当前任务还是新目标 |
| GET /api/runs/{id} | 状态、当前计划、步骤、等待原因、结果引用 |
| POST /api/runs/{id}/pause | 请求暂停后续动作 |
| POST /api/runs/{id}/cancel | 请求取消，回执说明在途动作状态 |
| POST /api/runs/{id}/resume | 从检查点恢复，经幂等和权限核验 |
| POST /api/runs/{id}/responses | 补充信息或提交绑定具体动作的确认 |
| GET /api/runs/{id}/events | 带游标 SSE |
| GET /api/artifacts/{id} | 授权获取产物 |
| GET/POST /api/mcp/servers | 管理接入，由管理员权限控制 |
| POST /api/mcp/servers/{id}/refresh | 发现工具并更新版本 |
| GET /api/tools | 当前主体可见工具和可用状态 |
| GET/POST/PATCH/DELETE /api/memories | 记忆查看、创建、纠错和遗忘 |

创建、恢复、取消和响应接口支持幂等请求标识；并发控制接口携带 expected_version。对已经终结、旧计划或过期确认的请求返回明确冲突，不重启动作。

原有部署按钮迁为“针对指定 artifact_id 创建发布任务”，统一走策略、账本、事件和检查点，不能保留绕过执行器的独立写入口。

前端保留聊天与过程流，增加：当前目标；可展开的计划与修改原因；步骤输入摘要/工具/状态；产物列表；等待补充或授权卡片；暂停、取消、恢复；结果不确定提示。技术字段可折叠到详情，主界面显示用户能理解的动作和结果。

日志和指标关联 task_id/run_id/plan_version/step_id/action_id/attempt_id；记录模型用量、工具延迟、重试、unknown 动作数量、恢复耗时、队列积压、授权等待、上下文选择与截断。日志不记录凭据，涉及敏感内容的结果使用受权限保护的引用。

## 14. 目录与兼容迁移

建议按职责逐步迁移，避免一次性重命名全部文件：

```text
app/
  api/                 # 任务、运行、工具、产物、记忆路由
  domain/              # 通用数据模型、状态、错误分类
  orchestration/       # planner、validator、graph、observer
  execution/           # executor、bindings、actions、recovery、scheduler
  integrations/mcp/    # Server 配置、发现、连接、结果封装
  registry/            # 工具目录与候选筛选
  context/             # 上下文与预算
  memory/              # 摘要、记忆、遗忘
  artifacts/           # 本地/对象存储适配
  persistence/         # 仓储、事务、迁移、检查点集成
  policies/            # 身份、权限与授权
  capabilities/
    web/               # 当前 search/fetch
    reports/           # 当前报告生成与渲染
    publishing/        # 当前部署、base64、哈希与远端回执处理
  web/                 # 现有前端
scripts/
  serve_api.py
  run_worker.py
```

旧 app.api、app.main、tools 入口在过渡期保留薄兼容层。旧状态里的 documents/report/deployment 通过业务能力适配映射到 outputs/artifacts；旧 task 查询与事件读取可继续用，执行新任务逐步切到 run 模型。

## 15. 实施阶段与验收

### 阶段 A：通用工具与计划合同

工作：统一 ID 语义；新增 Server 配置与工具版本；保留完整描述；本地/远端统一输入输出；步骤引用与计划校验；业务提示词移到能力包；移除研究型通用兜底与静默跳过。

验收：接入两个测试 MCP Server，即使工具同名也能正确路由；查询输出可绑定下游动作；普通非网页任务无需修改执行图；工具 schema 不匹配时不执行残缺计划；纯文本 MCP 结果正常保存和作答。

### 阶段 B：可靠执行与恢复

工作：动作账本、attempt、持久化 checkpointer、版本控制、错误分类、有限重新规划、暂停取消、核实策略。旧部署快捷入口改走统一链路。

验收：逐个模拟三个崩溃窗口；已成功动作不重发；远端超时且无法核实则 unknown；取消后回执仍能入账；恢复继承预算；计划修订不改变已完成历史。

### 阶段 C：多用户运行底座

工作：认证、对象权限、凭据隔离、PostgreSQL 仓储、独立 Worker、租约与恢复扫描、SSE 游标、数据库后台任务和 outbox。

验收：两个用户无法交叉读取任务/产物/摘要/事件；两个 Worker 竞争同一运行只有一个有效领取；旧租约 Worker 不能覆盖状态；API 重启不打断 Worker；SSE 跨实例重连可补完整事件；任务提交后 API 崩溃仍可被领取。

外部多用户开放必须在本阶段完成后。若已有近期对外计划，认证及权限模型应从阶段 A 同时落实，而非最后补字段。

### 阶段 D：上下文与长期记忆

工作：完整预算、结果引用和按需读取、摘要覆盖边界、范围化记忆、同步纠错遗忘、后台整理和删除失效链路。

验收：长会话仍保留当前约束；结果截断不会把部分列表说成全量；项目约定不串用；显式纠正即时生效；删除后后台旧任务不能重新生成；记忆服务故障可降级普通对话，关键执行事实不可降级为模型猜测。

### 阶段 E：按实测扩展

根据工具数量、积压和成本，再增加语义工具召回、pgvector、并行步骤、事件通知总线或专用队列。先定义资源互斥、分支失败语义、预算和补偿，再启用并行写操作。

## 16. 推荐第一批开发范围

以“同一套内核完成一个 HTML 发布任务和一个完全不涉及网页的 MCP 任务”为首个里程碑。

先改五处：schemas.py 增加步骤 ID/引用/通用结果；mcp_adapter.py 接收 ServerConfig 并保留完整返回；tools.py 拆出注册表与业务适配；nodes.py 增加校验和结果观察；store.py 增加 plans/steps/actions/attempts 及迁移。

这批变化应同时预留身份作用域和稳定 action_id，然后接入持久化图与故障恢复。不要先投入向量记忆、可视化流程编辑器或大规模服务拆分。

## 17. 设计依据与限制

- 当前项目的 main、graph、nodes、tools、mcp_adapter、api_hub、store、context、memory、profile、schemas、api 与现有测试代码，已按 2026-10-19 的工作区内容检查。
- [MCP Tools 规范（固定版本 2025-06-18）](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)：用于核对工具元数据、输出结构与错误类型。实施时按实际 Server 协商协议与锁定 SDK 版本测试，不假设所有 Server 都支持全部字段。
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：用于核对检查点与长期存储的职责，以及持久化恢复的基本配置。
- 本文是设计和验收建议，没有修改业务代码，没有执行真实远端工具，也未声称已有端到端恢复或幂等保证。远端 Server 的实际参数、回执及幂等合同仍需逐个验证。
