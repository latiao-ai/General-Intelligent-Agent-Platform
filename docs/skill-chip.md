# 消息级技能胶囊（skill chip）

> 迁移自 WorkBuddy 的「技能胶囊」：把技能做成一个**不可编辑的内联 content block**，
> 插进聊天输入框（富文本编辑器里的原子节点），发送时拍平成 `content_blocks` +
> 纯文本，服务端据此让**这条消息**真正启用该技能。

## 与「会话级技能选择」的区别（别混）

| | 会话级（addon_selection） | 消息级（content blocks） |
| --- | --- | --- |
| 存哪 | `sessions.addon_selection`（SQLite/PG 列） | `messages.content_blocks`（本条消息） |
| 生命周期 | 跨轮次，切会话还在 | 随这条消息发出去，发送后清空 |
| 生效 | 每轮 `resolve_turn` 解析工具集 | 本轮 `resolve_turn` 把点名的技能**追加**进 `skill_ids` |
| UI | 输入框上方胶囊 / 会话技能按钮 | 输入框内联 chip |
| 语义 | 「会话里可用」 | 「这一轮请用这个技能」（更强信号） |

两者可同时存在：会话挂了 A 技能，这条消息又 @ 了 B 技能，本轮 A、B 都会物化；
B 会在系统提示里被标成「消息级点名，优先执行」。

## 数据模型（block）

```jsonc
{
  "type": "resource_link",
  "name": "skill://excel",      // 或 command://excel（/ 命令入口）
  "uri": "skill://excel",
  "title": "Excel 表格处理",
  "_meta": {
    "mentionType": "skill",
    "type": "skill",
    "displayText": "Excel 表格处理",
    "skillId": "excel",
    "trigger": "at"             // at（@ 提及 / + 菜单）| slash（/ 命令）
  }
}
```

纯文本块 `{ "type": "text", "text": "..." }`。发送时前端同时产出：

- `message`：纯文本，胶囊处替换成 `@skill:<id>`（模型可读、后端可兜底解析）；
- `content_blocks`：结构化 blocks（只在含胶囊时随请求发送）。

## 前端（`api/web/index.html`）

- 输入框 `#msg` 由 `<textarea>` 改成 `contenteditable` 原子节点（零依赖，文档路线 B）。
- 三个入口共用一套 blocks 与同一个菜单：
  1. `+` 菜单 →「技能」：点开技能菜单，点选插入胶囊；
  2. `@` 提及：输入 `@` 弹出技能补全，选中替换 token 为胶囊；
  3. `/` 命令：输入 `/` 弹出命令技能，选中插入 `command://<id>` 胶囊。
- 序列化 / 回显 / 删除 / 粘贴 / 输入法边界都在 `<script id="wb-skill-chip-js">` 里，
  对外暴露 `window.WB_COMPOSER`、`window.WB_BLOCKS_HTML`、`window.WB_OPEN_SKILL_CHIP`。
- 会话级选择仍保留在输入框下方「会话技能」按钮（`#addonSkillBtn`）。

## 接口契约

`POST /api/sessions/{sid}/messages` 新增可选字段：

```jsonc
{
  "request_id": "...",
  "message": "帮我做表 @skill:excel",
  "content_blocks": [
    {"type": "text", "text": "帮我做表 "},
    {"type": "resource_link", "uri": "skill://excel", "_meta": {"skillId": "excel", "displayText": "Excel 表格处理", "mentionType": "skill"}}
  ]
}
```

时间线 / 历史返回的消息带 `content_blocks`（缺省为 `[]`），前端据此只读回显胶囊。

## 后端解析

- `addons/resolver.extract_message_skill_ids(message, content_blocks)`：优先读结构化
  blocks 的 `skill://<id>` / `command://<id>` 与 `_meta.skillId`，兜底扫正文里的
  `skill://<id>` / `@skill:<id>` 标记；只返回合法 id（去重、保序）。
- `resolve(..., message_skill_ids=...)`：把消息级 id 去重追加进 `skill_ids` 再物化，
  不改变 `skills_explicit`（那描述的是会话级语义）。
- `addons/skills.render_prompt(materialized, message_skill_ids)`：在技能索引里补一句
  「用户通过技能胶囊明确点名了 …，优先执行」，缺失时如实提示不可用。

## 迁移

`v28_message_content_blocks`：`messages` 增 `content_blocks TEXT NOT NULL DEFAULT ''`。
存量库跑 `python -m scripts.migrate`；新库由 SCHEMA 快照直接建。
