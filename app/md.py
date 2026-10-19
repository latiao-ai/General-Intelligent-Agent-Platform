"""极简 Markdown → HTML。

为什么自己写而不是装 `markdown` 包：本项目坚持零新增依赖（环境里也确认没装），
而我们要覆盖的只是网页正文里常见的那些语法。

**安全前提**：来源是任意网页，属于不可信内容。所以先把整段文本 HTML 转义，
再只把我们自己生成的标签插进去——绝不放行原文里的任何标签或属性。
"""
import html
import re

# 拼装 HTML 骨架时也要转义，对外暴露同一个实现
escape = html.escape

_FENCE = re.compile(r'^\s*```(.*)$')
_HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
_ULIST = re.compile(r'^\s*[-*+]\s+(.*)$')
_OLIST = re.compile(r'^\s*\d+[.)]\s+(.*)$')
_QUOTE = re.compile(r'^\s*>\s?(.*)$')
_HR = re.compile(r'^\s*([-*_])\1{2,}\s*$')


def _inline(text: str) -> str:
    """行内语法。输入是**已转义**的文本，这里只插入标签。"""
    # 行内代码优先，避免里面的 * _ 被当成强调
    parts = re.split(r'(`[^`]+`)', text)

    def rest(seg: str) -> str:
        seg = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)',
                     lambda m: (f'<a href="{m.group(2)}" rel="noreferrer" '
                                f'target="_blank">{m.group(1)}</a>'), seg)
        seg = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', seg)
        seg = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', seg)
        return seg

    out = []
    for seg in parts:
        if len(seg) > 1 and seg.startswith('`') and seg.endswith('`'):
            out.append(f'<code>{seg[1:-1]}</code>')
        else:
            out.append(rest(seg))
    return ''.join(out)


def to_html(text: str) -> str:
    """把一段 Markdown 转成 HTML 片段（不含 <html>/<body>）。"""
    lines = (text or '').replace('\r\n', '\n').split('\n')
    out, i = [], 0
    while i < len(lines):
        line = lines[i]

        fence = _FENCE.match(line)
        if fence:
            lang = html.escape(fence.group(1).strip())
            i += 1
            block = []
            while i < len(lines) and not _FENCE.match(lines[i]):
                block.append(lines[i])
                i += 1
            i += 1                                  # 跳过结束围栏
            code = html.escape('\n'.join(block))
            cls = f' class="language-{lang}"' if lang else ''
            out.append(f'<pre><code{cls}>{code}</code></pre>')
            continue

        if not line.strip():
            i += 1
            continue

        if _HR.match(line):
            out.append('<hr>')
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f'<h{level}>{_inline(html.escape(heading.group(2).strip()))}'
                       f'</h{level}>')
            i += 1
            continue

        for pattern, tag in ((_ULIST, 'ul'), (_OLIST, 'ol')):
            if pattern.match(line):
                items = []
                while i < len(lines):
                    m = pattern.match(lines[i])
                    if not m:
                        break
                    items.append(f'<li>{_inline(html.escape(m.group(1).strip()))}</li>')
                    i += 1
                out.append(f'<{tag}>' + ''.join(items) + f'</{tag}>')
                break
        else:
            if _QUOTE.match(line):
                block = []
                while i < len(lines):
                    m = _QUOTE.match(lines[i])
                    if not m:
                        break
                    block.append(_inline(html.escape(m.group(1).strip())))
                    i += 1
                out.append('<blockquote>' + '<br>'.join(block) + '</blockquote>')
                continue

            # 普通段落：连续非空行合并
            para = []
            while i < len(lines) and lines[i].strip() \
                    and not _HEADING.match(lines[i]) and not _FENCE.match(lines[i]) \
                    and not _ULIST.match(lines[i]) and not _OLIST.match(lines[i]) \
                    and not _QUOTE.match(lines[i]) and not _HR.match(lines[i]):
                para.append(_inline(html.escape(lines[i].strip())))
                i += 1
            out.append('<p>' + '<br>'.join(para) + '</p>')
    return '\n'.join(out)
