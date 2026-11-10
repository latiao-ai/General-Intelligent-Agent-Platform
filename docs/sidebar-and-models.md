# 左侧栏、工作空间与模型选择

> 目标：拆解 WorkBuddy 左侧栏的「置顶 / 任务 / 空间」三段式与折叠、输入框里的「选择工作空间」和「模型选择」，并在本项目里实现同等效果。

## 1. 左侧栏：三个可折叠的大类

WorkBuddy 的侧栏是「三段式 + 计数 + 折叠」：

```
置顶 (3)        ⌄
  置顶的会话…
任务 (20)       ⌄
  会话列表（默认 20 条，查看更多）
空间 (4)        ⌄
  ▸ demo demo
  ▸ BeefTV
  ▸ Toonflow-app
  ▸ cli相关
```

设计点：

1. **每个大类都有计数和折叠箭头**，折叠后只留标题，侧栏不会被长列表撑满；
2. **任务默认只显示一页**（20 条），底部「查看更多」再展开，避免 50+ 条把「空间」挤到屏幕外；
3. **空间是文件夹**，每个空间可以单独展开/收起，展开后显示这个空间里的任务；
4. **空间右侧有「＋」**，新建空间；标题右侧有「展开全部」图标（WorkBuddy 的 `↗`）；
5. **置顶是横切分组**：置顶的会话从任务列表里抽出来放最上面，和任务分组用分隔线隔开。

![侧栏分组](assets/workbuddy/sidebar-groups.png)

## 2. 输入框里的「选择工作空间」

WorkBuddy 的「选择工作空间」和左侧「空间」是**同一组实体**：

- 面板列表就是侧栏的空间（Toonflow-app、demo demo、BeefTV…）；
- **新建工作空间**：只创建一个自命名的空间，不选择文件夹；
- **打开本地文件夹**：选择本机目录，绑定到这个工作空间；
- 选中后输入框标签、侧栏高亮同步；
- 绑定目录时后端记录工作路径；未绑定目录的空间只用于归档会话。

本项目的对应实现：

- 输入框的 `#wsPill` 打开 `#wsPicker`：搜索、工作空间列表、当前项打勾；
- 服务端 `/api/workspace` 返回**当前登录用户自己的**命名空间，登录后合并进左侧「空间」；
- 「新建工作空间」→ `POST /api/workspace/namespace`，按用户私有目录创建；
- 切换工作空间 → `POST /api/workspace/select`；
- 「打开本地文件夹」→ `/api/workspace/pick` 原生目录框，绑定当前用户自己的路径；
- 本地空间/任务归属按用户分键存储：
  `wb.spaces.v2.<user_id>`、`wb.assign.v2.<user_id>`、`wb.workspaces.v2.<user_id>`；
  切换账号会重新加载，不会复用上一个账号的侧栏；
- 列表不再显示「私有命名空间」副标题，只有名称；
- BeeFTV 画布模式（`data-workspace="true"`）仍保留原来的 `#workspace` 浮层，和本地工作空间不冲突。

## 3. 模型选择

WorkBuddy 的模型选择是一个胶囊按钮：

```
[● DeepSeek-V4.1-Flash ⌄]
```

点开列出可用模型，当前项打勾；切换后下一轮对话使用新模型，选择会记住。

本项目实现：

- 输入框右侧 `#modelPill` 从静态文字改成按钮；
- 点开 `#modelMenu` 列出 `AVAILABLE_MODELS`；
- 选择后 `POST /api/models/select`，后端更新 `settings.llm_model` 并写入 `profile/model.json`；
- 进程启动时 `restore_model()` 恢复上次选择；
- 下一轮聊天/任务使用新模型（PiRuntime / LangGraph 都从 `settings.llm_model` 取）；
- 支持 `deepseek-v4-1-flash`、`deepseek-chat`、`deepseek-reasoner`（可在 `.env` 的 `AVAILABLE_MODELS` 增减）。

![模型选择](assets/workbuddy/model-menu.png)

### 接口

```text
GET  /api/models          → {current, models:[{id,name}]}
POST /api/models/select   → {current, name}   请求体 {"id":"deepseek-chat"}
```

### 配置

```env
AVAILABLE_MODELS=deepseek-v4-1-flash,deepseek-chat,deepseek-reasoner
```

## 4. 本次改动

| 位置 | 改动 |
| --- | --- |
| 侧栏 | `置顶/任务/空间` 标题可点击折叠，带箭头和计数；状态存 `wb.groups.v1` |
| 任务列表 | 默认 20 条，底部「查看更多（N）」/「收起」 |
| 空间 | 保留文件夹展开/收起、新建空间、任务归属 |
| 模型 | 新增 `#modelPill` 菜单、`/api/models`、`/api/models/select`、`profile/model.json` |

## 5. 边界

- 模型切换是**进程级全局**（本地单用户够用），不是每个会话独立；多租户应改成会话级字段；
- 切换模型后正在跑的任务不会中途换模型，下一轮生效；
- 服务端命名空间按用户隔离；本地空间/任务归属按 `.v2.<user_id>` 分键，同一浏览器切换账号不会串；
- 本地文件夹工作空间绑定的是当前用户的路径；未绑定目录的空间只归档会话；
- `AVAILABLE_MODELS` 必须与 `LLM_BASE_URL` 网关实际支持的模型一致，否则下一轮请求会失败。
