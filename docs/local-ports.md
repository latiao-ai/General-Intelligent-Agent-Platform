# 本机联调端口

| 服务 | 地址 |
| --- | --- |
| 中台前端 | http://127.0.0.1:8001 |
| 中台后端 | http://127.0.0.1:8002 |
| BeeFTV 前端 | http://127.0.0.1:8003 |
| BeeFTV 后端 | http://127.0.0.1:8004 |

在中台项目目录，用 demo 环境分别启动：

```sh
python -m scripts.serve_api
python -m scripts.serve_frontend
```

两个命令分别在终端运行。8001 页面和画布会话 API 通过 BeeFTV 8004 的 `/api/hub/workbench/` 身份代理访问中台 8002，沿用 BeeFTV 的 HttpOnly 登录 Cookie；中台 Cookie 留在平台服务端。8001 的 `/healthz` 直接转发到 8002。

一键起后端 + Worker（+ 前端代理）：

```sh
python -m scripts.serve_dev --with-frontend
```

端口被占用（上一次没退干净、孤儿进程还占着 8001/8002）时：

```sh
python -m scripts.serve_dev --with-frontend --force       # 结束本项目残留进程（含残留 Worker）后重启
python -m scripts.serve_dev --with-frontend --auto-port   # 不动任何进程，自动换到后一个空闲端口
python -m scripts.serve_dev --with-frontend --force-any   # 连非本项目的占用进程也结束（确认过再用）
```

`--force` 只认命令行里带 `scripts.serve_api` / `serve_frontend` / `serve_dev` / `run_worker`、
且工作目录是本仓库的进程；遇到别人的进程会打印 pid 与命令后拒绝。`--auto-port` 不结束任何
进程，结束时打印实际使用的地址。

先在 8003 登录，再打开 8001。页面会选择最近对话对应的画布，也可用 `?workspace_id=画布ID` 指定画布。侧栏和独立页面使用同一画布会话；独立页面每三秒检查外部新增消息，运行中的任务继续使用事件流。平台允许的前端来源应包含 `http://127.0.0.1:8001`。此页面显示对话、工具执行记录和画布上下文，画布可视化仍在 BeeFTV 中展示。

在 BeeFTV 项目目录运行 `./start.sh`，默认启动 8003 和 8004。
其 `.local/hub-bridge.env` 的 `BEEFTV_HUB_URL` 指向 `http://127.0.0.1:8002`。

中台 `.env` 的 `MCP_SERVERS` 中，BeeFTV 的 `stdio_env.BEEFTV_BASE_URL`
使用 `http://127.0.0.1:8004/api`。沿用既有凭据绑定及可信登录主体转发，
不启用 `--read-only`。`AUTH_ALLOWED_ORIGINS` 应包含 8001、8003 的前端来源。

BeeFTV 条目配置 `idempotency_arg: "operationId"`，让写入操作的编号由中台执行账本生成，避免模型在不同画布或任务中复用固定编号。其 `deterministic_reasons` 包含 `operation_id_reused_with_different_payload`：BeeFTV 明确拒绝此类新请求，没有应用新写入；未知网络结果仍需核实。

健康检查：中台前端 `/healthz` 代理到后端，BeeFTV 前端 `/api/health/live` 代理到后端。
健康检查仅验证连接；业务工具仍按登录主体和权限校验。
