"""账号密码登录的运行期：**把密码交给平台校验，中台不存也不验**。

以前这段逻辑硬编码在 `routes.platform_login` 里：地址取 `BEEFTV_API_BASE`，
平台标识写死 `beeftv`，响应按 `data.user.*` 取值。现在配置来自
`auth_platforms` 表（见 `store/pg_platforms.py`），这段只负责：

1. 找到平台配置（库里没有就退回环境变量，保住只配了 `BEEFTV_API_BASE` 的旧部署）；
2. 发一次 POST；
3. 把平台返回的用户摊平成中台认识的四元组（subject / 昵称 / 状态 / 角色）。

## 为什么响应要按"路径"取值

各平台的登录接口形状不一样：beeftv 是 `{"data": {"user": {...}}}`，别家可能
`{"user": {...}}` 或 `{"data": {...}}`。写死一种就永远只能接一种平台，所以
取值路径做成配置（`data.user.id` 这种点路径）。**只认点路径**，不解析括号、
不支持数组下标——这点表达能力足够覆盖常见形状，又不会变成一门小语言。

## 边界

- 平台不可达 → 503；凭据不对 → 401；被限流 → 429；其余 4xx/5xx → 502。
  这四条是前端唯一能区分的情况，别把上游状态码原样透传（那会把 500 页面的
  HTML 塞进 JSON 错误里）。
- 平台返回的 `status` 不是 `active` → 403，账号停用不该变成"密码错"。
- 超时时间取平台配置，上限 30 秒：登录是同步请求，卡住就是用户卡住。
"""
import logging
import os
from urllib.parse import urlsplit

import httpx

from webpage_agent.core.config import settings
from webpage_agent.observe.runtime_check import transport_issue
from webpage_agent.store.pg_platforms import DEFAULT_CONFIG, normalize_config

logger = logging.getLogger(__name__)


class PlatformLoginError(RuntimeError):
    """登录链路上的失败。`status` 直接当 HTTP 状态码用。"""

    def __init__(self, detail: str, status: int = 502) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


def pick(payload, path: str):
    """按点路径取值：`data.user.id` → payload['data']['user']['id']。

    任何一段缺失都返回 None，而不是抛错——平台少给一个字段不该让整个登录 500，
    由调用方按"没拿到用户标识"处理。
    """
    current = payload
    for part in str(path or '').split('.'):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def validate_base_url(raw: str) -> str:
    """平台根地址的准入检查。**和 `_platform_api_base` 用同一套规则**。

    只允许 http/https、不能带 query/fragment、生产环境不许指向环回/内网。
    这条挡的是 SSRF：这个地址由超管在界面上填，填错了就等于让服务器去打内网。
    """
    value = str(raw or '').strip().rstrip('/')
    if not value:
        raise PlatformLoginError('平台接口根地址未配置', status=404)
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise PlatformLoginError('平台接口根地址必须是 http(s) 完整地址')
    if parsed.query or parsed.fragment:
        raise PlatformLoginError('平台接口根地址不能带查询参数或锚点')
    allow_loopback = settings.app_env != 'production'
    if transport_issue(value, allow_loopback=allow_loopback):
        raise PlatformLoginError(
            '这个地址在当前环境不被允许（生产环境禁止内网/环回地址）')
    return value


def _env_platform() -> dict | None:
    """旧部署的兜底：只配了 `BEEFTV_API_BASE` 时仍然能登录。

    这不是兼容层的洁癖——存量 `.env` 里就有这一行，直接删掉会让正在用的人
    第二天登不进来。库里一旦登记了平台，就走库里的。
    """
    base = os.getenv('BEEFTV_API_BASE', '').strip().rstrip('/')
    if not base:
        return None
    config = dict(DEFAULT_CONFIG)
    return {
        'platform_id': 'beeftv',
        'display_name': 'BeeFTV',
        'login_mode': 'password_http',
        'enabled': True,
        'tenant_id': (os.getenv('PLATFORM_LOGIN_TENANT', '').strip()
                      or 'default'),
        'identity_provider_id': (os.getenv('PLATFORM_LOGIN_IDP', 'beeftv')
                                 .strip() or 'beeftv')[:80],
        'base_url': base,
        'config': normalize_config(config),
        'from_env': True,
    }


async def available(store) -> list[dict]:
    """登录页可选的平台。只给标识与显示名，不含地址等内部信息。

    库里没有登记时退回环境变量里的 beeftv——存量部署升级后登录入口不该消失。
    """
    items = []
    if store is not None:
        try:
            items = await store.platform_public_list()
        except Exception:                               # noqa: BLE001
            logger.warning('读取登录平台列表失败，退回环境变量', exc_info=True)
            items = []
    if not items:
        env = _env_platform()
        if env:
            items = [{'platform_id': env['platform_id'],
                      'display_name': env['display_name']}]
    return items


async def seed_from_env(store) -> str | None:
    """把 `BEEFTV_API_BASE` 登记成一行，只在表为空时做一次。

    不做的话，升级后控制台会显示"没有平台"，而登录其实是通的——用户会以为
    迁移把登录入口弄丢了。返回登记的平台标识，没做任何事时返回 None。
    """
    env = _env_platform()
    if env is None or store is None:
        return None
    try:
        listed = await store.platform_list()
    except Exception:                                   # noqa: BLE001
        return None
    if listed.get('platforms'):
        return None
    await store.platform_create({
        'platform_id': env['platform_id'],
        'display_name': env['display_name'],
        'base_url': env['base_url'],
        'tenant_id': env['tenant_id'],
        'identity_provider_id': env['identity_provider_id'],
        'note': '由环境变量 BEEFTV_API_BASE 自动登记，可在这里继续增删平台',
        'is_default': True,
    }, actor_id='system:env')
    return env['platform_id']


async def resolve(store, platform_id: str = '') -> dict | None:
    """找到要用的平台配置。库优先，环境变量兜底。"""
    config = None
    if store is not None:
        try:
            config = await store.platform_login_config(platform_id)
        except Exception:                               # noqa: BLE001
            # 库还没迁到 v19（表不存在）时不要让登录整体 500：退回环境变量，
            # 并留下一行日志说明为什么这次没读到配置。
            logger.warning('读取登录平台配置失败，退回环境变量', exc_info=True)
            config = None
    if config is None:
        fallback = _env_platform()
        if fallback and (not platform_id or platform_id == fallback['platform_id']):
            return fallback
    return config


def _role_of(raw) -> str:
    return ('operator' if str(raw or '').lower() in ('admin', 'owner', 'operator')
            else 'member')


async def sign_in(store, *, platform_id: str, username: str, password: str,
                  client: httpx.AsyncClient | None = None) -> dict:
    """校验账号密码，返回中台建会话需要的字段。失败一律抛 `PlatformLoginError`。

    `client` 只给测试注入用；生产走内部新建的 `AsyncClient`（`follow_redirects`
    必须是 False：跟着跳转就把密码又发给了下一个地址）。
    """
    platform = await resolve(store, platform_id)
    if platform is None:
        raise PlatformLoginError('平台账号登录未启用', status=404)
    base = validate_base_url(platform.get('base_url'))
    config = platform.get('config') or dict(DEFAULT_CONFIG)
    login_path = str(config.get('login_path') or '/auth/login')
    url = base + ('' if login_path.startswith('/') else '/') + login_path
    body = {str(config.get('username_field') or 'username'): username,
            str(config.get('password_field') or 'password'): password}
    timeout = float(config.get('timeout_seconds') or 10)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        upstream = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        logger.warning('平台登录校验不可用 platform=%s err=%s',
                       platform.get('platform_id'), exc)
        raise PlatformLoginError('平台身份服务不可用', status=503) from None
    finally:
        if owns_client:
            await client.aclose()

    if upstream.status_code in (401, 403):
        raise PlatformLoginError('账号或密码不正确', status=401)
    if upstream.status_code == 429:
        raise PlatformLoginError('尝试过于频繁，请稍后再试', status=429)
    if upstream.status_code >= 400:
        raise PlatformLoginError('平台身份服务返回异常', status=502)

    try:
        payload = upstream.json() or {}
    except ValueError:
        raise PlatformLoginError('平台身份服务返回异常', status=502) from None

    subject = str(pick(payload, config.get('subject_path') or 'data.user.id')
                  or '').strip()
    if not subject:
        raise PlatformLoginError('平台未返回用户标识', status=502)
    status = str(pick(payload, config.get('status_path') or 'data.user.status')
                 or 'active').strip() or 'active'
    if status != 'active':
        raise PlatformLoginError('平台账号已停用', status=403)
    display_name = str(pick(payload, config.get('display_name_path')
                            or 'data.user.username') or username).strip()
    role = _role_of(pick(payload, config.get('role_path') or 'data.user.role'))

    return {'platform_id': platform['platform_id'],
            'identity_provider_id': (platform.get('identity_provider_id')
                                     or platform['platform_id']),
            'tenant_id': platform.get('tenant_id') or 'default',
            'subject': subject[:200],
            'display_name': display_name[:120],
            'role': role}
