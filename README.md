# 通用智能体执行中台

按用户目标生成计划、从已接入的能力里选工具、逐步执行并记录每一次外部调用的中台。
执行内核与业务解耦：**网页检索、资料转 HTML、发布到 MCP 服务器**只是当前接入的
一个业务能力包，换成工单/文件/数据库查询只需增加能力包，不动内核。

当前能力面（有意保持最小）：

| 工具 | 作用 | 来源 | 身份（tool_key） |
|---|---|---|---|
| `web_search` | 检索网页 | DeepSeek 官方 Search API | `local:web:search` |
| `fetch_pages` | 读取**本轮输入里给出的 URL** 正文（Markdown） | DeepSeek 官方 Fetch API | `local:web:fetch` |
| `render_sources_html` | 把资料原样排版成 HTML，不调用模型 | 本地能力包 | `local:export:sources_html` |
| `answer_directly` | 用模型自身知识直接回答，不检索 | 本地能力包 | `local:answers:direct` |
| `answer_with_sources` | 基于已抓到的正文作答并附来源 | 本地能力包 | `local:answers:with_sources` |
| `deploy_site` | 发布产物到服务器 | **MCP 服务端**（带平台适配器） | `mcp:<server>:<tool>` |
| `mcp__*` | 服务端动态发现的工具（含查询/删除/更新站点） | MCP 服务端 | `mcp:<server>:<tool>` |

只有前五个是"内置"工具：不需要外部服务、身份以 `local:` 开头。`deploy_site`
**不是内置工具**——它就是服务端声明的那个部署工具，身份是 `mcp:<server>:<tool>`，
和 `mcp__*` 走同一条调用通道（同一份账本记录、同样进 `mcp_calls`、同样的
结果未定判定）。平台只为它多挂了一个适配器（调用前的开关/产物闸门 + 调用后的
回执归一，见 `app/capabilities/publishing.py`）。

> 结构化报告生成（compose/render 模板）已按需移除，内核仍保留通用的计划、
> 账本、恢复等机制。

选哪个工具由规划器按"需要什么程度的能力"判断，而不是一律去检索：

| 情况 | 走哪条 |
|---|---|
| 闲聊、概念解释、原理、常识（模型自己有把握） | `answer_directly`（不检索） |
| 需要外部事实，但只想在聊天里问个明白 | `web_search → answer_with_sources` |
| 要产出一个 HTML 文件 | `web_search → render_sources_html` |
| 本轮输入里给了 URL，要读这个网页 | `fetch_pages(urls=[用户给出的链接]) → answer_with_sources / render_sources_html` |
| 还要发布上线 | 再加 `deploy_site` |

`fetch_pages` 的调用时机只有一条：**用户输入里出现了 URL**。检索结果自带的正文
就是本轮资料（官方 Search 用 `need_content=True` 拉正文），不需要再逐条抓一遍；
输入里没有 URL 时它不该出现在计划里，硬传别的链接会被工具直接拒绝。

"没有合适工具"不等于"直接编一个回答"：查实时数据但手里没对应工具时，
应当说明无法确认或请用户提供数据，而不是拿不相关的工具硬凑。

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
| `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` | 模型配置，需支持 tool calling |
| `LLM_PARSE_DISABLE_THINKING` | 解析阶段关闭模型推理，实测约 2 倍提速（见下） |
| `DEPLOY_ENABLED` | 部署总开关；`false` 时只生成本地 HTML |
| `MCP_URL` / `MCP_BEARER_TOKEN` | 远端 MCP 部署服务地址与鉴权 |
| `MCP_TOOL_NAME` | 部署工具名（当前远端为 `deploy_site`） |
| `MCP_EXTRA_ARGS` | 额外入参，如 `{"ttl":"7d"}` 控制站点有效期 |
| `MCP_SERVERS` | **多服务端**：JSON 数组，每项 `{id, transport, url, token, deploy_tool, default, capability_tool, tool_name_arg}`；设置后不再读上面的单项 `MCP_*` |
| `capability_tool` | 服务端的"能力清单"工具名（如 `list_capabilities`）。有些服务端把动作藏在一个通用 `run_tool` 后面，配上它才能在规划前读到**真实内部工具名**，并在调用前校验 |
| `tool_name_arg` | 那个通用调用工具里承载内部工具名的参数名（如 `name`） |
| `IDEMPOTENCY_TTL_SECONDS` | 创建任务幂等键的有效期，默认 86400（24 小时） |
| `DATABASE_URL` | **必填**，PostgreSQL 连接串（`postgresql://…`）。SQLite 实现已删除，留空直接报错 |
| `DB_POOL_SIZE` | PostgreSQL 连接池上限，默认 4（别超过服务端 `max_connections` ÷ 副本数） |
| `DB_POOL_TIMEOUT` | 从池里借连接的等待上限（秒），默认 10；等不到就报错，不会无限期挂住请求 |
| `PROFILE_PATH` | 用户画像文件，默认 `profile/USER.md`（见 3.1.2） |

> 改了 `.env` 必须重启进程才生效。
> `LLM_MODEL` 必须与网关实际可用模型名完全一致（`model_not_found` 多为名称不匹配）。

### 接入本机 OpenMontage

客户端已支持 `stdio`：由客户端启动 OpenMontage 子进程，通过标准输入输出通信。
在 `.env` 的 `MCP_SERVERS` 数组中追加以下对象；原有发布服务条目和默认服务设置继续保留：

```json
{
  "id": "openmontage",
  "title": "OpenMontage 本地视频制作",
  "transport": "stdio",
  "stdio_command": "/opt/anaconda3/bin/conda",
  "stdio_args": [
    "run", "--no-capture-output", "-n", "openmontage",
    "python", "-m", "openmontage_mcp.server",
    "--project-dir", "/Users/latiao/Desktop/demo demo/webpage-agent-demo/openmontage-zh-mcp"
  ]
}
```

`.env` 中 `MCP_SERVERS` 的实际值是完整 JSON 数组，须放在同一行。
`conda run` 让 Server 使用独立的 `openmontage` 环境；客户端继续使用 `demo` 环境。
`--no-capture-output` 保证 MCP 消息直接通过输入输出传递。换机器时修改 Conda 和项目路径。
无需在另一个终端手动启动 Server。更新配置后重启客户端，然后执行
`python -m scripts.list_mcp_tools`，应能在 `openmontage` 下发现 9 个业务工具。
多个服务端同时配置时，模型看到的工具名如 `mcp__openmontage__list_capabilities`。

当前连接方式适合工具发现与同步调用。客户端每次调用结束都会关闭 stdio 子进程，
而 OpenMontage 的异步任务保存在进程内存中；正式使用后台视频任务与跨调用轮询前，
需将客户端改为复用持续存活的会话。

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
🔹 资料转 HTML · 已中断                  0.02s      ← 冻结为实际耗时，不再增长

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
   📄 已把资料导出为 HTML：5 份来源 · 148229 字节
   📝 已生成 HTML：21680 字节
   🔍 产物校验通过
   ⏸️ 未部署：用户未要求部署，可在界面点击「部署到服务器」
```

执行器用 `stream_mode=['updates', 'custom']` 同时消费节点级进度与细粒度动作。

### 中途停止

点「停止」调用 `POST /api/tasks/{id}/cancel`，取消该任务（实测在检索阶段
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

## 3.1.1 闲聊不占规划

寒暄、确认这类消息在进图之前直接给固定应答，不产生模型调用、不占计划步骤。
除此之外的所有请求都走「检索 → 抓正文 → 转网页 →（可选）发布」。

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
{"type":"stage_start","stage":"render_sources_html","label":"资料转 HTML","at":1792126225.4}
{"type":"thought","text":"我先帮你查找「SQLite 的适用场景」相关资料…"}
{"type":"action","icon":"🌐","label":"已搜索网页","detail":"SQLite 适用场景 应用场景"}
{"type":"stage_end","stage":"render_sources_html","label":"资料转 HTML","seconds":0.02,"status":"rendered"}
{"type":"result","public_url":"http://…/sites/xxx/"}              // 手动部署后
{"type":"status","status":"local_ready","run_status":"succeeded",
 "outcome":{"kind":"artifact","produced":[…],"effects":[…]},
 "detail":"页面已生成…"}
{"type":"end","status":"local_ready","run_status":"succeeded","outcome":{…}}
```

`status` 是**展示标签**（`answered` / `local_ready` / `deployed` / `failed` …），
给人和前端看；`run_status` 是状态机里的**通用**值（`succeeded` / `needs_reconcile`
/ `failed` …）。判断流程用 `run_status`，别拿 `status` 做分支——理由见下一节。

晚连上来的客户端会**先补发历史事件**再续推实时事件（订阅与快照在同一同步块内完成，
不漏也不重）。整理/检索阶段可能静默较久，因此服务端每 15 秒发一次 `{"type":"ping"}` 心跳
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

- **PostgreSQL**：`psycopg` 3.x **异步**驱动 + `AsyncConnectionPool` 连接池。
  每次调用 `async with store._lease()` 借一条连接（正常提交 / 异常回滚 / 一定
  归还），全链路 `await`——等数据库的时候事件循环可以继续跑别的协程（SSE 推送、
  其他任务）。池大小与等待超时可配（`DB_POOL_SIZE` / `DB_POOL_TIMEOUT`），
  `GET /api/health` 里的 `db_pool` 给出 `size / available / requests_waiting`。
  并发正确性**不靠应用锁，靠数据库**：租约 + `lease_epoch` fencing、
  `FOR UPDATE SKIP LOCKED` 领取、事件序号用 `pg_advisory_xact_lock` 按任务串行化。
  （演进过程：单连接 + 线程锁 → 连接池 → 异步驱动。每一步都把上一步盖住的
  问题暴露出来：去掉进程锁才发现事件序号的真实竞争，改成异步之后同步调用卡住
  事件循环这件事也没了。）
- **SQLite**：标准库 `sqlite3`，WAL 模式，零新增依赖，适合单机快速起步。
  它没有官方异步驱动，所以对外同样是异步接口、内部用 `asyncio.to_thread`
  把同步执行丢进线程池——**两种实现对调用方长得完全一样**。
- 仓储接口见 `app/persistence.py`（全部 `async def`），`check_repository()`
  会同时校验"方法齐备"和"实现确实是协程函数"（同步实现会在异步调用点返回一个
  没人 await 的协程，表现成"调了但什么都没发生"，最难查）。

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
- **异步驱动 + 连接池解决的是单进程吞吐，不是高可用**。多副本之间仍然只靠数据库
  协作（租约/账本对账），**跨实例实时事件推送**还没做：副本 A 上的客户端看不到
  副本 B 产生的实时事件（要靠重连 + `Last-Event-ID` 补发，事件本身不丢）。
- **池大小是静态配置**，没有按负载自适应；借不到连接会直接报错而不是排队降级。
- **SQLite 模式仍是"线程池 + 单写者"**：接口异步了，但底层还是一条写连接；
  真要并发就换 PostgreSQL。
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

## 4. 状态、outcome 与展示标签

三层分工（实现见 `app/outcome.py`）：

| 层 | 存在哪 | 词汇 | 会不会随能力增长 |
|---|---|---|---|
| 状态机 | `tasks.status` / `runs.status` | 11 个通用值：`queued` `running` `reconciling` `waiting_input` `waiting_approval` `succeeded` `partially_succeeded` `needs_reconcile` `failed` `cancelled` `interrupted` | **不会**。接第 N 个 MCP Server 只让 `tool_key` 的行数变多 |
| outcome | `state.outcome`（随任务持久化、随接口返回） | `{kind, produced[], effects[], answered, unverified}` | 会。它是数据，可以有无穷多种组合 |
| 展示标签 | 接口字段 `status` / 前端文案表 | `answered` `local_ready` `deployed` `deploy_needs_check` `compensated` … | 会，而且随便加——它不进状态机、不写库 |

规矩只有一条：**要加展示词就改 `app/outcome.py:label()` 和前端文案表，不要往
`tasks.status` / `runs.status` 里加业务值。** `StatusVocabularyTests` 把这条钉住了
（加一个业务终态会先让那组用例红）。

历史数据用一次性的迁移脚本收口（幂等，可重复跑）：

```bash
python -m scripts.migrate_status_vocabulary            # 只报告
python -m scripts.migrate_status_vocabulary --apply    # 写库
```

读取端也留了兜底：库里若还有旧的业务终态（比如从旧备份恢复），
`outcome.normalize_status()` 会归一后再用，不会把 `local_ready` 当成一个新状态。

## 5. 加一个能力包（不用改内核）

本地工具由能力包自己用 `@tool` 声明，内核只负责汇总。加一个能力 = 加一个文件：

```python
# app/capabilities/notes.py —— 全部内容都在这一个文件里
from app.capabilities.registry import tool

CAPABILITY = 'notes'
PROMPT_RULES = '【笔记】用户要记/查笔记时才用这些工具……'   # 自动拼进规划器提示词

@tool(name='save_note', label='记笔记', effect='write',
      description='把一段文本存成本地笔记',
      args={'type': 'object', 'properties': {'text': {'type': 'string'}}})
async def save_note(args, state):
    ...
```

重启进程即可：`app/capabilities/registry.py` 扫 `app/capabilities/*.py`，`@tool`
登记的东西自动出现在规划器清单里，`tool_key` 默认 `local:<CAPABILITY>:<name>`。
`app/tools.py`（内核）**一个字都不用改**，它也不 import 任何具体业务包
（`CapabilityRegistryTests.test_kernel_does_not_import_business_packages` 盯着这条）。

两条来源分得很清，别混：

| 来源 | 谁提供 | 怎么来 | 身份 |
|---|---|---|---|
| 本地能力包 | `app/capabilities/*.py` 里的 `@tool` | import 即注册（静态） | `local:<能力>:<动作>` |
| 远端 MCP 工具 | MCP 服务端 | 运行时发现（有 TTL、掉线、前缀） | `mcp:<server>:<tool>` |

## 6. 通用调用型 MCP 服务端（run_tool 形态）

有些服务端只暴露一个 `run_tool`，真正的动作名放在参数里（`run_tool(name="tts_selector")`）。
这种服务端必须多配两项，否则模型只能**猜**内部工具名：

```jsonc
{"id": "openmontage", "transport": "stdio", ...,
 "capability_tool": "list_capabilities",   // 清单工具
 "tool_name_arg": "name"}                  // 内部工具名参数
```

配上之后有三层保护：

| 层 | 行为 |
|---|---|
| 发现（规划前） | 顺带调用清单工具，把**真实内部工具名 + 可用状态**写进 `run_tool`/`list_capabilities` 的工具描述里，规划器看得到 |
| 调用前 | `name` 不在清单里 → 本地直接失败，报错里带真实可用名字（不再空跑一次远端往返） |
| 回执 | 服务端回 `status: error` / `code: E_*` → **判为失败**、停下后续依赖步骤，并把服务端原话透到回执里 |

**本机特有用法**（例如本地 TTS 必须传语音模型绝对路径）用 `tool_hints` 交给规划器，
不用让模型猜：

```jsonc
{"id": "openmontage", ..., "tool_hints": {
   "piper_tts": "model 要传语音模型绝对路径（~/.piper-voices/zh_CN-huayan-medium.onnx）"}}
```

键可以是 MCP 工具名，也可以是 `run_tool` 的内部工具名——后者会被拼到通用调用工具的描述里。

失败时还会区分**"我们调度错了"和"服务端缺依赖"**：错误文本里出现
`Unable to find voice` / `ffmpeg` / `not installed` 这类字样时，回执会补一句该怎么装。
服务端自检经常只查"命令在不在"，报 AVAILABLE 但真跑起来才炸——这类只能靠失败文本认。

再加两条硬规则（`MCP_PROMPT_RULES`）：

- **创作内容（剧本/分镜/文案）由模型自己写**，用 `write_file` 落盘；不要假设服务端有
  "生成脚本/分镜"的接口——多数服务端只提供执行动作。
- **封装型执行工具**（如 `render_video`）也依赖内部工具，调度前先看清单里它们是否可用。

## 7. 部署行为

```
DEPLOY_ENABLED=false            → 清单里没有 deploy_site（连占位都没有）
DEPLOY_ENABLED=true + 用户要求  → 计划最后加一步 deploy_site
DEPLOY_ENABLED=true + 未要求    → 停在本地产物，界面出现「部署到服务器」按钮
DEPLOY_ENABLED=true + 服务端掉线 → 留占位工具，调用时报「部署不可用：<原因>」
```

意图由模型判定（能力包的【发布】规则），**不使用关键词匹配**：
"对比数据库的部署方式"属于研究内容，不会被误判为要求部署。

部署工具的身份是 `mcp:<server>:<tool>`，账本里因此能看出"这一轮依赖哪个服务端"；
`DEPLOY_ENABLED=false` 时它整体不在清单里，没法绕过去调原始工具。

只重试部署（不重新生成）：

```bash
python -m scripts.retry_deploy <task_id> --server-state-checked
```

查看远端全部工具：

```bash
python -m scripts.list_mcp_tools
```

## 8. 耗时统计

每个节点的耗时写入 `state.json` 的 `timings` 与 `elapsed_seconds`（CLI 打印汇总表），
节点包装层同时发出 `stage_start` / `stage_end` 事件供界面实时累加。

当前链路的实测构成（DeepSeek 官方接口，检索 2 次 + 抓取 + 转网页）：

| 阶段 | 实测 | 说明 |
|---|---|---|
| 规划 | 2 ~ 4s | 唯一必有的模型调用 |
| 检索来源（每次） | 0.5 ~ 0.7s | 直接使用官方 Search 返回的正文，不补抓搜索结果 |
| 抓取正文 | 0 ~ 3s | 只有输入里给了 URL 才有这一步 |
| 资料转 HTML | 0.02s | 纯本地渲染，不调用模型 |
| 部署 | 1 ~ 3s | MCP 往返 |

所以一条「检索 → 转网页」的任务通常在 **5 秒上下**，其中模型只花在规划上。
早期版本还有"结构化报告生成"阶段（单次 60~100s、占总耗时 85%），该能力已按需移除。

### 规划阶段为什么能关推理

`LLM_PARSE_DISABLE_THINKING=true` 让规划阶段关掉推理：实测多轮场景 4.4s → 2.3s
（约 2 倍），判断准确率不变。耗时几乎全在推理 token 上，与输入长度无关。

### 真实瓶颈可能是网关抖动

同一模型、同一提示词的耗时波动可以到一倍以上，判断"是不是代码变慢了"前，先看
`timings` 里是不是所有阶段一起变长——那种情况通常是网关或网络，而不是本项目的逻辑。

### 网络提示

代理/CONNECT 不可用时，**所有模型与检索调用**会报 `APIConnectionError` /
`ConnectError`。DeepSeek 官方接口与 MCP 服务端都走网络，排查时先用 `curl` 直连确认。

## 9. 测试

```bash
python -m unittest discover -s tests -v
```

**233 条用例**。项目只支持 PostgreSQL，用例也直接跑真实 PG（每个用例前 TRUNCATE）：

```bash
PG_TEST_DSN=postgresql://…/agent_platform_test python -m unittest tests.test_core
```

没有 `PG_TEST_DSN` 时数据库相关用例会逐条跳过（不会静默退回 SQLite——已经没有那个实现）。

覆盖：URL 安全校验、HTML 自动转义、无正文禁止导出、部署闸门与回执归一、结构化输出重试、
MCP 参数组装（base64）、多服务端路由与失败隔离、**MCP 业务失败识别 / 内部工具名校验 /
创作内容落盘 / 未解析占位符拦截**、阶段事件与实时计时、PostgreSQL 仓储一致性与连接池并发、
任务中枢、HTTP 接口与 SSE 事件流、助手回执文案、状态机词汇封闭与 outcome 派生、能力包注册化。

覆盖：URL 安全校验、引用存在性、HTML 自动转义、无正文禁止导出、部署闸门与回执归一、
结构化输出重试、MCP 参数组装（base64）、多服务端路由与失败隔离、阶段事件与实时计时、
SQLite 存储与重启恢复、任务中枢、HTTP 接口与 SSE 事件流、助手回执文案、
**状态机词汇封闭 / outcome 记录 / 展示标签派生 / 历史数据迁移**。

```bash
# 真实 PostgreSQL 一致性（DSN 必须指向一次性测试库）
PG_TEST_DSN=postgresql://…/agent_platform_test \
  python -m unittest tests.test_core.PostgresParityTests
```
