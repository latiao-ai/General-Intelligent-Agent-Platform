"""消息平台控制面：渠道清单、配置、状态、配对、扫码登录、平台回调。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/messaging/channels` | 三个渠道的清单 + 配置 + 派生状态（按 `instance` 过滤） |
| GET | `/api/messaging/channels/{id}` | 单渠道详情（含事件、配对、运行态） |
| PUT | `/api/messaging/channels/{id}` | 开关 / 绑定机器人 / 授权策略 / 非敏感选项 |
| POST | `/api/messaging/channels/{id}/test` | 用当前配置发一条测试消息（进出站队列） |
| POST | `/api/messaging/channels/{id}/reload` | 让 Worker 重建这个渠道的连接 |
| GET | `/api/messaging/instances` | 机器人实例列表（默认 + 每个机器人一套） |
| POST | `/api/messaging/instances` | 给一个机器人开出独立的接入实例 |
| DELETE | `/api/messaging/instances/{id}` | 删掉一个实例的配置与运行态 |
| GET | `/api/messaging/pairing` | 待批 + 已批准的配对 |
| GET | `/api/messaging/sessions` | 最近的 IM 会话 + 每段会话由谁回话 |
| PUT | `/api/messaging/sessions` | 给一段会话单独指定机器人 / 恢复跟随渠道 |
| POST | `/api/messaging/pairing/approve` | 按 8 位码批准 |
| POST | `/api/messaging/pairing/revoke` | 撤销某用户的授权 |
| POST | `/api/messaging/channels/weixin/login/start` | 取二维码（iLink 扫码登录） |
| GET | `/api/messaging/channels/weixin/login/status` | 轮询扫码状态 |
| POST | `/api/messaging/channels/weixin/login/logout` | 退出登录（清 token） |
| GET | `/api/messaging/events` | 审计事件（无正文，只有状态与哈希） |
| POST | `/api/messaging/webhook/feishu` | 飞书事件回调（验签） |
| GET/POST | `/api/messaging/webhook/wecom` | 企业微信 URL 验证 / 事件回调（验签） |

**密钥永不返回**：`env` 里只给 `is_set`，不给值；个人微信只给 account_id，
不给 token。运行态由 Worker 写、这里只读。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.core.config import settings
from webpage_agent.governance import policies
from webpage_agent.messaging import router as router_module
from webpage_agent.messaging import state as state_module
from webpage_agent.messaging.base import ChannelConfig, TransportError
from webpage_agent.messaging.gateway import build_channel_config
from webpage_agent.messaging.registry import (CHANNELS, channels_for, diagnose,
                                              get_manifest, instance_env_suffix)
from webpage_agent.messaging.security import mask_secret
from webpage_agent.messaging.transports.feishu import FeishuTransport
from webpage_agent.messaging.transports.wecom import WeComTransport
from webpage_agent.messaging.weixin_login import LoginManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/messaging', tags=['messaging'])

MAX_WEBHOOK_BYTES = 1_000_000
WEBHOOK_RATE_MAX = 120          # 每分钟每 IP 的回调上限
WEBHOOK_RATE_WINDOW = 60.0
_webhook_hits: dict[str, tuple[float, int]] = {}
_login_manager = LoginManager()


class ChannelPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool | None = None
    bot_id: str | None = Field(default=None, max_length=64)
    dm_policy: str | None = Field(default=None, max_length=16)
    group_policy: str | None = Field(default=None, max_length=16)
    allowed_users: list[str] | None = Field(default=None, max_length=200)
    options: dict[str, str] | None = None


class TestBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chat_id: str = Field(default='', max_length=200)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(min_length=4, max_length=16)


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    platform: str = Field(min_length=1, max_length=32)
    user_id: str = Field(min_length=1, max_length=200)


def _store(request: Request):
    store = getattr(request.app.state, 'store', None)
    if store is None:
        raise HTTPException(status_code=503, detail='服务未就绪：没有数据库连接')
    return store


def _ids(principal) -> tuple[str, str]:
    return (str(getattr(principal, 'tenant_id', 'default') or 'default'),
            str(getattr(principal, 'user_id', 'default') or 'default'))


def _enabled() -> None:
    if not settings.messaging_enabled:
        raise HTTPException(
            status_code=403,
            detail='消息平台能力未开放（MESSAGING_ENABLED=false）')


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


async def _gateway_alive(store) -> bool:
    try:
        return bool(await store.live_count(
            'messaging', settings.messaging_alive_window_seconds))
    except Exception:                              # noqa: BLE001
        return False


_UNSET = object()


def _row_defaults(manifest, tenant: str, owner: str, instance_id: str) -> dict:
    """还没落库的那一行。非默认实例的机器人身份是固定的，不读行上的字段。"""
    return {
        'channel_id': manifest.id, 'tenant_id': tenant,
        'owner_user_id': owner, 'instance_id': instance_id,
        'enabled': False, 'config': {},
        'bot_id': instance_id or '',
        'dm_policy': manifest.default_dm_policy,
        'group_policy': manifest.default_group_policy, 'allowed_users': [],
    }


async def _channel_state(store, manifest, row: dict, *, instance_id: str,
                         gateway_alive: bool, login: dict | None = None,
                         runtime=_UNSET) -> dict:
    """这个渠道在**这个实例**下的状态。列表、详情、实例摘要共用这一份推导。

    为什么要按实例推：凭据是带后缀的环境变量，运行态也是按实例存的。
    「飞书已连接」只对某一个实例成立——混着算，两个实例会共用一个状态灯，
    而它们可能一个跑着、一个连 AppID 都没填。
    """
    tenant = str(row.get('tenant_id') or 'default')
    owner = str(row.get('owner_user_id') or 'default')
    login = login or {}
    configured, missing, callback = diagnose(
        manifest, login_state=login,
        public_base_url=settings.messaging_public_base_url,
        instance_id=instance_id)
    if runtime is _UNSET:
        runtime = await store.im_runtime(
            manifest.id, tenant_id=tenant, owner_user_id=owner,
            instance_id=instance_id)
    runtime = dict(runtime) if runtime else None
    if runtime is not None:
        runtime['updated_at_ts'] = _timestamp(runtime.get('updated_at') or '')

    dm_policy = str(row.get('dm_policy') or manifest.default_dm_policy)
    group_policy = str(row.get('group_policy') or manifest.default_group_policy)
    fingerprint = _fingerprint(row, manifest, login, dm_policy, group_policy,
                               instance_id)
    if not settings.messaging_enabled:
        derived = {'state': 'disabled', 'reason': '消息平台能力未开放',
                   'tone': 'muted'}
    else:
        derived = state_module.derive(
            enabled=bool(row.get('enabled')), configured=configured,
            gateway_alive=gateway_alive, runtime=runtime,
            fingerprint=fingerprint,
            now_ts=time.time(),
            stale_seconds=max(90.0, settings.messaging_alive_window_seconds * 2))
    return {'configured': configured, 'missing': missing, 'callback': callback,
            'login': login, 'runtime': runtime, 'dm_policy': dm_policy,
            'group_policy': group_policy, 'derived': derived}


async def _view(store, manifest, row: dict, *, gateway_alive: bool,
                instance_id: str = '', detailed: bool = False) -> dict:
    tenant = str(row.get('tenant_id') or 'default')
    owner = str(row.get('owner_user_id') or 'default')
    login = {}
    if manifest.id == 'weixin':
        login = await store.im_login_get(
            manifest.id, tenant_id=tenant, owner_user_id=owner)
    state = await _channel_state(
        store, manifest, row, instance_id=instance_id,
        gateway_alive=gateway_alive, login=login)
    runtime = state['runtime']
    derived = state['derived']
    configured = state['configured']
    missing = state['missing']
    callback = state['callback']
    dm_policy = state['dm_policy']
    group_policy = state['group_policy']
    env = []
    values = manifest.env_values(instance_id)
    for item in manifest.env_fields:
        value = str(values.get(item.key) or '')
        env.append({
            'key': item.key, 'env': item.env, 'label': item.label,
            # 实例的变量名带后缀，界面要显示**真正要填的那个**，
            # 否则用户照着 FEISHU_APP_ID 填进 .env，第二个实例永远读不到。
            'env_name': manifest.env_name(item, instance_id),
            'required': item.required, 'secret': item.secret,
            'is_set': bool(value),
            'preview': ('' if item.secret else value) if value else '',
            'masked': mask_secret(value) if value and not item.secret else '',
            'help': item.help, 'url': item.url,
        })
    config = dict(row.get('config') or {})
    options = [
        {'key': item.key, 'label': item.label, 'kind': item.kind,
         'choices': [{'value': key, 'label': label}
                     for key, label in item.choices],
         'help': item.help, 'value': str(config.get(item.key) or item.default)}
        for item in manifest.option_fields
    ]
    view = {
        'id': manifest.id,
        'name': manifest.name,
        'vendor': manifest.vendor,
        'tagline': manifest.tagline,
        'description': manifest.description,
        'docs_url': manifest.docs_url,
        'priority': manifest.priority,
        'capabilities': {
            'card': manifest.capabilities.card,
            'stream_edit': manifest.capabilities.stream_edit,
            'typing': manifest.capabilities.typing,
            'thread': manifest.capabilities.thread,
            'reaction': manifest.capabilities.reaction,
            'group': manifest.capabilities.group,
            'max_text_chars': manifest.capabilities.max_text_chars,
        },
        'enabled': bool(row.get('enabled')),
        'configured': configured,
        'missing': missing,
        'callback_url': callback,
        'needs_public_url': manifest.needs_public_url,
        'env': env,
        'options': options,
        'instance_id': instance_id,
        'bot_id': str(row.get('bot_id') or instance_id),
        'dm_policy': dm_policy,
        'group_policy': group_policy,
        'allowed_users': list(row.get('allowed_users') or []),
        'dm_policies': list(router_module.DM_POLICIES),
        'group_policies': list(router_module.GROUP_POLICIES),
        'state': derived['state'],
        'state_label': state_module.label(derived['state']),
        'tone': state_module.tone(derived['state']),
        'reason': derived.get('reason') or '',
        'runtime': {
            'state': str((runtime or {}).get('state') or ''),
            'detail': str((runtime or {}).get('detail') or ''),
            'error_code': str((runtime or {}).get('error_code') or ''),
            'updated_at': str((runtime or {}).get('updated_at') or ''),
            'worker_id': str((runtime or {}).get('worker_id') or ''),
        },
        'setup_steps': list(manifest.setup_steps),
        'gateway_alive': gateway_alive,
    }
    if manifest.id == 'weixin':
        view['login'] = {
            'logged_in': bool(login.get('token')),
            'account_id': str(login.get('account_id') or ''),
            'user_id': str(login.get('user_id') or ''),
            'saved_at': int(login.get('saved_at') or 0),
            'updated_at': str(login.get('updated_at') or ''),
        }
    if detailed:
        pending = await store.im_pairing_pending(
            tenant_id=tenant, owner_user_id=owner)
        approved = await store.im_pairing_approved(
            tenant_id=tenant, owner_user_id=owner, platform=manifest.id)
        view['pairing'] = {
            'pending': [_pairing_view(item, pending=True) for item in pending
                        if item.get('platform') == manifest.id],
            'approved': [_pairing_view(item, pending=False) for item in approved],
        }
        view['backlog'] = {
            'inbox': await store.im_inbox_backlog(
                tenant_id=tenant, owner_user_id=owner, instance_id=instance_id),
            'outbox': await store.im_outbox_pending(
                tenant_id=tenant, owner_user_id=owner, channel_id=manifest.id,
                instance_id=instance_id),
        }
        view['events'] = await _events(store, tenant, owner, manifest.id, 30,
                                       instance_id)
    return view


def _pairing_view(item: dict, *, pending: bool) -> dict:
    return {
        'platform': str(item.get('platform') or ''),
        'instance': str(item.get('instance_id') or ''),
        'user_id': str(item.get('user_id') or ''),
        'chat_id': str(item.get('chat_id') or ''),
        'status': str(item.get('status') or ''),
        'expires_in': max(0, int(float(item.get('expires_at') or 0) - time.time()))
        if pending else 0,
        'updated_at': str(item.get('updated_at') or ''),
    }


async def _events(store, tenant: str, owner: str, channel_id: str,
                  limit: int, instance_id: str | None = None) -> list[dict]:
    rows = await store.im_events_list(
        tenant_id=tenant, owner_user_id=owner, channel_id=channel_id,
        limit=limit, instance_id=instance_id)
    return [{
        'direction': row.get('direction') or '',
        'chat_id_hash': row.get('chat_id_hash') or '',
        'message_id': row.get('message_id') or '',
        'status': row.get('status') or '',
        'error_code': row.get('error_code') or '',
        'created_at': row.get('created_at') or '',
    } for row in rows]


def _fingerprint(row: dict, manifest, login: dict, dm_policy: str,
                 group_policy: str, instance_id: str = '') -> str:
    config = build_channel_config(
        row, manifest, login, instance_id=instance_id or str(row.get('instance_id') or ''))
    return state_module.config_fingerprint(
        enabled=bool(row.get('enabled')), bot_id=config.bot_id,
        dm_policy=dm_policy, group_policy=group_policy,
        allowed_users=config.allowed_users, config=config.options,
        secrets=config.secrets, public_base_url=config.public_base_url)


# ---------------------------------------------------------------- 渠道


def _instance_param(instance: str) -> str:
    """实例 ID 就是机器人的 id，也是环境变量后缀的来源。做一次收敛校验，
    免得 `?instance=../../x` 这种值被拼进环境变量名或写进库里。"""
    text = str(instance or '').strip()
    if len(text) > 64:
        raise HTTPException(status_code=400, detail='实例标识过长')
    return text


async def _ensure_instance(store, instance_id: str, *, tenant: str,
                           owner: str) -> None:
    """非默认实例必须指向一个真实存在的机器人。

    不校验的话，`PUT ?instance=随便写` 会凭空造出一行配置：它会出现在实例
    列表里，名字只能用 id 兜底，而且永远配不成——因为没人会去填那套环境变量。
    """
    if not instance_id:
        return
    bot = await store.bot_row(instance_id, tenant_id=tenant, owner_user_id=owner)
    if bot is None:
        raise HTTPException(status_code=404, detail='实例不存在：机器人已被删除或不属于你')


@router.get('/channels')
async def list_channels(request: Request, instance: str = Query(''),
                        principal=Depends(policies.current_principal)):
    store = _store(request)
    tenant, owner = _ids(principal)
    instance_id = _instance_param(instance)
    rows = {str(row['channel_id']): row
            for row in await store.im_list_channels(
                tenant_id=tenant, owner_user_id=owner)
            if str(row.get('instance_id') or '') == instance_id}
    alive = await _gateway_alive(store)
    out = []
    for manifest in channels_for(instance_id):
        row = rows.get(manifest.id) or _row_defaults(
            manifest, tenant, owner, instance_id)
        out.append(await _view(store, manifest, row, gateway_alive=alive,
                               instance_id=instance_id))
    return {'channels': out, 'gateway_alive': alive,
            'public_base_url': settings.messaging_public_base_url,
            'enabled': settings.messaging_enabled,
            'instance_id': instance_id}


@router.get('/channels/{channel_id}')
async def get_channel(channel_id: str, request: Request, instance: str = Query(''),
                      principal=Depends(policies.current_principal)):
    store = _store(request)
    manifest = get_manifest(channel_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='未知渠道')
    instance_id = _instance_param(instance)
    if manifest not in channels_for(instance_id):
        raise HTTPException(
            status_code=404,
            detail=f'{manifest.name}不支持多实例：一个部署只能挂一份，请到「默认」里配置')
    tenant, owner = _ids(principal)
    row = await store.im_channel(channel_id, tenant_id=tenant,
                                 owner_user_id=owner,
                                 instance_id=instance_id) or _row_defaults(
        manifest, tenant, owner, instance_id)
    alive = await _gateway_alive(store)
    return await _view(store, manifest, row, gateway_alive=alive,
                       instance_id=instance_id, detailed=True)


@router.put('/channels/{channel_id}')
async def update_channel(channel_id: str, body: ChannelPatch, request: Request,
                         instance: str = Query(''),
                         principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    manifest = get_manifest(channel_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='未知渠道')
    instance_id = _instance_param(instance)
    if manifest not in channels_for(instance_id):
        raise HTTPException(
            status_code=400,
            detail=f'{manifest.name}不支持多实例：一个部署只能挂一份')
    tenant, owner = _ids(principal)
    await _ensure_instance(store, instance_id, tenant=tenant, owner=owner)
    existing = await store.im_channel(channel_id, tenant_id=tenant,
                                      owner_user_id=owner,
                                      instance_id=instance_id)
    if existing is None:
        other = await store.im_channel_owner(
            channel_id, tenant_id=tenant, instance_id=instance_id)
        if other is not None and str(other.get('owner_user_id')) != owner:
            raise HTTPException(
                status_code=409,
                detail='该渠道的凭据是部署级的，已被其他账号配置；请让管理员调整')
    fields: dict = {}
    if body.enabled is not None:
        fields['enabled'] = bool(body.enabled)
    if instance_id:
        # 实例 = 机器人：这个通道叫什么，回话的就是谁。允许改就等于让 tab 标题
        # 和实际身份脱钩，那还不如不叫这个名。
        wanted = (body.bot_id or '').strip()
        if wanted and wanted != instance_id:
            raise HTTPException(
                status_code=400,
                detail='这个实例固定以创建时选的机器人回话；要换身份请新建一个实例')
        fields['bot_id'] = instance_id
    elif body.bot_id is not None:
        bot_id = body.bot_id.strip()
        if bot_id:
            bot = await store.bot_row(bot_id, tenant_id=tenant,
                                      owner_user_id=owner)
            if bot is None:
                raise HTTPException(status_code=404, detail='绑定的机器人不存在')
        fields['bot_id'] = bot_id
    if body.dm_policy is not None:
        if body.dm_policy not in router_module.DM_POLICIES:
            raise HTTPException(status_code=400, detail='不支持的私聊策略')
        fields['dm_policy'] = body.dm_policy
    if body.group_policy is not None:
        if body.group_policy not in router_module.GROUP_POLICIES:
            raise HTTPException(status_code=400, detail='不支持的群聊策略')
        fields['group_policy'] = body.group_policy
    if body.allowed_users is not None:
        fields['allowed_users'] = _clean_users(body.allowed_users)
    if body.options is not None:
        allowed = {item.key for item in manifest.option_fields}
        unknown = set(body.options) - allowed
        if unknown:
            raise HTTPException(status_code=400,
                                detail=f'不支持的选项：{",".join(sorted(unknown))}')
        current = dict((existing or {}).get('config') or {})
        for key, value in body.options.items():
            text = str(value or '').strip()[:500]
            if text:
                current[key] = text
            else:
                current.pop(key, None)
        fields['config'] = current
    if existing is None:
        # 新行用清单的默认策略：个人微信/企微默认不响应群聊。
        fields.setdefault('dm_policy', manifest.default_dm_policy)
        fields.setdefault('group_policy', manifest.default_group_policy)
    row = await store.im_upsert_channel(
        channel_id, tenant_id=tenant, owner_user_id=owner,
        instance_id=instance_id, **fields)
    alive = await _gateway_alive(store)
    return await _view(store, manifest, row, gateway_alive=alive,
                       instance_id=instance_id)


def _clean_users(items) -> list[str]:
    out: list[str] = []
    for item in items or []:
        text = str(item or '').strip()[:200]
        if text and text not in out:
            out.append(text)
    return out[:200]


@router.post('/channels/{channel_id}/test')
async def test_channel(channel_id: str, body: TestBody, request: Request,
                       instance: str = Query(''),
                       principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    manifest = get_manifest(channel_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='未知渠道')
    instance_id = _instance_param(instance)
    tenant, owner = _ids(principal)
    row = await store.im_channel(channel_id, tenant_id=tenant,
                                 owner_user_id=owner,
                                 instance_id=instance_id)
    if row is None or not row.get('enabled'):
        raise HTTPException(status_code=409, detail='渠道未启用')
    login = {}
    if manifest.id == 'weixin':
        login = await store.im_login_get(channel_id, tenant_id=tenant,
                                         owner_user_id=owner)
    configured, missing, _callback = diagnose(
        manifest, login_state=login,
        public_base_url=settings.messaging_public_base_url,
        instance_id=instance_id)
    if not configured:
        raise HTTPException(status_code=409,
                            detail='还缺少：' + '、'.join(missing))
    if not await _gateway_alive(store):
        raise HTTPException(status_code=409, detail='消息网关未运行（需要 Worker）')
    target = (body.chat_id or str((row.get('config') or {}).get('home_chat_id') or '')).strip()
    if not target:
        raise HTTPException(status_code=400,
                            detail='请填写目标会话 ID（默认会话 ID 或本次测试地址）')
    sent_key = f'test:{secrets.token_hex(8)}'
    await store.im_outbox_add(
        tenant_id=tenant, owner_user_id=owner, channel_id=channel_id,
        instance_id=instance_id,
        platform_chat_id=target, sent_key=sent_key,
        text='这是一条来自智能体中台的测试消息。收到它就说明渠道已打通。')
    return {'ok': True, 'queued': True, 'target': target}


@router.post('/channels/{channel_id}/reload')
async def reload_channel(channel_id: str, request: Request,
                         instance: str = Query(''),
                         principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    if get_manifest(channel_id) is None:
        raise HTTPException(status_code=404, detail='未知渠道')
    instance_id = _instance_param(instance)
    tenant, owner = _ids(principal)
    row = await store.im_channel(channel_id, tenant_id=tenant,
                                 owner_user_id=owner,
                                 instance_id=instance_id)
    if row is not None:
        # 只清运行态不够：Worker 进程里的连接还活着，下一拍会认为"没变化"。
        # 写一个不展示的重载标记，让配置指纹变化，从而真正停掉再重建。
        config = dict(row.get('config') or {})
        config['_reload'] = secrets.token_hex(4)
        await store.im_upsert_channel(channel_id, tenant_id=tenant,
                                      owner_user_id=owner,
                                      instance_id=instance_id, config=config)
    await store.im_clear_runtime(channel_id, tenant_id=tenant,
                                 owner_user_id=owner, instance_id=instance_id)
    return {'ok': True, 'note': 'Worker 下一个节拍会重建连接'}


# ---------------------------------------------------------------- 机器人实例
#
# 一个实例 = 一个机器人 = 一套 IM 应用（各自的 AppID/Secret，环境变量加后缀）。
# instance_id 直接就是 `bot_id`：机器人的 id 唯一且稳定，再造一套命名只会让
# 「界面上的 tab」和「实际回话的机器人」之间多一层需要对照的映射。


class InstanceCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bot_id: str = Field(min_length=1, max_length=64)


def _instance_summary(channels: list[dict]) -> str:
    """几个渠道的状态收成一个实例的灯。

    复用 `state.py` 的词表而不是另发明一套：否则 tab 上的颜色和卡片里的
    badge 会对不上，同一个问题看起来像两个。
    """
    enabled = [item for item in channels if item['enabled']]
    if not enabled:
        return 'disabled'
    states = [item['state'] for item in enabled]
    for bad in ('fatal', 'startup_failed'):
        if bad in states:
            return bad
    if 'not_configured' in states:
        return 'not_configured'
    if 'gateway_stopped' in states:
        return 'gateway_stopped'
    if all(state == 'connected' for state in states):
        return 'connected'
    return 'pending_restart'


async def _instance_view(store, *, tenant: str, owner: str, instance_id: str,
                         channel_rows: dict, alive: bool,
                         runtimes: dict, login: dict) -> dict:
    bot = {}
    if instance_id:
        bot = await store.bot_row(instance_id, tenant_id=tenant,
                                  owner_user_id=owner) or {}
    channels = []
    for manifest in channels_for(instance_id):
        row = channel_rows.get(manifest.id) or _row_defaults(
            manifest, tenant, owner, instance_id)
        state = await _channel_state(
            store, manifest, row, instance_id=instance_id, gateway_alive=alive,
            login=login if manifest.id == 'weixin' else {},
            runtime=runtimes.get((manifest.id, instance_id)))
        derived = state['derived']
        channels.append({
            'id': manifest.id, 'name': manifest.name,
            'enabled': bool(row.get('enabled')),
            'configured': state['configured'],
            'missing': state['missing'],
            'state': derived['state'],
            'state_label': state_module.label(derived['state']),
            'tone': state_module.tone(derived['state']),
        })
    state = _instance_summary(channels)
    return {
        'instance_id': instance_id,
        'builtin': not instance_id,
        # 默认实例是存量那一份：凭据读基础键，名字只能是「默认」。
        'name': '默认' if not instance_id else str(bot.get('name') or instance_id),
        'handle': str(bot.get('handle') or ''),
        # 机器人被删了，但配置还在——要能看见并能收拾掉，不能假装它没事
        'bot_missing': bool(instance_id) and not bot,
        'env_suffix': instance_env_suffix(instance_id),
        'channels': channels,
        'enabled_count': sum(1 for item in channels if item['enabled']),
        'configured_count': sum(1 for item in channels if item['configured']),
        'state': state,
        'state_label': state_module.label(state),
        'tone': state_module.tone(state),
    }


def _env_names(instance_id: str) -> list[dict]:
    """这个实例**真正要填**的环境变量名。建完实例当场就得告诉用户，不然
    他照着文档填了基础键，第二个实例永远读不到凭据，而界面上只显示「缺凭据」。"""
    out = []
    for manifest in channels_for(instance_id):
        for item in manifest.env_fields:
            if not item.required:
                continue
            out.append({'channel': manifest.id, 'channel_name': manifest.name,
                        'env': manifest.env_name(item, instance_id),
                        'label': item.label})
    return out


@router.get('/instances')
async def list_instances(request: Request,
                         principal=Depends(policies.current_principal)):
    store = _store(request)
    tenant, owner = _ids(principal)
    rows = await store.im_list_channels(tenant_id=tenant, owner_user_id=owner)
    alive = await _gateway_alive(store)
    runtimes = {(str(item.get('channel_id') or ''),
                 str(item.get('instance_id') or '')): item
                for item in await store.im_runtime_list(
                    tenant_id=tenant, owner_user_id=owner)}
    login = await store.im_login_get('weixin', tenant_id=tenant,
                                     owner_user_id=owner)
    grouped: dict[str, dict[str, dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get('instance_id') or ''), {})[
            str(row.get('channel_id') or '')] = row
    # 默认实例永远在第一位，哪怕一行配置都没有：新部署进来得有落脚的地方。
    order = [''] + sorted(key for key in grouped if key)
    out = [await _instance_view(
        store, tenant=tenant, owner=owner, instance_id=instance_id,
        channel_rows=grouped.get(instance_id) or {}, alive=alive,
        runtimes=runtimes, login=login) for instance_id in order]
    return {'instances': out, 'gateway_alive': alive,
            'enabled': settings.messaging_enabled,
            'public_base_url': settings.messaging_public_base_url}


@router.post('/instances')
async def create_instance(body: InstanceCreate, request: Request,
                          principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, owner = _ids(principal)
    bot_id = body.bot_id.strip()
    bot = await store.bot_row(bot_id, tenant_id=tenant, owner_user_id=owner)
    if bot is None:
        raise HTTPException(status_code=404, detail='机器人不存在')
    if str(bot.get('status') or '') == 'archived':
        raise HTTPException(status_code=409, detail='这个机器人已归档，先恢复再接入')
    rows = await store.im_list_channels(tenant_id=tenant, owner_user_id=owner)
    if any(str(row.get('instance_id') or '') == bot_id for row in rows):
        raise HTTPException(status_code=409,
                            detail='这个机器人已经有独立的接入实例')
    created = []
    for manifest in channels_for(bot_id):
        # 先建成「关闭」：凭据还没填，开着的渠道只会立刻报一次启动失败。
        await store.im_upsert_channel(
            manifest.id, tenant_id=tenant, owner_user_id=owner,
            instance_id=bot_id, enabled=False, bot_id=bot_id,
            dm_policy=manifest.default_dm_policy,
            group_policy=manifest.default_group_policy)
        created.append(manifest.id)
    alive = await _gateway_alive(store)
    rows = await store.im_list_channels(tenant_id=tenant, owner_user_id=owner)
    channel_rows = {str(row.get('channel_id') or ''): row for row in rows
                    if str(row.get('instance_id') or '') == bot_id}
    view = await _instance_view(
        store, tenant=tenant, owner=owner, instance_id=bot_id,
        channel_rows=channel_rows, alive=alive,
        runtimes={(str(item.get('channel_id') or ''),
                   str(item.get('instance_id') or '')): item
                  for item in await store.im_runtime_list(
                      tenant_id=tenant, owner_user_id=owner)},
        login={})
    return {'ok': True, 'instance': view, 'channels': created,
            'env_required': _env_names(bot_id)}


@router.delete('/instances/{instance_id}')
async def delete_instance(instance_id: str, request: Request,
                          principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, owner = _ids(principal)
    key = _instance_param(instance_id)
    if not key:
        raise HTTPException(
            status_code=400,
            detail='默认实例不能删除：要停掉渠道请用「启用」开关，'
                   '整体下线请改部署配置')
    removed = await store.im_delete_instance(
        key, tenant_id=tenant, owner_user_id=owner)
    if not removed:
        raise HTTPException(status_code=404, detail='实例不存在')
    return {'ok': True, 'removed': removed,
            'note': '环境变量与历史会话都保留；要彻底清理请自行删 .env 里那几条'}


# ---------------------------------------------------------------- 会话身份


class SessionBindBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    channel_id: str
    instance: str = ''
    chat_id: str
    # 空串 = 恢复跟随渠道默认机器人。
    bot_id: str = ''


@router.get('/sessions')
async def list_sessions(
        request: Request,
        channel_id: str = Query(''),
        instance: str = Query(''),
        limit: int = Query(30, ge=1, le=200),
        principal=Depends(policies.current_principal)):
    """这个实例下最近的 IM 会话，以及每段会话由谁回话。

    「由谁回话」有两个来源，这里一次算清：会话单独指定（`im_sessions.bot_id`）
    优先，否则跟随渠道（`im_channels.bot_id`）。前端只显示 `bot_source`，不自己
    推导——否则卡片与顶栏会各算一套，出现两种答案。
    """
    store = _store(request)
    tenant, owner = _ids(principal)
    key = _instance_param(instance)
    if channel_id and get_manifest(channel_id) is None:
        raise HTTPException(status_code=404, detail='未知渠道')
    rows = await store.im_sessions_list(
        tenant_id=tenant, owner_user_id=owner,
        platform=channel_id or None,
        instance_id=key if channel_id else None,
        limit=limit)
    channels = await store.im_list_channels(tenant_id=tenant, owner_user_id=owner)
    defaults = {(str(row.get('channel_id') or ''),
                 str(row.get('instance_id') or '')): str(row.get('bot_id') or '')
                for row in channels}
    bots = await store.list_bot_rows(tenant_id=tenant, owner_user_id=owner)
    names = {str(bot.get('bot_id') or ''): bot for bot in bots}
    out = []
    for row in rows:
        override = str(row.get('bot_id') or '')
        channel_bot = defaults.get(
            (str(row.get('platform') or ''), str(row.get('instance_id') or '')), '')
        effective = override or channel_bot
        bot = names.get(effective) or {}
        out.append({
            'platform': str(row.get('platform') or ''),
            'instance': str(row.get('instance_id') or ''),
            'chat_id': str(row.get('chat_id') or ''),
            'chat_type': str(row.get('chat_type') or 'dm'),
            'bot_id': override,
            'effective_bot_id': effective,
            'effective_bot_name': str(bot.get('name') or '')
            if effective else '通用助手',
            'effective_bot_missing': bool(effective) and not bot,
            'bot_source': 'session' if override else 'channel',
            'generation': int(row.get('generation') or 1),
            'updated_at': str(row.get('updated_at') or ''),
        })
    return {
        'sessions': out,
        'bots': [{'bot_id': str(bot.get('bot_id') or ''),
                  'name': str(bot.get('name') or ''),
                  'handle': str(bot.get('handle') or '')} for bot in bots],
        'channel_bot_id': defaults.get((channel_id, key), '') if channel_id else '',
    }


@router.put('/sessions')
async def bind_session(body: SessionBindBody, request: Request,
                       principal=Depends(policies.current_principal)):
    """给一段会话单独指定机器人，或恢复跟随渠道（`bot_id=''`）。

    这里只写覆盖值，**不迁移已有对话**：下一条消息进来时 bridge 发现会话里
    的人格与生效身份不一致，会自己换一代新会话。旧对话连同历史留在本机。
    """
    _enabled()
    store = _store(request)
    manifest = get_manifest(body.channel_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail='未知渠道')
    tenant, owner = _ids(principal)
    key = _instance_param(body.instance)
    chat_id = str(body.chat_id or '').strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail='缺少会话标识')
    bot_id = str(body.bot_id or '').strip()
    if bot_id:
        bot = await store.bot_row(bot_id, tenant_id=tenant, owner_user_id=owner)
        if bot is None:
            raise HTTPException(status_code=404, detail='指定的机器人不存在')
    if not await store.im_session_set_bot(
            tenant_id=tenant, owner_user_id=owner, platform=manifest.id,
            chat_id=chat_id, bot_id=bot_id, instance_id=key):
        raise HTTPException(status_code=404,
                            detail='这段会话还没有和智能体聊过，先让对方发一条消息')
    return {'ok': True, 'chat_id': chat_id, 'bot_id': bot_id,
            'note': '下一条消息会按新身份开一段新对话' if bot_id else '已恢复跟随渠道'}


# ---------------------------------------------------------------- 配对


@router.get('/pairing')
async def list_pairing(request: Request,
                       principal=Depends(policies.current_principal)):
    store = _store(request)
    tenant, owner = _ids(principal)
    pending = await store.im_pairing_pending(
        tenant_id=tenant, owner_user_id=owner)
    approved = []
    for manifest in CHANNELS:
        approved.extend(await store.im_pairing_approved(
            tenant_id=tenant, owner_user_id=owner, platform=manifest.id))
    return {
        'pending': [_pairing_view(item, pending=True) for item in pending],
        'approved': [_pairing_view(item, pending=False) for item in approved],
    }


@router.post('/pairing/approve')
async def approve_pairing(body: ApproveBody, request: Request,
                          principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, owner = _ids(principal)
    code_hash = hashlib.sha256(body.code.strip().encode('utf-8')).hexdigest()
    matched = await store.im_pairing_approve(
        tenant_id=tenant, owner_user_id=owner, code_hash=code_hash)
    if matched is None:
        raise HTTPException(status_code=404, detail='配对码不存在、已过期或尝试次数过多')
    await _notify_pairing(store, matched, approved=True)
    return {'ok': True, 'platform': matched.get('platform'),
            'user_id': matched.get('user_id')}


@router.post('/pairing/revoke')
async def revoke_pairing(body: RevokeBody, request: Request,
                        principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, owner = _ids(principal)
    if get_manifest(body.platform) is None:
        raise HTTPException(status_code=404, detail='未知渠道')
    ok = await store.im_pairing_revoke(
        tenant_id=tenant, owner_user_id=owner, platform=body.platform,
        user_id=body.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail='授权不存在')
    # 撤销时行已经删了，拿不到 instance：走默认实例。tenant/owner 必须显式带上，
    # 否则会落到 `default` 名下，那条通知永远没人发出去。
    await _notify_pairing(store, {'platform': body.platform,
                                  'tenant_id': tenant, 'owner_user_id': owner,
                                  'user_id': body.user_id, 'chat_id': body.user_id},
                          approved=False)
    return {'ok': True}


async def _notify_pairing(store, row: dict, *, approved: bool) -> None:
    """把结果发回**发起配对请求的那个实例**。

    授权本身仍是渠道级的：同一个人在飞书里被批准过，换一个飞书应用进来不必再
    批一次——他是不是同一个人，与应用无关。但"通知从哪个机器人发出去"是应用级
    的事：请求是 A 应用收到的，回复却从 B 应用冒出来，用户会当成陌生人搭话。
    所以 `im_pairing.instance_id` 只用来找回话的通道，不参与授权判定。
    """
    platform = str(row.get('platform') or '')
    tenant = str(row.get('tenant_id') or 'default')
    owner = str(row.get('owner_user_id') or 'default')
    instance_id = str(row.get('instance_id') or '')
    chat_id = str(row.get('chat_id') or row.get('user_id') or '')
    if not chat_id:
        return
    text = ('配对成功，现在可以直接和我对话了。发 /help 看可用命令。'
            if approved else '管理员已撤销这个会话的访问授权。')
    try:
        await store.im_outbox_add(
            tenant_id=tenant, owner_user_id=owner, channel_id=platform,
            instance_id=instance_id,
            platform_chat_id=chat_id,
            sent_key=f'pair-{"ok" if approved else "revoked"}:{platform}:{row.get("user_id")}',
            text=text)
    except Exception:                              # noqa: BLE001
        logger.debug('写配对通知失败', exc_info=True)


# ---------------------------------------------------------------- 个人微信登录


@router.post('/channels/weixin/login/start')
async def weixin_login_start(request: Request,
                             principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, owner = _ids(principal)
    row = await store.im_channel('weixin', tenant_id=tenant,
                                 owner_user_id=owner)
    if row is None:
        other = await store.im_channel_owner('weixin', tenant_id=tenant)
        if other is not None and str(other.get('owner_user_id')) != owner:
            raise HTTPException(
                status_code=409,
                detail='个人微信登录已由其他账号发起；一个部署只能挂一个 iLink bot')

    async def persist(state: dict) -> None:
        await store.im_login_set('weixin', tenant_id=tenant,
                                 owner_user_id=owner, state=state)
        await store.im_clear_runtime('weixin', tenant_id=tenant,
                                     owner_user_id=owner)

    session = await _login_manager.start(tenant, owner, persist=persist)
    view = session.view()
    view['logged_in'] = session.status == 'confirmed'
    return view


@router.get('/channels/weixin/login/status')
async def weixin_login_status(request: Request,
                              principal=Depends(policies.current_principal)):
    store = _store(request)
    tenant, owner = _ids(principal)
    session = _login_manager.get(tenant, owner)
    if session is not None:
        view = session.view()
        view['logged_in'] = session.status == 'confirmed'
        return view
    saved = await store.im_login_get('weixin', tenant_id=tenant,
                                     owner_user_id=owner)
    if saved.get('token'):
        return {'status': 'confirmed', 'logged_in': True,
                'message': '已登录',
                'account_id': str(saved.get('account_id') or ''),
                'qr_svg': '', 'expires_in': 0}
    return {'status': 'idle', 'logged_in': False, 'message': '尚未扫码登录',
            'qr_svg': '', 'expires_in': 0}


@router.post('/channels/weixin/login/cancel')
async def weixin_login_cancel(request: Request,
                              principal=Depends(policies.current_principal)):
    tenant, owner = _ids(principal)
    await _login_manager.cancel(tenant, owner)
    return {'ok': True}


@router.post('/channels/weixin/login/logout')
async def weixin_login_logout(request: Request,
                              principal=Depends(policies.current_principal)):
    _enabled()
    store = _store(request)
    tenant, owner = _ids(principal)
    await _login_manager.cancel(tenant, owner)
    await store.im_login_clear('weixin', tenant_id=tenant,
                               owner_user_id=owner)
    await store.im_clear_runtime('weixin', tenant_id=tenant,
                                 owner_user_id=owner)
    return {'ok': True}


# ---------------------------------------------------------------- 审计事件


@router.get('/events')
async def list_events(request: Request, channel_id: str = Query(''),
                      instance: str = Query(''),
                      limit: int = Query(50, ge=1, le=200),
                      principal=Depends(policies.current_principal)):
    store = _store(request)
    tenant, owner = _ids(principal)
    if channel_id and get_manifest(channel_id) is None:
        raise HTTPException(status_code=404, detail='未知渠道')
    return {'events': await _events(store, tenant, owner, channel_id, limit,
                                    _instance_param(instance) or None)}


# ---------------------------------------------------------------- 平台回调


def _rate_ok(request: Request) -> bool:
    """极简固定窗口限流：验签要解密，不能让公网把 CPU 打满。"""
    host = str(getattr(request.client, 'host', '') or 'unknown')
    now = time.time()
    window, count = _webhook_hits.get(host, (now, 0))
    if now - window > WEBHOOK_RATE_WINDOW:
        window, count = now, 0
    count += 1
    _webhook_hits[host] = (window, count)
    if len(_webhook_hits) > 4096:
        _webhook_hits.clear()
    return count <= WEBHOOK_RATE_MAX


async def _callback_rows(store, channel_id: str) -> list[tuple[dict, ChannelConfig]]:
    manifest = get_manifest(channel_id)
    if manifest is None or not settings.messaging_enabled:
        return []
    rows = [row for row in await store.im_enabled_channels()
            if str(row.get('channel_id')) == channel_id]
    out = []
    for row in rows:
        login = {}
        if channel_id == 'weixin':
            login = await store.im_login_get(
                channel_id, tenant_id=str(row['tenant_id']),
                owner_user_id=str(row['owner_user_id']))
        out.append((row, build_channel_config(row, manifest, login)))
    return out


@router.post('/webhook/feishu')
async def feishu_webhook(request: Request):
    if not _rate_ok(request):
        raise HTTPException(status_code=429, detail='too many requests')
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail='payload too large')
    store = _store(request)
    candidates = await _callback_rows(store, 'feishu')
    if not candidates:
        raise HTTPException(status_code=404, detail='飞书渠道未配置或未启用')
    last_error = ''
    for row, config in candidates:
        try:
            parsed = FeishuTransport.parse_webhook(raw, request.headers, config)
        except TransportError as exc:
            last_error = exc.code
            continue
        except Exception as exc:                    # noqa: BLE001
            last_error = type(exc).__name__
            logger.warning('飞书回调解析异常', exc_info=True)
            continue
        if parsed.kind == 'challenge':
            return JSONResponse({'challenge': parsed.challenge})
        if parsed.kind == 'message' and parsed.inbound is not None:
            event = parsed.inbound
            await store.im_inbox_add(
                platform=event.platform, tenant_id=str(row['tenant_id']),
                owner_user_id=str(row['owner_user_id']),
                instance_id=str(row.get('instance_id') or ''),
                message_id=event.message_id,
                payload=_inbound_payload(event))
        return JSONResponse({'code': 0})
    logger.warning('飞书回调未通过校验：%s', last_error)
    raise HTTPException(status_code=403, detail='callback verification failed')


@router.api_route('/webhook/wecom', methods=['GET', 'POST'])
async def wecom_webhook(request: Request):
    if not _rate_ok(request):
        raise HTTPException(status_code=429, detail='too many requests')
    store = _store(request)
    candidates = await _callback_rows(store, 'wecom')
    if not candidates:
        raise HTTPException(status_code=404, detail='企业微信渠道未配置或未启用')
    raw = b'' if request.method == 'GET' else await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail='payload too large')
    query = dict(request.query_params)
    last_error = ''
    for row, config in candidates:
        try:
            parsed = WeComTransport.parse_webhook(
                method=request.method, query=query, raw=raw, config=config)
        except TransportError as exc:
            last_error = exc.code
            continue
        except Exception as exc:                    # noqa: BLE001
            last_error = type(exc).__name__
            logger.warning('企业微信回调解析异常', exc_info=True)
            continue
        if parsed.kind == 'verify':
            return PlainTextResponse(parsed.plaintext)
        if parsed.kind == 'message' and parsed.inbound is not None:
            event = parsed.inbound
            await store.im_inbox_add(
                platform=event.platform, tenant_id=str(row['tenant_id']),
                owner_user_id=str(row['owner_user_id']),
                instance_id=str(row.get('instance_id') or ''),
                message_id=event.message_id,
                payload=_inbound_payload(event))
        return PlainTextResponse('')
    logger.warning('企业微信回调未通过校验：%s', last_error)
    raise HTTPException(status_code=403, detail='callback verification failed')


def _inbound_payload(event) -> dict:
    from webpage_agent.messaging.models import inbound_to_payload
    return inbound_to_payload(event)
