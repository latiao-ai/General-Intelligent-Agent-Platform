# MCP 工具运行时：长任务、权限准入与通用结果设计

日期：2026-10-23。状态：待实施设计。本次只新增文档，不代表接口、迁移或执行能力已实现。

## 1. 目标、当前证据与实施顺序

解决三件事：工具返回受理回执不等于业务完成；登录与对象归属不等于有权执行某项工具；JSON 不是全部工具结果。

本次代码读取确认：

- `mcp_adapter.result_payload()` 仍要求 structuredContent 或可解析的文本 JSON；其它结果会抛错。
- `call_tool()` 仍有 90 秒同步调用限制；`task_timeout` 默认 300 秒。
- `Server.token` 及 MCP Server 列表主要来自进程配置，工具缓存主要按全局服务配置组织。
- `policies.py` 已有可信身份和对象归属，但通用工具能力、凭据和批准规则尚未接入。
- 现有 background_jobs、动作准入/结算、运行快照、输入版本和结果观察相关代码可作为基础；本轮不覆盖工作区中其它未提交修改。

推荐顺序：**统一结果合同 → 工具授权与凭据绑定 → 异步业务合同与跟踪 → 安全接续与前端 → 故障验收**。三者最终在同一工具运行链路交汇，不能各自开一条绕过账本的执行路径。

异步合同是本平台与客户适配器约定的版本化业务合同，不假定任意 MCP Server 原生支持任务查询、取消或平台幂等键。

## 2. 工具运行链路与模块边界

正常调用顺序：

1. 解析可信用户、租户、项目/资源范围。
2. 按权限发现工具和选定凭据主体。
3. 解析步骤输入与产物引用，执行 schema 校验，计算最终参数指纹。
4. 检查当前策略、批准、额度、租约与要求版本，事务准入 action/attempt。
5. 临发送前解析凭据并再次确认准入仍有效；使用受控 MCP 连接发出。
6. 保存原始结果，形成统一结果信封。
7. 同步结果完成本步骤；受理回执登记原外部任务并暂停依赖；未知效果进入核实。
8. 后台查询原任务、保存产物和完成证据；重新授权后才允许接续新的业务操作。

建议新增模块：`tool_results.py`（结果与投影）、`tool_policy.py`（许可）、`credentials.py`（凭据引用解析）、`approvals.py`（批准）、`external_tasks.py`（业务任务状态与调度）。MCP adapter 只管协议连接与调用，节点不直接读取密钥。

## 3. 先定义统一结果，分开三种结论

### 3.1 结果信封

建议标准 `ToolResult`：

```json
{
  "result_id": "res_...",
  "action_id": "act_...",
  "attempt_id": "att_...",
  "tool_key": "mcp:media:create_video",
  "transport_status": "ok",
  "business_status": "accepted",
  "verification_status": "unverified",
  "structured_data": {"external_task_id": "job_123"},
  "content_refs": [{"kind": "text", "ref": "result-part:..."}],
  "artifact_refs": [],
  "external_task_ref": "ext_...",
  "raw_result_ref": "result:...",
  "completeness": {"complete": true},
  "error": null
}
```

信封内部身份由平台生成；工具返回的 action_id、owner 等不能覆盖可信字段。上例只说明业务形状，不代表现有服务端已有这个工具。

### 3.2 状态语义

| 维度 | 建议值 | 说明 |
| --- | --- | --- |
| transport_status | ok / error / unknown | 本次 MCP 交换是否取得可靠返回 |
| business_status | not_applicable / accepted / running / succeeded / failed / cancelled / unknown | 业务进展由已注册适配合同解释 |
| verification_status | not_applicable / unverified / verified / failed | 是否完成合同要求的结果核对 |

普通文本查询没有异步业务时 business_status 可为 not_applicable，不能因为没有 JSON 就失败。MCP 协议成功不证明业务成功；业务完成也不证明产物保存或内容质量合格。

默认通用结果不按任意正文里的 status/error 字段推断业务状态。当前 `failure_of()` 等经验规则逐步迁到已验证的业务适配器，保留旧部署适配兼容；配置未声明的工具不得仅靠名称或字符串被识别为视频生成任务。

## 4. 普通文本、图片、音频、资源如何保存

### 4.1 保留原始结构

修改 result_payload 为“原始结果采集/归一”，接受 structuredContent 和全部 content blocks：文本、图片、音频、资源链接、内嵌资源；未知类型保留其可序列化原始结构与类型标记。isError 也先保存可用原始返回，再记录错误。

结构化结果和文本 JSON 可以同时保留，不强制二选一。工具声明 outputSchema 时单独校验 structuredContent；schema 不兼容不抹掉收到的事实，有副作用的请求不能因解析失败重新发出。

大 base64 媒体与文件写受控结果存储，数据库保存清单、大小、hash、MIME 和归属；不把大量二进制塞进 AgentState 或提示词。

### 4.2 文件与数据库之间的提交

文件存储和 PostgreSQL 不是单事务：先将收到的内容写入临时受控对象并校验 hash，再在动作结算事务中引用该对象并登记 manifest；成功后标 ready。未引用临时对象由延迟回收清理，已引用对象不能被回收。

存储失败时，已经受理的远端调用仍可能发生过。保存已知任务身份和错误证据，无法可靠提交时保持 unknown/待核实；不能抛个普通失败后重新生成。

设置合理的传输和存储配额。合法但超过平台能力的响应明确记录 incomplete 与原因；不能宣称无限大小均可完整保存。日志、提示词、摘要隐藏凭据和敏感头；受控原始存档仍只允许有权主体访问。

### 4.3 模型只看投影，需要时继续读

新增受控读取能力 `read_tool_result`，参数仅接受 result_id、part_id、JSON Pointer、offset/limit、expected_hash 或版本。

返回 complete、next_offset/next_read_ref、总量与来源。字符串分块保持原文；数组分页保持项目身份；不能把截断摘要当作完整原结果。

旧 `tool_results_text()` 改为生成摘要加引用。规划与观察模型可请求读取细节；正文中的“忽略系统规则”等是业务数据，不获得指令权限。

前端可新增只读接口 `GET /api/tool-results/{id}` 及分块/媒体预览入口，沿用可信主体和对象权限。模型内部读取工具与 HTTP 入口共享同一资源服务，不接受任意磁盘路径。

### 4.4 链接和多模态边界

资源链接只是引用，不默认下载。读取前按来源、资源归属和网络出口规则验证，禁止把模型提供的任意 URL 当可读取内部资源。短时授权地址不能替代对象权限。

保存图片/音频不代表当前模型已经理解它；多模态转换只在模型实际支持且本轮有权读取时执行。读取事实关联具体字节版本与区间，质量核实另记。

## 5. 工具能力与凭据隔离

### 5.1 最小治理对象

| 对象 | 主要字段 |
| --- | --- |
| server_binding | tenant_id、server_id、受信端点/stdio 配置、enabled、contract_version |
| credential_binding | binding_id、tenant_id、subject_user/service、credential_ref、scope、revision、status |
| tool_policy | tool_key、适用角色/项目/资源范围、允许效果、批准要求、限额、revision |
| tool_definition | 描述、schema/hash、服务端来源、annotations、可信本地 effect/重试分类 |
| authorization_receipt | 可信主体、工具/schema、资源、最终参数指纹、凭据主体、策略/批准版本、准入身份 |

服务端 annotations 只是声明，不能单凭 readOnlyHint 或 idempotentHint 自动授予访问或重试资格。缺少可信效果信息维持 unknown。

首版策略由部署管理员配置与迁移数据管理，不急于做复杂角色管理后台。客户业务系统仍需执行其自身权限校验，中台许可不能代替远端许可。

### 5.2 凭据引用与连接

生产多客户连接使用 credential_ref，由现有密钥设施、受控环境映射或加密存储解析；先选主体再取密钥。明文不进入模型、步骤、事件、状态快照和普通日志。

不为本次强制引入某个 Vault 产品；单机开发保留显式 dev profile 的原环境配置。生产缺少凭据绑定应拒绝，不能回退到全局默认 token。

连接、发现和工具缓存至少按 tenant、server、凭据主体/绑定修订、工具版本与策略修订隔离，失效后重新发现。不能把 A 的工具清单或权限结果作为 B 的缓存。

模型无权创建任意 MCP 端点、headers 或 stdio 命令。新端点由管理员绑定，避免把凭据发送到模型提供的地址。

## 6. 权限需要覆盖发现、执行和恢复

### 第一步：发现过滤

`catalog(principal, execution_scope)` 和 `mcp_tools(...)` 按客户绑定、可用凭据、项目与工具许可过滤，只给规划器有权工具。返回受控别名，账本保留稳定 tool_key。

### 第二步：最终参数校验

输入绑定完成后再计算参数指纹与资源范围。模型填写 tenant/user 字段不得覆盖可信身份；资源字段通过已注册适配器解析并核实。任意 MCP 无法自动证明所有业务资源归属，未建合同的高影响工具维持受限或不开放。

### 第三步：事务准入

在现有 admit_action 中检查租约/要求版本、策略状态、批准匹配和额度预留，生成准入回执。数据库可锁定的政策检查与动作身份提交保持原子；外部鉴权调用不在数据库事务里等待。

准入前使用已核实的短时鉴权结果，记录版本和期限；真正 dispatch 前再确认本地策略/凭据未撤销。跨系统没有绝对无窗口的撤销保证，远端必须保有自己的校验。

### 第四步：恢复与后台调用

成功历史回执重放仍须检查读取权，但不重新消耗一次性批准。新的 attempt、查询、取消和接续业务操作必须重新授权。

长任务后台查询使用绑定在该任务上的受限委托，不能借 Worker 身份切换到全局管理员凭据。凭据轮换只允许同主体、兼容范围的更新；换用户或范围扩大需要重新授权。

## 7. 结构化批准合同

新增 approval 对象，至少保存 tenant、主体/批准者、task/run、工具或有限清单、资源范围、最终参数指纹/manifest、schema/业务合同版本、有效期、次数/额度上限、状态与审计时间。

提供 `GET /api/approvals/{id}`、`POST /api/approvals/{id}/approve` 与 reject/revoke 入口。批准者身份来自已验证会话和批准权限，模型说“用户同意”不产生批准。

首版采用精确参数批准与有限 manifest，不开发任意范围表达式：一次确认多个明确对象/参数，允许其中已批准的后续动作继续。参数变化、模型替换、增加对象或费用范围扩大时重新匹配，不无条件沿用。

准入事务绑定批准与 action：同 action 的安全重放不重复扣次数；不同 action 不能重复消费同一次批准。批准后策略或凭据撤销仍会阻断。用户补充不是批准，intent_version 变化后按当前 manifest 再验证。

一次性批准应在动作准入时预留，最终明确未执行后可按合同释放；unknown 不释放并转供另一个动作，否则可能超额执行。费用硬上限只有可信计量合同才能实现，不靠模型估价承诺。

## 8. 异步业务合同：客户需要明确提供什么

建议给工具增加平台侧注册的 `async_contract`，采用受限字段映射和枚举，不执行配置中的任意代码。复杂服务用受信 Python 适配器。

示意：

```json
{
  "version": "1",
  "submit_tool": "create_video",
  "task_id_pointer": "/job_id",
  "status_tool": "get_video_job",
  "status_args": {"job_id": {"from": "external_task_id"}},
  "status_pointer": "/status",
  "status_map": {
    "queued": "accepted",
    "processing": "running",
    "done": "provider_succeeded",
    "failed": "provider_failed"
  },
  "output_pointer": "/outputs",
  "cancel_tool": "cancel_video_job",
  "idempotency": {"supported": false},
  "poll_policy": "bounded_backoff",
  "completion_verifier": "owned_video_resource_v1"
}
```

这些名称是说明性示例，实际需匹配真实工具与 schema。cancel、幂等与核实都可选，未声明就是不支持。

接入验证至少核对：工具存在、查询参数形状、状态覆盖、任务 ID 非空、输出字段、权限与凭据要求、取消语义、结果过期/404 的解释。未知状态保持 unknown，不随意当失败或成功。

支持三类能力：同步直接完成；异步有可靠跟踪合同；可能长时间阻塞却无跟踪能力。最后一类不能仅靠平台包装变成可恢复异步；可配置单次上限，超时后待核实，向客户明确需要补查询或幂等能力。

## 9. 外部任务数据与状态

新增 `external_tasks`，至少保存：external_task_id（平台内部 ID）、remote_task_id、tenant/owner、run/step/action、server/tool/contract 版本、credential_binding、accepted_result_id、submission_key（支持时）、状态、next_poll_at、deadline、poll_owner/epoch/lease、last_result_id、output_refs、version 和时间。

远端任务 ID 的唯一性不能跨客户猜测，唯一作用域包含租户、服务端、凭据主体与远端 ID；同 action 原则上只登记一份提交，批量输出用合同明确一对多关系。

建议业务状态：accepted → running → provider_succeeded → materializing → ready。分支包括 provider_failed、provider_cancelled、observation_blocked、deadline_exceeded/needs_reconcile。

provider_succeeded 只表示供应商完成；materializing 表示资源入库中；ready 表示本合同所需产物可用且绑定完成。质量检查状态另存，不能用 ready 冒充作品质量通过。

本地 run 建议增加通用 `waiting_external`，明确属于未结束目标；同步修改 outcome、会话当前目标、claim 条件、前端和状态词汇测试。它不会被普通 Worker 当成可执行步骤反复领取，只能由受控唤醒转 queued。

动作层增加 accepted/waiting_external 等明确阶段：提交 attempt 已取得回执，但该逻辑步骤尚未完成。不能把提交成功记为整个 action/step succeeded，然后让下游执行。异步状态变化与本地步骤最终结算有明确映射。

## 10. 长任务执行流程

### 10.1 受理

1. 根据 async_contract 识别受理返回；原始回执已持久引用。
2. 事务登记 external_task、原 action 的受理状态和本步骤等待状态。
3. 保存 run 快照与等待任务引用、追加 external_task_accepted 事件，并以原身份登记轮询作业。
4. run 进入 waiting_external，释放本次执行租约；不占住模型会话或 Python 协程等数十分钟。
5. 若事务失败，外部可能已受理，保留 unknown 并核实，不重发提交。

如果响应丢失而没有 remote_task_id，只有服务端支持按稳定提交键查询或安全幂等重试，才可能自动找回；不支持时保持待核实。禁止从工具名字猜查询接口。

### 10.2 后台轮询

复用现有 background_jobs 或增加到期任务扫描器，两者择一作唯一轮询调度源。建议作业 kind=external_task_poll，带 due_at、重试计划和受限任务引用。

现有 claim_job 如果只处理 queued，需要补租约过期 reclaim、epoch、条件结算与 due_at，否则 Worker 崩溃会留下 running 作业。不要因已有队列表就假定后台跟踪可靠。

每次只查询原 remote_task_id；带任务级轮询租约，查询和结果提交均检查其 epoch/version。网络等待不持数据库锁。

处理中按有界退避和 jitter 更新 next_poll_at；遵循供应商限流提示。认证失效转 observation_blocked，恢复原主体凭据后再观察；网络查询失败不等于任务业务失败。

不让模型参加进度轮询；轮询次数、出口请求额度仍受程序预算控制，并向用户显示最后成功查询时间。

### 10.3 产物落地和完成

provider_succeeded 后保存结果、验证输出归属并幂等登记产物。绑定键包含原任务/输出索引/目标；重试物化不重复创建素材或重新购买。

产物未取回、链接过期或绑定失败时保留原外部成功身份，允许只修复取回/绑定；不把整项任务重置为“重新提交”。全部必需结果 ready 后，事务结算逻辑 action/step 和结果引用。

### 10.4 唤醒接续

状态更新、ready 事件和 continuation 唤醒记录同事务提交，用稳定去重键。唤醒原 run 时检查当前目标、pause/cancel、新输入和未知动作。

未暂停且可继续则 queued，Worker 按原 run 领取；存在新用户要求先消费最新输入，不沿旧计划直接写。paused 保持 paused；已取消只保存迟到结果，不继续制作。

如果只有一组前置任务全部就绪才可继续，依据明确依赖集合核对，不因一项先完成就反复唤醒模型。独立任务并发可后续开放，首版保持已有顺序执行。

## 11. 取消、等待期限与权限撤销

用户“停止中台任务”与“取消供应商任务”分别记录。支持远端取消时使用专门授权和合同调用，保存取消请求与真实回执；不支持时告知已提交任务可能继续产生结果或费用。

远端取消请求已接受也不等于已取消，继续查询终态；停止中台后保留有限后台对账，目的只为记录已发生效果，不自动接续原业务。

重新划分超时：连接/单次 RPC 超时、模型调用超时、活跃执行预算、外部等待期限、任务总墙钟截止时间。300 秒不应一次覆盖整个视频生成等待；也不能通过每次唤醒重置所有预算而无限执行。

等待期限到达表示平台不再按正常频率等待，不证明远端失败。保留 task 身份与待核实结局，按照明确策略低频核实或交人工处理，不能自动购买替代任务。

策略或凭据撤销后，不再执行新的提交/接续。后台能否继续只读跟踪由仍有效的受限委托决定；不能绕过撤销改用更高权限凭据。无法观察时保留已知事实和阻断原因。

## 12. 结果读取与权限贯穿全链

tool_results、result_parts、artifacts 和 external_tasks 全部保存 tenant/owner/资源范围。HTTP 读取、模型读取、摘要缓存、媒体转换与下载必须同样校验，不因持有 result_id 就放行。

原始响应可能包含服务端诊断与敏感字段，摘要和 UI 投影使用明确可见字段；管理员原始诊断读取与普通用户业务结果读取分开。日志不打印 token、完整 headers 或连接配置。

GC 按活跃 run/action/result 引用保留，清理不删除正在跟踪任务的回执或恢复依据。超大结果和不支持类型的限制必须在 completeness 中可见。

## 13. 接口与前端

保留现有 task/run 查询和 SSE，新增的业务读取建议为：

- `GET /api/external-tasks/{id}`：当前外部阶段、产物、最后观察时间和能力限制。
- `POST /api/external-tasks/{id}/refresh`：有界重新观察原任务，不重新提交。
- `POST /api/external-tasks/{id}/cancel`：只在合同和授权允许时发取消请求。
- `GET /api/tool-results/{id}` 与分块/预览接口。
- 结构化 approval 读取、approve、reject/revoke 入口。

状态事件包括 tool_result_saved、external_task_accepted、external_task_progress、external_task_ready、observation_blocked、approval_required。继续使用原 task 的持久 seq 和回放。

前端等待外部任务时不显示“已完成”，也不能把一次执行协程退出的 end 事件当整个目标终态；显示“已受理/处理中/正在保存/结果可用/待核实”。是否保持 SSE 长连接属于观察策略，断开后查询和后台任务仍继续。

## 14. 增量实施与文件清单

| 阶段 | 主要文件 | 交付标准 |
| --- | --- | --- |
| A 通用结果 | mcp_adapter.py、tools.py、tool_results.py、新存储/读取仓储、base.py | 文本与多块结果正常，原始返回有引用，旧部署适配兼容 |
| B 工具准入 | policies.py、tool_policy.py、credentials.py、mcp_servers.py、registry.py、ledger.py、pg_store.py | 客户/用户/凭据隔离，发现与执行双重校验 |
| C 结构化批准 | approvals.py、api.py、pg_store.py、前端 | 精确 manifest、并发消费去重、参数变化重新判断 |
| D 异步合同 | external_tasks.py、工具适配配置、schemas.py、nodes.py、pg_store.py | accepted 不推进下游，原任务受理身份持久化 |
| E 后台接续 | run_worker.py、api_hub.py、background_jobs、快照与 outcome | 可回收轮询租约，ready 幂等唤醒，暂停/新输入保持 |
| F UI 与故障验收 | events.py、web/index.html、专项 tests、配置/接入文档 | 状态真实、断流不丢、权限撤销与重启矩阵通过 |

每一阶段提供可重复迁移；存在其它实现中的未提交修改，施工时增量调整，不覆盖或重置。生产策略默认拒绝，逐能力接入；dev 兼容模式要明确配置。

灰度时只有注册 async_contract 的工具走异步路径，未声明工具保留同步行为和明确限制。服务端状态映射与合同版本变化不得静默影响进行中任务；任务继续绑定原合同，无法兼容时待核实。

## 15. 验收矩阵

1. 普通文本、多个文本块、structuredContent 加文本、图片/音频/资源结果均保留身份和完整性。
2. outputSchema 失败、isError、解码失败保存可用原始事实；已发生写操作不盲重发。
3. 大结果分块重组 hash 一致，分页尾部可读取，摘要不冒充全文。
4. A 用户不能读取 B 的结果或复用 B 的发现/连接缓存。
5. 两租户同名工具、同名远端任务 ID 分别正确路由。
6. 发现允许但执行前权限撤销，准入失败，远端调用计数零。
7. 模型伪造 owner、credential_ref、approval_id 不能扩大范围。
8. 一次批准被两个并发不同动作消费时按上限阻断；同 action 重放不重复消耗。
9. 参数/对象/模型变化后旧批准不自动生效；unknown 不释放成新批准余额。
10. accepted/running 返回不把逻辑 step 标 succeeded，下游调用计数零。
11. provider 完成但结果未保存，仍未 ready；只重试物化而不重新生成。
12. 受理后断响应：有合同则按原键核实，无合同时保持 unknown。
13. 杀掉轮询 Worker 后租约过期可被接手；旧 epoch 不能覆盖结果。
14. 重复 ready 或唤醒只接续一次，原 action/task 身份不变。
15. 等待期间用户补充，ready 后先处理新要求；暂停不自动恢复，取消不自动继续。
16. 远端取消不支持/被拒绝/仅受理时，UI 不显示已撤回成功。
17. 网络/认证查询故障不误判供应商失败，凭据不跨主体回退。
18. 等待超过平台期限不重新购买；迟到产物可安全登记。
19. JSON 内容里的业务文字 error 不被默认通道误当所有工具失败；适配器解释有边界。
20. RPC、模型、活跃时间和外部等待预算分别生效，进程重启不归零。

测试采用一次性 PostgreSQL、本地假 MCP/模型与受控资源服务；计数真实 submit/status/cancel 调用，使用 barrier 和独立进程固定竞态。没有实际执行的验收不能在文档标通过。

## 16. 最小端到端示例

用户要求生成一段视频。中台按其主体发现可用工具、核实模型/参数和批准，准入 action 后调用一次提交。MCP 返回 job_123，只保存为 accepted；数据库登记原外部任务并进入 waiting_external。

后台每次查询 job_123，不调用模型、不重新提交。供应商完成后，中台取回并校验视频、登记受控产物，再结算步骤 ready。未暂停或取消时唤醒原 run，按最新输入进入观察、核对和交付。

用户随后要求改字幕，模型读取的是有授权的产物摘要与引用；需要更多细节再按块读取。是否再次生成、是否付费分别经当前权限和批准判断，不因已有视频就获得无限执行授权。
