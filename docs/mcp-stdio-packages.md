# 上传文件夹型 MCP Server（stdio）与自动登记

> 场景：对方交付的是**一个文件夹**，没有 HTTP 地址，只能 `node index.js` /
> `python -m server` 这样在本地起进程。中台现在的控制台接不了这类东西——不是没做
> 入口，是三处**刻意**拒绝。这份文档解决「管理员在浏览器里把它传上来」这件事。

## 0. 结论

可以做成「**拖文件夹 → 上传 → 自动识别 → 试跑探针 → 预填表单 → 管理员确认发布**」。

但「自动」只到**预填**为止，不自动启用。上传完成的那一刻，中台就多了一个能在自己
机器上起进程的东西；`tools/list` 跑通只证明它握手成功，不证明它的工具安全。平台连接器
是**全员可见**的，一次误点等于把未知进程开放给所有用户的对话。

一句话：**自动登记，人工发布。**

## 1. 现状为什么拒绝

| 位置 | 现在的行为 |
|---|---|
| `store/platform_catalog.py:56` | `_TRANSPORTS = ('streamable_http', 'sse')`，校验直接拒 stdio |
| `api/mcp_manager_api.py:74` | `entry.get('command')` 存在即 400 |
| `integrations/mcp_servers.py:299` | 用户自建配置里的 stdio 条目静默跳过 |

三处注释的理由是同一个：中台服务账号在服务器上跑，网页表单里一条 `command` 就是
远程命令执行。这个判断是对的，**本方案不推翻它**，而是把「执行什么」从管理员手填
变成「上传包 + 白名单解释器 + 受管目录」，可管控面从「任意命令」收窄到「一个已经
落在磁盘上的包」。

同时，底层能力其实已经齐了：

- `Server` 有 `stdio_command` / `stdio_args` / `stdio_env`；
- `mcp_adapter.py` 有完整 stdio 客户端（子进程复用、空闲回收、env 注入、主体透传）；
- `MCP_SERVERS` 写 `transport: "stdio"` 今天就能跑。

现成的例子就是 `libtv-local`（LibTV 本地桥，`docs/connectors.md` §6.7）：它是一条
`MCP_SERVERS` 里的 stdio 条目，`node lib/cli.mjs` 起进程。**这条路本来就在跑，
只是入口在 `.env` 里，只有运维能改。** 本方案要做的，是把入口挪到管理台，同时
把「随便写一条命令」收窄成「上传一个包」。

缺的只有三样：**cwd 透传、文件托管、管理台入口**。

## 2. 关键技术缺口：cwd

MCP Python SDK 的签名（已在本机核对）：

```python
StdioServerParameters(*, command: str, args: list[str] = [],
                      env: dict[str, str] | None = None,
                      cwd: str | Path | None = None, ...)
```

**支持 cwd**，但 `mcp_adapter.py:186`（`_open_slot` 里的 `StdioServerParameters(...)`）
构造时没传，子进程的工作目录是 API 进程的 cwd。托管包必须在自己的目录里起（`require('./lib/x')`、`import server` 都依赖它），
所以：

1. `Server` 增加 `stdio_cwd: str = ''`；
2. `_from_entry` 解析 `stdio_cwd`，**必须是绝对路径且落在 `MCP_PACKAGE_ROOT` 下**
   （`Path(p).resolve()` 后 `is_relative_to(root)`，不是字符串前缀比较）；
3. `_borrow_stdio` / `_open_slot` 把 `cwd=` 传给 `StdioServerParameters`；
4. **`_stdio_key` 必须把 cwd 算进 key**（`mcp_adapter.py:126`）。不算的话，管理员改了
   包目录但命令没变，会静默复用旧子进程——工具清单还是上一版的，最难查的一类 bug。

## 3. 数据模型

### 3.1 平台目录条目新增字段

```jsonc
{
  "id": "jiaxing-mcp",
  "name": "嘉兴画布",
  "transport": "stdio",
  "stdio_command": "/usr/local/bin/node",   // 绝对路径，白名单解释器
  "stdio_args": ["index.js"],                // 相对 cwd，不许绝对路径不许 ..
  "stdio_cwd": "/data/mcp-packages/jiaxing-mcp/1.2.0",  // 只由上传生成，前端不可写
  "stdio_env": {"JX_API_KEY": "..."},        // 敏感值，接口返回时打码
  "package_id": "pkg_7f3a...",               // 指向托管包
  "package_version": "1.2.0"
}
```

### 3.2 托管包索引（新文件）

`profile/mcp_packages/index.json`，与 `platform_catalog.json` 同一套 `flock` +
`os.replace` 写法：

```jsonc
{
  "pkg_7f3a": {
    "id": "pkg_7f3a", "name": "jiaxing-mcp", "version": "1.2.0",
    "root": "pkg_7f3a/1.2.0", "file_count": 128, "bytes": 3204112,
    "sha256": "...", "runtime": "node@20.11", "has_deps": true,
    "probe": {"ok": true, "tools": ["create_node", "read_project"], "at": "..."},
    "uploaded_by": "u_admin", "uploaded_at": "..."
  }
}
```

目录布局：`${MCP_PACKAGE_ROOT:-profile/mcp_packages}/<pkg_id>/<version>/`。
用上传时生成的随机 `pkg_id` 做目录名，**不用上传的文件名**——文件名是攻击面
（控制字符、超长、`..`、Windows 保留名）。

## 4. 链路

### 4.1 上传

- 前端虚线框支持拖**文件夹**：`DataTransferItem.webkitGetAsEntry()` 递归读，
  在浏览器里打 zip（`JSZip` 或原生 `CompressionStream` + 自写 zip 头）再传；
  也接受单个 `.zip` / `.tar.gz`。这样管理员看到的是"把文件夹拖进来"。
- 上限：解压前 50 MB，解压后 5000 文件 / 200 MB。超了当场中止并清理。
- 准入（按顺序，任一失败即 400）：
  - 必须是 zip 或 tar.gz，嗅探 magic 而不是信扩展名；
  - 逐条校验 entry：拒绝绝对路径、含 `..` 的路径、符号链接、硬链接、设备文件、
    以及解压后落在根目录外的路径（**zip-slip**）；
  - 单条路径长度 ≤ 240，层级 ≤ 12；
  - 解压时 `os.open(..., O_NOFOLLOW)`，只写普通文件；
  - **不保留可执行位**（统一 0644，目录 0755）。
- 解压完再算一次实际文件数/字节数，超阈值就整目录删掉（防 zip 炸弹：头部声明
  10 KB，实际解出 10 GB）。

### 4.2 自动识别启动方式

按优先级探测，产出**候选列表**（带置信度和依据），不替管理员拍板：

| 依据 | 推断 |
|---|---|
| `package.json` 的 `bin` | `node <bin>`（最高置信） |
| `package.json` 的 `main` | `node <main>` |
| `pyproject.toml` 的 `[project.scripts]` | `python -m <module>` 或 entry point |
| `requirements.txt` + `server.py`/`main.py`/`app.py` | `python <file>` |
| README 里的 `npx` / `uvx` / `python` 命令行 | 解析成 args |
| `Dockerfile` + `CMD` | 标记为「建议 docker 运行」，一期不自动接 |

同时探测依赖状态：`node_modules/` 或 `.venv/` 在不在、`requirements.txt` 有几个包。
不在就在表单上标红「需在包内自带依赖，本机不联网安装」。

### 4.3 探针（真正起一次）

这一步是"自动登记"的价值所在——**不猜，跑一次**。

- 独立起进程（**不要**走 `_borrow_stdio`，否则会污染 `_stdio_slots` 缓存）；
- `initialize` → `tools/list`，总超时 20s；
- 超时或失败：`killpg` 整个进程组（`start_new_session=True` 起的），回收 stdout/stderr，
  返回 stderr 尾部 2000 字符；
- 失败归因（别只说"连接失败"）：
  - `Cannot find module` / `ModuleNotFoundError` → 缺依赖
  - `command not found` / `ENOENT` → 解释器不对
  - 启动即退出且 stderr 空 → 入口猜错
  - 起了但 20s 没握手 → 可能不是 MCP（或要等首次编译）
  - stderr 含 `API_KEY` / `401` → 缺环境变量
- 成功：工具清单 + 实际耗时回填表单，并显示"这 7 个工具将对所有用户可见"。

### 4.4 登记与发布

1. 探针成功后表单预填：`stdio_command` / `stdio_args` / `stdio_cwd`（只读）/ `stdio_env`；
2. 管理员补 `id`、显示名、简介、env 敏感值，点「保存」→ 写 catalog，`enabled: false`；
3. 再点「发布」→ `PATCH` 置 `enabled: true`，`servers()` 下一轮请求即生效；
4. 全部动作写审计（谁、什么包、什么命令、从哪个 IP）。

### 4.5 升级与删除

- 升级 = 传新版本到 `<pkg_id>/<new_version>/`，探针通过后改条目里的 `stdio_cwd`
  再切换；旧版本目录保留一份用于回滚（`stdio_cwd` 改回去即可，不用重启）。
- 删除连接器时提示：「只删配置」或「配置和包一起删」；包被其他条目引用时禁止删包。
- 切版本要 `close_stdio_sessions(server_id)` 主动关掉旧子进程，别等空闲回收——
  否则最多有 `stdio_session_idle_seconds` 的窗口在跑旧代码。

## 5. 接口

```
POST   /api/console/mcp-packages            上传（multipart，zip/tar.gz）
GET    /api/console/mcp-packages            列表（含探针结果、引用数）
POST   /api/console/mcp-packages/{id}/probe 试跑（body: command/args/env，可反复试）
GET    /api/console/mcp-packages/{id}/files 目录树（脱敏，只给人看）
DELETE /api/console/mcp-packages/{id}       删包（被引用时 409）
```

全部 `require_all` + `require_trusted_origin`。`console_api.py` 里已有这两个依赖，
照 `/platform/connectors` 的写法挂。

`/probe` 必须限流（每管理员每分钟 6 次）：它每次都会起一个真实进程，是免费的
资源消耗入口。

## 6. 运行期改造

| 文件 | 改动 |
|---|---|
| `integrations/mcp_servers.py` | `Server` 加 `stdio_cwd`；`_from_entry` 解析并校验；`_platform_server_entries` 把 `stdio_command/args/cwd/env` 传给 `_from_entry`（现在只传 url/headers） |
| `integrations/mcp_adapter.py` | `_stdio_key` 纳入 cwd；`_open_slot` 传 `cwd=`；新增 `close_server_sessions(server_id)` |
| `store/platform_catalog.py` | `_TRANSPORTS` 加 `stdio`；`validate_connector` 分支校验（见 §7）；条目落盘新字段 |
| `api/console_api.py` | `PlatformConnectorRequest` 加 5 个字段；新增包管理路由 |
| `api/web/admin.html` | 连接器抽屉（`~1323-1420`）加「传输方式」单选：远程 / 本地进程；本地进程档显示上传框 + 探针 + 只读 cwd |

## 7. 安全清单

**必须（缺一个就不该上线）**

1. `stdio_cwd` 由服务端生成，`resolve()` 后必须 `is_relative_to(MCP_PACKAGE_ROOT)`；
2. `stdio_command` 只接受绝对路径，且 basename 在白名单（`node` / `python3` / `uv` /
   `docker`），`resolve()` 后 `is_relative_to` 允许的解释器目录——防 `/tmp/pwn/python3`；
3. `stdio_args` 拒绝绝对路径与 `..`（arg 里的 `--config /etc/shadow` 也要拦：
   至少禁止 `/etc`、`/proc`、`/sys`、`~/.ssh` 前缀）；
4. 子进程 env **只**给 `PATH` / `HOME`(指到包目录) / `LANG` / `TZ` + 表单里的键，
   **绝不继承**中台的 `DATABASE_URL` / `*_SECRET` / `MCP_*`。当前 `_borrow_stdio`
   从 `dict(server.stdio_env)` 起步是对的，加白名单时别退化成 `{**os.environ, ...}`；
5. 解压不保留符号链接与可执行位，逐条防穿越；
6. 探针超时必须真的杀掉进程组（`start_new_session=True` + `os.killpg`），
   否则被杀掉的只是 await，子进程还在跑；
7. 敏感 env 值接口返回打码（`sk-****abcd`），编辑时留空表示"不修改"。

**建议**

- 子进程降权：`preexec_fn` 切到专用 uid，或整包在容器/`nsjail` 里起；
- `resource.setrlimit` 限 CPU 60s、内存 512 MB、单文件 100 MB、最多 64 进程；
- 出站网络限制：只放行必要域名（很多 MCP 会回调自己的 API）；
- **单 worker**：多 worker 下每个进程各起一份 stdio 子进程，工具清单和进程数都翻倍。
  现在中台如果跑 `--workers N`，stdio 连接器要先确认这一点。

## 8. 依赖策略

一期**只做离线自带**，这是不引入供应链风险的前提：

- 上传包里必须已经有 `node_modules/` 或 `.venv/`；
- 没有就在表单上标「需装依赖」，并给出提示：在外部机器 `npm ci --omit=dev` 后
  连 `node_modules` 一起打包再传；
- 留开关 `MCP_PACKAGE_INSTALL=off|npm|uv`，默认 `off`。开 `npm` 时只能
  `npm ci --ignore-scripts --omit=dev`——**`--ignore-scripts` 不能省**，
  否则 `postinstall` 就是上传即执行。

## 9. 改动清单（按 PR）

**PR1 打通 cwd（可独立合，无 UI）**
`mcp_servers.Server.stdio_cwd` + `_from_entry` + `_platform_server_entries`；
`mcp_adapter._stdio_key` / `_open_slot`；测试：改 cwd 必须换进程。

**PR2 托管包存储**
`store/mcp_packages.py`：解压、校验、索引、删除；`tests/test_mcp_packages.py`
（zip-slip / 符号链接 / 炸弹 / 超限 / 正常包 各一条）。

**PR3 探针**
`integrations/mcp_probe.py`：识别启动方式 + 起进程握手 + 失败归因；
测试用两个假包（正确的、缺依赖的）。

**PR4 接口与前端**
`console_api` 5 个路由 + `admin.html` 抽屉改造。

## 10. 验收剧本

1. 传一个 `node_modules` 齐全的 node MCP → 识别出 `node index.js` → 探针返回 N 个工具 → 保存（此时用户端看不到）→ 发布 → 普通用户连接器页出现该项 → 用户点「+」→ 对话里能真的调用一个工具；
2. 传缺依赖的包 → 探针失败，提示 `Cannot find module 'x'`，表单不预填 command，不产生目录残留；
3. 传一个含 `../../etc/passwd` 的 zip → 400，`MCP_PACKAGE_ROOT` 外无任何写入；
4. 传一个 10 KB 解压出 2 GB 的 zip → 中途中止，目录已清理；
5. 把 `stdio_cwd` 指向 `/etc` → 400；
6. 两个管理员同时传同名包 → 各自 `pkg_id` 独立，不互相覆盖；
7. 删除连接器 → 用户端该项消失，子进程在 1 个空闲周期内退出。

## 11. 明确不做

- 不做「上传即发布」；
- 不做在线改代码（编辑器）；
- 不做自动装依赖（一期）；
- 不做 Node/Python 运行时自动安装——运行时由运维预装，表单只选白名单里的绝对路径；
- 不支持 Windows（服务器侧），路径校验按 POSIX 写。
