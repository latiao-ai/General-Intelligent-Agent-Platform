# 第十阶段正式链路收口：写后回读、审批恢复与补充要求注入

范围：第十阶段正式链路收口，不扩展生产写权限，不实施第十一阶段会话改造。

## 一、本阶段完成内容

### 1. 写后回读接入宿主执行链（任务 15）
- `pi_host_tools.HostToolCaller` 新增 `_find_readback_tool` / `_perform_readback`：
  写工具成功结算后，用**同一可信主体**（同 MCP server）独立回读对象，回读走
  **同一条受治理链**（`governed_call`，授权/预算/结果记录），不是手工调用核验函数。
- 逐个写 action 保存证据到 `state['write_evidences']`（列表），每项含对象 ID、
  操作 ID、参数指纹（args_hash）、要求版本、回读 action ID、逐项核验结论
  （checks）。不是顶层单个 `readback_match`。
- 读白名单/写白名单统一经 `_effective_read_keys` / `_effective_write_keys`，
  支持环境变量 `PI_READ_TOOL_KEYS` / `PI_WRITE_TOOL_KEYS` 显式扩展（受控启用，
  不靠测试注入或临时全局切换）。

### 2. 加强对象及版本匹配（任务 16）
- `core/platform_verify.verify_platform_write` 逐项核对：对象一致（object_id）、
  revision 符合合同（缺 new_revision 不跳过）、预期字段存在且匹配（缺字段不跳过）。
  返回 `checks` 逐项明细，不再是顶层单个布尔。
- `verify_operation` 严格四态：`applied` / `not_found`（明确不存在）/
  `permission_denied`（权限错误，不能当未执行）/ `query_failed`（结果未知）。

### 3. 修正宿主成功判定顺序（任务 17）
- `core.outcome.final_status`：存在平台写动作（`write_evidences`）时，先检查
  **完整写动作集合**（逐项核验 + 无未知状态）再决定；`answer` 仅展示，不是写
  任务成功依据。未核实写 + answer / 回读错对象 / 缺 revision / 第一项未核实而
  最后一项已核实，均判 `needs_reconcile`。纯只读/闲聊保留「有答复即成功」。

### 4. 修通审批暂停与批准恢复（任务 18）
- Node `finished` 增 `wait_reason`（`approval` / `external`）；`PiRuntime.
  _apply_finished` 据此把 run 停在 `waiting_approval` 或 `waiting_external`。
- 审批快照保存完整调用身份 + 最终参数 + 参数指纹 + 对象 revision + 要求版本。
- 批准后 `_decide_approval_recovery` 恢复原候选（原 call_id + 最终参数），执行点
  重查权限/版本/revision；拒绝/过期转待核实。恢复成功（RESUME_ADMIT）后清空
  `awaiting_approval` 事实，不残留旧等待。

### 5. 让补充要求真正进入模型上下文（任务 19）
- 打通 Hub → run_task → PiRuntime → Node 的增量输入传递：`_pi_resume_plan` 装配
  `pending_inputs`（原文 + 序号），`run_task` 展平，`PiRuntime` 传入 `resume.
  pending_inputs`，Node `importContext` 后逐条 `pushUser` 作为**明确的新消息**。
- 恢复时 `state['user_input']` 用 merged 要求（原目标 + 补充），不再用原始 text。

### 6. 执行真实的未知操作核实（任务 20）
- `execution_gateway` 的 unknown 结算把 `final_args`（含 operation_id）写进 receipt；
- `HubRecoveryMixin._verify_unknown_writes` 恢复 unknown 写时按原 operation ID 调
  `get_operation` 核实，严格四态：applied → 复用（RESUME_DELIVER）、not_found →
  可显式重试、permission_denied / query_failed → 保持待核实。不盲目重发。

### 7. 正式链路测试（任务 21）
- 新增 `tests/test_phase10_final_e2e.py`：真实 MCP transport + 真实 PG 账本，验证
  写后回读接入宿主执行链（两次写/两个对象逐项证据）、未知操作四态核实。

### 8. 阶段报告证据分类（任务 22）
见下方「验证结果（分层）」——不把分层测试合称完整链路验证。

## 二、验证结果（分层，不合并）

| 证据类别 | 套件 | 通过 |
|---|---|---|
| **核验单测**（纯逻辑，无 PG/无 MCP） | `test_phase10_platform_write.py`（回读核验、完成条件、成功判定顺序） | 21 |
| **适配器测试**（真实 MCP transport，无 PG） | `test_phase9_mcp_contract.py`（受控 MCP 服务协议） | 10 |
| **PG 仓储 + 治理链测试**（真实 PG，无完整 Pi 推理） | `test_phase10_e2e.py`（受治理写 + final_args） | 5 |
| **正式端到端测试**（真实 MCP transport + 真实 PG 账本 + 受治理写后回读） | `test_phase10_final_e2e.py` | 5 |
| **异步链路回归**（真实 PG + 真实 Node + Hub） | `test_phase8_e2e.py` | 3 |
| **Node 桥接单测** | `pi-runtime` `node --test` | 14 |

> 说明：以上各类是**分层证据**，互不替代。「正式端到端」覆盖的是「写工具经受治理
> 链 + 真实 MCP transport + 写后回读」，但**未走完整 Hub/Worker 入口 + 受控模型 +
> 真实 Node 的写链路**（Pi 完整推理写场景尚未用受控模型 + 真实 Node 串联验证）。

## 三、跳过项与剩余限制

- **真实供应商联调未执行**：受控平台是 `tests/mcp_platform_server.py`（JSON 持久化），
  不是真实供应商平台；真实供应商凭据/对象归属联调另行报告。
- **完整 Pi 写推理链路**（受控模型 + 真实 Node + 真实 MCP transport 的 update_object
  端到端）尚未串联验证——本阶段验证到「受治理写 + 写后回读」这一层，Pi 模型自主
  决策调用的完整闭环留待后续补充。
- **第 11 阶段会话改造未实施**（正式会话入口、聊天/业务 run 区分、多任务关联等）。

## 四、改动文件

新增：`tests/test_phase10_final_e2e.py`、本收口文档。
修改：`pi-runtime/src/{main,protocol}.ts`、
`src/webpage_agent/agent/{pi_host_tools,pi_runtime}.py`、
`src/webpage_agent/api/{main,hub_recovery}.py`、
`src/webpage_agent/core/{outcome,platform_verify}.py`、
`src/webpage_agent/tools/execution_gateway.py`。
