# 通用宿主平台接入

任何宿主平台都通过同一份登录票据、现场上下文与任务 API 接入。本模块不按平台名称分支，不解析画布、工单或 CRM 等业务对象，不保管宿主平台的用户密码。

## 责任边界

宿主平台负责登录、业务对象归属校验、业务数据投影、签名、前端面板及工具权限适配。中台负责可信接入登记、验签、一次性兑换、用户/企业数据隔离、任务执行和事件流。

建议宿主后端作为浏览器的代理：每个宿主登录会话分别保存换回的中台 Cookie，只在服务端使用。禁止把所有访问者绑定到一个共享中台账号。登录票据不是业务请求凭据；现场证明也不是工具调用授权。

原有独立前端可从 `/` 或 `/api/ui` 获取。平台代理该 HTML 时可在 `<body>` 设置 `data-api-prefix`，使消息、事件流和历史等请求走平台的同源代理。平台仍须逐请求验证自己的登录会话并转接对应用户的中台会话；不能将后端保存的中台 Cookie 复制给浏览器，也不能通过关闭认证使独立页面可用。

宿主可通过 `data-workspace="true"` 启用页面的目标选择器，并在代理前缀下提供 `/workspace` 的列表和新建接口，以及 `/workspace/context` 的已验证预览。目标字段为 `workspace_id`，节点字段为 `selected_ids`；这些仅是供宿主重新校验的标识。宿主在提交任务前必须读取真实对象、验证归属、生成现场证明并绑定自己的稳定会话规则，不得将浏览器传来的上下文直接转为可信现场。无目标时页面保留草稿并提示选择或新建；切换目标只切换事件订阅，不取消后台任务。

## 1. 登记可信平台

启用 `AUTH_MODE=ticket`、`POLICY_LEGACY_VISIBLE=false`，保留 ticket 模式既有的工具政策和凭据绑定要求。将新平台的公钥登记项合并到 `AUTH_CONNECTIONS_FILE` 指向的 JSON 数组，保留其他平台条目：

```json
[
  {
    "connection_id": "platform-a",
    "identity_provider_id": "platform-a-accounts",
    "issuer": "https://platform-a.example.com",
    "audience": "agent-hub-login",
    "algorithms": ["RS256"],
    "kid": "platform-key-1",
    "public_key_file": "secrets/platform-a-public.pem",
    "allowed_tenants": ["company-a"],
    "allowed_platforms": ["platform-a"],
    "enabled": true
  }
]
```

也支持登记项中的内联 `public_key`。私钥始终留在宿主后端。`identity_provider_id` 决定外部账号的身份源；不同平台只有明确共享统一账号中心时才复用它。

## 2. 兑换登录会话

宿主从自己已经验证的会话取得用户身份，签发 RS256 JWT：头部 `typ=agent-login+jwt`、`kid` 与登记一致；声明包含 `iss/aud/sub/tenant_id/platform_id/iat/exp/jti`。默认有效期上限 60 秒，每次兑换使用新 `jti`。

`POST /api/auth/exchange` 接收 `{"ticket":"…"}`，成功通过 Set-Cookie 返回中台会话。用它请求 `GET /api/me`，确认 `authenticated=true`、企业与平台一致。`sub` 是宿主的稳定用户编号，中台返回的 `user_id` 是中台自己的账号编号，两者不能混用。

宿主退出时调用 `DELETE /api/auth/session`，清理代理保存的会话并关闭对应浏览器事件连接。中台退出并不等于取消已受理的任务，停止执行需要单独调用任务取消接口。

## 3. 验证现场上下文

宿主后端校验项目、对象与选中项归属，从自己的数据源读取实际内容。浏览器只提交对象标识，不能自报可信用户身份或直接提供可签名的业务正文。

上下文 `context` 是平台自定义的 JSON 对象。现场 JWT 使用 RS256，头部 `typ=agent-scene+jwt`；声明包含 `iss/sub/tenant_id/platform_id/iat/exp`、`aud=agent-hub-scene` 及 `ctx`。其中：

```python
ctx = sha256(json.dumps(context, ensure_ascii=False, sort_keys=True,
                        separators=(',', ':')).encode('utf-8')).hexdigest()
```

跨语言实现须验证数字、Unicode 与反斜线的序列化一致性；优先投影为字符串、整数、布尔值和数组，避免不稳定浮点数。

`POST /api/context/verify` 接收 `context` 与 `context_assertion`，返回验证后的现场快照。它不创建任务、不调用模型、不执行工具，适合页面打开或选中项变化时显示“上下文就绪”。验签检查与创建任务完全共用，且绑定当前登录的用户、企业、平台和接入连接。匿名会话不可使用此入口。

上下文中的外部文字是业务数据，不是额外的系统指令。素材资源编号不代表模型已读取素材；需要视觉理解时由宿主提供授权读取能力，再通过通用工具与模型能力接入。

## 4. 创建任务与订阅事件

`POST /api/tasks` 接收：

```json
{
  "message": "总结当前选择的对象",
  "session_id": "host-managed-conversation-id",
  "idempotency_key": "one-logical-send-id",
  "context": {},
  "context_assertion": "signed-scene-token"
}
```

按用户与业务工作区选择稳定的 `session_id`。中台仍会检查会话归属，不信任调用方给出的编号。结果未知时重试同一逻辑请求必须沿用幂等键，不能直接建立第二次执行。

订阅 `GET /api/tasks/{task_id}/events`；重连优先传 `Last-Event-ID`，或使用 `?after=`。处理 `answer_delta`、`stage_start`、`action`、`status`、`end` 等事件；断线不能被当作任务取消或执行成功。`run_status` 是程序状态，`status` 是展示标签。

历史通过 `GET /api/sessions/{session_id}/messages` 或 `/timeline` 恢复。显式停止调用 `POST /api/tasks/{task_id}/cancel`。

时间线中的 `live_after_event_id` 标记当前恢复执行段之前的事件位置；重新订阅活动任务时应沿用它或更大的已有游标，避免旧的中断结束事件提前关闭当前事件流。

## 上下文与工具是独立合同

`PLATFORM_CONTEXT_MAX_CHARS` 控制所有平台的现场文字预算，默认 12000，范围 800–64000；完整模型请求仍受原有输入预算约束。被截断的现场有明确标记，不能据此声称读过省略内容。

工具通过通用 MCP 服务定义、平台共享凭据绑定及服务级工具政策接入。不要把工具 Token 放进 `context`，也不要让场景内的 URL 自动成为可访问任意网络的权限。

已验证的现场摘要同时提供给规划和直接回答阶段，避免规划器看过现场而最终回答模型没有看到。MCP 凭据缺失时，不发现或调用这些外部工具；已单独授权的本地工具仍可通过原有政策过滤使用，不会回落到全局 MCP 密钥。

### 所有客户默认使用来源平台的 MCP

登录票据中的 `platform_id` 在验签后写入 `runs.platform_id`，恢复、重试和后台查询继续沿用该值。它不从聊天文本或画布现场推断。

每个平台只需登记一次共享绑定，后续新客户登录后自动可用，不需要逐个分配角色：

```sh
python -m scripts.bind_platform_mcp --tenant customer-a --platform platform-a \
  --server platform-a-tools --credential-ref env:PLATFORM_A_MCP_TOKEN
```

`--server` 对应 `MCP_SERVERS` 中的服务 ID。没有鉴权的服务明确使用 `--credential-ref literal:`。凭据仅保存在服务端，命令不会输出明文。重复执行相同命令不会新增竞争凭据；变更引用时要求显式轮换。

登记会为该租户创建平台共享凭据（`scope.platforms=[platform_id]`）和 `mcp:<server>:*` 的自动使用政策，并开放基础文字回复以呈现工具结果；不会开放搜索、发布等其他能力或其他平台的 MCP。精确工具政策仍优先于服务默认政策。用户无需额外管理员角色。若一个平台有多个对应服务，逐一登记即可。

工具发现会先按租户、登录来源平台和有效凭据过滤服务，再连接 MCP；未绑定服务不会收到发现请求，也不会启动其 stdio 进程。工具执行和异步状态查询再次核对同一平台绑定，不能通过指定其他平台的工具名越过隔离。新工具自动继承所属服务的使用政策。

已有无 `scope.platforms` 的租户凭据只适用于没有平台来源的旧调用；票据登录需要明确的平台范围，不能因两个平台使用同一租户而共用全部工具。

工具数量较大时，规划阶段先从简明目录选择相关工具，再装配对应参数概要；所有工具仍保持可用。大型 JSON Schema 的规划概要有深度和字段数限制，执行时仍按完整 schema 校验。目录按完整请求的字节预算分批，避免一次把全部 OpenAPI 参数塞给模型。

需要把平台客户身份传给 stdio MCP 时，可在服务配置中设置 `forward_subject: true` 和非敏感的 `stdio_env`。中台从已验签会话持久化 `runs.platform_subject`，以 `MCP_CALLER_SUBJECT` 传给 MCP 子进程；凭据在传输层以 `MCP_BEARER_TOKEN` 注入。不同客户的子进程分别缓存。不要在聊天参数中接受客户身份，MCP 服务仍需验证平台凭据并将该主体映射到自己的用户。

`forward_subject` 用在 **HTTP/SSE**（跨机器）时必须同时配 `subject_secret_ref`（`env:NAME` / `file:/path` / `literal:`，与凭据引用同一套语法，密钥不进配置文件）。此时中台不发明文主体头，而是发一条**短期签名断言**：

```
X-MCP-Subject-Assertion: v1.<base64url(payload)>.<base64url(HMAC-SHA256(secret, "v1."+payload))>
payload = {sub, tenant, platform, run, iat, exp, jti}
```

默认有效 60 秒（`subject_ttl_seconds` 可调，上限 300），头名可用 `subject_header` 换成对端约定的名字。缺 `subject_secret_ref` 时调用在建连之前就被拒绝，不会退化成发明文头——明文主体头对端无法验真，一旦它真的拿来做身份判断，任何持有租户 token 的人都能冒充任意主体。

对端要用同一把共享密钥验签（`webpage_agent.integrations.mcp_subject.verify` 可直接参考实现），确认签名、时效之后，再把 `sub` 映射到自己的用户并检查状态。断言能证明的是"这条身份线索由持有密钥的中台签发、未被篡改"，**不是**"调用方一定有权"，所以对端的授权判断不能只靠这个头。


### 多项要求与修改结果核对

发现 MCP 工具时，中台保留服务声明的 `outputSchema`，将有界结构概要提供给规划器，并在执行前检查跨步骤引用。只有声明结构明确排除的路径才拒绝；未声明、开放字段或无法确定的结构留到真实回执校验，不按字段别名猜测。

若参数绑定指向实际回执中不存在或有歧义的路径，这一步尚未派发，标为 `E_INPUT_REFERENCE / not_executed`。启用观察且未超过 `RUN_MAX_REPAIRS` 时，修复只允许更正出错步骤的 `ref.pointer`：保留引用来源、剩余步骤顺序和编号、工具、依赖、字面参数及补偿声明；真实回执必须能解析新路径。禁止重新创建已完成的对象、增加步骤或改变提示词。新计划通过既有版本 CAS 提交，后续准入和结算使用新版本；已完成步骤及其结果保留。修复不合法、回执缺失、达到次数上限或存在未知写操作时停下，不自动重发写操作。

对带已验证现场的 MCP 任务，工具筛选同时接收当前对象上下文；服务可配置 `planning_tools`（远端原始工具名数组），为选中的业务工具保留已授权的上下文读取工具。此配置不授予新权限。模型精确引用了被初筛遗漏的工具时，只能从完整的已授权目录补入，不能发现其他服务的工具。

执行前独立审查原始请求中的全部并列要求、修改对象与保留要求，缺项或用创建替代修改会退回修正；两轮仍不通过则不执行。每个实际写步骤自动补上按步骤 ID 核对的成功条件。已有同服务读取工具时，最后一次写入之后必须有读取步骤。

执行后再用真实工具回执逐项核对原始要求，聊天里的“已完成”不作为证据；操作前现场也不覆盖更新后的读取回执。无法核对或仍有遗漏时返回部分完成及缺项，不将一次接口成功等同于整项任务成功。上述规则不包含某个平台的字段或工具名，平台特有的安全改名、草稿编辑和完整保存约束由接入平台实现。

## 通用服务与平台私有服务

一个租户可能同时接两类 MCP 服务端，两类用**同一套绑定**表达，差别只在 `scope_json`：

| 类型 | 绑定位置 | `scope_json` | 谁能用 |
|---|---|---|---|
| 平台私有 | 该平台所属租户 | `{"platforms": ["beeftv"]}` | 只有 `platform_id=beeftv` 的运行 |
| 通用（部署、本地合成这类"谁都能用"的能力） | `__shared__` 归属租户 | `{"platforms": ["*"], "tenants": ["*"], "tools": [...], "inner_tools": [...]}` | 任意平台、任意租户的已登录主体 |

判定唯一入口是 `src/webpage_agent/core/credentials.py::CredentialBinding.matches()`：两个通配必须**显式写出**，缺键或空列表都不放行（少写一个字段不构成放行）。`binding_for_server()` 在租户内查不到服务端绑定时才回退到 `__shared__` 行，并把「归属租户」（查凭据）和「调用租户」（判断租户维度）分开传。

三条收敛手段，按粒度从粗到细：

1. `scope.tools`：外层工具白名单（`mcp:<server>:<tool>`）。**发现期就生效**——模型看不到就不会先试一次再被拒；
2. `scope.inner_tools`：`run_tool(name=…)` 的内层能力白名单。通用绑定下 **fail-closed**：名字缺失、白名单为空、能力清单读不到（stdio 起不来 / `list_capabilities` 超时）都在本地拒绝，不发远端请求；
3. 工具政策：通用服务没有登记政策时按内置 `builtin:universal` 放行（否则每接一个平台都要补政策），但租户**只要登记了**命中该工具的政策就按政策走（可加审批、可整体关掉）。

登记通用服务只能走运维脚本，管理 API 明确拒绝：

```bash
ALLOW_GLOBAL_BIND=true python scripts/bind_platform_mcp.py \
    --tenant __shared__ --platform '*' --server publishing \
    --credential-ref env:PUBLISH_TOKEN --audience global \
    --tools mcp:publishing:deploy_site,mcp:publishing:update_site
```

`ALLOW_GLOBAL_BIND=true` 是运维专属开关，`/api/admin/credential-bindings` 的 `platform_id` 来自宿主平台自报，允许它写 `'*'` 就等于允许任何登记过的平台把自己那把钥匙升级成跨平台通用，因此 `src/webpage_agent/api/routes.py::_reject_reserved_scope` 直接 400 拒绝。

通用服务把入口开放给了所有人，因此**必须**配 §「工具权限适配」之外的额外约束：共享凭据要最小权限、要有限流/配额、服务端入参不得接受宿主业务标识（`source_path`、业务对象 id 这类"读本机/读别人数据"的参数一律禁用或只允许中台产物路径）。


## 验证

`python -m unittest tests.test_platform_context_api tests.test_scene_context tests.test_ticket_auth` 可验证上下文握手、账号/企业/平台绑定、篡改拒绝和票据合同；不使用生产数据库，也不调用付费模型。数据库兑换与对象隔离测试继续需要独立的 `PG_TEST_DSN` 测试库。
