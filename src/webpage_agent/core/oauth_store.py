"""OAuth 凭据的落盘与刷新：一人一份，按主体隔离。

为什么要单独一层，而不是复用 `credentials.credential_ref` 直接读文件：

- OAuth 的凭据是**一份结构**（client_id、refresh_token、过期时间），不是一行
  token。`credential_ref` 的 `file:` 约定是"文件内容就是密钥"，套上去会把
  `Bearer <整段 JSON>` 发出去。
- 取 token 之前要先判断过期、必要时刷新，这一步是**异步**的，而
  `materialize_credential` 是同步函数。

所以分工是：`credential_bindings` 仍然记"谁在哪个服务端上有凭据"，
`credential_ref` 指向本模块写的 JSON 文件；明文只在传输层短时取出。
`mcp_connections.bearer_for` 看到 `scope['auth'] == 'oauth'` 就转到本模块。

三条纪律（与 `core/credentials.py` 一致）：

1. **一人一份**：文件名带 `tenant__user__server`，绝不共用；跨主体取用会被
   `CredentialBinding.matches()` 挡住。
2. **文件权限 0600、目录 0700**：token 等价于账号密码。
3. **不进日志、不进事件**：错误文案只说状态码与原因，不回显 token。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from webpage_agent.core import credentials as credentials_module
from webpage_agent.core.config import settings
from webpage_agent.integrations import oauth_client

# 提前 60 秒判定过期：避免"取出来时还有 200ms 有效期"这种必然失败的请求。
EXPIRY_SKEW_MS = 60_000

# 每个主体一把锁：dict 本身只在事件循环线程里读写，不需要额外守卫。
_LOCKS: dict[str, asyncio.Lock] = {}


class OAuthAuthorizationRequired(credentials_module.CredentialMissing):
    """还没有授权（或已被撤销/刷新失败），需要用户去授权页走一次。

    单独一个类型是为了让 API 能区分"缺凭据"和"该弹授权"——两者对用户是
    完全不同的动作：前者要找运维，后者点一下就好。
    """

    def __init__(self, message: str, *, server_id: str = '',
                 authorize_url: str = '') -> None:
        super().__init__(message)
        self.server_id = server_id
        self.authorize_url = authorize_url


def _slug(value: str, limit: int = 60) -> str:
    text = re.sub(r'[^0-9A-Za-z_.-]', '_', str(value or ''))[:limit]
    return text or 'default'


def storage_dir() -> Path:
    return Path(settings.oauth_dir)


def credentials_path(*, tenant_id: str, user_id: str, server_id: str) -> Path:
    name = f'{_slug(tenant_id)}__{_slug(user_id)}__{_slug(server_id)}.json'
    return storage_dir() / name


def load(*, tenant_id: str, user_id: str, server_id: str) -> dict:
    path = credentials_path(tenant_id=tenant_id, user_id=user_id, server_id=server_id)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(*, tenant_id: str, user_id: str, server_id: str, data: dict) -> Path:
    path = credentials_path(tenant_id=tenant_id, user_id=user_id, server_id=server_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(oauth_client.dumps(data), encoding='utf-8')
    try:
        os.chmod(tmp, 0o600)
    except OSError:                                  # pragma: no cover - 非 POSIX
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:                                  # pragma: no cover
        pass
    return path


def clear(*, tenant_id: str, user_id: str, server_id: str) -> None:
    try:
        credentials_path(tenant_id=tenant_id, user_id=user_id,
                         server_id=server_id).unlink()
    except OSError:
        pass


def has_token(*, tenant_id: str, user_id: str, server_id: str) -> bool:
    """有没有**可用**凭据：有 access_token，或其过期了但还有 refresh_token。"""
    data = load(tenant_id=tenant_id, user_id=user_id, server_id=server_id)
    tokens = data.get('tokens') or {}
    if tokens.get('access_token'):
        return True
    return bool(tokens.get('refresh_token'))


def save_tokens(*, tenant_id: str, user_id: str, server_id: str,
                base: dict, tokens: dict, scope: str = '') -> dict:
    """把一次授权/刷新的结果并进凭据文件。"""
    data = dict(base or {})
    merged = {**(data.get('tokens') or {}), **tokens}
    data['tokens'] = merged
    if scope:
        data['scope'] = scope
    data['service'] = data.get('service') or ''
    save(tenant_id=tenant_id, user_id=user_id, server_id=server_id, data=data)
    return data


def _fresh(tokens: dict) -> bool:
    access = str(tokens.get('access_token') or '')
    if not access:
        return False
    expires_at = tokens.get('expires_at')
    try:
        stamp = int(expires_at or 0)
    except (TypeError, ValueError):
        stamp = 0
    if stamp <= 0:
        # 对方没给 expires_in：当它不过期，等真的 401 再强制刷新。
        return True
    return stamp - EXPIRY_SKEW_MS > int(time.time() * 1000)


def _lock(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


async def _refresh(creds: dict, *, server, tenant_id: str, user_id: str,
                   server_id: str) -> str:
    """用 refresh_token 换新 access_token 并写回。失败就清凭据、要求重新授权。"""
    tokens = creds.get('tokens') or {}
    refresh_token = str(tokens.get('refresh_token') or '')
    if not refresh_token:
        raise OAuthAuthorizationRequired(
            f'「{server.title or server_id}」的授权已过期且没有 refresh_token，请重新授权',
            server_id=server_id)
    issuer = str(creds.get('issuer') or getattr(server, 'oauth_issuer', '') or '')
    meta = await oauth_client.discover(issuer)
    resource = (str(creds.get('service') or '')
                or getattr(server, 'oauth_resource', '')
                or getattr(server, 'url', ''))
    try:
        payload = await oauth_client.refresh(
            meta,
            refresh_token=refresh_token,
            client_id=str(creds.get('client_id') or ''),
            client_secret=str(creds.get('client_secret') or ''),
            resource=resource)
    except oauth_client.OAuthError as exc:
        # 4xx 说明 refresh_token 被撤销/轮换失效，继续留着只会每次失败。
        if 400 <= exc.status < 500:
            clear(tenant_id=tenant_id, user_id=user_id, server_id=server_id)
            raise OAuthAuthorizationRequired(
                f'「{server.title or server_id}」的授权已失效，请重新授权：{exc}',
                server_id=server_id) from exc
        raise
    save_tokens(tenant_id=tenant_id, user_id=user_id, server_id=server_id,
                base=creds, tokens=payload)
    credentials_module.invalidate_cache()
    return payload['access_token']


async def ensure_access_token(*, server, tenant_id: str,
                              user_id: str, server_id: str) -> str:
    """取这个主体在这个服务端上的 access_token，必要时先刷新。

    同一主体并发请求时会撞在一起刷新（refresh_token 通常一次性轮换，
    并发刷新会让其中一个拿到已作废的 token）。所以按主体加锁，锁内二次检查。
    """
    key = f'{tenant_id}:{user_id}:{server_id}'
    async with _lock(key):
        creds = load(tenant_id=tenant_id, user_id=user_id, server_id=server_id)
        if not creds:
            raise OAuthAuthorizationRequired(
                f'「{server.title or server_id}」还没有授权，请先在连接器面板点「授权」',
                server_id=server_id)
        tokens = creds.get('tokens') or {}
        if _fresh(tokens):
            return str(tokens.get('access_token') or '')
        return await _refresh(creds, server=server, tenant_id=tenant_id,
                              user_id=user_id, server_id=server_id)


def summary(*, tenant_id: str, user_id: str, server_id: str) -> dict:
    """给前端/排障看的元信息：不含 token，只回答"有没有、什么时候到期"。"""
    data = load(tenant_id=tenant_id, user_id=user_id, server_id=server_id)
    tokens = data.get('tokens') or {}
    expires_at = tokens.get('expires_at')
    return {
        'authorized': bool(tokens.get('access_token')),
        'has_refresh': bool(tokens.get('refresh_token')),
        'scope': str(tokens.get('scope') or data.get('scope') or ''),
        'expires_at': int(expires_at) if isinstance(expires_at, (int, float)) else 0,
        'obtained_at': str(tokens.get('obtained_at') or ''),
    }
