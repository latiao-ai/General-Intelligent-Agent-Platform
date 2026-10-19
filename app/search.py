"""检索后端：Tavily 与 DeepSeek 官方 Search/Fetch API 的可切换封装。

两家的接口形态差别不小，这里统一归一化成 tools.py 一直在用的形状：

    search(query) -> {'results': [{'url','title','content','raw_content', ...}],
                      'usage': ...}
    fetch(urls)   -> {'results': [{'url','raw_content'}],
                      'failed_results': [...], 'usage': ...}

DeepSeek 官方接口（`POST /v1/tools/search`、`/v1/tools/fetch`）：
- search 的 `need_content=True` 才会在 `content` 里回正文，等价于 Tavily 的
  `include_raw_content`；`count` 对应我们的 `max_pages`。
- fetch **一次只抓一个 url**，所以内部循环，并把失败项汇总成 failed_results。
- 两者都用 Bearer 鉴权，key 与 DeepSeek 平台账号一致。

切换方式：`.env` 里 `SEARCH_PROVIDER=deepseek`（默认 tavily，保持既有行为不变）。
"""
import asyncio

import httpx
from tavily import AsyncTavilyClient

from app.config import settings

TAVILY = 'tavily'
DEEPSEEK = 'deepseek'


def provider() -> str:
    return (settings.search_provider or TAVILY).strip().lower()


def provider_ready() -> tuple[bool, str]:
    """当前后端是否可用，不可用时返回原因（给界面/接口做前置检查）。"""
    name = provider()
    if name == DEEPSEEK:
        if not settings.deepseek_api_key:
            return False, '缺少 DEEPSEEK_API_KEY（api.deepseek.com 平台 key）'
        return True, ''
    if not settings.tavily_key or settings.tavily_key == 'replace-me':
        return False, '缺少 TAVILY_API_KEY'
    return True, ''


# ---------- Tavily ----------

async def _tavily_search(query: str):
    client = AsyncTavilyClient(api_key=settings.tavily_key)
    return await client.search(
        query=query, search_depth='basic', max_results=settings.max_pages,
        include_answer=False, include_raw_content='markdown',
        auto_parameters=False, include_usage=True, timeout=30,
    )


async def _tavily_fetch(urls: list[str]):
    client = AsyncTavilyClient(api_key=settings.tavily_key)
    return await client.extract(urls=urls, extract_depth='basic',
                                format='markdown', timeout=30)


# ---------- DeepSeek ----------

def _deepseek_headers() -> dict:
    return {'Authorization': f'Bearer {settings.deepseek_api_key}',
            'Content-Type': 'application/json'}


async def _deepseek_search(query: str):
    payload = {
        'query': query[:128],
        'count': max(1, min(50, settings.max_pages)),
        # 开这个才拿得到正文，否则 content 恒为空
        'need_content': True,
    }
    timeout = 30
    async with httpx.AsyncClient(timeout=timeout) as client:
        reply = await client.post(f'{settings.deepseek_base_url}/v1/tools/search',
                                  json=payload, headers=_deepseek_headers())
        reply.raise_for_status()
        data = reply.json()
    results = []
    for item in data.get('results') or []:
        content = item.get('content') or ''
        results.append({
            'url': item.get('url') or '',
            'title': item.get('title') or item.get('url') or '',
            # snippet 给界面看，summary 更适合喂模型
            'content': item.get('snippet') or item.get('summary') or '',
            'raw_content': content,
            # 这几个字段在"来源转 HTML"里会展示出来
            'site_name': item.get('site_name'),
            'date': item.get('date'),
            'score': item.get('rerank_score'),
            'authority': item.get('authority'),
        })
    return {'results': results, 'usage': {'provider': DEEPSEEK}}


async def _deepseek_fetch(urls: list[str]):
    results, failed = [], []

    async def one(client, url):
        try:
            reply = await client.post(
                f'{settings.deepseek_base_url}/v1/tools/fetch',
                json={'url': url}, headers=_deepseek_headers())
            reply.raise_for_status()
            data = reply.json()
            body = data.get('content') or ''
            if body.strip():
                results.append({'url': url, 'raw_content': body,
                                'title': data.get('title') or ''})
            else:
                failed.append({'url': url, 'error': '空正文'})
        except Exception:
            failed.append({'url': url, 'error': '抓取失败'})

    async with httpx.AsyncClient(timeout=60) as client:
        # fetch 一次只收一个 url，并发几个即可，别把服务端打满
        for chunk_start in range(0, len(urls), 3):
            chunk = urls[chunk_start:chunk_start + 3]
            await asyncio.gather(*(one(client, url) for url in chunk))
    return {'results': results, 'failed_results': failed,
            'usage': {'provider': DEEPSEEK}}


# ---------- 对外统一入口 ----------

async def search(query: str):
    if provider() == DEEPSEEK:
        return await _deepseek_search(query)
    return await _tavily_search(query)


async def fetch(urls: list[str]):
    if provider() == DEEPSEEK:
        return await _deepseek_fetch(urls)
    return await _tavily_fetch(urls)
