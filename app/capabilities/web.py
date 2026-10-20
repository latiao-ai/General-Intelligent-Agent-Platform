"""web 能力包：抓取资料。对应 DeepSeek 官方 Search / Fetch 两个接口。"""
from datetime import datetime, timezone
from app.integrations import search as search_backend
from app import sources
from app.services import clean_public_url, urls_from_input
from app.capabilities.base import ToolError, error_entry
from app.events import action, emit
from app.config import settings

# 本能力包自带的规划规则。规划器提示词按"候选工具属于哪些能力包"自动拼接，
# 内核里不再写死检索链路（文档 §4：业务提示词归能力包）。
PROMPT_RULES = (
    '【联网检索】需要外部或实时事实（最新、价格、版本、新闻）或用户要求调研时用 '
    'web_search，搜索结果自带的正文就是本轮资料，直接用。'
    '【指定网页】只有本轮用户输入里出现了 URL、要求读某个链接时，才用 fetch_pages；'
    'urls 就填用户给出的那几个链接，输入里没有 URL 就不要调用它。'
    '但若「已有资料」里已经有正文，而用户只是要把这些内容转成网页，'
    '就不要再检索，直接进入收尾步骤——重复检索既慢又会引入无关来源。'
)


async def _web_search(args: dict, state: dict) -> dict:
    query = str(args.get('query') or '').strip()
    if not query:
        raise ToolError('web_search 需要 query 参数')
    documents = [dict(d) for d in state.get('documents', [])]
    errors = list(state.get('errors', []))
    usage = list(state.get('usage', []))
    by_url = {d['url']: d for d in documents}
    emit(action(f'已搜索网页（{search_backend.PROVIDER} 官方 API）', query, '🌐'))
    try:
        response = await search_backend.search(query)
    except Exception as exc:
        errors.append(error_entry('search', exc))
        raise ToolError('搜索失败，检查 DEEPSEEK_API_KEY 与网络') from exc
    usage.append({'stage': 'search', 'backend': response.get('usage')})
    added = 0
    for item in response.get('results', []):
        try:
            url = clean_public_url(item['url'])
        except (KeyError, ValueError):
            continue
        if url in by_url:
            continue
        if len(documents) >= settings.max_pages:
            break
        task_id = state.get('task_id') or 'task'
        # 检索接口常常直接带回正文：落盘，记录里只留字数
        document = sources.metadata(
            task_id, 's' + str(len(documents) + 1),
            url=url, title=item.get('title') or url,
            snippet=item.get('content', ''),
            text=item.get('raw_content') or '',
        )
        # 官方 Search 会带回站点名/发布时间/排序分/权威度，资料页要用到，别丢
        for key in ('site_name', 'date', 'score', 'authority'):
            if item.get(key) is not None:
                document[key] = item[key]
        documents.append(document)
        by_url[url] = document
        added += 1
    with_content = sum(1 for d in documents if sources.has_text(d))
    emit(action('命中来源',
                f'新增 {added} 个，累计 {len(documents)} 个（{with_content} 个自带正文）',
                '📚'))
    return {'documents': documents, 'errors': errors, 'usage': usage,
            'status': 'searched'}


async def _fetch_pages(args: dict, state: dict) -> dict:
    """读取**用户在本轮输入里给出的 URL**。

    调用时机只有一种：用户输入里出现了链接。检索结果里的链接不走这条路——
    `web_search` 已经带回正文，再逐条抓一遍既慢又多花一次配额。
    这里仍然硬校验"只能抓用户给的链接"：提示词是软约束，模型偶尔会把搜索结果
    塞进来，工具自己得挡住。
    """
    requested = args.get('urls')
    if not isinstance(requested, list) or not requested:
        raise ToolError('fetch_pages 需要 urls 参数：填入本轮用户输入里给出的链接')
    supplied = set(urls_from_input(state.get('user_input') or ''))
    urls = []
    for raw in requested:
        if not isinstance(raw, str):
            raise ToolError('fetch_pages 的 urls 必须是 URL 字符串数组')
        try:
            url = clean_public_url(raw)
        except ValueError as exc:
            raise ToolError('fetch_pages 只接受有效的公开网页 URL') from exc
        if url not in supplied:
            raise ToolError(f'fetch_pages 只读取本轮用户输入里的链接，'
                            f'{url} 不在其中')
        if url not in urls:
            urls.append(url)

    documents = [dict(d) for d in state.get('documents', [])]
    errors = list(state.get('errors', []))
    usage = list(state.get('usage', []))
    by_url = {d['url'] for d in documents}

    # 只登记并处理用户明确提供的链接，其他来源保持原样。
    for url in urls:
        if url in by_url or len(documents) >= settings.max_pages:
            continue
        documents.append(sources.metadata(
            state.get('task_id') or 'task', 's' + str(len(documents) + 1),
            url=url, title=url))
        by_url.add(url)

    selected = [d for d in documents if d['url'] in urls]
    missing = [d['url'] for d in selected if not sources.has_text(d)]
    result_map = {}
    if missing:
        emit(action('提取正文', f'{len(missing)} 个链接', '📄'))
        try:
            response = await search_backend.fetch(missing)
        except Exception as exc:
            errors.append(error_entry('extract', exc))
            raise ToolError('正文提取失败，检查代理或链接可达性') from exc
        usage.append({'stage': 'extract', 'backend': response.get('usage')})
        for result in response.get('results', []):
            try:
                result_map[clean_public_url(result['url'])] = result
            except (KeyError, ValueError):
                continue
        for _failed in response.get('failed_results', []):
            errors.append({'stage': 'extract', 'type': 'URLExtractionFailed'})

    now = datetime.now(timezone.utc).isoformat()
    task_id = state.get('task_id') or 'task'
    for d in selected:
        if sources.has_text(d) and d.get('status') == 'success':
            continue
        text = sources.load_text(d) or (result_map.get(d['url'], {}) or {}).get('raw_content') or ''
        if isinstance(text, str) and text.strip():
            chars = sources.save_text(task_id, d['source_id'], text)
            d.update(chars=chars, status='success', fetched_at=now, error=None)
        else:
            d.update(chars=0, status='failed', error='未取得有效正文')
    ok = [d for d in selected if d.get('status') == 'success']
    if ok:
        total_chars = sum(sources.chars_of(d) for d in ok)
        emit(action('正文就绪', f'{len(ok)} 个来源，共 {total_chars} 字符', '✅'))
    else:
        emit(action('没有可用正文', f'{len(selected)} 个指定来源均未取到内容', '⚠️'))
    return {'documents': documents, 'errors': errors,
            'usage': usage, 'status': 'fetched'}
