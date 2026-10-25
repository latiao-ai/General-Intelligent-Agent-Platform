"""中台登录：**平台签票 → 中台验票 → 换中台自己的登录会话**。

契约分三段，各自只做一件事：

| 段 | 谁做 | 职责 |
|---|---|---|
| 证明身份 | 客户平台后端 | 用它已建立的登录态签一张短期票据（`app/ticket_auth.py` 验证） |
| 换会话 | 中台 `/api/auth/exchange` | 验票 → 一次性登记 → 绑定身份 → 建中台会话（同一个事务） |
| 认身份 | 中台全部业务接口 | 只认中台会话凭证，**不再信任任何请求头/外部票据** |

三条边界：

1. **票据只能兑一次**，且只能拿来兑换，不能当业务凭证用；
2. **库里没有明文**：票据不落库，会话凭证只存 SHA-256 摘要；
3. 身份 = 企业 + 身份源 + 外部用户 → 中台 `user_id`；中台 `user_id` 终身不变，
   业务外键只挂它。

`AUTH_MODE`：

| 值 | 行为 |
|---|---|
| `off`（默认） | 不介入：单用户本地开发，`X-DSH-User` 仍可作为声明 |
| `optional` | 有中台会话就按会话；没有就匿名（灰度期） |
| `ticket` | 必须兑换拿到中台会话，否则 401。**正式环境用这个** |

会话凭证走 cookie：浏览器的 `EventSource` 与 `<a download>` 发不出自定义请求头，
只认 header 的话 SSE 和产物下载会全部 401。
"""
import hashlib
import logging
import secrets
import time

from fastapi import HTTPException, Request

from app import ticket_auth
from app.config import settings

logger = logging.getLogger(__name__)


class ExchangeError(Exception):
    """票据兑换失败。`status` 给接口层决定状态码。"""

    def __init__(self, reason: str, status: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


# ---------- 会话凭证 ----------

def new_session_token() -> str:
    """密码学安全的随机凭证（32 字节 → 43 字符 URL-safe）。"""
    return secrets.token_urlsafe(32)


def digest_of(token: str) -> str:
    """只存摘要：数据库泄露也拿不到可用凭证。"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def session_token_from_request(request: Request) -> str:
    return (request.cookies.get(settings.auth_session_cookie) or '').strip()


# ---------- 兑换 ----------

async def exchange(ticket: str, store, *, user_agent: str = '',
                   remote_addr: str = '') -> dict:
    """验证票据并建立中台登录会话；返回会话凭证与身份。

    顺序：验票 → （同一事务）登记一次性使用 + 绑定身份 + 建会话。
    验票失败不写任何数据；事务中途失败整笔回滚，不留半成品。
    """
    if store is None:
        raise ExchangeError('身份仓储未就绪', status=503)
    try:
        identity = ticket_auth.verify(ticket)
    except ticket_auth.TicketError as exc:
        logger.info('票据验证失败：%s', exc.reason)
        raise ExchangeError(f'票据无效：{exc.reason}', status=401) from exc

    token = new_session_token()
    session_id = 'as_' + secrets.token_hex(12)
    expires_at = time.time() + settings.auth_session_ttl
    try:
        row = await store.redeem_ticket(
            issuer=identity.issuer, jti=identity.jti,
            expires_at=identity.expires_at,
            connection_id=identity.connection_id,
            platform_id=identity.platform_id,
            tenant_id=identity.tenant_id,
            identity_provider_id=identity.identity_provider_id,
            subject=identity.subject,
            session_id=session_id,
            session_digest=digest_of(token),
            session_expires_at=expires_at,
            email=identity.email, display_name=identity.display_name,
            user_agent=user_agent, remote_addr=remote_addr)
    except Exception as exc:                                  # noqa: BLE001
        if type(exc).__name__ == 'TicketAlreadyUsed':
            logger.info('票据重复兑换：iss=%s jti=%s', identity.issuer,
                        identity.jti)
            raise ExchangeError('票据已被使用，请重新申请', status=409) from exc
        logger.exception('兑换事务失败')
        raise ExchangeError('登录失败，请重试', status=503) from exc

    logger.info('登录成功 user=%s tenant=%s platform=%s connection=%s',
                row['user_id'], identity.tenant_id, identity.platform_id,
                identity.connection_id)
    return {'token': token,
            'auth_session_id': row['auth_session_id'],
            'expires_at': expires_at,
            'user_id': row['user_id'],
            'tenant_id': row['tenant_id'],
            'role': row.get('role') or 'member',
            'platform_id': identity.platform_id,
            'connection_id': identity.connection_id}


async def revoke(request: Request, store) -> bool:
    """退出登录：撤销数据库会话（cookie 由接口层清）。"""
    token = session_token_from_request(request)
    if not token or store is None:
        return False
    row = await store.auth_session(digest_of(token))
    if row is None:
        return False
    return await store.revoke_auth_session(row['auth_session_id'])


# ---------- 请求 → 主体 ----------

async def principal_from_session(request: Request, store):
    """按中台会话解析主体。返回 None = "没有会话"，由调用方决定 401 还是匿名。"""
    from app import policies

    token = session_token_from_request(request)
    if not token:
        return None
    row = await store.auth_session(digest_of(token))
    if row is None:
        return None
    if row.get('revoked_at'):
        raise HTTPException(status_code=401, detail='登录已失效')
    if float(row.get('expires_at') or 0) < time.time():
        raise HTTPException(status_code=401, detail='登录已过期')
    user = await store.user(row['user_id'])
    if user and str(user.get('status') or 'active') != 'active':
        # 用户被停用：会话立刻失效，不等它自己过期
        await store.revoke_user_sessions(row['user_id'])
        raise HTTPException(status_code=401, detail='账号已停用')

    return policies.Principal(
        tenant_id=row['tenant_id'] or policies.DEFAULT_TENANT,
        user_id=row['user_id'],
        role=row.get('role') or 'member',
        platform_id=row.get('platform_id') or '',
        auth_session_id=row['auth_session_id'],
        authenticated=True)


async def resolve(request: Request):
    """请求 → Principal。`policies.current_principal()` 转调这里。

    ticket 模式下**只看中台会话**：请求头里填什么都不影响身份。
    """
    from app import policies

    mode = settings.auth_mode
    store = getattr(request.app.state, 'store', None)

    if mode == 'off':
        # 本地开发/既有测试：不介入认证。请求头只是"声明"，**绝不能**用在
        # ticket 模式——那条路已经在上面被 401 拦死了。
        return policies.Principal(
            tenant_id=request.headers.get('x-dsh-tenant') or policies.DEFAULT_TENANT,
            user_id=request.headers.get('x-dsh-user') or policies.DEFAULT_USER)

    if mode in ('ticket', 'optional'):
        if store is None:
            raise HTTPException(status_code=503, detail='身份仓储未就绪')
        principal = await principal_from_session(request, store)
        if principal is not None:
            return principal
        if mode == 'ticket':
            raise HTTPException(status_code=401, detail='未登录',
                                headers={'WWW-Authenticate': 'Ticket'})
        # optional：没会话就匿名。注意**不接受请求头声明**，那条口子只留给 off 模式，
        # 否则"改个 X-DSH-User 就换身份"会一直存在。
        return policies.Principal()
    raise HTTPException(status_code=500, detail=f'未知 AUTH_MODE：{mode}')
