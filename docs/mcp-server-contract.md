# 企业 MCP Server 对接协议 v1

目标：企业通过 MCP Server 暴露已有业务能力，中台通过同一套发现、规划、授权、执行、恢复与结果存储机制调用它们。接入一个新行业，不应要求中台新增企业专属 Python 文件。

本文是**本中台的接入约定**，建立在 MCP 工具协议上；`_meta.agent_hub` 是我们的可选扩展，不是 MCP 官方标准。本文区分已实现的能力与后续建设项，不把符合工具协议等同于已完成单点登录、部署和权限配置。

## 1. 责任边界

| 企业 MCP Server 负责 | 通用中台负责 |
| --- | --- |
| 业务操作、对象权限、业务默认值、事务与版本冲突 | 工具发现、参数绑定、计划和完成条件校验 |
| 提供可准确调用的输入和输出定义 | 授权检查、可信动作身份、动作账本、暂停与恢复 |
| 幂等回放、持久任务号、真实执行结果 | 隔离存储、有限预算、结果引用、用户回执 |
| 在 Server 层封装已有业务接口 | HTTP/stdio 连接和凭据分发 |

例如“新建画布”所需的完整默认文档应由企业的 MCP 工具构造，可以调用已有后端接口；中台不保存画布模板，也不合成企业专属 REST 请求。MCP Server 可以独立部署，不要求把中台逻辑写入企业业务后端。

## 2. 传输与认证

生产接入优先使用 Streamable HTTP；本机和受控部署支持 stdio。地址、凭据引用与企业/用户归属由中台管理员登记，模型不能指定或更换调用主体。

HTTP 使用当前凭据绑定解析出的 Bearer token。企业必须校验 token 对应的真实业务权限；模型参数中的 `tenant_id`、`user_id`、角色或界面上下文都不能作为授权依据。

stdio 不接受 HTTP 认证头。配置 `credential_env` 指定子进程接收当前主体凭据的环境变量名；其它环境变量通过 `stdio_env` 的受控引用提供。示例：

```json
{
  "id": "inventory",
  "transport": "stdio",
  "stdio_command": "/opt/company/bin/inventory-mcp",
  "stdio_args": ["serve"],
  "credential_env": "INVENTORY_ACCESS_TOKEN",
  "stdio_env": {"INVENTORY_BASE_URL": "literal:https://inventory.example/api"}
}
```

引用支持 `env:NAME`、`file:/controlled/path`；`literal:` 适合普通配置或本机联调，生产密钥应使用前两种。子进程不会被额外交付整份中台环境（SDK 仍会补充运行所需的基础环境）。有显式凭据但没有环境映射时拒绝连接，不静默丢弃凭据。凭据、环境或主体不同不会复用同一 stdio 会话。

中台登录认证与中台调用企业 MCP 的认证是两条链路。目前中台登录使用已实现的平台票据模式；OIDC/SSO 自动接入不在此版实现中。

## 3. 工具发现与描述

使用标准 `tools/list`，支持分页。优先“一项业务动作对应一个工具”，不要把所有业务藏在一个缺少具体参数定义的 `run_tool(name, inputs)` 中。旧通用调用工具仍兼容，但不是新协议的推荐接法。

每个工具必须提供：

- 稳定的 `name`，足以说明用途、限制、对象定位方式和完成语义的 `description`。
- 有效的 object `inputSchema`，明确必填参数、类型、对象结构与数组元素。
- 有效的 object `outputSchema`，明确下游会引用的 ID、版本、任务号、结果位置等字段。
- 明确的 `annotations.readOnlyHint`；写工具还须明确 `destructiveHint` 和 `idempotentHint`。

中台保留输出 schema，并在规划提示中与输入 schema 一起提供；调用成功时验证 `structuredContent`。下游按真实输出字段和 JSON Pointer 绑定参数，不根据输入字段名猜测返回字段。服务端描述、注解和扩展声明是能力信息，**不会自动生成授权政策或授予权限**。

## 4. 写操作与幂等

需要可恢复写操作时，工具可以通过 `_meta.agent_hub` 声明一个由中台注入的幂等字段：

```json
{
  "name": "create_record",
  "description": "新建库存记录，完成后返回 record_id。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "name": {"type": "string"},
      "operation_id": {"type": "string"}
    },
    "required": ["name", "operation_id"],
    "additionalProperties": false
  },
  "outputSchema": {
    "type": "object",
    "properties": {"record_id": {"type": "string"}},
    "required": ["record_id"]
  },
  "annotations": {
    "readOnlyHint": false,
    "destructiveHint": false,
    "idempotentHint": true
  },
  "_meta": {"agent_hub": {"version": 1, "idempotency_field": "operation_id"}}
}
```

`idempotency_field` 必须是顶层独立 string 属性，不应在组合条件中再引用它。中台将它从模型输入定义中移除，在账本准入后注入已持久化的 `action_id`，然后验证完整输入。业务参数不得覆盖它；没有可信动作身份的直调会被拒绝。

Server 必须按调用主体限定幂等键的作用域，持久保存“键—参数指纹—执行结果”：同一键和参数返回已有结果；同一键不同参数拒绝执行。不同动作即使参数完全相同也不能互相去重。中台不使用“工具名＋业务参数哈希”替代动作身份。

幂等声明不会让中台自动重发结果未知的写操作。网络超时、断流或发送后的输出校验失败仍可能已经产生业务效果，必须保留待核实状态。

## 5. 版本冲突与业务边界

企业 Server 负责封装版本读取和写入规则，或在输入输出中明确暴露版本。冲突必须作为真实失败返回，不得假装完成。中台不会根据 `expectedRevision` 字段名自动读取新版本并覆盖用户修改，也不会自动改业务对象名称。

复合动作应由企业 Server 提供有明确事务和失败语义的工具。中台不会为某家企业拼空白对象模板、展开特定模型目录、或给 `id` 自动补出 `nodeId` 别名。

## 6. 结果与错误

可编排工具成功返回符合 `outputSchema` 的 `structuredContent`，可附文本、图片、音频、资源链接或内嵌资源。只有文本或媒体的旧工具也可调用，但不满足本版对可编排输出的验收要求。

中台保存原始内容分块；模型视图不直接携带图片或音频的 base64。生产应开启结果信封 v2（正式票据模式自动开启），通过数据库登记的 `result_id` / `part_id` 按归属读取。资源链接不会被中台自动下载。二进制分页返回 `data_base64`，offset 和 limit 是字节数。

单条结果有体积上限；超限明确标记不完整，不把截断内容视为完整事实。输出与声明不符时不能把请求判成成功，也不能推断远端没有执行。

失败使用 MCP `isError=true`，内容中返回可安全展示的错误码、说明及必要操作指引，不包含 token、内部栈或敏感路径。不要使用 HTTP 200 加业务 `code=400` 但 `isError=false` 的伪成功；旧格式中的部分错误启发式仅作兼容，不是新接入保证。当前通用通道对 `isError` 使用安全的通用错误信息，不承诺将所有上游错误详情直接展示给用户。

“提案已创建”“任务已受理”“执行完成”必须明确区分。远端状态变更的完成条件指向实际写操作回执；`artifact_exists` 只用于本地产物，查询成功不能证明远端产物已经生成。

## 7. 长任务

当前实现使用版本化异步合同：提交工具返回持久任务号；查询工具按任务号返回状态和结果；合同配置映射字段路径与状态值。参考 `contracts/async_tools.example.json`。

合同可由运维提供数据配置，无需增加 Python 适配器。未知状态不算成功；只有提案、没有持久任务号和查询方式时，不承诺进程重启后可恢复。建议将任务状态放入 `job.status` 等明确的业务结构中，避免与旧兼容格式的顶层调用 `status` 混淆。

标准 Tasks 协商、合同自动发现和任务取消的统一交互属于后续建设；此版没有把手工合同包装为已经自动接入。

## 8. 只读接入检查

管理员配置好连接后运行：

```bash
python -m scripts.check_mcp_contract --server-id inventory
```

只发送初始化和工具清单请求，不调用业务工具。检查描述、输入输出 schema、读写注解和自定义幂等声明。JSON 报告 `ok=true` 且退出码为 0 表示静态合同通过；缺工具、连接失败或合同不合格返回非零。

这不是业务验收：不能由静态清单证明幂等真的持久、权限真的正确、版本冲突不会覆盖数据。企业仍须使用测试账户验证这些行为。生产强制模式还需登记企业连接、主体凭据和工具政策；检查通过不会自动放行新工具。检查命令使用管理员配置的连接凭据，不代替在线运行的用户身份。

## 9. 当前接入成本与后续目标

本版已经减少专属代码的需求，但仍需管理员配置 `MCP_SERVERS`、凭据绑定、政策，以及必要的异步合同。数据库中的 `config_ref` 不是动态连接配置的完整实现。

下一阶段优先建设租户级动态连接管理、统一接入页面、OIDC/SSO、按授权范围检索工具、标准异步能力协商。届时以“接第二种行业 Server 时中台零新增适配代码”为验收目标，不以适配文件数量增加为能力增长指标。

参考：[MCP 工具协议](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[MCP 授权协议](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)。
