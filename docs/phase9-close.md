# 第九阶段收口：单一 MCP 服务的受控接入与协议验收

分支 `pi_agent_inset`。保留 LangGraph 生产默认、不开放通用写工具、不修改全局
默认引擎、不实施第 10～12 阶段。

## 范围与结论

本阶段完成三件事：

1. **补强结果消费断言**：修复 `test_phase8_e2e.py` 中「远端结果与模拟答复
   硬编码相同文本」的证据不足——受控模型替身现在必须**检查真实收到的
   toolResult** 后才生成答复。
2. **建立真正通过 MCP transport 的受控测试服务**：新增 `tests/mcp_probe_server.py`
   （stdio transport + 标准 MCP Tasks 原语），经 `mcp_adapter` 的正式
   connect / list / call / task 链路验证，不依赖任何外部供应商。
3. **Pi 工具白名单去硬编码**：把 `pi_host_tools.py` 固定的 `local:web:search`
   解析改为可信目录（能力包 registry）驱动的显式只读白名单；同步只开放只读
   工具，异步只开放固定合同能力（协议 `mcp-task` 与显式选中的文件合同）。

## 关键改动

### 1. 结果消费断言（Node 侧 + 测试）

- `pi-runtime/src/session.ts`：`MockTurn` 的 `answer` 轮新增可选 `expect` 字段。
  受控模型（`runMock`）在产出答复前，检查上下文里是否已有含该标记的
  toolResult 文本；命中则答复文本**派生自真实 toolResult 内容**
  （`结果：<toolResult文本>`），未命中判失败、绝不产出答复。
- `pi-runtime/src/protocol.ts`：`mock` 类型声明同步加 `expect` 字段。
- `tests/test_phase8_e2e.py`：恢复轮的 mock 脚本改为
  `{'type': 'answer', 'text': '占位', 'expect': '指定远端结果 clip-001'}`，
  用 `expect` 证明模型替身真实消费了注入的结果，而非硬编码相同文本。

### 2. 受控 MCP 测试服务（真实 transport）

- `tests/mcp_probe_server.py`：stdio 服务端，暴露：
  - `read_only_probe`（`readOnlyHint=true`）：同步只读，回显带唯一标记的结果；
  - `async_submit`（`taskSupport=required`）：用 `server.experimental.enable_tasks()`
    + `ctx.experimental.run_task(work)` 走标准 MCP Tasks 原语。
  - 计数（read_only_calls / async_submissions）经 `--state-file` JSON 文件
    跨进程暴露，测试端据此断言「提交恰好一次」。
- `tests/test_phase9_mcp_contract.py`：10 项协议验收测试（发现/schema、同步
  只读、异步提交一次、结果交付含唯一标记、取消协作式、错误确定性标注）。

### 3. 工具白名单去硬编码

- `pi_host_tools.py`：
  - `ALLOWED_TOOL_KEY` → `SYNC_READ_TOOL_KEYS`（显式只读白名单，默认仍只含
    `local:web:search`，行为不变）；
  - `_resolve_web_search()` 从 registry 按白名单查找并校验 `effect == 'read'`；
  - `_search_receipt` → `_receipt_content(updates, tool_name)`，工具名由调用方
    传入，增量归一沿用通用字段（documents / errors / usage），不再写死 web_search。
- `pi_runtime.py`：`default_tool_catalog()` 按 `SYNC_READ_TOOL_KEYS` 白名单从
  registry 取声明，不再硬编码 `local:web:search`。

## 验证结果

- Node 侧：`npx tsc` 干净构建，`node --test` 14 项通过。
- Python 侧（`.venv-pi-test`，无需 PG）：
  - `test_phase9_mcp_contract.py`：10 项通过（真实 MCP transport）；
  - `test_execution_gateway.py`：22 项通过；
  - `test_pi_runtime.py` + `test_pi_snapshot.py` + `test_pi_recovery.py`：71 项通过；
  - `test_governed_pi_integration.py` / `test_mcp_tasks.py` / `test_mcp.py` /
    `test_shipped_async_contracts.py`：96 项通过 + 4 跳过（需 PG）+ 17 子测试通过。

## 跳过项与剩余限制

- **真实供应商联调未执行**：仍需可查询的任务身份与真实凭据，按规格另行报告。
  受控 MCP 服务端 + 受控模型已覆盖完整协议与恢复不变量。
- **主体隔离（tenant/user 凭据绑定）**：由 Python 侧授权层负责，第六/八阶段
  已用 mock 账本覆盖；受控 stdio 服务端本身无鉴权，本阶段未在真实 transport
  上重复验证凭据注入（需真实供应商的租户凭据）。
- **第 10～12 阶段未实施**：平台修改/写后回读、正式会话入口、部署与受控启用
  Pi 均不在本阶段范围。
- `pi-runtime/dist/` 为编译产物，被 gitignore，提交只含 `.ts` 源码。
