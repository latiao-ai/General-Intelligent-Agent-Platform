# 积分（余额 / 消耗 / 管理台）

> 一句话：**余额不是存在表里的一个数，而是「进账 − 已用」算出来的**。
> 进账落 `credit_grants`，已用从 `provider_charges` 实时聚合。两张账各自只有
> 一个写入方，不存在"流水和余额对不上时不知道信谁"的问题。

## 1. 口径

| 概念 | 来源 | 说明 |
|---|---|---|
| 积分 | 展示单位 | 1 积分 = ¥0.01 = 10 000 micros |
| 进账 | `credit_grants` | 购买、赠送、人工调整（append-only，负数表示扣减/冲正） |
| 已用 | `provider_charges.charged_micros` | 每次模型调用的实付金额，按用户归集 |
| 余额 | 进账合计 − 已用合计 | 每次读取时现算，不落库 |

归集链：`provider_charges.run_id → runs.task_id → tasks.session_id →
sessions.owner_user_id`。链上断一环（存量库里确实有）就归不到人，那笔消耗不计入
任何人的"使用情况"——宁可把余额算高，也不要把别人的花费算到某个人头上。

## 2. 表

```sql
credit_grants(grant_id, tenant_id, user_id, amount_micros, kind, note, operator_id, created_at)
credit_orders(order_id, tenant_id, user_id, package_id, points, price_cents, status, created_at, paid_at)
```

- 迁移 `v25_credits` 建表；`SCHEMA` 快照同步（新库建表脚本要保持完整）。
- `credit_orders` 目前只有 `pending`：支付通道没接，下单只登记不到账。

## 3. 接口

| 方法 | 路径 | 谁 | 说明 |
|---|---|---|---|
| GET | `/api/credits/balance` | 本人 | 余额 / 进账 / 已用 / 最近进账时间；未登录回 0 且 `authenticated:false` |
| GET | `/api/credits/events?limit=` | 本人 | 变动时间线：进账逐条，消耗按天并成一条 |
| POST | `/api/credits/orders` | 本人 | 落 pending 订单，回 **503**（`detail.message` 说明未接入支付） |
| GET | `/api/admin/credits?days=` | operator | 本租户每个用户：余额、窗口内消耗、调用次数、token、每日趋势、最近变动 |
| POST | `/api/admin/credits/{user_id}/grant` | operator | 人工充值/冲正，写一条流水；调整后不允许为负 |
| GET | `/api/console/credits?days=&tenant=` | 控制台 | 平台超管跨租户；租户运维只看本企业 |

写接口都要过 `require_trusted_origin()`（同 `console_api` 的规则）。

## 4. 前端

- 侧栏账号菜单：`#creditValue`。拿不到余额显示「—」，**不显示占位数字**——
  以前的 4,056.38 是假值，容易被当成真余额读。
- 管理台（内嵌 `#adminPanel`）：`账号 / 积分` 两个页签。积分行点开是每日柱状
  图 + 进账合计 + 累计消耗 + 窗口内 token + 「记一笔」人工充值。
- 跨租户控制台 `admin.html`：`积分` 页签只读，列余额 / 近 N 天消耗 / 调用 /
  token / 每日柱 / 最近变动。

## 5. 自测

`credit_check` 临时库跑过完整链路（建库 → v25 迁移 → 造 sessions/tasks/runs/
provider_charges → 充值 → 读余额/流水/概览 → 负数拒绝 → 下单不改余额），并在
`agent_platform_main` 的副本上验证了**存量库升级**路径（v1~v25 全部走完后
`verify_schema` 通过）。无新增单测文件；上述脚本是手工回归用的临时脚本。
