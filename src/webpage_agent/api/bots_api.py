"""机器人（Bot Mode）接口：花名册、规范会话、投递收据、群聊房间、例行任务。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/bots` | 花名册（含分区、房间、投递未读数） |
| POST | `/api/bots` | 新建机器人（身份 + 人设 + 默认模型/技能/连接器） |
| PATCH | `/api/bots/{id}` | 改标题/描述/人设/分区/置顶/隐藏/归档 |
| DELETE | `/api/bots/{id}` | 删除身份行（档案默认保留；`?archive=1` 只归档） |
| GET | `/api/bots/{id}/chat` | 取或建规范会话（**幂等**）+ 最近消息 + 能力纪元 |
| POST | `/api/bots/{id}/message` | 以用户身份给机器人发一条消息（202 + turn_id） |
| GET | `/api/bots/{id}/inbox` | 收件箱：给它发过消息的收据（含状态与失败原因） |
| GET | `/api/bots/{id}/routines` | 归属它的例行任务 |
| POST/GET | `/api/bot-sections` | 分区 |
| DELETE | `/api/bot-sections/{id}` | 删分区（成员归属一并清空） |
| GET/POST | `/api/bot-rooms` | 群聊房间 |
| GET | `/api/bot-rooms/{id}` | 房间详情：事件日志 + 成员 + 每步任务 |
| POST | `/api/bot-rooms/{id}/messages` | 用户发言（追加事件；不自动推进，推进在 Worker） |
| POST | `/api/bot-rooms/{id}/step` | 手动推进一步（演示/排障；Worker 会自己推进） |
| POST | `/api/bot-rooms/{id}/close` | 关闭房间 |

边界：

- **归属只从 `principal` 取**，客户端传的 tenant/user 一律不信；
- 机器人会话对普通侧栏隐藏（`sessions.hidden=1`），只能从这里进；
- 删除机器人**不级联删消息**：档案是审计资产，删了就没法复盘"它当时说了什么"；
- 群聊推进默认交给 Worker，`/step` 只是把同一段逻辑手动触发一次（幂等）。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.bot import planner, prompts
from webpage_agent.bot import rooms as rooms_module
from webpage_agent.bot.service import ensure_bot_home
from webpage_agent.core.config import settings
from webpage_agent.governance import policies

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api', tags=['bots'])

MAX_BOTS = 50
MAX_SOUL = 12_000
MAX_ROOMS = 30


class BotBody(BaseModel):
    """新建/整体更新机器人。`handle` 留空时由名字推导。"""

    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    handle: str = Field(default='', max_length=32)
    title: str = Field(default='', max_length=60)
    description: str = Field(default='', max_length=400)
    soul: str = Field(default='', max_length=MAX_SOUL)
    model: str = Field(default='', max_length=120)
    skill_ids: list[str] = Field(default_factory=list, max_length=50)
    connector_ids: list[str] = Field(default_factory=list, max_length=50)
    section_id: str = Field(default='', max_length=64)
    icon: str = Field(default='🤖', max_length=16)
    color: str = Field(default='#eef4ff', max_length=32)
    shape: str = Field(default='circle', max_length=16)
    image: str = Field(default='', max_length=200_000)
    pinned: bool = False
    hidden: bool = False


class BotPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=40)
    handle: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=400)
    soul: str | None = Field(default=None, max_length=MAX_SOUL)
    model: str | None = Field(default=None, max_length=120)
    skill_ids: list[str] | None = Field(default=None, max_length=50)
    connector_ids: list[str] | None = Field(default=None, max_length=50)
    section_id: str | None = Field(default=None, max_length=64)
    icon: str | None = Field(default=None, max_length=16)
    color: str | None = Field(default=None, max_length=32)
    shape: str | None = Field(default=None, max_length=16)
    image: str | None = Field(default=None, max_length=200_000)
    pinned: bool | None = None
    hidden: bool | None = None
    status: str | None = Field(default=None, max_length=16)


class SectionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    position: int = 0
    section_id: str = Field(default='', max_length=64)


class BotMessageBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=10_000)
    request_id: str = Field(default='', max_length=64)


class RoomBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    topic: str = Field(default='', max_length=400)
    members: list[str] = Field(min_length=2, max_length=6)
    max_rounds: int = Field(default=0, ge=0, le=10)
    max_messages: int = Field(default=0, ge=0, le=50)


class RoomMessageBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=10_000)


def _store(request: Request):
    store = getattr(request.app.state, 'store', None)
    if store is None:
        raise HTTPException(status_code=503, detail='服务未就绪：没有数据库连接')
    return store


def _hub():
    from webpage_agent.api import routes
    return routes.hub


def _ids(principal) -> tuple[str, str]:
    return (str(getattr(principal, 'tenant_id', 'default') or 'default'),
            str(getattr(principal, 'user_id', 'default') or 'default'))


def _enabled() -> None:
    if not settings.bot_mode_enabled:
        raise HTTPException(status_code=403, detail='机器人能力未开放（BOT_MODE_ENABLED=false）')


async def _owned_bot(store, bot_id: str, principal) -> dict:
    tenant, user = _ids(principal)
    bot = await store.bot_row(bot_id, tenant_id=tenant, owner_user_id=user)
    if bot is None:
        raise HTTPException(status_code=404, detail='机器人不存在')
    return bot


async def _owned_room(store, room_id: str, principal) -> dict:
    tenant, user = _ids(principal)
    room = await store.room_row(room_id, tenant_id=tenant, owner_user_id=user)
    if room is None:
        raise HTTPException(status_code=404, detail='群聊不存在')
    return room


def _view(bot: dict, *, routine_ids: list[str] | None = None) -> dict:
    """对外的机器人视图：**不含** soul 以外的大字段也没有凭据。"""
    return {
        'bot_id': bot.get('bot_id'),
        'handle': bot.get('handle') or '',
        'name': bot.get('name') or '',
        'title': bot.get('title') or '',
        'description': bot.get('description') or '',
        'soul': bot.get('soul') or '',
        'model': bot.get('model') or '',
        'skill_ids': list(bot.get('skill_ids') or []),
        'connector_ids': list(bot.get('connector_ids') or []),
        'section_id': bot.get('section_id') or '',
        'icon': bot.get('icon') or '🤖',
        'color': bot.get('color') or '#eef4ff',
        'shape': bot.get('shape') or 'circle',
        'image': bot.get('image') or '',
        'pinned': bool(bot.get('pinned')),
        'hidden': bool(bot.get('hidden')),
        'status': bot.get('status') or 'active',
        'chat_session_id': bot.get('chat_session_id') or '',
        'last_preview': bot.get('last_preview') or '',
        'message_count': int(bot.get('message_count') or 0),
        'chat_updated_at': bot.get('chat_updated_at') or '',
        'routine_ids': list(routine_ids or []),
        'created_at': bot.get('created_at'),
        'updated_at': bot.get('updated_at'),
    }


async def _routines_of(store, bot_ids: list[str], tenant: str, user: str) -> dict[str, list[str]]:
    """每个机器人名下的例行任务 id（一次查询，不做 N+1）。"""
    if not bot_ids:
        return {}
    try:
        rows = await store.list_schedules(tenant_id=tenant, owner_user_id=user,
                                          limit=200)
    except Exception:                              # noqa: BLE001
        logger.debug('读取例行任务失败', exc_info=True)
        return {}
    out: dict[str, list[str]] = {}
    for row in rows or []:
        bot_id = str(row.get('bot_id') or '')
        if bot_id:
            out.setdefault(bot_id, []).append(str(row.get('schedule_id') or ''))
    return out


# ---------------------------------------------------------------- 花名册


@router.get('/bots')
async def list_bots(request: Request, principal=Depends(policies.current_principal)):
    """花名册：机器人行 + 分区 + 群聊房间 + 开关状态。"""
    store = _store(request)
    tenant, user = _ids(principal)
    bots = await store.list_bot_rows(tenant_id=tenant, owner_user_id=user)
    routines = await _routines_of(store, [str(item.get('bot_id') or '') for item in bots],
                                  tenant, user)
    sections = await store.list_bot_sections(tenant_id=tenant, owner_user_id=user)
    rooms = await store.list_room_rows(tenant_id=tenant, owner_user_id=user)
    members_by_room = {str(room.get('room_id') or ''): room.get('members') or []
                       for room in rooms}
    handles = {str(item.get('bot_id') or ''): item.get('handle') or ''
               for item in bots}
    return {
        'enabled': settings.bot_mode_enabled,
        'items': [_view(item, routine_ids=routines.get(str(item.get('bot_id') or '')))
                  for item in bots],
        'sections': [dict(row) for row in sections],
        'rooms': [{
            'room_id': room.get('room_id'),
            'name': room.get('name') or '',
            'topic': room.get('topic') or '',
            'members': [{'bot_id': bot_id, 'handle': handles.get(bot_id, ''),
                         'name': next((item.get('name') for item in bots
                                       if str(item.get('bot_id') or '') == bot_id), '')}
                        for bot_id in members_by_room.get(str(room.get('room_id') or ''), [])],
            'max_rounds': int(room.get('max_rounds') or 3),
            'max_messages': int(room.get('max_messages') or 10),
            'status': room.get('status') or 'open',
            'round_no': int(room.get('round_no') or 0),
            'created_at': room.get('created_at'),
        } for room in rooms],
        'limits': {'bots': MAX_BOTS, 'rooms': MAX_ROOMS,
                   'max_hops': settings.bot_max_hops},
    }


@router.post('/bots', status_code=201)
async def create_bot(body: BotBody, request: Request,
                     principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, user = _ids(principal)
    existing = await store.list_bot_rows(tenant_id=tenant, owner_user_id=user,
                                         include_archived=True)
    if len([item for item in existing
            if str(item.get('status') or '') == 'active']) >= MAX_BOTS:
        raise HTTPException(status_code=400, detail=f'最多 {MAX_BOTS} 个机器人')

    handle = prompts.normalize_handle(body.handle or body.name, name=body.name)
    if not handle:
        raise HTTPException(status_code=400, detail='无法从名字推导出合法的 handle，请显式填写')
    taken = {str(item.get('handle') or '').lower() for item in existing}
    base, suffix = handle, 2
    while handle.lower() in taken:
        handle = f'{base[:20]}-{suffix}'
        suffix += 1

    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat()
    bot_id = 'bot_' + uuid.uuid4().hex[:12]
    row = await store.create_bot_row(
        bot_id=bot_id, tenant_id=tenant, owner_user_id=user,
        created_at=stamp, updated_at=stamp,
        handle=handle, name=body.name.strip(), title=body.title.strip(),
        description=body.description.strip(), soul=body.soul.strip(),
        model=body.model.strip(), skill_ids=body.skill_ids,
        connector_ids=body.connector_ids, section_id=body.section_id,
        icon=body.icon or '🤖', color=body.color or '#eef4ff',
        shape=body.shape or 'circle', image=body.image, pinned=body.pinned,
        hidden=body.hidden, status='active', ui={})
    # 规范会话在这里就建好：花名册第一行就该有"最近会话"，不用等用户点开。
    session = await store.ensure_bot_session(
        bot_id=bot_id, tenant_id=tenant, owner_user_id=user,
        session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
    try:
        ensure_bot_home(tenant, user, bot_id)
    except Exception:                              # noqa: BLE001
        logger.warning('准备机器人目录失败 bot=%s', bot_id, exc_info=True)
    view = _view({**row, 'chat_session_id': session.get('session_id') or ''})
    return view


@router.get('/bots/{bot_id}')
async def get_bot(bot_id: str, request: Request,
                  principal=Depends(policies.current_principal)):
    store = _store(request)
    bot = await _owned_bot(store, bot_id, principal)
    tenant, user = _ids(principal)
    session = await store.bot_session(bot_id)
    view = _view({**bot, 'chat_session_id': (session or {}).get('session_id') or ''})
    view['inbox'] = [dict(row) for row in
                     await store.list_deliveries(bot_id=bot_id, tenant_id=tenant,
                                                 owner_user_id=user, limit=20)]
    view['epoch'] = str((session or {}).get('capability_epoch') or '')
    return view


@router.patch('/bots/{bot_id}')
async def patch_bot(bot_id: str, body: BotPatch, request: Request,
                    principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    await _owned_bot(store, bot_id, principal)
    tenant, user = _ids(principal)
    fields = body.model_dump(exclude_unset=True)
    if 'handle' in fields and fields['handle'] is not None:
        handle = prompts.normalize_handle(fields['handle'])
        if not handle:
            raise HTTPException(status_code=400, detail='handle 不合法')
        others = await store.list_bot_rows(tenant_id=tenant, owner_user_id=user,
                                           include_archived=True)
        if any(str(item.get('handle') or '').lower() == handle for item in others
               if str(item.get('bot_id') or '') != bot_id):
            raise HTTPException(status_code=409, detail=f'handle @{handle} 已被占用')
        fields['handle'] = handle
    if fields.get('status') not in (None, '', 'active', 'archived'):
        raise HTTPException(status_code=400, detail='status 只能是 active 或 archived')
    updated = await store.update_bot_row(bot_id, tenant_id=tenant,
                                         owner_user_id=user, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail='机器人不存在')
    session = await store.bot_session(bot_id)
    return _view({**updated, 'chat_session_id': (session or {}).get('session_id') or ''})


@router.delete('/bots/{bot_id}')
async def delete_bot(bot_id: str, request: Request, archive: int = Query(0),
                     principal=Depends(policies.current_principal)):
    """`?archive=1` 只归档（推荐）：身份行留着，档案与收据引用都还能解析。"""
    _enabled()
    store = _store(request)
    await _owned_bot(store, bot_id, principal)
    tenant, user = _ids(principal)
    if archive:
        updated = await store.update_bot_row(bot_id, tenant_id=tenant,
                                             owner_user_id=user,
                                             status='archived')
        return {'archived': bool(updated), 'bot_id': bot_id}
    deleted = await store.delete_bot_row(bot_id, tenant_id=tenant,
                                         owner_user_id=user)
    return {'deleted': deleted, 'bot_id': bot_id}


# ---------------------------------------------------------------- 会话与消息


@router.get('/bots/{bot_id}/chat')
async def bot_chat(bot_id: str, request: Request,
                   principal=Depends(policies.current_principal)):
    """取或建规范会话（幂等），返回最近消息与能力纪元。

    前端打开机器人时只调这一个接口：会话不存在就建，存在就复用——
    **没有任何 saved session-id**，这就是"标题即身份"在本项目的等价物。
    """
    store = _store(request)
    bot = await _owned_bot(store, bot_id, principal)
    tenant, user = _ids(principal)
    session = await store.ensure_bot_session(
        bot_id=bot_id, tenant_id=tenant, owner_user_id=user,
        session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
    session_id = str(session.get('session_id') or '')
    messages = await _hub().history(session_id)
    turns = await store.turns_of(session_id) if hasattr(store, 'turns_of') else []
    rooms = await store.list_room_rows(tenant_id=tenant, owner_user_id=user)
    my_rooms = [{'room_id': room.get('room_id'), 'name': room.get('name'),
                 'status': room.get('status')}
                for room in rooms
                if bot_id in (room.get('members') or [])]
    return {
        'bot': _view({**bot, 'chat_session_id': session_id}),
        'session_id': session_id,
        'epoch': str(session.get('capability_epoch') or ''),
        'messages': messages,
        'turns': [{'turn_id': turn.get('turn_id'), 'status': turn.get('status'),
                   'message': turn.get('message'), 'answer': turn.get('answer'),
                   'created_at': turn.get('created_at')}
                  for turn in (turns or [])[-20:]],
        'rooms': my_rooms,
        'inbox': [dict(row) for row in
                  await store.list_deliveries(bot_id=bot_id, tenant_id=tenant,
                                              owner_user_id=user, limit=20)],
    }


@router.post('/bots/{bot_id}/message', status_code=202)
async def post_bot_message(bot_id: str, body: BotMessageBody, request: Request,
                           principal=Depends(policies.current_principal)):
    """以用户身份给机器人发一条消息：走与普通会话完全相同的接纳链路。"""
    _enabled()
    store = _store(request)
    await _owned_bot(store, bot_id, principal)
    tenant, user = _ids(principal)
    session = await store.ensure_bot_session(
        bot_id=bot_id, tenant_id=tenant, owner_user_id=user,
        session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
    session_id = str(session.get('session_id') or '')
    from webpage_agent.api.hub_messages import CapabilityDisabled, NoWorkerAvailable
    from webpage_agent.store.pg_store import TurnRequestConflict
    try:
        result = await _hub().accept_session_message(
            session_id, request_id=body.request_id or uuid.uuid4().hex[:16],
            message=body.message, principal=principal,
            platform_subject=str(getattr(principal, 'subject', '') or ''))
    except TurnRequestConflict:
        raise HTTPException(status_code=409, detail='该 request_id 已用于不同内容') from None
    except NoWorkerAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc),
                            headers={'Retry-After': '30'}) from None
    except PermissionError:
        raise HTTPException(status_code=404, detail='会话不存在') from None
    except CapabilityDisabled as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    result['session_id'] = session_id
    return result


@router.get('/bots/{bot_id}/inbox')
async def bot_inbox(bot_id: str, request: Request, status: str = Query(''),
                    limit: int = Query(50, ge=1, le=200),
                    principal=Depends(policies.current_principal)):
    store = _store(request)
    await _owned_bot(store, bot_id, principal)
    tenant, user = _ids(principal)
    rows = await store.list_deliveries(bot_id=bot_id, tenant_id=tenant,
                                       owner_user_id=user, status=status,
                                       limit=limit)
    return {'items': [dict(row) for row in rows]}


@router.get('/bots/{bot_id}/routines')
async def bot_routines(bot_id: str, request: Request,
                       principal=Depends(policies.current_principal)):
    store = _store(request)
    await _owned_bot(store, bot_id, principal)
    tenant, user = _ids(principal)
    try:
        rows = await store.list_schedules(tenant_id=tenant, owner_user_id=user,
                                          limit=200)
    except Exception:                              # noqa: BLE001
        rows = []
    return {'items': [{'schedule_id': row.get('schedule_id'),
                       'name': row.get('name'), 'prompt': row.get('prompt'),
                       'enabled': bool(row.get('enabled')),
                       'next_run_at': row.get('next_run_at'),
                       'last_status': row.get('last_status') or '',
                       'rrule': row.get('rrule') or '',
                       'type': row.get('schedule_type') or 'recurring'}
                      for row in rows or []
                      if str(row.get('bot_id') or '') == bot_id]}


# ---------------------------------------------------------------- 分区


@router.get('/bot-sections')
async def list_sections(request: Request,
                        principal=Depends(policies.current_principal)):
    store = _store(request)
    tenant, user = _ids(principal)
    return {'items': await store.list_bot_sections(tenant_id=tenant,
                                                   owner_user_id=user)}


@router.post('/bot-sections', status_code=201)
async def upsert_section(body: SectionBody, request: Request,
                         principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, user = _ids(principal)
    section_id = body.section_id.strip() or ('sec_' + uuid.uuid4().hex[:10])
    row = await store.upsert_bot_section(
        section_id=section_id, tenant_id=tenant, owner_user_id=user,
        name=body.name.strip(), position=int(body.position))
    if not row:
        raise HTTPException(status_code=409, detail='同名分区已存在，或分区不属于当前主体')
    return row


@router.delete('/bot-sections/{section_id}')
async def delete_section(section_id: str, request: Request,
                         principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, user = _ids(principal)
    deleted = await store.delete_bot_section(section_id, tenant_id=tenant,
                                             owner_user_id=user)
    if not deleted:
        raise HTTPException(status_code=404, detail='分区不存在')
    return {'deleted': True, 'section_id': section_id}


# ---------------------------------------------------------------- 群聊


@router.get('/bot-rooms')
async def list_rooms(request: Request,
                     principal=Depends(policies.current_principal)):
    store = _store(request)
    tenant, user = _ids(principal)
    rooms = await store.list_room_rows(tenant_id=tenant, owner_user_id=user)
    return {'items': rooms}


@router.post('/bot-rooms', status_code=201)
async def create_room(body: RoomBody, request: Request,
                      principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, user = _ids(principal)
    existing = await store.list_room_rows(tenant_id=tenant, owner_user_id=user,
                                          limit=200)
    if len(existing) >= MAX_ROOMS:
        raise HTTPException(status_code=400, detail=f'最多 {MAX_ROOMS} 个群聊')
    bots = await store.list_bot_rows(tenant_id=tenant, owner_user_id=user)
    by_id = {str(item.get('bot_id') or ''): item for item in bots
             if str(item.get('status') or 'active') == 'active'}
    members: list[str] = []
    for raw in body.members:
        bot_id = str(raw or '').strip()
        if bot_id not in by_id:
            raise HTTPException(status_code=400, detail=f'成员 {bot_id} 不存在或已归档')
        if bot_id not in members:
            members.append(bot_id)
    if len(members) < 2:
        raise HTTPException(status_code=400, detail='群聊至少要有 2 个不同的机器人')
    room = await store.create_room_row(
        room_id='room_' + uuid.uuid4().hex[:12], tenant_id=tenant,
        owner_user_id=user, name=body.name.strip(), members=members,
        topic=body.topic.strip(),
        max_rounds=int(body.max_rounds or settings.bot_room_max_rounds),
        max_messages=int(body.max_messages or settings.bot_room_max_messages))
    await store.append_room_event(
        str(room.get('room_id') or ''), event_id=f'{room.get("room_id")}:opened',
        kind='room.opened', actor=f'user:{user}',
        payload={'name': room.get('name'), 'topic': room.get('topic'),
                 'members': members})
    # 议题作为用户的第一条发言落进日志：规划器据此决定谁开场。
    if body.topic.strip():
        await store.append_room_event(
            str(room.get('room_id') or ''),
            event_id=f'{room.get("room_id")}:topic',
            kind='message.user', actor=f'user:{user}',
            payload={'text': body.topic.strip(), 'epoch': 1,
                     'mentions': prompts.parse_mentions(body.topic)})
    return room


@router.get('/bot-rooms/{room_id}')
async def get_room(room_id: str, request: Request,
                   principal=Depends(policies.current_principal)):
    store = _store(request)
    room = await _owned_room(store, room_id, principal)
    events = await store.room_events(room_id)
    tasks = await store.room_tasks(room_id)
    tenant, user = _ids(principal)
    bots = await store.list_bot_rows(tenant_id=tenant, owner_user_id=user)
    by_id = {str(item.get('bot_id') or ''): item for item in bots}
    return {
        'room': room,
        'members': [{'bot_id': bot_id,
                     'handle': (by_id.get(bot_id) or {}).get('handle') or '',
                     'name': (by_id.get(bot_id) or {}).get('name') or bot_id,
                     'icon': (by_id.get(bot_id) or {}).get('icon') or '🤖'}
                    for bot_id in (room.get('members') or [])],
        'events': events,
        'tasks': tasks,
        'plan': planner.plan_next_task(
            room, events,
            [by_id[bot_id] for bot_id in (room.get('members') or [])
             if bot_id in by_id]),
    }


@router.post('/bot-rooms/{room_id}/messages', status_code=201)
async def post_room_message(room_id: str, body: RoomMessageBody, request: Request,
                            principal=Depends(policies.current_principal)):
    """用户发言：只追加事件。**不在这里派发**——推进由 Worker 的统一节拍做，
    否则"用户按了发送"和"调度器推了一步"会变成两套并发写入口。"""
    _enabled()
    store = _store(request)
    room = await _owned_room(store, room_id, principal)
    if str(room.get('status') or 'open') != 'open':
        raise HTTPException(status_code=409, detail='房间已结束，请新建一个群聊')
    tenant, user = _ids(principal)
    events = await store.room_events(room_id)
    epoch = planner.epoch_of(events) + 1
    event = await store.append_room_event(
        room_id, event_id=f'{room_id}:user:{uuid.uuid4().hex[:10]}',
        kind='message.user', actor=f'user:{user}',
        payload={'text': body.message.strip(), 'epoch': epoch,
                 'mentions': prompts.parse_mentions(body.message)})
    return {'event': event, 'epoch': epoch}


@router.post('/bot-rooms/{room_id}/step')
async def step_room(room_id: str, request: Request,
                    principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    await _owned_room(store, room_id, principal)
    driver = rooms_module.RoomDriver(store, worker_id='api:step')
    return await driver.step(room_id)


@router.post('/bot-rooms/{room_id}/close')
async def close_room(room_id: str, request: Request,
                     principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    await _owned_room(store, room_id, principal)
    await store.append_room_event(room_id, event_id=f'{room_id}:closed',
                                 kind='room.closed', actor='user', payload={})
    await store.update_room_row(room_id, status='closed')
    return {'closed': True, 'room_id': room_id}
