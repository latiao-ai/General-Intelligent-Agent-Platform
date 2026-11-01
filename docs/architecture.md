# 目录结构

采用 **src/ 布局**：可导入的包放在 `src/webpage_agent/`，仓库根只放工程文件。
这样测试必须通过 `PYTHONPATH=src`（或安装后）才能导入，包装错了立刻暴露，
而不是被"当前目录恰好在 `sys.path` 里"掩盖过去。

```
webpage-agent-main/
├── src/webpage_agent/        # 唯一的可导入包
│   ├── api/                  # 对外接口层
│   ├── agent/                # LangGraph 执行内核
│   ├── tools/                # 工具调用与权限策略
│   ├── store/                # 持久化
│   ├── governance/           # 治理、审批、身份
│   ├── observe/              # 可观测性
│   ├── core/                 # 领域模型、配置、共享服务
│   ├── capabilities/         # 本地能力插件（import 即注册）
│   └── integrations/         # 外部集成（MCP）
├── scripts/                  # 可执行入口，统一用 python -m scripts.xxx 调用
├── tests/                    # 用例，镜像源码职责
├── contracts/                # 异步工具契约（JSON）
├── docs/                     # 文档
├── deploy/                   # 部署配置
├── Dockerfile
├── compose.yaml
└── pyproject.toml            # 打包 + ruff + mypy 配置，单一来源
```

## 分层职责

| 包 | 职责 | 关键模块 |
|---|---|---|
| `api/` | HTTP 层：路由、鉴权、应用装配 | `routes.py`（对外接口与页面）、`api_hub.py`（任务编排入口）、`main.py`（执行流程与阶段标签）、`auth.py`、`auth_connections.py` |
| `agent/` | LangGraph 图与节点，只依赖状态与工具 | `graph.py`、`nodes.py`（规划/续跑/执行/观察/核对/补偿）、`context.py`、`run_inputs.py`、`scene_context.py` |
| `tools/` | 工具汇总、调用上下文、权限策略、结果整形 | `core.py`（内核工具汇总，不 import 任何业务包）、`tool_context.py`、`tool_policy.py`、`tool_authorization.py`、`tool_results.py` |
| `store/` | 持久化仓储，唯一实现是 PostgreSQL | `pg_store.py`、`persistence.py`（`Repository` 协议）、`migrations.py`、`ledger.py`、`idempotency.py`、`artifact_store.py` |
| `governance/` | 治理：策略、审批、票据、执行控制、复核 | `policies.py`、`core.py`、`approvals.py`、`ticket_auth.py`、`request_review.py`、`execution_control.py` |
| `observe/` | 可观测性：事件、观测、健康检查、日志、遥测 | `events.py`、`observation.py`、`runtime_check.py`、`logging_setup.py`、`telemetry.py`、`maintenance.py`、`task_notify.py`、`errors.py` |
| `core/` | 领域模型与共享设施 | `schemas.py`（LangGraph 状态定义）、`config.py`（设置与项目根）、`services.py`、`messages.py`、`outcome.py`、`memory.py`、`credentials.py`、`external_tasks.py`、`async_contracts.py`、`profile.py`、`mcp_connections.py`、`md.py`、`files.py`、`sources.py` |
| `capabilities/` | 本地能力插件，`@tool` 声明，import 即注册 | `registry.py`、`base.py`、`web.py`、`answers.py`、`export.py`、`publishing.py` |
| `integrations/` | 外部平台集成 | `mcp_adapter.py`、`mcp_servers.py`、`mcp_subject.py`、`mcp_tasks.py`、`search.py` |

## 两条硬约束

1. **`tools/core.py` 不 import 任何具体业务包。** 加一个本地能力等于往
   `capabilities/` 加一个文件，内核一个字都不用改。
2. **`agent/` 不认识数据库和 HTTP。** 节点只读状态、调工具，副作用全部经
   `store/` 与 `tools/`。

## 路径解析

不要按 `__file__` 的固定层数算项目根——文件在 `src/webpage_agent/core/config.py`，
层数和"仓库根"之间隔了 3 级，一挪位置就全错（`.env` 读不到、`artifacts/` 跑偏）。

`core/config.py` 的 `_discover_project_root()` 改为**向上查找 `pyproject.toml`**
作为仓库根标记，装成 wheel 后没有该文件则退回上三级。`core/async_contracts.py`
的 `contracts/async_tools.json` 同样向上查找。

## 本地运行

```bash
export PYTHONPATH=src          # 或 pip install -e .
python -m scripts.serve_api        # 起 API
python -m scripts.run_worker       # 起 Worker
bash scripts/run_tests.sh          # ruff + mypy + 全部用例（需要 PG）
```
