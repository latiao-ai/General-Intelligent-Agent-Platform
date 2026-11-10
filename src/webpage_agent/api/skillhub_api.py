"""技能中心 API：目录、搜索、筛选、安装/卸载、自定义技能。

挂载点：`/api/skills`（见 routes.py）。
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.governance import policies
from webpage_agent.skillhub import catalog, remote, store

router = APIRouter(prefix='/api/skills', tags=['skillhub'])
registry_router = APIRouter(prefix='/api/skillhub', tags=['skillhub'])
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_KINDS = ('skill', 'connector')
_SORTS = ('comprehensive', 'usage', 'installs', 'latest')
_TABS = ('recommend', 'skillhub', 'suite')


class CustomSkill(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default='', max_length=240)
    category: str = '效率工具'
    kind: Literal['skill', 'connector'] = 'skill'


async def _catalog_items(principal) -> list[dict]:
    data = store.load(principal)
    items = [dict(x) for x in catalog.SKILLS] + [dict(x) for x in catalog.CONNECTORS]
    items.extend(dict(x) for x in data['custom'])
    # 远端目录：同 id 时本地优先，避免远端覆盖内置条目。
    seen = {x['id'] for x in items}
    for entry in await remote.fetch_items():
        if entry['id'] not in seen:
            items.append(dict(entry))
            seen.add(entry['id'])
    installed = set(data['installed'])
    for item in items:
        item['installed'] = item['id'] in installed
    return items


def _sort_key(sort: str):
    if sort == 'usage':
        return lambda x: (-int(x.get('usage') or 0), x['name'])
    if sort == 'installs':
        return lambda x: (-int(x.get('installs') or 0), x['name'])
    if sort == 'latest':
        return lambda x: (str(x.get('created') or ''), x['name'])
    return lambda x: (0 if x.get('featured') else 1,
                      -int(x.get('installs') or 0), x['name'])


def _find(items: list[dict], skill_id: str) -> dict:
    for item in items:
        if item.get('id') == skill_id:
            return item
    raise HTTPException(status_code=404, detail='技能不存在')


def _installed_count(items: list[dict], kind: str) -> int:
    return sum(1 for x in items if x.get('kind') == kind and x.get('installed'))


@registry_router.get('/registry')
async def registry_file():
    """内置示例目录：返回 deploy/skillhub.registry.json。"""
    path = _PROJECT_ROOT / 'deploy' / 'skillhub.registry.json'
    if not path.exists():
        raise HTTPException(status_code=404, detail='registry 不存在')
    return JSONResponse(json.loads(path.read_text(encoding='utf-8')),
                        headers={'Cache-Control': 'no-store'})


@registry_router.get('/status')
async def registry_status():
    """排查用：当前远端地址与成功加载的条数。"""
    items = await remote.fetch_items(force=True)
    return {'url': remote.registry_url(), 'items': len(items),
            'skills': sum(1 for x in items if x.get('kind') == 'skill'),
            'connectors': sum(1 for x in items if x.get('kind') == 'connector')}


@router.get('')
async def list_skills(
        kind: str = Query(default='skill'),
        q: str = Query(default='', max_length=80),
        category: str = Query(default='', max_length=40),
        sort: str = Query(default='comprehensive'),
        tab: str = Query(default='recommend'),
        installed: bool = Query(default=False),
        principal=Depends(policies.current_principal)):
    """技能/连接器目录。所有筛选都在服务端完成，前端只负责渲染。"""
    kind = kind if kind in _KINDS else 'skill'
    sort = sort if sort in _SORTS else 'comprehensive'
    tab = tab if tab in _TABS else 'recommend'

    all_items = await _catalog_items(principal)
    pool = [x for x in all_items if x.get('kind') == kind]

    categories = []
    for name in catalog.CATEGORIES:
        count = sum(1 for x in pool if x.get('category') == name)
        if count:
            categories.append({'name': name, 'count': count})

    items = pool
    if tab == 'skillhub':
        items = [x for x in items if x.get('source') == 'skillhub']
    elif tab == 'suite':
        items = [x for x in items if x.get('source') == 'suite']
    if category:
        items = [x for x in items if x.get('category') == category]
    keyword = q.strip().lower()
    if keyword:
        items = [x for x in items
                 if keyword in str(x.get('name', '')).lower()
                 or keyword in str(x.get('description', '')).lower()
                 or keyword in str(x.get('author', '')).lower()]
    if installed:
        items = [x for x in items if x.get('installed')]

    items = sorted(items, key=_sort_key(sort))
    featured = sorted([x for x in pool if x.get('featured')],
                      key=lambda x: -int(x.get('installs') or 0))[:4]
    featured_pool = sorted(
        pool, key=lambda x: (0 if x.get('featured') else 1,
                             -int(x.get('installs') or 0)))[:8]
    return {
        'items': items,
        'featured': featured,
        'featured_pool': featured_pool,
        'categories': categories,
        'total': len(items),
        'installed_count': _installed_count(all_items, kind),
    }


@router.get('/{skill_id}')
async def get_skill(skill_id: str, principal=Depends(policies.current_principal)):
    item = _find(await _catalog_items(principal), skill_id)
    return item


@router.post('', status_code=201)
async def create_custom(payload: CustomSkill,
                        principal=Depends(policies.current_principal)):
    """新建自定义技能/连接器，并自动安装。"""
    data = store.load(principal)
    item = {
        'id': 'custom-' + uuid.uuid4().hex[:8],
        'name': payload.name.strip(),
        'kind': payload.kind,
        'icon': '✨',
        'color': '#e9faf5',
        'description': payload.description.strip() or '我创建的自定义技能。',
        'category': payload.category if payload.category in catalog.CATEGORIES else '效率工具',
        'author': '我',
        'installs': 0,
        'usage': 0,
        'featured': False,
        'paid': False,
        'source': 'custom',
        'suite': '',
        'version': '1.0.0',
        'created': date.today().isoformat(),
        'prompt': f"{payload.name.strip()}：{payload.description.strip()}".strip('：'),
        'custom': True,
    }
    data['custom'].append(item)
    data['installed'].append(item['id'])
    store.save(principal, data)
    item['installed'] = True
    return item


@router.post('/{skill_id}/install')
async def install_skill(skill_id: str, principal=Depends(policies.current_principal)):
    items = await _catalog_items(principal)
    item = _find(items, skill_id)
    package = None
    if item.get('remote'):
        try:
            package = await remote.download_package(
                skill_id, item.get('version') or '1.0.0',
                _PROJECT_ROOT / 'profile' / 'skillhub' / 'packages')
        except Exception as exc:                       # noqa: BLE001
            raise HTTPException(status_code=502,
                                detail=f'技能包下载失败：{exc}') from None
    data = store.load(principal)
    if skill_id not in data['installed']:
        data['installed'].append(skill_id)
        store.save(principal, data)
    fresh = await _catalog_items(principal)
    return {'id': skill_id, 'installed': True, 'name': item['name'],
            'package': package,
            'installed_count': _installed_count(fresh, item['kind'])}


@router.delete('/{skill_id}/install')
async def uninstall_skill(skill_id: str, principal=Depends(policies.current_principal)):
    items = await _catalog_items(principal)
    item = _find(items, skill_id)
    data = store.load(principal)
    data['installed'] = [x for x in data['installed'] if x != skill_id]
    store.save(principal, data)
    fresh = await _catalog_items(principal)
    return {'id': skill_id, 'installed': False, 'name': item['name'],
            'installed_count': _installed_count(fresh, item['kind'])}
