"""技能中心 API：目录、搜索、筛选、安装/卸载、自定义技能、上传/导入/启停。

挂载点：`/api/skills`（见 routes.py）。

目录（catalog）只描述"有哪些技能"；**真正能被会话使用的是磁盘上的技能包**
（`addons.skills`：`<SKILLS_DIR>/<id>/SKILL.md`）。两者在这里合流：

- 列表把磁盘技能并进目录（没进目录的用户上传也能出现在技能中心）；
- 安装目录条目 = 生成一份 SKILL.md 落盘（`skill_md_from_catalog`）；
- 上传/导入直接产生磁盘技能，不需要先进目录。
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import (APIRouter, Depends, File, HTTPException, Query, UploadFile)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.addons import skills as skills_lib
from webpage_agent.governance import policies
from webpage_agent.skillhub import catalog, remote, store

router = APIRouter(prefix='/api/skills', tags=['skillhub'])
registry_router = APIRouter(prefix='/api/skillhub', tags=['skillhub'])
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_KINDS = ('skill', 'connector')
_SORTS = ('comprehensive', 'usage', 'installs', 'latest')
_TABS = ('recommend', 'skillhub', 'suite')
# 上传包上限与 `addons.skills` 保持一致；这里再挡一道是因为 multipart 会先把
# 整个文件读进内存/临时文件，不能等到解包阶段才发现太大。
MAX_UPLOAD_BYTES = skills_lib.MAX_ARCHIVE_BYTES + 1024


class CustomSkill(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default='', max_length=240)
    category: str = '效率工具'
    kind: Literal['skill', 'connector'] = 'skill'


class ImportBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=1024)
    name: str = Field(default='', max_length=80)


def _disk_skills() -> dict[str, dict]:
    try:
        return {row['id']: row for row in skills_lib.list_skills()}
    except OSError:
        return {}


def _decorate(item: dict, disk: dict[str, dict], installed: set[str]) -> dict:
    row = disk.get(item['id'])
    item['on_disk'] = row is not None
    item['installed'] = bool(item.get('installed')) or row is not None
    item['enabled'] = bool(row.get('enabled')) if row else False
    if row is not None:
        # 磁盘是事实来源：目录里的简介可能过时，SKILL.md 才是模型看到的东西。
        item['name'] = row.get('name') or item.get('name')
        item['description'] = row.get('description') or item.get('description')
        item['version'] = row.get('version') or item.get('version')
        item['files'] = row.get('files') or []
        item['root_path'] = row.get('root_path') or ''
    elif item['id'] in installed and item.get('kind') == 'skill':
        # 目录里"安装过"但磁盘没有：老版本只记了 id，没有 SKILL.md。
        # 标成 need_files，前端可以提示"重新安装以生成技能文件"。
        item['need_files'] = True
    return item


def _disk_item(row: dict) -> dict:
    """磁盘上有、目录里没有的技能 → 目录条目。"""
    return {
        'id': row['id'], 'name': row['name'], 'kind': 'skill',
        'icon': '📁', 'color': '#eef4ff',
        'description': row.get('description') or '',
        'category': row.get('category') or '效率工具',
        'author': '我', 'installs': 0, 'usage': 0, 'featured': False,
        'paid': False, 'source': row.get('source') or 'user', 'suite': '',
        'version': row.get('version') or '1.0.0',
        'created': (row.get('updated_at') or '')[:10],
        'prompt': '', 'custom': True,
    }


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
    disk = _disk_skills()
    for skill_id, row in disk.items():
        if skill_id not in seen:
            items.append(_disk_item(row))
            seen.add(skill_id)
        else:
            # 目录条目与磁盘技能同 id：磁盘存在就等于"已安装"。
            installed.add(skill_id)
    return [_decorate(item, disk, installed) for item in items]


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
    # 自建技能也落磁盘：只存一条 id 的技能在会话里读不到任何内容，
    # 而"创建技能"的下一步就是 @ 它。
    if item['kind'] == 'skill':
        try:
            skills_lib.install_from_catalog(item)
        except skills_lib.SkillError:                 # pragma: no cover - 名字极端时
            pass
    return item


def _materialize_skill(item: dict, package: dict | None) -> dict:
    """把目录条目变成磁盘技能：优先用下载到的技能包，其次生成最小 SKILL.md。

    下载包里的 SKILL.md 可能没有 frontmatter（远端生成器只写正文），描述用条目
    介绍补上——描述是列表里唯一进模型上下文的信息，缺了等于技能不可见。
    """
    skill_id = skills_lib.slug(str(item.get('id') or ''), fallback='')
    existing = skills_lib.get_skill(skill_id) if skill_id else None
    if existing is not None:
        return existing
    path = str((package or {}).get('path') or '')
    if path:
        try:
            payload = Path(path).read_bytes()
            return skills_lib.install_archive(
                Path(path).name, payload, skill_id=skill_id,
                fallback_description=str(item.get('description') or ''))
        except (OSError, skills_lib.SkillError):
            # 包坏了不阻断安装：退回"用条目元数据生成 SKILL.md"，
            # 至少会话里能读到技能说明，而不是点了安装什么都没有。
            pass
    return skills_lib.install_from_catalog(item)


@router.post('/{skill_id}/install')
async def install_skill(skill_id: str, principal=Depends(policies.current_principal)):
    """安装 = 目录条目落成磁盘技能（幂等）。没有 SKILL.md 的技能不能被会话使用。"""
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
    # 连接器没有 SKILL.md：它的"安装"在连接器面板里（连接/授权），不落技能目录。
    written = None
    if item.get('kind') != 'connector':
        try:
            written = _materialize_skill(item, package)
        except skills_lib.SkillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    fresh = await _catalog_items(principal)
    return {'id': skill_id, 'installed': True, 'name': item['name'],
            'package': package, 'skill': written,
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


# ---------------------------------------------------------------- 上传 / 导入


@router.post('/upload', status_code=201)
async def upload_skill(file: UploadFile = File(...),
                       name: str = Query(default='', max_length=80),
                       principal=Depends(policies.current_principal)):
    """上传技能包：.zip / .tar.gz / .tgz / 单个 SKILL.md。

    与 WorkBuddy 的 `assertSkillUploadFrontmatterValid` 对齐：**必须有 SKILL.md，
    且 frontmatter 里要有 description**。校验失败直接 400，并说明缺什么——
    技能装进去但模型读不到描述，等于没装。
    """
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=400, detail='文件为空')
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f'技能包超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限')
    try:
        skill = skills_lib.install_archive(file.filename or 'SKILL.md', payload,
                                           skill_id=name)
    except skills_lib.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    data = store.load(principal)
    if skill['id'] not in data['installed']:
        data['installed'].append(skill['id'])
        store.save(principal, data)
    return {'skill': skill, 'id': skill['id'], 'installed': True,
            'on_disk': True, 'enabled': True}


@router.post('/import', status_code=201)
async def import_skill(body: ImportBody,
                       principal=Depends(policies.current_principal)):
    """把服务器上的一个本地目录变成技能（WorkBuddy 的「从路径导入」）。

    路径由操作者提供，所以只在"导入"这一处接受绝对路径；解包、物化都不接受。
    """
    try:
        skill = skills_lib.import_from_path(body.path, skill_id=body.name)
    except skills_lib.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    data = store.load(principal)
    if skill['id'] not in data['installed']:
        data['installed'].append(skill['id'])
        store.save(principal, data)
    return {'skill': skill, 'id': skill['id'], 'installed': True,
            'on_disk': True, 'enabled': True}


@router.post('/{skill_id}/enable')
async def enable_skill(skill_id: str, enabled: bool = Query(default=True),
                       principal=Depends(policies.current_principal)):
    """启停是软开关：只写 `.state.json`，不删目录（禁用往往是临时的）。"""
    try:
        row = skills_lib.set_enabled(skill_id, enabled)
    except skills_lib.SkillError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return row


@router.delete('/{skill_id}')
async def delete_skill(skill_id: str, principal=Depends(policies.current_principal)):
    """删除磁盘技能（不是卸载目录条目：卸载见 `DELETE /{id}/install`）。"""
    removed = skills_lib.remove_skill(skill_id)
    if not removed:
        raise HTTPException(status_code=404, detail='磁盘上没有这个技能')
    data = store.load(principal)
    data['installed'] = [x for x in data['installed'] if x != skill_id]
    store.save(principal, data)
    return {'id': skill_id, 'deleted': True, 'installed': False}
