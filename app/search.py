"""检索后端：DeepSeek 官方 Search / Fetch API。

统一归一化成 tools.py 使用的形状：

    search(query) -> {'results': [{'url','title','content','raw_content', ...}],
                      'usage': {'provider': 'deepseek'}}
    fetch(urls)   -> {'results': [{'url','raw_content'}],
                      'failed_results': [...], 'usage': {'provider': 'deepseek'}}

官方接口要点（`POST /v1/tools/search`、`/v1/tools/fetch`）：

- search 的 `need_content=True` 才会在 `content` 里回正文，否则恒为空字符串；
  `count` 对应本项目的 `max_pages`。
- fetch **一次只抓一个 url**，所以内部循环，并把失败项汇总成 failed_results。
- 两者都用 Bearer 鉴权，key 与 DeepSeek 平台账号一致（与 LLM 网关账号不是一回事）。
"""
import asyncio

import httpx

from app.config import settings

PROVIDER = 'deepseek'



def _headers() -> dict:
    return {'Authorization': f'Bearer {settings.deepseek_api_key}',
            'Content-Type': 'application/json'}


async def search(query: str):
    payload = {
        'query': query[:128],
        'count': max(1, min(50, settings.max_pages)),
        # 开这个才拿得到正文，否则 content 恒为空
        'need_content': True,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        reply = await client.post(f'{settings.deepseek_base_url}/v1/tools/search',
                                  json=payload, headers=_headers())
        reply.raise_for_status()
        data = reply.json()
    results = []
    for item in data.get('results') or []:
        results.append({
            'url': item.get('url') or '',
            'title': item.get('title') or item.get('url') or '',
            # snippet 给界面看，summary 更适合喂模型，这里按可用性兜底
            'content': item.get('snippet') or item.get('summary') or '',
            'raw_content': item.get('content') or '',
            # 资料页会展示这三项
            'site_name': item.get('site_name'),
            'date': item.get('date'),
            'score': item.get('rerank_score'),
            'authority': item.get('authority'),
        })
    return {'results': results, 'usage': {'provider': PROVIDER}}


async def fetch(urls: list[str]):
    results, failed = [], []

    async def one(client, url):
        try:
            reply = await client.post(f'{settings.deepseek_base_url}/v1/tools/fetch',
                                      json={'url': url}, headers=_headers())
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
        # fetch 一次只收一个 url；小批并发，别把服务端打满
        for start in range(0, len(urls), 3):
            await asyncio.gather(*(one(client, url)
                                   for url in urls[start:start + 3]))
    return {'results': results, 'failed_results': failed,
            'usage': {'provider': PROVIDER}}
