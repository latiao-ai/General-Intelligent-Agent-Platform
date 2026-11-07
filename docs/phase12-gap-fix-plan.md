# 第十二阶段缺口修复方案（评审建议落地）

> 依据上轮评审意见，聚焦两大缺口 + SIGILL 归因 + 浏览器实测。
> 本文档只做**定位 + 方案**，未改动代码；改动经确认后执行。

## 一、查询身份模型（评审点 1）

### 现状定位（根因链）

只读查询（query turn）调用只读工具（`local:web:search`）被拒绝，根因是**两层缺失**：

1. **无账本**：`api/hub_messages.py::_run_pi_chat` 只绑定
   `governance.bind`（billing scope）和 `execution_control.bind`（ExecutionContext），
   **没有绑定 `ActionLedger`**。于是 `execution_gateway.governed_call` 里
   `book = current_ledger()` 为 None → 直接返回 `kind='no_run'`（E_NO_LEDGER）。
2. **无 run 行**：即使绑了账本，`store.admit_action` → `_owned` 要求 `runs` 表存在
   `status IN ('running','queued')` 的行；query turn 用 `turn_id` 作 run_id，但接纳时
   只落 `session_turns`，**没有对应 `runs` 行** → 返回 `{'status': 'no_run'}`。

> 第十一阶段 A 组第 5 点已做了一半：`tool_context` 能在无 run 时回退到
> `session_turns` 的 tenant_id/owner_user_id 构造可信主体（ctx）。但 ctx 有了，
> 账本和 run 行还没有，所以工具调用仍被 `governed_call` 的 no_run 挡住。

### 修复方案（评审建议的「明确创建内部查询执行记录」路线）

在 `_run_pi_chat` 的 query 分支：

1. **建内部执行记录**：为 `turn_id` 创建一条 `runs` 行（`status='running'`，
   `runtime_kind='langgraph'` 或专有标记 `query`，`owner_user_id`/`tenant_id` 取自
   `claimed`），并领取/写入租约（复用 turn 已有的 `lease_owner`/`lease_epoch`）。
   —— 这就是「内部查询执行记录」，让授权/预算/准入有可落账的执行主体。
2. **绑定 ActionLedger**：`ActionLedger(store, turn_id, plan_version=1, context=exec_ctx)`，
   用 `ledger_module.bind` 挂上，使 `governed_call` 走完整治理链。
3. **执行后收尾**：turn 结算时把内部 run 行置为终态（succeeded/failed），不留
   running 僵尸行（避免污染 `claim_run` 的业务领取池）。

**保证的拒绝路径**（无需新代码，补测试证明）：
- **跨主体拒绝**：`authorize_call` 依据 ctx（来自 turn 的 tenant/user）拒绝他人主体；
- **超预算拒绝**：`consume_budget` 走 `runs.consumed_json` + provider scope，超限抛
  `BudgetExceeded` → 结算失败；
- **撤权拒绝**：`_owned` 校验 lease_owner/lease_epoch，失租抛 `LeaseLost`。

### 需改动文件（预估）

| 文件 | 改动 |
|---|---|
| `src/webpage_agent/api/hub_messages.py` | `_run_pi_chat` query 分支：建内部 run 行 + 绑账本 + 收尾 |
| `src/webpage_agent/store/pg_runs.py` | 视需要增加「建内部查询 run」的专用方法（复用 `create_run` + `claim_run` 或新轻量方法） |
| 测试 | 新增 query 工具调用成功 + 跨主体/超预算/撤权拒绝用例 |

**风险**：核心治理路径（账本绑定 + runs 行生命周期）。必须保证：
- 内部 run 行不进入 Worker 业务领取池（用专有 status/runtime_kind 区分）；
- turn 异常时内部 run 行正确收尾，不泄漏。

## 二、Pi 自主修改闭环（评审点 2）

### 目标

补一条 **7 步完整闭环**，全程用受控模型（`PI_MOCK_SCRIPT`）+ 受控 MCP
（`tests/mcp_platform_server.py`，stdio transport + JSON 持久化），**不手动调用
写入/核验函数**替代任何一步：

```
正式 HTTP 输入自然语言目标
→ Worker 启动 Pi
→ Pi 读取对象并提出修改
→ 经过授权/审批和账本执行
→ 宿主独立回读并核验
→ Pi 消费真实结果并答复
→ 宿主依据证据成功收尾
```

### 复用基础设施（已存在）

- 受控模型脚本：`PI_MOCK_SCRIPT`（确定性 Node Pi 产出，见
  `test_phase11_process_e2e.py` 的 `_mock_answer`）。
- 受控 MCP 平台：`tests/mcp_platform_server.py`（`seed_state`/`read_state`，
  `update_object`/`get_object`，`idempotency_arg='operation_id'`）。
- 写白名单扩展：`PI_WRITE_TOOL_KEYS`/`PI_READ_TOOL_KEYS`（环境变量受控启用）。
- 写后回读证据：`pi_host_tools._perform_readback` → `write_evidences`（已实现，
  第十阶段收口）。

### 待补的缺口

1. **受控模型要「真的调用写工具」**：需要一份 mock script，让 Node Pi 输出
   `tool_call`（update_object）而非直接 answer，从而驱动宿主走授权→准入→写→回读。
2. **进程级串联**：正式 HTTP → Worker 启动 Pi → 工具调用 → 回读 → Pi 答复 → 收尾，
   用真实 serve_api + run_worker 子进程（仿 `test_phase11_process_e2e`）。
3. **重启不重复写**：复用 `PI_CRASH_AFTER_SETTLE` 故障注入钩子，验证工具已结算、
   Node 未吸收时恢复不重发。

### 需改动文件（预估）

| 文件 | 改动 |
|---|---|
| `tests/test_phase12_write_loop.py`（新增） | 7 步闭环进程级验收 |
| `tests/fixtures/` 或 mock script | 写闭环的确定性 Pi 工具调用脚本 |
| （可能）`pi_runtime.py` | 若 mock script 工具调用链路缺钩子则补 |

## 三、SIGILL 归因（评审点 3）

上轮完整回归 1 个失败：`test_worker_execution.test_worker_process_starts_and_exits_cleanly`
（Worker 子进程退出码 -9，当时记为 SIGKILL；评审称 SIGILL，需按实际退出码/信号
复核）。动作：

1. **记录**：失败用例名、退出码/信号、日志（重跑并抓 worker 子进程 stderr）。
2. **有限复验**：相同条件（完整 discover 或该文件单独）跑 N 次，观察是否复现；
   区分「运行环境（资源/信号）」vs「程序缺陷」。
3. **结论写进验收文档**：不再写「偶发所以忽略」，改为「已复验 + 归因」。

## 四、浏览器手工测试（评审点 4）

无 Playwright 也可手工测，记录结果到 `docs/phase12-browser-manual.md`：
- 连续发消息
- 刷新
- 断网重连
- 切换会话
- 后台任务并存

（此项需用户在浏览器端操作后回填，或由我提供手工测试清单 + 记录模板。）

## 五、执行顺序建议

1. 先做「查询身份模型」方案确认 → 最小改动 + 补测试；
2. 再做「Pi 自主修改闭环」7 步测试；
3. SIGILL 复验归因；
4. 浏览器手工测试清单 + 记录模板；
5. 更新 `docs/phase12-acceptance.md` 结论。
