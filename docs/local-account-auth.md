# 本地账号登录（第 4 种登录方式）设计

> 状态：**已实现并端到端验证**（2026-11-12）。实现记录见 §17。
> 目标：中台自己校验密码，能创建 `test/test` 之类的账号并直接登录，不依赖 BeeFTV 或任何外部身份服务。
> 前提：现有三条链路（平台账号密码、票据 / OIDC、短信验证码）一律不动，默认关闭。
> 追加需求（§16）：管理员后端账号管理——手动建号、停用后才能删除、删除 = 清空该用户全部信息。

---

## 0. 一句话结论

新增一条独立的「本地账号」链路：`local_accounts` 存密码哈希，登录成功后**复用**现有的
`login_platform_identity` + `_set_session_cookie` 建会话，身份仍写进 `identities`
（`identity_provider_id='local'`）。因此账号、角色、停用、活跃度、审计、退出登录、
`/api/me` 全部沿用现有机制，只需要新增「密码从哪来」这一层。

设计上明确回答最初的问题：**中台本地建账号密码，不需要 BeeFTV，但必须新增代码**，
因为当前 `app_users` 没有密码列（`store/pg_schema.py:263`），
`api/platform_login.py` 开头也写明「中台不存、不验密码」。

---

## 1. 现状盘点（2026-11-12 实测）

| 事实 | 位置 |
|---|---|
| 三条链路 | `api/routes.py:618` exchange、`api/routes.py:697` platform-login、`api/routes.py:838` sms-login；OIDC 在 `api/oidc.py:107` |
| 当前生效入口 | `/api/auth/platform-login/status` → `enabled=true, platforms=[beeftv]`；`/api/auth/sms-login/status` → `enabled=true`；`/api/auth/oidc/login` → 404 |
| 密码不在中台 | `store/pg_schema.py:263` `app_users` 只有 5 列；`store/pg_auth.py:87` 注释「密码不进这个库」 |
| 会话只存摘要 | `api/auth.py:53` `new_session_token()`、`api/auth.py:58` `digest_of()`、`store/pg_schema.py:428` `auth_sessions` |
| 会话解析自动查停用 | `api/auth.py:137` `principal_from_session()` 会读 `app_users.status != 'active'` → 401 |
| 身份唯一键 | `store/pg_schema.py:274` 主键 `(provider, subject)` + 部分唯一索引 `uq_identities_scope(tenant_id, identity_provider_id, subject)` |
| 现成限流 | `governance/core.py:157` `rate_limit(scope, limit)`，内部对 scope 做 SHA-256 后入库，不存明文 IP；配置 `AUTH_RATE_LIMIT_PER_MINUTE=10`（`core/config.py:76`） |
| 现成守卫 | `api/console_api.py:84` `require_all()`（platform_admin）、`:93` `require_trusted_origin()`（写操作校验 Origin） |
| 现成审计 | `store/pg_admin.py:660` `_audit(conn, payload, stamp)` 写 `events` 表 |
| 迁移机制 | `store/migrations.py` 当前 `LATEST = ADDONS = 'v22_addons'` |
| 哈希依赖 | 实测两个 venv 都**没有** argon2 / bcrypt / passlib；`hashlib.scrypt` 可用，`cryptography` 已装 |

---

## 2. 架构决策（先定这 7 条，后面都由此推导）

| # | 决策 | 理由 |
|---|---|---|
| 1 | **独立第 4 条链路，不塞进 `auth_platforms`** | 那张表的语义是「把密码 POST 给外部 `/auth/login`」，`base_url` 必填且要过 SSRF 校验。本地校验没有外部地址，硬塞进去要扩 `LOGIN_MODES`、允许 `base_url` 为空、改控制台表单和校验规则，把一张干净的表弄脏 |
| 2 | **身份仍写 `identities`，`identity_provider_id='local'`** | 控制台用户列表、活跃度、角色授予、停用、删除全都基于 `identities` + `app_users`（`store/pg_admin.py:151-174` 动态取身份源，无需改代码）。不复用就要在每处统计加分支 |
| 3 | **会话完全复用 `login_platform_identity`** | `store/pg_auth.py:74` 已经把「建号 / 复用身份 / 写 `auth_sessions` / 只存摘要」封装好了，且「身份已存在」分支不同步角色（正好让控制台授的角色不被登录冲掉）。登录时它只会命中已建好的身份 |
| 4 | **账号状态以 `app_users.status` 为唯一真相** | `principal_from_session()` 已检查。`local_accounts` 只管凭据和失败计数，避免两处状态打架 |
| 5 | **密码算法用 `hashlib.scrypt`，零新依赖** | 项目无 argon2/bcrypt。封一层 `LocalPasswordHasher`，参数编码进 `password_algo`，将来换 argon2id 只改这一层并支持登录时透明重算 |
| 6 | **首个账号走 CLI / 环境变量种子，不走控制台** | 否则「要超管才能建号，但超管得先登录」鸡生蛋。类比 `BEEFTV_SEED_USERS`，`LOCAL_AUTH_SEED_USERS` 仅在表为空时执行一次 |
| 7 | **自助注册默认关闭，先只做超管建号** | 开放注册等于任何人可建号；当前需求只是本机一个 `test/test` |

### 分层

```
web/index.html            登录弹窗第 3 个 tab（本地账号）
        ↓ POST /api/auth/local-login
api/routes.py             路由 / StrictBody / Set-Cookie（复用 _set_session_cookie）
        ↓
api/local_auth.py         归一化、密码校验、失败锁定、rehash、业务编排
        ↓
store/pg_local_accounts.py  local_accounts CRUD + 建号事务（挂在 PostgresStore mixin）
        ↓
store/pg_auth.py          login_platform_identity()  ← 已有，不修改
        ↓
auth_sessions / identities / app_users
```

---

## 3. 数据模型

### 3.1 新表 `local_accounts`（迁移 `v23_local_accounts`）

```sql
CREATE TABLE IF NOT EXISTS local_accounts (
    username             TEXT NOT NULL,
    tenant_id            TEXT NOT NULL DEFAULT 'default',
    user_id              TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    password_algo        TEXT NOT NULL DEFAULT 'scrypt-16384-8-1',
    password_updated_at  TEXT NOT NULL,
    failed_count         INTEGER NOT NULL DEFAULT 0,
    locked_until         DOUBLE PRECISION NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    last_login_at        TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    created_by           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, username)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_accounts_user
    ON local_accounts(user_id);
```

沿用项目风格：TEXT 存 ISO 时间戳、DOUBLE PRECISION 存 epoch、不建外键。
`password_hash` 的格式自带盐和参数，不另开列。

### 3.2 已有三张表怎么配合

| 表 | 写入内容 | 谁写 |
|---|---|---|
| `app_users` | 建号时插一行 `user_id='u_'+16hex`、`display_name=username`、`status='active'` | `create_local_account()` 事务 |
| `identities` | `provider='local'`、`subject=username`、`identity_provider_id='local'`、`tenant_id='default'`、`role` | 同上；登录时由 `login_platform_identity` 更新 `last_seen_at` |
| `auth_sessions` | `platform_id='local'`、`connection_id='local'`、`identity_provider_id='local'`、`subject=username`、`token_digest` | `login_platform_identity()`，完全不动 |

> **坑（必须写进代码注释）**：`identities` 主键是 `(provider, subject)`，**跨租户全局唯一**。
> 一期因此**只允许 `tenant_id='default'`**，建号接口遇到其它租户返回 400。
> 将来要支持多租户，把 `subject` 改成 `f'{tenant_id}:{username}'` 并做一次数据迁移，
> 不能只改唯一索引。`local_accounts` 的主键已经带 `tenant_id`，不需要重做。

### 3.3 迁移（照 `store/migrations.py` 开头的铁律做）

1. `store/pg_schema.py`：`SCHEMA` 快照末尾追加上面那段 DDL（保持新库完整）。
2. `store/migrations.py`：新增 `LOCAL_ACCOUNTS = 'v23_local_accounts'`，
   `UPGRADES` 追加**逐字相同**的 DDL 条目，`LATEST = LOCAL_ACCOUNTS`。
3. 只改 1 不改 2 → 存量库永远没有这张表，且 `verify_schema()` 会在启动时报
   `表.列` 缺失。上线前跑 `python -m scripts.migrate`，再用 `require_current()` 自检。
4. 迁移单独一个 PR 先合、先执行，不带功能代码：加表是幂等且无副作用的。

---

## 4. 密码体系

### 4.1 算法与存储格式

```python
# api/local_auth.py
SCRYPT_N, SCRYPT_R, SCRYPT_P, DK_LEN = 2**14, 8, 1, 32
ALGO = 'scrypt-16384-8-1'
FORMAT = 'scrypt$<N>$<r>$<p>$<salt_b64>$<hash_b64>'

def hash_password(password: str) -> str:
    raw = unicodedata.normalize('NFKC', password)
    if pepper:                       # 可选，LOCAL_AUTH_PEPPER
        raw = hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(raw.encode(), salt=salt, n=SCRYPT_N,
                            r=SCRYPT_R, p=SCRYPT_P, dklen=DK_LEN)
    return f'scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}'

def verify(password: str, encoded: str) -> bool:       # hmac.compare_digest 常量时间
def needs_rehash(encoded: str) -> bool:                 # 参数低于当前 ALGO 时为真
DUMMY_HASH = 固定预生成的合法哈希                      # 账号不存在时也跑满一次 scrypt
```

- `N=2**14` 单次约 16 MB 内存 / 60-100 ms，配合 IP 限流防止并发打爆内存。
- 每次哈希都用新盐；**同一个密码两次存储结果不同**。
- pepper 从环境变量读、绝不入库；轮换代价高，一期作为可选项，默认空。
- 登录成功且 `needs_rehash()` 为真时，静默重算并落库（改参数不需要全量重置密码）。

### 4.2 用户名与密码策略

| 项 | 规则 |
|---|---|
| 用户名 | `unicodedata.normalize('NFKC')` → 小写 → 去首尾空格 → 必须匹配 `^[a-z0-9._-]{3,32}$`；不接受邮箱/手机号，避免与短信身份源混淆 |
| 密码 | 不做 trim（空格算密码内容），只做 NFKC；长度按字符数计 |
| 最小长度 | `LOCAL_AUTH_MIN_LENGTH` 默认 **8**；`APP_ENV=production` 时代码强制下限 8（配 4 会在启动时报错）；非生产可设 4，**`test/test` 正是走这条** |
| 最大长度 | 128（防止超长输入把 scrypt 变成 DoS）；`StrictBody` 用 `Field(max_length=128)` |
| 弱口令 | 不在一期做强策略（黑名单/复杂度），但管理端建号时对非生产环境的短密码返回 `warning` 字段，前端显示黄字提示 |

### 4.3 防枚举 / 防时序 / 防爆破

- 账号不存在也用 `DUMMY_HASH` 跑一次完整 `verify()`，响应时间与「密码错」同量级。
- 所有失败统一 `401 {"detail":"账号或密码不正确"}`：账号不存在、密码错、账号停用，三种**同样文案**。
- IP 维度：`store.rate_limit('auth:local:ip:'+sha256(ip)[:16], settings.auth_rate_limit_per_minute)`，
  返回 `(allowed, retry)`，超限 429 + `Retry-After`。scope 入库前还会被 `rate_limit()` 再哈希一次，不存明文 IP。
- 账号维度：`failed_count` 连续 5 次失败 → `locked_until = now + 900`；成功后清零。
- 登录接口只记脱敏日志（`username` 的短哈希 / `user_id`），**绝不打印密码、哈希、pepper**；
  审计 payload 同样不含凭据字段。

---

## 5. 接口契约

所有请求体继承 `StrictBody`（`api/routes.py:211`，未知字段 422）。
所有 `GET .../status` 带 `Cache-Control: no-store`。

### 5.1 认证端点（无需登录）

| 方法 / 路径 | 请求 | 成功 | 说明 |
|---|---|---|---|
| `GET /api/auth/local-login/status` | — | `{enabled, allow_signup, min_length}` | 前端据此决定是否显示入口与注册按钮；不泄露账号列表 |
| `POST /api/auth/local-login` | `{username, password}` | 200 + `Set-Cookie: agent_session` | 响应体与 platform-login 一致：`user_id / tenant_id / role / first_login / auth_session_id` |
| `POST /api/auth/local-signup` | `{username, password, display_name?}` | 201 `{user_id}` | 默认 404（`LOCAL_AUTH_ALLOW_SIGNUP=false`）；**不自动发会话**，注册后需再登录一次 |
| `POST /api/auth/local-login/change-password` | `{username, old_password, new_password}` | 200 `{ok:true}` | 凭旧密码改密，不要求已有会话（配合「必须改密」）；成功后撤销该用户全部会话 |

`enabled` 判定与短信端点保持一致：

```python
enabled = (settings.auth_mode != 'off'
           and settings.local_auth_enabled
           and (settings.app_env != 'production'
                or (settings.auth_cookie_secure
                    and settings.local_auth_min_length >= 8)))
```

即生产环境必须同时满足 Secure cookie 与密码下限 8，否则视为未启用（启动自检会先报错）。
`AUTH_MODE=off` / 开关关闭时，登录端点与 `/api/auth/platform-login:701` 一样返回 400 / 404。

### 5.2 管理端点（需 `require_all` + `require_trusted_origin`）

| 方法 / 路径 | 动作 |
|---|---|
| `GET /api/console/local-accounts?q=&page=` | 列表：username、tenant、user_id、role、status、锁定状态、最近登录；**不返回哈希** |
| `POST /api/console/local-accounts` | 建号：`{username, password, role?, must_change_password?}`；409 重名、422 密码不合规 |
| `PATCH /api/console/local-accounts/{username}` | `{action: reset_password|unlock|force_change|disable|enable, password?}` |
| `POST /api/console/local-accounts/{username}/test` | 拿一组账号密码试一次，**不建会话不写身份**（照 `console_api.py:437` 平台试登录的先例） |

**停用与删除不另开端点**：复用现有 `/api/console/users` 的停用 / 删除接口，
在 store 层加钩子——命中 `identity_provider_id='local'` 时同步处理 `local_accounts` 行。
现有「确认回填 user_id」「不能删自己」「唯一超管」守卫、审计都自动生效。

> **2026-11-12 追加需求**：删除必须是「停用之后才能删」，且「删除用户 = 删除他的所有信息」。
> 现有 `console_delete_user()` 只删 3 张身份表、刻意保留历史会话与消息，与本需求冲突，
> 必须改造。完整设计见 §16。

### 5.3 状态码

| 码 | 场景 |
|---|---|
| 400 | 未启用 / `AUTH_MODE=off` / 租户不是 default |
| 401 | 账号或密码不正确（含不存在、停用） |
| 403 | `PASSWORD_CHANGE_REQUIRED`（`must_change_password=1`） |
| 409 | 用户名已存在 |
| 422 | 请求体非法、用户名格式、密码长度 |
| 429 | IP 限流 / 账号锁定（带 `Retry-After`） |
| 503 | 身份仓储未就绪 |

> 权衡：账号锁定返回 429 会让攻击者区分「账号存在且已被锁」与「账号不存在」。
> 内网中台可接受；**若部署在公网，改成统一 401**，锁定只在服务端静默生效。

---

## 6. 登录流程

```python
async def local_login(store, username, password, *, client_ip, user_agent):
    name = normalize_username(username)                 # 不合格直接 401，不单列 422
    allowed, retry = await store.rate_limit(
        'auth:local:ip:' + sha256(client_ip.encode())[:16],
        settings.auth_rate_limit_per_minute)
    if not allowed:
        raise LocalAuthError('尝试过于频繁，请稍后再试', 429, retry=retry)

    row = await store.local_account_for_login(name)      # 联表取 app_users.status
    ok = hasher.verify(password, row['password_hash'] if row else DUMMY_HASH)
    if not row or not ok or row['status'] != 'active':
        if row: await store.local_account_note_failure(name, threshold=5, seconds=900)
        raise LocalAuthError('账号或密码不正确', 401)
    if float(row['locked_until'] or 0) > time.time():
        raise LocalAuthError('尝试过于频繁，请稍后再试', 429)
    if row['must_change_password']:
        raise LocalAuthError('请先修改密码', 403,
                             code='PASSWORD_CHANGE_REQUIRED')
    if hasher.needs_rehash(row['password_hash']):
        await store.local_account_set_password(name, hasher.hash(password))

    token = auth.new_session_token()
    session = await store.login_platform_identity(
        platform_id='local', provider='local', subject=name,
        identity_provider_id='local', tenant_id=row['tenant_id'],
        display_name=name, role='member',                  # 已存在身份时不会被写回
        session_id='as_' + secrets.token_hex(12),
        session_digest=auth.digest_of(token),
        session_expires_at=time.time() + settings.auth_session_ttl,
        user_agent=user_agent, remote_addr=client_ip)
    await store.local_account_note_success(name)
    return token, session
```

路由层只做三件事：`StrictBody` 校验 → 调 `local_login()` → `_set_session_cookie(response, token, settings.auth_session_ttl)`
+ `_ensure_private_workspace()`（与 `routes.py:733` 一致）。

**为什么注册不能调 `login_platform_identity`**：它会顺手插一条 `auth_sessions`，
注册接口不该发会话。新增 `create_local_account()` 在一个事务里用 advisory lock
（照 `pg_auth.py:97` 的写法）串行化，一次性写 `app_users` + `identities` + `local_accounts`，
返回 `user_id`。登录时再走 `login_platform_identity`，只会命中「身份已存在」分支。

---

## 7. 账号生命周期与「第一个账号」

| 阶段 | 做法 |
|---|---|
| Bootstrap | `scripts/local_account_seed.py --username test --password test [--role member]`：直接连库建号，不依赖 HTTP、不需要已有超管会话。也可 `LOCAL_AUTH_SEED_USERS=test:test:member`，**仅在 `local_accounts` 为空时**在 startup 执行一次（照 `platform_login.seed_from_env()` 的写法） |
| 建号 | 控制台超管 POST；或一个事务写三张表；密码立即哈希，明文不落盘 |
| 首次登录 | 若 `must_change_password=1` → 403，前端切「设置新密码」表单，用 change-password 端点改完再登 |
| 改密 / 重置 | 新哈希落库、`password_updated_at` 更新、`failed_count=0`、`locked_until=0`；`UPDATE auth_sessions SET revoked_at=... WHERE user_id=? AND revoked_at IS NULL`（新增 `revoke_user_sessions()`），当前会话也一起失效，回登录页重登 |
| 停用 / 删除 | 走现有控制台用户接口 + store 钩子；`app_users.status` 是唯一真相，`principal_from_session()` 自动拦 401 |
| 解锁 | 超管 PATCH `unlock`，或直接等 `locked_until` 过期 |

---

## 8. 安全清单

- [x] 密码只存 scrypt 哈希 + 随机盐；pepper 可选且不入库
- [x] 常量时间比较、账号不存在时 dummy verify
- [x] 统一失败文案防枚举；公网部署时把锁定 429 也收敛成 401
- [x] IP 限流（复用 `rate_limit`）+ 账号失败锁定双维度
- [x] 所有管理写操作 `require_all` + `require_trusted_origin`（cookie 鉴权必须防 CSRF）
- [x] 登录成功用 `new_session_token()` 生成新 token，不复用请求里带来的旧 cookie
- [x] 改密 / 重置 / 停用后撤销该用户全部会话
- [x] 审计 payload 只记 `type / operator_id / username / tenant_id / role`，绝不记密码或哈希
- [x] 生产环境强制 `LOCAL_AUTH_MIN_LENGTH >= 8`；`AUTH_COOKIE_SECURE=true` 时才允许开启本地登录（启动自检）
- [ ] 不做：邮件找回、MFA、自助注册默认开启、跨身份源账号自动合并

---

## 9. 前端改造

**`api/web/index.html`**（行号为当前版本，实现时会漂移）

| 位置 | 改动 |
|---|---|
| `:1879-1932` 登录弹窗 | 把「账号密码 / 短信」两个 form 切换改成 **3 个 tab**：平台账号、本地账号、短信验证码。新增 `localLoginForm`（username + password + 提交 + 错误区） |
| `:4936-4954` 状态拉取 | 增加 `GET /auth/local-login/status`；`auth.localLogin` 初值必须是 **`null`**（不是 `false`），区分「还没问出来」和「确实关闭」——`platformLogin` 当初就是这里踩过坑 |
| `:5252-5285` `submitLogin()` | 抽公共 `afterLogin(data)`（flash 提示、`nextTarget` / `isConsoleRole` 跳转），让三个提交函数共用，别复制第三份 |
| `:5297` 事件绑定 | 绑 `localLoginForm.submit`、tab 切换；403 `PASSWORD_CHANGE_REQUIRED` 时切到改密表单 |
| 默认选中 | 由 status 决定：本地开启且平台未开启时默认本地 tab；否则保持现状（平台账号优先），不改变存量用户习惯 |

**`api/web/admin.html`**

| 位置 | 改动 |
|---|---|
| `:250-254` tab 按钮 / `:324-357` `tab-login` | 在「登录系统」页内新增一个 panel：总开关、最小长度、种子说明、账号表格（username / 角色 / 状态 / 最近登录）、建号与重置密码抽屉、解锁按钮 |
| 现有 `api()` | 401 → showLogin、403 → refreshIdentity 已实现，无需改 |

---

## 10. 配置项（追加到 `.env.example`）

```dotenv
# ---------- 本地账号登录（第 4 种登录方式，默认关闭） ----------
# 打开后中台自己校验密码；不影响平台账号 / 票据 OIDC / 短信三条链路。
# LOCAL_AUTH_ENABLED=false
# 自助注册默认关闭；正式环境只允许超管在控制台建号。
# LOCAL_AUTH_ALLOW_SIGNUP=false
# 生产环境强制 >= 8；本机可设 4 以创建 test/test。
# LOCAL_AUTH_MIN_LENGTH=8
# 仅在 local_accounts 为空时执行一次，格式 user:pass[:role]，role 默认 member。
# LOCAL_AUTH_SEED_USERS=
# 可选 pepper，只从环境变量读，绝不入库；轮换需全量重置密码。
# LOCAL_AUTH_PEPPER=
# 连续失败锁定阈值与时长（秒）。
# LOCAL_AUTH_LOCK_THRESHOLD=5
# LOCAL_AUTH_LOCK_SECONDS=900
# 身份源与租户标识；一期租户只支持 default。
# LOCAL_AUTH_IDP=local
# LOCAL_AUTH_TENANT=default
```

`core/config.py` 用现有 `_env_flag` / `os.getenv` 风格加对应字段，
`Settings` 里加一条启动自检：生产 + 短密码 + 未开 Secure cookie 时直接抛错，不静默降级。

---

## 11. 改动清单（按 PR 切分，每个 PR 独立可回滚）

| PR | 新增 | 修改 |
|---|---|---|
| P0 迁移 | `store/pg_local_accounts.py`（mixin，照 `pg_platforms.py`）、`tests/test_local_auth_store.py` | `store/pg_schema.py`（SCHEMA）、`store/migrations.py`（v23 + LATEST）、`store/pg_store.py:108`（`PostgresStore` 继承列表加 `PgLocalAccounts`） |
| P1 后端 | `api/local_auth.py`、`tests/test_local_auth.py` | `core/config.py`、`api/routes.py`（4 个认证路由）、`.env.example` |
| P2 前端 | — | `api/web/index.html`（第 3 tab + status + afterLogin）、`api/web/admin.html`（登录系统 panel） |
| P3 管理端 | `scripts/local_account_seed.py`、`tests/test_console_local_accounts.py`、本文档 | `api/console_api.py`（管理端点 + 停用/删除钩子）、`store/pg_admin.py`（`revoke_user_sessions`） |

`api/auth.py`、`governance/policies.py`、`api/platform_login.py`、`store/pg_auth.py`、
`api/sms_login.py` **一行都不改**——这是「并列不替代」的验收线。

---

## 12. 测试计划

**单元（`tests/test_local_auth.py`）**

1. `hash_password` 两次结果不同；`verify` 对 / 错密码；`needs_rehash` 对旧参数为真。
2. NFKC + 小写归一化；非法用户名 / 超长密码 / 4 位密码在生产配置下被拒。
3. 账号不存在时 `verify` 仍被调用一次（用注入的 fake hasher 断言），失败文案完全一致。
4. 连续 5 次失败写 `locked_until`，锁定期内 429，过期后恢复，成功登录清零。
5. `create_local_account` 并发 10 次同用户名 → 只有 1 行 `local_accounts` + 1 行 `app_users`。
6. 停用账号登录 401；改密后旧密码 401、新密码 200、`auth_sessions` 全部 `revoked_at` 非空。
7. 审计 payload 不含 `password` / `hash` / `pepper` 键。

**HTTP 层（`tests/test_console_local_accounts.py`，照 `test_console_platforms.py` 的 TestClient + fake store）**

8. status 开关四种组合（off 模式 / 开关关 / 开 / 生产）；登录 200 带 `Set-Cookie`（HttpOnly、SameSite、Secure 按配置）。
9. 管理端点 401 未登录 / 403 operator / 200 platform_admin；缺 Origin 或跨源写操作被拒。
10. 重名 409、弱密码 422、试登录端点不建会话不写身份。

**端到端（手工 + 可脚本化）**

11. 现有库跑 `python -m scripts.migrate` → `require_current()` 通过、`verify_schema()` 不报缺列。
12. 全量测试基线不新增失败（当前 1403 条里 12 条为既有环境/配置问题）。
13. 浏览器：平台账号 `admin/admin`、短信、OIDC 三条老路径行为不变；本地账号登录 / 退出 / 改密 / 控制台停用后 401。

---

## 13. 灰度与回滚

1. **默认 `LOCAL_AUTH_ENABLED=false`**：代码合入后所有新端点 404，前端不出现第 3 个 tab，线上行为零变化。
2. 先合 P0 迁移并单独执行（只加空表，无副作用），再合 P1-P3。
3. 灰度：非生产打开开关 → 种子建一个测试号 → 走一遍第 15 节验收 → 观察 `auth_sessions` 里 `platform_id='local'` 的记录与控制台活跃度。
4. 回滚：设 `LOCAL_AUTH_ENABLED=false` 即关闭入口；**不要回滚迁移**（空表留着无害）。
   需要彻底清理时删 `local_accounts` 行 + `identities WHERE identity_provider_id='local'`，
   `app_users` 行保留（历史会话/任务还引用它），把 `status` 置 `disabled`。

---

## 14. 风险与开放问题

| 风险 | 说明 / 处置 |
|---|---|
| 密码责任转移到中台 | DB 备份泄露 = 哈希泄露；需明确备份加密、DB 账号最小权限、日志脱敏。这是本方案最大的代价，也是现有架构坚持「不存密码」的原因 |
| 同名账号不合并 | BeeFTV 的 `test` 与本地的 `test` 是两个 `identity_provider_id`，会落到**两个中台账号**。需要「控制台手工绑定两个身份到同一 user_id」的能力（后续 PR，本期不做） |
| 多租户 | 一期只允许 `default`，原因见 §3.2 的主键坑 |
| scrypt 内存 | `N=2**14` 约 16 MB/次，靠 IP 限流兜底；若登录 QPS 高需下调并发或换 argon2id（改 `LocalPasswordHasher` 一处） |
| 429 可枚举账号 | 内网可接受，公网需改成统一 401 |
| 无找回密码 | 只提供超管重置；引入邮件/短信找回会再拖一套令牌体系进来，本期明确不做 |

待定：是否允许本地账号被授予 `platform_admin`（建议允许，但种子脚本建超管必须显式 `--role platform_admin`，
并受现有「唯一超管」守卫约束）。

---

## 15. `test/test` 端到端验收剧本

```bash
cd "/Users/latiao/Desktop/demo demo/main_branch"

# 1) 迁移（新表，幂等）
python -m scripts.migrate

# 2) 开关 + 非生产放宽长度，写进 .env
#    LOCAL_AUTH_ENABLED=true
#    LOCAL_AUTH_MIN_LENGTH=4
#    AUTH_MODE=ticket            # 现状，保持不变

# 3) 重启中台（当前 8002），种子建号
python -m scripts.local_account_seed --username test --password test --role member

# 4) 状态与登录
curl -s http://127.0.0.1:8002/api/auth/local-login/status
# → {"enabled":true,"allow_signup":false,"min_length":4}
curl -s -i -c /tmp/local.cookies -H 'Content-Type: application/json' \
     -d '{"username":"test","password":"test"}' \
     http://127.0.0.1:8002/api/auth/local-login
# → 200 + Set-Cookie: agent_session=...; HttpOnly
# → {"user_id":"u_...","tenant_id":"default","role":"member","first_login":false,...}

curl -s -b /tmp/local.cookies http://127.0.0.1:8002/api/me     # → 200，user_id 同上
curl -s http://127.0.0.1:8002/api/auth/platform-login/status    # → beeftv 仍在，老入口没坏
```

浏览器打开 `http://127.0.0.1:8002`：登录弹窗出现「本地账号」tab，输 `test/test` 直接进；
控制台「用户」列表能看到该账号，身份源显示 `local`，「最近活跃」记为登录。

---

# 16. 管理员后端账号管理（2026-11-12 追加需求）

## 16.1 需求与冲突

浩南追加三条：

1. **停用之后才能删除**（两段式，防误删）；
2. **手动创建用户**，管理员自行设置账号密码；
3. **删除用户 = 删除他的所有信息**（不是「不能登录」而已）。

第 3 条与现有实现**直接冲突**：`store/pg_admin.py:563` 的 `console_delete_user()`
注释写得很明确——只删 `auth_sessions` / `identities` / `app_users` 三张表，
历史会话、消息、任务**刻意保留**作为审计证据。所以这不是「加个钩子」，
而是要把删除语义从「撤销身份」改成「彻底清除」（purge），必须重写该方法。
第 1 条也是新增约束：现有 `DELETE /api/console/users/{user_id}` 允许直接删 active 账号。

其余保护沿用现有实现：不能删自己、不能删最后一个超管、`confirm` 必须回填完整
`user_id`（`console_api.py:259`）、`require_all` + `require_trusted_origin`。

## 16.2 生命周期状态机

```
[不存在] --超管建号--> [active] --停用--> [disabled] --删除--> [彻底清除]
                          ^                  |
                          +----启用----------+
                          |                  +--（不可恢复；无备份、无撤销）
                   停用同时撤销全部登录会话
```

- 停用：`console_set_user_status(status='disabled')`，已有实现，撤销全部 `auth_sessions`。
- 删除：**前置校验 `app_users.status == 'disabled'`**，否则 409「请先停用账号」。
- 删除成功后再用同一用户名建号，视为全新账号（`user_id` 重新生成），不复用旧数据。

## 16.3 硬删除范围（按活库 `information_schema` 实测，2026-11-12）

### A. 身份与凭据（4 张）

| 表 | 列 |
|---|---|
| `app_users` | `user_id` |
| `identities` | `user_id` |
| `auth_sessions` | `user_id` |
| `local_accounts` | `user_id` / `username`（v23 新表） |

### B. 直接归属（按用户 ID 删除，17 张）

`user_workspaces(owner_user_id)`、`request_keys(owner_user_id)`、
`tool_policies(user_id)`、`tool_authorizations(user_id)`、
`tool_result_manifests(owner_user_id)`、`tasks_meta(owner_user_id)`、
`approvals(owner_user_id / requester_user_id / approver_user_id)`、
`external_tasks(owner_user_id)`、`session_turns(owner_user_id)`、
`scheduled_tasks(owner_user_id)`、`scheduled_task_runs(owner_user_id)`、
`experts(owner_user_id)`、`memories(created_by / subject)`、
`credential_bindings(subject_id WHERE subject_kind='user')`、
`sessions(owner_user_id)`、`runs(owner_user_id)`、`local_accounts`（见 A）

> `auth_platforms.created_by` 是**全局配置表的操作人**，不删；删了会让平台配置
> 变成「创建人不明」，且不属于某个用户的数据。

### C. 会话 / 任务 / run 的下游（按 ID 级联）

先收集该用户的 `session_id` → `task_id` → `run_id` → `turn_id`，再按依赖顺序删：

| 层级 | 表 |
|---|---|
| 叶子 | `messages`、`session_memory`、`session_summaries`、`session_turn_events`、`steps`、`actions`、`action_attempts`、`plans`、`run_decisions`、`run_inputs`、`run_snapshots`、`artifacts`、`provider_charges`、`events`、`tasks_meta` |
| 中间 | `runs`、`tasks`、`session_turns`、`scheduled_task_runs`、`approvals`、`external_tasks` |
| 根 | `sessions` |

### D. 文件系统（DB 事务之外）

- `workspaces/<tenant>/<user_id>/` 私有工作目录（整目录）；
- `artifacts/` 下属于该用户 run 的产物（按 `artifacts` 表记录的 path 逐个删）；
- `secrets/oauth/<tenant>__<user_id>__*.json` 连接器凭据。

**顺序**：DB 事务先提交，再删文件。文件删除不可回滚，失败只记告警、不回滚 DB；
删之前把路径清单写进审计事件，便于事后人工补齐。

### E. 明确保留（唯一例外）

一条 `console_user_purge` 审计事件写进 `events`，只含：操作人、被删用户 ID 的
**哈希前 16 位**（不存明文 `user_id`）、删除时间、各表删除行数、失败的文件路径。
不含昵称、邮箱、subject、消息内容。理由：完全没有删除记录的系统无法追责；
但按「删除所有信息」的要求，记录里不保留可反查到人的字段。

## 16.4 删除事务

```
advisory_xact_lock('console-role') → SELECT app_users FOR UPDATE
  → 校验：存在 / 不是自己 / 不是最后一个超管（不再要求先停用，见 §18）
  → purge_preview()：统计各表行数与文件清单（也给前端确认框用）
  → 收集 session_ids / task_ids / run_ids / turn_ids
  → 按 C 的「叶子 → 中间 → 根」顺序 DELETE
  → 按 B 的归属列 DELETE（每条语句都带 user_id 条件，禁止全表删）
  → 按 A 删身份与凭据
  → 写 console_user_purge 审计（哈希引用 + 计数）
  → COMMIT
  → 事务外删 D 的文件；失败写 warning 日志 + 一条补充审计
```

- 全程**一个事务**，任何一句失败整体回滚，不留半删状态。
- 所有 DELETE 必须带 `WHERE ...=%s`，禁止 `TRUNCATE`、禁止按 `tenant_id` 删。
- 大用户（几万条消息）会形成长事务；一期接受，后续可改成分批 + 幂等重入。
- 重复删除返回 404「用户不存在」。

## 16.5 接口变更

| 方法 / 路径 | 变更 |
|---|---|
| `POST /api/console/users` | **新增**：手动建号 `{username, password, display_name?, role?, must_change_password?}`；内部调 `create_local_account()`；409 重名、422 密码不合规。要求 `LOCAL_AUTH_ENABLED=true` |
| `GET /api/console/users/{user_id}` | 响应加 `purge_preview`：`{sessions, messages, tasks, runs, files, total}`，供删除确认框显示 |
| `PATCH /api/console/users/{user_id}/status` | 不变（停用 / 启用） |
| `POST /api/console/users/{user_id}/password` | **新增 / §18 扩展**：`{password, username?}`。超管可对**任意**用户设置密码：已有本地凭据则改哈希；外部身份源则调 `local_account_bind_to_user()` 给同一个 `user_id` 补一条本地登录凭据（角色取该用户最高角色，不新建账号）。撤销该用户全部会话 |
| `POST /api/console/users/{user_id}/unlock` | **新增**：清 `failed_count` / `locked_until`；无本地凭据时返回 no-op 成功而非 404 |
| `DELETE /api/console/users/{user_id}` | **改造**：payload 加 `purge: true` 显式语义；不限制身份源与 `status`；仍禁止删自己 / 最后一个超管；返回各表删除行数 `purged{}` |

`DeleteRequest` 的 `confirm` 仅为兼容老客户端保留、**内容不再校验**；`reason` 可选；
`purge: bool` 传 false / 不传时拒 400，避免老客户端把「撤销身份」当成「清空数据」。

## 16.6 前端变更（`api/web/admin.html`）

| 位置 | 改动 |
|---|---|
| 用户 tab 工具栏（`:309-320`） | 加「创建用户」主按钮，打开建号抽屉：用户名、密码、确认密码、显示名、角色、首次登录强制改密 |
| 用户列表（`:630-700`） | 超管视角下**每一行**都有「重置密码」「删除」；有本地凭据的行额外显示「解锁」。是否本地账号由 `/api/console/local-accounts` 构建的 `state.localByUser` 判断，不再靠 `platforms` 猜 |
| 重置密码抽屉（§18 新增） | 任意用户可设置新密码；外部身份源用户可自定「登录账号」，并明确提示绑定的是同一个中台账号、原有登录方式不受影响 |
| 详情抽屉危险区 | 删除按钮对所有状态可用；显示 `purge_preview` 的预计清除条数 |
| 删除确认（§18 简化） | 一次 `confirm()`：写明删谁、会清掉什么、预计 N 条、不可恢复。**不再要求手打 user_id**；行内删除按钮会先取详情拿条数再确认 |

## 16.7 风险与需要你拍板的点

| 风险 | 说明 |
|---|---|
| 不可逆、无备份 | 需求是「删除所有信息」，因此**不做数据导出备份**。如果以后要「可恢复」，得改成「停用 + 归档」而不是 purge |
| 审计里的哈希引用 | 保留 `sha256(user_id)[:16]` 已是不可反查，但严格意义上仍「留了一条记录」。若要求零留痕，可改为只记计数不记引用，代价是无法追责误删 |
| 文件删除在事务外 | 崩溃时可能出现「DB 已清空、文件还在」，需要靠审计里的路径清单人工清理 |
| 共享数据 | `messages` / `events` 里可能有其他人 @ 到该用户的内容，按会话归属一并删；不做跨用户引用改写 |
| 长事务 | 大账号删除期间会持有 `FOR UPDATE` 锁，建议放在低峰期操作 |

**待确认**：删除时是否需要「先导出一份脱敏统计快照」（不含消息正文）；
以及 `purge` 是否只允许 `platform_admin`（建议是，`operator` 不应有跨租户清数据的权力）。

实现时按默认值落地：**不做数据导出备份**、**只允许 `platform_admin`**（`require_all`）。

---

# 17. 实现记录（2026-11-12）

## 17.1 落地的文件

| 新增 | 作用 |
|---|---|
| `store/pg_local_accounts.py` | 建号事务、凭据读写、失败锁定、列表、改密撤销会话 |
| `store/pg_user_purge.py` | `purge_preview()` 与 `purge_user()`：级联清空 + 事务外删文件 |
| `api/local_auth.py` | scrypt 哈希、dummy verify、限流/锁定、`sign_in()`、`change_password()`、`seed_from_env()` |
| `scripts/local_account_seed.py` | 不依赖超管会话的 CLI 建号 / 列表 |
| `tests/test_local_auth.py` | 密码与登录编排的单元测试 |

| 修改 | 作用 |
|---|---|
| `store/pg_schema.py`、`store/migrations.py` | `v23_local_accounts`：SCHEMA 快照 + UPGRADES + `LATEST` 三处齐全 |
| `store/pg_store.py` | `PostgresStore` 挂上 `PgLocalAccounts` / `PgUserPurge` |
| `store/pg_admin.py` | `console_delete_user()` 改为委托 purge |
| `core/config.py`、`.env.example`、`.env` | 本地账号配置项；本机设 `LOCAL_AUTH_ENABLED=true`、`LOCAL_AUTH_MIN_LENGTH=4` |
| `api/routes.py` | 4 个认证端点 + startup 自检与种子；新增 `local_auth_service` 导入 |
| `api/console_api.py` | 建号 / 重置密码 / 解锁 / 本地账号列表；`DeleteRequest` 加 `purge`；详情加 `purge_preview`；§18 起重置密码覆盖任意身份源 |
| `api/web/index.html` | 登录弹窗第 3 个 tab + 强制改密表单 + 抽公共 `afterLogin()` |
| `api/web/admin.html` | 「创建用户」抽屉、任意用户重置密码 / 解锁、一次确认删除 |
| `tests/test_console_access.py` | 补 `purge=true`、显式确认、以及「不再需要打字确认」用例 |

`api/auth.py`、`policies.py`、`platform_login.py`、`pg_auth.py`、`sms_login.py` **一行未改**。

## 17.2 第一轮端到端验证（8012 临时实例，真实 PostgreSQL + 真实 BeeFTV）

> 下表记录的是第一轮行为。删除前置与确认方式已在 §18 调整，以 §18 为准。

| 项 | 结果 |
|---|---|
| 迁移 | `Applied: v23_local_accounts`，`Schema verified: 45 tables / 530 columns` |
| `test/test` 登录 | 200 + `Set-Cookie: agent_session`（HttpOnly / SameSite=lax），`/api/me` 返回 `platform_id=local` |
| 错误密码 / 不存在的账号 | 均 401 且文案一致 |
| 超管手动建号 | `POST /api/console/users` → 200，列表不返回 `password_hash` |
| 重置密码 | 旧密码 401 / 新密码 200，全部会话被撤销 |
| active 直接删除 | 409「请先停用账号，再执行删除」（**§18 起改为允许**） |
| 不带 `purge` 删除 | 400，要求显式确认 |
| 停用后 purge | 200：删 6 行（身份/凭据/工作区/2 个会话）+ 1 个文件目录，审计留 `user_ref=sha256[:16]` |
| 删完后 `test/test` | 仍可登录 |
| 老入口 | `/api/auth/platform-login/status` 仍返回 beeftv |
| 测试 | 相关 81 passed；全量 1040 passed / 3 failed（3 条均为既有环境类失败：admin_users 来源校验、user_isolation sandbox） |

## 17.3 使用方式

```bash
cd "/Users/latiao/Desktop/demo demo/main_branch"
python -m scripts.migrate                                  # 只需一次
python -m scripts.local_account_seed --username test --password test
# .env: LOCAL_AUTH_ENABLED=true、LOCAL_AUTH_MIN_LENGTH=4（本机）
# 重启中台后打开 /，登录框选「本地账号」
```

## 17.4 与设计稿的差异 / 已知限制

- **`LOCAL_AUTH_MIN_LENGTH` 的生产下限**落在 `local_auth.startup_check()` 与
  `validate_password()` 两处；`enabled()` 也做了同样的判定，配置不对时入口直接不显示。
- **改密端点**不需要已有会话（凭旧密码），因此也带 IP 限流；成功后清当前 cookie。
- **删除审计**只留 `user_ref` 哈希，不存明文 `user_id`；控制台「针对此人的管理动作」
  依赖 payload 里的 `user_id` 筛选，所以 purge 那条不会出现在该用户的详情页（人已删）。
- **文件删除**在 DB 提交后执行，路径限定在 `workspaces/`、`artifacts/`、`secrets/oauth/`
  之下，逐路径校验，不做通配符批量删。
- **主服务 8002 已于 2026-11-12 02:10 重启**，加载了全部新代码与新 `.env`；
  此前「新前端 HTML + 旧 Python 后端」的错配会让新按钮点出 404 `Not Found`，重启后消失。
- 多租户仍只支持 `default`（§3.2 的主键坑）；同名跨平台账号不自动合并。
- 给外部身份源用户设置密码时，新身份行的 `tenant_id` 固定为
  `LOCAL_AUTH_TENANT_ID`（default），否则 `login_platform_identity()` 会另建账号。

## 18. 第二轮调整（2026-11-12）：删繁就简 + 超管全覆盖

反馈来自实际使用截图：① 删除要手打完整 `user_id` 太麻烦；② 「重置密码 / 解锁」
只出现在 `test`（local）那一行，超管希望对**任何用户**都有重置密码与删除的权利。

### 18.1 删除：去掉两道多余门槛

| 之前 | 现在 |
|---|---|
| 前端 `prompt()` 要求输入完整 `user_id`，后端 `DeleteRequest.confirm` 逐字比对 | 前端一次 `confirm()`，写明昵称 + `purge_preview` 条数 + 「不可恢复」；后端 `confirm` 仅兼容保留、不再校验 |
| `purge_user()` 强制 `status='disabled'`，active 账号删除返回 409 | 任何状态均可直接删；停用仍保留为**可逆**的独立操作 |
| `purge: true` 显式语义、`require_all` 超管限定、可信 Origin、不能删自己 / 最后一个超管 | 全部保留 |

理由：真正不可逆的是删除本身，防误删靠「看清楚删谁和删多少」比「抄一串 ID」更有效；
「先停用再删」只是把一个可逆动作变成删除的必经步骤，与「超管对任何用户都有删除权」冲突。
**两条硬保护不能去掉**：删自己会立刻失效当前会话，删最后一个超管会让控制台永久锁死。

### 18.2 重置密码：任意身份源，绑定同一个中台账号

中台**不保管** BeeFTV / 短信 / mock-idp 的密码，无法改上游。因此「重置」的语义是：
给该 `user_id` 补一条由中台自己校验的本地登录凭据
（`local_accounts` + `identities(provider=local)`），**不新建 `app_users`**。

- `PgLocalAccounts.local_account_bind_to_user()` 在一个事务里完成：advisory lock →
  校验用户存在 / 登录账号未被占用 → 取该用户已有身份的**最高角色**（避免换入口登录后
  权限降级）→ 插身份行与凭据行 → 撤销该用户全部会话 → 写
  `console_local_account_bind` 审计。
- 登录侧 `/api/auth/local-login` 走原有 `login_platform_identity()`，命中已有身份行，
  因此返回的 `user_id` 就是原账号，角色、租户、历史会话与数据全部保留；
  原有的 beeftv / sms / OIDC 登录方式**不受影响**。
- 登录账号默认取 `user_id`（`u_xxx` 一定合法且全局唯一），超管可在表单里改成好记的名字；
  格式非法返回 422、重名返回 409。已有本地账号的行只改哈希，登录名不可改。
- `/users/{id}/unlock` 对没有本地凭据的用户改为 no-op 成功（原来 404），避免前端误点报错。

### 18.3 前端

用户列表在超管视角下**每一行**都有「详情 / 停用 / 重置密码 / 删除」，有本地凭据的行
额外显示「解锁」；是否本地账号由 `/api/console/local-accounts` 构建 `state.localByUser`
判断。详情抽屉新增「本地登录」区（显示登录账号、重置密码、解锁）。删除既可从行内按钮
（先取详情拿条数再确认），也可从详情抽屉发起。

### 18.4 本轮改动文件

`api/console_api.py`（`DeleteRequest` / 重置密码 / 解锁）、
`api/local_auth.py`（`normalize_managed_username()`）、
`store/pg_local_accounts.py`（`local_account_bind_to_user()`）、
`store/pg_user_purge.py` 与 `store/pg_admin.py`（去掉停用前置）、
`api/web/admin.html`（操作列 / 重置密码抽屉 / 一次确认删除）、
`tests/test_local_auth.py` 与 `tests/test_console_access.py`。

### 18.5 8002 实测（真实库 + 真实 BeeFTV）

| 项 | 结果 |
|---|---|
| 新端点 | `/api/auth/local-login/status` 200；`/api/auth/platform-login/status` 仍返回 beeftv |
| 给 sms 用户 `u_54a1baa900894fd2` 设密码 | 200 `created=true, username=probe_bind`；身份表多一行 `local / probe_bind / default / member`，原 `sms` 行仍在 |
| 用 `probe_bind` 从「本地账号」入口登录 | 200，`/api/me` 的 `user_id` 仍是 `u_54a1baa900894fd2`、显示名仍是 `13228944571` —— 同一个账号 ✅ |
| active 用户不带 `confirm` 直接删 | 200，清 3 行（app_users / identities / local_accounts），库里归零 |
| 删自己 | 409（硬保护仍在） |
| 验证残留 | `probe_bind` 的凭据、身份行、会话与对应审计均已清理，真实 sms 用户恢复原状 |
| 测试 | 本轮相关 35 passed / 6 subtests；全量 1039 passed / 10 failed，10 条均落在工作区并行改动的 addons / OAuth / MCP / 既有环境类用例，与本轮改动文件无关 |
