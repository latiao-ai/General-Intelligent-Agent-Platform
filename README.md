# 网页研究与 HTML 报告智能体 Demo

基于 LangGraph + Tavily 的智能体：根据自然语言主题或指定 URL 抓取网页资料，
生成带引用的中文 HTML 报告，并可通过 MCP 部署到远端服务器返回公开访问地址。

## 1. Conda 环境

环境名 `demo`，位于 conda 标准目录，PyCharm 可直接识别：

```bash
conda activate demo          # 解释器：/opt/anaconda3/envs/demo/bin/python
python --version             # Python 3.12.15
```

依赖已按 `requirements.txt` 装好，精确版本见 `requirements.lock.txt`。
换机器时可 `python -m pip install -r requirements.lock.txt`。

### PyCharm 配置

1. `File → Settings → Project → Python Interpreter`
2. 齿轮 → `Add Interpreter` → `Add Local Interpreter`
3. 左侧选 `Conda`，`Conda executable` 填 `/opt/anaconda3/bin/conda`
4. 选 `Use existing environment`，下拉里选 **demo**
   （若列表里没有，点右侧刷新/`Reload environments`，或手动填解释器路径
   `/opt/anaconda3/envs/demo/bin/python`）
5. 确认后，`Run/Debug Configuration` 的 Python interpreter 选 `demo`

命令行运行前先激活：`conda activate demo`

## 2. 配置 `.env`

从 `.env.example` 复制为 `.env` 后填写。关键项：

| 变量 | 说明 |
|---|---|
| `TAVILY_API_KEY` | Tavily 搜索/提取密钥 |
| `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` | 模型配置，需支持 tool calling |
| `LLM_COMPOSE_MODEL` | 整理报告阶段单独用更快模型；留空沿用 `LLM_MODEL` |
| `LLM_PARSE_DISABLE_THINKING` | 解析阶段关闭模型推理，实测约 2 倍提速（见下） |
| `DEPLOY_ENABLED` | 部署总开关；`false` 时只生成本地 HTML |
| `MCP_URL` / `MCP_BEARER_TOKEN` | 远端 MCP 部署服务地址与鉴权 |
| `MCP_TOOL_NAME` | 部署工具名（当前远端为 `deploy_site`） |
| `MCP_EXTRA_ARGS` | 额外入参，如 `{"ttl":"7d"}` 控制站点有效期 |
| `MCP_SERVERS` | **多服务端**：JSON 数组，每项 `{id, transport, url, token, deploy_tool, default}`；设置后不再读上面的单项 `MCP_*` |
| `IDEMPOTENCY_TTL_SECONDS` | 创建任务幂等键的有效期，默认 86400（24 小时） |
| `DATABASE_URL` | 数据库连接串；填 `postgresql://…` 用 PostgreSQL，**留空回落 SQLite** |
| `DB_PATH` | 仅 SQLite 模式下的文件名，默认 `agent.db` |
| `PROFILE_PATH` | 用户画像文件，默认 `profile/USER.md`（见 3.1.2） |

> 改了 `.env` 必须重启进程才生效。
> `LLM_MODEL` 必须与网关实际可用模型名完全一致（`model_not_found` 多为名称不匹配）。

## 3. 运行

```bash
conda activate demo
cd webpage-agent-demo

# 网页界面（推荐）：启动后在浏览器打开 http://127.0.0.1:8010
python -m scripts.serve_api --port 8010
python -m scripts.serve_api --reload        # 开发模式，改代码自动重启

# 命令行模式
python -m app.main '整理 SQLite 的适用场景和限制，生成中文报告'
python -m app.main '整理 SQLite 的适用场景，生成中文报告，然后部署到服务器'
python -m app.main '阅读 https://www.sqlite.org/whentouse.html ，整理适用场景'
```

产物在 `artifacts/<task_id>/`：`index.html`、`report.json`、`sources.json`、
`manifest.json`、`state.json`、`deployment.json`。

## 3.1 界面：过程可见、可停止、可追加要求、阶段实时计时

技术栈是 **FastAPI + SSE + 原生 HTML/JS**（后端 `app/api.py`，前端 `app/web/index.html`）。
选它而不是 Streamlit，是因为后者在实时交互上处处受限：

| | Streamlit | **FastAPI + SSE（当前）** |
|---|---|---|
| 实时推送 | 0.5 秒轮询 | 事件驱动，**到达即推** |
| 计时刷新 | 0.5 秒一跳 | **100ms 平滑累加** |
| 多客户端 | ❌ 单会话 | ✅ 可订阅同一任务 |
| 状态持久化 | ❌ 纯内存 | ✅ PostgreSQL（可回落 SQLite） |
| 改代码生效 | 必须重启进程（模块不热重载） | `--reload` 自动 |

### 阶段耗时实时累加

每个节点进出时由 `app/graph.py` 的包装层发出 `stage_start` / `stage_end` 事件。
前端收到 `stage_start` 就开一行，并按**服务端给的开始时刻**每 100ms 刷新；收到
`stage_end` 冻结为该阶段实际耗时：

```
第 1 轮 ────────────────────────────────
💭 我先帮你查找「SQLite 的适用场景…」相关资料
   🌐 已搜索网页：SQLite 适用场景 优点
🔹 解析需求 · planned                  5.42s
🔹 检索来源 · sources_discovered       8.92s
🔹 抓取正文 · fetched                  0.00s
🔹 整理报告 · 已中断                    7.75s      ← 冻结为实际耗时，不再增长

第 2 轮 ────────────────────────────────
   🌐 已搜索网页：SQLite 限制 不足
🔹 解析需求 · planned                  4.22s
🔹 检索来源 · sources_discovered       8.53s
```

- 阶段耗时**内联在执行过程流里**，每阶段一行、右侧实时累加，结束后定格为该阶段实际耗时
- 标题显示 `本轮 X.XXs（累计 Y.YYs）`
- **新一轮只往后追加，不清空之前任何一轮的记录**（每轮一个分隔头）；只有「新对话」才真正清空
- 任务成功、中断、失败三种结局下，进行中那一行都会被冻结并标注状态（`已中断` / `已失败`），
  不会一直停在"进行中"、耗时一路涨

> 中断场景需要任务中枢**兜底补发**结束事件：节点自己发的 `stage_end` 会写进 LangGraph
> 的流队列，而消费循环此刻已被取消、没人来取，事件就此丢失。前端也有一层兜底。

> 「收到指令 → 开始搜索」这个合成阶段**不在界面上展示**：它 = 解析耗时 + 提交开销
> （实测只差 0.05 秒），在逐步显示的过程流里和「解析需求」完全重复。后端仍会记录它，
> 可从 `/api/tasks/{id}` 返回的 `state.timings` 看到。

用服务端时刻（事件里的 `at`）而不是"前端收到的时刻"来算：事件经网络/轮询到达会晚
几十到几百毫秒，按收到时刻计时会偏短。本机与服务端的时钟差在首个事件时自动校正。

> 节点包装层统一包成 `async` 是有意为之：LangGraph 会把**同步**节点丢进线程池，
> 而 `get_stream_writer()` 依赖 contextvars，跨线程会拿不到 writer、事件静默丢失。

### 实时展示执行过程

节点内部通过 `app/events.py` 的 `emit()` 发出动作事件：

```
💭 我先帮你查找「SQLite 的适用场景和限制」相关资料，整理成概述、适用场景、限制…等章节。
   🌐 已搜索网页：SQLite 适用场景 优点
   🌐 已搜索网页：SQLite 限制 不适合场景
   📚 命中来源：候选 5 个，其中 5 个自带正文
   ✅ 正文就绪：5 个来源，共 73513 字符
   ✍️ 正在整理报告：5 个来源 · 35000 字符 · 模型 deepseek-v4-flash
   📝 已生成 HTML：21680 字节
   🔍 产物校验通过
   ⏸️ 未部署：用户未要求部署，可在界面点击「部署到服务器」
```

执行器用 `stream_mode=['updates', 'custom']` 同时消费节点级进度与细粒度动作。

### 中途停止

点「停止」调用 `POST /api/tasks/{id}/cancel`，取消该任务（实测在整理报告阶段
约 0.5 秒内生效）。

> ⚠️ **停止是"取消"不是"撤销"**。如果停止发生在部署之后，站点已经发布在公网，
> 停止不会回滚。服务端会检测这种副作用并给出提示，例如
> 「注意：停止前部署已完成，站点仍在 http://…，停止不会撤销已发布的内容。」

### 追加要求（修正方向）——运行中可直接发送

**不需要先点停止。** 智能体运行期间输入框照常可用，发送按钮会变成「打断并发送」：
点下去会**自动取消当前任务**，并用同一会话立刻开新一轮，带着上文改方向。

```
user       整理 SQLite 的适用场景，生成中文报告
assistant  （上一轮被用户中断）
user       我只要其中关于限制和不足的部分
           → 模型还原为「SQLite 的局限性与不足（原 SQLite 适用场景报告中的限制与不足部分）」
```

两个细节：

- 中断时会给会话补一条助手回复（「上一轮被用户中断」）。否则下一轮上下文里会出现
  两条连续的用户消息，模型看不出上一轮其实是被打断的。
- 前端会等当前任务的结束提示经原事件流渲染出来后再开新一轮（最多 2 秒），
  这样「已停止…」这类副作用提示不会丢。

会话消息在服务端存在数据库里，但**前端不做自动恢复**：每次打开或刷新页面都是干净的新
会话（否则重启服务后旧对话会自动冒出来）。需要清空当前对话时点「新对话」——运行中点击
会先打断当前任务。历史任务仍保留在数据库里，可经 API 查询。

## 3.1.2 用户画像（跨会话记忆）

借鉴 WorkBuddy 的「文件即记忆」：画像是一份**人可读、可手改、可 git diff 的 Markdown**，
存在 `profile/USER.md`，不是数据库里的 JSON。

```
# User Profile
> Last updated: 2026-10-18T...
> Version: 2

姓名：张三
电话：138xxxx
偏好：回答简洁
```

- **自动记忆**：用户透露个人信息时（"我叫张三，电话是…"），模型在解析阶段提取并写入，
  界面上会显示「🧠 已记住：姓名=张三、电话=138…」
- **每轮注入**：画像会拼进 system prompt 的 `[用户画像]` 段，聊天、检索、生成报告都能用上
- **只记明说的**：不推断、不编造；依赖最新事实而没把握时模型会说"需要联网核实"
- **人可直接改**：打开文件增删一行即可，下次对话生效
- **写入安全**：临时文件 + rename 原子替换，写前自动备份 `USER.md.bak`，带版本号
- **清空**：删掉文件，或让模型"清空画像"

> 画像含电话等敏感信息，已加入 `.gitignore`，不会被提交。

## 3.1.1 按需分流：闲聊直答，研究才检索

**不是所有输入都该去搜网页。** 解析阶段会判断「本轮是否需要联网」，然后分流：

```
parse_request ─┬─ 需要联网 → 检索 → 抓取 → 整理报告 → 渲染 → 部署
               └─ 不需要   → 直接回答（不检索、不生成报告，回答流式逐字显示）
```

| 判定 | 触发条件 |
|---|---|
| **需要联网** | 要求产出报告/整理/对比/调研；依赖实时或外部事实（"最新""今天""多少钱""版本号""新闻"）；用户给了 URL |
| **不需要** | 寒暄（"你好""在吗""谢谢"）；一般性知识问答（"什么是 ACID"）；对上一轮产物的追问 |

**歧义时默认走检索。** 该搜没搜会让用户拿到没有来源的回答，代价比"多搜一次"大。
用户明确说"直接回答/不用搜"会被服从。

实测对比：

| 输入 | 耗时 | 是否检索 |
|---|---|---|
| `你好` | **2.65 秒** | 否，0 个来源 |
| `整理 SQLite 的适用场景，生成中文报告` | 约 100 秒 | 是 |

直答时**没有联网**，因此系统提示词要求：依赖最新事实而没把握时要说明"需要联网核实"，
**不许猜版本号、价格、日期**。想强制检索可以说"上网查一下"。

## 3.2 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 聊天界面 |
| `POST` | `/api/tasks` | 开一轮任务，body `{message, session_id?, idempotency_key?}`；建议带 `Idempotency-Key` 头，重试不会建出第二个任务 |
| `GET` | `/api/tasks/{id}/events` | **SSE 事件流**，实时推送执行过程 |
| `POST` | `/api/tasks/{id}/cancel` | 停止任务 |
| `GET` | `/api/tasks/{id}` | 任务状态（含 `state`、`timings`） |
| `GET` | `/api/tasks/{id}/artifact` | 下载生成的 HTML |
| `POST` | `/api/tasks/{id}/deploy` | 手动部署到远端服务器 |
| `POST` | `/api/runs/{id}/compensate` | 撤销这次运行已完成的有副作用步骤（独立补偿运行） |
| `GET` | `/api/sessions/{id}/messages` | 会话消息（多轮上下文） |

### 事件格式

`text/event-stream`，每帧 `data: {...}`，事件类型放在 JSON 的 `type` 字段：

```jsonc
{"type":"task","task_id":"976ec2ce0099","artifact_id":"765beaa5..."}
{"type":"stage_start","stage":"compose_report","label":"整理报告","at":1792126225.4}
{"type":"thought","text":"我先帮你查找「SQLite 的适用场景」相关资料…"}
{"type":"action","icon":"🌐","label":"已搜索网页","detail":"SQLite 适用场景 应用场景"}
{"type":"stage_end","stage":"compose_report","label":"整理报告","seconds":62.31,"status":"composed"}
{"type":"result","public_url":"http://…/sites/xxx/"}              // 手动部署后
{"type":"status","status":"local_ready","detail":"报告已生成…"}   // 终态，带助手回执
{"type":"end","status":"local_ready"}
```

晚连上来的客户端会**先补发历史事件**再续推实时事件（订阅与快照在同一同步块内完成，
不漏也不重）。整理报告阶段可能静默上百秒，因此服务端每 15 秒发一次 `: keepalive`
心跳，避免连接被中间层按空闲超时切断。

### 多轮示例

```bash
# 第一轮
SID=$(curl -s -X POST localhost:8010/api/tasks -H 'Content-Type: application/json' \
  -d '{"message":"整理 SQLite 的适用场景，生成中文报告"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

# 第二轮：指代型追加要求，不用重复主题
curl -s -X POST localhost:8010/api/tasks -H 'Content-Type: application/json' \
  -d "{\"message\":\"只要其中关于限制和不足的部分\",\"session_id\":\"$SID\"}"
# → 模型还原为 topic「SQLite 的限制与不足」，查询「SQLite 限制 不足 缺点」
```

## 3.3 数据持久化

会话、任务状态、计划步骤、动作账本与事件日志都落在数据库里，服务重启后不丢。

**默认后端是 PostgreSQL**：`.env` 里给 `DATABASE_URL` 一个连接串就走 PG；
留空则退回本地 SQLite（`agent.db`，用 `DB_PATH` 改路径）。两者实现同一套仓储接口
（`app/persistence.py` 的 `Repository`），业务代码不感知用的是哪个。

```ini
DATABASE_URL=postgresql://latiao@127.0.0.1:5432/agent_platform
```

从 SQLite 迁到 PG：

```bash
python -m scripts.migrate_to_postgres --dsn "postgresql://user@127.0.0.1:5432/agent_platform"
```

脚本会逐表搬数据并**校验行数一致**，完成后复位 `messages.id` 的序列。
本地实测迁移了 21 会话 / 139 消息 / 71 任务 / 1069 事件，行数校验通过。

### 表一览（16 张）

| 分组 | 表 | 内容 |
|---|---|---|
| 会话 | `sessions` / `messages` | 多轮上下文的主体与消息 |
| 记忆 | `session_memory` / `session_summaries` | 长会话滚动摘要与关键事实 |
| 任务 | `tasks` / `tasks_meta` | 任务状态、产物 ID、完整 `state`（含 timings） |
| 运行 | `runs` / `plans` / `steps` | 每次执行尝试、计划版本、步骤状态 |
| 账本 | `actions` / `action_attempts` | 动作身份（幂等键）与每一次真实调用 |
| 产物 | `artifacts` | 产物引用：kind / 路径 / 大小 / sha256 |
| 记忆库 | `memories` / `memory_tombstones` | 作用域化长期记忆与遗忘墓碑 |
| 后台 | `background_jobs` | outbox 派生的异步任务（带去重键） |
| 事件 | `events` | 每个任务的完整事件日志（带任务内自增 `seq`） |

### 内存与磁盘的分工

| | 存哪 | 为什么 |
|---|---|---|
| 事件日志 | **数据库** | 权威记录，重启后仍能补发完整过程 |
| 会话消息 | **数据库** | 多轮上下文不能丢 |
| 任务/运行状态 | **数据库** | 重启后仍可查历史，也是断点续跑的依据 |
| 动作账本 | **数据库** | 判断"这次调用到底发生过没有"，崩溃恢复靠它 |
| 订阅者队列 | 内存 | 连接级状态，不可能持久化 |
| `asyncio.Task` | 内存 | 活着的进程状态，不可能持久化 |

因此设计是**先落库再投递**：`publish()` 先把事件写入数据库拿到序号，再推给内存订阅者。
正在跑的客户端拿到零延迟事件；晚连上来的、甚至服务重启后再连的，从库里补发——两者用
`seq` 去重。

### 重启后的恢复

启动时不会简单地把遗留的 `running` 一律标成中断，而是**按动作账本对账**：

- 已经成功的动作 → 复用回执，不重复调用远端；
- 已发出但没拿到回执（`in_flight`）→ 标记 `unknown`，要求核实，**绝不盲目重发**；
- 从未发出的动作（`prepared`）→ 可以安全重试。

运行状态置为 `reconciling`，可通过 `GET /api/recovery` 查看统计。
详见 `docs/platform-acceptance.md`。

### 实现取舍

- **PostgreSQL**：`psycopg` 3.x，单连接 + 线程锁串行化（与 SQLite 实现同构）。
  本地单 Worker 够用；并发上来后应换连接池——这一步尚未做。
- **SQLite**：标准库 `sqlite3`，WAL 模式，零新增依赖，适合单机快速起步。
- 仓储接口见 `app/persistence.py`，`check_repository()` 可在换实现前校验方法齐备。

### 验证持久化

```bash
# 1) 起服务并建任务，记下返回的 task_id
python -m scripts.serve_api --port 8010
curl -X POST localhost:8010/api/tasks -H 'Content-Type: application/json' \
  -d '{"message":"整理 SQLite 的适用场景，生成中文报告"}'

# 2) Ctrl+C 停服，再重新启动，然后：
curl localhost:8010/api/runs/<run_id>      # 状态与步骤都在
curl -N localhost:8010/api/tasks/<task_id>/events   # 完整事件流从库里补发
curl localhost:8010/api/recovery           # 重启对账统计
```

用 PostgreSQL 时可以直接核库：

```bash
psql -d agent_platform -c "SELECT (SELECT count(*) FROM sessions) 会话,
  (SELECT count(*) FROM messages) 消息, (SELECT count(*) FROM runs) 运行;"
```

### 已知边界

- **实时通知是进程内的**。事件先落库、再推给本进程的订阅者；别的实例写入的事件要靠
  游标补发（`GET /api/tasks/{id}/events?after=<seq>` 或 `Last-Event-ID`）才能拿到，
  跨实例实时推送需要消息总线。
- **执行只有一条路径**：就地执行（API 进程）与 Worker 都走 `claim_run` 并写租约，
  同一 run 不会被领两次。领取在 SQLite 上是单条 `UPDATE … RETURNING`，在
  PostgreSQL 上是 `… FOR UPDATE SKIP LOCKED`。执行期间由 `Hub._guard` 续租，
  租约过期才会被判定为"执行者已死"并接手。
- **跨进程取消是协作式的**：取消请求写 `runs.cancel_requested_at`，持有租约的执行者
  在 2 秒轮询里发现后自行停止；进程已经死了的 run 会由对账按账本处理（有 in_flight
  动作标 `unknown` 等人工核实，没有就回到 `queued` 续跑）。
- **PostgresStore 用单连接 + 线程锁**串行化，没有连接池。并发量上来后是瓶颈。
- **SQLite 模式仍是单机单写**：多副本各写各的会冲突，只适合本地开发。
- **幂等只覆盖"创建任务"**。事件追加、部署、补偿各有更精确的机制（事件序号、
  账本 action、`compensated_at`），不共用这把钥匙。键按 `主体+接口+键` 作用域化，
  默认 24 小时过期（`IDEMPOTENCY_TTL_SECONDS`）。
- **补偿依赖"有撤销工具"**。计划里给某一步写了 `compensate`，失败时才会逆序执行；
  服务端没有撤销工具时这一步就是空的，系统不会假装撤回过。结果未定（unknown）
  的动作**不自动补偿**——那等于猜着再发一次写请求。
- **多 MCP 服务端已支持，但只在本机用假服务端验证过**。`MCP_SERVERS` 配多个后，
  工具名变成 `mcp__<server_id>__<tool>`；只有一个服务端时保持 `mcp__<tool>`，
  旧计划仍可用（`find_tool` 会按尾部唯一匹配回退）。

## 4. 部署行为

```
DEPLOY_ENABLED=false            → 不部署（界面按钮置灰）
DEPLOY_ENABLED=true + 用户要求  → 生成后自动部署
DEPLOY_ENABLED=true + 未要求    → 停在本地产物，界面出现「部署到服务器」按钮
```

意图由模型判定（`Plan.auto_deploy`），**不使用关键词匹配**：
"对比数据库的部署方式"属于研究内容，不会被误判为要求部署。

只重试部署（不重新生成）：

```bash
python -m scripts.retry_deploy <task_id> --server-state-checked
```

查看远端全部工具：

```bash
python -m scripts.list_mcp_tools
```

## 5. 耗时统计

两处都有：执行器把每个节点的耗时写入 `state.json` 的 `timings` 与 `elapsed_seconds`
（CLI 打印汇总表），节点包装层发出 `stage_start` / `stage_end` 事件供界面实时累加。

实测参考（正文预算 35000 字符）：

| 整理报告模型 | 整理报告 | 任务总耗时 | 产出规模 |
|---|---|---|---|
| `deepseek-v4-pro` | 122.6s | 137.0s | 5 章节 |
| `deepseek-v4-flash` | 90.6s | 103.4s | 5 章节 |
| `deepseek-v4-1-flash` | 84.3 ~ 103.4s | 104.6 ~ 118.1s | 8 章节 |

> 上表是少量采样的参考值，**方差很大**（同一模型两次能差 20 秒，受网络与输出长度影响），
> 不要当成基准。`deepseek-v4-1-flash` 两次采样分别产出 11929 / 9662 个 output token，
> 内容明显更长，因此耗时不能直接与其它模型横向对比。

**模型名必须与网关完全一致**。实测 `deepseek-v4-1-flash`（连字符）可用，
而 `deepseek-v4.1-flash`（点号）报 `model_not_found`。换模型前先单独验证一次
（见 §6 的验证方法），别直接改完就跑整条流程。

**整理报告阶段占 85%～90% 耗时**，是唯一值得优化的环节（检索约 9s，渲染/校验/部署均 <0.1s）。
换用 `deepseek-v4-flash` 后该阶段快约 26%。若还需压缩，可调小
`MAX_TOTAL_CHARS` 减少送入模型的正文量。

> 结构化输出偶发失败（快模型产出不符合 schema 的结构）会自动重试一次，
> 失败尝试的 token 同样计入 `state.json` 的 usage。

### 解析阶段为什么能关推理，整理阶段为什么不能

实测结果（`deepseek-v4-1-flash`）：

| 阶段 | 开推理 | 关推理 | 结论 |
|---|---|---|---|
| 解析需求 | 4.4s（多轮场景） | **2.3s** | ✅ 关掉，准确率不变（auto_deploy 判断 5/5） |
| 整理报告 | 62s，结构化输出正常 | **失败** | ❌ 必须保留推理 |

整理阶段关掉推理后，模型会生成 6000+ 个 output token，**但既没有 `tool_call` 也没有
`content`**——无法产出合法的结构化报告，整条流程会失败。

**耗时几乎全在推理 token 的生成上，与输入长度无关。** 实测把多轮历史从 536 字符砍到
203 字符，耗时几乎没变；关掉推理则稳定减半。所以不要花力气去压缩提示词。

可用参数也有讲究：

| 参数 | 实际效果 |
|---|---|
| `extra_body={'thinking': {'type': 'disabled'}}` | ✅ 真正关闭（推理 token = 0） |
| `reasoning_effort='none'` | ✅ 真正关闭 |
| `reasoning_effort='low'` / `'minimal'` | ❌ **被网关忽略**（推理 token 不变） |

### 但真正的瓶颈是网关抖动

同一个请求、同样的 payload，实测出现过 **4.36s 和 104.96s** 两种结果。这不是提示词或
参数能解决的——是网关侧的排队/卡顿。所以：

- 解析阶段的 2 倍提速是**稳定收益**，但绝对值只省 2 秒左右
- 总耗时（85%~90% 在整理报告）**不受解析优化影响**
- 遇到离奇的慢，先怀疑网关，不要先去改提示词

### 网络提示

本机若使用代理（macOS 系统代理），httpx 会自动走该代理；**代理进程停止时
所有模型与 Tavily 调用都会报 `APIConnectionError` / `ConnectError`**。
自行验证时注意 `curl` 不读系统代理，会出现"curl 通但 Python 不通"的现象。

## 6. 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：URL 安全校验、引用存在性、表格列数、HTML 自动转义、补搜上限、无来源禁止部署、
部署意图门控、结构化输出重试、MCP 参数组装（base64）、阶段事件与实时计时、
SQLite 存储与重启恢复、任务中枢、HTTP 接口与 SSE 事件流、助手回执文案。
