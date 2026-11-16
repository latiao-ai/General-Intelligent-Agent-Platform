---
name: 集成栈 Pi Runtime
description: 安静的调度台 — 纸与墨、一个靛青信号色的作业界面
colors:
  indigo: "#33557f"
  indigo-deep: "#28436a"
  indigo-wash: "#eef2f8"
  paper: "#ffffff"
  rail: "#f4f6f9"
  sunken: "#eef1f5"
  line: "#e3e8ef"
  line-strong: "#cfd8e4"
  ink: "#16202e"
  ink-secondary: "#42505f"
  muted: "#5d6b7a"
  ok: "#17795f"
  warn: "#8a5a11"
  danger: "#b03a3a"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Segoe UI', Roboto, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "0em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Segoe UI', Roboto, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Segoe UI', Roboto, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.65
    letterSpacing: "0em"
  secondary:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Segoe UI', Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "0em"
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Segoe UI', Roboto, sans-serif"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0.01em"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'PingFang SC', monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "0em"
rounded:
  sm: "6px"
  md: "8px"
  lg: "10px"
  xl: "12px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "6px"
  md: "8px"
  lg: "12px"
  xl: "16px"
  2xl: "20px"
  3xl: "24px"
  4xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.indigo}"
    textColor: "{colors.paper}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "0 12px"
    height: "30px"
  button-primary-hover:
    backgroundColor: "{colors.indigo-deep}"
    textColor: "{colors.paper}"
    rounded: "{rounded.md}"
    height: "30px"
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink-secondary}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "0 12px"
    height: "30px"
  input-composer:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xl}"
    padding: "11px 12px"
  chip:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink-secondary}"
    typography: "{typography.secondary}"
    rounded: "{rounded.pill}"
    padding: "0 12px"
    height: "28px"
  card:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "12px 14px"
  nav-item:
    textColor: "{colors.ink-secondary}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "0 10px"
    height: "30px"
  nav-item-active:
    backgroundColor: "{colors.indigo-wash}"
    textColor: "{colors.indigo-deep}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    height: "30px"
---

# Design System: 集成栈 Pi Runtime

## Overview

**Creative North Star: "The Quiet Console（安静的调度台）"**

这是一套作业界面，不是展示界面。它假设用户每天都在这里，脑子里面装着任务，不想被界面说服、也不想被界面表扬。所以整套视觉只做三件事：把当前状态说清楚、把下一步放在手边、把已经发生的事留痕。其余一切让位。

材料上是纸与墨：白纸、浅灰轨道、一条发丝线分区，没有玻璃、没有渐变、没有光晕。强调色只有一支靛青（`#33557f`），它不承担装饰功能，只标记"这个地方有事发生"——执行中、失败、待批准、当前选中。一支颜色被用得少，它说话才有人听。

手感定为**温润亲近**：控件 28–30px 高、圆角 8–12px、轻描边而非重投影；卡片悬停时上浮 2px、带一层极浅投影，像是纸被手指抬了一下。这与"调度"的冷静并不冲突——冷静来自留白和克制，不来自硬边和冷灰。

**Key Characteristics:**
- 纸白底 + 浅灰轨道，分区靠 1px 发丝线，不靠阴影
- 单一靛青信号色，任一屏占比 ≤10%
- 字号底线 11px，正文 13px，相邻字阶差 ≥1.25×
- 平放即默认状态；层次和反馈只在交互时出现
- 圆角四档（6/8/10/12px）+ 胶囊，一个元素只用一档

## Colors

一支靛青做信号，其余全是纸、墨与线——克制到近乎单调，正是这套系统想要的效果。

### Primary
- **调度靛青** (`#33557f`, 7.65:1 on white): 主按钮、当前选中、进度条、关键链接。它出现的地方就等于"这里可以动手"或"这里正在发生"。白字打在它上面同样是 7.65:1，主按钮不需要再加边框区分。
- **深靛（按下与悬停）** (`#28436a`, 9.99:1 on white): 主按钮 hover/active、选中态文字。比主色深一档，靠明度而非色相变化表达反馈。
- **靛青洗底** (`#eef2f8`): 选中行、选中导航项、强调区块的底。只用底色，不加边框，避免"选中"和"聚焦"两种语义撞在一起。

### Neutral
- **纸** (`#ffffff`): 主内容底、卡片底、输入框底。
- **轨道** (`#f4f6f9`): 侧栏底。侧栏永远比内容区暗一档，让主内容成为最亮的面。
- **内凹** (`#eef1f5`): 输入框内部凹槽、悬停底、代码块底。
- **发丝线** (`#e3e8ef`): 分区线、卡片描边、表格行线。1px，永远不加深成 2px。
- **强线** (`#cfd8e4`): 只在需要"可点"暗示的边缘使用——输入框、次要按钮、胶囊。
- **墨** (`#16202e`, 16.4:1): 标题、正文主体。
- **次墨** (`#42505f`, 8.25:1): 正文次要段、未选中的导航文字。
- **雾墨** (`#5d6b7a`, 5.46:1 on white / 5.04:1 on rail): 时间戳、单位、辅助说明。**这是最浅的一档文字**，再浅就不合规了。

### Semantic
- **成** (`#17795f`, 5.33:1)、**警** (`#8a5a11`, 5.91:1)、**败** (`#b03a3a`, 5.98:1)。三者只用于状态文字与状态点，不做整块背景填充；需要底色时用同色 8% 的洗底。

### Named Rules
**The One Signal Rule.** 靛青在任一屏的可见占比不超过 10%。它稀缺，所以它每次出现都被当真。一个屏幕里如果靛青到处都是，说明有地方该用墨或线却偷懒用了品牌色。

**The Single Source Rule.** 一个页面只能有一份 `:root` 令牌。本项目历史上出现过四份（橙红 `#ef4b36`、蓝 `#185fa5`、青绿 `#00c29a`、深青 `#0d7480`）互相覆盖，结果是"最后一个声明赢"，没人知道品牌色是什么。这四色**全部作废**，本文件的靛青是唯一口径。

**The Faint-Is-A-Limit Rule.** `#5d6b7a` 是可读性的下限，不是可选的浅色。任何比它更浅的文字（如 `#a9aeb7`、`#9aa0a8`）都在 3:1 以下，只能用作禁用态或纯装饰的图标描边，绝不能承载信息。

## Typography

**Display Font:** 系统 UI 栈 — `-apple-system / SF Pro Text / PingFang SC / Microsoft YaHei`（无网络字体依赖）
**Body Font:** 同一栈
**Label/Mono Font:** `ui-monospace / SF Mono / Menlo`（编号、耗时、工具名、ID）

**Character:** 只有一套字族，靠字号与字重分层，不靠换字体制造对比。中文走苹方/雅黑，西文数字跟随系统，等宽只在"这是一个标识符"时出现。字形本身不表演，密度和留白才是它的语气。

### Hierarchy
- **Headline** (600, 20px, 1.35): 页面级标题。一屏最多一个，出现在内容区顶部左侧，不居中。
- **Title** (600, 16px, 1.4): 分区标题、卡片组标题、对话块标题。
- **Body** (400, 13px, 1.65): 正文、对话气泡、表单标签。行宽控制在 38–46 个汉字（约 65–75ch）。
- **Secondary** (400, 12px, 1.6): 描述、辅助说明、列表副行。仍须 ≥4.5:1。
- **Label** (500, 11px, 1.4, `letter-spacing: 0.01em`): 时间戳、快捷键、单位、计数。**11px 是底线，不是起点。**
- **Mono** (400, 12px, 1.6): 编号（`run_7f3a`）、耗时（`1.8s`）、工具名（`shot.list`）。数字用等宽以保证纵向对齐。

### Named Rules
**The 11px Floor Rule.** 任何承载信息的文字不小于 11px。现状里 10.5px 的功能文字（账号说明、状态提示）必须提到 11px 并把对比度补到 4.5:1。

**The One-Step Rule.** 相邻两级字号差至少 1.25×。现状 15/14/13.5px 三级挤在一起（1.07:1），等于没有层级——标题只能靠加粗跟正文区分，这是列表页读起来累的根因。

## Layout

一个固定的左侧轨道 + 一块流动的工作区。轨道 `264px`，不随内容变化，可折叠为 0；工作区占满剩余宽度，内部再分"顶栏 / 滚动主体 / 底部输入"三段，只有中间那段滚动。

主体内容有两档宽度：对话与阅读面 `max-width: 760px` 居中（长文本再宽就难读）；列表与表格面 `max-width: 1200px` 靠左，让密度铺开。外层 padding 24px（窄屏 14–20px）。

密度是紧凑的：区块间距 24–32px，块内间距 8–12px，行间 6–8px。垂直节奏只从 `4 / 6 / 8 / 12 / 16 / 20 / 24 / 32` 这八档里取，不出现 13px、18px 这类随手值。

断点：`1280px` 收窄 padding；`1024px` 侧栏可折叠为图标条；`768px` 侧栏转为抽屉；`390px` 单列、按钮全宽、输入区吸底。任何宽度下都不允许横向滚动。

**The One Scroll Rule.** 一屏只有一条滚动条，落在主体区。顶栏、侧栏、底部输入固定不动——用户在长对话里永远能找到输入框。

## Elevation & Depth

这套系统**默认是平的**。分区靠 1px 发丝线（`#e3e8ef`）和底色明度差（纸 / 轨道 / 内凹三档），不靠投影。页面里任何常驻的阴影都是可疑的。

阴影只作为**状态回应**出现，共三档：

### Shadow Vocabulary
- **抬起** (`0 1px 2px rgba(16,24,40,.05)`): 卡片默认可以带这一层，只为把纸和纸分开一点点，几乎看不见。
- **悬停抬起** (`0 8px 18px -10px rgba(18,38,46,.20), 0 2px 4px rgba(18,38,46,.04)`): 可点击卡片 hover 时配 `translateY(-2px)`，是"这张纸被抬起来了"的唯一表达。
- **浮层** (`0 28px 64px -28px rgba(18,38,46,.42)`): 弹窗、下拉、命令面板。只有真正脱离文档流的元素才配得上它。

### Named Rules
**The Flat-By-Default Rule.** 静止状态不投影；投影只回答交互（hover、浮层）。滚动时出现/消失的投影也不允许——它会让页面在滚动中"呼吸"，抢走对内容的注意。

**The No-Colour-Glow Rule.** 阴影永远是中性灰黑，不带品牌色。现状里 `#ef3d2b` 的彩色光晕是反例：它把"发光"当成了强调，而强调应该交给靛青的实色。

## Shapes

**圆角四档，各有明确归属：**

- `6px` — 小控件：标签、徽章、键盘键帽、图标按钮
- `8px` — 标准控件：按钮、导航项、二级卡片
- `10px` — 容器：卡片、面板、代码块
- `12px` — 大容器：输入框外壳、抽屉、弹窗
- `999px` — 胶囊：筛选 chip、状态标签、头像框

**一个元素只用一档。** 不允许外层 12px、内层又 6px 的套娃减角——那是把圆角当装饰。嵌套容器要么同档，要么内层不设圆角靠内边距退让。

描边统一 1px，颜色取 `#e3e8ef`（分区）或 `#cfd8e4`（可点边缘）。不用 0.5px（非整数缩放下会消失），不用 2px（在这个密度下会显得油）。

### Named Rules
**The One Radius Rule.** 圆角表达的是"这个东西的物理量级"，不是风格。同一量级的元素给同一个值；两个同级元素圆角不同，就是错的。

## Components

### Buttons
- **Shape:** 温润方角（8px），高 30px，左右内边距 12px
- **Primary:** 实底靛青（`#33557f`）、白字、13px/500，无边框、无阴影；hover 转深靛（`#28436a`）
- **Secondary:** 白底、`#cfd8e4` 描边、次墨文字；hover 描边转 `#b9c9cf`、底色保持白
- **Ghost:** 无底无框，文字取次墨；hover 出现内凹底（`#eef1f5`）
- **Danger:** 文字或描边取败色，默认不做实底红按钮
- **States:** `active` 下压 0.5px；`disabled` 整体透明度 0.45 且不可点；transition 140ms `cubic-bezier(.22,.72,.32,1)`
- **尺寸:** 小号 28px（表格行内、卡片角上），标准 30px，无大号——这是作业界面，不需要主视觉按钮

### Chips
- **Style:** 胶囊（999px）、高 28px、白底、`#cfd8e4` 描边、次墨 12px
- **State:** 未选中白底；选中转靛青洗底（`#eef2f8`）+ 深靛文字 + 35% 靛青描边，**不做实底**——实底会跟主按钮抢权重

### Cards / Containers
- **Corner Style:** 10px
- **Background:** 纸白；列表行悬停转内凹（`#eef1f5`）
- **Shadow Strategy:** 静止用"抬起"那一档或干脆不用；可点卡片 hover 走"悬停抬起"+ 上浮 2px
- **Border:** 1px `#e3e8ef`，与阴影二选一，不叠加
- **Internal Padding:** 12px 14px（紧凑卡）/ 16px 20px（内容卡）

### Inputs / Fields
- **Style:** 白底、1px `#cfd8e4`、12px 圆角、内部 11px 12px；占位符用雾墨
- **Focus:** 描边转靛青 + `0 0 0 3px rgba(51,85,127,.10)` 外环，**不改变尺寸**（不允许 focus 时布局跳动）
- **Error:** 描边转败色，错误文字 12px 紧跟其下，取败色；错误只出现在自己那个字段下面
- **Disabled:** 底转内凹、文字转雾墨、不做半透明整体处理

### Navigation
- **Style:** 侧栏白/轨道底，条目高 30px、8px 圆角、13px、次墨；图标 16px 线性，与文字间距 8px
- **States:** hover 出内凹底 + 墨色文字；当前项用靛青洗底 + 深靛文字（不用左侧竖条，不用加粗）
- **Grouping:** 分组标题用 11px 雾墨大写间距标签，与条目间距 6px；分组之间留 16px
- **Mobile:** 768px 以下转抽屉，遮罩 `rgba(17,21,27,.3)`，从左侧滑入，带关闭按钮

### Signature: 任务台空状态
不再是营销首屏。空状态 = **输入框自动聚焦** + 最近 3 个任务（标题、相对时间、状态点）+ 一行 `能做什么` 的文字链接。没有大标题、没有 01–04 编号格、没有口号。用户打开就该能立刻开始打字；想找上次的活，往下看一眼就有。

## Do's and Don'ts

### Do:
- **Do** 用 1px `#e3e8ef` 发丝线分区，让留白承担结构。
- **Do** 让靛青只出现在状态与动作上，占比 ≤10%（The One Signal Rule）。
- **Do** 所有文字 ≥11px，正文 13px，相邻字阶差 ≥1.25×。
- **Do** 一页一份 `:root`，令牌只在 `src/webpage_agent/api/web/` 的共享样式里定义一次。
- **Do** 给每个异步状态留位置：排队、执行中、失败、待批准、已受理未完成——五种都要有各自的文案和颜色。
- **Do** focus 状态用外环（`0 0 0 3px rgba(51,85,127,.10)`）补齐，键盘用户必须看得见自己在哪。

### Don't:
- **Don't** 在应用界面里用营销式宣言标题（如"给一个主题，拿回一份带来源的报告"）。页面标题写清楚这是什么就行："新任务"。
- **Don't** 叠加第二份 `:root` 覆盖。历史上四色互盖、`.btn` 高度被改五次（30→32→28→32→26px），这是所有视觉不一致的源头。
- **Don't** 用彩色光晕阴影、常驻脉动点、滚动出现/消失的投影做强调。
- **Don't** 用比 `#5d6b7a` 更浅的文字承载信息（`#a9aeb7`、`#9aa0a8` 实测只有 2.5–3:1）。
- **Don't** 把"已受理"写成"已完成"，或把"停止后续步骤"写成"已撤销"。
- **Don't** 给同级元素不同圆角，或做套娃减角。
- **Don't** 用紫罗兰/薰衣草色系——本项目已定靛青，不再引入第二个冷色信号。
