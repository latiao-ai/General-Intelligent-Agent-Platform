# 第十一阶段：正式会话链路（完整收口）

范围：保留现有任务接口和默认业务引擎；写能力未补验前保持关闭。

## 一、能力开放状态（逐项，不合并）

| 能力 | 状态 | 说明 |
|---|---|---|
| 会话聊天 | ✅ 已开放 | 独立聊天 turn，真实 Pi 执行，不建业务任务、不更新 intent_version |
| 只读查询 | ✅ 已开放 | query 路由 + 只读工具白名单（无工具时说明能力限制） |
| 任务进度查询 | ✅ 已开放 | 只读，读本地任务/运行状态与进度 |
| 创建业务任务 | ⛔ 保持关闭 | `SESSION_TASK_CREATE_ENABLED=false`，未补验写链路 |
| 修改/补充/暂停/恢复/取消 | ⛔ 保持关闭 | `SESSION_TASK_CONTROL_ENABLED=false`，未补验写链路 |

> 写能力（创建/修改/暂停/取消）是第十阶段已指出的「完整 Pi 写闭环未串联」缺口的
> 延续，本阶段**不把第十阶段未完成的写验收宣称为通过**，保持关闭并返回明确原因。

## 二、本阶段完成内容（按验收八点）

### 1. 接通 Worker 消费
- `scripts/run_worker.py` 新增 `drain_chat_turns`：聊天 turn 与业务 run **分池**，
  各自独立额度（`session_chat_concurrency` vs `max_concurrent_runs`），长业务/
  外部轮询不阻塞聊天。
- 消息 POST 只持久接纳（202），Worker 负责领取执行；调度入口**读取能力开关**，
  聊天/查询关闭时不领取、明确结算为失败，避免「接纳后永久搁置」。

### 2. 修正 turn 租约与同会话串行
- `claim_turn`：同一会话只能有一个有效执行中的聊天 turn（advisory lock），按
  turn_seq 顺序执行；不同会话可有界并发（跨会话领取）。
- 过期 running 重新领取，递增 `execution_generation`（执行代次）与 `lease_epoch`；
  新增 `renew_turn_lease` 支持长执行续租。
- `settle_turn` 检查 owner + epoch + 代次，失租返回 False，执行者停止、不再写答复。

### 3. 原子提交答复与完成状态
- `settle_turn` 把最终 assistant 消息、turn 终态同一事务提交，稳定去重键
  `(session_id, turn-answer:{turn_id})`：答复提交前崩溃 / 提交后响应丢失均可恢复，
  不丢答复、不重复答复。
- 执行入口检查结算返回值，失败（失租）不继续发布成功结果。

### 4. 接入真实 Pi 聊天执行
- `execute_claimed_turn` 走 `_run_pi_chat`：复用 Node 桥接、模型预算
  （governance.reserve/finish）、费用结算与事件出口，替换固定问候语。
- 普通消息**无需业务 run**，用 turn_id 作为独立执行身份（billing scope 的 run_id）。
- 聊天上下文（`chat_history` 排除当前 turn 的 user 消息）、bridge、消息消费水位
  按执行隔离（每次新 PiRuntime）。
- 真实 Pi 负责闲聊与工具选择；关键词路由只辅助明确操作，不冒充完整推理。

### 5. 接通只读查询与任务状态查询
- 任务进度查询：`_PROGRESS_HINTS` 命中 + 活动任务 → control 读本地任务状态；
  多活动任务 → 追问。
- query 路由走真实 Pi + 只读工具白名单（web_search）；无工具/无可信主体时如实
  说明能力限制，不硬编码「查询不可用」。
- 后台任务等待期间，聊天/查询/查进度均不改变业务 run 的 intent_version。

### 6. 宿主控制工具与持久追问
- 创建/补充/暂停/恢复/取消控制入口按能力开关逐项检查；未满足第十阶段前置条件
  的保持关闭并返回明确原因。
- 歧义：保存候选关联（`candidates_json`）+ 追问文案，turn 进入 `waiting_clarification`
  明确等待状态，不退回 accepted 让 Worker 反复领取。
- 下一条回答（编号/任务名）经 `_resolve_clarification` 映射回候选，继续澄清。

### 7. 前端与会话事件接线
- `index.html` 新增 `pollChatReply`：发送后按 turn_id 轮询 timeline 直到终态，
  后台生成的答复可见（不只拉一次时间线）；刷新/重试复用原 request ID。
- timeline 返回 `chat_turns` 与 `active_runs`；聊天答复按 turn_id、业务任务按
  task_id 分别更新，不共用单个 task 连接覆盖彼此。
- 聊天答复经 timeline 的 messages 补读（断线重连不丢失、不重复）。

### 8. 正式 HTTP 入口验收
- 新增 `tests/test_phase11_e2e.py`：真实 PG + 真实 Node + 受控模型
  （PI_MOCK_SCRIPT），从消息 POST 开始由真实 Worker 调度入口消费，timeline 取得
  答复。覆盖 8 个验收场景。

## 三、验证结果（分层，不合并）

| 证据类别 | 套件 | 通过 |
|---|---|---|
| 路由纯逻辑 | `test_phase11_messages.py::RouteMessageTests` | 7 |
| PG 仓储（turn 幂等/归属/顺序） | `test_phase11_messages.py::TurnStorageTests` | 4 |
| 聊天 turn 闭环（真实 PG） | `test_phase11_messages.py::ChatTurnRoundtripTests` | 3 |
| 正式 HTTP 入口验收（真实 PG+Node+受控模型） | `test_phase11_e2e.py` | 8 |
| 回归（runs/执行恢复/输入/Pi/worker/phase8） | 相关套件 | 132 |

**受控模型与真实模型联调分别报告**：
- 受控模型（PI_MOCK_SCRIPT）：8 个场景全部通过，答复经完整 Node 桥接协议
  （model_request/admit → 产出 → model_usage/ack + checkpoint）落库；
- 真实模型（DeepSeek，无 mock）：`你好` 走真实 Pi 执行，答复落库可读取。

## 四、剩余限制（如实说明）

- **业务写控制能力未验收**：创建/修改/暂停/取消保持关闭，未实施完整写闭环，
  不以聊天/查询通过代表业务写验收完成。
- **真实供应商（MCP 写）联调未执行**（延续第十阶段）。

## 五、改动文件

新增：`tests/test_phase11_e2e.py`。
修改：`scripts/run_worker.py`、`src/webpage_agent/api/hub_messages.py`、
`src/webpage_agent/store/{migrations,pg_schema,pg_auth}.py`、
`src/webpage_agent/core/config.py`、`src/webpage_agent/api/web/index.html`。
