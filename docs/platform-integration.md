# 通用宿主平台接入

任何宿主平台都通过同一份登录票据、现场上下文与任务 API 接入。本模块不按平台名称分支，不解析画布、工单或 CRM 等业务对象，不保管宿主平台的用户密码。

## 责任边界

宿主平台负责登录、业务对象归属校验、业务数据投影、签名、前端面板及工具权限适配。中台负责可信接入登记、验签、一次性兑换、用户/企业数据隔离、任务执行和事件流。

建议宿主后端作为浏览器的代理：每个宿主登录会话分别保存换回的中台 Cookie，只在服务端使用。禁止把所有访问者绑定到一个共享中台账号。登录票据不是业务请求凭据；现场证明也不是工具调用授权。

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

### 多项要求与修改结果核对

对带已验证现场的 MCP 任务，工具筛选同时接收当前对象上下文；服务可配置 `planning_tools`（远端原始工具名数组），为选中的业务工具保留已授权的上下文读取工具。此配置不授予新权限。模型精确引用了被初筛遗漏的工具时，只能从完整的已授权目录补入，不能发现其他服务的工具。

执行前独立审查原始请求中的全部并列要求、修改对象与保留要求，缺项或用创建替代修改会退回修正；两轮仍不通过则不执行。每个实际写步骤自动补上按步骤 ID 核对的成功条件。已有同服务读取工具时，最后一次写入之后必须有读取步骤。

执行后再用真实工具回执逐项核对原始要求，聊天里的“已完成”不作为证据；操作前现场也不覆盖更新后的读取回执。无法核对或仍有遗漏时返回部分完成及缺项，不将一次接口成功等同于整项任务成功。上述规则不包含某个平台的字段或工具名，平台特有的安全改名、草稿编辑和完整保存约束由接入平台实现。

## 验证

`python -m unittest tests.test_platform_context_api tests.test_scene_context tests.test_ticket_auth` 可验证上下文握手、账号/企业/平台绑定、篡改拒绝和票据合同；不使用生产数据库，也不调用付费模型。数据库兑换与对象隔离测试继续需要独立的 `PG_TEST_DSN` 测试库。
