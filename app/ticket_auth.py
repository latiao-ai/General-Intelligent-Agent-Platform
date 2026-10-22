"""登录票据验证：**原始票据 → 验证 → 可信身份对象**。

只做这一件事：不调模型、不读聊天记录、不建会话。分工是刻意的——把"证明你是谁"
和"建立你的登录态"分开，才能各自测透。

验证顺序（每一步都不能省，顺序本身也是安全边界）：

1. 限制票据大小，只解析**未验签**头部里的 `iss`/`alg`/`kid` 用于查表；
2. 在服务端登记表里找匹配的接入（查不到 → 直接拒绝，不再往下解析任何字段）；
3. 按该接入与该 `kid` 取**可信公钥**（公钥只来自登记表，绝不来自票据）；
4. 校验签名，并**固定允许的算法**（拒绝 `alg=none`/`HS256` 混淆这类攻击）；
5. 校验 `iss`、`aud`、`exp`/`iat`（含少量时钟误差）、必填字段类型；
6. 校验票据类型、最大有效时长、企业/平台是否在该接入的授权范围内；
7. 返回 `VerifiedIdentity`——**只有走到这里的字段才可以用来建立身份**。

`AUD`/`ISS` 的比较一律精确匹配：不做前缀、不做忽略大小写。
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt

from app import auth_connections
from app.auth_connections import Connection
from app.config import settings

logger = logging.getLogger(__name__)

# 票据类型：专用 type 头，避免把别处的 JWT 拿来当登录票据
TICKET_TYPE = 'agent-login+jwt'
# 票据本体上限：正常票据 1~2KB，超过这个量级的是攻击或配置错误
MAX_TICKET_BYTES = 8 * 1024
# 允许的时钟误差（秒）
LEEWAY_SECONDS = 5
REQUIRED_CLAIMS = ('iss', 'aud', 'sub', 'iat', 'exp', 'jti',
                   'tenant_id', 'platform_id')


class TicketError(Exception):
    """票据不可信。`reason` 只给服务端日志与测试用，不直接回给浏览器。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedIdentity:
    """验签通过后的可信身份。**业务侧只认这个对象**。"""

    issuer: str
    subject: str
    tenant_id: str
    platform_id: str
    jti: str
    issued_at: int
    expires_at: int
    connection_id: str
    identity_provider_id: str
    audience: str
    display_name: str = ''
    email: str = ''
    # 票据里可能带角色声明，但**权限以中台配置为准**：这里只是透传，不参与授权
    claimed_role: str = ''

    @property
    def provider(self) -> str:
        """身份绑定的隔离键：身份源，不是接入连接。"""
        return self.identity_provider_id


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _peek_header(ticket: str) -> dict:
    """只读未验签的头部。**这些值仅用于查配置，不得用于建立身份。**"""
    try:
        return jwt.get_unverified_header(ticket)
    except jwt.InvalidTokenError as exc:
        raise TicketError(f'头部不可解析：{exc}') from exc


def _peek_claims(ticket: str) -> dict:
    try:
        return jwt.decode(ticket, options={'verify_signature': False,
                                          'verify_exp': False,
                                          'verify_aud': False})
    except jwt.InvalidTokenError as exc:
        raise TicketError(f'载荷不可解析：{exc}') from exc


def _require_str(claims: dict, name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TicketError(f'{name} 缺失或不是非空字符串')
    return value.strip()


def _require_int(claims: dict, name: str) -> int:
    value = claims.get(name)
    # bool 是 int 的子类，得单独挡掉（True 会被当成 1）
    if isinstance(value, bool) or not isinstance(value, int):
        raise TicketError(f'{name} 缺失或不是整数时间戳')
    return value


def verify(ticket: str, *, now: int | None = None,
           connections: list[Connection] | None = None) -> VerifiedIdentity:
    """验证票据并返回可信身份。任何可疑都抛 `TicketError`。

    `now` 可注入，测试里固定时间，不用等票据自然过期。
    """
    if not isinstance(ticket, str) or not ticket.strip():
        raise TicketError('票据为空')
    if len(ticket.encode('utf-8')) > MAX_TICKET_BYTES:
        raise TicketError(f'票据过大（>{MAX_TICKET_BYTES} 字节）')
    ticket = ticket.strip()

    header = _peek_header(ticket)
    alg = header.get('alg')
    typ = header.get('typ')
    kid = str(header.get('kid') or '')
    if typ and typ != TICKET_TYPE:
        raise TicketError(f'票据类型不对：{typ!r}，应为 {TICKET_TYPE!r}')
    if not alg:
        raise TicketError('头部缺少 alg')

    # ── 1) 先用未验签的 iss 找到登记过的接入。找不到就到此为止。
    claims = _peek_claims(ticket)
    issuer = str(claims.get('iss') or '').strip()
    audience = claims.get('aud')
    if isinstance(audience, list):
        audience = next((str(a) for a in audience
                         if isinstance(a, str)), '')
    audience = str(audience or '').strip()
    if not issuer:
        raise TicketError('缺少 iss')

    candidates = connections if connections is not None else \
        auth_connections.connections()
    matched = [item for item in candidates
               if item.enabled and item.issuer == issuer
               and item.audience == audience]
    if not matched:
        raise TicketError(
            f'签发方未登记或不接受该接收方：iss={issuer!r} aud={audience!r}')

    # 同一个签发方可能登记了多条接入（不同企业/平台/身份源）。这里先用**未验签**
    # 的企业/平台做**筛选**——它只决定"试哪把钥匙"，不构成信任：接下来仍要验签，
    # 验签之后再用已验证的声明复核一次授权范围。
    claimed_tenant = str(claims.get('tenant_id') or '')
    claimed_platform = str(claims.get('platform_id') or '')
    scoped = [item for item in matched
              if item.allows_tenant(claimed_tenant)
              and item.allows_platform(claimed_platform)]
    if not scoped:
        raise TicketError(
            f'该签发方无权声明企业 {claimed_tenant!r} / 平台 {claimed_platform!r}')

    # ── 2) 按接入 + kid 取可信公钥，只允许登记过的算法
    connection = scoped[0]
    if alg not in connection.algorithms or alg not in auth_connections.ALLOWED_ALGORITHMS:
        raise TicketError(f'算法不被允许：{alg}')
    try:
        pem = auth_connections.public_key(connection, kid)
    except auth_connections.ConnectionError_ as exc:
        raise TicketError(str(exc)) from exc

    # ── 3) 验签 + 标准声明校验（iss/aud/exp/iat 由库校验，含 leeway）
    #
    # `now` 是给测试用的：注入固定时间时关掉库内的时间校验，由下面第 4 步用
    # 同样的规则自己判断——否则"固定时间"根本测不了，只能等票据真过期。
    injected = now is not None
    try:
        verified = jwt.decode(
            ticket, pem, algorithms=[alg], audience=connection.audience,
            issuer=connection.issuer, leeway=LEEWAY_SECONDS,
            options={'require': ['iss', 'aud', 'sub', 'iat', 'exp', 'jti'],
                     'verify_signature': True,
                     'verify_exp': not injected, 'verify_iat': not injected,
                     'verify_aud': True, 'verify_iss': True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TicketError('票据已过期') from exc
    except jwt.ImmatureSignatureError as exc:
        raise TicketError('票据尚未生效（iat 在未来）') from exc
    except jwt.InvalidAudienceError as exc:
        raise TicketError('接收方不匹配') from exc
    except jwt.InvalidIssuerError as exc:
        raise TicketError('签发方不匹配') from exc
    except jwt.InvalidSignatureError as exc:
        raise TicketError('签名无效') from exc
    except jwt.InvalidAlgorithmError as exc:
        raise TicketError(f'算法不被允许：{alg}') from exc
    except jwt.MissingRequiredClaimError as exc:
        raise TicketError(f'缺少必需声明：{exc}') from exc
    except jwt.InvalidTokenError as exc:
        raise TicketError(f'票据不可信：{exc}') from exc

    # ── 4) 字段类型、专用类型、时长、授权范围
    missing = [name for name in REQUIRED_CLAIMS if name not in verified]
    if missing:
        raise TicketError(f'缺少必需声明：{", ".join(missing)}')
    subject = _require_str(verified, 'sub')
    tenant_id = _require_str(verified, 'tenant_id')
    platform_id = _require_str(verified, 'platform_id')
    jti = _require_str(verified, 'jti')
    issued_at = _require_int(verified, 'iat')
    expires_at = _require_int(verified, 'exp')
    if expires_at <= issued_at:
        raise TicketError('exp 必须晚于 iat')
    lifetime = expires_at - issued_at
    if lifetime > settings.auth_ticket_max_lifetime:
        raise TicketError(
            f'有效期过长（{lifetime}s > {settings.auth_ticket_max_lifetime}s）')

    current = _now_ts() if now is None else int(now)
    if issued_at > current + LEEWAY_SECONDS:
        raise TicketError('iat 在未来')
    if expires_at < current - LEEWAY_SECONDS:
        raise TicketError('票据已过期')

    # 用**已验证**的声明复核授权范围（前面的筛选用的是未验签字段）
    if not connection.allows_tenant(tenant_id):
        raise TicketError(
            f'接入 {connection.connection_id} 无权声明企业 {tenant_id}')
    if not connection.allows_platform(platform_id):
        raise TicketError(
            f'接入 {connection.connection_id} 无权声明平台 {platform_id}')
    if connection.platform_id and platform_id != connection.platform_id \
            and connection.allowed_platforms and \
            platform_id not in connection.allowed_platforms:
        raise TicketError('平台标识与接入不一致')

    name = verified.get('name') or verified.get('display_name') or ''
    email = verified.get('email') or ''
    role = verified.get('role') or ''
    for label, value in (('name', name), ('email', email), ('role', role)):
        if value and not isinstance(value, str):
            raise TicketError(f'{label} 必须是字符串')

    return VerifiedIdentity(
        issuer=connection.issuer,
        subject=subject,
        tenant_id=tenant_id,
        platform_id=platform_id,
        jti=jti,
        issued_at=issued_at,
        expires_at=expires_at,
        connection_id=connection.connection_id,
        identity_provider_id=connection.identity_provider_id,
        audience=connection.audience,
        display_name=str(name)[:120],
        email=str(email)[:200],
        claimed_role=str(role),
    )
