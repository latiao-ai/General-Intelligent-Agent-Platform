"""可信平台登记表：**谁能给中台签票据**。

票据里的字段在验签完成之前一律不可信，所以"用哪把公钥验票"这件事不能由票据
自己决定。第一版把接入方写在服务端配置里：

```json
[
  {
    "connection_id": "platform-a",
    "issuer": "https://a.example.com",
    "audience": "agent-hub-login",
    "algorithms": ["RS256"],
    "public_key_file": "secrets/platform-a.pub.pem",
    "allowed_tenants": ["tenant-a"],
    "allowed_platforms": ["a"],
    "identity_provider_id": "idp-a",
    "enabled": true
  }
]
```

配置来源（按优先级）：

1. `AUTH_CONNECTIONS_FILE` 指向的 JSON 文件（推荐：公钥是 PEM 文件，放文件更安全）；
2. `AUTH_CONNECTIONS` 环境变量里的 JSON 数组（公钥可以内联成 PEM 字符串）。

两条边界必须守住：

- **票据自称的 `iss` 只用来查表**，查不到就不验、直接拒绝；
- **`iss` / `aud` 必须和登记的一致**，不做"前缀匹配""忽略大小写"这类宽松比较。

`connection` 与 `identity_provider` 是两回事：三个平台可能共用同一个统一账号中心
（同一 `identity_provider_id`），也可能各自独立。身份绑定按身份源隔离，所以
"两个平台各自有个 user-123" 不会变成同一个中台账号。
"""
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_AUDIENCE = 'agent-hub-login'

# 第一版只允许 RS256。允许调用方指定 alg 是 JWT 的经典漏洞（alg=none / HS256 混淆）。
ALLOWED_ALGORITHMS = frozenset({'RS256'})


class ConnectionError_(RuntimeError):
    """登记表本身有问题（配置错、公钥读不到）。启动阶段就该炸。"""


@dataclass(frozen=True)
class Connection:
    """一个已登记的平台接入。"""

    connection_id: str
    issuer: str
    audience: str = DEFAULT_AUDIENCE
    algorithms: tuple = ('RS256',)
    identity_provider_id: str = ''
    # 允许该接入声明的范围。空列表 = 不限（仅本地开发用，生产必须写死）
    allowed_tenants: tuple = ()
    allowed_platforms: tuple = ()
    enabled: bool = True
    platform_id: str = ''
    # kid → PEM。第一版固定公钥，后续可换成 JWKS 缓存。
    keys: dict = field(default_factory=dict)

    def allows_tenant(self, tenant_id: str) -> bool:
        return not self.allowed_tenants or tenant_id in self.allowed_tenants

    def allows_platform(self, platform_id: str) -> bool:
        return not self.allowed_platforms or platform_id in self.allowed_platforms


def _read_key(entry: dict, base: Path) -> dict:
    """读公钥：支持 `public_key`（PEM 内联）、`public_key_file`、`keys{kid: PEM}`。

    单把钥匙的 `kid` 默认取**接入编号**（签发方通常就用它当 kid），同时也登记成
    `default`：既不会出现"票据带了 kid 但表里没有"的假拒绝，也保留了按 kid 选钥匙
    的能力（多把钥匙时必须显式给 kid）。
    """
    default_kid = str(entry.get('kid') or entry.get('connection_id') or 'default')
    keys: dict[str, str] = {}

    def remember(pem: str) -> None:
        keys[default_kid] = pem
        keys.setdefault('default', pem)

    inline = (entry.get('public_key') or '').strip()
    if inline:
        remember(inline)
    path_text = (entry.get('public_key_file') or '').strip()
    if path_text:
        path = Path(path_text)
        if not path.is_absolute():
            path = base / path
        try:
            remember(path.read_text(encoding='utf-8'))
        except OSError as exc:
            raise ConnectionError_(f'读不到公钥文件 {path}：{exc}') from exc
    for kid, pem in (entry.get('keys') or {}).items():
        keys[str(kid)] = str(pem)
    if not keys:
        raise ConnectionError_(
            f'接入 {entry.get("connection_id")} 没有配置公钥'
            '（public_key / public_key_file / keys 至少给一个）')
    return keys


def _build(entry: dict, base: Path) -> Connection:
    for required in ('connection_id', 'issuer'):
        if not str(entry.get(required) or '').strip():
            raise ConnectionError_(f'接入缺少必填字段 {required}')
    algorithms = tuple(entry.get('algorithms') or ('RS256',))
    bad = [alg for alg in algorithms if alg not in ALLOWED_ALGORITHMS]
    if bad:
        # 明确报错而不是静默降级：允许 HS256 就等于允许拿公钥当对称密钥伪造票据
        raise ConnectionError_(
            f'接入 {entry["connection_id"]} 声明了不支持的算法 {bad}，'
            f'只允许 {sorted(ALLOWED_ALGORITHMS)}')
    connection_id = str(entry['connection_id']).strip()
    return Connection(
        connection_id=connection_id,
        issuer=str(entry['issuer']).strip(),
        audience=str(entry.get('audience') or DEFAULT_AUDIENCE).strip(),
        algorithms=algorithms,
        identity_provider_id=str(
            entry.get('identity_provider_id') or connection_id).strip(),
        allowed_tenants=tuple(str(x) for x in (entry.get('allowed_tenants') or ())),
        allowed_platforms=tuple(str(x) for x in (entry.get('allowed_platforms') or ())),
        enabled=bool(entry.get('enabled', True)),
        platform_id=str(entry.get('platform_id') or connection_id).strip(),
        keys=_read_key(entry, base),
    )


def load_connections() -> list[Connection]:
    """读登记表。配置错了就抛错——**不能静默当成"没有接入方"**。

    后者会让所有登录都失败，而日志里只有一句"票据无效"，排查成本极高。
    """
    raw = os.getenv('AUTH_CONNECTIONS', '').strip()
    file_path = os.getenv('AUTH_CONNECTIONS_FILE', '').strip()
    entries: list[dict] = []
    base = PROJECT_ROOT

    if file_path:
        path = Path(file_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except OSError as exc:
            raise ConnectionError_(f'读不到接入登记表 {path}：{exc}') from exc
        except ValueError as exc:
            raise ConnectionError_(f'接入登记表不是合法 JSON：{exc}') from exc
        entries = data if isinstance(data, list) else data.get('connections') or []
        # 相对路径统一相对**项目根**解析：写 secrets/x.pub.pem 时不会因为配置文件
        # 自己也放在 secrets/ 下就变成 secrets/secrets/x.pub.pem
        base = PROJECT_ROOT

    if raw:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ConnectionError_(f'AUTH_CONNECTIONS 不是合法 JSON：{exc}') from exc
        # 环境变量里的公钥只能内联，所以按项目根解析相对路径
        entries = (entries + (data if isinstance(data, list)
                              else data.get('connections') or []))

    if not isinstance(entries, list):
        raise ConnectionError_('接入登记表必须是数组')

    seen = set()
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConnectionError_(f'接入登记表里有非对象条目：{entry!r}')
        connection = _build(entry, base)
        if connection.connection_id in seen:
            raise ConnectionError_(f'接入编号重复：{connection.connection_id}')
        seen.add(connection.connection_id)
        out.append(connection)
    return out


_cache: dict = {'at': 0.0, 'items': None}


def connections(force: bool = False) -> list[Connection]:
    """带小缓存的登记表读取：登录是热路径，但配置改动要能很快生效。"""
    import time
    now = time.monotonic()
    if force or _cache['items'] is None or now - _cache['at'] > 5:
        _cache['items'] = load_connections()
        _cache['at'] = now
    return _cache['items']


def find(issuer: str, audience: str = DEFAULT_AUDIENCE) -> list[Connection]:
    """按 `iss`/`aud` 找候选接入（**精确匹配**）。"""
    return [item for item in connections()
            if item.enabled and item.issuer == issuer and item.audience == audience]


def public_key(connection: Connection, kid: str = '') -> str:
    """按 kid 取公钥。票据写了 kid 就必须命中登记表，不能因为只有一把钥匙就放行。

    没写 kid 时，只有恰好一把钥匙才允许直接用。
    """
    if kid:
        pem = connection.keys.get(kid)
        if not pem:
            raise ConnectionError_(
                f'接入 {connection.connection_id} 没有 kid={kid} 的公钥')
        return pem
    distinct = {value for value in connection.keys.values()}
    if len(distinct) == 1:
        return next(iter(distinct))
    raise ConnectionError_(
        f'接入 {connection.connection_id} 有多把公钥，票据必须带 kid')
