"""中台身份提供方：把中台会话换成子平台可验证的 OIDC id_token。

## 它解决什么

改造前中台是"借"子平台的账号：登录页把密码 POST 给子平台校验，身份主数据在
别人那里。这个模块把方向翻过来——**中台签发身份，子平台验证身份**。

    | 段 | 谁做 | 职责 |
    |---|---|---|
    | 认证 | 中台（短信/密码/扫码） | 证明"你是你"，产出中台会话 |
    | 授权 | 本模块 `/authorize` | 确认"你同意把身份告诉这个子平台" |
    | 换票 | 本模块 `/token` | 发一次性授权码 → 签 RS256 id_token |
    | 验票 | 子平台的 OIDC 客户端 | 拉 discovery + JWKS，验签、验 nonce |

`sub` 直接用中台 `user_id`（`u_xxx`）。这是刻意的：它终身不变，而手机号会被
运营商回收、邮箱会换域名。子平台拿 `sub` 找到本地账号，拿不到就拒绝——**不做
按邮箱/手机号自动匹配**，那种"猜"错了就是数据串号。

## 三个安全边界

1. **`redirect_uri` 精确匹配白名单**，不做前缀、不做通配。校验不过就连错误都
   不往那边跳——否则本模块就是一个开放重定向器。
2. **授权码一次性**，`pop` 取出即删，重放一律 410。码是 32 字节 URL-safe 随机，
   不可枚举。
3. **强制 PKCE S256**。没有 `code_challenge` 直接拒绝，公开客户端也不放行。

## 已知限制

授权码存在进程内存里，**只适用于单进程部署**。多 worker 时请求可能落到没有该
码的进程上，表现为"登录偶发失败"。用户量到需要多进程时改成 Redis 或入库。
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from joserfc import jwt as jose_jwt

from webpage_agent.api import auth as auth_api
from webpage_agent.core import idp_keys
from webpage_agent.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# 固定路径。`idp_issuer` 的环境变量里路径部分必须与它一致，启动自检会校验。
_PATH = '/api/idp'


# ---------- 客户端注册表 ----------

_clients_cache: dict | None = None
_key_cache = None
_kid_cache: str | None = None
_cache_lock = threading.Lock()


class IdPConfigError(RuntimeError):
    """配置不可用。启动期抛，不要等到用户点登录才 500。"""


def _parse_clients(raw: str) -> dict:
    """解析 `IDP_CLIENTS`。任何结构性错误都直接失败，不做静默降级。

    静默跳过一条坏配置的后果是"某个子平台登不进来"，而日志里什么都看不到。
    """
    if not str(raw or '').strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise IdPConfigError(f'IDP_CLIENTS 不是合法 JSON：{exc}') from exc
    if not isinstance(parsed, dict):
        raise IdPConfigError('IDP_CLIENTS 必须是对象')

    clients: dict[str, dict] = {}
    for client_id, entry in parsed.items():
        if not isinstance(client_id, str) or not client_id.strip():
            raise IdPConfigError('IDP_CLIENTS 存在空的 client_id')
        if not isinstance(entry, dict):
            raise IdPConfigError(f'客户端 {client_id} 的配置必须是对象')
        uris = entry.get('redirect_uris')
        if (not isinstance(uris, list) or not uris
                or any(not isinstance(u, str) or not u.strip() for u in uris)):
            raise IdPConfigError(f'客户端 {client_id} 缺少有效的 redirect_uris')
        secret = entry.get('secret', '')
        if secret is not None and not isinstance(secret, str):
            raise IdPConfigError(f'客户端 {client_id} 的 secret 必须是字符串')
        clients[client_id] = {
            'secret': (secret or '').strip(),
            'redirect_uris': [u.strip() for u in uris],
        }
    return clients


def clients() -> dict:
    global _clients_cache
    with _cache_lock:
        if _clients_cache is None:
            _clients_cache = _parse_clients(settings.idp_clients_raw)
        return _clients_cache


def _key_and_kid():
    global _key_cache, _kid_cache
    with _cache_lock:
        if _key_cache is None or _kid_cache is None:
            _key_cache, _kid_cache = idp_keys.load_or_create(settings.idp_key_path)
        return _key_cache, _kid_cache


def reset_cache() -> None:
    """测试与配置热更新用：丢掉已解析的客户端与密钥句柄。"""
    global _clients_cache, _key_cache, _kid_cache
    with _cache_lock:
        _clients_cache = None
        _key_cache = None
        _kid_cache = None


# ---------- 启动自检 ----------

def startup_check() -> None:
    """启用时把配置问题挡在启动阶段。

    最值得挡的是 issuer 与路由路径不一致：那会让子平台的 discovery 请求打到
    404，而错误信息只会说"OIDC 服务发现失败"，排查方向完全跑偏。
    """
    if not settings.idp_enabled:
        return
    issuer = settings.idp_issuer
    if not issuer:
        raise IdPConfigError('IDP_ENABLED=true 时必须配置 IDP_ISSUER')
    parts = urlsplit(issuer)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise IdPConfigError(f'IDP_ISSUER 必须是完整 http(s) 地址：{issuer}')
    if parts.query or parts.fragment:
        raise IdPConfigError('IDP_ISSUER 不能带查询参数或锚点')
    if parts.path.rstrip('/') != _PATH:
        raise IdPConfigError(
            f'IDP_ISSUER 的路径必须是 {_PATH}，当前是 {parts.path or "/"}')
    if not clients():
        raise IdPConfigError('IDP_ENABLED=true 时 IDP_CLIENTS 不能为空')
    # 私钥在启动时就加载：等到第一个用户点登录才发现密钥不可写太晚了。
    _key_and_kid()
    logger.info('OIDC 身份提供方已启用 issuer=%s clients=%d',
                issuer, len(clients()))


# ---------- 授权码 ----------

# code -> {client_id, redirect_uri, code_challenge, nonce, user_id, tenant_id, role, exp}
_CODES: dict[str, dict] = {}
_codes_lock = threading.Lock()


def _purge_expired_codes(now: float) -> None:
    for code in [c for c, v in _CODES.items() if v['exp'] < now]:
        _CODES.pop(code, None)


def _put_code(payload: dict) -> str:
    code = secrets.token_urlsafe(32)
    with _codes_lock:
        now = time.time()
        _purge_expired_codes(now)
        _CODES[code] = {**payload, 'exp': now + settings.idp_code_ttl}
    return code


def _take_code(code: str) -> dict | None:
    """取出并删除。重放拿到的永远是 None，调用方据此回 410。"""
    with _codes_lock:
        return _CODES.pop(code, None)


def reset_codes() -> None:
    with _codes_lock:
        _CODES.clear()


# ---------- 工具 ----------

def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _append_query(uri: str, params: dict) -> str:
    """在保留原有 query 的前提下追加参数。子平台的回调地址可能自带参数。"""
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((k, v) for k, v in params.items() if v is not None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


def _error_redirect(uri: str, state: str, error: str, description: str = ''):
    params = {'error': error}
    if description:
        params['error_description'] = description
    if state:
        params['state'] = state
    return RedirectResponse(_append_query(uri, params), status_code=302,
                            headers={'Cache-Control': 'no-store'})


def _client_credentials(request: Request, form) -> tuple[str, str]:
    """按 RFC 6749 取客户端凭据：优先 Basic，其次表单。

    `golang.org/x/oauth2` 默认用自动探测（先 Basic，4xx 后改表单），
    因此两种都必须支持，否则子平台的第一次请求会被判成认证失败。
    """
    header = request.headers.get('authorization', '')
    if header[:6].lower() == 'basic ':
        try:
            decoded = base64.b64decode(header[6:]).decode('utf-8')
        except Exception:                       # noqa: BLE001
            return '', ''
        client_id, _, client_secret = decoded.partition(':')
        return client_id, client_secret
    return str(form.get('client_id') or ''), str(form.get('client_secret') or '')


async def _current_principal(request: Request):
    """中台会话 → 主体。没有会话返回 None，由 authorize 决定送去登录。"""
    store = getattr(request.app.state, 'store', None)
    if store is None:
        return None
    try:
        return await auth_api.principal_from_session(request, store)
    except HTTPException:
        # 过期/撤销/停用都按"没登录"处理：让用户重新走一遍登录，
        # 而不是把一个 401 甩在授权页上。
        return None


# ---------- 端点 ----------

@router.get(_PATH + '/.well-known/openid-configuration')
async def discovery(request: Request):
    """OIDC 服务发现。`issuer` 必须与配置逐字节一致，go-oidc 会比对。"""
    if not settings.idp_enabled:
        raise HTTPException(status_code=404, detail='身份提供方未启用')
    issuer = settings.idp_issuer
    return JSONResponse({
        'issuer': issuer,
        'authorization_endpoint': issuer + '/authorize',
        'token_endpoint': issuer + '/token',
        'jwks_uri': issuer + '/jwks.json',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code'],
        'subject_types_supported': ['public'],
        'id_token_signing_alg_values_supported': ['RS256'],
        'token_endpoint_auth_methods_supported': [
            'client_secret_basic', 'client_secret_post', 'none'],
        'code_challenge_methods_supported': ['S256'],
        'scopes_supported': ['openid', 'profile'],
        'claims_supported': ['iss', 'sub', 'aud', 'exp', 'iat', 'nonce',
                             'azp', 'name', 'preferred_username',
                             'tenant_id', 'role'],
    }, headers={'Cache-Control': 'no-store'})


@router.get(_PATH + '/jwks.json')
async def jwks(request: Request):
    """公钥集合。公开可读——子平台建立首次信任前没有别的凭据可用。"""
    if not settings.idp_enabled:
        raise HTTPException(status_code=404, detail='身份提供方未启用')
    key, kid = _key_and_kid()
    return JSONResponse(idp_keys.jwks(key, kid),
                        headers={'Cache-Control': 'no-store'})


@router.get(_PATH + '/authorize')
async def authorize(request: Request):
    """授权码端点。

    错误处理分两段，这是开放重定向的分界线：
    - **`redirect_uri` 校验之前**的错误只能直接 400，绝不跳转；
    - 校验之后的错误才带 `error` 跳回子平台。
    """
    params = request.query_params
    client_id = (params.get('client_id') or '').strip()
    redirect_uri = (params.get('redirect_uri') or '').strip()
    state = params.get('state') or ''

    client = clients().get(client_id)
    if client is None:
        raise HTTPException(status_code=400, detail='未知的客户端')
    # 精确匹配：不做前缀、不做通配。这里是整条链路上最容易出洞的地方。
    if redirect_uri not in client['redirect_uris']:
        raise HTTPException(status_code=400, detail='回调地址不在白名单')

    if params.get('response_type') != 'code':
        return _error_redirect(redirect_uri, state,
                               'unsupported_response_type')
    code_challenge = (params.get('code_challenge') or '').strip()
    if (params.get('code_challenge_method') or '') != 'S256' or not code_challenge:
        return _error_redirect(redirect_uri, state, 'invalid_request',
                               '必须使用 PKCE S256')
    if 'openid' not in (params.get('scope') or '').split():
        return _error_redirect(redirect_uri, state, 'invalid_scope')
    nonce = (params.get('nonce') or '').strip()
    if not nonce:
        # 子平台的客户端强制校验 nonce；这里放过去只会让失败延后到 token 之后，
        # 错误信息还更难懂。
        return _error_redirect(redirect_uri, state, 'invalid_request',
                               '缺少 nonce')

    principal = await _current_principal(request)
    if principal is None:
        # 送去登录页，登录后回到本次授权请求。前端若不认识 next 参数，
        # 用户手动点一次也能继续——不会造成死循环。
        target = str(request.url)
        login = _append_query(settings.idp_login_path, {'next': target})
        return RedirectResponse(login, status_code=302,
                                headers={'Cache-Control': 'no-store'})

    code = _put_code({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'code_challenge': code_challenge,
        'nonce': nonce,
        'user_id': principal.user_id,
        'tenant_id': principal.tenant_id,
        'role': principal.role,
    })
    logger.info('签出授权码 user=%s client=%s', principal.user_id, client_id)
    return RedirectResponse(
        _append_query(redirect_uri, {'code': code, 'state': state or None}),
        status_code=302, headers={'Cache-Control': 'no-store'})


@router.post(_PATH + '/token')
async def token(request: Request):
    """授权码换 id_token。带 `Cache-Control: no-store` 是规范要求。"""
    if not settings.idp_enabled:
        raise HTTPException(status_code=404, detail='身份提供方未启用')

    form = await request.form()
    if str(form.get('grant_type') or '') != 'authorization_code':
        return _token_error('unsupported_grant_type', 400)

    client_id, client_secret = _client_credentials(request, form)
    client = clients().get(client_id)
    if client is None:
        return _token_error('invalid_client', 401)
    expected_secret = client['secret']
    if expected_secret and not hmac.compare_digest(client_secret, expected_secret):
        # 常量时间比较：普通 == 会让攻击者按响应时间逐字节猜 secret。
        logger.warning('客户端密钥校验失败 client=%s', client_id)
        return _token_error('invalid_client', 401)

    code = str(form.get('code') or '')
    record = _take_code(code)
    if record is None:
        # 可能是重放，也可能是过期后被清理。两种情况对外都是"码不可用"。
        return _token_error('invalid_grant', 400, '授权码无效或已使用')
    if record['client_id'] != client_id:
        return _token_error('invalid_grant', 400, '授权码不属于该客户端')
    if record['exp'] < time.time():
        return _token_error('invalid_grant', 400, '授权码已过期')
    # redirect_uri 必须与授权时一致（RFC 6749 §4.1.3）。
    if str(form.get('redirect_uri') or '') != record['redirect_uri']:
        return _token_error('invalid_grant', 400, '回调地址不一致')

    verifier = str(form.get('code_verifier') or '')
    if not verifier:
        return _token_error('invalid_request', 400, '缺少 code_verifier')
    if not hmac.compare_digest(_s256(verifier), record['code_challenge']):
        logger.warning('PKCE 校验失败 client=%s', client_id)
        return _token_error('invalid_grant', 400, 'PKCE 校验失败')

    store = getattr(request.app.state, 'store', None)
    user = await store.user(record['user_id']) if store is not None else None
    if not user or str(user.get('status') or 'active') != 'active':
        # 授权码签发后账号被停用：此时不能再签一张有效身份票。
        return _token_error('invalid_grant', 400, '账号不可用')

    key, kid = _key_and_kid()
    now = int(time.time())
    claims = {
        'iss': settings.idp_issuer,
        'sub': record['user_id'],
        'aud': client_id,
        'azp': client_id,
        'iat': now,
        'nbf': now,
        'exp': now + settings.idp_id_token_ttl,
        'nonce': record['nonce'],
        'name': str(user.get('display_name') or ''),
        'preferred_username': str(user.get('display_name') or ''),
        'tenant_id': record['tenant_id'],
        'role': record['role'],
    }
    id_token = jose_jwt.encode({'alg': 'RS256', 'kid': kid}, claims, key)

    logger.info('签发 id_token user=%s client=%s', record['user_id'], client_id)
    return JSONResponse({
        'access_token': secrets.token_urlsafe(32),
        'token_type': 'Bearer',
        'expires_in': settings.idp_id_token_ttl,
        'id_token': id_token,
    }, headers={'Cache-Control': 'no-store'})


def _token_error(error: str, status: int, description: str = '') -> JSONResponse:
    """OAuth2 错误响应。状态码按 RFC 6749 §5.2：认证类 401，其余 400。"""
    body = {'error': error}
    if description:
        body['error_description'] = description
    return JSONResponse(body, status_code=status,
                        headers={'Cache-Control': 'no-store'})
