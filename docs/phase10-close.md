# 第十阶段收口：平台修改、写后回读与任务要求变更

范围：一个隔离测试平台，不开放生产通用写工具，不切换默认引擎（生产仍 LangGraph）。

## 一、本阶段完成内容

### 1. 可核验的最小平台合同（任务 5）
- 新增 `tests/mcp_platform_server.py`：stdio transport 受控平台，提供
  `get_object` / `update_object` / `get_operation`。
- 合成 tenant/user（`subject`）；对象归属、revision、操作记录持久化到 JSON
  文件，跨进程重启后仍可查询。
- 写参数含 object_id / expected_revision / operation_id（幂等键）。服务端原子
  执行归属校验、revision 比较、幂等去重；同键不同参数拒绝（E_IDEMPOTENCY）。
- 服务端记录 `actual_writes`（实际写入次数），与本地 action 数量解耦。

### 2. 多工具解析 + 显式写白名单（任务 6）
- `pi_host_tools.py`：`HostToolCaller.resolve_tool(tool_name)` 按请求工具名
  **唯一匹配**，不再取「白名单首项」；名字匹配但不在白名单 → 拒绝。
- 读白名单 `SYNC_READ_TOOL_KEYS` 与写白名单 `WRITE_TOOL_KEYS`（默认空，
  即「默认不开放写入」）分离。
- 完整 schema 进 Pi，Python 执行点再次校验；继续走现有 MCP runner 与共享网关。

### 3. 对象级隔离（任务 7）
- `tool_policy.scope_satisfied` 由「字段非空即授权」改为**值约束比较**：支持
  `key=value` 形式（如 `object_id=obj-a`），把可信租户/项目/对象约束与最终参数
  实际值逐一比较。可信范围由宿主（政策表）提供，模型不能自行扩大。

### 4. 统一最终参数与幂等身份（任务 8）
- `HostToolCaller.call()` 在宿主分配逻辑调用身份后，用
  `tools/core.execution_arguments()` 生成**最终发送参数**（含幂等键注入），
  授权、审批指纹、账本准入、远端发送针对同一份最终参数。
- `execution_gateway` 把最终参数写入 receipt（`final_args`），随 settle 持久化，
  恢复时复用；同键不同参数由服务端拒绝，不静默换 revision 重试。

### 5. 审批暂停与恢复（任务 9）
- `ToolReceipt` 增加 `approval_id` 与 `needs_approval` 标记；`_handle_tool_call`
  在 needs_approval 时持久化等待事实到快照（`awaiting_approval`）、结束本轮，
  不再给模型错误让它继续尝试。
- 快照 `_SNAPSHOT_KEYS` 增加 `awaiting_approval`；Node 侧协议/主循环支持
  `needs_approval` 标记（等价 accepted，结束本轮 `finished('waiting')`）。
- `pi_recovery.decide` 识别 awaiting_approval；`Hub._decide_approval_recovery`
  按审批状态恢复：批准后转 RESUME_ADMIT 重查权限/版本/revision，拒绝/过期
  有明确行为。

### 6. 要求内容—决策版本—动作准入绑定（任务 10）
- `execution_gateway.plan_intent` 增加 Pi 顶层 `intent_version` 回退，Pi 工具
  调用现在携带决策版本，准入事务比较 `expected_intent_version`。
- 恢复时 `_pi_resume_plan` 把 pending 补充输入内容真正注入 `user_input`，而非
  仅更新版本号。

### 7. 通用写结果 + 独立回读证据（任务 11）
- `pi_host_tools` 结果归一泛化，保存 object_id / operation_id / new_revision /
  修改字段；`_MERGEABLE_KEYS` 扩展，禁止任意增量覆盖宿主身份/租约/终态。
- 新增 `core/platform_verify.py`：`verify_platform_write` 用同一可信主体独立
  读回对象、核对 revision 与字段，返回 `readback_match`；`needs_readback` 判定
  写结果；`verify_operation` 按原操作 ID 核实远端。

### 8. 远端已写本地未记账窗口（任务 12）
- 恢复先 `verify_operation` 按原 operation_id 核实，再结合对象回读判断；
  无法确认进 `RESUME_RECONCILE`（不盲目重发、不生成新写冒充恢复）。

### 9. 宿主完成条件（任务 13）
- `outcome._verified` 识别 `readback_match=False`（缺回读/回读不符 → 未核实），
  写任务不再「有 answer 即成功」。

### 10. 正式链路验收（任务 14）
- 新增 `tests/test_phase10_e2e.py`（真实 PG + Node + 受控模型 + 真实 MCP
  transport）与 `tests/test_phase10_platform_write.py`（纯逻辑回读/完成条件）。

## 二、验证结果

| 套件 | 结果 |
|---|---|
| Node 构建 + session.test | 干净构建，14 项通过 |
| test_phase10_e2e.py | 5 项通过（真实 PG + Node + MCP transport） |
| test_phase10_platform_write.py | 14 项通过 |
| test_phase9_mcp_contract.py | 10 项通过 |
| test_phase8_e2e.py | 通过（真实 PG 回归基线） |
| 网关/Pi runtime/snapshot/recovery/run_inputs | 163 项通过 |

回归同步读取、异步等待、LangGraph 路径均无破坏（生产默认引擎未切换）。

## 三、跳过项与剩余限制

- **真实供应商联调未执行**（需可查询任务身份 + 真实凭据，另行报告）。
- 越权/revision 冲突/断连恢复/迟到执行者等完整 Pi 推理链路，部分以受控模型
  替身 + mock 写工具验证，未接入真实供应商写。
- 第 11～12 阶段未实施；不开放通用写工具、不改全局默认引擎。

## 四、改动文件

新增：`src/webpage_agent/core/platform_verify.py`、`tests/mcp_platform_server.py`、
`tests/test_phase10_e2e.py`、`tests/test_phase10_platform_write.py`、本收口文档。
修改：`pi-runtime/src/{main,protocol}.ts`、
`src/webpage_agent/agent/{pi_host_tools,pi_runtime,pi_recovery,pi_snapshot}.py`、
`src/webpage_agent/api/hub_recovery.py`、`src/webpage_agent/core/outcome.py`、
`src/webpage_agent/tools/{execution_gateway,tool_policy}.py`。
