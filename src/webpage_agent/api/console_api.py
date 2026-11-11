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

import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.core.config import settings
from webpage_agent.governance import policies

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
    # 让调用方把 user_id 原样回填一遍。删账号不可撤销，光一个"确定吗"的
    # confirm() 挡不住误点——尤其是表格行挨得近的时候。
    confirm: str = Field(min_length=1, max_length=120)
    reason: str = Field(default='', max_length=200)


@router.delete('/users/{user_id}')
async def console_delete_user(user_id: str, payload: DeleteRequest, request: Request,
                              principal=Depends(policies.current_principal),
                              store=Depends(store_of)):
    """删除账号。仅超管，且只能从跨租户视角删。

    租户运维（`operator`）不给这个能力：删账号会同时抹掉这个人在**所有**租户的
    身份绑定，超出"本企业运维"的边界。
    """
    require_all(principal)
    require_trusted_origin(request)
    if payload.confirm.strip() != user_id:
        raise HTTPException(status_code=400, detail='确认信息与目标账号不一致')
    try:
        row = await store.console_delete_user(
            user_id=user_id, actor_id=principal.user_id, reason=payload.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ok(row)


# ---------- 连接器与技能 ----------

@router.get('/connectors')
async def console_connectors(principal=Depends(policies.current_principal),
                             store=Depends(store_of)):
    require_all(principal)
    return _ok(await store.console_connectors())


@router.get('/policies')
async def console_policies(principal=Depends(policies.current_principal),
                           store=Depends(store_of)):
    require_all(principal)
    return _ok(await store.console_policies())


class PolicyToggleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


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
