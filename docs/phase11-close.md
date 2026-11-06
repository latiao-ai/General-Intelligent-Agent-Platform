# 第十一阶段：正式会话入口与聊天、业务任务协作（部分收口）

范围：保留现有任务接口和默认业务引擎；写能力未补验前保持关闭。

## 一、能力开放状态（逐项，不合并）

| 能力 | 状态 | 说明 |
|---|---|---|
| 会话聊天 | ✅ 已开放 | 独立聊天 turn，不建业务任务、不更新 intent_version |
| 任务查询 | ✅ 已开放 | 只读查询路由 + 只读工具（缺参数追问） |
| 创建业务任务 | ⛔ 保持关闭 | `SESSION_TASK_CREATE_ENABLED=false`，需补齐写链路验收 |
| 修改/补充任务 | ⛔ 保持关闭 | `SESSION_TASK_CONTROL_ENABLED=false` |
| 暂停/恢复/取消 | ⛔ 保持关闭 | 同上，需经正式链路验证后再开放 |

> 第 1 点要求的「把上表缺口列为已有问题」：**业务写控制能力（创建/修改/暂停/取消）
> 是已有问题，未在本阶段补齐验证，保持关闭**。不是本阶段新增成果。

## 二、本阶段完成内容

### 1. 能力开关（任务 23）
- `core/config.py` 新增 `session_chat_enabled` / `session_query_enabled` /
  `session_task_create_enabled`（默认 false）/ `session_task_control_enabled`
  （默认 false）/ `session_chat_concurrency`。聊天、查询默认开；创建/控制默认关。

### 2. 持久化会话消息入口（任务 24）
- `routes.py` 新增 `POST /api/sessions/{session_id}/messages`：接纳后返回 202
  （turn_id/message_id/status/kind/target），不等待模型完成。
- 同一 request_id 同载荷返回原 turn（replayed）；不同载荷 409。
- 用户消息 + 待执行 turn + 接纳事件同事务提交（`store.create_turn`）。

### 3. 独立聊天 turn（任务 25）
- 迁移 `v8_session_turns` 新增 `session_turns` 表：请求指纹、顺序、状态、引擎
  身份、租约、执行代次、关联业务 run。
- `pg_auth` 新增 `create_turn` / `turn` / `turns_of` / `claim_turn` /
  `settle_turn` / `finish_turn_message`。

### 4. 会话 Pi 执行入口（任务 26）
- `api/hub_messages.py`：`route_message`（纯逻辑）+ `HubSessionMessageMixin`
  （`accept_session_message` / `run_chat_turn` / `_chat_answer`）。
- `run_chat_turn` 按 turn_seq 顺序领取（有界，不并发修改同一 Pi session），
  答复生成可注入（`chat_responder`），结算后写 assistant 消息。

### 5. 消息路由与歧义处理（任务 28）
- `route_message`：闲聊→chat（不更新 intent_version）；查询词→query；控制动词+
  单活动任务→control；控制动词+多活动任务→ambiguous（追问，不自动投递）。

### 6. 业务创建引擎归属（任务 29）
- `create_goal` 显式写入 `runtime_kind`/`runtime_version`/`runtime_state_schema`
  （缺省 `default_kind()`），支持 `source_turn_id` 关联（避免重复保存用户消息）。

### 7. 会话级 timeline（任务 31）
- `session_timeline` 新增 `chat_turns` 与 `active_runs[]`（含等待/审批/暂停/
  待核实任务），前端按 turn 展示聊天、按 run 展示任务卡。

### 8. 前端改造（任务 32）
- 普通输入统一走 messages 入口；移除「goalOpen 自动投递 inputs」和「409 后自动
  改投 inputs」逻辑。明确目标的补充/控制保留独立入口。

## 三、验证结果（分层，不合并）

| 证据类别 | 套件 | 通过 |
|---|---|---|
| 路由纯逻辑（无 PG） | `test_phase11_messages.py::RouteMessageTests` | 7 |
| PG 仓储（turn 幂等/归属/顺序） | `test_phase11_messages.py::TurnStorageTests` | 4 |
| 聊天 turn 闭环（真实 PG） | `test_phase11_messages.py::ChatTurnRoundtripTests` | 3 |
| 回归（网关/Pi/输入/平台写） | 相关套件 | 138 |

## 四、跳过项与剩余限制（如实说明）

- **业务写控制能力未验收**：创建/修改/暂停/取消保持关闭，未实施宿主控制工具的
  完整链路（任务 27）、Worker 聊天/业务额度隔离（任务 30）。这是第十阶段已指出的
  「完整 Pi 写闭环未串联」缺口的延续，**不以聊天通过代表业务写验收完成**。
- **正式 HTTP 端到端验收**：仅验证到「接纳→领取→答复→结算」这一层（真实 PG），
  未覆盖「后台任务期间聊天、SSE 断线回放、跨主体拒绝」等 HTTP 全链路场景。
- **真实供应商联调未执行**（延续第十阶段）。

## 五、改动文件

新增：`src/webpage_agent/api/hub_messages.py`、`tests/test_phase11_messages.py`、
本收口文档。
修改：`pi-runtime` 无；`src/webpage_agent/store/{migrations,pg_schema,pg_auth,pg_tasks,pg_store}.py`、
`src/webpage_agent/core/config.py`、`src/webpage_agent/api/{routes,api_hub,hub_session}.py`、
`src/webpage_agent/api/web/index.html`、`tests/exec_env.py`。
