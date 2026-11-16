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
from webpage_agent.core.config import settings
from webpage_agent.governance import policies
from webpage_agent.skillhub import catalog, remote, store

router = APIRouter(prefix='/api/skills', tags=['skillhub'])
registry_router = APIRouter(prefix='/api/skillhub', tags=['skillhub'])
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_KINDS = ('skill', 'connector')
_SORTS = ('comprehensive', 'usage', 'installs', 'latest')
_TABS = ('recommend', 'skillhub', 'suite')
# 上传包上限来自配置（`SKILL_MAX_ARCHIVE_MB`，0 = 不限），与 `addons.skills`
# 用同一套取值函数。这里再挡一道是因为 multipart 会先把文件落进临时文件、
# 再被整体读进内存，不能等到解包阶段才发现太大。
_UPLOAD_CHUNK = 1024 * 1024


def _archive_limit() -> int:
    """整包上限（字节）。0 = 不限。"""
    return skills_lib.max_archive_bytes()


class CustomSkill(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default='', max_length=240)
    category: str = '效率工具'
    kind: Literal['skill', 'connector'] = 'skill'
    # 用户自建技能也用控制台那张表单的字段：图标、概述、示例问法、正文。
    # 图标可以是 emoji，也可以是前端缩到 128px 后传上来的 data URL。
    icon: str = Field(default='', max_length=300_000)
    overview: str = Field(default='', max_length=600)
    prompt: str = Field(default='', max_length=20_000)
    examples: list[str] = Field(default_factory=list, max_length=8)


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
        item['file_tree'] = row.get('file_tree') or []
        item['root_path'] = row.get('root_path') or ''
        # 目录条目缺概述/示例时，用磁盘上 SKILL.md 里写的补——上传的技能包没有目录条目。
        if not item.get('overview') and row.get('overview'):
            item['overview'] = row['overview']
        if not item.get('examples') and row.get('examples'):
            item['examples'] = list(row['examples'])
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
        'overview': row.get('overview') or '',
        'examples': list(row.get('examples') or []),
        'category': row.get('category') or '效率工具',
        'author': '我', 'installs': 0, 'usage': 0, 'featured': False,
        'paid': False, 'source': row.get('source') or 'user', 'suite': '',
        'version': row.get('version') or '1.0.0',
        'created': (row.get('updated_at') or '')[:10],
        'prompt': '', 'custom': True,
    }


def _platform_items() -> list[dict]:
    """控制台发布的平台技能 → 目录条目。

    `source='platform'` 让前端能标出"管理员发布"，也给以后的按来源筛选留了口子。
    """
    from webpage_agent.store import platform_catalog

    out: list[dict] = []
    for row in platform_catalog.enabled_skills():
        # 要判断"已装的那份是不是当前这个包"，就得有指纹。存量条目上线前传的、
        # 没记过摘要，这里补一次（之后不再读盘）。
        resolved = platform_catalog.package_path(row.get('package'))
        out.append({
            'id': row['id'],
            'name': row.get('name') or row['id'],
            'kind': 'skill',
            'icon': row.get('icon') or '✨',
            'color': '#eef4ff',
            'description': row.get('description') or '',
            'overview': row.get('overview') or '',
            'examples': list(row.get('examples') or []),
            'category': row.get('category') or '效率工具',
            'author': '平台管理员',
            'installs': 0,
            'usage': 0,
            'featured': False,
            'paid': False,
            'source': 'platform',
            'suite': '',
            'version': row.get('version') or '1.0.0',
            'created': str(row.get('created_at') or '')[:10],
            'prompt': row.get('prompt') or '',
            'platform': True,
            'installed_by_default': bool(row.get('installed_by_default')),
            # 有整包 ≠ 已安装：前者只是"服务端存了原件"，用户还得点「+」。
            'has_package': bool(row.get('package')),
            'package_files': int(row.get('package_files') or 0),
            # 包指纹：判断"用户磁盘上那份是不是当前这个包"唯一可靠的依据。
            'package_digest': _package_digest(row, resolved),
        })
    return out


def _platform_package(skill_id: str) -> dict | None:
    """平台技能上传过整包的话，返回它在磁盘上的位置。

    返回 `None` 表示"只能按目录元数据生成 SKILL.md"——包没上传、或者文件被
    人工删掉了（换机器、清目录）。这时退回生成模式，技能至少还能用，不会
    因为包丢了一份就整条链路失败。
    """
    from webpage_agent.store import platform_catalog

    try:
        rows = platform_catalog.load()['skills']
    except (OSError, ValueError):
        return None
    for row in rows:
        if row.get('id') != skill_id:
            continue
        # 路径存在 JSON 里，但可信度只有"当前这台机器"：`package_path` 会按
        # 相对包仓库的路径重新解析，找不到才认输。
        path = platform_catalog.package_path(row.get('package'))
        if path is None:
            return None
        return {'path': str(path),
                'version': str(row.get('package_version') or row.get('version') or '1.0.0'),
                'source': 'platform',
                'files': int(row.get('package_files') or 0),
                'digest': _package_digest(row, path)}
    return None


def _package_digest(row: dict, path: Path | None) -> str:
    """目录里记的包指纹；老条目没记过，按文件算一次并补进目录。

    只在这里补：调用方都是"真要读这个包"的路径（安装、同步），不是列表热路径。
    """
    digest = str(row.get('package_digest') or '')
    if digest or path is None:
        return digest
    from webpage_agent.store import platform_catalog

    try:
        digest = skills_lib.package_digest(path.read_bytes())
        platform_catalog.set_package_digest(str(row.get('id') or ''), digest)
    except (OSError, ValueError):                      # pragma: no cover - 包被删/没权限
        return ''
    return digest


async def _catalog_items(principal) -> list[dict]:
    data = store.load(principal)
    items = [dict(x) for x in catalog.SKILLS] + [dict(x) for x in catalog.CONNECTORS]
    items.extend(dict(x) for x in data['custom'])
    # 平台技能排在内置之后、远端之前：管理员发布的东西不该被远端目录顶掉。
    seen = {x['id'] for x in items}
    for entry in _platform_items():
        if entry['id'] not in seen:
            items.append(entry)
            seen.add(entry['id'])
    # 远端目录：同 id 时本地优先，避免远端覆盖内置条目。
    for entry in await remote.fetch_items():
        if entry['id'] not in seen:
            items.append(dict(entry))
            seen.add(entry['id'])
    installed = set(data['installed'])
    disk = _disk_skills()
    # 「默认安装」的平台技能：管理员勾了这个选项，就代表新用户开箱即用。
    # 只有磁盘上真的缺文件时才写一次——list 是热路径，不能每次都落盘。
    for item in items:
        if not item.get('installed_by_default') or item['id'] in disk:
            continue
        try:
            # 有整包就按整包装（带 references/scripts），没有才生成 SKILL.md。
            _materialize_skill(item, _platform_package(item['id']))
        except (skills_lib.SkillError, OSError):
            continue
        disk = _disk_skills()
    # 换包之后自动对齐：管理员上传时已经同步过一次（见 console_api），这里兜的是
    # "上传时同步失败了 / 包是从别的机器拷过来的 / 目录被手工改过"。用户下次打开
    # 技能中心就自己修好，不用点按钮，更不用删目录重装。
    repaired = False
    for item in items:
        if not item.get('platform') or item.get('id') not in disk:
            continue
        row = disk.get(item['id']) or {}
        digest = str(item.get('package_digest') or '')
        if not digest or str(row.get('package_digest') or '') == digest:
            continue
        try:
            package = _platform_package(item['id'])
            if package is None:
                continue
            if _sync_if_stale(item, package, row) is not row:
                repaired = True
        except (skills_lib.SkillError, OSError):
            continue
    if repaired:
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
    # 默认排序：精选在最前，平台管理员发布的紧随其后。
    # 平台技能 installs 恒为 0，按原规则会沉到几百条末尾——刚发完就"看不见"，
    # 管理员只能靠搜索找到，等于没发。
    return lambda x: (0 if (x.get('featured') or x.get('platform')) else 1,
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
        existing: bool = Query(default=False),
        principal=Depends(policies.current_principal)):
    """技能/连接器目录。所有筛选都在服务端完成，前端只负责渲染。

    `existing=1` 是「技能管理」视角：只返回真实存在的技能——磁盘上有 SKILL.md 的，
    或管理员在控制台发布并启用的平台技能。内置目录与远端目录属于"商店货架"，
    不在这一页出现。
    """
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
    if existing:
        items = [x for x in items if x.get('on_disk') or x.get('platform')]

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


@router.get('/upload-limits')
async def upload_limits():
    """上传体积上限，给前端显示与预校验用。0 = 不限。

    前端**不再写死数字**：之前 UI 里写 20 MB、后端常量也写 20 MB，改一处就漂一次，
    用户看到的是"界面说超限、接口说没超"这种对不上的提示。上限只有一个来源。
    不挂鉴权：它只是三个整数，不含任何业务数据。
    """
    return {'max_archive_bytes': skills_lib.max_archive_bytes(),
            'max_member_bytes': skills_lib.max_member_bytes(),
            'max_members': skills_lib.max_members()}


@router.get('/{skill_id}')
async def get_skill(skill_id: str, principal=Depends(policies.current_principal)):
    item = _find(await _catalog_items(principal), skill_id)
    return item


@router.post('', status_code=201)
async def create_custom(payload: CustomSkill,
                        principal=Depends(policies.current_principal)):
    """新建自定义技能/连接器，并自动安装。"""
    data = store.load(principal)
    description = payload.description.strip()
    prompt = payload.prompt.strip()
    examples = [x.strip()[:120] for x in payload.examples if x.strip()][:8]
    item = {
        'id': 'custom-' + uuid.uuid4().hex[:8],
        'name': payload.name.strip(),
        'kind': payload.kind,
        'icon': (payload.icon or '').strip() or '✨',
        'color': '#e9faf5',
        'description': description or '我创建的自定义技能。',
        'overview': payload.overview.strip(),
        'examples': examples,
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
        # 正文优先用上传/粘贴进来的那份，没有才用「名字：描述」兜底。
        'prompt': (prompt or f"{payload.name.strip()}：{description}".strip('：')),
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


def _sync_if_stale(item: dict, package: dict | None, existing: dict) -> dict:
    """已装目录 vs 当前包：不一样就把包重新盖上去，一样就原样返回。

    这是"老用户点安装也不更新"的补丁。指纹为空（生成的 SKILL.md、手写技能、
    导入的目录）也算"不一样"——那正是当年只落了一个 SKILL.md 的那种。
    """
    path = str((package or {}).get('path') or '')
    skill_id = str(existing.get('id') or '')
    if not path or not skill_id:
        return existing
    digest = str((package or {}).get('digest') or '')
    current = str(existing.get('package_digest') or '')
    if digest and current == digest:
        return existing
    try:
        payload = Path(path).read_bytes()
        if not digest:
            digest = skills_lib.package_digest(payload)
        if current == digest:
            return existing
        synced = skills_lib.sync_installed(
            Path(path).name, payload, skill_id=skill_id, digest=digest,
            version=str((package or {}).get('version') or ''),
            fallback_description=str(item.get('description') or ''))
    except (OSError, skills_lib.SkillError):
        # 包坏了不阻断：技能还能按旧文件用，报错留给同步入口去说。
        return existing
    if not synced:
        return existing
    return skills_lib.get_skill(skill_id) or existing


def _materialize_skill(item: dict, package: dict | None) -> dict:
    """把目录条目变成磁盘技能：优先用下载到的技能包，其次生成最小 SKILL.md。

    下载包里的 SKILL.md 可能没有 frontmatter（远端生成器只写正文），描述用条目
    介绍补上——描述是列表里唯一进模型上下文的信息，缺了等于技能不可见。
    """
    skill_id = skills_lib.slug(str(item.get('id') or ''), fallback='')
    existing = skills_lib.get_skill(skill_id) if skill_id else None
    if existing is not None:
        # 装过了不等于装对了：管理端换过包、或者当年是"按描述生成"装的，
        # 磁盘上那份就是旧的。安装接口是幂等的，只能在这里把包补上。
        return _sync_if_stale(item, package, existing)
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
                Path(settings.skillhub_packages_dir))
        except Exception as exc:                       # noqa: BLE001
            raise HTTPException(status_code=502,
                                detail=f'技能包下载失败：{exc}') from None
    elif item.get('platform'):
        # 管理端上传过整包：直接解本地包，不再"按描述生成 SKILL.md"。
        package = _platform_package(skill_id)
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
    limit = _archive_limit()
    payload = bytearray()
    # 分块读而不是 `file.read(limit + 1)`：不限体积时后者会把整个文件一次
    # 塞进内存；分块至少让超额请求在超出的那一刻就停下，而不是先吃满内存。
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        payload.extend(chunk)
        if limit and len(payload) > limit:
            raise HTTPException(
                status_code=413,
                detail=f'技能包超过 {limit // (1024 * 1024)} MB 上限')
    payload = bytes(payload)
    if not payload:
        raise HTTPException(status_code=400, detail='文件为空')
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
