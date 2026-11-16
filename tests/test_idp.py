"""中台 OIDC 身份提供方（`api/idp.py`）的契约测试。

重点不是"能登录成功"，而是**拒绝的姿势对不对**。这三个断言各自挡着一类事故：

- `redirect_uri` 不在白名单时必须 400 而不是跳转，否则本模块就是一个开放
  重定向器（攻击者拿它把授权码送到自己的域名）。
- 授权码重放必须失败，否则抓包拿到一次码就能换出无限张身份票。
- PKCE 必须真校验，否则公开客户端没有 secret 时等于没有任何保护。

最后一组用例走完整链路，用子平台同样的方式（拉 JWKS、按 kid 取公钥、验签、
比对 nonce）验证签出来的 id_token 能被独立验证——这是"能完成目标"的唯一证据。
"""
import base64
import hashlib
import json
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt as jose_jwt
from joserfc.jwk import RSAKey

from webpage_agent.api import idp
from webpage_agent.core.config import settings

ISSUER = 'http://testserver/api/idp'
REDIRECT = 'http://sub.example.com/api/auth/oidc/callback'
CLIENT = 'beeftv'
SECRET = 'test-secret'
VERIFIER = 'a' * 64


def _challenge(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


class _Store:
    """只实现 `principal_from_session` 会调到的两个方法。

    刻意不引入真实数据库：这条链路的正确性不依赖存储实现，而依赖
    "会话 → 用户 → sub"这个映射关系。
    """

    def __init__(self, status: str = 'active'):
        self.status = status

    async def auth_session(self, digest: str):
        return {
            'user_id': 'u_test', 'tenant_id': 'default', 'role': 'member',
            'platform_id': 'sms', 'auth_session_id': 'as_1',
            'connection_id': '', 'subject': '13800138000',
            'identity_provider_id': 'sms', 'expires_at': 9e18,
            'revoked_at': None,
        }

    async def user(self, user_id: str):
        return {'user_id': user_id, 'display_name': '张三',
                'email': '', 'status': self.status}


def _patched_settings(monkeypatch, tmp_path, **overrides):
    """`settings` 是 frozen dataclass，只能用 replace 造新实例再替换引用。

    直接 setattr 会抛 FrozenInstanceError；改环境变量又来不及（默认值在类定义
    时就求值了）。替换 `idp` 模块里的引用是最小的侵入面。
    """
    base = {
        'idp_enabled': True,
        'idp_issuer': ISSUER,
        'idp_clients_raw': json.dumps({
            CLIENT: {'secret': SECRET, 'redirect_uris': [REDIRECT]},
        }),
        'idp_key_path': str(tmp_path / 'signing.pem'),
        'idp_login_path': '/',
    }
    base.update(overrides)
    patched = replace(settings, **base)
    monkeypatch.setattr(idp, 'settings', patched)
    idp.reset_cache()
    idp.reset_codes()
    return patched


@pytest.fixture
def client(monkeypatch, tmp_path):
    _patched_settings(monkeypatch, tmp_path)

    app = FastAPI()
    app.state.store = _Store()
    app.include_router(idp.router)
    yield TestClient(app)

    idp.reset_cache()
    idp.reset_codes()


def _authorize_params(**overrides) -> dict:
    params = {
        'client_id': CLIENT,
        'redirect_uri': REDIRECT,
        'response_type': 'code',
        'scope': 'openid',
        'state': 'st-123',
        'nonce': 'nc-456',
        'code_challenge': _challenge(),
        'code_challenge_method': 'S256',
    }
    params.update(overrides)
    return params


# ---------- 服务发现与公钥 ----------

def test_discovery_declares_exactly_what_clients_need(client):
    """issuer 必须与配置逐字节一致，go-oidc 会拿它和请求值比对。"""
    body = client.get('/api/idp/.well-known/openid-configuration').json()
    assert body['issuer'] == ISSUER
    assert body['authorization_endpoint'] == ISSUER + '/authorize'
    assert body['token_endpoint'] == ISSUER + '/token'
    assert body['jwks_uri'] == ISSUER + '/jwks.json'
    assert body['id_token_signing_alg_values_supported'] == ['RS256']
    # 少了这条，子平台的 oauth2 客户端会判定 PKCE 不可用。
    assert 'S256' in body['code_challenge_methods_supported']


def test_jwks_exposes_only_public_material(client):
    body = client.get('/api/idp/jwks.json').json()
    assert len(body['keys']) == 1
    key = body['keys'][0]
    assert key['kty'] == 'RSA'
    assert key['use'] == 'sig'
    assert key['kid']
    # RSA 私钥指数叫 d；它出现在公钥文档里就说明密钥泄漏了。
    assert 'd' not in key
    assert 'p' not in key and 'q' not in key


# ---------- 授权端点的拒绝路径 ----------

def test_unknown_client_is_rejected_without_redirect(client):
    resp = client.get('/api/idp/authorize',
                      params=_authorize_params(client_id='nobody'),
                      follow_redirects=False)
    assert resp.status_code == 400


def test_unlisted_redirect_uri_never_redirects(client):
    """核心安全断言：白名单之外的回调地址连错误都不能跳过去。

    如果这里改成"带 error 跳转"，攻击者只要把 redirect_uri 换成自己的域名，
    就能收到中台发过去的授权码。
    """
    resp = client.get('/api/idp/authorize',
                      params=_authorize_params(
                          redirect_uri='http://evil.example.com/cb'),
                      follow_redirects=False)
    assert resp.status_code == 400
    assert 'location' not in {k.lower() for k in resp.headers}


def test_missing_pkce_is_rejected(client):
    resp = client.get('/api/idp/authorize',
                      params=_authorize_params(code_challenge='',
                                               code_challenge_method=''),
                      follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers['location']
    assert 'error=invalid_request' in location
    # 白名单地址校验通过后，错误才允许跳回去。
    assert location.startswith(REDIRECT)


def test_missing_nonce_is_rejected(client):
    """子平台客户端强制校验 nonce；这里放行只会把失败推迟到更后面。"""
    resp = client.get('/api/idp/authorize',
                      params=_authorize_params(nonce=''),
                      follow_redirects=False)
    assert resp.status_code == 302
    assert 'error=invalid_request' in resp.headers['location']


def test_anonymous_request_is_sent_to_login(client):
    resp = client.get('/api/idp/authorize', params=_authorize_params(),
                      follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers['location'].startswith('/?next=')


# ---------- 完整链路 ----------

def _code_from(client) -> str:
    client.cookies.set(settings.auth_session_cookie, 'session-token')
    resp = client.get('/api/idp/authorize', params=_authorize_params(),
                      follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers['location']
    assert location.startswith(REDIRECT)
    return dict(
        pair.split('=', 1) for pair in location.split('?', 1)[1].split('&')
    )['code']


def test_id_token_is_verifiable_by_a_third_party(client):
    """按子平台的做法独立验签：拉 JWKS、按 kid 取公钥、验 claims。"""
    code = _code_from(client)

    resp = client.post('/api/idp/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT,
        'client_id': CLIENT,
        'client_secret': SECRET,
        'code_verifier': VERIFIER,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body['token_type'] == 'Bearer'
    assert body['id_token']
    # 授权码响应绝不能被缓存，否则中间代理可能把一张票发给两个人。
    assert resp.headers['cache-control'] == 'no-store'

    jwks = client.get('/api/idp/jwks.json').json()
    public = RSAKey.import_key({k: jwks['keys'][0][k] for k in ('kty', 'n', 'e')})
    verified = jose_jwt.decode(body['id_token'], public,
                               algorithms=['RS256']).claims

    assert verified['iss'] == ISSUER
    # sub 是中台 user_id，不是手机号——换号不该丢身份。
    assert verified['sub'] == 'u_test'
    assert verified['aud'] == CLIENT
    assert verified['azp'] == CLIENT
    assert verified['nonce'] == 'nc-456'
    assert verified['tenant_id'] == 'default'
    assert verified['exp'] > verified['iat']


def test_authorization_code_cannot_be_replayed(client):
    code = _code_from(client)
    payload = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT,
        'client_id': CLIENT,
        'client_secret': SECRET,
        'code_verifier': VERIFIER,
    }
    assert client.post('/api/idp/token', data=payload).status_code == 200
    second = client.post('/api/idp/token', data=payload)
    assert second.status_code == 400
    assert second.json()['error'] == 'invalid_grant'


def test_wrong_code_verifier_is_rejected(client):
    code = _code_from(client)
    resp = client.post('/api/idp/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT,
        'client_id': CLIENT,
        'client_secret': SECRET,
        'code_verifier': 'b' * 64,
    })
    assert resp.status_code == 400
    assert resp.json()['error'] == 'invalid_grant'


def test_wrong_client_secret_is_rejected(client):
    code = _code_from(client)
    resp = client.post('/api/idp/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT,
        'client_id': CLIENT,
        'client_secret': 'wrong',
        'code_verifier': VERIFIER,
    })
    assert resp.status_code == 401
    assert resp.json()['error'] == 'invalid_client'


def test_redirect_uri_must_match_at_token_step(client):
    """授权时用 A 地址、换票时用 B 地址必须失败（RFC 6749 §4.1.3）。"""
    code = _code_from(client)
    resp = client.post('/api/idp/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': 'http://sub.example.com/other',
        'client_id': CLIENT,
        'client_secret': SECRET,
        'code_verifier': VERIFIER,
    })
    assert resp.status_code == 400


def test_disabled_provider_hides_endpoints(client, monkeypatch, tmp_path):
    _patched_settings(monkeypatch, tmp_path, idp_enabled=False)
    assert client.get('/api/idp/jwks.json').status_code == 404
    assert client.get('/api/idp/.well-known/openid-configuration').status_code == 404


# ---------- 启动自检 ----------

def test_startup_check_rejects_issuer_path_mismatch(monkeypatch, tmp_path):
    """issuer 路径和路由不一致时子平台只会看到"服务发现失败"，很难反推。"""
    _patched_settings(monkeypatch, tmp_path,
                      idp_issuer='http://hub.example.com/oauth')
    with pytest.raises(idp.IdPConfigError):
        idp.startup_check()


def test_startup_check_requires_clients(monkeypatch, tmp_path):
    _patched_settings(monkeypatch, tmp_path, idp_clients_raw='')
    with pytest.raises(idp.IdPConfigError):
        idp.startup_check()
