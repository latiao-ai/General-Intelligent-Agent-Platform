"""外部身份换取本地主体：token → 平台 userinfo → 本地 user_id。

这是 `policies.current_principal()` 的真实实现，也是"用合法凭证调平台接口拿用户
信息"这条链路的全部。三条纪律，缺一条都会在后面变成事故：

1. **平台 token 只在请求进入时用一次**。不落库、不进 prompt、不传给 MCP server。
   它证明的是"你是谁"，不是"你能用什么"。
2. **业务外键只挂本地 `user_id`**。平台字段（openid / sub / 邮箱）只用来**找到或
   新建**本地身份，因为平台侧标识会变：微信 openid 按 appid 隔离，某些平台的
   `sub` 也会轮换。挂错主键的后果是用户换个入口登录，历史上下文就断了。
3. **缓存按 token 摘要，不存明文**。进程内 TTL 缓存，默认 60 秒。

`AUTH_MODE`：

| 值 | 行为 | 用途 |
|---|---|---|
| `off`（默认） | 不介入，保持单用户行为 | 本地开发、现有测试 |
| `optional` | 带了 token 就验证，没带就匿名 | 灰度期，旧客户端不受影响 |
| `token` | 必须带合法 token，否则 401 | 正式启用 |

凭证从哪里取：优先 `Authorization: Bearer`，其次 cookie。**cookie 不是可选项**——
浏览器的 `EventSource` 和 `<a download>` 都发不出自定义请求头，只认 header 的话
SSE 事件流和产物下载会全部 401。
"""
import hashlib
import logging
import time
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException, Request

from app.config import settings

logger = logging.getLogger(__name__)

# 缓存：token 摘要 → (过期时间戳, UserInfo)。只存摘要，原文不留在进程里。
_CACHE: dict[str, tuple[float, 'UserInfo']] = {}
_MAX_CACHE = 2000

_client: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class UserInfo:
    """平台 userinfo 的归一化结果。raw 保留原文，便于接新平台时排错。"""
    provider: str
    subject: str
    email: str = ''
    display_name: str = ''
    raw: dict = field(default_factory=dict)


class AuthError(Exception):
    """凭证无效或身份服务不可用。调用方统一转成 401。"""


def http_client() -> httpx.AsyncClient:
    """复用一条连接：每个请求新建客户端会反复握手，登录高峰期很明显。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.auth_timeout)
    return _client


async def aclose() -> None:
    """进程退出时调用，避免连接池靠 GC 收。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def token_from_request(request: Request) -> str:
    """取凭证：Authorization 头优先，其次 cookie。"""
    header = request.headers.get('authorization', '')
    if header.lower().startswith('bearer '):
        return header[7:].strip()
    return (request.cookies.get(settings.auth_cookie_name) or '').strip()


def _pick(payload: dict, path: str) -> str:
    """按字段名取值，支持 `a.b` 这种嵌套（有些平台把 uid 放在 data 里）。"""
    cur = payload
    for part in path.split('.'):
        if not isinstance(cur, dict):
            return ''
        cur = cur.get(part)
    return '' if cur is None else str(cur)


async def fetch_userinfo(token: str) -> UserInfo:
    """调平台 userinfo 端点，把返回归一化成 UserInfo。

    失败一律抛 AuthError：401/403 是凭证问题，5xx/超时是平台问题，
    但对调用方都只能表现为"这次没能证明你是谁"，不能放行。
    """
    if not token:
        raise AuthError('缺少凭证')

    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    stamp = time.monotonic()
    hit = _CACHE.get(digest)
    if hit and hit[0] > stamp:
        return hit[1]

    url = settings.auth_userinfo_url
    if not url:
        raise AuthError('未配置 AUTH_USERINFO_URL，无法校验凭证')

    try:
        if settings.auth_token_in == 'query':
            # 少数平台（如微信公众号）只接受 query 里的 access_token
            resp = await http_client().get(url, params={'access_token': token})
        else:
            resp = await http_client().get(
                url, headers={'Authorization': f'Bearer {token}'})
    except httpx.HTTPError as exc:
        # 平台不可达不能当成"凭证无效"之外的例外放过去，但也要能区分，
        # 否则线上排查分不清是用户 token 过期还是对方挂了。
        raise AuthError(f'身份服务不可达：{exc.__class__.__name__}') from exc

    if resp.status_code in (401, 403):
        raise AuthError('凭证无效或已过期')
    if resp.status_code >= 400:
        raise AuthError(f'身份服务返回 {resp.status_code}')

    try:
        payload = resp.json()
    except ValueError as exc:
        raise AuthError('身份服务返回的不是 JSON') from exc
    if not isinstance(payload, dict):
        raise AuthError('身份服务返回结构不是对象')

    subject = _pick(payload, settings.auth_subject_field).strip()
    if not subject:
        raise AuthError(f'身份服务未返回主体字段 {settings.auth_subject_field}')

    info = UserInfo(
        provider=settings.auth_provider,
        subject=subject,
        email=_pick(payload, settings.auth_email_field),
        display_name=_pick(payload, settings.auth_name_field),
        raw=payload,
    )
    _CACHE[digest] = (stamp + settings.auth_cache_ttl, info)
    if len(_CACHE) > _MAX_CACHE:
        for key in [k for k, v in _CACHE.items() if v[0] <= stamp]:
            _CACHE.pop(key, None)
    return info


async def resolve(request: Request):
    """请求 → Principal。`policies.current_principal()` 转调这里。

    延迟导入 `app.policies`：policies 在模块级 import 本模块的话会成环，
    而 Principal 的构造又必须用它的定义。
    """
    from app import policies

    mode = settings.auth_mode
    token = token_from_request(request)

    if mode == 'off' or (mode == 'optional' and not token):
        # 单用户行为原样保留：请求头仍然只是"声明"，不是认证
        return policies.Principal(
            tenant_id=request.headers.get('x-dsh-tenant') or policies.DEFAULT_TENANT,
            user_id=request.headers.get('x-dsh-user') or policies.DEFAULT_USER)

    if not token:
        raise HTTPException(status_code=401, detail='未登录',
                            headers={'WWW-Authenticate': 'Bearer'})

    store = getattr(request.app.state, 'store', None)
    if store is None:
        # 仓储没就绪时不能放行，也不能报 500 让人去猜
        raise HTTPException(status_code=503, detail='身份仓储未就绪')

    try:
        info = await fetch_userinfo(token)
    except AuthError as exc:
        logger.info('凭证校验失败：%s', exc)
        raise HTTPException(status_code=401, detail=str(exc),
                            headers={'WWW-Authenticate': 'Bearer'}) from exc

    row = await store.bind_identity(
        info.provider, info.subject,
        email=info.email, display_name=info.display_name)

    return policies.Principal(
        tenant_id=row.get('tenant_id') or policies.DEFAULT_TENANT,
        user_id=row['user_id'],
        role=row.get('role') or 'member')
