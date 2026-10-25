"""现场上下文：当前项目和选中对象必须由平台后端签名，不能由浏览器自报。

登录票据证明人是谁。这份证明只证明「平台认可这些现场信息」，
类型和 audience 与登录票据分开，不能拿登录票当现场证明用。
"""
import hashlib
import json

import jwt

from app import auth_connections
from app import ticket_auth

SCENE_TYPE = 'agent-scene+jwt'
SCENE_AUDIENCE = 'agent-hub-scene'


class SceneError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def canonical_hash(context: dict | None) -> str:
    body = json.dumps(context or {}, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), default=str)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def verify(assertion: str, context: dict, *, tenant_id: str) -> dict:
    """验签并核对租户与上下文哈希。返回可落库的快照，不含密钥。"""
    if not assertion or not str(assertion).strip():
        raise SceneError('缺少现场上下文证明')
    if not isinstance(context, dict):
        raise SceneError('现场上下文必须是对象')
    try:
        header = jwt.get_unverified_header(assertion)
    except jwt.InvalidTokenError as exc:
        raise SceneError(f'现场证明头部不可解析：{exc}') from exc
    if header.get('typ') != SCENE_TYPE:
        raise SceneError(f'现场证明类型不对：{header.get("typ")!r}')
    try:
        unverified = jwt.decode(
            assertion, options={'verify_signature': False, 'verify_aud': False,
                                'verify_exp': False})
    except jwt.InvalidTokenError as exc:
        raise SceneError(f'现场证明载荷不可解析：{exc}') from exc
    issuer = str(unverified.get('iss') or '')
    matches = [item for item in auth_connections.connections()
               if item.enabled and item.issuer == issuer]
    if not matches:
        raise SceneError(f'现场证明的签发方未登记：{issuer!r}')
    connection = matches[0]
    kid = str(header.get('kid') or '')
    try:
        pem = auth_connections.public_key(connection, kid)
    except auth_connections.ConnectionError_ as exc:
        raise SceneError(str(exc)) from exc
    alg = header.get('alg') or 'RS256'
    try:
        claims = jwt.decode(
            assertion, pem, algorithms=[alg], audience=SCENE_AUDIENCE,
            issuer=issuer, leeway=ticket_auth.LEEWAY_SECONDS)
    except jwt.InvalidTokenError as exc:
        raise SceneError(f'现场证明无效：{exc}') from exc
    if str(claims.get('tenant_id') or '') != tenant_id:
        raise SceneError('现场证明的企业与当前登录不一致')
    expected = canonical_hash(context)
    if str(claims.get('ctx') or '') != expected:
        raise SceneError('现场上下文与证明不一致')
    if not connection.allows_tenant(tenant_id):
        raise SceneError('该签发方无权声明这个企业的现场')
    return {
        'verified': True,
        'issuer': issuer,
        'tenant_id': tenant_id,
        'platform_id': str(claims.get('platform_id') or ''),
        'subject': str(claims.get('sub') or ''),
        'context_hash': expected,
        'context': context,
    }
