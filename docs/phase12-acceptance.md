# 第十二阶段：本地集成验收 — 能力矩阵与验收基线

> 分支 `pi_agent_inset`，HEAD `4be0f1a`。本文件是能力矩阵（P3）+ 验收说明（P13）的
> 交付物，随验收进展持续更新。

## 一、代码基线

| 项 | 值 |
|---|---|
| 分支 | `pi_agent_inset` |
| HEAD | `4be0f1a`（第十一阶段最后四项定点修复） |
| 工作区 | 干净（无未提交修改） |
| Python | 3.12.15（`/opt/anaconda3/envs/demo/bin/python`） |
| Node | v22.22.2 |
| 依赖锁 | `pi-runtime/package-lock.json`（@mariozechner/pi-ai 0.73.1）、`requirements.lock.txt` |
| Schema | `LATEST = v10_turn_principal`（`v1_baseline_20261029` … `v10_turn_principal`） |
| 默认引擎 | 验收当时是 `langgraph`。**当前代码默认已改为 `pi`**（见 `docs/architecture.md`）。本表其余行仍是当时的验收记录 |

## 二、能力矩阵

> 「是否实现」= 代码链路存在；「是否开放」= 生产/正式入口默认放行。
> 「开关为 false」不等于能力通过：只能证明拒绝路径正确，不能证明功能完成。

| 能力 | 是否实现 | 是否开放 | 正式入口 | 所需配置 | 已有证据 | 本轮验收项 |
|---|---|---|---|---|---|---|
| 闲聊 chat | ✅ | ✅ | `POST /api/sessions/{id}/messages`（路由 kind=chat）→ 真实 Pi | `SESSION_CHAT_ENABLED=true`（默认） | `test_phase11_*`、进程级 e2e | P7 问候/连续对话 |
| 只读查询 query | ⚠️ 部分 | ✅ 路由开放 | 同上（kind=query，带只读工具白名单 `local:web:search`） | `SESSION_QUERY_ENABLED=true`（默认） | `test_phase11_*` 路由/执行 | P7 缺参追问/工具失败说明；**工具成功调用未验证**（见下） |
| 任务创建 | ✅ | ⛔ 会话内关闭 | `POST /api/tasks`（正式创建入口，不受会话开关约束） | 会话内建任务需 `SESSION_TASK_CREATE_ENABLED` | `test_api`/`test_runs` 创建链路 | P9 经 `POST /api/tasks` 创建+引擎身份+去重 |
| 进度查询 | ✅ | ✅（只读） | 会话消息（控制动词+查进度）读本地任务状态；`GET /api/tasks/{id}` | — | `test_phase11_e2e` | P8 业务进行中查进度不改要求版本 |
| 补充要求 | ✅ | ⚠️ 会话内关闭 | `POST /api/runs/{run_id}/inputs`（正式入口） | `RUN_INPUTS_ENABLED=true`（默认） | `test_run_inputs` | P9 指定任务补充要求进模型+旧候选阻断 |
| 审批 | ✅ | ✅ | `POST /api/approvals/{id}/approve` / `reject` | 治理开启 | `test_phase10_*` | P10 审批批准/拒绝 |
| 暂停/恢复/取消 | ✅ | ⚠️ 会话内控制关闭 | `POST /api/runs/{run_id}/pause` / `resume` / `cancel` | 会话内需 `SESSION_TASK_CONTROL_ENABLED` | `test_execution_control` | P9 暂停/恢复/取消 |
| 平台修改回读 | ✅ | ⚠️ 写白名单默认空 | 经 Pi 写工具 + `verify_platform_write` 回读 | `PI_WRITE_TOOL_KEYS`（受控启用） | `test_phase10_platform_write` | P10 真实 MCP 写+回读证据 |
| 异步等待 | ✅ | ✅ | 异步合同 `ASYNC_CONTRACTS_ENABLED` | `async_contracts_enabled` | `test_phase8_e2e` | P11 持久等待+唤醒 |
| 重启恢复 | ✅ | ✅ | 租约/快照/对账（`reconcile`） | 租约配置 | `test_execution_recovery` | P11 崩溃点接管 |

### 已知缺陷（如实记录，不视为通过）

- **只读查询的工具调用（web_search）**：query turn 无业务 run 行，`governed_call`
  因 `current_ledger()` 为 None 返回 `no_run` 拒绝工具调用；且 web_search 依赖真实
  搜索 API（`DEEPSEEK_API_KEY`），隔离环境无受控搜索服务。因此「只读工具查询」的
  **成功路径未验证**。已实现的是：query 路由 + 真实 Pi 执行 + 工具失败时的诚实说明
  （Pi 基于 `no_run` 生成"无法回答"，非硬编码"查询不可用"）。若要完整交付需：① 让
  query turn 的只读工具取得执行身份（账本/run 行，或 governed_call 对无 run 只读工具
  走轻治理路径）；② 受控搜索服务。**列为未交付项**。

### 关键开关当前值（默认）

| 开关 | 默认 | 说明 |
|---|---|---|
| `SESSION_CHAT_ENABLED` | `true` | 会话聊天开放 |
| `SESSION_QUERY_ENABLED` | `true` | 会话查询开放 |
| `SESSION_TASK_CREATE_ENABLED` | `false` | 会话内建任务**关闭**（正式建任务走 `POST /api/tasks`） |
| `SESSION_TASK_CONTROL_ENABLED` | `false` | 会话内暂停/恢复/取消**关闭**（正式控制走 `/runs/{id}/...`） |
| `WRITE_TOOL_KEYS` | `()` 空 | Pi 写工具默认不开放。`PI_WRITE_TOOL_KEYS` 必须再开 `PI_WRITE_TOOLS_ENABLED=true` 才生效，并写审计日志 |
| `SYNC_READ_TOOL_KEYS` | `('local:web:search',)` | Pi 只读工具白名单 |
| `SESSION_CHAT_CONCURRENCY` | `2` | 聊天并发额度 |
| `SESSION_TURN_LEASE_SECONDS` | `120` | 聊天 turn 租约 |
| `DEFAULT_RUNTIME_KIND` | 验收当时 `langgraph`；当前源码默认 `pi` | 只影响新建 run。写工具仍默认关闭，扩展开关是 `PI_WRITE_TOOLS_ENABLED` |

## 三、隔离验收环境

- 测试库：`postgresql://…/agent_platform_test`（库名含 `test`，`PG_TEST_DSN` 指定）。
- 测试清库范围：`tests/exec_env.py::TEST_TABLES` 的 `TRUNCATE ... CASCADE`，只指向
  上述隔离测试库（`valid_test_dsn` 校验库名含 `test`，否则 SkipTest）。
- 产物目录：临时目录（`tempfile.mkdtemp()`），不触碰真实业务对象。
- 合成用户/租户：`tenant-a` / `user-u`（ticket 鉴权），不连真实账号。

## 四、构建与启动命令

以下命令均在本仓库根目录（`main_branch/`）执行。解释器 `/opt/anaconda3/envs/demo/bin/python`
（3.12.15），Node v22.22.2。

```bash
# 1. 迁移（也作为 schema 护栏：只改 SCHEMA 没写 UPGRADES 会非零退出）
PYTHONPATH=src /opt/anaconda3/envs/demo/bin/python -m scripts.migrate
#    （API/Worker 启动时 store.ready() 也会在 DB_AUTO_MIGRATE=true 下自动迁移）

# 2. Node 桥接构建（产出 dist/main.js，Python 桥接启动的就是它）
cd pi-runtime && npm run typecheck && npm run build && cd ..
#    Node 测试：node --test dist/session.test.js（14 项）

# 3. 启动 API（默认 127.0.0.1:8002，ticket 鉴权）
PYTHONPATH=src /opt/anaconda3/envs/demo/bin/python -m scripts.serve_api --port 8002

# 4. 启动 Worker（消费业务 run + 聊天 turn）
PYTHONPATH=src /opt/anaconda3/envs/demo/bin/python -m scripts.run_worker

# 5. 测试（隔离测试库，库名含 test，用例会 TRUNCATE）
export PG_TEST_DSN='postgresql://<user>@127.0.0.1:5432/agent_platform_test'
export PYTHONPATH=src APP_ENV=test AUTH_MODE=off
/opt/anaconda3/envs/demo/bin/python -m unittest discover -s tests -t .
#    或单个：-m unittest tests.test_phase11_process_e2e -v

# 6. 停止：API/Worker 为长期进程，Ctrl-C 或 kill PID；无持久状态需清理
```

关键配置项（环境变量）：`DATABASE_URL`、`AUTH_MODE`（ticket/off）、
`SESSION_CHAT_ENABLED`、`SESSION_QUERY_ENABLED`、`SESSION_TASK_CREATE_ENABLED`、
`SESSION_TASK_CONTROL_ENABLED`、`PI_WRITE_TOOL_KEYS`、`PI_READ_TOOL_KEYS`、
`DEFAULT_RUNTIME_KIND`（默认 langgraph）、`ASYNC_CONTRACTS_ENABLED`。**凭据与临时
端口不作为固定部署配置保存**。

## 五、本轮验证结果汇总

| 层级 | 命令/文件 | 结果 |
|---|---|---|
| 单元测试（纯逻辑） | `test_phase11_messages`（路由/幂等）、`test_phase10_platform_write`（写证据） | ✅ 通过 |
| 真实 PG | `test_store`、`test_runs`、`test_execution_recovery`、`test_pi_runtime` | ✅ 163 项通过 |
| 正式 HTTP 进程 | `test_phase11_process_e2e`（真实 serve_api + run_worker 子进程） | ✅ 6 项通过 |
| Node | `node --test dist/session.test.js` | ✅ 14 项通过 |
| 受控 MCP | `test_phase10_e2e`/`test_phase10_final_e2e`/`test_phase9_mcp_contract`（stdio transport） | ✅ 31+ 项通过 |
| 真实模型 | DeepSeek「你好」基础聊天 | 见下「未执行项」 |
| ruff / mypy | `ruff check src scripts tests` / `mypy` | ⚠️ 历史遗留 F401/F841/F821（非本轮引入） |
| 完整回归 | `python -W error::RuntimeWarning -m unittest discover` | ⚠️ 1157 项，1 个偶发失败（见下） |

### 完整回归说明

`unittest discover` 全量 1157 项，唯一失败是
`test_worker_execution.test_worker_process_starts_and_exits_cleanly`（Worker 子进程
退出码 -9 = SIGKILL）。该用例**单独跑通过**（0.9s），且走 `--jobs-only --once`
路径（不经本轮改动的聊天/业务调度）；-9 是外部强杀而非代码异常，判定为完整
discover 下多子进程测试累积的**偶发资源/时序问题**，非本轮引入。

## 六、已知问题与未执行项（第十二阶段缺口修复后更新）

> 第十二阶段按评审建议补齐两大缺口（查询身份模型 + Pi 自主修改闭环），
> 详见 `docs/phase12-gap-fix-plan.md`。下表反映修复后的状态。

| 项 | 修复前 | 修复后 |
|---|---|---|
| 只读查询工具调用 | query turn 无 run 行 + 无账本，`governed_call` 返回 no_run，工具成功路径不可用 | ✅ 建内部查询执行记录（runs 行）+ 绑 ActionLedger，只读工具走完整治理链；跨主体/超预算/撤权仍拒绝（`test_phase12_query_identity`，8 项） |
| Pi 写推理完整闭环 | 写后回读只到工具链路（HostToolCaller 直调），未到「Node Pi 自主决策写」 | ✅ 受控模型脚本驱动 Node Pi 自主读→写→回读→答复，进程级 7 步闭环（`test_phase12_write_loop`） |
| 真实供应商联调 | 未执行 | ⚠️ 仍未执行（受控 MCP 替代）；真实模型仅基础聊天可验证 |
| 浏览器刷新/断线恢复 | 未执行 | ⚠️ 手工测试清单见 `docs/phase12-browser-manual.md`，待浏览器端回填 |
| SIGKILL 偶发失败 | 记为「偶发忽略」 | ✅ 复验 5 次通过 + 归因（见 phase12-browser-manual.md 附注），非程序缺陷 |

**本轮修复的代码缺陷**（均为真实 bug，非仅测试）：
1. `execution_gateway`/`hub_messages`：query turn 无账本 → 新增
   `store.create_turn_run` + `settle_turn_run`（内部查询执行记录），`_run_pi_chat`
   的 query 分支绑账本。
2. `pi_host_tools._find_readback_tool`：硬编码 `name == 'get_object'`，自动发现的
   MCP 工具名带 `mcp__` 前缀导致回读工具匹配失败 → 改为按 tool_key 尾缀匹配。
3. `pi_host_tools._perform_readback`：读工具（get_object）也带 object_id 被误判
   「需要回读」→ 加 effect 判断，只对写工具回读。
4. `pi_protocol.validate_args_against_schema`：`type: object` 且无 properties 的子
   schema 被误判「含未声明字段」→ 尊重 additionalProperties / 空 properties 视为开放对象。
5. `tests/mcp_platform_server.py`：update_object 的 `fields` 缺 additionalProperties → 补齐。

**ruff/mypy 历史遗留**：`ruff check` 本轮改动文件已全绿；剩余 F841/F821 均为更早
阶段遗留，非本轮引入。

---

## 七、最终结论

**结论：两大核心缺口已补齐（查询身份模型 + Pi 自主修改闭环），仍为「限定能力
本地试运行」，非「本地集成验收完成」。**

### 本轮补齐的缺口（第十二阶段）

| 缺口 | 状态 | 证据 |
|---|---|---|
| 只读查询工具调用（web_search） | ✅ 已修复 | `test_phase12_query_identity.py`（8 项：成功 + 跨主体/超预算/撤权拒绝 + 收尾 + 不污染领取池） |
| Pi 自主修改 7 步闭环 | ✅ 已补齐 | `test_phase12_write_loop.py`（进程级：HTTP→Worker→Pi 读/写→授权/账本→回读核验→Pi 答复→收尾） |
| SIGKILL 偶发失败归因 | ✅ 已复验 | 5 次通过 + 归因（非程序缺陷，见 phase12-browser-manual.md） |
| 浏览器手工测试 | ⚠️ 清单已出 | `docs/phase12-browser-manual.md`，待浏览器端回填 |

### 仍未交付（如实记录）

| 缺项 | 影响 |
|---|---|
| 真实供应商联调未执行 | 平台写/外部服务用受控 MCP 替代，未连真实业务对象 |
| 浏览器刷新/断线恢复未实机验证 | 前端逻辑 + request_id 幂等已测，真实浏览器交互待回填 |

这些缺项**不影响**已实现的：聊天、查询工具调用、任务创建/补充/控制、进度查询、
审批、写后回读工具链路、Pi 自主写闭环、异步等待、重启恢复——均通过受控验证。

### 交付清单与可继续开发基线

- **代码修改**（本轮缺口修复 + 验收脚本）：
  - `src/webpage_agent/store/pg_runs.py`：新增 `create_turn_run` / `settle_turn_run`
    （内部查询执行记录）+ 清理历史 F401。
  - `src/webpage_agent/api/hub_messages.py`：`_run_pi_chat` query 分支绑账本 + 建内部记录 + 收尾。
  - `src/webpage_agent/agent/pi_host_tools.py`：`_find_readback_tool` 按 tool_key 尾缀匹配；
    `_perform_readback` 只对写工具回读；清理历史 F841。
  - `src/webpage_agent/agent/pi_protocol.py`：`validate_args_against_schema` 尊重 additionalProperties。
  - `tests/mcp_platform_server.py`：update_object 的 fields 补 additionalProperties。
  - `tests/test_phase12_query_identity.py`（新增，8 项）、`tests/test_phase12_write_loop.py`（新增，1 项进程级）。
  - 文档：`docs/phase12-gap-fix-plan.md`（方案）、`docs/phase12-browser-manual.md`（浏览器清单 + SIGKILL 归因）。
- **可继续开发基线**：HEAD `4be0f1a` + 上述缺口修复增量。分支 `pi_agent_inset`，
  默认引擎 langgraph 未切换，写能力开关保持关闭。

**不自动发布、不推送、不切换生产默认。** 后续新增需求属于继续开发，不新增阶段编号。
Docker、服务器、上线流程留待明确需要部署时再做。

（本次验收未推送；如需入库请显式确认。）
