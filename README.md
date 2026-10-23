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
| `AUTH_MODE` | `off`（默认，本地单用户）/ `optional` / **`ticket`**（正式）/ `token`（旧） |
| `AUTH_CONNECTIONS_FILE` | 可信平台登记表（公钥只用这里的） |
| `AUTH_TICKET_MAX_LIFETIME` | 票据有效期上限，默认 60 秒 |
| `AUTH_SESSION_TTL` | 中台登录会话有效期，默认 7200 秒 |
| `AUTH_SESSION_COOKIE` / `AUTH_COOKIE_SAMESITE` / `AUTH_COOKIE_SECURE` | 会话 cookie 参数 |
| `AUTH_SSE_RECHECK` | 长连 SSE 复查登录态的周期，默认 20 秒 |
| `POLICY_LEGACY_VISIBLE` | 存量 `default` 数据是否对所有登录用户可见，**ticket 模式必须 false** |
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

### 零密钥中文动画解说视频

主项目在 `demo` 环境运行，OpenMontage MCP 在 `openmontage` 环境运行。
首次安装本地视频链路的依赖与中文语音模型：

```bash
conda activate demo
python -m scripts.setup_local_video
```

该流程需要 FFmpeg/FFprobe、Piper 及 `zh_CN-huayan-medium` 模型和配套 JSON、
Remotion 的 npm 依赖以及 Chrome（macOS 优先复用已安装的 Google Chrome）。
模型默认保存到 `openmontage-zh-mcp/models/piper/`，也可通过
`LOCAL_VIDEO_VOICE_MODEL` 指定完整的 `.onnx` 路径。

重启网页服务后输入「制作一个 45 秒的动画解说视频，解释为什么天空是蓝色的」。
客户端的 `make_local_explainer` 能力会先核对 MCP 的真实能力和参数，再按项目 Skills
编写分镜，保持同一个 MCP 会话完成逐镜中文配音、动画渲染和成片检查。
最终输出有中文旁白和字幕的 1280×720 MP4，并在网页提供播放器及下载按钮。
当前这条本地路径不添加背景音乐，不调用付费图片或视频生成 API；
编写剧本和分镜仍使用主项目已经配置的大模型。
建议 `TASK_TIMEOUT=900`，为首次渲染保留足够时间。

如果语音模型下载未完成，任务会在模型调用和配音前报告缺失，而不会把脚本文件当成视频交付。

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
| 状态持久化 | ❌ 纯内存 | ✅ PostgreSQL（唯一实现） |
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

### 切会话不打断任务

- 切换会话只是**解绑事件流**，后端 run 继续跑（不会 POST `/cancel`）；
- 切回来时 `/api/sessions/{id}/timeline` 会给出 `live_task_id`，前端重新订阅，
  服务端按 `seq` 把期间发生的事件全量补发，过程看得完整；
- 每轮的 `artifact`（视频/页面）也在时间线里，切回来还能播放/下载——
  以前只有"刚跑完那一刻"才渲染过产物入口。

产物接口按扩展名/`kind` 给媒体类型（`.mp4 → video/mp4`、`inline`、支持 Range），
所以视频能直接播放、拖进度条。

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

**仓储只有 PostgreSQL 一种实现**：`.env` 里必须给 `DATABASE_URL`，留空直接报错。
SQLite 实现已整体删除——两套实现意味着租约、代次、事务边界、动作准入这些
**只有真库才能提供的语义**要在两边各写一遍，而其中一边永远测不充分。

```ini
DATABASE_URL=postgresql://latiao@127.0.0.1:5432/agent_platform
```

早期 SQLite 数据的迁移脚本仍保留（一次性用）：

```bash
python -m scripts.migrate_to_postgres --dsn "postgresql://user@127.0.0.1:5432/agent_platform"
```

脚本会逐表搬数据并**校验行数一致**，完成后复位 `messages.id` 的序列。
本地实测迁移了 21 会话 / 139 消息 / 71 任务 / 1069 事件，行数校验通过。

### 表一览（25 张）

| 分组 | 表 | 内容 |
|---|---|---|
| 会话 | `sessions` / `messages` | 多轮上下文的主体与消息 |
| 记忆 | `session_memory` / `session_summaries` | 长会话滚动摘要与关键事实 |
| 任务 | `tasks` / `tasks_meta` | 任务状态、产物 ID、完整 `state`（含 timings） |
| 运行 | `runs` / `plans` / `steps` / `run_snapshots` | 每次执行尝试、计划版本、步骤状态、**逐步快照**（恢复的唯一依据） |
| 补充 | `run_inputs` / `run_input_processing` | **不可变**的补充日志（一人一条、顺序由服务端分配）与可重建的处理投影 |
| 决策 | `run_decisions` | 每次自主决策的台账：预留/模型请求数/候选哈希/已提交计划版本/结果水位 |
| 身份 | `app_users` / `identities` / `auth_sessions` / `ticket_redemptions` | 中台用户、外部身份绑定、登录会话（只存摘要）、一次性票据登记 |
| 幂等 | `request_keys` | 建任务幂等键（按主体+接口作用域） |
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

### 结果驱动的观察与决策（不必提前猜未知参数）

计划里最尴尬的一类步骤是"依赖还不知道的结果"：素材 ID、片段区间、要读哪个文件。
以前只能让模型在第一份计划里猜，猜错就整轮失败。现在分两段执行：

```text
plan → persist_plan → run_step → observe_result ─┬→ run_step（参数能直接绑定）
                                                 ├→ decide_next（需要判断）
                                                 ├→ revise_plan（用户补了要求）
                                                 ├→ verify_completion（都跑完了）
                                                 └→ wait_async / 结束（控制/未定）
decide_next → run_step | persist_plan | verify_completion | 结束
verify_completion → decide_next（可有限修复）| finalize（核对通过）
```

**每个工具之后都跑一次程序观察**（不花模型请求），只有真正需要语义判断时才问模型。
确定链路（读已知产物 → 打包 → 导出）因此一次模型请求都不多花。

三条合同写在 `app/observation.py` 与 `app/schemas.py`：

| 合同 | 内容 |
|---|---|
| `ToolResult` | 每次调用归一成 `{execution_status, phase, data_ref, summary, artifact_refs, external_receipt_refs, evidence_refs, complete, error}`。**完整结果落 `results/*.json`**，模型只看有界摘要 + 引用（`totals`/`has_more`/`next_cursor` 都会保留，不会把"前 3 项"当"全部"） |
| 片段策略 | 计划声明 `after_segment: observe/verify` + `observation_goal`：首版计划可以只包含"能确定的事"，剩下的交给观察之后决定 |
| `ObservationDecision` | 观察后模型只能说六件事：`continue` / `revise` / `wait_input` / `wait_approval` / `finish` / `stop`，每种都有必备字段（空 `revise`、无问题的 `wait_input` 直接校验失败） |

两条纪律值得单独说：

- **完成由程序核对，不由模型宣布**。`verify_completion` 逐条检查完成条件：
  `artifact_exists` / `file_written` / `deployment_receipt` / `answer_present` /
  `tool_succeeded` 有证据才算过；`content_quality` 这类只能看内容判断的，一律标
  **未检查**——文件存在不等于画面合格。缺证据就报"还没满足什么"，不会因为模型说了
  `finish` 就成功。
- **结果未定不许猜**。写操作 `in_flight/unknown` 直接转 `needs_reconcile`，
  观察层根本不会走到模型决策；权限拒绝、暂停、失租同样优先走既有门禁。

预算与无进展保护落在库里（`runs.consumed_json`，跨重启累计）：
`RUN_MAX_PROVIDER_REQUESTS`（含结构化解析重试）、`RUN_MAX_TOOL_CALLS`、
`RUN_MAX_DECISIONS`、`RUN_MAX_REPAIRS`、`RUN_MAX_NO_PROGRESS`。
"没有进展"按**同查询 + 同结果**判定并停下，不靠文字摘要猜。
决策本身有台账（`run_decisions`：预留 → 调模型 → 校验 → CAS 提交），
同一观察的重复执行按稳定 `decision_key` 去重，换 ID 绕不过预算。

### 运行中补充要求（不打断、不新建任务）

任务跑着的时候再说一句，默认是**给当前任务的补充要求**：同一个 task/run、同一条
事件流，只是"接下来做什么"变了。要另开一轮，点「新任务」。

```bash
# 补一句要求（202 = 已落库，不代表已经纳入计划）
curl -X POST localhost:8010/api/runs/<run_id>/inputs \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"6f1c…（客户端生成并保存）","message":"改成竖屏，结尾不要品牌名"}'

# 回读补充与处理状态
curl localhost:8010/api/runs/<run_id>/inputs?after_seq=0
```

**三个版本分开存**，混用任何一个都会让"旧要求产生的操作"重新获得资格：

| 版本 | 代表什么 | 什么时候变 |
|---|---|---|
| `lease_epoch` | 哪个执行者有权推进 | 领取时 +1 |
| `intent_version` | 用户要求更新到哪一版 | 接受一条补充时 +1 |
| `plan_version` | 当前采用哪份计划 | 提交一份计划时 +1 |

一次补充的完整生命周期（都在数据库里，可恢复）：

1. **接受**（一次事务）：插 `run_inputs` 日志 → `intent_version += 1` → 写
   `run_input_processing(accepted)` → 追加 `input_received` 事件。四件事同生共死。
   同一个 `request_id` 重试只会返回原记录，**版本不再增长**。
2. **阻断**：旧计划的下一步在 `admit_action` 里比较版本，不相等就抛
   `IntentChanged`——这不是失败、不是 tool error、不触发补偿，只是"这一步没派发"。
   已准入的动作照常结算：真实发生过的效果不能因为用户改主意就丢掉。
3. **修订**：`revise_plan` 读"原目标 + 按序补充 + 已执行事实 + 在途动作"，
   只重写**剩余**步骤（新步骤 ID 形如 `p2_s1`，旧计划的未执行步骤标
   `superseded`），提交时用 `base_plan_version`/`intent_version` 做 CAS。
4. **收尾门禁**：`finish_run_owned` 在同一个行锁里检查"最新版本已处理 + 没有未处理
   补充"，否则拒绝结束——绝不出现"已接受但没人管"的悬空输入。

状态文案要分清：`accepted`（已收到）/ `applied`（已纳入计划）/ 业务完成，
三件事不一样，界面上也分开显示。修订有次数上限（`RUN_MAX_REVISIONS`），
超限就进入 `waiting_input` 等用户显式继续，不假装已理解。

### 重启后的恢复

恢复**不靠图重放**，只靠库里的四样东西：已提交的计划、逐步快照、动作账本、
步骤状态（`graph.checkpointer()` 明确返回 `None`——图重放会绕开账本，等于把
远端调用再发一遍）。算法：

1. 用 `claim_run` 重新领取（代次 +1，旧执行者的写入全部被拒）；
2. 读计划：有计划就**沿用**，没有才规划。空计划不等于"有计划的续跑"；
3. 有动作却没有计划 → 转 `needs_reconcile`，**不允许**用新规划冒充恢复；
4. 按动作推断起点：`succeeded` 的步骤直接复用回执（不重发），`prepared` 可以
   沿同一动作身份重做，`in_flight`/`unknown` 一律阻断，等人工核实；
5. 所有步骤都已结算、只差收尾 → 直接用快照（或动作回执）收尾，不再跑图。

对账统计看 `GET /api/recovery`；"待核实"的 run 状态是 `needs_reconcile`，
它既不是成功也不是失败。详见 `docs/platform-acceptance.md`。

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
- **只留一种仓储实现**：SQLite 版已删除。它对"租约代次、`SKIP LOCKED` 领取、
  单事务内动作准入"这些东西只能做近似，而近似恰恰是执行可靠性最不能接受的部分。
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
- **执行权靠租约与代次，不靠"谁是最后一个写的"**：`runs.lease_owner` +
  `lease_epoch` 做 fencing，准入/结算/终态/续租全部带条件提交；租约过期或代次
  变化一律按失租处理（抛 `LeaseLost`，**不写任何业务状态**）。续租失败同理：
  停下来，而不是继续派发下一步。
- **暂停是"请求 + 边界确认"两段式**：`queued` 的 run 立刻变 `paused`（Worker 领不到）；
  `running` 的只登记 `pause_requested`，由执行者在**下一处准入前**读到并退出。
  准入与暂停在同一行锁上裁决：暂停先提交 → 这一步不获得调用资格；准入在先 →
  只允许该步收尾，后续不再派发。快照随暂停一起提交，续跑从下一步继续。
- **补充要求不是取消，也不是批准**。它不会撤销已经发生的外部效果（已发布的站点
  仍然在线），也不会凭空扩大权限：一条"直接发布"的补充拿不到原来的计划里没有的
  发布工具。要收回副作用走 `POST /api/runs/{id}/compensate`。
- **跨进程的补充通知是"最终一致"的**：接受事务提交后，其它进程里的执行者在自己的
  下一个边界读到新版本；进程内会额外发一条事件加速，但**正确性不依赖通知**。
- **"结果未定"是独立结论**：超时/断流算 `unknown`（可能已经写到远端），业务失败
  算 `failed_confirmed`，连接都没建起来算 `not_executed`。确定性与效果类型一起决定
  要不要拦人：`unknown + 有副作用` 才要求核实，只读调用重发一次没有可重复后果。
- **幂等只覆盖"创建任务"**。事件追加、部署、补偿各有更精确的机制（事件序号、
  账本 action、`compensated_at`），不共用这把钥匙。键按 `主体+接口+键` 作用域化，
  默认 24 小时过期（`IDEMPOTENCY_TTL_SECONDS`）。
- **补偿依赖"有撤销工具"**。计划里给某一步写了 `compensate`，失败时才会逆序执行；
  服务端没有撤销工具时这一步就是空的，系统不会假装撤回过。结果未定（unknown）
  的动作**不自动补偿**——那等于猜着再发一次写请求。
- **多 MCP 服务端已支持，但只在本机用假服务端验证过**。`MCP_SERVERS` 配多个后，
  工具名变成 `mcp__<server_id>__<tool>`；只有一个服务端时保持 `mcp__<tool>`，
  旧计划仍可用（`find_tool` 会按尾部唯一匹配回退）。

## 3.4 登录与用户隔离（平台签票 → 中台换会话）

中台不存客户平台的账号体系，也不信任浏览器自报身份。流程是：

```
浏览器 ──(平台自己的登录 cookie)──▶ 客户平台后端
                                   │ 用平台私钥签短期票据（RS256，≤60s，一次性）
                                   ├──▶ 中台 POST /api/auth/exchange
                                   ◀── Set-Cookie: agent_session=…
浏览器 ──(agent_session)──▶ 中台全部业务接口（聊天/历史/画像/事件流/产物）
```

平台后端的签票身份**必须来自它已验证的登录态**，不能让浏览器传个 `user_id` 就代签。

| 组件 | 文件 | 职责 |
|---|---|---|
| 登记表 | `app/auth_connections.py` | 谁可信、允许哪些企业/平台、公钥与 kid |
| 票据验证 | `app/ticket_auth.py` | 票据 → 可信身份；只做这一件事 |
| 兑换与会话 | `app/auth.py` | 验票 + 一次性登记 + 绑定身份 + 建会话（同一事务） |
| 接口 | `app/api.py` | `POST /api/auth/exchange`、`GET /api/me`、`DELETE /api/auth/session` |

几条硬约束（都有用例钉着）：

- **算法固定 RS256**，拒绝 `alg=none` 与"拿公钥当 HMAC 密钥"的 HS256 混淆；
- **公钥只来自登记表**，票据里带的地址永远不会被信任；
- 同一张票据 `(issuer, jti)` 唯一 → 重复/并发兑换只会成功一次（第二次 409）；
- **库里没有明文**：票据不落库，会话凭证只存 SHA-256 摘要；
- 身份 = 企业 + 身份源 + 外部用户 → 中台 `user_id`；中台 ID 终身不变，
  不同平台的同名 `user_id-123` 是两个账号；
- `AUTH_MODE=ticket` 时业务接口一律要求中台会话，**改 `X-DSH-User` 无效**；
- 退出登录撤销数据库会话；已连上的 SSE 会按 `AUTH_SSE_RECHECK` 周期复查并断开。

> **给客户平台研发的对接文档**：[`docs/platform-integration.md`](docs/platform-integration.md)
> —— 签什么票、怎么调、错误码怎么处置、续期/退出、同源代理配置、验收表，都在这份里。

### 本地跑通（两个模拟平台）

```bash
# 1) 生成模拟平台的密钥对与接入登记表（私钥只在 secrets/ 下，已 gitignore）
python -c "from scripts.mock_identity_platform import main"   # 或直接跑下面的服务

# 2) 中台（ticket 模式）
AUTH_MODE=ticket AUTH_CONNECTIONS_FILE=secrets/auth-connections.json \
AUTH_COOKIE_SECURE=false python -m scripts.serve_api --port 8010

# 3) 模拟两个客户平台
python -m scripts.mock_identity_platform --port 8090 --hub http://127.0.0.1:8010
```

打开 `http://127.0.0.1:8090/`，用"平台A/张三"和"平台B/王五"分别登录——两边的
`sub` 都是 `user-123`，但落到两个不同的中台账号，历史与画像互不可见。

浏览器接入第一版用**同源路径代理**（客户平台 `/agent/api/*` → 中台）；前端 API
前缀已可配：给 `<body data-api-prefix="/agent/api">` 即可，不用改代码。

正式环境：`AUTH_MODE=ticket` + `AUTH_COOKIE_SECURE=true` +
`POLICY_LEGACY_VISIBLE=false`（否则存量 `default` 数据对所有登录用户可见）。

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
| 发现（规划前） | 顺带调用清单工具，把**真实内部工具名 + 可用状态 + 参数签名**写进 `run_tool`/`list_capabilities` 的工具描述里，规划器看得到 |

参数签名只列必填项和形状（`direct_clip_search(*output_dir:string,*queries:[{query}],+5可选)`）——
只给名字是不够的：`queries` 是"对象数组"还是"字符串数组"直接决定调用成不成立，
真实报错就是 `'xxx' is not of type 'object'`。
| 调用前 | `name` 不在清单里、或**必填参数没给** → 本地直接失败，报错里带真实可用名字/参数名（不再空跑一次远端往返） |
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
# PG_TEST_DSN 必须指向一次性测试库（库名含 test），用例会 TRUNCATE 全部业务表
PG_TEST_DSN=postgresql://…/agent_platform_test \
  python -W error::RuntimeWarning -m unittest discover -s tests -v
```

**381 条用例，全绿**。项目只支持 PostgreSQL，用例也直接跑真实 PG：

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_core.py` | 249 | 能力包、计划校验、账本、状态词汇、MCP 识别、接口与 SSE、仓储一致性、连接池 |
| `tests/test_worker_execution.py` | 6 | Worker 领取/执行一次、两 Worker 抢同一条、异常不带走循环、空计划、无未 await 协程 |
| `tests/test_execution_control.py` | 9 | 旧代次四件事全被拒、续租失败停派、暂停三种时序、续跑不重发、取消、工具消失 |
| `tests/test_execution_recovery.py` | 7 | 计划先于调用提交、提交失败零调用、prepared 重做、unknown 阻断、succeeded 复用、结算失败不推进 |
| `tests/test_mcp_uncertainty.py` | 6 | 业务失败=failed_confirmed、超时=unknown、连接失败=not_executed、包装后确定性不丢 |
| `tests/test_observation.py` | 15 | 摘要保留结构、路由优先级、结果完整可追溯、空 continue 拒绝、缺证据不成功、无进展停止、预算耗尽、观察开关 |
| `tests/test_run_inputs.py` | 28 | 补充接受/幂等/背压、旧版本动作零准入、计划 CAS、修订校验、收尾门禁、暂停期间补充、HTTP 202/409/404/422 |
| `tests/test_ticket_auth.py` 等 | 56 | 票据验证、兑换、退出、跨平台隔离 |
| `tests/test_local_video.py` | 3 | 本地解说视频能力包 |

`-W error::RuntimeWarning` 是刻意的：漏 `await` 的执行路径不会报错，只会静默失效，
把它提升成错误才能守住"Worker 真的在等结果"这件事。

没有 `PG_TEST_DSN` 时数据库相关用例会逐条跳过（不会静默退回 SQLite——已经没有那个实现）。
