#!/usr/bin/env python3
"""把设计层（字体 / 样式 / 主题脚本）注入五个单文件页面。

为什么是注入而不是外链：
    这五个页面本来就是「单文件前端」——结构、样式、交互都写在同一个 HTML 里，
    服务端只做文本返回。加静态路由去托管 CSS/字体，会在部署到客户平台时多出
    必须一起搬的路径；漏了就静默降级（字体回退、主题失效），很难发现。
    所以三样东西都以内联块的形式写进 <head>：

        <style  id="taste-fonts">   Host Grotesk / JetBrains Mono 的 latin 子集
        <style  id="taste-layer">   设计层（令牌 + 五个页面的作用域规则）
        <script id="taste-theme">   首帧定主题、插切换按钮、收口裸露的内部标识

幂等：重复运行只替换这三个块，不叠加，也不碰任何业务 JS / DOM 结构。
除了这三个块，只给 <html> 补一个 data-page（每个页面区分作用域用）。

用法：
    python3 scripts/frontend/build_font_css.py                 # 先按需重建字体
    python3 scripts/frontend/apply_frontend_skin.py            # 写入
    python3 scripts/frontend/apply_frontend_skin.py --check    # 只检查是否与源一致
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "webpage_agent" / "api" / "web"
HERE = Path(__file__).resolve().parent
FONTS_PATH = HERE / "fonts.css"
CSS_PATH = HERE / "taste-layer.css"
JS_PATH = HERE / "taste-layer.js"

# 文件名 -> <html data-page="...">
PAGES = {
    "index.html": "hub",
    "bots.html": "bots",
    "plaza.html": "plaza",
    "messaging.html": "messaging",
    "admin.html": "admin",
}

FONTS_RE = re.compile(r'<style id="taste-fonts">.*?</style>\s*(?:<!-- /taste-fonts -->)?', re.S)
CSS_RE = re.compile(r'<style id="taste-layer">.*?</style>\s*(?:<!-- /taste-layer -->)?', re.S)
JS_RE = re.compile(r'<script id="taste-theme">.*?</script>\s*(?:<!-- /taste-theme -->)?', re.S)
HTML_RE = re.compile(r"<html\b([^>]*)>", re.I)


def build_blocks(fonts: str, css: str, js: str) -> str:
    return "\n".join([
        '<style id="taste-fonts">\n'
        "/* 由 scripts/frontend/build_font_css.py 生成，勿手改；源在 scripts/frontend/fonts/vendor */\n"
        f"{fonts}\n</style>\n<!-- /taste-fonts -->",
        '<style id="taste-layer">\n'
        "/* 由 scripts/frontend/taste-layer.css 生成，勿手改；改源文件后重跑 apply_frontend_skin.py */\n"
        f"{css}\n</style>\n<!-- /taste-layer -->",
        '<script id="taste-theme">\n'
        "/* 由 scripts/frontend/taste-layer.js 生成，勿手改 */\n"
        f"{js}\n</script>\n<!-- /taste-theme -->",
    ])


def render(html: str, page: str, blocks: str) -> str:
    html = HTML_RE.sub(
        lambda m: '<html%s data-page="%s">' % (
            re.sub(r'\s*data-page="[^"]*"', "", m.group(1)), page),
        html, count=1)
    for pattern in (FONTS_RE, CSS_RE, JS_RE):
        html = pattern.sub("", html)
    # 删块会留下空行，不清掉的话每跑一次就多几行——幂等就断了
    html = re.sub(r"[ \t]*\n[ \t\n]*(</head>)", r"\n\1", html, count=1, flags=re.I)
    return re.sub(r"</head>", lambda _: blocks + "\n</head>", html, count=1, flags=re.I)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    args = ap.parse_args()

    for path in (FONTS_PATH, CSS_PATH, JS_PATH):
        if not path.exists():
            print(f"缺少源文件：{path}", file=sys.stderr)
            return 2

    blocks = build_blocks(
        FONTS_PATH.read_text(encoding="utf-8"),
        CSS_PATH.read_text(encoding="utf-8"),
        JS_PATH.read_text(encoding="utf-8"),
    )
    drift = 0

    for name, page in PAGES.items():
        path = WEB_DIR / name
        if not path.exists():
            print(f"跳过 {name}（不存在）")
            continue
        old = path.read_text(encoding="utf-8")
        new = render(old, page, blocks)
        if new == old:
            print(f"= {name} 已是最新")
            continue
        drift += 1
        if args.check:
            print(f"! {name} 与源文件不一致")
        else:
            path.write_text(new, encoding="utf-8")
            print(f"✓ {name} 已更新（data-page={page}）")

    if args.check and drift:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
