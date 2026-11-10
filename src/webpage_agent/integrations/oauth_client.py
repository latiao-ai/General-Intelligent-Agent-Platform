"""通用 OAuth 2.1 客户端：授权码 + PKCE + 动态客户端注册。

只负责"怎么跟授权服务器说话"，不认识 MCP 服务端、不碰数据库、不存用户。
绑定关系的落盘与刷新在 `webpage_agent/core/oauth_store.py`。

三条约束：

1. **不硬编码任何厂商**：issuer / scopes / client_name 全部来自 `MCP_SERVERS`
   的 `oauth` 配置。同一份代码要能接 LibTV、GitHub、飞书。
2. **优先公共客户端**：MCP 授权规范推荐的动态注册是
   `token_endpoint_auth_method=none` + PKCE，不保存 client_secret。对方发了
   secret 也照常带（部分企业级 server 会发），但流程不依赖它。
3. **回调只允许回环或 HTTPS**：`redirect_uri` 是浏览器地址栏里的东西，明文
   回环（127.0.0.1）是桌面/本机联调唯一安全的选择；公网必须 HTTPS。

为什么不用现成的 OAuth 库：各家 MCP server 的元数据端点、Resource Indicators
（RFC 8707）强制程度、以及"注册端点是否匿名可用"差异很大，套库反而要在中间
层反复绕过它的假设。这里只实现用到的六个动作。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx

# 授权服务器元数据缓存：同一 issuer 不必每次连接都打一次 well-known。
# 过期时间给 10 分钟而不是永久：issuer 换端点（灰度、迁域）时不能一直用旧的。
META_TTL_SECONDS = 600
_META_CACHE: dict[str, tuple[dict, float]] = {}
_RESOURCE_CACHE: dict[str, tuple[str, float]] = {}

DEFAULT_TIMEOUT = 20.0


class OAuthError(RuntimeError):
    """授权流程失败。`status` 是对方返回的 HTTP 状态（本地校验失败时为 0）。"""

    def __init__(self, message: str, *, status: int = 0, body: str = '') -> None:
        text = message if not body else f'{message}（HTTP {status}: {body[:200]}）'
        super().__init__(text)
        self.status = status


@dataclass(frozen=True)
class OAuthConfig:
    """一次授权要用到的全部外部参数。来自 `MCP_SERVERS` 条目，不来自代码。"""
    issuer: str
    scopes: tuple = ()
    client_name: str = '智能体中台'
    resource: str = ''
    redirect_uri: str = ''


async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """唯一的网络出口。

    测试直接替换这个函数即可，不必起假服务器——授权流程的价值在于参数拼得对
    不对（state / PKCE / resource），不在于 httpx 会不会发请求。
    """
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        return await client.request(method, url, **kwargs)


def _well_known(issuer: str, suffix: str) -> str:
    return issuer.rstrip('/') + '/.well-known/' + suffix


def clear_cache() -> None:
    _META_CACHE.clear()
    _RESOURCE_CACHE.clear()


async def discover(issuer: str, *, ttl: int = META_TTL_SECONDS) -> dict:
    """读授权服务器元数据（RFC 8414），读不到再退 OIDC 发现。

    两个端点都要试：LibTV 只实现前者，不少企业 IdP 只实现后者。
    只认一次失败就放弃，会把"能用"的环境配成"不能用"。
    """
    key = issuer.rstrip('/')
    if not key:
        raise OAuthError('oauth.issuer 不能为空')
    cached = _META_CACHE.get(key)
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]

    last_error = ''
    for suffix in ('oauth-authorization-server', 'openid-configuration'):
        try:
            res = await _request('GET', _well_known(key, suffix),
                                 headers={'accept': 'application/json'})
        except Exception as exc:                     # noqa: BLE001
            last_error = f'{type(exc).__name__}: {exc}'
            continue
        if res.status_code != 200:
            last_error = f'HTTP {res.status_code}'
            continue
        try:
            meta = res.json()
        except ValueError:
            last_error = '返回的不是 JSON'
            continue
        if not isinstance(meta, dict):
            last_error = '元数据不是对象'
            continue
        if not meta.get('authorization_endpoint') or not meta.get('token_endpoint'):
            last_error = '缺少 authorization_endpoint / token_endpoint'
            continue
        _META_CACHE[key] = (meta, now + ttl)
        return meta
    raise OAuthError(f'读取授权服务器元数据失败：{last_error or "未知原因"}')


async def discover_resource(issuer: str, *, fallback: str = '') -> str:
    """RFC 9728 保护资源标识。

    LibTV 网关强制要求授权与换 token 时都带 `resource`，缺了直接
    `invalid_request`；拿不到元数据就退回网关地址本身，不要空着。
    """
    key = issuer.rstrip('/')
    cached = _RESOURCE_CACHE.get(key)
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]
    value = fallback
    try:
        res = await _request('GET', _well_known(key, 'oauth-protected-resource'),
                             headers={'accept': 'application/json'})
        if res.status_code == 200:
            body = res.json()
            if isinstance(body, dict) and isinstance(body.get('resource'), str):
                value = body['resource'].strip() or value
    except Exception:                                # noqa: BLE001
        # 元数据是加分项，不是前置条件：拿不到就用调用方给的地址。
        pass
    _RESOURCE_CACHE[key] = (value, now + META_TTL_SECONDS)
    return value


async def register_client(meta: dict, *, redirect_uri: str, client_name: str,
                          scopes: tuple) -> dict:
    """动态客户端注册（RFC 7591）。

    这一步就是"不用去后台填表"的全部原因：客户端自己报备自己，拿回 client_id。
    对端没提供注册端点时**明确报错**，不退化成"猜一个 client_id"——那只会
    在授权页上得到一个看不懂的 400。
    """
    endpoint = str(meta.get('registration_endpoint') or '').strip()
    if not endpoint:
        raise OAuthError(
            '对方未提供动态注册端点（registration_endpoint），'
            '需要按其要求预先注册应用并在 MCP_SERVERS 里填 oauth.client_id / client_secret')
    body = {
        'client_name': client_name,
        'redirect_uris': [redirect_uri],
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'token_endpoint_auth_method': 'none',
    }
    if scopes:
        body['scope'] = ' '.join(scopes)
    try:
        res = await _request('POST', endpoint, json=body,
                            headers={'accept': 'application/json'})
    except Exception as exc:                         # noqa: BLE001
        raise OAuthError(f'动态注册客户端失败：{type(exc).__name__}: {exc}') from exc
    if res.status_code not in (200, 201):
        raise OAuthError('动态注册客户端被拒绝', status=res.status_code,
                         body=res.text)
    try:
        data = res.json()
    except ValueError as exc:
        raise OAuthError('动态注册返回的不是 JSON', status=res.status_code) from exc
    client_id = str(data.get('client_id') or '').strip()
    if not client_id:
        raise OAuthError('动态注册没有返回 client_id', status=res.status_code)
    return {
        'client_id': client_id,
        'client_secret': str(data.get('client_secret') or ''),
        'redirect_uri': redirect_uri,
        'issuer': str(meta.get('issuer') or ''),
    }


def make_pkce() -> tuple[str, str]:
    """返回 `(verifier, challenge)`；S256，verifier 32 字节随机。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip('=')
    return verifier, challenge


def make_state() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip('=')


def build_authorize_url(meta: dict, *, client_id: str, redirect_uri: str,
                        scopes: tuple, state: str, challenge: str,
                        resource: str = '') -> str:
    """拼授权页地址。参数顺序固定，便于测试与排障时逐项比对。"""
    endpoint = str(meta.get('authorization_endpoint') or '').strip()
    if not endpoint:
        raise OAuthError('元数据里没有 authorization_endpoint')
    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': ' '.join(scopes),
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    if resource:
        # RFC 8707：告诉授权服务器"这个 token 是给哪个资源用的"。
        # 有的网关强制校验，缺了就是 invalid_request。
        params['resource'] = resource
    separator = '&' if '?' in endpoint else '?'
    return endpoint + separator + urlencode(params)


def _token_payload(body: dict) -> dict:
    """把 token 响应归一化成存档结构；`expires_at` 用毫秒时间戳。"""
    access = str(body.get('access_token') or '')
    if not access:
        raise OAuthError('token 响应里没有 access_token')
    expires_in = body.get('expires_in')
    expires_at = 0
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        expires_at = int(time.time() * 1000) + seconds * 1000
    return {
        'access_token': access,
        'refresh_token': str(body.get('refresh_token') or ''),
        'token_type': str(body.get('token_type') or 'Bearer'),
        'scope': str(body.get('scope') or ''),
        'expires_at': expires_at,
        'obtained_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


async def exchange_code(meta: dict, *, code: str, code_verifier: str,
                         client_id: str, redirect_uri: str,
                         resource: str = '', client_secret: str = '') -> dict:
    """授权码换 token。失败时不回显 code（它在日志里等价于一次性密码）。"""
    endpoint = str(meta.get('token_endpoint') or '').strip()
    if not endpoint:
        raise OAuthError('元数据里没有 token_endpoint')
    form = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': code_verifier,
    }
    if resource:
        form['resource'] = resource
    if client_secret:
        form['client_secret'] = client_secret
    try:
        res = await _request('POST', endpoint, data=form,
                             headers={'accept': 'application/json'})
    except Exception as exc:                         # noqa: BLE001
        raise OAuthError(f'换取 token 失败：{type(exc).__name__}: {exc}') from exc
    if res.status_code != 200:
        raise OAuthError('换取 token 被拒绝', status=res.status_code, body=res.text)
    try:
        return _token_payload(res.json())
    except ValueError as exc:
        raise OAuthError('token 响应不是 JSON', status=res.status_code) from exc


async def refresh(meta: dict, *, refresh_token: str, client_id: str,
                  resource: str = '', client_secret: str = '') -> dict:
    """用 refresh_token 换新 access_token。

    对方可能**不返回新的 refresh_token**：这时必须沿用旧的，否则下一次
    刷新就没了凭据（表现为"用一小时后突然要求重新授权"）。
    """
    endpoint = str(meta.get('token_endpoint') or '').strip()
    if not endpoint:
        raise OAuthError('元数据里没有 token_endpoint')
    form = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
    }
    if resource:
        form['resource'] = resource
    if client_secret:
        form['client_secret'] = client_secret
    try:
        res = await _request('POST', endpoint, data=form,
                             headers={'accept': 'application/json'})
    except Exception as exc:                         # noqa: BLE001
        raise OAuthError(f'刷新 token 失败：{type(exc).__name__}: {exc}') from exc
    if res.status_code != 200:
        raise OAuthError('刷新 token 被拒绝', status=res.status_code, body=res.text)
    try:
        payload = _token_payload(res.json())
    except ValueError as exc:
        raise OAuthError('刷新响应不是 JSON', status=res.status_code) from exc
    if not payload.get('refresh_token'):
        payload['refresh_token'] = refresh_token
    return payload


def validate_redirect_uri(uri: str) -> str:
    """回调地址只允许回环或 HTTPS。

    明文 HTTP 只放过 127.0.0.1 / localhost：授权码就在 URL 里，
    在局域网明文传等于把 token 交给同网段任何人。
    """
    text = str(uri or '').strip()
    if not text:
        raise OAuthError('没有配置 oauth.redirect_uri（或 OAUTH_PUBLIC_BASE）')
    parts = urlsplit(text)
    if parts.scheme == 'https' and parts.netloc:
        return text
    if parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost'):
        return text
    raise OAuthError('回调地址必须是 https:// 或 http://127.0.0.1（明文回调会把授权码暴露在链路上）')


def dumps(creds: dict) -> str:
    return json.dumps(creds, ensure_ascii=False, indent=2) + '\n'
