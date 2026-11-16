"""本地账号登录的业务层：密码哈希 + 校验编排。设计见 docs/local-account-auth.md。

和 `platform_login.py` 的分工一样：这一层只负责「证明身份」，路由层负责
发 cookie，`store.login_platform_identity()` 负责建会话。中台以前从不存密码，
打开这条链路后密码哈希落在 `local_accounts`，所以这里的每一条安全约束都不是
可选的：常量时间比较、dummy verify、双维度限流、失败锁定、脱敏日志。

密码算法用标准库 `hashlib.scrypt`（项目没有 argon2/bcrypt 依赖）。参数编码进
哈希字符串本身，将来调整成本或换算法时，登录成功会透明重算，不需要全量重置。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
import unicodedata

from webpage_agent.api import auth
from webpage_agent.core.config import settings

logger = logging.getLogger(__name__)

N, R, P, DK_LEN = 2**14, 8, 1, 32
# 与 local_accounts.password_algo 的 DDL 默认值保持一致。不 import store 模块：
# api → store → pg_store → store mixin 会形成循环导入。
ALGO = 'scrypt-16384-8-1'
# 账号不存在时也跑满一次 scrypt：响应时间与「密码错」同量级，防用户枚举。
DUMMY_HASH = ('scrypt$16384$8$1$bG9jYWwtYXV0aC1kdW1teS1zYWx0$'
              'oCRIMZApVOF0JXv1AF66tGGJy991KQX7c/0FNxjaHAk=')
_USERNAME = re.compile(r'^[a-z0-9._-]{3,32}$')
MAX_PASSWORD = 128


class LocalAuthError(RuntimeError):
    """登录链路上的失败。`status` 直接当 HTTP 状态码用。"""

    def __init__(self, detail: str, status: int = 400, *,
                 code: str = '', retry: int = 0) -> None:
        self.detail = detail
        self.status = status
        self.code = code
        self.retry = retry
        super().__init__(detail)


def normalize_username(raw: str) -> str:
    """NFKC → 小写 → 去首尾空格。登录时不合格按 401 处理，不泄露格式细节。"""
    value = unicodedata.normalize('NFKC', str(raw or '')).strip().lower()
    if not _USERNAME.match(value):
        raise LocalAuthError('账号或密码不正确', status=401)
    return value


def normalize_managed_username(raw: str) -> str:
    """管理端指定登录账号：格式错误直接 422，不伪装成「账号或密码不正确」。"""
    value = unicodedata.normalize('NFKC', str(raw or '')).strip().lower()
    if not _USERNAME.match(value):
        raise LocalAuthError('登录账号需为 3-32 位小写字母 / 数字 / ._-', status=422)
    return value


def validate_password(raw: str) -> str:
    """建号 / 改密时的密码准入。登录侧不调用（老账号的短密码仍要能登）。"""
    value = unicodedata.normalize('NFKC', str(raw or ''))
    minimum = 8 if settings.app_env == 'production' else settings.local_auth_min_length
    if len(value) < minimum:
        raise LocalAuthError(f'密码至少 {minimum} 位', status=422)
    if len(value) > MAX_PASSWORD:
        raise LocalAuthError(f'密码最多 {MAX_PASSWORD} 位', status=422)
    return value


def _key_material(password: str) -> bytes:
    value = unicodedata.normalize('NFKC', password)
    if settings.local_auth_pepper:
        value = hmac.new(settings.local_auth_pepper.encode(), value.encode(),
                         hashlib.sha256).hexdigest()
    return value.encode('utf-8')


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(_key_material(password), salt=salt,
                            n=N, r=R, p=P, dklen=DK_LEN)
    return (f'scrypt${N}${R}${P}${base64.b64encode(salt).decode()}$'
            f'{base64.b64encode(digest).decode()}')


def verify_password(password: str, encoded: str) -> bool:
    """常量时间比较。任何格式异常都当作密码错，不把异常细节暴露给调用方。"""
    try:
        kind, n, r, p, salt_b64, hash_b64 = str(encoded).split('$')
        if kind != 'scrypt':
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(_key_material(password), salt=salt,
                                n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, OverflowError):
        return False


def needs_rehash(encoded: str) -> bool:
    try:
        parts = str(encoded).split('$')
        return not (parts[0] == 'scrypt' and int(parts[1]) == N
                    and int(parts[2]) == R and int(parts[3]) == P)
    except (ValueError, IndexError):
        return True


def enabled() -> bool:
    """前端 /status 与所有登录端点共用这一个判定。"""
    if settings.auth_mode == 'off' or not settings.local_auth_enabled:
        return False
    if settings.app_env == 'production':
        return settings.auth_cookie_secure and settings.local_auth_min_length >= 8
    return True


def startup_check() -> None:
    """配置自检：生产环境不允许短密码或非 Secure cookie 的本地登录。"""
    if settings.app_env != 'production' or not settings.local_auth_enabled:
        return
    if settings.local_auth_min_length < 8:
        raise RuntimeError('生产环境 LOCAL_AUTH_MIN_LENGTH 不能小于 8')
    if not settings.auth_cookie_secure:
        raise RuntimeError('生产环境开启本地账号登录必须设置 AUTH_COOKIE_SECURE=true')


async def sign_in(store, *, username: str, password: str,
                  client_ip: str = '', user_agent: str = '') -> tuple[str, dict]:
    """校验密码 → 复用现有建会话逻辑。返回 (会话 token, 会话信息)。"""
    name = normalize_username(username)
    allowed, retry = await store.rate_limit(
        'auth:local:ip:' + hashlib.sha256(
            (client_ip or '').encode()).hexdigest()[:16],
        settings.auth_rate_limit_per_minute)
    if not allowed:
        raise LocalAuthError('尝试过于频繁，请稍后再试', status=429, retry=retry)

    row = await store.local_account_for_login(name)
    ok = verify_password(password, row['password_hash'] if row else DUMMY_HASH)
    if not row or not ok or str(row.get('status') or '') != 'active':
        if row:
            await store.local_account_note_failure(
                name, threshold=settings.local_auth_lock_threshold,
                seconds=settings.local_auth_lock_seconds)
        raise LocalAuthError('账号或密码不正确', status=401)
    if float(row.get('locked_until') or 0) > time.time():
        raise LocalAuthError('尝试过于频繁，请稍后再试', status=429)
    if int(row.get('must_change_password') or 0):
        raise LocalAuthError('请先修改密码', status=403,
                             code='PASSWORD_CHANGE_REQUIRED')
    if needs_rehash(row['password_hash']):
        await store.local_account_set_password(
            username=name, password_hash=hash_password(password),
            revoke_sessions=False)

    token = auth.new_session_token()
    session = await store.login_platform_identity(
        platform_id='local', provider=settings.local_auth_identity_provider_id,
        subject=name,
        identity_provider_id=settings.local_auth_identity_provider_id,
        tenant_id=row['tenant_id'], display_name=name, role='member',
        session_id='as_' + secrets.token_hex(12),
        session_digest=auth.digest_of(token),
        session_expires_at=time.time() + settings.auth_session_ttl,
        user_agent=user_agent[:200], remote_addr=client_ip[:64])
    await store.local_account_note_success(name)
    logger.info('本地账号登录 user=%s first=%s',
                session['user_id'], session.get('created'))
    return token, session


async def change_password(store, *, username: str, old_password: str,
                          new_password: str, actor_id: str = '') -> dict:
    """凭旧密码改密（不要求已有会话，配合「首次登录强制改密」）。"""
    name = normalize_username(username)
    password = validate_password(new_password)
    row = await store.local_account_for_login(name)
    ok = verify_password(old_password, row['password_hash'] if row else DUMMY_HASH)
    if not row or not ok:
        raise LocalAuthError('账号或密码不正确', status=401)
    updated = await store.local_account_set_password(
        username=name, password_hash=hash_password(password), actor_id=actor_id)
    if not updated:
        raise LocalAuthError('账号不存在', status=404)
    return {'ok': True, 'username': name}


async def seed_from_env(store) -> list[str]:
    """LOCAL_AUTH_SEED_USERS=user:pass[:role][,user2:pass2]；仅在表为空时执行一次。

    目的是解决「要超管才能建号、超管得先登录」的鸡生蛋问题，类比
    BEEFTV_SEED_USERS。密码只从环境变量读，绝不写日志。
    """
    raw = settings.local_auth_seed_users
    if not raw or store is None:
        return []
    if await store.local_account_count():
        return []
    created: list[str] = []
    for item in raw.split(','):
        parts = item.strip().split(':')
        if len(parts) < 2 or not parts[0] or not parts[1]:
            logger.warning('LOCAL_AUTH_SEED_USERS 有一项格式不对，已跳过')
            continue
        name = normalize_username(parts[0])
        password = validate_password(parts[1])
        role = parts[2].strip() if len(parts) > 2 else 'member'
        if role not in ('member', 'platform_admin'):
            role = 'member'
        try:
            await store.create_local_account(
                username=name, password_hash=hash_password(password),
                role=role, tenant_id=settings.local_auth_tenant_id,
                identity_provider_id=settings.local_auth_identity_provider_id,
                created_by='system:env')
            created.append(name)
        except (FileExistsError, ValueError) as exc:
            logger.warning('种子账号建立失败 username=%s err=%s', name, exc)
    return created
