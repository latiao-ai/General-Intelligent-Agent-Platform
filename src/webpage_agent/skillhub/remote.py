"""远端 SkillHub 客户端：目录、详情、包下载。

- `SKILLHUB_URL` 指向 SkillHub 的目录接口（如 `/api/skillhub/v1/skills`）；
- 目录缓存 `SKILLHUB_TTL` 秒（默认 300）；
- 安装时按详情里的 `package.url` 下载 zip，并校验 `package.sha256`；
- 拉取失败时返回上一次成功缓存；没有缓存就只用本地目录。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx

logger = logging.getLogger(__name__)

_URL = os.getenv('SKILLHUB_URL', '').strip().rstrip('/')
_TOKEN = os.getenv('SKILLHUB_TOKEN', '').strip()
_PROVIDER = os.getenv('SKILLHUB_PROVIDER', 'native').strip().lower()
_PACKAGE_TEMPLATE = os.getenv('SKILLHUB_PACKAGE_URL', '').strip()
_REMOTE_LIMIT = max(20, int(os.getenv('SKILLHUB_REMOTE_LIMIT', '300') or 300))
_TTL = max(30, int(os.getenv('SKILLHUB_TTL', '300') or 300))
_lock = asyncio.Lock()
_cache: dict = {'at': 0.0, 'items': []}


def _headers() -> dict:
    headers = {'Accept': 'application/json'}
    if _TOKEN:
        headers['Authorization'] = f'Bearer {_TOKEN}'
    return headers


def _workbuddy() -> bool:
    return _PROVIDER in ('workbuddy', 'lightmake') or '/api/' in _URL


def _list_url() -> str:
    if _workbuddy() and not _URL.endswith('/api/skills'):
        return _URL + '/api/skills'
    return _URL


def registry_url() -> str:
    return _list_url()


def _timestamp(value) -> str:
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        import datetime
        return datetime.datetime.fromtimestamp(number, datetime.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ''


def _normalize_workbuddy(raw: list) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        namespace = entry.get('namespace') if isinstance(entry.get('namespace'), dict) else {}
        slug = str(entry.get('slug') or namespace.get('publicSlug') or '').strip()
        sid = str(namespace.get('canonicalName') or slug).strip()
        name = str(entry.get('name') or entry.get('displayName') or slug).strip()
        if not sid or not name or sid in seen:
            continue
        seen.add(sid)
        publisher = entry.get('publisher') if isinstance(entry.get('publisher'), dict) else {}
        tags = entry.get('tags') if isinstance(entry.get('tags'), dict) else {}
        suite = ', '.join(str(x) for x in (entry.get('subCategories') or []) if isinstance(x, dict) and x.get('name'))[:80]
        items.append({
            'id': sid,
            'name': name,
            'kind': 'skill',
            'icon': str(entry.get('iconUrl') or '✨'),
            'color': '#eef4ff',
            'description': str(entry.get('description_zh') or entry.get('description') or entry.get('summary') or ''),
            'category': str(entry.get('category') or '效率工具'),
            'author': str(publisher.get('name') or entry.get('ownerName') or 'SkillHub'),
            'installs': int(entry.get('installs') or 0),
            'usage': int(entry.get('downloads') or 0),
            'featured': bool(entry.get('verified')),
            'paid': False,
            'source': 'skillhub',
            'suite': suite,
            'version': str(entry.get('version') or '1.0.0'),
            'created': _timestamp(entry.get('created_at')),
            'prompt': '',
            'remote': True,
        })
    return items


def _normalize(raw: list) -> list[dict]:
    if _workbuddy():
        return _normalize_workbuddy(raw)
    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get('id') or '').strip()
        name = str(entry.get('name') or '').strip()
        if not sid or not name or sid in seen:
            continue
        seen.add(sid)
        kind = entry.get('kind') if entry.get('kind') in ('skill', 'connector') else 'skill'
        items.append({
            'id': sid,
            'name': name,
            'kind': kind,
            'icon': str(entry.get('icon') or '✨'),
            'color': str(entry.get('color') or '#eef4ff'),
            'description': str(entry.get('description') or ''),
            'category': str(entry.get('category') or '效率工具'),
            'author': str(entry.get('author') or 'SkillHub'),
            'installs': int(entry.get('installs') or 0),
            'usage': int(entry.get('usage') or 0),
            'featured': bool(entry.get('featured')),
            'paid': bool(entry.get('paid')),
            'source': str(entry.get('source') or 'skillhub'),
            'suite': str(entry.get('suite') or ''),
            'version': str(entry.get('version') or '1.0.0'),
            'created': str(entry.get('created') or ''),
            'prompt': str(entry.get('prompt') or ''),
            'remote': True,
        })
    return items


async def fetch_items(force: bool = False) -> list[dict]:
    """返回远端目录。没有配置 URL、拉取失败都返回缓存/空列表。"""
    if not _URL:
        return []
    now = time.time()
    if not force and _cache['at'] and now - _cache['at'] < _TTL:
        return list(_cache['items'])

    async with _lock:
        if not force and _cache['at'] and time.time() - _cache['at'] < _TTL:
            return list(_cache['items'])
        raw: list = []
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                page = 1
                while page <= 20:
                    if _workbuddy():
                        response = await client.get(
                            _list_url(), params={'page': page, 'pageSize': 100},
                            headers=_headers())
                    else:
                        response = await client.get(
                            _URL, params={'page': page, 'page_size': 200},
                            headers=_headers())
                    response.raise_for_status()
                    payload = response.json()
                    if _workbuddy():
                        data = payload.get('data') if isinstance(payload, dict) else None
                        chunk = data.get('skills') if isinstance(data, dict) else None
                        total = int((data or {}).get('total') or 0) if isinstance(data, dict) else 0
                    else:
                        chunk = payload.get('items') if isinstance(payload, dict) else payload
                        total = 0
                    if not isinstance(chunk, list):
                        raise ValueError('远端目录不是数组')
                    raw.extend(chunk)
                    if _workbuddy():
                        if not chunk or (total and len(raw) >= min(total, _REMOTE_LIMIT)):
                            break
                    elif not (isinstance(payload, dict) and payload.get('has_more')):
                        break
                    page += 1
        except Exception as exc:                       # noqa: BLE001
            logger.warning('技能目录远端拉取失败 %s: %s', _list_url(), exc)
            return list(_cache['items'])

        items = _normalize(raw)
        _cache.update(at=time.time(), items=items)
        logger.info('技能目录远端加载完成：%s 条（%s）', len(items), _URL)
        return list(items)


async def fetch_detail(skill_id: str) -> dict:
    """取详情（含 package.url / package.sha256）。"""
    if not _URL:
        return {}
    if _workbuddy():
        raw = str(skill_id or '').strip()
        namespace = ''
        slug = raw
        if raw.startswith('@') and '/' in raw:
            namespace, slug = raw[1:].split('/', 1)
        url = f"{_URL.rstrip('/')}/api/v1/skills/{quote(slug)}"
        params = {'namespace': namespace} if namespace else None
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(url, params=params, headers=_headers())
            response.raise_for_status()
            return response.json()
    url = f"{_URL}/{quote(skill_id)}"
    async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
        response = await client.get(url, headers=_headers())
        response.raise_for_status()
        return response.json()


async def download_package(skill_id: str, version: str, dest_dir: Path) -> dict:
    """下载技能包并校验 sha256，返回 {path, bytes, sha256}。"""
    detail = await fetch_detail(skill_id)
    package = detail.get('package') or {}
    url = str(package.get('url') or '')
    expected = str(package.get('sha256') or '')
    if not url and _workbuddy() and _PACKAGE_TEMPLATE:
        url = _PACKAGE_TEMPLATE.format(slug=quote(str(skill_id)), version=quote(str(version)))
    if not url:
        if _workbuddy():
            raise RuntimeError('该远端技能没有公开下载地址；可配置 SKILLHUB_PACKAGE_URL 模板')
        raise RuntimeError('该技能没有提供下载地址')
    if url.startswith('/'):
        url = urljoin(_URL + '/', url)
    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        response = await client.get(url, headers=_headers())
        response.raise_for_status()
        data = response.content
    actual = hashlib.sha256(data).hexdigest()
    if expected and actual != expected:
        raise RuntimeError(f'技能包校验失败：期望 {expected}，实际 {actual}')
    folder = Path(dest_dir) / skill_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{version or "1.0.0"}.zip'
    target.write_bytes(data)
    return {'path': str(target), 'bytes': len(data), 'sha256': actual}
