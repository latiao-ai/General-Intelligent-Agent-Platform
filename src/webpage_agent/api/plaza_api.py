"""Bot 广场接口：浏览、详情、一键启用、投稿与撤回。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/bot-plaza` | 广场列表（分类计数 + 已装标记 + 关键词/分类/排序） |
| GET | `/api/bot-plaza/mine` | 我的投稿（含审核状态与驳回理由）+ 我装过的 |
| DELETE | `/api/bot-plaza/mine/{item_id}` | 撤回自己的投稿（下架，不删除历史） |
| GET | `/api/bot-plaza/{item_id}` | 条目详情（含 readme、推荐开场白） |
| POST | `/api/bot-plaza/{item_id}/install` | **点「使用」**：在我名下装一份副本并返回会话 |
| DELETE | `/api/bot-plaza/{item_id}/install` | 卸载副本（归档机器人，不动历史消息） |
| POST | `/api/bots/{bot_id}/publish` | 把我的机器人投稿到广场（进入 pending 评审） |
| DELETE | `/api/bots/{bot_id}/publish` | 撤回该机器人的投稿 |

设计要点（全文见 `docs/bot-plaza.md`）：

- **广场是快照，启用是复制**。用户点「使用」拿到的是自己名下的新机器人，人设、
  技能、连接器、模型都是条目发布那一刻的副本；之后广场改版、下架、删除都不
  影响已经装好的人。这也是不做"远程引用"的原因：机器人的记忆与家目录必须在
  用户自己的私有根里，跨账号共享一份身份会把审计线索抹掉。
- **启用是幂等的**。同一个条目对同一个人只会有一个副本（`bot_plaza_installs`
  唯一约束）；已装过再点「使用」直接回到原来那个机器人，不新建、不重复计数。
- **条目可见性两档**：`public`（全平台）与 `tenant`（只有发布方企业能看到）。
  用户投稿一律先落 `tenant + pending`——跨企业可见必须由平台管理员显式上架。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.api.bots_api import MAX_BOTS
from webpage_agent.bot import plaza as plaza_domain
from webpage_agent.bot import prompts
from webpage_agent.bot.service import ensure_bot_home
from webpage_agent.core.config import settings
from webpage_agent.governance import policies

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api', tags=['bot-plaza'])

# 分类 / 状态 / 视图都在 `bot/plaza.py`：控制台与仓储也读同一份，避免各写一套口径。
CATEGORIES = plaza_domain.CATEGORIES
CATEGORY_IDS = plaza_domain.CATEGORY_IDS
MAX_README = plaza_domain.MAX_README
MAX_TAGS = plaza_domain.MAX_TAGS
MAX_STARTERS = plaza_domain.MAX_STARTERS


class PublishBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category: str = Field(default='general', max_length=24)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    readme: str = Field(default='', max_length=MAX_README)
    starters: list[str] = Field(default_factory=list, max_length=MAX_STARTERS)
    note: str = Field(default='', max_length=200)


def _store(request: Request):
    store = getattr(request.app.state, 'store', None)
    if store is None:
        raise HTTPException(status_code=503, detail='服务未就绪：没有数据库连接')
    return store


def _ids(principal) -> tuple[str, str]:
    return (str(getattr(principal, 'tenant_id', 'default') or 'default'),
            str(getattr(principal, 'user_id', 'default') or 'default'))


def _enabled() -> None:
    if not settings.bot_mode_enabled:
        raise HTTPException(status_code=403, detail='机器人能力未开放（BOT_MODE_ENABLED=false）')
    if not settings.bot_plaza_enabled:
        raise HTTPException(status_code=403, detail='Bot 广场未开放（BOT_PLAZA_ENABLED=false）')


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _unique_handle(store, *, tenant: str, user: str, desired: str,
                         name: str) -> str:
    """装副本时的 handle 去重：同一个用户可能已经有一个同名机器人。"""
    handle = prompts.normalize_handle(desired or name, name=name)
    if not handle:
        handle = 'bot-' + uuid.uuid4().hex[:8]
    existing = await store.list_bot_rows(tenant_id=tenant, owner_user_id=user,
                                         include_archived=True)
    taken = {str(item.get('handle') or '').lower() for item in existing}
    base, suffix = handle, 2
    while handle.lower() in taken:
        handle = f'{base[:20]}-{suffix}'
        suffix += 1
    return handle


async def _unique_slug(store, base: str) -> str:
    """slug 全局唯一（唯一索引兜底，这里先挑一个不撞的，避免直接 500）。"""
    slug = (prompts.normalize_handle(base) or 'bot')[:40]
    candidate = slug
    suffix = 2
    while await store.plaza_item_by_handle(candidate) is not None:
        candidate = f'{slug[:34]}-{suffix}'
        suffix += 1
    return candidate


# ---------------------------------------------------------------- 浏览


@router.get('/bot-plaza')
async def list_plaza(request: Request, q: str = Query('', max_length=80),
                     category: str = Query('', max_length=24),
                     order: str = Query('featured', max_length=12),
                     limit: int = Query(0, ge=0, le=200),
                     principal=Depends(policies.current_principal)):
    """广场列表。`installed` 标记来自安装登记，不来自机器人行上的标记。"""
    store = _store(request)
    tenant, user = _ids(principal)
    if category and category not in CATEGORY_IDS:
        category = ''
    size = limit or settings.bot_plaza_page_size
    items = await store.list_plaza_items(
        viewer_tenant=tenant, category=category, q=q.strip(),
        order=order if order in ('featured', 'new', 'popular') else 'featured',
        limit=size)
    installs = {str(row.get('item_id') or ''): str(row.get('bot_id') or '')
                for row in await store.list_plaza_installs(tenant_id=tenant,
                                                           owner_user_id=user)}
    # 分类计数按"当前这批可见条目"算：用户看到的数字和点进去的条数必须一致。
    all_visible = items if not (q.strip() or category) else await store.list_plaza_items(
        viewer_tenant=tenant, limit=200)
    counts: dict[str, int] = {}
    for item in all_visible:
        key = str(item.get('category') or 'general')
        counts[key] = counts.get(key, 0) + 1
    return {
        'enabled': bool(settings.bot_mode_enabled and settings.bot_plaza_enabled),
        'categories': [{'id': key, 'name': name, 'count': counts.get(key, 0)}
                       for key, name in CATEGORIES],
        'items': [plaza_domain.list_view(item, installed_bot_id=installs.get(
            str(item.get('item_id') or ''), '')) for item in items],
        'installed_count': len([value for value in installs.values() if value]),
        'total': len(all_visible),
    }


@router.get('/bot-plaza/mine')
async def my_plaza(request: Request, principal=Depends(policies.current_principal)):
    """我的投稿与我的安装。工作台侧栏用它显示"已上架 / 审核中"和投稿入口。"""
    store = _store(request)
    tenant, user = _ids(principal)
    items = await store.list_plaza_items(statuses=(), viewer_tenant='',
                                         publisher_tenant=tenant,
                                         publisher_user_id=user, order='new',
                                         limit=50)
    installs = await store.list_plaza_installs(tenant_id=tenant, owner_user_id=user)
    by_id = {str(item.get('item_id') or ''): item for item in items}
    install_rows = []
    for row in installs:
        item = by_id.get(str(row.get('item_id') or '')) or {}
        install_rows.append({
            'item_id': row.get('item_id'),
            'bot_id': row.get('bot_id') or '',
            'item_version': int(row.get('item_version') or 1),
            'name': item.get('name') or '',
            'icon': item.get('icon') or '🤖',
            'color': item.get('color') or '#eef4ff',
            'status': item.get('status') or '',
            'created_at': row.get('created_at') or '',
        })
    return {
        'items': [{
            'item_id': item.get('item_id'),
            'name': item.get('name') or '',
            'icon': item.get('icon') or '🤖',
            'color': item.get('color') or '#eef4ff',
            'category': item.get('category') or 'general',
            'tags': list(item.get('tags') or []),
            'readme': item.get('readme') or '',
            'starters': list(item.get('starters') or []),
            'status': item.get('status') or '',
            'scope': item.get('scope') or 'tenant',
            'review_note': item.get('review_note') or '',
            'install_count': int(item.get('install_count') or 0),
            'source_bot_id': item.get('source_bot_id') or '',
            'version': int(item.get('version') or 1),
            'updated_at': item.get('updated_at') or '',
        } for item in items],
        'installs': install_rows,
    }


@router.get('/bot-plaza/{item_id}')
async def plaza_detail(item_id: str, request: Request,
                       principal=Depends(policies.current_principal)):
    store = _store(request)
    tenant, user = _ids(principal)
    item = await store.plaza_item(item_id)
    if item is None or not plaza_domain.visible_to(item, tenant):
        raise HTTPException(status_code=404, detail='这条 Bot 不在广场上')
    install = await store.plaza_install_for(item_id=item_id, tenant_id=tenant,
                                            owner_user_id=user)
    try:                                            # 浏览量是观测量，不该因它失败
        await store.bump_plaza_counter(item_id, 'view_count', 1)
    except Exception:                               # noqa: BLE001
        logger.debug('浏览计数失败 item=%s', item_id, exc_info=True)
    return plaza_domain.detail_view(item, installed_bot_id=str((install or {}).get('bot_id') or ''))


# ---------------------------------------------------------------- 启用 / 卸载


@router.post('/bot-plaza/{item_id}/install')
async def install_plaza_bot(item_id: str, request: Request,
                            principal=Depends(policies.current_principal)):
    """点「使用」：把条目复制成我名下的机器人，并返回它的规范会话。

    幂等：已经装过（且机器人还在）就直接回到原来那个，不新建第二个。
    """
    _enabled()
    store = _store(request)
    tenant, user = _ids(principal)
    item = await store.plaza_item(item_id)
    if item is None or not plaza_domain.visible_to(item, tenant):
        raise HTTPException(status_code=404, detail='这条 Bot 不在广场上')

    install = await store.plaza_install_for(item_id=item_id, tenant_id=tenant,
                                            owner_user_id=user)
    if install and str(install.get('bot_id') or ''):
        bot = await store.bot_row(str(install['bot_id']), tenant_id=tenant,
                                  owner_user_id=user)
        if bot and str(bot.get('status') or '') == 'active':
            session = await store.ensure_bot_session(
                bot_id=str(bot['bot_id']), tenant_id=tenant, owner_user_id=user,
                session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
            return {
                'installed': False, 'reused': True,
                'bot_id': bot['bot_id'],
                'session_id': (session or {}).get('session_id') or '',
                'chat_url': f"/api/ui/bots?bot={bot['bot_id']}",
                'name': bot.get('name') or '',
                'item_id': item_id,
            }

    existing = await store.list_bot_rows(tenant_id=tenant, owner_user_id=user,
                                         include_archived=True)
    if len([row for row in existing
            if str(row.get('status') or '') == 'active']) >= MAX_BOTS:
        raise HTTPException(status_code=400,
                            detail=f'机器人已达上限（{MAX_BOTS} 个），先归档几个再装新的')

    handle = await _unique_handle(store, tenant=tenant, user=user,
                                  desired=str(item.get('handle') or ''),
                                  name=str(item.get('name') or '机器人'))
    bot_id = 'bot_' + uuid.uuid4().hex[:12]
    stamp = _stamp()
    row = await store.create_bot_row(
        bot_id=bot_id, tenant_id=tenant, owner_user_id=user,
        created_at=stamp, updated_at=stamp,
        handle=handle,
        name=str(item.get('name') or '机器人')[:40],
        title=str(item.get('title') or '')[:60],
        description=str(item.get('description') or '')[:400],
        soul=str(item.get('soul') or ''),
        model=str(item.get('model') or ''),
        skill_ids=list(item.get('skill_ids') or []),
        connector_ids=list(item.get('connector_ids') or []),
        section_id='',
        icon=str(item.get('icon') or '🤖'),
        color=str(item.get('color') or '#eef4ff'),
        shape=str(item.get('shape') or 'circle'),
        image=str(item.get('image') or ''),
        pinned=False, hidden=False, status='active',
        ui={'plaza_item_id': item_id, 'plaza_version': int(item.get('version') or 1)},
    )
    session = await store.ensure_bot_session(
        bot_id=bot_id, tenant_id=tenant, owner_user_id=user,
        session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
    try:
        ensure_bot_home(tenant, user, bot_id)
    except Exception:                               # noqa: BLE001
        logger.warning('准备机器人目录失败 bot=%s', bot_id, exc_info=True)

    if install:
        await store.bind_plaza_install_bot(str(install['install_id']), bot_id=bot_id,
                                           item_version=int(item.get('version') or 1))
    else:
        claimed = await store.claim_plaza_install(
            install_id='ins_' + uuid.uuid4().hex[:12], item_id=item_id,
            tenant_id=tenant, owner_user_id=user,
            item_version=int(item.get('version') or 1))
        fresh = await store.plaza_install_for(item_id=item_id, tenant_id=tenant,
                                              owner_user_id=user)
        if fresh and not str(fresh.get('bot_id') or ''):
            await store.bind_plaza_install_bot(str(fresh['install_id']),
                                               bot_id=bot_id,
                                               item_version=int(item.get('version') or 1))
        if claimed:
            # 只有"首次登记成功"那一次计数：并发点两下也只加一次。
            try:
                await store.bump_plaza_counter(item_id, 'install_count', 1)
            except Exception:                       # noqa: BLE001
                logger.debug('安装计数失败 item=%s', item_id, exc_info=True)

    return {
        'installed': True, 'reused': False,
        'bot_id': row.get('bot_id') or bot_id,
        'session_id': (session or {}).get('session_id') or '',
        'chat_url': f"/api/ui/bots?bot={row.get('bot_id') or bot_id}",
        'name': row.get('name') or '',
        'item_id': item_id,
    }


@router.delete('/bot-plaza/{item_id}/install')
async def uninstall_plaza_bot(item_id: str, request: Request,
                              principal=Depends(policies.current_principal)):
    """卸载：归档副本机器人 + 删安装登记。

    归档而不是硬删——对话历史是审计资产；用户以后想再用，重新点「使用」会拿到
    一个新的同名副本（安装登记已经删掉了，所以会重新计数）。
    """
    _enabled()
    store = _store(request)
    tenant, user = _ids(principal)
    install = await store.plaza_install_for(item_id=item_id, tenant_id=tenant,
                                            owner_user_id=user)
    if install is None:
        raise HTTPException(status_code=404, detail='没有装过这条 Bot')
    bot_id = str(install.get('bot_id') or '')
    if bot_id:
        await store.update_bot_row(bot_id, tenant_id=tenant, owner_user_id=user,
                                   status='archived')
    removed = await store.drop_plaza_install(item_id=item_id, tenant_id=tenant,
                                             owner_user_id=user)
    return {'removed': removed, 'bot_id': bot_id, 'archived': bool(bot_id)}


# ---------------------------------------------------------------- 投稿 / 撤回


@router.post('/bots/{bot_id}/publish', status_code=201)
async def publish_bot(bot_id: str, body: PublishBody, request: Request,
                      principal=Depends(policies.current_principal)):
    """把我的机器人投稿到广场。

    权限只看归属：不是自己的机器人一律 404（不区分"不存在"和"不是你的"）。
    投稿**不改动机器人本身**——条目是快照，之后改机器人不会自动同步，
    想更新广场版本就再投一次（version+1，重新评审）。
    """
    _enabled()
    store = _store(request)
    tenant, user = _ids(principal)
    bot = await store.bot_row(bot_id, tenant_id=tenant, owner_user_id=user)
    if bot is None:
        raise HTTPException(status_code=404, detail='机器人不存在')
    if str(bot.get('status') or '') == 'archived':
        raise HTTPException(status_code=400, detail='已归档的机器人不能发布')
    category = body.category if body.category in CATEGORY_IDS else 'general'
    existing = await store.plaza_item_by_source(source_bot_id=bot_id,
                                                publisher_tenant=tenant,
                                                publisher_user_id=user)
    fields = dict(
        source_bot_id=bot_id, publisher_user_id=user, publisher_tenant=tenant,
        name=str(bot.get('name') or '')[:40],
        handle=str(bot.get('handle') or '')[:32],
        title=str(bot.get('title') or '')[:60],
        description=str(bot.get('description') or '')[:400],
        soul=str(bot.get('soul') or ''),
        model=str(bot.get('model') or ''),
        skill_ids=list(bot.get('skill_ids') or []),
        connector_ids=list(bot.get('connector_ids') or []),
        icon=str(bot.get('icon') or '🤖'),
        color=str(bot.get('color') or '#eef4ff'),
        shape=str(bot.get('shape') or 'circle'),
        image=str(bot.get('image') or ''),
        category=category,
        tags=plaza_domain.clean_tags(body.tags),
        readme=body.readme.strip(),
        starters=plaza_domain.clean_starters(body.starters),
        # 投稿一律先进"本企业可见 + 待审"：跨企业上架是管理员的动作，不是投稿人的。
        scope='tenant', status='pending', review_note='', reviewed_by='',
        reviewed_at='',
    )
    if existing:
        fields['version'] = int(existing.get('version') or 1) + 1
        item = await store.update_plaza_item(str(existing['item_id']), **fields)
    else:
        slug = await _unique_slug(store, str(bot.get('handle') or bot.get('name') or 'bot'))
        stamp = _stamp()
        fields.update(slug=slug, version=1)
        item = await store.create_plaza_item(
            item_id='plz_' + uuid.uuid4().hex[:12], created_at=stamp,
            updated_at=stamp, **fields)
    return {
        'item_id': (item or {}).get('item_id'),
        'status': (item or {}).get('status') or 'pending',
        'version': int((item or {}).get('version') or 1),
        'scope': (item or {}).get('scope') or 'tenant',
        'note': '已提交，等待管理员审核后出现在广场',
        'message': body.note.strip(),
    }


@router.delete('/bots/{bot_id}/publish')
async def withdraw_publication(bot_id: str, request: Request,
                               principal=Depends(policies.current_principal)):
    """撤回投稿：条目下架（`offline`），不删除历史与安装关系。"""
    _enabled()
    store = _store(request)
    tenant, user = _ids(principal)
    item = await store.plaza_item_by_source(source_bot_id=bot_id,
                                            publisher_tenant=tenant,
                                            publisher_user_id=user)
    if item is None:
        raise HTTPException(status_code=404, detail='这个机器人没有投稿记录')
    updated = await store.update_plaza_item(
        str(item['item_id']), status='offline',
        review_note='投稿人撤回', reviewed_by=user, reviewed_at=_stamp())
    return {'item_id': (updated or {}).get('item_id'),
            'status': (updated or {}).get('status') or 'offline'}


@router.delete('/bot-plaza/mine/{item_id}')
async def withdraw_item(item_id: str, request: Request,
                        principal=Depends(policies.current_principal)):
    """按条目撤回（工作台只知道条目 id 时用它）。"""
    _enabled()
    store = _store(request)
    tenant, user = _ids(principal)
    item = await store.plaza_item(item_id)
    if item is None or str(item.get('publisher_user_id') or '') != user or \
            str(item.get('publisher_tenant') or '') != tenant:
        raise HTTPException(status_code=404, detail='没有这条投稿')
    updated = await store.update_plaza_item(
        item_id, status='offline', review_note='投稿人撤回',
        reviewed_by=user, reviewed_at=_stamp())
    return {'item_id': item_id, 'status': (updated or {}).get('status') or 'offline'}
