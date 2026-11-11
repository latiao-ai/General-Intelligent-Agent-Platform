# 连接器工具调用、结果侧栏与链接返回

## 1. WorkBuddy 的底层做法

WorkBuddy 不是“猜到用户要画布就直接生成”，而是四段配合：

### 1.1 连接器安装后暴露真实工具

LibTV 插件提供：

```text
mcp.json → https://mcp.liblib.tv/mcp
```

连接成功后，真实 MCP 工具（create_project、canvas nodes、generation 等）
进入模型工具目录；模型根据工具名、描述和 schema 判断能做什么。

### 1.2 提示词/技能描述使用方式

LibTV 插件的 Skill 文档写明：

- 用 `list_projects` 定位项目；
- 用 `target_project` 绑定；
- 创建画布/节点；
- 付费生成前 `run=false` 预检；
- 用户要看时，用 `makeVisible=true` 返回的 URL 打开。

所以“意图识别”主要来自：

```text
工具描述 + 连接器提示词 + 当前用户请求
```

而不是一个单独的意图分类器。

### 1.3 工具结果带资源身份

LibTV 工具返回结构化字段：

```json
{
  "projectId": "...",
  "canvasUrl": "https://www.liblib.tv/canvas?...",
  "nodeId": "...",
  "imageUrl": "https://..."
}
```

模型把这些字段写进回答，所以用户能看到画布入口和图片直链。

### 1.4 宿主打开结果视图

WorkBuddy 宿主有 `open_result_view` / `preview_url`：

```text
工具返回 makeVisible=true
→ 宿主打开右侧结果视图/内嵌浏览器
```

`open_result_view` 打开本地文件；`preview_url` 打开网页。
这是**宿主 UI 能力**，不是 MCP 服务端直接控制界面。

## 2. 中台迁移

### 2.1 工具注入

当前会话选中连接器后，服务端把该连接器的全部工具注入 Pi：

```text
create_project
canvas.*
generation.*
...
```

模型能直接调用，不需要额外意图分类器。

### 2.2 提示词约束

连接器上下文新增：

```text
用户要求创建画布/节点、生成图片/视频、查看作品时，必须先调用工具，
不要只给步骤或建议；projectId、canvas URL、node ID、媒体 URL 要原样展示。
```

### 2.3 工具结果事件

Pi 执行工具成功后发出：

```text
tool_result
{
  "name": "mcp__libtv__create_project",
  "urls": ["https://www.liblib.tv/canvas?...", "https://...png"],
  "make_visible": true
}
```

- 递归收集结果里的 http(s) 链接；
- 跳过 token/authorization/cookie 等字段；
- `makeVisible` / `make_visible` 透传；
- 通过既有 SSE 事件通道送到前端。

### 2.4 结果侧栏

前端新增右侧「结果预览」：

- 图片 URL 直接显示预览；
- LibTV/外部链接显示「打开 LibTV 查看结果」；
- `makeVisible` 或链接命中 liblib.tv/图片时自动打开；
- 可关闭，不阻塞聊天。

> 注意：LibTV 站点返回 `X-Frame-Options: SAMEORIGIN`，普通 Web 页面不能把它
> 嵌进 iframe。WorkBuddy 能用 Electron 内嵌浏览器，中台 Web 版用“结果卡片 +
> 新窗口打开”近似；不伪装成内嵌画布。

## 3. 当前能达到的效果

```text
用户：帮我创建一个浙江画布，并生成杭州图片节点
→ 模型看到 LibTV 工具
→ 调用 create_project / canvas.nodes.create / generation
→ tool_result 带 canvas URL / image URL
→ 聊天回答展示 projectId、nodeId、链接
→ 右侧结果侧栏显示图片、打开画布入口
```

与 WorkBuddy 的差异：

- 没有 Electron 内嵌浏览器，不能直接显示 liblib.tv 页面；
- 付费生成仍受高风险确认门禁；
- 具体字段形状由 MCP 服务端返回，前端只提取 URL，不伪造 ID。

## 4. 测试

- URL 提取只保留 http(s)，跳过敏感字段；
- `makeVisible` / `make_visible` 识别；
- SSE `tool_result` 事件进入会话事件表；
- Web 页面加载无 JS 错误。
