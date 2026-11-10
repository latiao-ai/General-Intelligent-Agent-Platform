"""可选 OIDC 授权码登录：可信映射后兑换中台本地会话。"""
import json
import logging
import os
import secrets
import time
from urllib.parse import urlsplit

import httpx
import jwt
from authlib.integrations.httpx_client import AsyncOAuth2Client
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webpage_agent.api import auth
from webpage_agent.core.config import settings
from webpage_agent.observe.runtime_check import transport_issue

router = APIRouter()
logger = logging.getLogger(__name__)
_COOKIE = 'agent_oidc_transaction'
_PATH = '/api/auth/oidc'
_TTL = 300


def _config():
    if os.getenv('OIDC_ENABLED', '').lower() != 'true':
        raise HTTPException(404, 'OIDC 未启用')
    values = {k: os.getenv('OIDC_' + k.upper(), '').strip() for k in
              ('issuer', 'client_id', 'client_secret', 'redirect_uri', 'transaction_key', 'identity_map')}
    try:
        if settings.auth_mode != 'ticket' or not all(values.values()):
            raise ValueError('missing configuration or AUTH_MODE')
        allow_local = settings.app_env != 'production'
        if transport_issue(values['issuer'], allow_loopback=allow_local) or transport_issue(values['redirect_uri'], allow_loopback=allow_local):
            raise ValueError('insecure URL')
        issuer = urlsplit(values['issuer'])
        if issuer.query or issuer.fragment:
            raise ValueError('invalid issuer')
        redirect = urlsplit(values['redirect_uri'])
        if redirect.path != _PATH + '/callback' or redirect.query or redirect.fragment:
            raise ValueError('invalid redirect URI')
        if not settings.auth_cookie_secure and (settings.app_env == 'production' or redirect.scheme != 'http' or redirect.hostname not in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('insecure cookie')
        Fernet(values['transaction_key'].encode())
        mapping = json.loads(values['identity_map'])
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError('empty identity mapping')
        for sub, item in mapping.items():
            if not isinstance(sub, str) or not sub or not isinstance(item, dict):
                raise ValueError('invalid identity mapping')
            if any(not isinstance(item.get(k), str) or not item[k].strip() for k in
                   ('tenant_id', 'platform_id', 'identity_provider_id', 'subject', 'connection_id')):
                raise ValueError('invalid identity mapping')
        values['mapping'] = mapping
    except (ValueError, TypeError):
        logger.error('OIDC 配置无效')
        raise HTTPException(503, 'OIDC 配置无效') from None
    return values


async def _metadata(config):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(config['issuer'].rstrip('/') + '/.well-known/openid-configuration')
        response.raise_for_status()
        data = response.json()
    if data.get('issuer') != config['issuer']:
        raise ValueError('issuer mismatch')
    for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
        if transport_issue(data.get(name, ''), allow_loopback=settings.app_env != 'production'):
            raise ValueError('insecure endpoint')
    if 'S256' not in data.get('code_challenge_methods_supported', []):
        raise ValueError('PKCE S256 unsupported')
    return data


def _disabled_page(status: int, detail: str) -> str:
    """OIDC 不可用时的说明页：自包含、不引外部资源，避免浏览器看到裸 JSON。"""
    import html
    reason = html.escape(detail)
    hint = ('当前进程没有开启统一身份登录。本地演示（AUTH_MODE=off）不需要登录，'
            '直接回中台就能用。'
            if status == 404 else
            '统一身份登录的配置不完整，服务端拒绝了这次请求。请检查 OIDC_* 配置后重启。')
    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>未启用统一身份登录</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f7f7f8;color:#1f2329;font:14px/1.7 -apple-system,"PingFang SC",sans-serif}}
.card{{max-width:560px;padding:32px 36px;background:#fff;border:1px solid #e8e8e8;border-radius:12px;
box-shadow:0 1px 3px rgba(0,0,0,.04)}}
h1{{margin:0 0 12px;font-size:17px;font-weight:600}}
p{{margin:0 0 10px;color:#4b5563}}
code{{padding:2px 6px;background:#f3f3f3;border-radius:4px;font-size:12px}}
a{{display:inline-block;margin-top:14px;color:#00a884;text-decoration:none;font-weight:500}}
a:hover{{text-decoration:underline}}
</style></head><body><div class="card">
<h1>这个地址不提供登录（{status}）</h1>
<p>{reason}</p>
<p>{hint}</p>
<p>需要真正的登录时，用 <code>AUTH_MODE=ticket</code> 加 <code>OIDC_*</code>（或 <code>BEEFTV_API_BASE</code> 账号登录）启动。</p>
<a href="/">← 回到中台</a>
</div></body></html>'''


@router.get(_PATH + '/login')
async def login(request: Request, prompt: str = ''):
    """`?prompt=login` 强制身份提供方重新认证，用于切换账号。"""
    try:
        config = _config()
    except HTTPException as exc:
        # 浏览器直接打开这个地址（书签、刷新、旧页面上的链接）时，回一页能看懂的话，
        # 不要把 {"detail": "OIDC 未启用"} 这种 JSON 摆到地址栏里。
        if 'text/html' in request.headers.get('accept', ''):
            return HTMLResponse(_disabled_page(exc.status_code, str(exc.detail)),
                                status_code=exc.status_code)
        raise
    try:
        meta = await _metadata(config)
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        # 只接受白名单值，避免把任意参数透传给身份提供方
        extra = {'prompt': 'login'} if prompt == 'login' else {}
        async with AsyncOAuth2Client(config['client_id'], config['client_secret'],
                                     redirect_uri=config['redirect_uri'], scope='openid',
                                     code_challenge_method='S256') as client:
            url, _ = client.create_authorization_url(meta['authorization_endpoint'], state=state,
                                                       nonce=nonce, code_verifier=verifier, **extra)
        transaction = Fernet(config['transaction_key'].encode()).encrypt(json.dumps(
            {'state': state, 'nonce': nonce, 'verifier': verifier}).encode()).decode()
        response = RedirectResponse(url, status_code=302, headers={'Cache-Control': 'no-store'})
        response.set_cookie(_COOKIE, transaction, max_age=_TTL, httponly=True,
                            secure=settings.auth_cookie_secure, samesite='lax', path=_PATH)
        return response
    except Exception:
        logger.exception('OIDC 登录初始化失败')
        raise HTTPException(503, '身份服务不可用') from None


@router.get(_PATH + '/callback')
async def callback(request: Request):
    config = _config()
    try:
        if len(request.query_params.getlist('state')) != 1 or len(request.query_params.getlist('code')) != 1:
            raise ValueError('invalid callback')
        token = request.cookies.get(_COOKIE, '')
        transaction = json.loads(Fernet(config['transaction_key'].encode()).decrypt(token.encode(), ttl=_TTL))
        state = request.query_params['state']
        if not secrets.compare_digest(state, transaction['state']):
            raise ValueError('state mismatch')
        meta = await _metadata(config)
        async with AsyncOAuth2Client(config['client_id'], config['client_secret'],
                                     redirect_uri=config['redirect_uri'], timeout=10,
                                     follow_redirects=False) as client:
            result = await client.fetch_token(meta['token_endpoint'], code=request.query_params['code'],
                                               code_verifier=transaction['verifier'])
        id_token = result['id_token']
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(meta['jwks_uri'])
            response.raise_for_status()
            jwks = response.json()
        header = jwt.get_unverified_header(id_token)
        if header.get('alg') != 'RS256' or not header.get('kid'):
            raise ValueError('invalid algorithm')
        keys = [key for key in jwks['keys'] if key.get('kid') == header['kid'] and key.get('kty') == 'RSA' and key.get('use', 'sig') == 'sig']
        if len(keys) != 1:
            raise ValueError('invalid signing key')
        claims = jwt.decode(id_token, jwt.PyJWK.from_dict(keys[0]).key, algorithms=['RS256'],
                            audience=config['client_id'], issuer=config['issuer'],
                            options={'require': ['iss', 'aud', 'sub', 'exp', 'iat', 'nonce']})
        if not isinstance(claims['nonce'], str) or not secrets.compare_digest(claims['nonce'], transaction['nonce']):
            raise ValueError('nonce mismatch')
        if isinstance(claims['aud'], list) and len(claims['aud']) > 1 and claims.get('azp') != config['client_id']:
            raise ValueError('invalid authorized party')
        if claims.get('azp') and claims['azp'] != config['client_id']:
            raise ValueError('invalid authorized party')
        mapping = config['mapping'].get(claims['sub'])
        if not mapping:
            raise ValueError('identity not mapped')
        store = getattr(request.app.state, 'store', None)
        if store is None:
            raise RuntimeError('store unavailable')
        session_token = auth.new_session_token()
        row = await store.redeem_ticket(issuer='oidc:' + config['issuer'], jti=auth.digest_of(state),
            expires_at=int(time.time()) + _TTL, session_id='as_' + secrets.token_hex(12),
            session_digest=auth.digest_of(session_token), session_expires_at=time.time() + settings.auth_session_ttl,
            user_agent=request.headers.get('user-agent', ''), remote_addr=request.client.host if request.client else '',
            **{k: mapping[k] for k in ('tenant_id', 'platform_id', 'identity_provider_id', 'subject', 'connection_id')})
        user = await store.user(row['user_id'])
        if not user or user.get('status') != 'active':
            await store.revoke_auth_session(row['auth_session_id'])
            raise ValueError('inactive user')
        response = RedirectResponse('/', status_code=303, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie(settings.auth_session_cookie, session_token, max_age=settings.auth_session_ttl,
                            httponly=True, secure=settings.auth_cookie_secure,
                            samesite=settings.auth_cookie_samesite, path='/')
    except Exception:
        logger.warning('OIDC 回调验证失败')
        response = RedirectResponse('/?login_error=oidc', status_code=303,
                                    headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
    response.delete_cookie(_COOKIE, path=_PATH, secure=settings.auth_cookie_secure, httponly=True, samesite='lax')
    return response
