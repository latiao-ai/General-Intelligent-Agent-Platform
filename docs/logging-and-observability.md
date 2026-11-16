# 日志与运行态

控制台「日志」页看什么、日志写到哪里、怎么把一次请求从 API 串到 Worker。
实现：`src/webpage_agent/observe/logging_setup.py`、`observe/log_buffer.py`、
`api/console_api.py` 的 `/api/console/logs*`。

## 三个出口

| 出口 | 内容 | 用途 |
|---|---|---|
| stdout | 全部（受 `LOG_LEVEL` 限制） | 终端 / 容器日志；进程被 kill 后唯一留存的东西 |
| `artifacts/logs/api.log`、`worker.log` | 同上，10MB 轮转 5 份 | 排障取证；重启后还能翻 |
| 进程内环形缓冲（默认 2000 条） | 最近 N 条，结构化 | 控制台实时流、QPS/错误率/P95 |

三处共用同一个 Formatter：开发是

```
2026-11-13 10:32:48,170 WARNING webpage_agent.api.console_api trace=86abcd12 日志级别已调整 level=WARNING
```

生产（`APP_ENV=production`）是 JSON，字段 `time/level/logger/message/trace_id/span_id/run_id`。

## trace 号

`guard_origin` 中间件每个请求生成一个：

- 响应头 `X-Request-ID` 是**完整** 32 位，`traceparent` 符合 W3C；
- 日志里只打**前 8 位**（`trace=86abcd12`）—— 一屏放三个 32 位串就看不见消息了；
- 上游带 `traceparent` 时沿用它的 trace id，所以跨系统能对上。

排障顺序：用户报错 → 拿 `X-Request-ID` 前 8 位 → 控制台日志页搜这 8 位 →
看到同一请求的 `http_span`、异常栈、以及它触发的下游调用。

## 环境变量

| 键 | 默认 | 说明 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | 启动级别；运行期可在控制台临时改，重启回到这里 |
| `LOG_DIR` | `$ARTIFACT_DIR/logs` | 日志目录；多副本要指到各副本都能写的共享路径 |
| `LOG_TO_FILE` | 开（`APP_ENV=test`、pytest 下自动关） | 设 `0` 只写 stdout |
| `LOG_BUFFER_SIZE` | `2000` | 环形缓冲条数；调小会翻不到"刚才那条" |

`httpx` 的 INFO 被压到 WARNING：它每个请求打一行，没有排障价值，
真正的请求证据是 `http_span`。

## 控制台怎么用

`/admin` → 「日志」标签（只有 `platform_admin` 能看到，见下）。

- **来源**：`后端进程` 走 SSE 实时推送；`Worker` 是另一个进程，读 `worker.log`
  尾部（5 秒一轮）。
- **级别 / 关键字**：过滤在服务端做（`level=` 是"及以上"，`q=` 匹配消息、
  logger、trace），DEBUG 全开时不会把整包日志推到浏览器再筛。
- **暂停滚动**：只是不往页面上塞，连接不断、缓冲区照收，恢复时一次性补上。
- **运行级别**：改根 logger 的级别，立刻生效；**重启失效**是有意的 ——
  "临时调到 DEBUG 忘了调回来"不该变成持久的线上配置。
- 顶部四张卡来自 `GET /api/console/logs/status`：uptime、窗口 QPS、
  5xx 率、P95，以及慢请求按路由聚合。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/console/logs?source=api\|worker&level=&q=&limit=` | 最近 N 条，最新在前 |
| GET | `/api/console/logs/status` | 进程、缓冲、请求指标、日志文件清单 |
| GET | `/api/console/logs/stream?level=&q=` | SSE，15 秒心跳 |
| POST | `/api/console/logs/level` | `{"level":"DEBUG"}`，查 Origin |

四个都要求 `platform_admin`（`require_all`）。日志里有 `tenant_id`、`user_id`、
连接器名，`operator`（本企业运维）看到别家企业的日志等于越权，
所以这里不套用 `/users` 那套"租户内可见"的规则。

## 已知边界

- **只有 API 进程的日志能实时推**。API 与 Worker 是两个进程，各自一份缓冲；
  要看 Worker 就得读文件。真要一份合并的流，得上 Redis 或 Loki，
  对一个自托管中台来说不划算。
- **SSE 不受 `API_RATE_LIMIT_ENABLED` 之外的背压控制**：一个浏览器最多一条连接，
  但有人用脚本开一百条也能开——它们只占内存队列（上限 500 条），
  不会拖住业务请求。
- **文件里的 trace 只在前 8 位**。要完整值看响应头或生产环境的 JSON 日志。
- 控制台改级别**不写审计表**：它只改内存、重启即失效，且会在日志里留一条
  WARNING（带 actor），翻日志就能看到谁改的。
