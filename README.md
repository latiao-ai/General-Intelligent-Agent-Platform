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
| `DEPLOY_ENABLED` | 部署总开关；`false` 时只生成本地 HTML |
| `MCP_URL` / `MCP_BEARER_TOKEN` | 远端 MCP 部署服务地址与鉴权 |
| `MCP_TOOL_NAME` | 部署工具名（当前远端为 `deploy_site`） |
| `MCP_EXTRA_ARGS` | 额外入参，如 `{"ttl":"7d"}` 控制站点有效期 |
| `DB_PATH` | SQLite 持久化文件，默认 `agent.db` |

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
| 状态持久化 | ❌ 纯内存 | ✅ SQLite |
| 改代码生效 | 必须重启进程（模块不热重载） | `--reload` 自动 |

### 阶段耗时实时累加

每个节点进出时由 `app/graph.py` 的包装层发出 `stage_start` / `stage_end` 事件。
前端收到 `stage_start` 就开一行，并按**服务端给的开始时刻**每 100ms 刷新；收到
`stage_end` 冻结为该阶段实际耗时：

```
从发出指令到开始搜索：7.05 秒          ← 单独一行，不混在阶段表里

阶段          耗时(秒)   状态
解析需求         7.00    planned
检索来源        22.41    sources_discovered
抓取正文         0.00    fetched
整理报告        62.31    进行中…        ← 实时增长
```

**「从发出指令到开始搜索」单独放在表头**，因为它**跨越了解析需求**（= 解析耗时 + 提交
开销），混进阶段表会被误当成一个需要相加的独立阶段。实测两者只差约 0.04 秒——
它衡量的是用户视角的"我发出去之后多久才真正开始干活"。

**阶段行一定会被关闭。** 任务成功、中断、失败三种结局下，进行中的那一行都会被冻结为
实际耗时并标注状态（`已中断` / `已失败`），不会一直停在"进行中"、耗时一路涨。

> 中断场景需要任务中枢**兜底补发**结束事件：节点自己发的 `stage_end` 会写进 LangGraph
> 的流队列，而消费循环此刻已被取消、没人来取，事件就此丢失。前端也有一层兜底。

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

会话消息在服务端存在 SQLite 里，但**前端不做自动恢复**：每次打开或刷新页面都是干净的新
会话（否则重启服务后旧对话会自动冒出来）。需要清空当前对话时点「新对话」——运行中点击
会先打断当前任务。历史任务仍保留在数据库里，可经 API 查询。

## 3.2 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 聊天界面 |
| `POST` | `/api/tasks` | 开一轮任务，body `{message, session_id?}`，返回 `{task_id, session_id}` |
| `GET` | `/api/tasks/{id}/events` | **SSE 事件流**，实时推送执行过程 |
| `POST` | `/api/tasks/{id}/cancel` | 停止任务 |
| `GET` | `/api/tasks/{id}` | 任务状态（含 `state`、`timings`） |
| `GET` | `/api/tasks/{id}/artifact` | 下载生成的 HTML |
| `POST` | `/api/tasks/{id}/deploy` | 手动部署到远端服务器 |
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

## 3.3 SQLite 持久化

会话、任务状态与事件日志都落在 SQLite（默认 `agent.db`，用 `DB_PATH` 改路径），
服务重启后不丢。

| 表 | 内容 |
|---|---|
| `sessions` | 会话（多轮上下文的主体） |
| `messages` | 会话消息，`parse_request` 靠它还原指代型要求 |
| `tasks` | 任务状态、产物 ID、完整 `state`（含 timings） |
| `events` | 每个任务的完整事件日志（带任务内自增 `seq`） |

### 内存与磁盘的分工

| | 存哪 | 为什么 |
|---|---|---|
| 事件日志 | **SQLite** | 权威记录，重启后仍能补发完整过程 |
| 会话消息 | **SQLite** | 多轮上下文不能丢 |
| 任务状态 | **SQLite** | 重启后仍可查历史任务 |
| 订阅者队列 | 内存 | 连接级状态，不可能持久化 |
| `asyncio.Task` | 内存 | 活着的进程状态，不可能持久化 |

因此设计是**先落库再投递**：`publish()` 先把事件写入 SQLite 拿到序号，再推给内存订阅者。
正在跑的客户端拿到零延迟事件；晚连上来的、甚至服务重启后再连的，从库里补发——两者用
`seq` 去重。

### 孤儿任务恢复

服务启动时会把库里遗留的 `running` 任务标记为 `interrupted`。进程重启后不可能还有任务
在跑；不处理的话前端会永远等一个不会结束的任务。

### 实现取舍

用 Python 标准库 `sqlite3`，**没有引入新依赖**。WAL 模式（读不阻塞写），单连接 + 线程锁
同步调用——SQLite 本地写入是亚毫秒级，demo 规模完全够用；真正高并发时应改为
`asyncio.to_thread` 或换专门的数据库。

### 验证持久化

```bash
# 1) 起服务并建任务，记下返回的 task_id
python -m scripts.serve_api --port 8010
curl -X POST localhost:8010/api/tasks -H 'Content-Type: application/json' \
  -d '{"message":"整理 SQLite 的适用场景，生成中文报告"}'

# 2) Ctrl+C 停服，再重新启动，然后：
curl localhost:8010/api/tasks/<task_id>             # status 变为 interrupted
curl -N localhost:8010/api/tasks/<task_id>/events   # 完整事件流从库里补发
```

### 已知边界

这仍是**单机单进程**的持久化：库文件在本机，多个服务副本各写各的会冲突。SQLite 解决的是
"重启不丢"，**不是**"横向扩展"。要真正多副本，需要换 Postgres/MySQL 这类支持并发的
数据库，并把事件广播换成消息总线（Redis Pub/Sub 等）。

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
