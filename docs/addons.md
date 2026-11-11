# 专家 · 技能 · 连接器（会话级选择）

> 迁移自 WorkBuddy 5.9.2 的「专家 / 技能 / 连接器」模型。逆向分析原文见
> `workbuddy-hub-analysis.md`（附件），这里是**在本项目里怎么落地**。
>
> 一句话：**会话是选择单元，不是消息**。用户在输入框里勾的专家/技能/连接器写进
> `sessions.addon_selection`，每一轮由解析层翻译成"系统提示 + 技能目录 + MCP server
> 集合"；技能正文按需读取，不预加载。

## 1. 统一模型

WorkBuddy 把它们做成三套 UI，底层是**一套"插件 + 资源选择"模型**。本项目照抄这个
结构，但把"插件"换成已有的机制：

| 概念 | WorkBuddy | 本项目 | 代码 |
| --- | --- | --- | --- |
| 技能 | `~/.workbuddy/skills/<name>/SKILL.md` | `<SKILLS_DIR>/<id>/SKILL.md` | `addons/skills.py` |
| 专家 | 带人设的 `SKILL.md` 包 | 系统提示 + 预设技能/连接器/模型（内置目录 + 库行） | `addons/experts.py`、`addons/catalog.py` |
| 连接器 | 带身份与授权的 MCP Server | 已是同一形态：`integrations/mcp_servers` + `mcp_adapter` | `api/connectors_api.py`、`api/mcp_manager_api.py` |
| 会话选择 | `sessions.addon_selection` | `sessions.addon_selection`（TEXT/JSON） | `store/pg_addons.py` |
| 每轮解析 | `resolveConversationConfig` + `resolveMcpServers` | `addons/resolver.py` | 同上 |

没有引入第二套执行引擎：专家只是提示词与默认值，真正的执行仍是 Pi 运行时。

## 2. 数据模型

```sql
-- v22_addons
ALTER TABLE sessions ADD COLUMN addon_selection TEXT NOT NULL DEFAULT '{}';

CREATE TABLE experts (
    expert_id          TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    owner_user_id      TEXT NOT NULL DEFAULT 'default',
    name               TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    system_prompt      TEXT NOT NULL DEFAULT '',
    skill_ids_json     TEXT NOT NULL DEFAULT '[]',
    connector_ids_json TEXT NOT NULL DEFAULT '[]',
    model              TEXT NOT NULL DEFAULT '',
    icon/color/tag     TEXT,
    created_at/updated_at TEXT NOT NULL
);
```

`addon_selection` 的形状：

```json
{
  "expert_id": "exp-research",
  "skill_ids": ["fact-check"],        // 缺这个键 = 用专家预设
  "connector_ids": ["beeftv"],        // 缺这个键 = 沿用旧行为
  "model": ""
}
```

### 为什么技能不建表

WorkBuddy 的 `skills` 表只是磁盘索引。本项目**不建索引表**：磁盘是唯一事实来源，
列表接口每次扫目录（几十个技能的 `stat` 可以忽略）。建索引就要处理"目录在、表里
没有"这类双写不一致，而技能本来就不是需要联表查询的数据。

### 为什么专家要建表

自定义专家要跨前端实例共享、要按租户/用户做归属检查，所以进库。内置专家是代码里
的只读目录（`addons/catalog.py`），**不灌进每张库**——否则升级时无法判断"用户改过的
内置专家"该留谁的版本。

## 3. 解析层（唯一入口）

`addons/resolver.py::resolve()` 每轮执行一次，只读库与磁盘，不发网络请求：

```
addon_selection
   ├── expert_id   → 专家 → 系统提示（人设）+ 预设技能/连接器/模型
   ├── skill_ids   → 技能 → 物化到 <workspace>/.agent-skills/<session>/ + 索引提示
   └── connector_ids → MCP server 集合（交给既有的 connector_scope / 工具目录）
```

**优先级（照 WorkBuddy 的做法）**

| 情形 | 结果 |
| --- | --- |
| 会话显式 `skill_ids` | 覆盖专家预设 |
| 没有 `skill_ids` 键 | 用专家预设 |
| `skill_ids: []` | 明确不要技能（连预设也不要） |
| `skill_ids: null`（PATCH 时） | 删掉显式键 = 恢复专家预设 |
| 没有 `connector_ids` 键 | 沿用旧行为（消息/会话行里的选择） |
| `connector_ids: []` | 本轮不要任何连接器 |

> "没选"和"选了空"必须是两种状态。把 `null` 当 `[]`，用户一点「恢复预设」就变成
> "不要技能"；把缺键当空数组，老会话会突然一个工具都没有。

### 技能物化

每轮把选中的技能拷进 `<workspace>/.agent-skills/<session>/<id>/`，然后**整体重建**
（先删后拷）：

- 模型用现有的 `list_dir` / `read_file` 按相对路径读取，不需要新增宿主工具、
  也不需要给 `read_file` 开第二个根；
- 取消选择后旧目录被清掉，不会出现"取消了还在用"；
- 系统提示里只放**名字 + 描述 + 目录**，正文由模型按需读——这是"少烧 token"的关键。

## 4. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/skills` | 目录 + 磁盘技能合并；条目含 `installed / on_disk / enabled / files` |
| POST | `/api/skills/{id}/install` | 安装：目录条目 → 生成 `SKILL.md` 落盘（幂等） |
| POST | `/api/skills/upload` | 上传 zip / tar.gz / tgz / SKILL.md（multipart `file`） |
| POST | `/api/skills/import` | 把服务器上一个目录变成技能 `{path, name?}` |
| POST | `/api/skills/{id}/enable?enabled=` | 启停软开关（不删目录） |
| DELETE | `/api/skills/{id}` | 删除磁盘技能并从"已安装"摘掉 |
| GET | `/api/experts` | 内置 + 自定义专家 |
| POST | `/api/experts` | 新建自定义专家 |
| PATCH | `/api/experts/{id}` | 改自定义专家（内置只读） |
| DELETE | `/api/experts/{id}` | 删自定义专家（内置 400） |
| GET | `/api/sessions/{sid}/addons` | 当前选择 + 实际生效（dry-run，不碰磁盘） |
| PATCH | `/api/sessions/{sid}/addons` | **聊天框勾选写这里** |

### 上传校验

对齐 WorkBuddy 的 `assertSkillUploadFrontmatterValid`：

- 包里必须有 `SKILL.md`，且 frontmatter 里有 `description`（它是唯一进上下文的元信息）；
- 拒绝 `../` / 绝对路径 / 符号链接（zip/tar 都校验），单文件 5 MB、整包 20 MB；
- 多个 `SKILL.md` 视为歧义，直接拒绝（否则"装哪个"要靠猜）。

### 第一条消息就带选择

新会话还没有会话行，先 `PATCH /addons` 会 404。前端把选择攒在本地，随第一条消息以
`addon_selection` 发出；服务端在 `create_turn` 的**同一个事务**里合并写入——先提交
turn 再单独 UPDATE 会有窗口，Worker 可能抢先领走这一轮。

## 5. 前端

单文件前端（`api/web/index.html`）里新增：

- `#addonChips`：专家/技能胶囊（连接器胶囊沿用 `#connectorChips`）；
- `+ → 专家` / `+ → 技能`：打开会话级选择弹层（**不再跳技能中心整页**）；
- `+ → 连接器`：仍是原来的面板，用户动作后把当前选择回写 `connector_ids`；
- 技能中心整页保留（安装/上传/详情），入口导出为 `window.WB_OPEN_HUB`。

交互约定：

| 动作 | 发送的 PATCH |
| --- | --- |
| 选专家 | `{expert_id}`（不清 `skill_ids`，让预设继续生效） |
| 取消专家 | `{expert_id: null}` |
| 勾/取消技能 | `{skill_ids: [...]}`（当前生效列表 ± 这一项） |
| 恢复专家预设 | `{skill_ids: null}` |
| 连接器增删 | `{connector_ids: [...]}` |

## 6. 与 WorkBuddy 的差异（刻意不照抄）

| WorkBuddy 的做法 | 本项目 | 原因 |
| --- | --- | --- |
| `welcomemode-*` / `interactionmode-*` 插件包 | 专家目录的 `tag` | 没有插件市场，不需要为两个页面做插件抽象 |
| 云端双份技能资产（云 list + 本地副本） | 磁盘单一来源 | SkillHub 已是"下载 zip"模型，双份只会产生对不上的版本 |
| `skills` 索引表 | 扫目录 | 避免双写不一致（见 §2） |
| `fullAccess` 无审批语义 | 工具政策照常生效 | 定时/专家任务也以用户身份跑，不能借专家绕过确认门禁 |
| MCP 配置指纹审批 | 自定义 MCP 仍只允许 HTTP/SSE | `mcp_manager_api` 的既有边界不变：stdio 进程属于运维配置 |

## 7. 运维与验收

- 环境变量：`SKILLS_DIR`（默认 `profile/skills`）、`SKILLS_WORKSPACE_DIRNAME`
  （默认 `.agent-skills`）；迁移版本 `v22_addons`，启动时 `verify_schema()` 会核对。
- 磁盘布局：

```text
profile/skills/
├── .state.json            # {"<id>": {"enabled": false}}，启停软状态
├── deep-research/SKILL.md
└── my-upload/{SKILL.md, references/, scripts/}
```

- 用例：
  - `tests/test_addons_skills.py`：上传/校验/启停/物化（无库）；
  - `tests/test_addons_resolver.py`：优先级与 null/[] 语义（PG）；
  - `tests/test_addons_api.py`：HTTP 接口、归属隔离、首条消息携带（PG）；
  - `tests/test_addons_chat.py`：聊天 turn 真的把专家/技能交给模型（假 store）；
  - `tests/test_addons_e2e.py`：接纳 → Worker 执行 → 技能落进工作目录（PG + 受控模型）；
  - `tests/test_addons_ui.py`：单文件前端的接线约束（源码级）。

```sh
PG_TEST_DSN=postgresql://…/agent_platform_test PYTHONPATH=src \
  .venv-pi-test/bin/python -m pytest tests/test_addons_*.py -q
```

## 8. 已知边界

- **只在 chat/query 与业务 run 两条路解析**；控制类 turn（暂停/取消/补充）不加载
  专家与技能，它们不调模型。
- 技能里的 `scripts/` 需要宿主有对应的执行能力（当前模型读出来照做，不自动执行）。
- 连接器选择仍是客户端 localStorage 为准（历史实现），服务端只保存回写结果；
  换设备时不会自动带过去。
- 技能包不做服务端安全扫描（WorkBuddy 走 `uploadScanPackage`）；当前只有结构校验。
