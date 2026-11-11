"""连接器 API：把 MCP Server 的启停做成前端可操作的「开关」。

- `GET /api/connectors`：已配置的 MCP Server + 目录里未配置的连接器
- `POST /api/connectors/{id}/connect`：真实建立 MCP 会话并列出工具
- `POST /api/connectors/{id}/disconnect`：关闭开关（保留配置）
- `GET /api/connectors/{id}/tools`：最近一次连接发现的工具
- `POST /api/connectors/{id}/oauth/start`：拿授权页地址（需要走 OAuth 的连接器）
- `GET /api/connectors/{id}/oauth/callback`：授权服务器把我们带回来，换 token
- `DELETE /api/connectors/{id}/oauth`：撤销本主体的授权

连接状态按主体保存在 `profile/skillhub/connectors/`。

## 授权回调为什么要单独一套

`/connect` 是前端点的，带 Cookie；`/oauth/callback` 是**浏览器从对方站点跳回来**
的，SameSite 可能把 Cookie 拦掉，且发起方是对方站点。所以回调不依赖登录态，
而是依赖一次性 `state`：它同时承载"是谁在授权"、PKCE verifier 和过期时间。
state 用完即删，重放拿不到第二次。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from webpage_agent.core import credentials as credentials_module
from webpage_agent.core import oauth_store
from webpage_agent.core.config import settings
from webpage_agent.governance import policies
from webpage_agent.integrations import mcp_adapter, mcp_servers, oauth_client
from webpage_agent.skillhub import catalog

router = APIRouter(prefix='/api/connectors', tags=['connectors'])
_ROOT = Path(__file__).resolve().parents[3]
_STATE_DIR = _ROOT / 'profile' / 'skillhub' / 'connectors'
_OAUTH_STATE_DIR = _ROOT / 'profile' / 'skillhub' / 'oauth_state'


def _key(principal) -> str:
    tenant = str(getattr(principal, 'tenant_id', 'default') or 'default')
    user = str(getattr(principal, 'user_id', 'default') or 'default')
    return re.sub(r'[^0-9A-Za-z_.-]', '_', f'{tenant}__{user}')[:120] or 'default'


def _ids(principal) -> tuple[str, str]:
    return (str(getattr(principal, 'tenant_id', 'default') or 'default'),
            str(getattr(principal, 'user_id', 'default') or 'default'))


def _load(principal) -> dict:
    path = _STATE_DIR / f'{_key(principal)}.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _save(principal, data: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _STATE_DIR / f'{_key(principal)}.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _reset_tool_cache() -> None:
    """连接/授权状态变了，让下一轮工具发现立刻重来。

    缓存 TTL 是 300s：不主动失效的话，刚授完权、刚点完连接，会话里仍然拿的是
    上一次"这个服务端 0 个工具"的目录，用户会以为授权没生效。
    """
    try:
        from webpage_agent.tools import core as tools_core
        tools_core.reset_cache()
    except Exception:                                # noqa: BLE001
        pass


def _server_view(server, state: dict, *, tenant: str = '', user: str = '') -> dict:
    row = state.get(server.id) or {}
    auth_mode = str(getattr(server, 'auth', '') or '')
    oauth_ready = True
    if auth_mode == 'oauth' and tenant:
        oauth_ready = oauth_store.has_token(tenant_id=tenant, user_id=user,
                                            server_id=server.id)
    # 面板上"连接过"不等于"这轮真拿到工具"：把最近一次工具发现的失败原因
    # 也带出来，否则用户只能看到"已连接"和一个只会用三个本地工具的模型。
    runtime_error = ''
    try:
        from webpage_agent.tools import core as tools_core
        runtime_error = str(
            (tools_core.discovery_errors() or {}).get(server.id) or '')
    except Exception:                                # noqa: BLE001
        runtime_error = ''
    return {
        'id': server.id,
        'name': server.title or server.id,
        'transport': server.transport,
        'auth': auth_mode,
        'configured': True,
        'authorized': oauth_ready,
        'need_authorize': auth_mode == 'oauth' and not oauth_ready,
        'enabled': bool(row.get('enabled')),
        'tools': int(row.get('tools') or 0),
        'tool_names': list(row.get('tool_names') or []),
        'error': str(row.get('error') or runtime_error),
        'at': str(row.get('at') or ''),
    }


@router.get('')
async def list_connectors(principal=Depends(policies.current_principal)):
    state = _load(principal)
    tenant, user = _ids(principal)
    servers = mcp_servers.servers()
    configured = {server.id for server in servers}
    items = [_server_view(server, state, tenant=tenant, user=user)
             for server in servers]
    for item in catalog.CONNECTORS:
        if item.get('id') in configured:
            continue
        items.append({
            'id': item['id'], 'name': item.get('name') or item['id'],
            'transport': '', 'auth': '', 'configured': False, 'authorized': False,
            'need_authorize': False, 'enabled': False,
            'tools': 0, 'tool_names': [], 'error': '',
            'icon': item.get('icon') or '🔗', 'category': item.get('category') or '',
        })
    return {'items': items, 'configured': len(servers),
            'enabled': sum(1 for x in items if x.get('enabled'))}


# ---------------------------------------------------------------- OAuth 辅助

def _base_url(request: Request) -> str:
    """回调地址前缀。

    生产环境必须配 `OAUTH_PUBLIC_BASE`：反代之后 `request.base_url` 拿到的
    常是内网 http 地址，和动态注册时报给对方的 https 地址不一致，对方会拒。
    """
    configured = str(getattr(settings, 'oauth_public_base', '') or '').strip()
    if configured:
        return configured.rstrip('/')
    return str(request.base_url).rstrip('/')


def _callback_url(server, request: Request) -> str:
    path = str(getattr(server, 'oauth_redirect_path', '') or '').strip()
    if not path:
        path = f'/api/connectors/{server.id}/oauth/callback'
    if not path.startswith('/'):
        path = '/' + path
    return oauth_client.validate_redirect_uri(_base_url(request) + path)


def _state_slug(value: str) -> str:
    return re.sub(r'[^0-9A-Za-z_-]', '', str(value or ''))[:160]


def _save_state(state: str, payload: dict) -> None:
    _OAUTH_STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _OAUTH_STATE_DIR / f'{_state_slug(state)}.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:                                  # pragma: no cover - 非 POSIX
        pass


def _take_state(state: str) -> dict | None:
    """读并**立刻删除** state：授权码一次性，state 也必须一次性。"""
    slug = _state_slug(state)
    if not slug:
        return None
    path = _OAUTH_STATE_DIR / f'{slug}.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else None


def _client_credentials(server, existing: dict) -> tuple[str, str]:
    """应用级凭据：优先用上次动态注册的结果，其次用配置里的预注册值。"""
    client_id = str(existing.get('client_id') or getattr(server, 'oauth_client_id', '') or '')
    secret = str(existing.get('client_secret') or '')
    ref = str(getattr(server, 'oauth_client_secret_ref', '') or '').strip()
    if not secret and ref:
        try:
            secret = credentials_module.read_secret_ref(ref)
        except credentials_module.CredentialMissing:
            secret = ''
    return client_id, secret


async def _authorize_url(server, request: Request, principal) -> dict:
    tenant, user = _ids(principal)
    redirect_uri = _callback_url(server, request)
    issuer = str(getattr(server, 'oauth_issuer', '') or '').strip()
    meta = await oauth_client.discover(issuer)
    existing = oauth_store.load(tenant_id=tenant, user_id=user, server_id=server.id)
    client_id, client_secret = _client_credentials(server, existing)

    # 回调地址变了就必须重新注册：DCR 把 redirect_uri 写在注册结果里，
    # 换了地址还沿用旧 client_id，授权服务器会在回调阶段直接拒绝。
    if not client_id or (not getattr(server, 'oauth_client_id', '')
                         and existing.get('redirect_uri') != redirect_uri):
        registered = await oauth_client.register_client(
            meta, redirect_uri=redirect_uri,
            client_name=str(getattr(server, 'oauth_client_name', '') or '智能体中台'),
            scopes=tuple(getattr(server, 'oauth_scopes', ()) or ()))
        client_id = registered['client_id']
        client_secret = registered.get('client_secret') or ''
        existing = {**existing, **registered, 'redirect_uri': redirect_uri,
                    'issuer': issuer, 'service': getattr(server, 'url', '')}

    verifier, challenge = oauth_client.make_pkce()
    state = oauth_client.make_state()
    resource = (str(getattr(server, 'oauth_resource', '') or '')
                or str(getattr(server, 'url', '') or ''))
    if not resource:
        resource = await oauth_client.discover_resource(issuer)
    url = oauth_client.build_authorize_url(
        meta, client_id=client_id, redirect_uri=redirect_uri,
        scopes=tuple(getattr(server, 'oauth_scopes', ()) or ()),
        state=state, challenge=challenge, resource=resource)
    _save_state(state, {
        'state': state,
        'tenant_id': tenant,
        'user_id': user,
        'server_id': server.id,
        'verifier': verifier,
        'redirect_uri': redirect_uri,
        'resource': resource,
        'issuer': issuer,
        'client_id': client_id,
        'client_secret': client_secret,
        'created_at': int(time.time()),
    })
    return {'authorize_url': url, 'state': state,
            'expires_in': int(getattr(settings, 'oauth_state_ttl_seconds', 600))}


def _page(title: str, message: str, *, ok: bool, server_id: str = '') -> HTMLResponse:
    color = '#1D9E75' if ok else '#A32D2D'
    script = ''
    if ok:
        # 同源时通知 opener 立即刷新；跨源被拦也无所谓，前端还会按窗口关闭兜底。
        script = (
            '<script>try{window.opener&&window.opener.postMessage('
            '{"type":"wb-connector-authorized","id":' + json.dumps(server_id) +
            '},"*")}catch(e){}setTimeout(function(){window.close()},900)</script>'
        )
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8">'
        f'<title>{title}</title>'
        '<body style="font:15px/1.7 system-ui,-apple-system,sans-serif;'
        'padding:48px;max-width:560px;margin:0 auto;color:#2C2C2A">'
        f'<h2 style="color:{color};font-size:18px;margin:0 0 12px">{title}</h2>'
        f'<p style="color:#5F5E5A;margin:0">{message}</p>'
        f'{script}</body>',
        status_code=200 if ok else 400)


@router.post('/{connector_id}/oauth/start')
async def oauth_start(connector_id: str, request: Request,
                      principal=Depends(policies.current_principal)):
    """返回授权页地址。前端用新窗口打开，不要整页跳走。"""
    server = mcp_servers.by_id(connector_id)
    if server is None:
        raise HTTPException(status_code=404, detail='该连接器还没有配置 MCP_SERVERS')
    if str(getattr(server, 'auth', '') or '') != 'oauth':
        raise HTTPException(status_code=400,
                            detail='该连接器不使用 OAuth，直接点开关连接即可')
    try:
        return await _authorize_url(server, request, principal)
    except oauth_client.OAuthError as exc:
        raise HTTPException(status_code=502, detail=f'发起授权失败：{exc}') from None


@router.get('/{connector_id}/oauth/callback')
async def oauth_callback(connector_id: str, request: Request,
                         state: str = '', code: str = '', error: str = '',
                         error_description: str = ''):
    """授权服务器跳回来的落点。**不依赖登录 Cookie**，只认 state。"""
    row = _take_state(state)
    if not row:
        return _page('授权失败', 'state 无效或已过期，请回到连接器面板重新点「授权」。',
                     ok=False)
    if str(row.get('server_id') or '') != connector_id:
        return _page('授权失败', 'state 与连接器不匹配，请重新发起授权。', ok=False)
    if int(time.time()) - int(row.get('created_at') or 0) > int(
            getattr(settings, 'oauth_state_ttl_seconds', 600)):
        return _page('授权失败', '授权超时（超过 10 分钟），请重新点「授权」。', ok=False)
    if error:
        reason = f'{error} {error_description}'.strip()
        return _page('授权被拒绝', f'对方返回：{reason}', ok=False)
    if not code:
        return _page('授权失败', '回调里没有 code 参数。', ok=False)

    server = mcp_servers.by_id(connector_id)
    if server is None:
        return _page('授权失败', '该连接器已从配置里移除。', ok=False)

    tenant = str(row.get('tenant_id') or 'default')
    user = str(row.get('user_id') or 'default')
    issuer = str(row.get('issuer') or getattr(server, 'oauth_issuer', '') or '')
    try:
        meta = await oauth_client.discover(issuer)
        tokens = await oauth_client.exchange_code(
            meta, code=code, code_verifier=str(row.get('verifier') or ''),
            client_id=str(row.get('client_id') or ''),
            client_secret=str(row.get('client_secret') or ''),
            redirect_uri=str(row.get('redirect_uri') or ''),
            resource=str(row.get('resource') or ''))
    except oauth_client.OAuthError as exc:
        return _page('授权失败', f'换取 token 失败：{exc}', ok=False)

    data = {
        'client_id': str(row.get('client_id') or ''),
        'client_secret': str(row.get('client_secret') or ''),
        'redirect_uri': str(row.get('redirect_uri') or ''),
        'issuer': issuer,
        'service': str(getattr(server, 'url', '') or ''),
        'scope': tokens.get('scope') or ' '.join(
            str(x) for x in (getattr(server, 'oauth_scopes', ()) or ())),
        'tokens': tokens,
    }
    path = oauth_store.save(tenant_id=tenant, user_id=user,
                            server_id=server.id, data=data)

    store = getattr(request.app.state, 'store', None)
    if store is None:
        return _page('授权完成，但未写入绑定',
                     '凭据已保存到本机，但服务没有数据库连接，无法登记凭据绑定。'
                     '请配置 DATABASE_URL 后重新授权。', ok=False)

    try:
        binding = await store.upsert_server_binding(
            tenant_id=tenant, server_id=server.id, transport=server.transport)
        rows = await store.credential_bindings(
            tenant_id=tenant, server_binding_id=str(binding.get('binding_id') or ''))
        # 同一主体只留一条 active：重复授权是"换一把新钥匙"，不是新增一条。
        existing = next((r for r in rows
                         if str(r.get('subject_kind')) == 'user'
                         and str(r.get('subject_id')) == user
                         and str(r.get('status')) == 'active'), None)
        await store.upsert_credential_binding(
            tenant_id=tenant, subject_kind='user', subject_id=user,
            server_binding_id=str(binding.get('binding_id') or ''),
            credential_ref='file:' + str(path),
            # `platforms: ['*']` 不是放权，是"这个人的钥匙在任何平台都能用"：
            # 缺了这个键，带 platform_id 的运行会被 matches() 判成不适配。
            scope={'auth': 'oauth', 'platforms': ['*']},
            binding_id=str((existing or {}).get('binding_id') or ''))
        credentials_module.invalidate_cache()
    except Exception as exc:                          # noqa: BLE001
        return _page('授权完成，但登记失败',
                     f'凭据已保存，登记凭据绑定时出错：{type(exc).__name__}',
                     ok=False)

    # 授权完成 = 钥匙换了：下一轮必须重新发现，不能还吃 300s 的旧目录。
    _reset_tool_cache()
    return _page('授权完成', '可以关闭此页面，回到对话继续。', ok=True,
                 server_id=server.id)


@router.delete('/{connector_id}/oauth')
async def oauth_revoke(connector_id: str, request: Request,
                       principal=Depends(policies.current_principal)):
    """撤销本主体的授权：删凭据文件，并把绑定标成 inactive。"""
    server = mcp_servers.by_id(connector_id)
    if server is None:
        raise HTTPException(status_code=404, detail='连接器不存在')
    tenant, user = _ids(principal)
    oauth_store.clear(tenant_id=tenant, user_id=user, server_id=connector_id)
    store = getattr(request.app.state, 'store', None)
    if store is not None:
        binding = await store.server_binding(tenant, connector_id)
        if binding:
            rows = await store.credential_bindings(
                tenant_id=tenant,
                server_binding_id=str(binding.get('binding_id') or ''))
            for row in rows:
                if (str(row.get('subject_kind')) == 'user'
                        and str(row.get('subject_id')) == user
                        and str(row.get('status')) == 'active'):
                    await store.upsert_credential_binding(
                        tenant_id=tenant, subject_kind='user', subject_id=user,
                        server_binding_id=str(row.get('server_binding_id') or ''),
                        credential_ref=str(row.get('credential_ref') or ''),
                        scope=credentials_module.parse_scope(row.get('scope')),
                        revision=int(row.get('revision') or 1) + 1,
                        status='inactive',
                        binding_id=str(row.get('binding_id') or ''))
    credentials_module.invalidate_cache()
    _reset_tool_cache()
    return {'id': connector_id, 'authorized': False}


@router.post('/{connector_id}/connect')
async def connect_connector(connector_id: str,
                            principal=Depends(policies.current_principal)):
    server = mcp_servers.by_id(connector_id)
    if server is None:
        raise HTTPException(status_code=404,
                            detail='该连接器还没有配置 MCP_SERVERS，请先在 .env 添加服务端')
    state = _load(principal)
    token = None
    if str(getattr(server, 'auth', '') or '') == 'oauth':
        tenant, user = _ids(principal)
        try:
            token = await oauth_store.ensure_access_token(
                server=server, tenant_id=tenant, user_id=user,
                server_id=connector_id)
        except oauth_store.OAuthAuthorizationRequired as exc:
            # 428 Precondition Required：前端据此弹「授权」，不当作连接失败。
            raise HTTPException(status_code=428, detail=str(exc)) from None
        except oauth_client.OAuthError as exc:
            raise HTTPException(status_code=502,
                                detail=f'刷新授权失败：{exc}') from None
    try:
        # 连接器发现发生在 run 之外，但身份仍必须是已验证的登录主体。
        # BeeFTV 这类 stdio MCP 会用 MCP_CALLER_SUBJECT 校验画布归属。
        subject = str(getattr(principal, 'subject', '') or '')
        tools = await asyncio.wait_for(
            mcp_adapter.list_server_tools(server, token=token, subject=subject), 40)
    except Exception as exc:                            # noqa: BLE001
        message = f'{type(exc).__name__}: {exc}'[:300]
        state[connector_id] = {'enabled': False, 'tools': 0, 'tool_names': [],
                               'error': message, 'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
        _save(principal, state)
        _reset_tool_cache()
        raise HTTPException(status_code=502, detail=f'连接失败：{message}') from None
    names = [tool.get('name') for tool in tools[:80]]
    state[connector_id] = {'enabled': True, 'tools': len(tools), 'tool_names': names,
                           'error': '', 'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    _save(principal, state)
    _reset_tool_cache()
    return {'id': connector_id, 'enabled': True, 'tools': len(tools),
            'tool_names': names, 'name': server.title or server.id}


@router.post('/{connector_id}/disconnect')
async def disconnect_connector(connector_id: str,
                               principal=Depends(policies.current_principal)):
    if mcp_servers.by_id(connector_id) is None:
        raise HTTPException(status_code=404, detail='连接器不存在')
    state = _load(principal)
    previous = state.get(connector_id) or {}
    state[connector_id] = {**previous, 'enabled': False, 'error': '',
                           'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    _save(principal, state)
    _reset_tool_cache()
    return {'id': connector_id, 'enabled': False}


@router.get('/{connector_id}/tools')
async def connector_tools(connector_id: str,
                          principal=Depends(policies.current_principal)):
    row = _load(principal).get(connector_id) or {}
    return {'id': connector_id, 'enabled': bool(row.get('enabled')),
            'tools': int(row.get('tools') or 0),
            'tool_names': list(row.get('tool_names') or []),
            'error': str(row.get('error') or '')}
