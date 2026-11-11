"""专家 · 技能 · 连接器：会话级"插件 + 资源选择"模型（迁移自 WorkBuddy）。

三个东西看着是三套功能，底层是一套模型：

    专家(expert)  = SKILL.md 式人设包：系统提示 + 预设技能/连接器/模型
    技能(skill)   = 磁盘上的目录：SKILL.md + references/ + scripts/
    连接器(connector) = MCP Server（复用 integrations/mcp_servers + mcp_adapter）

会话是选择单元，不是消息：用户在输入框里勾的东西写进 `sessions.addon_selection`，
每一轮由 `addons.resolver` 解析成「这一轮能用的提示 + 技能目录 + MCP server 集合」。

模块划分：

- `skills`  技能库（磁盘唯一事实来源，按需物化进会话工作目录）
- `experts` 专家库（内置目录 + 库里自定义行）
- `catalog` 内置专家种子目录
- `resolver` 每轮解析层（专家默认值 ← 会话显式选择）
"""
