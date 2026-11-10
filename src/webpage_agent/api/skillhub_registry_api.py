"""SkillHub 服务端 API（内置示例实现）。

它模拟真实 SkillHub 的服务端职责：
- `GET /v1/skills`        目录（搜索/分类/排序/分页）
- `GET /v1/skills/{id}`   详情（含包下载地址、sha256、大小）
- `GET /v1/skills/{id}/download`  下载技能包（zip：SKILL.md + manifest.json）

元数据存在 `deploy/skillhub.registry.json`，但客户端拿到的是**接口响应**，
不是直接读这个文件；安装时也是真的下载 zip 并校验 sha256。
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

router = APIRouter(prefix='/v1', tags=['skillhub-registry'])

_DATA = Path(__file__).resolve().parents[3] / 'deploy' / 'skillhub.registry.json'
_cache = {'mtime': 0.0, 'items': []}


def _load() -> list[dict]:
    if not _DATA.exists():
        return []
    mtime = _DATA.stat().st_mtime
    if mtime != _cache['mtime']:
        payload = json.loads(_DATA.read_text(encoding='utf-8'))
        raw = payload.get('items') if isinstance(payload, dict) else payload
        _cache['items'] = [x for x in (raw or []) if isinstance(x, dict) and x.get('id')]
        _cache['mtime'] = mtime
    return _cache['items']


def _package(item: dict) -> bytes:
    manifest = {key: item.get(key) for key in
                ('id', 'name', 'kind', 'category', 'description', 'author', 'version', 'prompt')}
    manifest['metadata_sha256'] = hashlib.sha256(
        json.dumps(item, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    skill_md = (f"# {item['name']}\n\n{item.get('description') or ''}\n\n"
                f"## Prompt\n\n{item.get('prompt') or ''}\n")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in (('SKILL.md', skill_md),
                              ('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


def _detail(item: dict, base_path: str = '') -> dict:
    package = _package(item)
    path = base_path.rstrip('/') or f"/v1/skills/{item['id']}"
    return {
        **item,
        'package': {
            'url': f"{path}/download",
            'sha256': hashlib.sha256(package).hexdigest(),
            'bytes': len(package),
        },
    }


def _sort_key(sort: str):
    if sort == 'usage':
        return lambda x: (-int(x.get('usage') or 0), x['name'])
    if sort == 'installs':
        return lambda x: (-int(x.get('installs') or 0), x['name'])
    if sort == 'latest':
        return lambda x: (str(x.get('created') or ''), x['name'])
    return lambda x: (0 if x.get('featured') else 1,
                      -int(x.get('installs') or 0), x['name'])


@router.get('/skills')
async def registry_skills(
        kind: str = Query(default='all'),
        q: str = Query(default='', max_length=80),
        category: str = Query(default='', max_length=40),
        sort: str = Query(default='comprehensive'),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=40, ge=1, le=200)):
    """目录：服务端筛选、排序、分页。"""
    items = _load()
    if kind in ('skill', 'connector'):
        items = [x for x in items if (x.get('kind') or 'skill') == kind]
    if category:
        items = [x for x in items if x.get('category') == category]
    keyword = q.strip().lower()
    if keyword:
        items = [x for x in items
                 if keyword in str(x.get('name', '')).lower()
                 or keyword in str(x.get('description', '')).lower()]
    items = sorted(items, key=_sort_key(sort))
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]
    return {'items': page_items, 'total': total, 'page': page,
            'page_size': page_size, 'has_more': start + page_size < total}


@router.get('/skills/{skill_id}')
async def registry_skill(skill_id: str, request: Request):
    for item in _load():
        if item.get('id') == skill_id:
            return _detail(item, base_path=request.url.path)
    raise HTTPException(status_code=404, detail='技能不存在')


@router.get('/skills/{skill_id}/download')
async def registry_download(skill_id: str, version: str = Query(default='')):
    for item in _load():
        if item.get('id') == skill_id:
            package = _package(item)
            filename = f"{skill_id}-{item.get('version') or '1.0.0'}.zip"
            return Response(package, media_type='application/zip', headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'X-Package-SHA256': hashlib.sha256(package).hexdigest(),
            })
    raise HTTPException(status_code=404, detail='技能不存在')
