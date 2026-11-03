# 以「插件 / 拓展」形式交付的可行性与对外承诺规范

> 面向：销售话术、客户前置协议、接入交付物设计
> 依据：本仓库现有实现（`src/webpage_agent/governance/ticket_auth.py`、`api/auth.py`、`docs/platform-integration.md`、`智能体中台运行机制说明.md` §18/§19），
> 以及 2026-11-03 在本机实测的验签行为（见 §4）
> 结论口径：**可以做成"装一个东西就对接"，但这个"东西"不能是浏览器扩展。**

---

## 0. 结论速览

| 问题 | 回答 |
|---|---|
| 能不能对客户说"装上插件就能对接"？ | **能，但必须限定形态**。交付物是「服务端接入包 + 前端嵌入片段（+ 可选 MCP 服务包）」，不是 Chrome/Edge 扩展。 |
| 最危险的做法是什么？ | 把 RS256 私钥放进任何客户端可分发的插件包里。**一旦私钥进浏览器，租户隔离就没了**（§4 有实测证据）。 |
| 客户侧最少要做什么？ | 后端 3 个端点（签登录票 / 签现场票 / 代理 `/agent/api/*`）+ 前端一行 `data-api-prefix`。用我们给的包，是填空不是开发。 |
| 现在缺什么才能这么承诺？ | 接入套件未版本化、无 Dockerfile/compose 一键部署、无 LICENSE 与第三方许可声明、无客户可自跑的验收脚本（§7）。 |
| 合同上最该锁的是什么？ | 不是"功能清单"，而是**责任边界表 + 可执行验收用例集 + 密钥保管责任 + 数据流向**（§6）。 |

一句话对外话术（建议）：
> "你们不需要改架构。后端引入我们提供的接入包（约 200 行，已有参考实现），加 3 个路由；前端加一个 HTML 片段。验收按我们给的用例集跑通即视为对接完成。"

---

## 1. 先看清楚：你的中台对"对接"的硬性要求

你的平台刻意把信任根留在客户侧。`docs/platform-integration.md` 明确写了责任边界：

> 宿主平台负责登录、业务对象归属校验、业务数据投影、签名、前端面板及工具权限适配。
> 中台负责可信接入登记、验签、一次性兑换、用户/企业数据隔离、任务执行和事件流。

拆成可执行项，客户侧必须落地 5 件事：

| # | 客户侧义务 | 能否在浏览器完成 | 为什么 |
|---|---|---|---|
| 1 | 用 RSA 私钥签 **登录票**（RS256，`typ=agent-login+jwt`，≤60s，一次性 `jti`） | ❌ | 私钥一旦在浏览器就等于公开（§4） |
| 2 | 用 RSA 私钥签 **现场票**（`typ=agent-scene+jwt`，`ctx` 为现场 canonical JSON 的 sha256） | ❌ | 同上；且 `ctx` 必须来自客户真实数据源 |
| 3 | **归属校验**：确认"这个项目/对象确实属于这个登录用户" | ❌ | 浏览器自报的 `user_id`/`workspace_id` 不可信，你的 `context/verify` 只验签不查业务归属 |
| 4 | **同源代理** `/agent/api/*` → 中台 `/api/*`，按会话保存中台 Cookie | ❌（可放边缘网关） | `docs/platform-integration.md`：不能把后端 Cookie 复制给浏览器，也不能关闭认证 |
| 5 | 前端 `<body data-api-prefix="/agent/api">` | ✅ | 纯前端，一行属性 |
| 6 | 可选：客户内部系统以 **MCP 服务**暴露为工具 | ❌（服务端） | 你已支持 stdio / HTTP+SSE + `X-MCP-Subject-Assertion` |

> 关键判断：**6 项里只有 1 项能纯前端完成**。所以"浏览器插件搞定一切"在架构上不成立——
> 剩下 5 项都需要一个跑在客户服务器上的组件。这正是"接入包"存在的原因。

---

## 2. 五种"插件"形态辨析

| 形态 | 装在哪 | 能持私钥 | 能做归属校验 | 与现有票据模型 | 商业上像什么 | 判定 |
|---|---|---|---|---|---|---|
| **A. 浏览器扩展**（Chrome/Edge MV3） | 用户浏览器 | ❌ 不可 | ❌ 不可 | **破坏** | 免费工具 | ❌ 不要做 |
| **B. 服务端接入包**（Python/Node/Java SDK + 3 个路由） | 客户后端 | ✅ | ✅ | **完全兼容** | 交付件（License） | ✅ 主推 |
| **C. MCP 服务包**（客户能力反向暴露） | 客户内网 | 不需要（走 `MCP_BEARER_TOKEN` / HMAC 断言） | 由客户 MCP 自身做 | 兼容（`bind_platform_mcp` 路径） | 能力接入 | ✅ 配套 |
| **D. 私有化部署包**（Docker Compose/Helm） | 客户机房 | ✅（客户自己生成） | ✅ | 兼容 | 私有化项目 | ✅ 最强承诺 |
| **E. 前端嵌入片段**（`data-api-prefix` / iframe） | 客户前端 | ❌ | ❌ | 兼容（需 B 或 D 配合） | 内嵌 SDK | ✅ 配合件 |

你代码里已经存在的对应机制：

- **B 的参考实现已经写好了**：`src/webpage_agent/api/auth.py`（验票+兑换+绑定）、`governance/ticket_auth.py`（验签）、`api/auth_connections.py`（登记表）；
  本地双平台演练：`scripts/mock_identity_platform.py`（可直接改造成"给客户看的参考集成 demo"）。
- **C 已支持**：`scripts/bind_platform_mcp.py`、`docs/platform-integration.md` §"所有客户默认使用来源平台的 MCP"，含 `__shared__` 通用服务与 `scope.inner_tools` fail-closed。
- **E 已支持**：静态页 `api/web/index.html`，`data-api-prefix` 可配。
- **D 只做了源码包**：`scripts/package_release.py` 产出 tar.gz + `SHA256SUMS`（allowlist：`src scripts contracts docs deploy tests .github`，带私钥/`sk-` 正则扫描、拒绝覆盖已存在产物）。**但 `deploy/` 只有 3 个 `.example`，没有 Dockerfile / compose**。

---

## 3. 各形态"装完能承诺什么 / 不能承诺什么"

### A. 浏览器扩展 —— 为什么这条路看起来最像"插件"却最危险

装上之后，扩展运行在员工浏览器里，可以做的是：注入面板 UI、调 `/api/tasks`、显示事件流。
**做不到**的是：签登录票、签现场票、验业务归属、保存客户后端的代理 Cookie。

为了让扩展"自己就能对接"，唯一的办法是把私钥打包进扩展。这在密码学上不叫"方便"，叫**把身份签发权交给每一个员工**。
证据见 §4。

额外工程风险：Manifest V3 后台 service worker 会休眠、跨域 `fetch` 受 CORS 与 SameSite 限制、第三方 cookie 在主流浏览器持续收紧、
企业 IT 常直接禁用未签名扩展（企业策略白名单）。**销售承诺"装个扩展就行"，最后大概率卡在客户 IT 部门。**

### B. 服务端接入包（推荐主推）

**装法**：`pip install our-agent-kit`（或邮件发 tar.gz / 内网私服），`app.include_router(agent_kit.router)`，填 4 个环境变量。

**能承诺**：
- 不改客户现有账号体系（他们继续用自己的登录）；
- 中台侧自动完成企业/用户隔离（`identity_provider_id` + `tenant_id` 映射，你的 `(tenant, idp, sub)` 唯一键）；
- 提供参考实现的票据签发、现场规范化哈希、SSE 代理、错误码映射；
- 前端一行属性即可接入。

**不能承诺**：客户业务对象的归属规则由客户写（你只给接口形状与示例）；客户侧异步任务的持久性由客户保证。

### C. MCP 服务包（作为"插件"卖给客户的第二件东西）

如果客户说"我们要让智能体操作我们的系统"，那**插件在他们侧**、**客户端在中台侧**：
客户装一个 MCP 服务（stdio 或 HTTP/SSE），你在中台登记绑定即可。

注意你代码里已有的硬约束（写进交付文档，避免客户误用）：
- 跨机 HTTP/SSE + `forward_subject` 必须配 `subject_secret_ref`，否则**建连前就拒绝**，不会退化成发明文主体头；
- `X-MCP-Subject-Assertion` 只证明"主体线索由持有密钥者签发且未篡改"，**不是**授权凭证，对端仍须自己做授权判断；
- 通用（`__shared__`）绑定必须显式写 `{"platforms":["*"],"tenants":["*"]}`，缺键不放行；`scope.inner_tools` 在通用绑定下 fail-closed。

### D. 私有化交付包（对"数据不出域"客户唯一的答案）

现状：源码 tar.gz 可用（`package_release.py`），但**没有容器化**。要能说"安装即用"，需要补：
`Dockerfile` + `compose.yaml`（app / worker / postgres / nginx 四件）+ 首启自动迁移（`scripts/migrate.py` 已有）+ 健康探针（`/api/health/live`、`/api/health/ready` 已有）。

对这类客户，"插件"应改口叫**一体机/部署包**，交付物是镜像 + 部署手册 + 一键升级脚本。

---

## 4. 实测：为什么私钥绝不能进浏览器插件

用你仓库里的验签代码直接跑（`PYTHONPATH=src`，仅本机、无数据库、无付费模型）：

```
【谁持有私钥，就能替谁签票 —— 中台只认签名，不认人】
  ✅ 验签通过  sub=张三(真实登录)      tenant=company-a → company-a / platform-a
  ✅ 验签通过  sub=李四(从未登录过)     tenant=company-a → company-a / platform-a
  ❌ 拒绝      sub=别家公司CEO         tenant=company-b → TicketError: 该签发方无权声明企业 'company-b' / 平台 'platform-a'

【用另一把私钥伪造同一 issuer 的票据】
  ✅ 伪造票据被拒 → TicketError: 签名无效
```

读法：

1. 中台**无法分辨**"张三真的登录了"和"有人拿私钥替张三签了一张票"。票据就是身份，签名有效即身份成立。
   所以私钥 = 该企业内**所有员工**的签发权。
2. 你唯一的护栏是 `allowed_tenants` / `allowed_platforms` 白名单（跨企业被拒）。
   也就是说：**私钥泄露的后果 = 该租户内任意身份被冒用**，不是全平台，但也足够严重（能读该企业全部会话、画像、产物）。
3. 浏览器扩展的代码、内存、网络请求对用户完全可见（解包、DevTools、`chrome://extensions` 加载已解压扩展、内存 dump）。
   **技术上不存在"把私钥藏在扩展里"这回事。**

现有 31 个用例已把这条边界钉住（`python -m unittest tests.test_ticket_auth`，本次实跑 **31 项全部 OK**），
其中与"密钥不能出服务器"直接相关的：

| 用例 | 钉住的边界 |
|---|---|
| `test_hs256_with_public_key_is_rejected` | 公钥当 HMAC 密钥的经典混淆攻击被拒 |
| `test_alg_none_is_rejected` | 算法固定，不接受 `none` |
| `test_platform_a_key_cannot_impersonate_platform_b` | A 平台私钥签的票在只登记 B 的连接上无效 |
| `test_unauthorized_tenant_is_rejected` | 越企业声明被拒 |
| `test_role_in_ticket_is_not_trusted_as_permission` | 票据里的 `role` 不作权限 |
| `test_wrong_kid_is_rejected_even_with_one_key` | 只登记一把公钥时 `kid` 写错也不能过 |

> 一句话给客户的安全说明：
> "登录票据由你们的服务器用私有密钥签发，私钥永不出你们的机房和我们的任何代码。我们只登记公钥。"
> —— 这句话既是安全承诺，也是**拒绝做浏览器扩展**的最强理由。

---

## 5. 推荐的交付形态（三件套）

### 5.1 交付物清单

| 编号 | 交付物 | 形态 | 现状 | 用途 |
|---|---|---|---|---|
| D1 | **接入套件** `agent-integration-kit` | pip 包 / tar.gz，语义化版本 | ⚠️ 需从 `api/auth.py` + `ticket_auth.py` + `mock_identity_platform.py` 抽取 | 客户后端填空式接入 |
| D2 | **前端嵌入片段** | 一段 `<script>` + `data-api-prefix` | ✅ 已有静态页，需封装成一行 | 客户前端挂载对话/事件流 |
| D3 | **客户端 MCP 模板** | 20 行 stdio server 示例 | ✅ `scripts/local_mock_mcp_server.py` 可改 | 让智能体操作客户系统 |
| D4 | **验收用例集** `acceptance/` | pytest / 脚本，客户可自跑 | ❌ 需新写 | 合同附件的"完成定义" |
| D5 | **部署包** | Docker image + compose | ❌ 缺 Dockerfile/compose | 私有化客户 |
| D6 | **对接手册** | PDF/MD（客户研发可独立完成） | ✅ `docs/platform-integration.md` 已很完整，需补多语言签票示例 | 减少售前人力 |
| D7 | **第三方许可声明** `THIRD-PARTY-NOTICES` | 文本 | ❌ 无 | 合规（见 §6.7） |

### 5.2 话术改写对照

| ❌ 别这样承诺 | ✅ 改成 |
|---|---|
| "装个插件就能对接" | "后端引入接入包（约 200 行）+ 前端加一个片段，验收用例跑通即完成" |
| "零改造" | "零架构改造：不改你们的账号体系、不搬数据、不动业务表" |
| "自动适配你们系统" | "业务对象归属由你们写 3 个函数，我们给参考实现" |
| "装上就能用" | "装上后走一遍 12 条验收用例（附件 A），全绿即交付" |
| "数据都在你们那边" | 明确写：**模型推理时数据流向**（见 §6.4），别含糊 |

---

## 6. 前置协议 / 合同该写的 9 条（可直接抄成条款骨架）

> 提醒：下面是**技术与商务要点**，不构成法律意见；正式合同请法务过一遍。
> 你的仓库里 `智能体中台运行机制说明.md` §18「对接合同」已经是技术侧合同，本节是**商务侧配套**。

### 6.1 交付物与版本
列 D1–D7 及版本号；约定"接入套件"独立版本化（如 `kit 1.x`），与中台主版本解耦。
**不写"最新版"**——写具体版本，避免客户永远等升级。

### 6.2 验收标准（最重要的一条）
把 §附录 B 的用例集作为**合同附件**，明确：
- 验收 = 用例全绿 + 双方签字确认的《接入验收记录》；
- 用例由**客户在自己环境**执行（证明可复现，也避免"演示环境能用、生产不能用"的扯皮）；
- 未过项列入缺陷清单，约定修复期；
- **明确不包含**：模型输出准确率、业务决策正确性（你的 §19 已经诚实写了"测试通过不证明模型完成率已验收"，保持这个诚实度，合同里也这么写）。

### 6.3 责任边界表（直接贴表）
| 事项 | 客户 | 我方 |
|---|---|---|
| 账号体系与登录 | ✅ | — |
| RSA 私钥生成与保管 | ✅（私钥不出客户机房） | 只登记公钥 |
| 业务对象归属校验 | ✅ | 提供接口形状 |
| 票据/现场票签名 | ✅ | 验签 |
| 企业/用户数据隔离 | — | ✅（`tenant_id` + 身份唯一键） |
| 任务执行与事件流 | — | ✅ |
| MCP 工具自身权限 | ✅（对端须自行授权） | 只转发已验签的主体线索 |
| 代理 Cookie 保管 | ✅ | — |

### 6.4 数据流向与合规（客户法务一定会问）
必须能回答，且答案要与代码一致：
- 客户业务数据进入中台后，**发送给哪个模型供应商**、是否留存、是否用于训练；
- 是否出境（境外模型 API = 出境，涉及个保法/数据出境评估）；
- 中台侧留存期限（会话/画像/产物）、导出与删除方式（你有 `/api/artifacts/{id}`、`gc-results`、`DELETE /api/auth/session`）；
- 是否支持"仅私有化、不出域"（= D5 部署包，**这是很多国企/金融客户的唯一可接受项**）。

> ⚠️ **现状缺口（勿对客户过度承诺）**：仓库里 `docs/model-request-policy.md` 讲的是**请求预算、门禁与失败重试**，
> 并**不覆盖**"数据留存期限 / 是否用于训练 / 出境评估"这三项。
> 这三项目前**没有对应文档和代码开关**，属于售前必须补齐的 P0 项（见 §7）。
> 在补齐之前，对客户的口径只说到"我们有请求治理与日志脱敏（响应不暴露密钥/内网地址）"为止，不要声称已具备数据留存策略。

### 6.5 凭据与密钥保管
写入：密钥轮换流程（登记表支持多 `kid`，`test_multiple_keys_require_kid` 已支持双钥并行）、泄露应急（撤销登记项 → 已连 SSE 按 `AUTH_SSE_RECHECK` 20s 复查断开）、
以及**"我方不接收也不存储客户私钥"**的书面承诺（这条能让安全评审快很多）。

### 6.6 变更、兼容与弃用
- 票据字段、`aud/typ`、`ctx` 哈希算法 = **接口合同**，变更须提前 90 天通知 + 双版本并行期；
- `ctx` 的 canonical JSON 规则（`ensure_ascii=False, sort_keys=True, separators=(',',':')`）必须逐字写进合同附件——跨语言实现最容易在这里翻车（你的文档已提醒浮点、Unicode、反斜线）；
- 中台升级不得要求客户同时升级接入包（反向同理）。

### 6.7 知识产权与开源许可（⚠️ 现状有缺口）
- **仓库当前没有 `LICENSE` 文件**。商业交付前必须决定：源码交付 / 二进制交付 / 仅 SaaS 使用，并配对应许可文本。
- 直接依赖许可：`fastapi`/`langgraph`/`langchain-core`/`jsonschema`/`pydantic` = **MIT**；`uvicorn` = **BSD-3-Clause**；
  **`psycopg` 3.3.6 = LGPL-3.0-only**（实测元数据）。LGPL 在"以独立 Python 包形式导入、不静态链接修改"的常规用法下可控，
  但**对外分发时必须附第三方许可声明并提供其源码获取途径**（D7）。给客户交付前跑一次 SBOM 是硬要求。

### 6.8 责任限制与 SLA
- 明确"模型输出仅供参考、不构成业务决策依据"，写赔偿上限（通常不超过年度费用）；
- SLA 区分：中台可用性（`/api/health/ready`）≠ 模型供应商可用性 ≠ 客户 MCP 可用性。三者混谈，出事必吵。

### 6.9 终止与数据交还
终止后客户数据导出格式、保留期、销毁证明；以及"客户私钥自持，我方无法代客户继续签票"这一事实——**这其实是对客户有利的条款，主动说**。

---

## 7. 差距清单（要补什么，按优先级）

| 优先级 | 缺口 | 现状证据 | 补法 | 估算 |
|---|---|---|---|---|
| P0 | 无 `LICENSE` / 无 `THIRD-PARTY-NOTICES` | `ls` 无 LICENSE；psycopg = LGPL-3.0-only | 定商业许可 + 生成通知文件 | 1 天 |
| P0 | **数据合规三件套缺失** | `docs/model-request-policy.md` 只讲预算/重试，无留存・训练・出境条款 | 写《数据处理说明》+ 配置项（留存天数、是否关日志正文） | 2 天 |
| P0 | 接入套件未独立版本化 | 逻辑散在 `api/auth.py`、`governance/ticket_auth.py`、`scripts/mock_identity_platform.py` | 抽 `agent_integration_kit`，仅暴露 `sign_login_ticket / sign_scene_ticket / mount_client_router` | 3–4 天 |
| P0 | 无验收用例集 | 现有 537 项是**中台自测**，非客户可跑的对接受验收 | 新写 `acceptance/`，12–15 条，客户环境可执行 | 3 天 |
| P1 | 无 Dockerfile / compose | `deploy/` 仅 `mcp-servers.json.example`、`nginx.conf.example`、`production.env.example` | 加四项服务 + `migrate.py` 首启 + 健康探针 | 2–3 天 |
| P1 | 多语言签票示例缺失 | 只有 Python 参考实现 | 补 Node/Java 两个 30 行签票示例（`ctx` 哈希一致性是重点） | 2 天 |
| P2 | 前端片段未封装 | `index.html` 需手工配置 | 封成 `<script src=... data-api-prefix>` 一行 | 1 天 |
| P2 | 客户 MCP 模板未产品化 | `local_mock_mcp_server.py` 是 mock | 改成骨架 + README | 1 天 |

---

## 8. 两周最小可行路线

```
D1–D2  P0 许可与合规：LICENSE 决策 + THIRD-PARTY-NOTICES + SBOM
D3–D6  P0 接入套件：抽包 + 双平台 demo 改造为"客户参考集成"
D7–D9  P0 验收用例集：12 条，客户环境可跑，输出《接入验收记录》
D10–D12 P1 容器化：Dockerfile + compose + 首启迁移 + 探针（私有化客户敲门砖）
D13    P1 Node/Java 签票示例
D14    对接手册补章：把 §6.3 责任边界表 + §附录 B 用例集并入
```

到 D14，你就可以对客户说一句**经得起追问**的话：

> "后端引入我们一个包、前端加一行、跑通 12 条验收用例即完成对接。私钥始终在你们机房，我们只登记公钥。"

---

## 9. 风险清单

| 风险 | 触发场景 | 缓解 |
|---|---|---|
| 客户 IT 禁用浏览器扩展 | 走了 A 方案 | 走 B/D，从第一天就不承诺扩展 |
| 跨站 iframe 第三方 Cookie 被拦 | 客户想 iframe 内嵌 | 主推同源路径代理 `/agent/api/*`，iframe 仅作展示层 |
| 客户后端不是 Python | 多语言集成 | 提供 Node/Java 签票示例 + 固定 `ctx` 序列化规范 |
| 客户把私钥交给外包/放进容器环境变量镜像 | 部署不规范 | 交付手册要求 KMS/挂载密钥文件，禁止进镜像 |
| 客户误把 `context` 当授权 | 让模型调危险工具 | 文档已写明"现场证明≠工具调用授权"，验收用例里加一条负面用例 |
| 通用 MCP 服务被"升级成跨平台钥匙" | 客户要求 `platform_id="*"` | 已有护栏：`/api/admin/credential-bindings` 直接 400（`_reject_reserved_scope`），运维开关 `ALLOW_GLOBAL_BIND` 不外发 |
| 承诺 SLA 混谈三方可用性 | 商务口头承诺 | 合同 §6.8 分三段写 |
| 开源许可瑕疵 | 分发源码时漏声明 | P0 提前补齐 |

---

## 附录 A：接入套件建议目录结构

```
agent-integration-kit/
├── README.md                 # 15 分钟接入
├── LICENSE
├── THIRD-PARTY-NOTICES
├── pyproject.toml            # version = 1.0.0（与中台主版本解耦）
├── agent_kit/
│   ├── __init__.py           # 只导出 4 个函数
│   ├── tickets.py            # sign_login_ticket / sign_scene_ticket
│   ├── scene.py              # canonical ctx 哈希（必须与中台逐字一致）
│   ├── proxy.py              # /agent/api/* 转发 + 每会话 Cookie 容器 + SSE 关缓冲
│   ├── errors.py             # 中台错误码 → 客户可读文案
│   └── config.py             # 4 个环境变量
├── examples/
│   ├── fastapi_app.py        # 客户后端最小示例
│   ├── node/sign.mjs         # 跨语言签票
│   ├── java/Sign.java
│   └── mcp_stdio_server.py   # 客户能力反向暴露
├── frontend/
│   └── snippet.html          # 一行 data-api-prefix
└── acceptance/               # 合同附件 D4
    ├── test_login_exchange.py
    ├── test_scene_binding.py
    └── test_isolation.py
```

## 附录 B：验收用例集（12 条，作为合同附件）

| # | 用例 | 期望 | 证明什么 |
|---|---|---|---|
| 1 | 客户后端签票 → `POST /api/auth/exchange` | 200 + `agent_session` cookie | 兑换链路通 |
| 2 | 同一张票再兑一次 | **409** | 一次性 `jti` 生效 |
| 3 | 改 `exp` 超 60 秒 | 401 | 时长约束 |
| 4 | 用 B 平台密钥签 A 平台票 | 401 | 跨平台不可冒充 |
| 5 | 声明未登记 `tenant_id` | 401 | 越企业被拒 |
| 6 | `GET /api/me` | `authenticated=true`，企业与平台一致 | 身份落库正确 |
| 7 | 两用户同名 `sub=user-123` 分属两平台 | 两个不同中台账号，历史互不可见 | 身份唯一键正确 |
| 8 | `POST /api/context/verify` 带正确现场票 | 200，返回现场快照 | 现场握手通 |
| 9 | 篡改 `context` 后复用旧 `context_assertion` | 拒绝 | `ctx` 哈希绑定生效 |
| 10 | `POST /api/tasks` + 订阅 events，中途断线后带 `Last-Event-ID` 重连 | 事件不丢不重、任务未被视为取消 | 事件流契约 |
| 11 | 相同 `Idempotency-Key` 重放 | 返回原任务，不新建执行 | 幂等 |
| 12 | 客户退出 → `DELETE /api/auth/session` → 再调业务接口 | 401，SSE 断开 | 撤权生效 |

## 附录 C：现有文档索引（对外交付时打包）

| 文档 | 给谁看 |
|---|---|
| `docs/platform-integration.md` | 客户研发（接入步骤，最核心） |
| `智能体中台运行机制说明.md` §18 | 客户研发 + 客户架构评审（字段级合同） |
| `智能体中台运行机制说明.md` §19 | 我方 + 客户（诚实说明验证边界） |
| `docs/model-request-policy.md` | 客户法务/安全（数据流向） |
| `docs/production-deployment.md` | 客户运维 |
| `docs/architecture.md` | 客户架构评审 |
| 本文 | 我方销售/售前（口径统一，**不外发**） |

---

*本文档为内部决策依据。对外话术以 §5.2 与 §8 末句为准。*
