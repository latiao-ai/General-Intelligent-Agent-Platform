import re
import ipaddress
from urllib.parse import urlsplit, urlunsplit
from langchain_openai import ChatOpenAI
from tavily import AsyncTavilyClient
from app.config import settings


def clean_public_url(value: str) -> str:
    p = urlsplit(value.strip())
    if p.scheme.lower() not in {'http', 'https'} or not p.hostname:
        raise ValueError('只支持 HTTP/HTTPS URL')
    if p.username or p.password:
        raise ValueError('URL 不允许包含账户凭据')
    host = p.hostname.lower().rstrip('.')
    if '.' not in host and ':' not in host:
        raise ValueError('拒绝单标签主机')
    if host == 'localhost' or host.endswith(('.local', '.localhost', '.internal')):
        raise ValueError('拒绝本地地址')
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and not addr.is_global:
        raise ValueError('拒绝非公共 IP')
    if p.port not in (None, 80, 443):
        raise ValueError('首版只支持标准网页端口')
    # 保留查询参数，移除 fragment；域名基本规范化。
    netloc = '[' + host + ']' if ':' in host else host
    if p.port:
        netloc += ':' + str(p.port)
    return urlunsplit((p.scheme.lower(), netloc, p.path or '/', p.query, ''))


def urls_from_input(text: str) -> list[str]:
    candidates = re.findall(r'https?://[^\s<>"\u3000]+', text)
    result = []
    for value in candidates:
        value = value.rstrip('.,;!?)]}，。；！？）】、')
        value = clean_public_url(value)
        if value not in result:
            result.append(value)
    return result[:settings.max_pages]


def new_llm(model: str | None = None):
    options = dict(
        model=model or settings.llm_model,
        api_key=settings.llm_key,
        temperature=0,
        timeout=300,
        max_retries=1,
    )
    if settings.llm_base_url:
        options['base_url'] = settings.llm_base_url
    return ChatOpenAI(**options)


def _combine_usage(usages: list):
    """合并多次尝试的 token 用量。失败的尝试同样花钱，不能丢。"""
    items = [u for u in usages if isinstance(u, dict)]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    combined = {}
    for usage in items:
        for key, value in usage.items():
            if isinstance(value, int):
                combined[key] = combined.get(key, 0) + value
    return combined


async def structured_call(
    schema, system: str, user: str, model: str | None = None, attempts: int = 2
):
    """结构化调用，解析失败时重试。

    快模型偶发会产出不符合 schema 的结构（例如空 source_ids），
    单次失败就中断整个任务代价太大，因此默认重试一次。
    返回 (Pydantic 对象, usage)，usage 为各次尝试的合计。
    """
    usages = []
    for _ in range(max(1, attempts)):
        llm = new_llm(model).with_structured_output(
            schema, method='function_calling', include_raw=True
        )
        response = await llm.ainvoke([
            ('system', system), ('human', user)
        ])
        usages.append(getattr(response.get('raw'), 'usage_metadata', None))
        if not response.get('parsing_error') and response.get('parsed') is not None:
            return response['parsed'], _combine_usage(usages)
    raise RuntimeError('模型结构化输出失败；检查 tool calling 支持及 schema')


def new_tavily():
    return AsyncTavilyClient(api_key=settings.tavily_key)


async def search_query(query: str):
    return await new_tavily().search(
        query=query,
        search_depth='basic',
        max_results=settings.max_pages,
        include_answer=False,
        include_raw_content='markdown',
        auto_parameters=False,
        include_usage=True,
        timeout=30,
    )


async def extract_urls(urls: list[str]):
    return await new_tavily().extract(
        urls=urls,
        extract_depth='basic',
        format='markdown',
        timeout=30,
    )
