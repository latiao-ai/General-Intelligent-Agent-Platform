# 前端设计系统（paper & ink）

> 2026-11-13。五个单文件页面统一到一套设计令牌与组件规则上。
> 上一版改造记录见 [workbuddy-frontend.md](workbuddy-frontend.md)（已并入 index.html，记录留档）。

## 1. 为什么是「注入一层」而不是重写页面

`index.html` 10280 行、`admin.html` 4239 行，页面里已经叠了三层历史样式覆盖
（`wb-workbuddy-theme`、`wb-skillhub-polish` 等）。直接重写会丢功能，也会和正在改的
后端接口对不上。

所以这一版只做两件事：

1. 在 `<head>` 末尾注入一段 `<style id="taste-layer">`，它在层叠里永远最后生效；
2. 给 `<html>` 补一个 `data-page="hub|bots|plaza|messaging|admin"`，用来分页面作用域。

**JS、DOM 结构、路由、接口全部不动。** 唯一的 HTML 结构改动是 index 的首屏
（见第 4 节），因为那一块的问题不是换颜色能解决的。

源文件与工具：

| 文件 | 作用 |
|---|---|
| `scripts/frontend/taste-layer.css` | 样式唯一真源，改这里 |
| `scripts/frontend/taste-layer.js` | 主题切换 + 展示层收口（首帧定深浅、插按钮、改裸露的内部 ID） |
| `scripts/frontend/fonts/vendor/*.woff2` | 字体源文件（latin 子集，SIL OFL） |
| `scripts/frontend/build_font_css.py` | 把 woff2 压成 base64 的 `fonts.css` |
| `scripts/frontend/apply_frontend_skin.py` | 把三块写进五个页面（幂等） |

改完样式后：

```bash
python3 scripts/frontend/build_font_css.py              # 仅字体换版本时需要
python3 scripts/frontend/apply_frontend_skin.py         # 写入五个页面
python3 scripts/frontend/apply_frontend_skin.py --check # CI 用：和源不一致就退出 1
```

页面是服务端每次读文件返回的（`Cache-Control: no-store`），所以**不用重启 uvicorn**，
刷新即生效。

## 2. 令牌

调色来自设计分配器对「AI 产品 / 开发者工具」这一行业的输出（`cool-white-slate`），
把营销页需要的那几个令牌扩成了应用需要的一整套。全部定义在 `:root` 的
`--t-*` 命名空间里。

| 令牌 | 值 | 用在哪 | 对比度（白 / 纸 / 轨） |
|---|---|---|---|
| `--t-page` | `#ffffff` | 主内容底 | — |
| `--t-rail` | `#f4f7f8` | 侧栏 / 轨道 | — |
| `--t-paper` | `#f7f9fa` | 独立页纸底、登录提示条 | — |
| `--t-sunken` | `#eef3f5` | 输入内凹、悬停 | — |
| `--t-line` / `--t-line-2` | `#e4ebee` / `#d3dfe4` | 分隔线 / 控件描边 | — |
| `--t-ink` | `#16252c` | 正文、标题 | 15.7 / 14.9 / 14.6 |
| `--t-ink-2` | `#42565e` | 次级文字 | 7.7 / 7.3 / 7.2 |
| `--t-muted` | `#5d6b72` | 说明文字 | 5.5 / 5.2 / 5.1 |
| `--t-faint` | `#60727a` | 10–11px 辅助字 | 5.0 / 4.75 / 4.66 |
| `--t-accent` | `#0d7480` | 唯一信号色：主按钮、链接、焦点环 | 5.5 / 5.2 / 5.1 |
| `--t-surface` | `#ffffff` | 卡片 / 浮层 / 控件表面（暗色下翻成暗面） | — |
| `--t-on-accent` | `#ffffff` | 压在 accent 实心上的文字 | — |
| `--t-accent-2` | `#5aa3ab` | 同色相第二档，只给图表的第二条序列 | — |

旧令牌（`--bg / --panel / --line / --ink / --brand / --shadow-1` 等）通过
`html[data-page]{...}` 重映射到新值，所以一万八千行既有 CSS 不用逐条改就跟着走。
admin 是「纸上的白面板」，底与面板的映射和其余四页相反，单独一条规则。

**圆角**只有四档：6（控件）/ 10（小卡片）/ 12（卡片）/ 16（浮层）。
**阴影**只给浮层，卡片一律靠 1px 线分开，避免整页「浮起来」。
**动效**：140ms / 220ms，`cubic-bezier(.22,.72,.32,1)`；按钮按下位移 0.5px、
可点卡片抬 2px、焦点环 2px 且只对键盘出现；`prefers-reduced-motion` 全停。

## 3. 各页面改了什么

| 页面 | 主要变化 |
|---|---|
| `index.html` | 三色统一到青绿；首屏重排（见第 4 节）；侧栏、顶栏、会话、Composer、弹层、技能/连接器内页、灵感社区空态、侧栏空间入口 |
| `bots.html` | 橙红渐变 → 青绿；空态从「居中一坨」改为贴左上角的编辑式块；未选中时右侧空白详情栏不再占 312px |
| `plaza.html` | 橙红 → 青绿；筛选条的白色胶带改成纸色渐变；空态从整条灰带改成白卡片；页面左右边缘对齐到同一条线 |
| `messaging.html` | 橙红 → 青绿；提示条从通栏粉带改成 760px 宽描边告警；卡片、表单、渠道列表 |
| `admin.html` | 蓝色 → 青绿；标签页可横向滚动（窄屏不再切掉半个词）；表格行悬停、面板、表单动作行可换行 |

顺带统一了三处此前各写各的：「主按钮」在 `index` 是墨黑、在 `plaza`/`bots` 是品牌色、
在 `admin` 是蓝色 —— 现在全部是 `--t-accent` 的实心按钮；「危险按钮」统一为白底描边红字；
「轻提示」统一挪到右下角（原来固定在底部正中，会压住输入框）。

## 4. index 首屏

改之前是标准 AI 首页：居中大渐变圆角图标 + 大标题 + 一行副标题 + 六个胶囊 + 四张等高卡片。
问题不在配色，在版式本身。

改之后（`#welcome` 内部重写，JS 仍按 `[data-prompt]` 委托监听，未受影响）：

```
● 网页研究智能体 │ 集成栈 · Pi Runtime     ← 眉标：小标记 + 品类 + 版本
给一个主题，
拿回一份带来源的报告                       ← 左对齐两行标题
它会自己搜索、读完整页……                    ← 一段说明
场景  深度研究 / HTML 报告 / ……            ← 带标签的一行胶囊
01 看懂一个行业问题        →               ← 两列编号列表，用横线分隔
02 做一张能力速查表        →
……
```

标题文案也换了一句：原文案「网页研究智能体，我帮你」只说品类不说结果，
新文案把品类交给眉标，标题留给承诺。**如果不想要这句，改 `#welcome` 里的 `<h1>` 即可。**

## 5. 这一版补上的四件事

### 5.1 自托管字体（不再依赖 Google Fonts）

分配器给的搭配是 Host Grotesk + JetBrains Mono。取 latin 子集（3 个 woff2 共 48KB），
base64 后 66KB，直接内联进 `<head>`：

- 只把 **拉丁字母、数字、标点** 交给新字体，`unicode-range` 里不含 CJK，
  汉字逐字回退到系统中文——中文排版本来就不该用这两款西文字体。
- 中文仍是 `PingFang SC / 微软雅黑`，所以中英混排要保证字面宽度接近，
  否则会出现视觉上的"字跳"。Host Grotesk 的 x-height 与苹方接近，实测可接受。
- 内联而不是静态路由：这台机器上五个页面是文本返回，加 `/fonts/*` 就要在部署时
  多搬一个目录，漏了只是静默回退，很难发现。66KB 换零新增路由，值得。

### 5.2 深色模式

不是"再写一套页面"，而是把同一组令牌翻到暗面（`html[data-page][data-theme="dark"]`）。
一万八千行旧 CSS 走 `var()`，翻令牌就跟着走。剩下写死浅色的地方逐个收回：

| 类型 | 处理 |
|---|---|
| `background:#fff`（31 处，设计层内） | 换成 `var(--t-surface)` |
| 顶栏 / 粘性筛选条（`rgba(255,255,255,.9)`） | 暗色下单独覆盖 |
| 内联 `style="background:#fff"`（消息页返回按钮等） | `!important` 压 |
| Toast「墨底白字」 | 暗色下反过来：暗面 + 亮字 + 一条描边 |
| 图表配色 | 见 5.4 |

**实心 accent 底上的文字在暗色下要换色**：`--t-accent` 在暗面被提亮到 `#54b6c0`，
白字对比只有 2.2:1，所以新增 `--t-on-accent`（亮色白 / 暗色 `#0a1416`），
所有"实心青底"的按钮、徽章都用它。

切换按钮由 `taste-layer.js` 注入到各页页头，三态语义是**先跟随系统**，
点一次记住显式选择（`localStorage['taste-theme']`）。

### 5.3 窄屏触控

`@media (max-width:768px)` 下单点：

- 按钮 / 胶囊 / 导航项 / 标签页 `min-height:44px`，方形图标按钮连宽度一起 44；
- 输入框字号抬到 16px——iOS 在 `font-size<16px` 的输入框聚焦时会自动放大整页；
- 控制台页头是分行排的，44px 的按钮插进流里会自成一行，所以钉到 header 右上角
  并用 `padding-right` 让位。

桌面尺寸一律不动。

### 5.4 登录态验证，和它暴露的三个真问题

用 `scripts/serve_dev_login.sh`（`AUTH_MODE=ticket` + 本地登录桩）起了一个隔离实例，
临时把 `beeftv/dev-owner` 绑到库里有数据的账号，把五个页面在**有数据**的状态下截了一遍。
这一步值得做——之前空态看不出来的问题全在这时候冒出来：

| 问题 | 修法 |
|---|---|
| 控制台两张柱状图是 **蓝 + 绿**（`barChart()` 把颜色当参数写死在 JS 里：`#378add` / `#1d9e75`） | CSS 的 `fill` 声明优先于 SVG 的表现属性，直接在 `#chartMessages / #chartUsers` 上盖成 `--t-accent` / `--t-accent-2`。不动 JS |
| 控制台页头直接显示 **`u_0e538bb7f9f94b81`**，租户显示 **`default`** | 展示层收口：`/api/console/me` 不返回 `display_name`，所以 `taste-layer.js` 用 `/api/me` 的昵称替换裸 ID，把 `default` 说成「默认空间」 |
| 广场四张卡片各带一个实心主按钮，等于没有主按钮；emoji 头像四种彩色底 | 「使用」降成描边，指针落在卡片上才变实心；emoji 头像统一底色 + `grayscale(.88)` |

登录态的验证方式是临时的：**跑完已把 `identities`、`auth_platforms`、临时会话全部还原**，
没有留下测试数据。

## 6. 验证

- **无 JS 异常**：五个页面 × 浅/深两套，逐一捕获 `Runtime.exceptionThrown` 与
  `console.error`，均为空（`index` 在无会话时报的 401 是鉴权，不是脚本错误）。
- **无横向溢出**：1440 / 1024 / 768 / 390 四个宽度下 `scrollWidth == innerWidth`；
  390 宽单列、无截断。
- **对比度**：亮色最小号文字（`--t-faint`）4.66–5.0:1，过 WCAG AA。
- **深色没做逐像素审计**：只逐个页面截了图确认没有残留白块，没有像亮色那样把
  每个前景/背景对都算一遍。旧 CSS 里还可能藏着个别 `rgba()` 写的浅色。
- **截图**：`.taste-skill/shots/final2/`（改版前后、浅深对比），
  迭代过程在 `.taste-skill/shots/v*`。

## 7. 回滚

设计层是三段独立的块，回滚只需：

```bash
git checkout -- src/webpage_agent/api/web/{index,bots,plaza,messaging,admin}.html
```

（如果要保留工具链、只撤掉页面上的效果，把 `<style id="taste-fonts">`、
`<style id="taste-layer">`、`<script id="taste-theme">` 三段删掉即可。除此之外的
HTML 结构改动只有 index 首屏那一处和广场卡片上的 `.sug-n / .sug-go` 序号标记。）
