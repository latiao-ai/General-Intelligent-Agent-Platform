# 独立部署与运维

> 容器部署文件（Dockerfile、compose.yaml、.dockerignore、deploy/init-db.sh）
> 已移除，待重新编写。当前按下方「运行方式」直接以进程方式部署。

当前代码提供生产启动检查、迁移作业、共享限流与租户日预算。仓库内的 `.env`、`secrets/`、`profile/`、`artifacts/` 是本机运行数据，不属于交付物。现有密钥没有自动作废；若曾外发完整工作目录，应在密钥所属平台轮换，不能依靠删除本地文件撤销远端权限。

## 生成交付包

```sh
python -m scripts.package_release
```

产出 `dist/webpage-agent.tar.gz`，内部包含逐文件 `SHA256SUMS`。打包使用固定目录/文件允许列表，排除运行数据、部署环境文件、实际接入登记、缓存及符号链接，并拦截私钥、常见 API key 和当前本地已知密钥的复制品。新增的交付文件须加入允许列表。不要用 `tar .` 或 Finder 压缩整个工作目录。

## 配置与启动

1. 从发布包解压后，复制 `deploy/production.env.example` 为 `deploy/production.env`，权限设为 600；填入自己的模型、检索密钥、HTTPS 地址、模型名称、平台来源及计价上界。
2. 创建 `deploy/config/`。按现有平台接入文档生成 `auth-connections.json`，只放平台公钥和信任登记，不放平台私钥。复制 `deploy/mcp-servers.json.example` 为 `deploy/config/mcp-servers.json`。JSON 字符串中的 `${PUBLISH_MCP_URL}` / `${PUBLISH_MCP_TOKEN}` 由进程环境展开，缺变量或配置无效时拒绝使用该登记表，不回退旧 token。
3. 在部署终端分别设置数据库管理员 `POSTGRES_PASSWORD` 和应用账号 `AGENT_DB_PASSWORD`。初始化脚本建立非超级用户的 agent 账号，应用不使用 postgres 管理员。应用密码直接插入数据库 URL，使用随机十六进制密码（例如 `openssl rand -hex 32` 的输出）；使用包含 URL 特殊字符的现有密码时，改为自行设置经过 URL 编码的完整 `DATABASE_URL`。不要将生产密码提交到仓库。
4. 先运行迁移，再启动 API 与 Worker（命令见下节）。API 仅绑定宿主机 `127.0.0.1:8000`。
5. 按 `deploy/nginx.conf.example` 配置自己的域名与证书，将公网 HTTPS 转发给 API。模板关闭 SSE 缓冲。运维平台访问 `/healthz` 检查应用与数据库就绪状态。

运行方式：Python 3.12，先安装 `requirements.lock.txt`，从部署系统注入环境变量，再依次运行：

```sh
python -m scripts.migrate
python -m scripts.serve_api --host 127.0.0.1 --port 8000
# 独立终端/服务进程
python -m scripts.run_worker
```

必须在进程环境显式设置 `APP_ENV=production`；生产进程不读取项目根目录 `.env`。未知 APP_ENV 值会报错。生产模式要求票据认证、Secure Cookie、关闭开发兼容和历史数据共享、开启限流与租户预算、禁用启动迁移，且必须配置有效公钥、HTTPS 来源、模型与检索配置及正数计价。

生产 MCP 使用 HTTPS。已有的公网 HTTP MCP 必须由该服务端配置 TLS 或由部署方建立加密隧道；不能只给 URL 改成 https 就认定远端支持 TLS。显式 `MCP_ALLOW_LOOPBACK_HTTP=true` 仅允许当前进程网络命名空间的 loopback HTTP 隧道入口，不允许远端 HTTP。请求发送时也检查 URL，阻止重定向或 SSE 消息端点降级到远端明文 HTTP。

本机 stdio MCP 不随发布包分发。需要视频等能力时，在运行环境自行安装对应依赖并使用可用命令，或改接远端 HTTPS MCP。基础配置只示例通用发布服务，不包含开发者个人路径。

## 迁移与升级

`python -m scripts.migrate` 使用数据库事务级 advisory lock 串行升级。第一版将既有幂等 DDL 作为基线应用一次，然后按顺序执行新的版本；DDL 与版本登记同事务提交，失败回滚。当前版本为 `v3_run_trace`，合计 38 张表。以后改表必须新增迁移，不能只修改基线。

开发模式默认自动迁移；生产 `DB_AUTO_MIGRATE=false`，API/Worker 启动仅检查版本，旧库拒绝启动并提示运行迁移命令。生产数据库应用账号仍需业务表的 DML 权限；条件允许时，迁移作业使用单独的 DDL 账号。升级前备份数据库和共享卷。迁移回滚不能自动撤回已发生的外部业务操作。

## 限流和费用控制

- IP 请求限流：`API_RATE_LIMIT_PER_MINUTE`，默认 120；登录相关接口单独 `AUTH_RATE_LIMIT_PER_MINUTE`，默认 10。
- 已认证主体限流：企业 + 用户，`USER_RATE_LIMIT_PER_MINUTE`，默认 60。所有 API 副本共用数据库原子计数，超额返回 429 和 `Retry-After`；限流存储故障时拒绝处理请求。
- 采用固定分钟窗口，窗口边界可以出现两倍短时突发。客户端真实 IP 取 ASGI peer，只有在代理来源可信且应用服务器正确配置 proxy headers 时才能信任转发 IP。反向代理后可能把代理地址当作共享 IP，限流会更保守；用户级限流仍独立有效。不要向公网开放任意来源可信的 `--forwarded-allow-ips=*`。
- 每个企业独立的 UTC 日额度：`TENANT_DAILY_PROVIDER_REQUESTS` 与 `TENANT_DAILY_COST_MICROS`，跨新任务、重试、进程和重启累计。后台摘要、LLM 和内置搜索/抓取都计入。无可信企业计费上下文时不发送请求。
- `1,000,000 micros = 1` 个配置货币单位。输入/输出单价按每百万 token 的 micros 设置；搜索/抓取按每次请求的 micros 设置。必须填写适用于所有可选模型的保守计价上界，不能把示例数值当成供应商报价。第三方 MCP 自己的费用需通过其工具政策和外部账单管理，这里没有自动探测其价格。
- LLM 请求发送前，按最大上下文输入 + 最大输出预留费用；正常完成且提供完整 token usage 时按用量结算，多退少补。失败、超时、取消、进程崩溃或缺 usage 保留保守费用，不能通过重启/失败获得免费额度。异常供应商若突破声明的 token 上限，实际结算仍会如实记账，并阻断后续超额调用。
- `provider_charges` 保留请求、run、模型、trace、预留/结算金额与 token 用量。`reserved` 表示尚未完成结算或进程中断，不能擅自退款。预算并非供应商发票，人工修正需先核对供应商账单。

## 多副本与存储

API 和 Worker 都使用 PostgreSQL 领取运行与租约 fencing。每个进程的 `MAX_CONCURRENT_RUNS`、`DB_POOL_SIZE` 都可配置，但不按负载自动变化；集群容量由副本数和每进程上限控制。数据库总连接预算还需加上每个 API 的一条 LISTEN 连接、Worker 及迁移/运维连接。不要将每进程上限误当成集群总上限。

所有事件，包括 draft 与 answer_delta，先持久化后 NOTIFY；各副本按 seq 读取，通知失联时约 15 秒补读。跨进程结束事件会关闭 SSE；被动副本查询任务时重新读取数据库，不长期展示旧内存状态。客户端仍应在断线后携带 `Last-Event-ID` 或 `after`。逐条增量落库增加数据库写入量，实际容量需按自己的模型吞吐压测。

多副本部署须将 `ARTIFACT_DIR` 和 `PROFILE_PATH` 指向各副本可见的共享存储，并保持相同绝对挂载路径（旧产物仍可能包含绝对引用）。当前没有 S3 后端，不应把各主机各自的本地目录当成共享存储。

## 清理和监控

API 启动后台维护任务，按 `MAINTENANCE_INTERVAL_SECONDS` 执行，数据库锁保证只有一个副本做同轮清理。也可运行 `python -m scripts.maintenance`。只运行 Worker 时需由运维单独定时执行该命令。

- `RETENTION_EVENTS_DAYS` 清理到期且所有运行均最终成功/失败/取消的任务事件，保留最后一条，保证后续事件 seq 不倒退。
- `RETENTION_ARTIFACTS_DAYS` 清理同类已结束任务的过期产物与已登记工具结果，保留暂停、等待输入、等待批准、结果未知和需要核实的工作。到期文件清理后下载返回 404；台账、对话和运行历史保留。
- 任一保留天数设为 0 时关闭对应清理。`/api/ops/gc-results` 仍只清理超过宽限期、无 manifest 的暂存目录。维护不跟随 symlink 删除外部目录。
- 任务目录（`ARTIFACT_DIR/<task_id>` 下的 `state.json`、`results/`、产出文件）没有登记表可反查：库重建或任务被删之后，这些目录不会被保留策略扫到，会一直留在磁盘上。维护另外按「`task_id` 是否还在 `tasks` 表里」判断孤儿，删除超过 `RESULT_GC_GRACE_SECONDS`（默认 86400 秒）的孤儿目录；该值设为 0 关闭这段清理。只处理名字是 32 位十六进制的普通目录，符号链接既不跟随也不删除。
- 限流窗口、过期登录会话和幂等键自动清理。费用台账没有自动删除，长期保留/归档由组织审计策略决定。

`/api/ops/metrics` 保留 operator 权限。`METRICS_FORMAT=prometheus` 返回 Prometheus 文本，否则 JSON；包括队列、未知动作、批准积压、provider 请求/费用/token/未结算数。费用值为 micros，采集器应使用受控 operator 会话，不提供匿名指标入口。当前指标是数据库聚合快照（gauge），并非永久只增计数器。

生产日志使用 JSON，接受有效的 W3C traceparent，响应返回 traceparent 和 X-Request-ID。请求日志使用路由模板，不记录正文/查询参数；请求 trace ID 持久保存在 runs 中，Worker 领取后沿用；运行与 provider 各有 span，台账有可关联的 trace/run/charge 标识。当前提供日志关联与请求/provider 时间记录，没有安装 OpenTelemetry Collector 或外部追踪后台。

## 本次验证

详细结果见 [上线加固验证记录](hardening-validation.md)。自动测试使用独立的 PostgreSQL 测试库、临时目录和假模型；生产模式双 API 进程测试使用临时生成的签名密钥，验证共享登录、Secure Cookie、跨进程回答增量、终态关闭与游标重放。不会使用本机真实模型密钥进行扣费测试。
