# 连接器（MCP Server）设计与实现

> 目标：复刻 WorkBuddy 输入框「+ → 连接器」的交互：面板里一排 MCP Server，**打开开关就连接对应的 MCP Server**，关闭就断开。

## 1. WorkBuddy 这块怎么设计

从截图看，输入框左侧的「+」是一个统一入口：

```
+ ── 添加文件
     引用对话中的文件
     ─────────────
     模式   >
     专家   >
     技能   >
     连接器 >
```

点「连接器」弹出一个面板：

```
搜索连接器
GitHub          [开关 ON]     已连接
LibTV           [开关 OFF]    已配置未连接
通达信          [连接]        需要授权/配置
腾讯自选股      [连接]
QQ邮箱          [连接]
ima             [连接]
乐享知识库      [连接]
腾讯文档        [连接]
...
管理连接器
```

三种状态对应三种行为：

| UI | 含义 | 点击 |
| --- | --- | --- |
| 开关 ON | 已连接，工具可被 agent 使用 | 关闭 → 断开 |
| 开关 OFF | 已配置，未连接 | 打开 → 建立 MCP 会话、发现工具 |
| 「连接」链接 | 未配置/未授权 | 进入 OAuth / API Key / 账号绑定 |
| 「管理连接器」 | 完整市场/设置页 | 跳到连接器管理 |

原理：一个连接器 = 一个 **MCP Server 配置**（transport + url/command + 凭证）+ 连接状态 + 工具清单。开关触发连接/授权，后端保存用户级绑定；agent 在工具清单里看到这些工具。

![+ 菜单](assets/workbuddy/plus-menu.png)

![连接器面板](assets/workbuddy/connectors-panel.png)

## 2. 本项目实现

### 2.1 前端

- 输入框左侧新增 `+` 按钮（`#plusBtn`）；
- 菜单 `#plusMenu`：添加文件 / 引用文件 / 模式 / 专家 / 技能 / 连接器；
- 连接器面板 `#connectorPop`：搜索、列表、开关、管理连接器；
- 打开开关调用 `POST /api/connectors/{id}/connect`，关闭调用 `disconnect`；
- 未配置的连接器显示「配置」，提示去 `.env` 的 `MCP_SERVERS` 添加；
- 「管理连接器」跳到技能中心的连接器 tab；
- 「本轮选哪些连接器」仍由面板/胶囊控制（客户端 localStorage），用户动作后会
  回写 `PATCH /api/sessions/{id}/addons {connector_ids}`，与专家/技能同一份会话选择
  （见 [addons.md](addons.md)）。服务端解析层以会话选择为准，消息里带的
  `connector_ids` 只作迁移兜底。

### 2.2 后端

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/connectors` | 已配置的 MCP Server + 目录里未配置的连接器 |
| POST | `/api/connectors/{id}/connect` | 真实建立 MCP 会话，`list_tools()`，记录工具数 |
| POST | `/api/connectors/{id}/disconnect` | 关闭开关（保留配置） |
| GET | `/api/connectors/{id}/tools` | 最近一次连接发现的工具 |

数据源：

- MCP Server 配置：`.env` 的 `MCP_SERVERS`，解析在 `integrations/mcp_servers.py`；
- 连接：`integrations/mcp_adapter.list_server_tools(server)`，支持 `streamable_http` / `sse` / `stdio`；
- 状态：`profile/skillhub/connectors/<tenant>__<user>.json`；
- 未配置连接器：`skillhub/catalog.py` 的 `CONNECTORS`。

## 3. 当前本机可用的连接器

| id | 名称 | transport | 实测 |
| --- | --- | --- | --- |
| `publishing` | 默认服务端（MCP_*） | streamable_http | ✅ 5 个工具 |
| `libtv` | LibTV 视频/图片/音频创作 | streamable_http + OAuth | 每人自行授权一次（见 §6） |
| `libtv-local` | LibTV（本机桥） | stdio | ✅ 71 个工具（单用户） |
| `beeftv` | BeeFTV 画布 | stdio | ⚠️ 需要已验证的登录来源主体 |

`publishing` 打开开关就会真实连上并发现工具；`beeftv` 在未登录/无主体上下文时会明确报错，不会假装成功。
`libtv` 与 `libtv-local` 是同能力的两种接法，区别在 §6 末尾。

## 4. 怎么新增一个连接器

在 `.env` 的 `MCP_SERVERS` 里加一条，然后重启服务：

```json
[
  {
    "id": "github",
    "title": "GitHub",
    "transport": "streamable_http",
    "url": "https://your-github-mcp.example.com/mcp",
    "token": "..."
  },
  {
    "id": "my-stdio",
    "title": "本地工具",
    "transport": "stdio",
    "stdio_command": "node",
    "stdio_args": ["/path/to/server.mjs"]
  }
]
```

重启后打开「+ → 连接器」，新条目会出现，开关可用。未配置的条目会显示「配置」。

## 4.1 需要登录主体的 stdio MCP（BeeFTV）

BeeFTV 的 MCP 配置里带：

```json
{
  "id": "beeftv",
  "transport": "stdio",
  "forward_subject": true,
  "stdio_env": {"BEEFTV_BASE_URL": "http://127.0.0.1:8004"}
}
```

`forward_subject` 表示 MCP 子进程需要知道“当前是哪个登录用户”。原来的实现只在 **agent run** 里取 `runs.platform_subject`；连接器开关是在 run 之外做工具发现，所以会报：

```text
RuntimeError: MCP 服务要求已验证的登录来源主体
```

修复分两步：

1. 工具发现接受一个已验证的 `subject`：
   `list_server_tools(server, subject=principal.subject)`；
   `connect_session(..., subject=...)` 把它写进 stdio 的
   `MCP_CALLER_SUBJECT`；
2. BeeFTV 的 MCP 还要求 `MCP_BEARER_TOKEN`（hub MCP token）。
   在 `MCP_SERVERS` 的 beeftv 条目加：

```json
{
  "id": "beeftv",
  "token": "从 .local/secrets/hub-mcp-token 读取",
  "stdio_env": {"BEEFTV_BASE_URL": "http://127.0.0.1:8004"}
}
```

BeeFTV 服务端本身也要用同一个 token 文件启动：

```text
BEEFTV_HUB_MCP_TOKEN_FILE=/Users/.../BeefTV/.local/secrets/hub-mcp-token
```

本项目通过 `run.py` 启动 BeeFTV 时已经会自动读取这个文件。

修复后 BeeFTV 连接返回 216 个工具，输入框出现 🖼 标签：

![BeeFTV 连接成功](assets/workbuddy/beeftv-connected.png)

## 5. 边界

- 开关会**真实建立 MCP 会话并调用 `list_tools()`**，这是“连接”的证据；
- 当前“关闭”只更新本用户的连接状态，不强制杀掉已池化的 stdio 进程；
- agent 每次对话实际能用哪些工具，仍由现有工具策略/绑定决定；把「已启用连接器」注入每轮工具清单是下一步；
- 未配置的连接器需要先在 `MCP_SERVERS` 加配置，不能凭空连接；
- WorkBuddy 的「连接」还包含 OAuth/账号授权流程，本项目暂未实现 OAuth，只用 token/stdio env。

## 6. OAuth 连接器（按用户授权）

### 6.1 什么时候需要

对方要求"代表某个具体的人"访问他的私有数据（GitHub 仓库、LibTV 项目、飞书日历），
且不接受中台预置的应用级 token。这时凭据只能由用户本人在授权页上同意换来。

判断顺序：**要什么认证 → 要不要代表某个用户 → 支持不支持动态注册**。
第三种情况（对方有 `registration_endpoint`）不需要去对方后台填表，客户端自己注册自己。

### 6.2 配置

```json
{
  "id": "libtv",
  "title": "LibTV 视频/图片/音频创作",
  "transport": "streamable_http",
  "url": "https://mcp.liblib.tv/mcp",
  "auth": "oauth",
  "oauth": {
    "issuer": "https://mcp.liblib.tv",
    "scopes": ["libtv:account:read", "libtv:project:read", "libtv:project:write",
               "libtv:media:write", "libtv:task:write", "libtv:export:write"],
    "client_name": "智能体中台"
  }
}
```

| 字段 | 作用 | 缺省行为 |
| --- | --- | --- |
| `auth` | `oauth` 表示按主体授权 | 空 = 用静态 token（历史行为） |
| `oauth.issuer` | 授权服务器地址，用来读 well-known | **必填**，否则条目被拒绝 |
| `oauth.scopes` | 申请的权限范围 | 空 = 不带 scope 参数 |
| `oauth.client_name` | 授权页上显示的应用名 | `智能体中台` |
| `oauth.resource` | RFC 8707 资源标识 | 取 `url` |
| `oauth.redirect_path` | 自定义回调路径 | `/api/connectors/<id>/oauth/callback` |
| `oauth.client_id` / `client_secret_ref` | 预注册模式的应用凭据 | 有 `client_id` 时不再动态注册 |

校验是**加载时**做的：`auth=oauth` 配在 `stdio` 上、或缺 `issuer`，条目直接被跳过
并在 `/api/health` 的 `LAST_ERROR` 里报出来——这两类配错的共同点是"看起来能连，
实际上永远 401"。

### 6.3 三条链路

```
POST /api/connectors/{id}/oauth/start
  ├─ 读 well-known（oauth-authorization-server，退 openid-configuration）
  ├─ 没有 client_id → POST registration_endpoint 动态注册（公共客户端 + PKCE）
  ├─ 生成 state + PKCE verifier → 存 profile/skillhub/oauth_state/<state>.json（0600）
  └─ 返回 authorize_url + expires_in

GET /api/connectors/{id}/oauth/callback?code=&state=
  ├─ 取 state 并**立即删除**（一次性，重放失败）
  ├─ 校验 server_id / 过期 / state 里的 tenant+user
  ├─ code + verifier 换 token
  ├─ 落盘 secrets/oauth/<tenant>__<user>__<id>.json（0600）
  └─ 登记 mcp_server_bindings + credential_bindings(subject_kind=user, scope.auth=oauth)

运行期调用
  └─ mcp_connections.bearer_for → oauth_store.ensure_access_token
       过期前 60s 自动 refresh；refresh 被 4xx 拒绝就清凭据并要求重新授权
```

回调**不依赖登录 Cookie**：它是浏览器从对方站点跳回来的，SameSite 可能把 Cookie 拦掉。
身份由 state 承载，这也是 state 必须一次性、必须过期的原因。

### 6.4 一人一份，不能共用

- 凭据文件按 `tenant__user__server` 命名，`0600`，`secrets/oauth/` 目录 `0700`；
- 绑定记 `subject_kind='user'`，`scope.platforms=['*']`（同一个人的钥匙在任何登录平台都能用；
  缺这个键时带 `platform_id` 的运行会被 `matches()` 判成不适配）；
- 库里不存 token，`credential_ref` 指向文件，明文只在传输层短时取出；
- 拿 A 的 token 替 B 执行会被 `materialize_credential(expected_subject=...)` 拦成
  `CredentialMismatch`。

### 6.5 部署要求

| 配置 | 说明 |
| --- | --- |
| `OAUTH_PUBLIC_BASE` | 回调地址前缀。**生产必须配**，否则反代后会推断成内网 http，与动态注册时报的地址不一致 |
| `OAUTH_DIR` | 凭据目录，默认 `secrets/oauth` |
| `OAUTH_STATE_TTL_SECONDS` | state 有效期，默认 600 秒 |

回调地址只允许 `https://` 或 `http://127.0.0.1`：明文回环是桌面联调唯一安全的选择，
公网明文等于把授权码交给同网段任何人。

### 6.6 排障

| 现象 | 原因 |
| --- | --- |
| 点开关返回 428 | 还没授权（正常）。前端据此弹「授权」，不要当失败 |
| 授权页报 redirect_uri 不匹配 | 换过域名/端口但沿用了旧的动态注册结果；重新点「授权」会重新注册 |
| 回调页「state 无效或已过期」 | 超过 10 分钟，或重复使用了同一个回调链接 |
| 调用时 401 | token 被对方提前吊销；下次调用会强制 refresh，仍失败就要求重新授权 |
| 授权后仍显示「待授权」 | 服务没连上数据库，绑定没写进去；看回调页的提示 |

### 6.7 与 `libtv-local` 的区别

|  | `libtv`（oauth） | `libtv-local`（stdio 桥） |
| --- | --- | --- |
| 身份 | 每个登录用户各自的 token | 一份 `~/.libtv/credentials.json`，全中台共用 |
| 适合 | 多人共用一个中台 | 中台装在每个用户自己电脑上 |
| 授权 | 面板点「授权」，各自扫码 | `node lib/cli.mjs login`，本机扫一次 |
| 多用户风险 | 无（一人一份） | 项目、任务、配额全串到同一个微信账号 |
