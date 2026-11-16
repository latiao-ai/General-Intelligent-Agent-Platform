"""管理控制台 API（`/api/console/*`）：跨租户的用户、活跃度、连接器与审计。

## 和 `/api/admin/*` 的关系

`/api/admin/*` 是**租户运维**接口，语义是"管我这一个企业"，已有测试锁着行为，
这里一个字都不动。控制台是**平台超管**视角——一个人看所有租户——所以单开一套
路径，而不是把租户过滤从旧接口里拆掉。拆掉是最省事的改法，也是最容易把
"本企业运维"悄悄升级成"跨企业"的改法。

## 准入

`console_access()` 是唯一入口，返回三档：

| 结果 | 谁 | 能做什么 |
|---|---|---|
| `all` | `identities.role='platform_admin'` | 全部租户、全部功能（含删除账号） |
| `tenant` | `role='operator'` | 只看/管本租户用户；**不能**授予 platform_admin |
| `''` | 其余 | 403 |

控制台只有两档**可授予**的角色：`member`（普通用户）与 `platform_admin`（超管）。
`operator` 是遗留角色，仍被识别（否则存量账号会被挡在门外），但不再授予、也不再
出现在页面的下拉里。

另有一条只在本机成立的例外：`AUTH_MODE=off` 且 `APP_ENV != production` 时按 `all`
放行。没有它，本地开发根本进不去控制台——`/api/admin/users` 在 off 模式下恒 401，
就是这个坑的现成例子。这条口子**必须**和 `APP_ENV` 挂钩：只认 `AUTH_MODE=off`
的话，谁把生产环境的认证关掉，控制台就跟着裸奔。

## 为什么写操作都要查 Origin

控制台的鉴权靠 cookie，浏览器会自动带上，所以**任何第三方页面都能发起跨站请求**。
只靠 `SameSite=Lax` 不够：老浏览器、以及被配置成 `none` 的部署都不保险。
因此所有 PATCH 都走 `require_trusted_origin()`，非浏览器客户端（没有 Origin 头）
在正式环境同样拒绝。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     UploadFile)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.bot import prompts as bot_prompts
from webpage_agent.core.config import settings
from webpage_agent.governance import policies
from webpage_agent.api import local_auth as local_auth_service
from webpage_agent.integrations import mcp_servers
from webpage_agent.observe import log_buffer as log_buffer_module
from webpage_agent.observe import logging_setup

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/console', tags=['console'])

ACCESS_ALL = 'all'
ACCESS_TENANT = 'tenant'

# 只有这些角色可以被授予。写死在这里而不是从 store 里读：角色是准入的一部分，
# 不该允许运行时被数据改出新的取值。
#
# 只有两档：普通用户与超管。`operator`（本企业运维）是历史角色，`console_access`
# 仍然认它，只是为了不把存量账号挡在门外；这里不再授予，页面也不给它选项。
_ASSIGNABLE_ROLES = ('member', 'platform_admin')


def console_access(principal) -> str:
    """这次请求能管多大范围。见模块说明的准入表。"""
    role = str(getattr(principal, 'role', '') or 'member')
    if role == 'platform_admin':
        return ACCESS_ALL
    if getattr(principal, 'authenticated', False) is False:
        # 未认证还能走到这里，只有 AUTH_MODE=off 的开发环境。
        if settings.auth_mode == 'off' and settings.app_env != 'production':
            return ACCESS_ALL
        return ''
    if role == 'operator':
        return ACCESS_TENANT
    return ''


def require_console(principal) -> str:
    scope = console_access(principal)
    if not scope:
        raise HTTPException(status_code=403, detail='需要管理员权限')
    return scope


def require_all(principal) -> str:
    scope = require_console(principal)
    if scope != ACCESS_ALL:
        raise HTTPException(
            status_code=403,
            detail='这个视图是跨租户的，需要 platform_admin。'
                   '本企业范围请用 /api/admin/*')
    return scope


def require_trusted_origin(request: Request) -> None:
    """写操作的来源校验。规则见模块说明最后一段。

    判定顺序（**不要合并成一条 if**，合起来就分不清"没带 Origin"和"带了但不认识"）：

    1. 带了 Origin 且在白名单里 → 放行；
    2. 生产环境 → 必须有且必须在白名单里。浏览器对同源 PATCH 也会带 Origin，
       所以这条对浏览器流量几乎不产生误伤；不带 Origin 的只可能是脚本，
       控制台不允许脚本改数据；
    3. 非生产环境 → 接受同源，也接受干脆不带 Origin（curl / 脚本联调用）。
       但**带了别的源**仍然拒绝，免得开发时养出跨站随便写的习惯。
    """
    origin = (request.headers.get('origin') or '').strip()
    allowed = tuple(settings.auth_allowed_origins)
    host = (request.headers.get('host') or '').strip().lower()
    parsed = urlsplit(origin) if origin else None
    wellformed = bool(
        parsed and parsed.scheme in ('http', 'https') and parsed.hostname
        and not parsed.username and not parsed.password
        and not parsed.path and not parsed.query and not parsed.fragment
        and parsed.port != 0 and not any(c.isspace() for c in origin))

    if wellformed and origin in allowed:
        return

    production = settings.app_env == 'production'
    if production:
        raise HTTPException(status_code=403,
                            detail='Origin 不在允许列表（生产环境必须显式配置 AUTH_ALLOWED_ORIGINS）')
    if not origin:
        return
    if wellformed and host and parsed.netloc.lower() == host:
        return
    raise HTTPException(status_code=403, detail='跨源写操作已拒绝')


def store_of(request: Request):
    store = getattr(request.app.state, 'store', None)
    if store is None:
        raise HTTPException(status_code=503, detail='仓储未就绪')
    return store


def _ok(payload) -> JSONResponse:
    return JSONResponse(payload, headers={'Cache-Control': 'no-store'})


# ---------- 主体 ----------

@router.get('/me')
async def console_me(principal=Depends(policies.current_principal)):
    """页面启动时先问一句"我是谁、能管多大"。"""
    scope = console_access(principal)
    return _ok({'user_id': principal.user_id,
                'tenant_id': principal.tenant_id,
                'role': getattr(principal, 'role', 'member') or 'member',
                'scope': scope,
                'allowed': bool(scope),
                'auth_mode': settings.auth_mode,
                'roles': list(_ASSIGNABLE_ROLES)})


# ---------- 概览 ----------

@router.get('/overview')
async def console_overview(days: int = Query(14, ge=1, le=90),
                           principal=Depends(policies.current_principal),
                           store=Depends(store_of)):
    require_all(principal)
    return _ok(await store.console_overview(days=days))


@router.get('/credits')
async def console_credits(days: int = Query(7, ge=1, le=90),
                          tenant: str = '',
                          principal=Depends(policies.current_principal),
                          store=Depends(store_of)):
    """积分：每个用户一行，含余额、窗口内消耗、每日趋势与最近变动。

    余额 = 进账合计 − provider_charges 折算的已用合计（口径见
    `webpage_agent.store.pg_credits`）。跨租户时按租户逐个聚合再拼起来：
    积分是租户内的账，不能把两个企业的余额加在一起。
    """
    scope = require_console(principal)
    tenants = ([tenant] if tenant else await store.credit_tenants()) if scope == ACCESS_ALL \
        else [principal.tenant_id]
    rows: list[dict] = []
    for tenant_id in tenants:
        data = await store.credit_overview(tenant_id, days)
        for user in data['users']:
            user['tenant_id'] = tenant_id
            rows.append(user)
    rows.sort(key=lambda item: item['balance'])
    return _ok({'days': days, 'tenants': tenants, 'total': len(rows), 'users': rows})


# ---------- 用户 ----------

@router.get('/users')
async def console_users(q: str = '', status: str = '', role: str = '',
                        tenant: str = '', limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0),
                        principal=Depends(policies.current_principal),
                        store=Depends(store_of)):
    """用户列表。`tenant` 参数只对 platform_admin 生效——
    operator 传什么都不影响结果，避免"改个查询参数就看到别的企业"。"""
    scope = require_console(principal)
    tenant_id = tenant if scope == ACCESS_ALL else principal.tenant_id
    if status and status not in ('active', 'disabled'):
        raise HTTPException(status_code=400, detail='status 只能是 active 或 disabled')
    if role and role not in _ASSIGNABLE_ROLES:
        raise HTTPException(status_code=400, detail='未知角色')
    return _ok(await store.console_users(
        tenant_id=tenant_id, query=q.strip(), status=status, role=role,
        limit=limit, offset=offset))


@router.get('/users/{user_id}')
async def console_user(user_id: str, days: int = Query(14, ge=1, le=90),
                       principal=Depends(policies.current_principal),
                       store=Depends(store_of)):
    scope = require_console(principal)
    detail = await store.console_user(user_id, days=days)
    if detail is None:
        raise HTTPException(status_code=404, detail='用户不存在')
    if scope != ACCESS_ALL:
        # 租户运维只能看本企业的人。用 404 而不是 403：状态码差异本身就是
        # "这个 user_id 存在"的信息泄露。
        if not any(row['tenant_id'] == principal.tenant_id
                   for row in detail['identities']):
            raise HTTPException(status_code=404, detail='用户不存在')
    else:
        # 删除确认框要显示「会删掉多少东西」。只读统计，不产生任何写入。
        detail['purge_preview'] = await store.purge_preview(user_id)
    return _ok(detail)


class RoleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['member', 'platform_admin']
    tenant_id: str = Field(default='', max_length=80)
    reason: str = Field(default='', max_length=200)


@router.patch('/users/{user_id}/role')
async def console_set_role(user_id: str, payload: RoleRequest, request: Request,
                           principal=Depends(policies.current_principal),
                           store=Depends(store_of)):
    scope = require_console(principal)
    require_trusted_origin(request)
    if payload.role == 'platform_admin' and scope != ACCESS_ALL:
        raise HTTPException(status_code=403, detail='只有 platform_admin 能授予 platform_admin')
    tenant_id = payload.tenant_id if scope == ACCESS_ALL else principal.tenant_id
    try:
        row = await store.set_user_role(
            user_id=user_id, role=payload.role, tenant_id=tenant_id,
            actor_id=principal.user_id, reason=payload.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ok(row)


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['active', 'disabled']


@router.patch('/users/{user_id}/status')
async def console_set_status(user_id: str, payload: StatusRequest, request: Request,
                             principal=Depends(policies.current_principal),
                             store=Depends(store_of)):
    scope = require_console(principal)
    require_trusted_origin(request)
    if scope != ACCESS_ALL:
        detail = await store.console_user(user_id)
        if detail is None or not any(row['tenant_id'] == principal.tenant_id
                                     for row in detail['identities']):
            raise HTTPException(status_code=404, detail='用户不存在')
    try:
        row = await store.console_set_user_status(
            user_id=user_id, status=payload.status, actor_id=principal.user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ok(row)


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # 仅为兼容老客户端保留，服务端不再校验内容。防误删交给前端一次明确的
    # confirm()（带昵称与预计清除条数），不再要求手打 user_id。
    confirm: str = Field(default='', max_length=120)
    reason: str = Field(default='', max_length=200)
    # 2026-11-12 起删除 = 清空该用户的全部数据，必须显式确认这个语义，
    # 免得老客户端还以为只是「撤销登录身份」。
    purge: bool = False


@router.delete('/users/{user_id}')
async def console_delete_user(user_id: str, payload: DeleteRequest, request: Request,
                              principal=Depends(policies.current_principal),
                              store=Depends(store_of)):
    """删除账号。仅超管，对任何用户（不限身份源、不限状态）都可用。

    租户运维（`operator`）不给这个能力：删账号会同时抹掉这个人在**所有**租户的
    身份绑定，超出"本企业运维"的边界。仍保留两条硬保护：不能删自己、不能删
    最后一个超管——否则控制台会永久锁死。
    """
    require_all(principal)
    require_trusted_origin(request)
    if not payload.purge:
        raise HTTPException(status_code=400,
                            detail='请显式确认 purge=true（删除该用户的全部数据且不可恢复）')
    try:
        row = await store.console_delete_user(
            user_id=user_id, actor_id=principal.user_id, reason=payload.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ok(row)


# ---------- 本地账号管理（手动建号 / 重置密码 / 解锁） ----------

class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default='', max_length=80)
    role: Literal['member', 'platform_admin'] = 'member'
    must_change_password: bool = False


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=1, max_length=128)
    # 给外部身份源的用户首次绑定登录方式时才需要；不传就用 user_id 当登录账号。
    username: str = Field(default='', max_length=64)


@router.post('/users')
async def console_create_user(payload: CreateUserRequest, request: Request,
                              principal=Depends(policies.current_principal),
                              store=Depends(store_of)):
    """超管手动创建用户，自行设置账号密码。仅超管，且要求本地账号已启用。"""
    require_all(principal)
    require_trusted_origin(request)
    if not local_auth_service.enabled():
        raise HTTPException(status_code=404, detail='本地账号登录未启用')
    try:
        name = local_auth_service.normalize_username(payload.username)
        password = local_auth_service.validate_password(payload.password)
        row = await store.create_local_account(
            username=name,
            password_hash=local_auth_service.hash_password(password),
            display_name=payload.display_name.strip() or name,
            role=payload.role,
            tenant_id=settings.local_auth_tenant_id,
            identity_provider_id=settings.local_auth_identity_provider_id,
            must_change_password=payload.must_change_password,
            created_by=principal.user_id)
    except local_auth_service.LocalAuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ok(row)


@router.post('/users/{user_id}/password')
async def console_reset_password(user_id: str, payload: ResetPasswordRequest,
                                 request: Request,
                                 principal=Depends(policies.current_principal),
                                 store=Depends(store_of)):
    """超管为**任意用户**设置 / 重置密码，并撤销该用户全部登录会话。

    已有本地凭据 → 直接改哈希。BeeFTV / 短信 / mock-idp 等外部身份源 → 中台
    不保管上游密码，改为给**同一个中台账号**补一条本地登录凭据：用户之后在
    登录框选「本地账号」用新密码进入，user_id、角色与历史数据都不变。
    """
    require_all(principal)
    require_trusted_origin(request)
    try:
        password = local_auth_service.validate_password(payload.password)
        digest = local_auth_service.hash_password(password)
        account = await store.local_account_by_user(user_id)
        if account:
            await store.local_account_set_password(
                username=account['username'], password_hash=digest,
                actor_id=principal.user_id)
            username, created = account['username'], False
        else:
            name = local_auth_service.normalize_managed_username(
                payload.username.strip() or user_id)
            bound = await store.local_account_bind_to_user(
                user_id=user_id, username=name, password_hash=digest,
                tenant_id=settings.local_auth_tenant_id,
                identity_provider_id=settings.local_auth_identity_provider_id,
                actor_id=principal.user_id)
            username, created = bound['username'], True
    except local_auth_service.LocalAuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileExistsError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ok({'user_id': user_id, 'ok': True,
                'username': username, 'created': created})


@router.post('/users/{user_id}/unlock')
async def console_unlock_user(user_id: str, request: Request,
                              principal=Depends(policies.current_principal),
                              store=Depends(store_of)):
    """清除连续失败计数与锁定期。没有本地凭据的用户本来就不会被锁，直接返回成功。"""
    require_all(principal)
    require_trusted_origin(request)
    account = await store.local_account_by_user(user_id)
    if not account:
        return _ok({'user_id': user_id, 'ok': True, 'noop': True,
                    'detail': '该用户没有本地密码，无需解锁'})
    await store.local_account_unlock(account['username'],
                                     actor_id=principal.user_id)
    return _ok({'user_id': user_id, 'ok': True})


@router.get('/local-accounts')
async def console_local_accounts(q: str = '', limit: int = 100,
                                 offset: int = 0,
                                 principal=Depends(policies.current_principal),
                                 store=Depends(store_of)):
    """本地账号列表。响应里绝不返回密码哈希。"""
    require_all(principal)
    return _ok(await store.local_account_list(query=q, limit=limit, offset=offset))


# ---------- 连接器与技能 ----------

class PolicyToggleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


# 绑定表只存 `server_id / transport / enabled`：没有地址，也没有「这条是谁配的」。
# 控制台既然要把运维配置、平台目录、用户自建放在一张表里看，就得回 `mcp_servers`
# 的合并视图查一次——那一层已经把 `MCP_SERVERS`、平台目录、兼容期全局单文件按
# 优先级并好了。用户自建那层按主体分文件，控制台没有身份上下文，不去猜，标成
# 「用户自建」即可。
_CONNECTOR_SOURCE_LABELS = {
    'ops': '运维配置',
    'platform': '平台目录',
    'shared': '全局配置文件',
    'user': '用户自建',
}


def _global_servers() -> dict:
    try:
        return {server.id: server for server in mcp_servers.servers()}
    except Exception:  # 配置解析炸了不该让整个连接器页打不开
        logger.warning('读取 MCP 服务端清单失败，连接器来源无法解析', exc_info=True)
        return {}


def _with_connector_sources(data: dict) -> dict:
    """给绑定行补来源、显示名、地址；另外整份服务端清单也交给页面。

    「统一管理所有连接器」光看绑定不够：运维配好、还没人连的那几条根本不会出现，
    结果就是「libtv 明明配了，页面上却查不到它在哪加的」。所以额外带一份 `servers`，
    它就是用户连接器页能看到的全站清单。
    """
    known = _global_servers()
    data['servers'] = [{
        'id': server.id,
        'name': server.title or server.id,
        'transport': server.transport,
        'url': server.url,
        'command': server.stdio_command,
        'auth': server.auth,
        'source': server.scope,
        'source_label': _CONNECTOR_SOURCE_LABELS.get(server.scope, server.scope),
    } for server in known.values()]

    for group in data.get('tenants') or []:
        for binding in group.get('bindings') or []:
            # 版本号页面上早就不显示了，别再把没人用的字段送出去。
            binding.pop('revision', None)
            server = known.get(binding.get('server_id'))
            if server is None:
                binding.update({'source': 'user', 'source_label': '用户自建'})
                continue
            binding.update({
                'source': server.scope,
                'source_label': _CONNECTOR_SOURCE_LABELS.get(server.scope, server.scope),
                'name': server.title or server.id,
                'url': server.url,
                'command': server.stdio_command,
            })
    return data


@router.get('/connectors')
async def console_connectors(principal=Depends(policies.current_principal),
                             store=Depends(store_of)):
    require_all(principal)
    return _ok(_with_connector_sources(await store.console_connectors()))


# --- 平台连接器：管理员发布一次，所有用户的连接器页默认出现这一项 ---
#
# 与上面 `/connectors`（各租户**已经绑定**的服务端）不是一回事：
# 那个是"谁接了哪个"，只读；这里是"平台提供哪些"，可增删改。写进去的条目会
# 通过 `mcp_servers.servers()` 进入工具发现，用户点「+」就能真的连上。

class PlatformConnectorRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(default='', max_length=80)
    description: str = Field(default='', max_length=240)
    icon: str = Field(default='', max_length=8)
    transport: Literal['streamable_http', 'sse'] = 'streamable_http'
    url: str = Field(default='', max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: Literal['', 'oauth'] = ''
    oauth_issuer: str = Field(default='', max_length=300)
    oauth_scopes: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True


# 技能图标不再限定成一个 emoji：控制台支持上传图片，前端会先缩到 128px 再转
# data URL 提交，留 300KB 足够，也不至于让目录 JSON 被一张大图撑爆。
_SKILL_ICON_MAX = 300_000
# 技能指令现在可以直接拖一份 SKILL.md 进来，2000 字装不下一份正经技能说明。
_SKILL_PROMPT_MAX = 20_000
_SKILL_OVERVIEW_MAX = 600
# 「试试这样用」：用户在详情弹窗里点一下就填进输入框的示例问法。
# 8 条足够覆盖主要用法，再多就成了说明书。
_SKILL_EXAMPLES_MAX = 8
_SKILL_EXAMPLE_LEN = 120


class PlatformSkillRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default='', max_length=240)
    category: str = Field(default='效率工具', max_length=40)
    icon: str = Field(default='', max_length=_SKILL_ICON_MAX)
    version: str = Field(default='1.0.0', max_length=20)
    overview: str = Field(default='', max_length=_SKILL_OVERVIEW_MAX)
    prompt: str = Field(default='', max_length=_SKILL_PROMPT_MAX)
    examples: list[str] = Field(default_factory=list, max_length=_SKILL_EXAMPLES_MAX)
    enabled: bool = True
    installed_by_default: bool = False


def _catalog_guard(fn, *args, **kwargs):
    """把目录层的校验错误映射成 HTTP 状态码。"""
    from webpage_agent.store import platform_catalog

    try:
        return fn(*args, **kwargs)
    except platform_catalog.CatalogError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


def _occupied_connector_ids(platform_ids: set[str]) -> set[str]:
    """已经被 `MCP_SERVERS` 或用户自建占掉的 id。

    同名时运维配置优先，平台条目会被静默跳过；控制台必须把这件事显示出来，
    否则管理员会以为"加了但没生效"。
    """
    from webpage_agent.integrations import mcp_servers

    return {server.id for server in mcp_servers.servers()} - platform_ids


@router.get('/platform/connectors')
async def console_platform_connectors(principal=Depends(policies.current_principal)):
    from webpage_agent.store import platform_catalog

    require_all(principal)
    rows = platform_catalog.load()['connectors']
    occupied = _occupied_connector_ids({row['id'] for row in rows})
    items = []
    for row in rows:
        transport = row.get('transport') or 'streamable_http'
        if transport not in ('streamable_http', 'sse'):
            transport = 'streamable_http'
        auth = row.get('auth') if row.get('auth') in ('', 'oauth') else ''
        items.append({
            'id': row.get('id'),
            'name': row.get('name') or row.get('id'),
            'description': row.get('description') or '',
            'icon': row.get('icon') or '🔗',
            'transport': transport,
            'url': row.get('url') or '',
            'headers': row.get('headers') or {},
            'auth': auth,
            'oauth_issuer': row.get('oauth_issuer') or '',
            'oauth_scopes': list(row.get('oauth_scopes') or []),
            'enabled': bool(row.get('enabled', True)),
            # 被更高优先级的配置占了 → 用户看不到这一项
            'shadowed': row.get('id') in occupied,
            'created_at': row.get('created_at') or '',
            'updated_by': row.get('updated_by') or '',
        })
    return _ok({'items': items, 'total': len(items)})


@router.post('/platform/connectors')
async def console_save_platform_connector(payload: PlatformConnectorRequest,
                                          request: Request,
                                          principal=Depends(policies.current_principal)):
    from webpage_agent.store import platform_catalog

    require_all(principal)
    require_trusted_origin(request)
    row = _catalog_guard(platform_catalog.validate_connector, payload.model_dump())
    saved = _catalog_guard(platform_catalog.upsert, 'connectors', row,
                           actor=principal.user_id)
    return _ok(saved)


@router.patch('/platform/connectors/{connector_id}')
async def console_toggle_platform_connector(connector_id: str,
                                            payload: PolicyToggleRequest,
                                            request: Request,
                                            principal=Depends(policies.current_principal)):
    """只切启停。改地址/凭据走 POST 整体保存，避免半改半不改。"""
    from webpage_agent.store import platform_catalog

    require_all(principal)
    require_trusted_origin(request)
    return _ok(_catalog_guard(platform_catalog.set_enabled, 'connectors', connector_id,
                              payload.enabled, actor=principal.user_id))


@router.delete('/platform/connectors/{connector_id}')
async def console_delete_platform_connector(connector_id: str, request: Request,
                                            principal=Depends(policies.current_principal)):
    from webpage_agent.store import platform_catalog

    require_all(principal)
    require_trusted_origin(request)
    return _ok(_catalog_guard(platform_catalog.remove, 'connectors', connector_id))


@router.get('/platform/skills')
async def console_platform_skills(principal=Depends(policies.current_principal)):
    from webpage_agent.addons import skills as skills_lib
    from webpage_agent.skillhub import catalog as skill_catalog
    from webpage_agent.store import platform_catalog

    require_all(principal)
    rows = platform_catalog.load()['skills']
    # 内置目录先占位（本地优先），平台条目同 id 时不会出现在用户页上。
    builtin = {item['id'] for item in skill_catalog.SKILLS}
    items = []
    for row in rows:
        # 包"记着"不等于"还在"：迁移目录、清缓存都可能只剩一条路径。
        # 列表要给的是能装的包，不是曾经上传过的包。
        resolved = platform_catalog.package_path(row.get('package'))
        mark = skills_lib.installed_package(str(row.get('id') or ''))
        digest = str(row.get('package_digest') or '')
        installed = (skills_lib.root() / str(row.get('id') or '')).is_dir()
        items.append({
            'id': row.get('id'),
            'name': row.get('name') or row.get('id'),
            'description': row.get('description') or '',
            'category': row.get('category') or '效率工具',
            'icon': row.get('icon') or '✨',
            'version': row.get('version') or '1.0.0',
            'overview': row.get('overview') or '',
            'prompt': row.get('prompt') or '',
            'examples': row.get('examples') or [],
            'enabled': bool(row.get('enabled', True)),
            'installed_by_default': bool(row.get('installed_by_default')),
            'shadowed': row.get('id') in builtin,
            # 有包 = 用户安装时能拿到 references/scripts；没包的技能只有一份生成的正文。
            'package': resolved is not None,
            'package_name': resolved.name if resolved else '',
            'package_original_name': row.get('package_name') or '',
            'package_version': row.get('package_version') or '',
            'package_files': int(row.get('package_files') or 0),
            'package_bytes': int(row.get('package_bytes') or 0),
            'package_digest': digest,
            # 已装目录的状态：管理员改完包要能看见"用户手上还是旧的"，
            # 而不是等用户来问"为什么只有 1 个文件"。
            'installed': installed,
            'installed_digest': str(mark.get('digest') or ''),
            'sync_state': ('uninstalled' if not installed else
                           'synced' if digest and mark.get('digest') == digest else
                           'stale'),
            # 记着包但文件没了：这不是"仅正文"，而是"传过、后来丢了"，得单独提示重传。
            'package_missing': bool(row.get('package')) and resolved is None,
            'package_stale': bool(row.get('package')
                                  and row.get('package_version')
                                  and row.get('package_version') != row.get('version')),
            'created_at': row.get('created_at') or '',
            'updated_by': row.get('updated_by') or '',
        })
    return _ok({'items': items, 'total': len(items),
                'categories': list(skill_catalog.CATEGORIES)})


@router.post('/platform/skills')
async def console_save_platform_skill(payload: PlatformSkillRequest, request: Request,
                                      principal=Depends(policies.current_principal)):
    from webpage_agent.skillhub import catalog as skill_catalog
    from webpage_agent.store import platform_catalog

    require_all(principal)
    require_trusted_origin(request)
    row = _catalog_guard(platform_catalog.validate_skill, payload.model_dump(),
                         tuple(skill_catalog.CATEGORIES))
    saved = _catalog_guard(platform_catalog.upsert, 'skills', row,
                           actor=principal.user_id)
    return _ok(saved)


_PACKAGE_EXTS = ('.tar.gz', '.tgz', '.tar', '.zip', '.md')


def _package_dir(skill_id: str) -> Path:
    """技能包目录。id 已在 `platform_catalog` 校验过（小写字母数字 ._-），
    这里再核一遍再做路径拼接——`..` 之类绝不能拼进来。"""
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', skill_id):
        raise HTTPException(status_code=400, detail='技能 ID 不合法')
    return Path(settings.skillhub_packages_dir) / skill_id


def _parse_paths(raw: str, count: int) -> list[str]:
    """前端给的相对路径（JSON 数组）；长度对不上就按文件自己的名字兜底。"""
    try:
        items = json.loads(raw) if str(raw or '').strip() else []
    except ValueError:
        items = []
    if not isinstance(items, list):
        items = []
    out = [str(x or '') for x in items][:count]
    return out + [''] * (count - len(out))


def _repack(files: list[UploadFile], payloads: list[bytes],
            paths: list[str]) -> bytes:
    """散装文件（拖文件夹进来时浏览器就是一次给一堆 File）打成 zip。

    在服务端打包而不是让前端写 zip：浏览器里手搓 zip 的头部/CRC 一旦有偏差，
    表现为"上传成功但安装解不开"，排查成本远高于这里多写十行。
    """
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for upload, data, raw in zip(files, payloads, paths):
            path = str(raw or '').replace('\\', '/').strip() or str(upload.filename or '')
            parts = [p for p in path.split('/') if p not in ('', '.')]
            if not parts or any(p == '..' for p in parts):
                raise HTTPException(
                    status_code=400,
                    detail=f'文件路径不合法：{path or upload.filename}')
            archive.writestr('/'.join(parts), data)
    return buffer.getvalue()


@router.post('/platform/skills/{skill_id}/package')
async def console_upload_platform_package(skill_id: str, request: Request,
                                          files: list[UploadFile] = File(...),
                                          paths: str = Form(default=''),
                                          principal=Depends(policies.current_principal)):
    """上传技能包（zip / tar.gz / .md，或者一批散装文件），**只存不解包**。

    为什么不直接解到 `skills_dir`：那里出现目录就等于"所有用户都已安装"
    （`_decorate()` 把 on_disk 当作 installed），发布阶段落盘等于绕过安装。
    所以发布只把原件存进包仓库，等用户点「+」才解包。

    散装文件（拖文件夹/多选）在服务端压成 zip：浏览器端自己拼 zip 很容易在
    CRC 或头部写错，而那种错只会表现为"上传看起来成功、安装解不开"。
    """
    from webpage_agent.addons import skills as skills_lib
    from webpage_agent.store import platform_catalog

    require_all(principal)
    require_trusted_origin(request)
    skill_id = str(skill_id or '').strip().lower()
    rows = {row['id']: row for row in platform_catalog.load()['skills']}
    if skill_id not in rows:
        raise HTTPException(status_code=404, detail='先保存技能基本信息，再上传技能包')

    uploads = [item for item in (files or []) if item is not None]
    if not uploads:
        raise HTTPException(status_code=400, detail='没有收到文件')
    payloads = [await item.read() for item in uploads]
    total = sum(len(data) for data in payloads)
    # 上限读配置（默认不限）：发版和用户上传必须是同一套取值，否则会出现
    # "用户能传、管理员传不了"这种谁都不信的状态。
    archive_limit = skills_lib.max_archive_bytes()
    if archive_limit and total > archive_limit:
        raise HTTPException(
            status_code=413,
            detail=f'技能包超过 {archive_limit // (1024 * 1024)} MB 上限')
    if total == 0:
        raise HTTPException(status_code=400, detail='文件是空的')

    filename = Path(str(uploads[0].filename or 'skill.zip')).name
    lower = filename.lower()
    ext = next((e for e in _PACKAGE_EXTS if lower.endswith(e)), '')
    if len(uploads) > 1 or not ext:
        rel = _parse_paths(paths, len(uploads))
        payload = _repack(uploads, payloads, rel)
        filename, ext = 'skill.zip', '.zip'
        original_name = ''
    else:
        payload = payloads[0]
        # 单个包才留原名；散装文件夹是服务端压的，`skill.zip` 没有意义。
        original_name = filename

    try:
        # 与安装时同一套解析：这里放不过的包，绝不该显示"上传成功"。
        info = skills_lib.inspect_archive(
            filename, payload,
            fallback_description=str(rows[skill_id].get('description') or ''))
    except skills_lib.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    version = re.sub(r'[^0-9A-Za-z._-]', '-',
                     str(rows[skill_id].get('version') or '1.0.0')) or '1.0.0'
    packages_root = Path(settings.skillhub_packages_dir)
    target_dir = _package_dir(skill_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f'{version}{ext}'
    tmp = target_dir / f'.{target.name}.tmp'
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    # 同一技能只留一份包：换了版本/格式之后，旧包留着只会让"装的是哪份"变模糊。
    for stale in target_dir.iterdir():
        if stale.name != target.name and stale.is_file():
            stale.unlink(missing_ok=True)

    # 记相对包仓库的路径，不记绝对路径：换机器/换目录之后这份 JSON 才不会
    # 指向一堆不存在的文件（见 `platform_catalog.package_path`）。
    digest = skills_lib.package_digest(payload)
    stored = _catalog_guard(platform_catalog.set_skill_package, skill_id,
                            package=target.relative_to(packages_root).as_posix(),
                            version=version,
                            files=info['file_count'], size=len(payload),
                            filename=original_name, digest=digest,
                            actor=principal.user_id)
    # 已经装过这个技能的人，不等他重装：上传即覆盖同步。
    # 以前这里是"只存不解"，换包之后用户那份永远是旧文件——详情页显示
    # "技能包内容 · 1 个文件"就是这么来的。同步失败不算上传失败（包已经存好了），
    # 返回值里如实带上状态，管理端可以点「重新同步」重试。
    synced = None
    try:
        synced = skills_lib.sync_installed(
            filename, payload, skill_id=skill_id, digest=digest, version=version,
            fallback_description=str(rows[skill_id].get('description') or ''))
    except (skills_lib.SkillError, OSError) as exc:
        synced = {'error': str(exc)}
    return _ok({
        'id': skill_id,
        'package': {
            'filename': target.name, 'original_name': original_name,
            'version': version, 'bytes': len(payload), 'digest': digest,
            'files': info['file_count'], 'file_list': info['files'][:50],
            'name': info['name'], 'description': info['description'],
        },
        'row': stored,
        'sync': synced,
    })


@router.get('/platform/skills/{skill_id}/package')
async def console_platform_package(skill_id: str,
                                   principal=Depends(policies.current_principal)):
    """技能包的文件清单（相对技能根），给控制台的「查看包内容」用。

    只读：整包读进内存、走与安装完全相同的解析，所以这里列出来的文件
    就是用户安装后会落地的文件——不是另算一份。
    """
    from webpage_agent.addons import skills as skills_lib
    from webpage_agent.store import platform_catalog

    require_all(principal)
    skill_id = str(skill_id or '').strip().lower()
    rows = {row['id']: row for row in platform_catalog.load()['skills']}
    row = rows.get(skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail='条目不存在')
    path = platform_catalog.package_path(row.get('package'))
    if path is None:
        # `missing=True`：目录里记着包、文件却没了（换机器、清目录）。
        # 前端据此提示"重新上传"，而不是假装这个技能本来就只有正文。
        return _ok({'id': skill_id, 'package': None,
                    'missing': bool(row.get('package'))})
    try:
        listing = skills_lib.archive_entries(path.name, path.read_bytes())
    except (OSError, skills_lib.SkillError) as exc:
        raise HTTPException(status_code=400,
                            detail=f'技能包读不出来：{exc}') from None
    return _ok({'id': skill_id, 'missing': False, 'package': {
        'filename': path.name,
        'original_name': str(row.get('package_name') or ''),
        'bytes': path.stat().st_size,
        'files': len(listing['entries']),
        'root': listing['root'],
        'entries': listing['entries'],
    }})


@router.get('/platform/skills/{skill_id}/package/file')
async def console_platform_package_file(
        skill_id: str,
        path: str = Query(default='', max_length=400),
        principal=Depends(policies.current_principal)):
    """包内单个文件的内容（文本预览，256 KB 上限）。"""
    from webpage_agent.addons import skills as skills_lib
    from webpage_agent.store import platform_catalog

    require_all(principal)
    skill_id = str(skill_id or '').strip().lower()
    rows = {row['id']: row for row in platform_catalog.load()['skills']}
    row = rows.get(skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail='条目不存在')
    package = platform_catalog.package_path(row.get('package'))
    if package is None:
        raise HTTPException(status_code=404, detail='这个技能没有技能包')
    try:
        info = skills_lib.archive_member(package.name, package.read_bytes(), path)
    except skills_lib.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OSError as exc:
        raise HTTPException(status_code=400,
                            detail=f'技能包读不出来：{exc}') from None
    return _ok({'id': skill_id, 'file': info})


@router.post('/platform/skills/{skill_id}/sync')
async def console_sync_platform_package(skill_id: str, request: Request,
                                        principal=Depends(policies.current_principal)):
    """把当前技能包重新解到已装目录（管理端的手动兜底）。

    上传时已经自动同步过一次；这个入口兜的是"同步失败 / 目录被改坏 / 包是从
    别的机器拷过来的"——给一个可以自己按的重试按钮，而不是让人去删用户目录。
    """
    from webpage_agent.addons import skills as skills_lib
    from webpage_agent.store import platform_catalog

    require_all(principal)
    require_trusted_origin(request)
    skill_id = str(skill_id or '').strip().lower()
    rows = {row['id']: row for row in platform_catalog.load()['skills']}
    row = rows.get(skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail='条目不存在')
    path = platform_catalog.package_path(row.get('package'))
    if path is None:
        raise HTTPException(status_code=400, detail='这个技能没有可用的技能包，先上传')
    # 没装过的人不该被"同步"出目录来——那等于绕过安装、让所有人凭空多一个技能。
    if not (skills_lib.root() / skill_id).is_dir():
        return _ok({'id': skill_id, 'installed': False, 'synced': None})
    try:
        synced = skills_lib.sync_installed(
            path.name, path.read_bytes(), skill_id=skill_id,
            digest=str(row.get('package_digest') or ''),
            version=str(row.get('package_version') or ''),
            fallback_description=str(row.get('description') or ''))
    except (OSError, skills_lib.SkillError) as exc:
        raise HTTPException(status_code=400, detail=f'同步失败：{exc}') from None
    return _ok({'id': skill_id, 'installed': True, 'synced': synced})


@router.delete('/platform/skills/{skill_id}/package')
async def console_delete_platform_package(skill_id: str, request: Request,
                                          principal=Depends(policies.current_principal)):
    from webpage_agent.store import platform_catalog

    require_all(principal)
    require_trusted_origin(request)
    skill_id = str(skill_id or '').strip().lower()
    directory = _package_dir(skill_id)
    shutil.rmtree(directory, ignore_errors=True)
    return _ok(_catalog_guard(platform_catalog.clear_skill_package, skill_id,
                              actor=principal.user_id))


@router.patch('/platform/skills/{skill_id}')
async def console_toggle_platform_skill(skill_id: str, payload: PolicyToggleRequest,
                                        request: Request,
                                        principal=Depends(policies.current_principal)):
    from webpage_agent.store import platform_catalog

    require_all(principal)
    require_trusted_origin(request)
    return _ok(_catalog_guard(platform_catalog.set_enabled, 'skills', skill_id,
                              payload.enabled, actor=principal.user_id))


@router.delete('/platform/skills/{skill_id}')
async def console_delete_platform_skill(skill_id: str, request: Request,
                                        principal=Depends(policies.current_principal)):
    from webpage_agent.store import platform_catalog

    require_all(principal)
    require_trusted_origin(request)
    result = _catalog_guard(platform_catalog.remove, 'skills', skill_id)
    # 目录条目删了，包留着就是垃圾：下次同名发布很容易装到上一版的文件。
    try:
        shutil.rmtree(_package_dir(str(skill_id).strip().lower()), ignore_errors=True)
    except HTTPException:
        pass
    return _ok(result)


@router.get('/policies')
async def console_policies(principal=Depends(policies.current_principal),
                           store=Depends(store_of)):
    require_all(principal)
    return _ok(await store.console_policies())


@router.patch('/policies/{policy_id}')
async def console_toggle_policy(policy_id: str, payload: PolicyToggleRequest,
                                request: Request,
                                principal=Depends(policies.current_principal),
                                store=Depends(store_of)):
    require_all(principal)
    require_trusted_origin(request)
    try:
        row = await store.set_policy_enabled(
            policy_id=policy_id, enabled=payload.enabled, actor_id=principal.user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _ok(row)


# ---------- 登录平台 ----------
# "中台支持哪些平台的账号密码登录"从环境变量搬到了 `auth_platforms` 表，
# 这里给它一套增删改查。删除/停用有守门：不能把最后一个启用的平台关掉，
# 否则没人能登录——而登录是进控制台的前提。

class PlatformCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    platform_id: str = Field(min_length=2, max_length=48)
    display_name: str = Field(default='', max_length=80)
    login_mode: Literal['password_http'] = 'password_http'
    base_url: str = Field(default='', max_length=300)
    tenant_id: str = Field(default='default', max_length=80)
    identity_provider_id: str = Field(default='', max_length=80)
    enabled: bool = True
    is_default: bool = False
    note: str = Field(default='', max_length=200)
    config: dict | None = None


class PlatformUpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: str | None = Field(default=None, max_length=80)
    base_url: str | None = Field(default=None, max_length=300)
    tenant_id: str | None = Field(default=None, max_length=80)
    identity_provider_id: str | None = Field(default=None, max_length=80)
    enabled: bool | None = None
    is_default: bool | None = None
    note: str | None = Field(default=None, max_length=200)
    config: dict | None = None


class PlatformTestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(default='', max_length=128)
    password: str = Field(default='', max_length=256)


def _check_base_url(raw: str) -> str:
    """落库前就校验地址：填错了要当场报，不能等到用户登录时才发现。

    规则和登录期完全一致（`platform_login.validate_base_url`），避免"存得进去
    但登录时被拒"这种自相矛盾的状态。
    """
    from webpage_agent.api import platform_login as service

    try:
        return service.validate_base_url(raw)
    except service.PlatformLoginError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from None


@router.get('/platforms')
async def console_platforms(principal=Depends(policies.current_principal),
                            store=Depends(store_of)):
    """登录平台列表。只有超管能看：地址、租户映射都算内部配置。"""
    require_all(principal)
    return _ok(await store.platform_list())


@router.post('/platforms')
async def console_create_platform(payload: PlatformCreateRequest, request: Request,
                                  principal=Depends(policies.current_principal),
                                  store=Depends(store_of)):
    require_all(principal)
    require_trusted_origin(request)
    data = payload.model_dump()
    if data['base_url']:
        data['base_url'] = _check_base_url(data['base_url'])
    try:
        row = await store.platform_create(data, actor_id=principal.user_id)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ok(row)


@router.patch('/platforms/{platform_id}')
async def console_update_platform(platform_id: str, payload: PlatformUpdateRequest,
                                  request: Request,
                                  principal=Depends(policies.current_principal),
                                  store=Depends(store_of)):
    require_all(principal)
    require_trusted_origin(request)
    patch = {key: value for key, value in payload.model_dump().items()
             if value is not None}
    if patch.get('base_url'):
        patch['base_url'] = _check_base_url(patch['base_url'])
    try:
        row = await store.platform_update(
            platform_id, patch, actor_id=principal.user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ok(row)


@router.delete('/platforms/{platform_id}')
async def console_delete_platform(platform_id: str, request: Request,
                                  principal=Depends(policies.current_principal),
                                  store=Depends(store_of)):
    require_all(principal)
    require_trusted_origin(request)
    try:
        row = await store.platform_delete(platform_id, actor_id=principal.user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ok(row)


@router.post('/platforms/{platform_id}/test')
async def console_test_platform(platform_id: str, payload: PlatformTestRequest,
                                request: Request,
                                principal=Depends(policies.current_principal),
                                store=Depends(store_of)):
    """拿一组账号密码试一次，**不建会话、不写身份**。

    这是配置界面的"保存前先试一下"：平台地址填错、字段映射写错，都会在这一步
    暴露，而不是等真实用户登录失败。返回里不带平台原始响应——里面可能有 token。
    """
    require_all(principal)
    require_trusted_origin(request)
    from webpage_agent.api import platform_login as service

    record = await store.platform_get(platform_id)
    if record is None:
        raise HTTPException(status_code=404, detail='平台不存在')
    if not payload.username or not payload.password:
        # 没有凭据就只探可达性：能拿到 401/403 说明地址是通的，
        # 这类"预期内的拒绝"不能当失败报给用户。
        try:
            service.validate_base_url(record['base_url'])
        except service.PlatformLoginError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from None
        return _ok({'platform_id': platform_id, 'reachable': True,
                    'verified': False,
                    'detail': '地址合法。填入账号密码可以验证字段映射是否匹配'})
    started = time.monotonic()
    try:
        identity = await service.sign_in(
            store, platform_id=platform_id,
            username=payload.username, password=payload.password)
    except service.PlatformLoginError as exc:
        # 用 200 回一个"测试失败"而不是抛状态码：前端要展示具体原因，
        # 4xx 会被 fetch 封装当成请求错误、拿不到 detail。
        return _ok({'platform_id': platform_id, 'ok': False,
                    'detail': exc.detail, 'upstream_status': exc.status,
                    'elapsed_ms': int((time.monotonic() - started) * 1000)})
    return _ok({'platform_id': platform_id, 'ok': True,
                'verified': True,
                'subject': identity['subject'],
                'display_name': identity['display_name'],
                'role': identity['role'],
                'identity_provider_id': identity['identity_provider_id'],
                'tenant_id': identity['tenant_id'],
                'elapsed_ms': int((time.monotonic() - started) * 1000)})


# ---------- 审计 ----------

@router.get('/audit')
async def console_audit(limit: int = Query(100, ge=1, le=500), kind: str = '',
                        user: str = '',
                        principal=Depends(policies.current_principal),
                        store=Depends(store_of)):
    """管理动作。`user` 筛"针对某个人的动作"，已删除账号的记录仍查得到。"""
    require_all(principal)
    return _ok({'events': await store.console_audit(limit=limit, kind=kind,
                                                    user_id=user.strip())})


# ---------- 日志与运行态（docs/logging-and-observability.md） ----------
# 这一屏的读者是超管，用来回答两个问题：
#   1. 系统现在活着吗、跑得怎么样（QPS / P95 / 错误率 / 最近报错）；
#   2. 出问题的那一刻，上下文是什么（实时日志流 + trace 号）。
#
# 为什么只给 platform_admin：日志里有 tenant_id、user_id、连接器名，是跨租户的
# 东西。operator（本企业运维）看到别家企业的日志等于越权，所以这里用 require_all，
# 不跟 /users 那套"租户内可见"的规则走。

_LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
# 与 logging_setup.normalise_level 同一套排序。这里单独放一份是因为前端传上来的
# 可能是 WARN / ALL 这类写法，不能直接拿去 getattr。
_LEVEL_ORDER = {'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'WARN': 30,
                'ERROR': 40, 'CRITICAL': 50}

# 文本日志行的解析：dev 格式为 `时间 级别 logger trace=xxxx message`，
# 生产是 JSON。两者都要认，因为控制台可能连的是任意一种部署。
# 两条正则：`trace=` 是这次新加的列，存量日志文件里没有，得兼容着读。
_TEXT_LINE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+'
    r'(?P<level>[A-Z]+)\s+(?P<logger>[\w.]+)\s+trace=(?P<trace>\S+)\s?(?P<message>.*)$')
_LEGACY_LINE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+'
    r'(?P<level>[A-Z]+)\s+(?P<logger>[\w.]+)\s+(?P<message>.*)$')


def _log_files() -> list:
    """日志目录下的文件清单（含轮转出来的 .1/.2）。最新的在前。"""
    directory = logging_setup.log_dir()
    if not directory.is_dir():
        return []
    rows = []
    for path in directory.glob('*.log*'):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append({'name': path.name, 'size': stat.st_size,
                     'mtime': datetime.fromtimestamp(
                         stat.st_mtime, timezone.utc).isoformat()})
    rows.sort(key=lambda item: item['mtime'], reverse=True)
    return rows


def _read_tail(path, max_bytes: int = 512 * 1024, limit: int = 200) -> list:
    """从文件尾部读一段。不读全文件：日志轮转到 10MB 时，读全量只为看最后 200 行
    是纯浪费，而且会把内存峰值顶上去。
    """
    try:
        size = path.stat().st_size
        with path.open('rb') as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                # 跳到中间会切在半行上，第一行必然是残片，直接丢掉。
                handle.readline()
            raw = handle.read()
    except OSError:
        return []
    return raw.decode('utf-8', errors='replace').splitlines()[-limit * 2:]


def _parse_lines(lines: list) -> list:
    """把日志行折成条目。多行栈（Traceback）并入上一条。"""
    entries: list = []
    for line in lines:
        if line.startswith('{'):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and 'level' in payload and 'message' in payload:
                entries.append({'ts': payload.get('time', ''),
                                'level': str(payload.get('level', 'INFO')).upper(),
                                'logger': payload.get('logger', ''),
                                'trace': payload.get('trace_id', ''),
                                'message': str(payload.get('message', ''))})
                continue
        match = _TEXT_LINE.match(line)
        if match:
            entries.append({'ts': match['ts'].replace(',', '.'),
                            'level': match['level'],
                            'logger': match['logger'],
                            'trace': '' if match['trace'] == '-' else match['trace'],
                            'message': match['message']})
            continue
        legacy = _LEGACY_LINE.match(line)
        if legacy:
            entries.append({'ts': legacy['ts'].replace(',', '.'),
                            'level': legacy['level'],
                            'logger': legacy['logger'], 'trace': '',
                            'message': legacy['message']})
        elif entries and line.strip():
            # 上一行的续行：栈、SQL、异常说明。截断到 20 行，避免一个巨型栈
            # 把一条日志撑成整页。
            body = entries[-1]['message']
            if body.count('\n') < 20:
                entries[-1]['message'] = body + '\n' + line
    return entries


def _level_floor(level: str) -> int:
    return _LEVEL_ORDER.get((level or '').strip().upper(), 0)


def _matches(entry: dict, floor: int, needle: str) -> bool:
    if _LEVEL_ORDER.get(str(entry.get('level', 'INFO')).upper(), 20) < floor:
        return False
    if not needle:
        return True
    haystack = ' '.join(str(entry.get(key, '')) for key in
                        ('message', 'logger', 'trace')).lower()
    return needle in haystack


@router.get('/logs')
async def console_logs(source: Literal['api', 'worker'] = 'api', level: str = '',
                       q: str = '', limit: int = Query(200, ge=1, le=1000),
                       principal=Depends(policies.current_principal)):
    """日志。

    `source=api` 读本进程的环形缓冲（实时、结构化）；`source=worker` 读
    `logs/worker.log` 尾部（另一进程，只能读文件）。两边的字段形状一样，
    页面不用写两套渲染。
    """
    require_all(principal)
    floor = _level_floor(level)
    needle = (q or '').strip().lower()
    if source == 'worker':
        path = logging_setup.log_dir() / 'worker.log'
        rows = [row for row in _parse_lines(_read_tail(path, limit=limit))
                if _matches(row, floor, needle)]
        rows = rows[-limit:][::-1]
        meta = {'source': 'worker', 'available': path.exists(),
                'file': str(path)}
    else:
        rows = log_buffer_module.BUFFER.entries(level=level, query=q, limit=limit)
        meta = {'source': 'api', 'available': True, 'file': None}
    return _ok({**meta, 'level': level or 'ALL', 'count': len(rows),
                'entries': rows, 'files': _log_files()})


@router.get('/logs/status')
async def console_logs_status(principal=Depends(policies.current_principal)):
    """一屏运行态：进程、日志通道、请求指标。页面 5 秒刷一次。"""
    require_all(principal)
    stats = log_buffer_module.BUFFER.stats()
    return _ok({
        'process': log_buffer_module.process_snapshot(),
        'log': {**stats, 'level': logging_setup.current_level(),
                'levels': list(_LOG_LEVELS),
                'file_enabled': logging_setup.file_logging_enabled(),
                'dir': str(logging_setup.log_dir())},
        'http': log_buffer_module.METRICS.snapshot(),
        'files': _log_files(),
    })


@router.get('/logs/stream')
async def console_logs_stream(request: Request, level: str = '', q: str = '',
                              principal=Depends(policies.current_principal)):
    """实时日志（SSE）。

    用 SSE 而不是 WebSocket：只需要服务端单向推，SSE 自带断线重连、走普通 HTTP、
    还能穿过大多数反代；WebSocket 在这一层要多配一条 upgrade 规则。

    15 秒一次注释行心跳：反代和浏览器都会掐长期静默的连接，心跳让"没有日志"
    和"连接断了"在页面上能区分开。
    """
    require_all(principal)
    floor = _level_floor(level)
    needle = (q or '').strip().lower()

    async def generate():
        # retry 让 EventSource 断线后 3 秒重连，默认是 3 秒左右但各家浏览器不同，
        # 写死更可预期。
        yield 'retry: 3000\n: connected\n\n'
        with log_buffer_module.BUFFER.subscribe() as queue:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ': ping\n\n'
                    continue
                if not _matches(entry, floor, needle):
                    continue
                yield f'data: {json.dumps(entry, ensure_ascii=False)}\n\n'

    return StreamingResponse(
        generate(), media_type='text/event-stream',
        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no',
                 'Connection': 'keep-alive'})


class LogLevelRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']


@router.post('/logs/level')
async def console_set_log_level(payload: LogLevelRequest, request: Request,
                                principal=Depends(policies.current_principal)):
    """临时改日志级别。

    这是**写操作**（虽然只在内存里），所以照本文件惯例查 Origin；也正因为
    它只改内存、重启即失效，才敢做成一个按钮 —— 改 `.env` 再重启在生产上
    往往比问题本身还慢。
    """
    require_all(principal)
    require_trusted_origin(request)
    try:
        applied = logging_setup.set_level(payload.level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.warning('日志级别已调整 level=%s actor=%s', applied, principal.user_id)
    return _ok({'level': applied})


# ---------- Bot 广场（docs/bot-plaza.md） ----------

# 广场是"平台发布、所有人可用"的产品位，所以它的写操作全在这一层：
# 1) 审核用户投稿（pending → published / rejected）；
# 2) 直接上架官方 Bot（新建即 published，可设 public 跨企业可见）；
# 3) 运营位：推荐（featured）、排序（position）、下架（offline）。
#
# 两级权限的边界（和本文件其它视图一致）：
# - `platform_admin`（scope=all）能看、能改全部条目，也能把条目设成 public；
# - `operator`（scope=tenant）只能管**本企业**投的稿，不能跨企业上架——
#   否则"本企业运维"就能把自己公司的机器人推到所有客户的广场首页。

from webpage_agent.bot import plaza as plaza_domain

PLAZA_SCOPES = plaza_domain.SCOPES
PLAZA_STATUSES = plaza_domain.STATUSES
PLAZA_CATEGORIES = plaza_domain.CATEGORIES


class PlazaItemRequest(BaseModel):
    """新建/整体更新一个广场条目。字段与 `bots` 的人设字段保持同名，便于相互复制。"""

    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    handle: str = Field(default='', max_length=32)
    title: str = Field(default='', max_length=60)
    description: str = Field(default='', max_length=400)
    soul: str = Field(default='', max_length=12_000)
    model: str = Field(default='', max_length=120)
    skill_ids: list[str] = Field(default_factory=list, max_length=50)
    connector_ids: list[str] = Field(default_factory=list, max_length=50)
    icon: str = Field(default='🤖', max_length=16)
    color: str = Field(default='#eef4ff', max_length=32)
    shape: str = Field(default='circle', max_length=16)
    image: str = Field(default='', max_length=200_000)
    category: str = Field(default='general', max_length=24)
    tags: list[str] = Field(default_factory=list, max_length=8)
    readme: str = Field(default='', max_length=20_000)
    starters: list[str] = Field(default_factory=list, max_length=6)
    scope: str = Field(default='tenant', max_length=12)
    status: str = Field(default='published', max_length=12)
    featured: bool = False
    position: int = Field(default=0, ge=0, le=9999)
    source_bot_id: str = Field(default='', max_length=64)


class PlazaPatchRequest(BaseModel):
    """部分更新。审核动作也走这里（改 status + review_note）。"""

    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=40)
    handle: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=400)
    soul: str | None = Field(default=None, max_length=12_000)
    model: str | None = Field(default=None, max_length=120)
    skill_ids: list[str] | None = Field(default=None, max_length=50)
    connector_ids: list[str] | None = Field(default=None, max_length=50)
    icon: str | None = Field(default=None, max_length=16)
    color: str | None = Field(default=None, max_length=32)
    shape: str | None = Field(default=None, max_length=16)
    image: str | None = Field(default=None, max_length=200_000)
    category: str | None = Field(default=None, max_length=24)
    tags: list[str] | None = Field(default=None, max_length=8)
    readme: str | None = Field(default=None, max_length=20_000)
    starters: list[str] | None = Field(default=None, max_length=6)
    scope: str | None = Field(default=None, max_length=12)
    status: str | None = Field(default=None, max_length=12)
    featured: bool | None = None
    position: int | None = Field(default=None, ge=0, le=9999)
    review_note: str | None = Field(default=None, max_length=300)


def _plaza_guard(principal, item: dict | None) -> None:
    """operator 只能碰本企业的条目。不存在与越权都回 404（不泄露别人有什么）。"""
    scope = require_console(principal)
    if item is None:
        raise HTTPException(status_code=404, detail='条目不存在')
    if scope == ACCESS_ALL:
        return
    if str(item.get('publisher_tenant') or '') != str(principal.tenant_id or ''):
        raise HTTPException(status_code=404, detail='条目不存在')


def _plaza_payload(payload: PlazaItemRequest, *, principal) -> dict:
    """把请求体收敛成仓储字段：分类/范围/状态白名单，公开范围只有超管能给。"""
    category = payload.category if payload.category in dict(PLAZA_CATEGORIES) \
        else 'general'
    status = payload.status if payload.status in PLAZA_STATUSES else 'published'
    scope = payload.scope if payload.scope in PLAZA_SCOPES else 'tenant'
    if scope == 'public' and require_console(principal) != ACCESS_ALL:
        raise HTTPException(status_code=403, detail='只有平台超管能让条目跨企业可见')
    return {
        'name': payload.name.strip(),
        'handle': payload.handle.strip(),
        'title': payload.title.strip(),
        'description': payload.description.strip(),
        'soul': payload.soul.strip(),
        'model': payload.model.strip(),
        'skill_ids': list(payload.skill_ids),
        'connector_ids': list(payload.connector_ids),
        'icon': payload.icon or '🤖',
        'color': payload.color or '#eef4ff',
        'shape': payload.shape or 'circle',
        'image': payload.image,
        'category': category,
        'tags': [str(tag).strip()[:20] for tag in payload.tags if str(tag).strip()][:8],
        'readme': payload.readme.strip(),
        'starters': [str(s).strip()[:200] for s in payload.starters
                     if str(s).strip()][:6],
        'scope': scope,
        'status': status,
        'featured': 1 if payload.featured else 0,
        'position': int(payload.position or 0),
        'source_bot_id': payload.source_bot_id.strip(),
        'publisher_tenant': str(principal.tenant_id or 'default'),
        'publisher_user_id': str(principal.user_id or 'default'),
    }


@router.get('/plaza')
async def console_plaza_list(status: str = Query('', max_length=24),
                             q: str = Query('', max_length=80),
                             category: str = Query('', max_length=24),
                             principal=Depends(policies.current_principal),
                             store=Depends(store_of)):
    """全部条目（operator 只看本企业），带各状态计数与"实际安装数"对账列。"""
    scope = require_console(principal)
    statuses = (status,) if status in PLAZA_STATUSES else ()
    publisher_tenant = None if scope == ACCESS_ALL else str(principal.tenant_id or '')
    items = await store.list_plaza_items(statuses=statuses, category=category,
                                         q=q.strip(), publisher_tenant=publisher_tenant,
                                         order='new', limit=200)
    counts: dict[str, int] = {key: 0 for key in PLAZA_STATUSES}
    for row in await store.list_plaza_items(statuses=(), publisher_tenant=publisher_tenant,
                                            limit=200):
        key = str(row.get('status') or '')
        counts[key] = counts.get(key, 0) + 1
    actual = await store.plaza_install_counts(
        [str(item.get('item_id') or '') for item in items])
    return _ok({
        'items': [{
            'item_id': item.get('item_id'),
            'slug': item.get('slug') or '',
            'name': item.get('name') or '',
            'handle': item.get('handle') or '',
            'title': item.get('title') or '',
            'description': item.get('description') or '',
            'icon': item.get('icon') or '🤖',
            'color': item.get('color') or '#eef4ff',
            'category': item.get('category') or 'general',
            'tags': list(item.get('tags') or []),
            'scope': item.get('scope') or 'tenant',
            'status': item.get('status') or '',
            'featured': bool(item.get('featured')),
            'position': int(item.get('position') or 0),
            'install_count': int(item.get('install_count') or 0),
            'install_rows': int(actual.get(str(item.get('item_id') or '')) or 0),
            'view_count': int(item.get('view_count') or 0),
            'publisher_tenant': item.get('publisher_tenant') or '',
            'publisher_user_id': item.get('publisher_user_id') or '',
            'source_bot_id': item.get('source_bot_id') or '',
            'review_note': item.get('review_note') or '',
            'reviewed_by': item.get('reviewed_by') or '',
            'reviewed_at': item.get('reviewed_at') or '',
            'version': int(item.get('version') or 1),
            'created_at': item.get('created_at') or '',
            'updated_at': item.get('updated_at') or '',
        } for item in items],
        'counts': counts,
        'total': len(items),
        'categories': [{'id': key, 'name': name} for key, name in PLAZA_CATEGORIES],
        'scopes': list(PLAZA_SCOPES),
        'statuses': list(PLAZA_STATUSES),
    })


@router.get('/plaza/{item_id}')
async def console_plaza_detail(item_id: str,
                               principal=Depends(policies.current_principal),
                               store=Depends(store_of)):
    item = await store.plaza_item(item_id)
    _plaza_guard(principal, item)
    detail = dict(item or {})
    detail['install_rows'] = (await store.plaza_install_counts([item_id])).get(item_id, 0)
    return _ok(detail)


@router.post('/plaza')
async def console_plaza_create(payload: PlazaItemRequest, request: Request,
                               principal=Depends(policies.current_principal),
                               store=Depends(store_of)):
    """直接上架一条官方 Bot。

    给了 `source_bot_id` 且请求体没写人设时，从那个机器人复制一份快照——运营
    最常做的事是"把这个跑得不错的机器人放到广场"，而不是重新手打一遍人设。
    """
    require_console(principal)
    require_trusted_origin(request)
    fields = _plaza_payload(payload, principal=principal)
    if fields['source_bot_id'] and not fields['soul']:
        if require_console(principal) != ACCESS_ALL:
            raise HTTPException(status_code=403,
                                detail='从任意机器人复制只对平台超管开放')
        bot = await store.bot_row(fields['source_bot_id'])
        if bot is None:
            raise HTTPException(status_code=404, detail='来源机器人不存在')
        for key in ('name', 'handle', 'title', 'description', 'soul', 'model',
                    'skill_ids', 'connector_ids', 'icon', 'color', 'shape', 'image'):
            if not fields.get(key):
                value = bot.get(key)
                fields[key] = list(value or []) if key.endswith('_ids') else (value or '')
        fields['name'] = fields['name'] or '未命名 Bot'
    slug = bot_prompts.normalize_handle(fields['handle'] or fields['name']) or 'bot'
    suffix = 2
    candidate = slug
    while await store.plaza_item_by_handle(candidate) is not None:
        candidate = f'{slug[:34]}-{suffix}'
        suffix += 1
    stamp = datetime.now(timezone.utc).isoformat()
    if fields['status'] == 'published':
        fields['reviewed_by'] = str(principal.user_id or '')
        fields['reviewed_at'] = stamp
        fields['review_note'] = fields.get('review_note') or '官方上架'
    item = await store.create_plaza_item(
        item_id='plz_' + uuid.uuid4().hex[:12], created_at=stamp, updated_at=stamp,
        slug=candidate, version=1, **fields)
    return _ok(item)


@router.patch('/plaza/{item_id}')
async def console_plaza_patch(item_id: str, payload: PlazaPatchRequest, request: Request,
                              principal=Depends(policies.current_principal),
                              store=Depends(store_of)):
    """改内容，或做审核动作（`status` + `review_note`）。"""
    req_scope = require_console(principal)
    require_trusted_origin(request)
    item = await store.plaza_item(item_id)
    _plaza_guard(principal, item)
    fields = payload.model_dump(exclude_unset=True)
    if 'status' in fields and fields['status'] not in PLAZA_STATUSES:
        raise HTTPException(status_code=400, detail='status 不在白名单里')
    if 'scope' in fields:
        if fields['scope'] not in PLAZA_SCOPES:
            raise HTTPException(status_code=400, detail='scope 只能是 tenant / public')
        if fields['scope'] == 'public' and req_scope != ACCESS_ALL:
            raise HTTPException(status_code=403, detail='只有平台超管能让条目跨企业可见')
    if 'category' in fields and fields['category'] not in dict(PLAZA_CATEGORIES):
        fields['category'] = 'general'
    if 'status' in fields:
        fields.setdefault('reviewed_by', str(principal.user_id or ''))
        fields['reviewed_at'] = datetime.now(timezone.utc).isoformat()
    updated = await store.update_plaza_item(item_id, **fields)
    return _ok(updated or {})


@router.delete('/plaza/{item_id}')
async def console_plaza_delete(item_id: str, request: Request,
                               principal=Depends(policies.current_principal),
                               store=Depends(store_of)):
    """硬删条目。已经装过的副本不受影响——它们是用户自己的机器人了。"""
    require_console(principal)
    require_trusted_origin(request)
    item = await store.plaza_item(item_id)
    _plaza_guard(principal, item)
    return _ok({'item_id': item_id, 'deleted': await store.delete_plaza_item(item_id)})
