"""export 能力包：把检索到的资料原样排版成 HTML。

这是本项目唯一的"产出网页"能力：不调用模型、不做摘要改写，只把
JSON 元信息 + Markdown 正文转成一份可部署的静态页面。

（原先同包内还有"整理成结构化报告 → 渲染模板"的链路，已按需裁剪。）
"""
import hashlib

from app import md, sources
from app.capabilities.base import ToolError, usable_documents
from app.capabilities.registry import tool
from app.config import settings
from app.events import action, emit

# 能力包名，也进 tool_key（local:export:<动作>）
CAPABILITY = 'export'

PROMPT_RULES = (
    '【导出网页】用户要"把资料/搜索结果原样存成网页"时用 render_sources_html：'
    '它不调用模型、不改写内容，只把 JSON 元信息与 Markdown 正文排版成 HTML。'
    '必须在取得正文的步骤（web_search 或 fetch_pages）之后调用。'
)


SOURCES_PAGE_CSS = """
body{margin:0;background:#f5f6f8;color:#1b2430;
     font:15px/1.75 system-ui,-apple-system,"PingFang SC",sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:24px;margin:0 0 6px}
.meta{color:#6b7684;font-size:13px;margin-bottom:22px}
article{background:#fff;border:1px solid #e2e6eb;border-radius:10px;
        padding:18px 22px;margin-bottom:18px}
article h2{font-size:18px;margin:0 0 6px}
.src{color:#6b7684;font-size:12px;margin-bottom:14px;
     display:flex;gap:12px;flex-wrap:wrap}
.src a{color:#165eb4;word-break:break-all}
.body{overflow-wrap:anywhere}
.body pre{background:#f6f8fa;border:1px solid #e2e6eb;border-radius:8px;
          padding:10px 12px;overflow:auto;font-size:13px}
.body code{font-family:ui-monospace,Menlo,monospace}
.body blockquote{margin:10px 0;padding:6px 14px;border-left:3px solid #d6dde5;
                 color:#3b4757;background:#fafbfc}
.body img{max-width:100%}
.body table{border-collapse:collapse}
.tag{background:#eef1f5;border-radius:6px;padding:1px 7px;font-size:12px}
"""


MAX_PAGE_SOURCE_CHARS = 100_000


def _sources_page(title: str, documents: list[dict]) -> str:
    """把来源列表渲染成一份可独立打开的 HTML。

    来源是任意网页，正文一律先转义再转标签（见 app/md.py）；
    这里只负责拼装骨架与元信息。
    """
    cards = []
    for doc in documents:
        body = sources.load_text(doc)[:MAX_PAGE_SOURCE_CHARS]
        rendered = md.to_html(body) if body.strip() else (
            '<p>' + md.escape(doc.get('snippet') or '（这份来源没有正文）') + '</p>')
        bits = [f'<a href="{md.escape(doc["url"])}" target="_blank" '
                f'rel="noreferrer">{md.escape(doc["url"])}</a>']
        if doc.get('site_name'):
            bits.append(f'<span class="tag">{md.escape(str(doc["site_name"]))}</span>')
        if doc.get('date'):
            bits.append(f'<span class="tag">{md.escape(str(doc["date"]))}</span>')
        if doc.get('chars'):
            bits.append(f'<span class="tag">{doc["chars"]} 字符</span>')
        cards.append(
            f'<article><h2>{md.escape(doc.get("title") or doc["url"])}</h2>'
            f'<div class="src">{"".join(bits)}</div>'
            f'<div class="body">{rendered}</div></article>')
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{md.escape(title)}</title><style>{SOURCES_PAGE_CSS}</style></head>
<body><main>
<h1>{md.escape(title)}</h1>
<div class="meta">共 {len(documents)} 份来源 · 由智能体检索整理</div>
{''.join(cards)}
</main></body></html>"""


@tool(name='render_sources_html', label='资料转 HTML', effect='write',
      key='local:export:sources_html',
      description='把已检索到的原始资料（网页 JSON 元信息 + Markdown 正文）'
                  '直接排版成一份 HTML，不调用模型、不做摘要改写。'
                  '用户说"把资料/搜索结果存成网页"时用它；'
                  '要提炼总结请另外接入相应的 MCP 能力。',
      args={'type': 'object',
            'properties': {'title': {'type': 'string'}}})
async def _render_sources_html(args: dict, state: dict) -> dict:
    """把已检索到的资料（JSON 元信息 + Markdown 正文）导出成一份 HTML。

    这一步**不调用模型**，只是把原始资料排版成网页。
    用户说"把资料/搜索结果保存成网页/HTML"时用它。
    """
    documents, origin = usable_documents(state)
    if not documents:
        raise ToolError('还没有可用正文：先用 web_search 检索取得正文')
    if origin:
        emit(action('沿用上一轮的正文', f'{len(documents)} 个来源', '♻️'))
    title = str(args.get('title') or state.get('user_input') or '检索资料')
    task_id = state.get('task_id') or 'task'
    folder = settings.artifact_dir / task_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'sources.html'
    path.write_text(_sources_page(title, documents), encoding='utf-8')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    artifact = {
        'task_id': task_id, 'html_path': str(path.resolve()),
        'sha256': digest, 'size_bytes': path.stat().st_size,
        'kind': 'sources',
    }
    emit(action('已把资料导出为 HTML',
                f'{len(documents)} 份来源 · {artifact["size_bytes"]} 字节', '📄'))
    return {'artifact': artifact, 'sources_page': {'title': title,
                                                   'count': len(documents)},
            'status': 'rendered'}

