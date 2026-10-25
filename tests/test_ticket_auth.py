"""票据验证的单元测试：只测"票据 → 可信身份"，不碰数据库、不调模型。

覆盖验收表里与签名/字段/时间相关的行：正确票据通过；过期、篡改、错误签名、
错误签发方/接收方、缺字段、超长有效期、越权声明企业 —— 全部拒绝。

时间一律注入 `now=`，不等票据自然过期。
"""
import json
import sys
import time
import unittest
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import auth_connections, ticket_auth  # noqa: E402

AUD = 'agent-hub-login'
ISS = 'https://platform-a.example.com'


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private_pem, public_pem


PRIVATE_A, PUBLIC_A = _keypair()
PRIVATE_B, PUBLIC_B = _keypair()


def connection(connection_id='conn-a', issuer=ISS, key=PUBLIC_A, kid='k1',
               tenants=('tenant-a',), platforms=('pa',), enabled=True,
               provider='idp-a'):
    return auth_connections.Connection(
        connection_id=connection_id, issuer=issuer, audience=AUD,
        algorithms=('RS256',), identity_provider_id=provider,
        allowed_tenants=tuple(tenants), allowed_platforms=tuple(platforms),
        enabled=enabled, platform_id=platforms[0] if platforms else '',
        keys={kid: key})


def claims(**overrides):
    base = {
        'iss': ISS, 'aud': AUD, 'sub': 'user-123', 'tenant_id': 'tenant-a',
        'platform_id': 'pa', 'iat': 1_700_000_000, 'exp': 1_700_000_030,
        'jti': 'ticket-1',
    }
    base.update(overrides)
    return base


def sign(payload=None, *, private=PRIVATE_A, kid='k1',
         typ='agent-login+jwt', algorithm='RS256', **headers):
    merged = {'kid': kid, 'typ': typ}
    merged.update(headers)
    return jwt.encode(payload if payload is not None else claims(),
                      private, algorithm=algorithm, headers=merged)


class TicketAuthTests(unittest.TestCase):
    """票据验证器本身的行为。"""

    def setUp(self):
        self.connections = [connection()]
        self.now = 1_700_000_010        # 票据有效期内

    def verify(self, ticket, now=None, connections=None):
        return ticket_auth.verify(ticket, now=now or self.now,
                                  connections=connections or self.connections)

    # ---- 通过 ----
    def test_valid_ticket_returns_verified_identity(self):
        identity = self.verify(sign())
        self.assertEqual(identity.subject, 'user-123')
        self.assertEqual(identity.tenant_id, 'tenant-a')
        self.assertEqual(identity.platform_id, 'pa')
        self.assertEqual(identity.jti, 'ticket-1')
        self.assertEqual(identity.connection_id, 'conn-a')
        self.assertEqual(identity.identity_provider_id, 'idp-a')
        self.assertEqual(identity.provider, 'idp-a')

    def test_same_user_gets_new_jti_each_time(self):
        """同一用户每次登录 jti 不同、sub 不变（票据不能复用）。"""
        first = self.verify(sign(claims(jti='ticket-1')))
        second = self.verify(sign(claims(jti='ticket-2')))
        self.assertNotEqual(first.jti, second.jti)
        self.assertEqual(first.subject, second.subject)

    def test_clock_skew_within_leeway_is_accepted(self):
        """5 秒时钟误差内仍然受理，否则平台与中台的机器时间差会变成掉线。"""
        self.verify(sign(claims(iat=1_700_000_010 + 3)))   # iat 略在未来
        self.verify(sign(claims(exp=1_700_000_010 - 3)))   # 刚过期 3 秒

    # ---- 拒绝：签名与算法 ----
    def test_signature_from_another_key_is_rejected(self):
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(sign(private=PRIVATE_B))
        self.assertIn('签名', ctx.exception.reason)

    def test_tampered_payload_is_rejected(self):
        import base64

        def b64decode(part: str) -> bytes:
            return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))

        def b64encode(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')

        head, payload, signature = sign().split('.')
        claims_of = json.loads(b64decode(payload))
        claims_of['sub'] = 'attacker'
        # 只换载荷、签名不动 → 必须被拒
        tampered = f'{head}.{b64encode(json.dumps(claims_of).encode())}.{signature}'
        with self.assertRaises(ticket_auth.TicketError):
            self.verify(tampered)

    def test_alg_none_is_rejected(self):
        unsigned = jwt.encode(claims(), key='', algorithm='none',
                              headers={'kid': 'k1'})
        with self.assertRaises(ticket_auth.TicketError):
            self.verify(unsigned)

    def test_hs256_with_public_key_is_rejected(self):
        """经典混淆攻击：拿公钥内容当 HMAC 密钥签 HS256。算法固定后必须失败。

        用 PyJWT 自己的底层编码来构造（`jwt.encode` 会直接拒绝这种用法），
        模拟攻击者自己拼一个 HS256 令牌。
        """
        import base64
        import hashlib
        import hmac

        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')

        header = b64(json.dumps({'alg': 'HS256', 'kid': 'k1',
                                 'typ': 'agent-login+jwt'}).encode())
        payload = b64(json.dumps(claims()).encode())
        signing_input = f'{header}.{payload}'.encode()
        # 攻击者拿"公钥文本"当 HMAC 密钥
        signature = b64(hmac.new(PUBLIC_A.encode(), signing_input,
                                 hashlib.sha256).digest())
        forged = f'{header}.{payload}.{signature}'
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(forged)
        self.assertIn('算法', ctx.exception.reason)

    def test_unknown_kid_is_rejected(self):
        """kid 指向未登记的钥匙必须拒绝。

        单把钥匙的接入允许 kid 写成"接入编号/default"这类等价写法（否则换个
        kid 拼法就登不进来），但**多把钥匙时必须精确匹配**——否则轮换期的
        旧钥匙会让新 kid 蒙混过关。
        """
        multi = auth_connections.Connection(
            connection_id='conn-a', issuer=ISS, audience=AUD,
            algorithms=('RS256',), identity_provider_id='idp-a',
            allowed_tenants=('tenant-a',), allowed_platforms=('pa',),
            keys={'k1': PUBLIC_A, 'k2': PUBLIC_B})
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(sign(kid='nope'), connections=[multi])
        self.assertIn('kid', ctx.exception.reason)
        # 登记过的那把钥匙仍然可用（k2 对应 B 的公钥，所以用 B 的私钥签）
        self.assertEqual(self.verify(sign(kid='k2', private=PRIVATE_B),
                                     connections=[multi]).subject, 'user-123')

    # ---- 拒绝：签发方 / 接收方 ----
    def test_unregistered_issuer_is_rejected(self):
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(sign(claims(iss='https://evil.example.com')))
        self.assertIn('未登记', ctx.exception.reason)

    def test_wrong_audience_is_rejected(self):
        with self.assertRaises(ticket_auth.TicketError):
            self.verify(sign(claims(aud='someone-else')))

    def test_disabled_connection_is_rejected(self):
        disabled = [connection(enabled=False)]
        with self.assertRaises(ticket_auth.TicketError):
            self.verify(sign(), connections=disabled)

    def test_platform_a_key_cannot_impersonate_platform_b(self):
        """两个独立身份源：B 的公钥签的票据，在"只登记了 A 公钥"的连接上无效。"""
        with self.assertRaises(ticket_auth.TicketError):
            self.verify(sign(private=PRIVATE_B))

    # ---- 拒绝：时间 ----
    def test_expired_ticket_is_rejected(self):
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(sign(), now=1_700_000_100)
        self.assertIn('过期', ctx.exception.reason)

    def test_ticket_from_the_future_is_rejected(self):
        with self.assertRaises(ticket_auth.TicketError):
            self.verify(sign(), now=1_600_000_000)

    def test_lifetime_over_limit_is_rejected(self):
        long_lived = sign(claims(exp=1_700_000_000 + 3600))
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(long_lived)
        self.assertIn('有效期', ctx.exception.reason)

    def test_exp_before_iat_is_rejected(self):
        with self.assertRaises(ticket_auth.TicketError):
            self.verify(sign(claims(iat=1_700_000_020, exp=1_700_000_010)))

    # ---- 拒绝：字段 ----
    def test_missing_required_claims_are_rejected(self):
        for missing in ('sub', 'jti', 'tenant_id', 'platform_id', 'exp'):
            payload = claims()
            payload.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(ticket_auth.TicketError):
                    self.verify(sign(payload))

    def test_wrong_claim_types_are_rejected(self):
        for bad in ({'sub': 123}, {'tenant_id': ['tenant-a']},
                    {'iat': '1700000000'}, {'exp': 1_700_000_030.5}):
            payload = claims(**bad)
            ticket = sign(payload, private=PRIVATE_A)
            with self.subTest(bad=bad):
                with self.assertRaises(ticket_auth.TicketError):
                    self.verify(ticket)

    def test_wrong_ticket_type_is_rejected(self):
        """别处的 JWT（例如 API 访问令牌）不能当登录票据用。"""
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(sign(typ='JWT'))
        self.assertIn('票据类型', ctx.exception.reason)

    def test_wrong_kid_is_rejected_even_with_one_key(self):
        """只登记一把公钥时，kid 写错也不能验过。"""
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(sign(kid='not-the-registered-kid'))
        self.assertIn('kid', ctx.exception.reason)

    def test_missing_ticket_type_is_rejected(self):
        """typ 必填。缺省头不能靠「没写就不检查」混进登录票据。"""
        raw = sign()
        header, payload, signature = raw.split('.')
        import json
        import base64
        padded = header + '=' * (-len(header) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        data.pop('typ', None)
        encoded = base64.urlsafe_b64encode(
            json.dumps(data, separators=(',', ':')).encode()).rstrip(b'=').decode()
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(f'{encoded}.{payload}.{signature}')
        self.assertIn('票据类型', ctx.exception.reason)

    def test_oversized_ticket_is_rejected(self):
        with self.assertRaises(ticket_auth.TicketError):
            self.verify('x' * (ticket_auth.MAX_TICKET_BYTES + 10))

    # ---- 拒绝：越权声明 ----
    def test_unauthorized_tenant_is_rejected(self):
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(sign(claims(tenant_id='tenant-evil')))
        self.assertIn('企业', ctx.exception.reason)

    def test_unauthorized_platform_is_rejected(self):
        with self.assertRaises(ticket_auth.TicketError) as ctx:
            self.verify(sign(claims(platform_id='pb')))
        self.assertIn('平台', ctx.exception.reason)

    def test_role_in_ticket_is_not_trusted_as_permission(self):
        """票据里自称 admin 也只能"透传"，权限以中台配置为准。"""
        identity = self.verify(sign(claims(role='admin')))
        self.assertEqual(identity.claimed_role, 'admin')
        self.assertEqual(identity.tenant_id, 'tenant-a')


class ConnectionConfigTests(unittest.TestCase):
    """登记表本身：配置错要报错，不能静默放行。"""

    def test_unsupported_algorithm_is_rejected(self):
        with self.assertRaises(auth_connections.ConnectionError_):
            auth_connections._build(
                {'connection_id': 'c', 'issuer': ISS,
                 'algorithms': ['HS256'], 'public_key': PUBLIC_A}, ROOT)

    def test_missing_public_key_is_rejected(self):
        with self.assertRaises(auth_connections.ConnectionError_):
            auth_connections._build(
                {'connection_id': 'c', 'issuer': ISS}, ROOT)

    def test_missing_required_fields_are_rejected(self):
        for entry in ({'issuer': ISS, 'public_key': PUBLIC_A},
                      {'connection_id': 'c', 'public_key': PUBLIC_A}):
            with self.subTest(entry=entry):
                with self.assertRaises(auth_connections.ConnectionError_):
                    auth_connections._build(entry, ROOT)

    def test_multiple_keys_require_kid(self):
        conn = auth_connections.Connection(
            connection_id='c', issuer=ISS, keys={'k1': PUBLIC_A, 'k2': PUBLIC_B})
        with self.assertRaises(auth_connections.ConnectionError_):
            auth_connections.public_key(conn, '')
        self.assertEqual(auth_connections.public_key(conn, 'k2'), PUBLIC_B)

    def test_connection_file_is_loaded_and_scoped(self):
        import os
        import tempfile
        from unittest.mock import patch

        entry = {'connection_id': 'file-conn', 'issuer': ISS,
                 'audience': AUD, 'algorithms': ['RS256'],
                 'public_key': PUBLIC_A, 'allowed_tenants': ['tenant-a'],
                 'identity_provider_id': 'idp-file', 'enabled': True}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'connections.json'
            path.write_text(json.dumps([entry]), encoding='utf-8')
            with patch.dict(os.environ, {'AUTH_CONNECTIONS_FILE': str(path),
                                         'AUTH_CONNECTIONS': ''}, clear=False):
                loaded = auth_connections.load_connections()
        self.assertEqual([c.connection_id for c in loaded], ['file-conn'])
        self.assertTrue(loaded[0].allows_tenant('tenant-a'))
        self.assertFalse(loaded[0].allows_tenant('tenant-b'))
        self.assertEqual(loaded[0].identity_provider_id, 'idp-file')

    def test_bad_json_fails_loudly(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'AUTH_CONNECTIONS': '[{oops',
                                     'AUTH_CONNECTIONS_FILE': ''}, clear=False):
            with self.assertRaises(auth_connections.ConnectionError_):
                auth_connections.load_connections()


if __name__ == '__main__':
    unittest.main()
