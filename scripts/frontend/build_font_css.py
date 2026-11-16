#!/usr/bin/env python3
"""把 vendor 里的 woff2 打包成一段可内联的 @font-face CSS。

为什么内联而不是外链：
    这五个页面是「单文件前端」，服务端只做文本返回。外链字体要额外加静态路由，
    部署到客户平台时那条路径得跟着搬，漏了就是 404（虽然会回退到系统字体，
    但也就白做了）。latin 子集很小（3 个文件共 48KB），base64 后 64KB，
    换来的是零新增路由、零 404 风险。

    只取 latin 子集：中文字符本来就不在这些字体里，靠 unicode-range 让浏览器
    逐字回退到系统中文——拉丁字母、数字、标点用新字体，汉字仍是主机上那套。

用法：
    python3 scripts/frontend/build_font_css.py          # 生成 fonts.css
    python3 scripts/frontend/build_font_css.py --check  # 只检查是否与 vendor 一致
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "fonts" / "vendor"
OUT = HERE / "fonts.css"

# (文件名, family, 字重)
FACES = [
    ("host-grotesk-latin-400.woff2", "Host Grotesk", 400),
    ("host-grotesk-latin-600.woff2", "Host Grotesk", 600),
    ("jetbrains-mono-latin-400.woff2", "JetBrains Mono", 400),
]

# latin + latin-ext 的常用码位；不含中文，让汉字一路回退到系统字体
UNICODE_RANGE = (
    "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,"
    "U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,"
    "U+2193,U+2212,U+2215,U+FEFF,U+FFFD"
)

HEADER = """/* 自动生成，勿手改 —— 源：scripts/frontend/fonts/vendor/*.woff2
   重新生成：python3 scripts/frontend/build_font_css.py
   字体：Host Grotesk（SIL OFL 1.1）、JetBrains Mono（SIL OFL 1.1），均取自 @fontsource。 */
"""


def build() -> str:
    parts = [HEADER]
    for fname, family, weight in FACES:
        path = VENDOR / fname
        if not path.exists():
            raise SystemExit(f"缺少字体文件：{path}")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append(
            "@font-face{\n"
            f'  font-family:"{family}";\n'
            "  font-style:normal;\n"
            f"  font-weight:{weight};\n"
            "  font-display:swap;\n"
            f'  src:url(data:font/woff2;base64,{b64}) format("woff2");\n'
            f"  unicode-range:{UNICODE_RANGE};\n"
            "}\n"
        )
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    css = build()
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != css:
            print("! fonts.css 与 vendor 不一致，请重新生成")
            return 1
        print("= fonts.css 已是最新")
        return 0
    OUT.write_text(css, encoding="utf-8")
    total = sum((VENDOR / f).stat().st_size for f, _, _ in FACES)
    print(f"✓ 已生成 fonts.css（{len(css)} 字符，字体源 {total} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
