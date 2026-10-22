# 平台接入契约：智能体中台登录

> 面向客户平台的研发同学。读完这一页 + 复制一段示例代码，就能把"平台用户"接进中台。
> 版本：2026-10 · 对应中台实现 `app/auth_connections.py` / `app/ticket_auth.py` / `app/auth.py`

## 0. 一句话

平台**继续用自己的登录系统**；用户登录后，平台**后端**用自己的一把私钥签一张
**短期、一次性**的票据，把它 POST 给中台换一个**中台自己的登录会话**。之后浏览器
带着中台会话访问中台的一切（聊天、历史、画像、事件流、产物）。

```
浏览器 ──(平台自己的登录 cookie)──▶ 平台后端
                                    │  ① 确认当前登录用户（平台已有能力，不用改）
                                    │  ② 用平台私钥签一张票据（RS256，≤60 秒）
                                    ├──▶ ③ POST /api/auth/exchange
                                    ◀── ④ Set-Cookie: agent_session=…（中台会话）
浏览器 ──(agent_session)──▶ ⑤ 中台全部业务接口
```

三条不可协商的边界：

1. **签票身份必须来自平台已验证的登录态。** 不能接受浏览器传一个 `user_id` 就代签。
2. **票据只由平台后端持有**，不写进页面、不放进 URL、不下发到浏览器。
3. **票据只能兑换一次**，且只能拿来兑换——它不是业务接口的凭证。

---

## 1. 中台需要平台提供什么

| 材料 | 说明 | 示例 |
|---|---|---|
| `connection_id` | 这次接入的唯一编号（中台登记用） | `acme-portal` |
| `issuer` | 票据签发方标识，**精确匹配** | `https://portal.acme.com` |
| `audience` | 中台接收方标识，固定值 | `agent-hub-login` |
| 公钥 | RSA 公钥（PEM），可多把并用 `kid` 区分 | `secrets/acme.pub.pem` |
| `kid` | 用哪把钥匙签；**每把钥匙一个 kid** | `acme-2026-10` |
| `identity_provider_id` | 平台账号体系的稳定编号。**共用统一账号中心的多个平台填同一个值**，独立账号体系各填各的 | `acme-idp` |
| 企业与平台映射 | 允许这个接入声明的 `tenant_id` / `platform_id` 清单 | `['tenant-acme']` / `['acme-portal']` |
| 浏览器接入方式 | 见第 7 节（推荐同源路径代理） | `/agent/* → 中台` |
| 测试账号 | 至少两个用户用于验收隔离 | `alice` / `bob` |

中台侧登记（形如 `secrets/auth-connections.json`）：

```json
[
  {
    "connection_id": "acme-portal",
    "issuer": "https://portal.acme.com",
    "audience": "agent-hub-login",
    "algorithms": ["RS256"],
    "public_key_file": "secrets/acme.pub.pem",
    "kid": "acme-2026-10",
    "allowed_tenants": ["tenant-acme"],
    "allowed_platforms": ["acme-portal"],
    "identity_provider_id": "acme-idp",
    "enabled": true
  }
]
```

> `algorithms` 只接受 `RS256`。中台会拒绝 `HS256`、`none` 等其它算法——这是 JWT 最经典的
> 两类攻击面，不做"兼容"。

---

## 2. 票据格式

**头部**

| 字段 | 值 |
|---|---|
| `alg` | `RS256`（RSA + SHA-256） |
| `kid` | 平台公钥编号，与登记的一致 |
| `typ` | `agent-login+jwt`（**必须**，防止别处的 JWT 被拿来冒用） |

**载荷（全部必需）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `iss` | string | 必须与登记的 `issuer` **完全相同** |
| `aud` | string | 固定 `agent-hub-login` |
| `sub` | string | 该用户在此**身份源**中的稳定标识。**永远不要用邮箱/手机号**，用不变的用户 ID |
| `tenant_id` | string | 企业标识，必须在允许清单内 |
| `platform_id` | string | 平台标识，必须在允许清单内 |
| `iat` | int | 签发时间（Unix 秒） |
| `exp` | int | 到期时间（Unix 秒）。**`exp - iat ≤ 60 秒** |
| `jti` | string | 票据唯一编号。同一用户每次登录都要换新的 |

**可选（只用于展示，不参与授权）**

| 字段 | 说明 |
|---|---|
| `name` / `display_name` | 用于首登时创建用户档案 |
| `email` | 同上 |
| `role` | **中台不采信**：角色与工具权限由中台配置决定，票据里写 `admin` 也不会提权 |

**时间与一次性**

- 允许 ±5 秒时钟误差（`iat` 略在未来、`exp` 刚过几秒仍受理）。请保证平台机器 NTP 正常。
- 有效期上限默认 **60 秒**（中台 `AUTH_TICKET_MAX_LIFETIME` 可调）。票据是"通行一次"的
  凭证，不要设成小时级。
- 同一张票据（`iss` + `jti`）只能兑换成功**一次**。多个中台实例同时收到也只成功一次。

---

## 3. 签发票据（示例代码）

### Python（PyJWT）

```python
import time, uuid, jwt

PRIVATE_KEY = open('/etc/acme/jwt-private.pem').read()   # 私钥只在平台后端

def build_login_ticket(user) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            'iss': 'https://portal.acme.com',
            'aud': 'agent-hub-login',
            'sub': user.external_id,          # 稳定 ID，不要用邮箱
            'tenant_id': 'tenant-acme',
            'platform_id': 'acme-portal',
            'iat': now,
            'exp': now + 30,                  # ≤60 秒
            'jti': uuid.uuid4().hex,
            'name': user.display_name,
        },
        PRIVATE_KEY, algorithm='RS256',
        headers={'kid': 'acme-2026-10', 'typ': 'agent-login+jwt'},
    )
```

### Node.js（jose）

```js
import { SignJWT, importPKCS8 } from 'jose';
import { randomUUID } from 'node:crypto';

const key = await importPKCS8(process.env.JWT_PRIVATE_KEY, 'RS256');

export async function buildLoginTicket(user) {
  const now = Math.floor(Date.now() / 1000);
  return await new SignJWT({
    tenant_id: 'tenant-acme',
    platform_id: 'acme-portal',
    name: user.displayName,
  })
    .setProtectedHeader({ alg: 'RS256', kid: 'acme-2026-10', typ: 'agent-login+jwt' })
    .setIssuer('https://portal.acme.com')
    .setAudience('agent-hub-login')
    .setSubject(user.externalId)
    .setIssuedAt(now)
    .setExpirationTime(now + 30)
    .setJti(randomUUID())
    .sign(key);
}
```

### Java（jjwt）

```java
String ticket = Jwts.builder()
    .header().keyId("acme-2026-10").type("agent-login+jwt").and()
    .issuer("https://portal.acme.com")
    .audience().add("agent-hub-login").and()
    .subject(user.getExternalId())
    .claim("tenant_id", "tenant-acme")
    .claim("platform_id", "acme-portal")
    .issuedAt(now).expiration(Date.from(now.toInstant().plusSeconds(30)))
    .id(UUID.randomUUID().toString())
    .signWith(privateKey, Jwts.SIG.RS256)
    .compact();
```

---

## 4. 兑换：`POST /api/auth/exchange`

**请求**

```http
POST /api/auth/exchange HTTP/1.1
Content-Type: application/json

{"ticket": "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFjbWUtMjAyNi0xMCIsInR5cCI6ImFnZW50LWxvZ2luK2p3dCJ9..."}
```

**成功（200）**

```http
HTTP/1.1 200 OK
Set-Cookie: agent_session=…; HttpOnly; Max-Age=7200; Path=/; SameSite=lax; Secure

{
  "user_id": "u_22422e5f1f1f46c2",
  "tenant_id": "tenant-acme",
  "role": "member",
  "platform_id": "acme-portal",
  "connection_id": "acme-portal",
  "auth_session_id": "as_9f2c…",
  "expires_at": 1792659000.12
}
```

- 会话凭证**只出现在 `Set-Cookie` 里**，响应体里没有原文，中台日志也不记。
- 数据库只存它的 SHA-256 摘要。
- 会话默认 2 小时（中台 `AUTH_SESSION_TTL`）。

**失败**

| 状态码 | 含义 | 平台该怎么处理 |
|---|---|---|
| **401** | 票据不可信：签名/接收方/签发方/过期/类型/越权企业平台/缺字段 | 看 `detail` 定位；**重新签一张**再试。别用同一张重试 |
| **409** | 票据已被兑换过 | **重新签一张新票据**（新 `jti`），不要重试旧票 |
| **400** | 请求格式不对，或中台未启用认证（`AUTH_MODE=off`） | 检查请求体；联系中台确认环境配置 |
| **503** | 中台暂时不可用（仓储未就绪 / 兑换事务失败） | 指数退避重试（1s、2s、4s…），最多 3 次；仍失败就对用户提示稍后再试 |

> `409` 的典型触发场景：平台后端超时重试了同一个请求。**正确做法是每次重试都重新签票**，
> 因为旧票可能已经被成功消费（兑换成功了但响应没回到平台）。

`detail` 里是可读的中文原因，例如：

```
票据无效：票据已过期
票据无效：签发方未登记或不接受该接收方：iss='https://evil' aud='agent-hub-login'
票据无效：该签发方无权声明企业 'tenant-b' / 平台 'acme-portal'
票据无效：有效期过长（3600s > 60s）
票据无效：票据类型不对：'JWT'，应为 'agent-login+jwt'
票据无效：签名无效
```

---

## 5. 会话的使用、续期与退出

### 使用

浏览器拿到 `agent_session` cookie 后，中台所有业务接口都只认它：

- `GET /api/me` → 当前身份（企业/用户/平台）
- `/api/tasks`、`/api/sessions/*`、`/api/profile`、`/api/tasks/{id}/events`（SSE）、
  `/api/tasks/{id}/artifact`（下载）
- **请求头不能改变身份**：`X-DSH-User` 之类在中台 `ticket` 模式下完全无效。

### 续期

会话到期（或快到期）时，**不需要用户重新登录**：平台后端再签一张新票据、再兑换一次即可
（用户登录态还在，这一跳是无感的）。建议在会话剩余 < 10 分钟时由前端触发一次
"静默续期"接口，平台侧发现中台会话过期就重新签票兑换。

### 退出

用户点退出时，平台后端**必须**同时通知中台撤销会话（否则用户退出平台后，中台侧仍有
最多 2 小时的有效会话）：

```http
DELETE /api/auth/session          # 带上 agent_session cookie
→ 200 {"ok": true, "revoked": true}
```

撤销是**即时生效**的：已连上的 SSE 会在下个检查点（默认 20 秒）被断开，并收到
`{"type":"end","status":"interrupted","detail":"登录已失效，请重新登录"}`。

---

## 6. 身份映射规则（务必对齐）

中台的身份唯一键是：

```
企业(tenant_id) + 身份源(identity_provider_id) + 外部用户(sub)  →  中台 user_id
```

由此推出三条平台侧必须遵守的规则：

1. **`sub` 必须稳定**。改了 `sub` 就等于换了一个人，历史与画像会断。
2. **`sub` 不能跨企业/身份源复用含义**。两个平台各自的 `user-123` 是两个中台账号；
   同一平台若有两个独立账号体系，请用两个 `identity_provider_id`。
3. **不要指望"邮箱相同自动合并"**。中台不做这件事（那会把两个真实用户并成一个）。

`user_id` 由中台分配、终身不变；平台不需要保存它，但可以记录用于排障。

---

## 7. 浏览器接入：推荐同源路径代理

中台独立部署，但浏览器通过**平台自己的域名**访问，避免跨站 Cookie 问题：

```
https://portal.acme.com/agent/        → 中台 HTML（index.html）
https://portal.acme.com/agent/api/*   → 中台 /api/*
```

要点：

1. **转发时保留 Cookie 与 Set-Cookie**（`agent_session` 要能回写给浏览器）。
2. **SSE 不能缓冲**：`/agent/api/tasks/*/events` 必须关闭代理缓冲
   （Nginx：`proxy_buffering off;` `X-Accel-Buffering: no`），否则过程不会流式出现。
3. **前端 API 前缀**：中台首页支持 `<body data-api-prefix="/agent/api">`，无需改代码。
4. **文件下载**（`/agent/api/tasks/{id}/artifact`）走同一 cookie，注意别让代理改写
   `Content-Disposition` / `Range`（视频要能拖进度条）。

Nginx 片段：

```nginx
location /agent/api/ {
    proxy_pass         http://agent-hub:8010/api/;
    proxy_set_header   Host $host;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering    off;              # SSE
    proxy_read_timeout 3600s;
}
location /agent/ {
    proxy_pass http://agent-hub:8010/;
}
```

> 跨站 iframe 方案也可以，但要额外处理浏览器第三方 Cookie 限制，请单独评估；仅调整
> `SameSite` 并不保证所有浏览器/隐私模式都可用。

---

## 8. 联调步骤（建议顺序）

```bash
# 0) 平台生成密钥对，把公钥交给中台
openssl genrsa -out jwt-private.pem 2048
openssl rsa -in jwt-private.pem -pubout -out jwt-public.pem

# 1) 中台登记接入（见第 1 节），确认能被读到
python -m scripts.list_capabilities            # 顺手确认中台起来了
#   中台日志里应出现「已登记接入」，或直接开 ticket 模式看启动是否成功

# 2) 平台后端签一张票，手工兑换一次，确认能拿到会话
curl -i -X POST https://hub.example.com/api/auth/exchange \
  -H 'Content-Type: application/json' \
  -d '{"ticket":"<刚才签的票>"}'
#   期望：200 + Set-Cookie: agent_session=…

# 3) 带会话查询身份
curl -s -b 'agent_session=<cookie 值>' https://hub.example.com/api/me
#   期望：{"user_id":"u_…","tenant_id":"acme","platform_id":"acme-portal",...}

# 4) 用同一张票再兑换一次 → 必须 409
# 5) 平台退出 → DELETE /api/auth/session → 再查 /api/me 必须 401
```

浏览器侧再验一遍：A 用户登录 → 聊天列表只有自己的会话；切到 B 用户 → 看不到 A 的任何
东西；刷新页面、SSE 重连、下载产物都仍是正确身份。

---

## 9. 验收表（双方一起过一遍）

| 场景 | 期望 |
|---|---|
| 正确票据兑换 | 200，`/api/me` 返回正确的企业/用户/平台 |
| 同一张票兑换两次 | 第二次 **409**，且不产生第二个会话 |
| 平台超时后用**同一张**票重试 | 同 409；平台应改为重新签票 |
| 票据过期 / 有效期 > 60 秒 / 改过载荷 / 用错私钥签 | **401**，不建立会话 |
| `aud` 不是 `agent-hub-login`、`iss` 未登记 | **401** |
| 票据声明了未授权的 `tenant_id` / `platform_id` | **401** |
| 票据里写 `role: admin` | 仍是普通成员（角色由中台配置决定） |
| 同一用户再次登录 | 中台 `user_id` 不变 |
| 两个平台各自的 `user-123` | 两个不同的中台账号，历史互不可见 |
| 不登录直接访问业务接口 | **401** |
| 修改 `X-DSH-User` 请求头 | 身份不变 |
| 退出后访问 | **401**；已连的 SSE 在 20 秒内断开 |
| B 用户拿 A 的任务/会话/产物 ID | **404**（不区分"不存在/无权限"） |
| 中台重启后再访问 | 会话仍有效；已撤销的仍是 401 |
| 密钥轮换（新旧 kid 并存） | 过渡期内两把钥匙各自有效；未登记的 kid 被拒 |

---

## 10. 常见问题

**Q：票据能不能直接放到前端，让浏览器自己调兑换？**
技术上可以（`/api/auth/exchange` 接受任何客户端的 JSON），但**不建议**：票据一旦进入
浏览器就可能出现在 DevTools、前端日志、浏览器扩展里。让平台后端做这一跳，票据的生命
周期只有几十毫秒。

**Q：能不能复用平台现有的 access token，不再单独签票？**
不建议。平台 token 通常有效期长、权限范围大、会被前端持有。中台只要"你是谁"，不需要
平台的全部权限。单独签一张 60 秒、只能换会话的票据，泄露面小得多。

**Q：中台会不会信任票据里自带的公钥地址 / JWKS URL？**
不会。公钥只从**中台的接入登记表**读取。票据里出现的任何地址都不会被访问。

**Q：多个平台共用一个统一账号中心怎么办？**
它们可以共用同一个 `identity_provider_id`（身份源），但各自的 `connection_id` /
`issuer` / 公钥独立——这样同一个自然人从不同入口进来仍是同一个中台账号。

**Q：中台侧的用户名/头像从哪来？**
首次登录时取票据里可选的 `name` / `email` 建档案。之后以中台侧资料为准；平台改名不会
自动同步（需要的话可以单独做同步接口）。

**Q：`sub` 用什么值最安全？**
平台内部不可变的主键（如 `user.id`、工号），不要用邮箱、手机号、用户名——它们会变。

---

## 附：中台侧配置项速查

| 配置 | 作用 | 建议值 |
|---|---|---|
| `AUTH_MODE` | 认证模式 | 正式环境 `ticket` |
| `AUTH_CONNECTIONS_FILE` | 接入登记表 | `secrets/auth-connections.json` |
| `AUTH_TICKET_MAX_LIFETIME` | 票据有效期上限（秒） | `60` |
| `AUTH_SESSION_TTL` | 中台会话有效期（秒） | `7200` |
| `AUTH_SESSION_COOKIE` | 会话 cookie 名 | `agent_session` |
| `AUTH_COOKIE_SAMESITE` | cookie SameSite | 同源代理用 `lax` |
| `AUTH_COOKIE_SECURE` | cookie Secure | 线上必须 `true`（本地 http 调试才 `false`） |
| `AUTH_SSE_RECHECK` | SSE 复查登录态周期（秒） | `20` |
| `POLICY_LEGACY_VISIBLE` | 存量默认数据是否对所有人可见 | **`false`** |
