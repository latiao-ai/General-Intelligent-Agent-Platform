"""按租户解析 MCP 凭据：**不把全局 token 当成所有人的钥匙**。

为什么需要这一层（《智能体中台说明书》）：

进程级 `MCP_BEARER_TOKEN` 对所有租户是同一把。接第二家客户时，这把钥匙要么
被共享（对方平台的 `list_*` 会泄漏别人的资源、`delete_*` 能删别人的），要么
被换掉（第一家客户的服务断掉）。所以凭据必须**按绑定**取：

    10|- `mcp_server_bindings`：哪个租户接了哪个服务端、用哪种传输；
- `credential_bindings`：某个主体（租户 / 用户）在某个服务端上用哪把钥匙，
  以及这把钥匙的**范围**；
- 密钥本身不进库，只进受控环境（`credential_ref` → 环境变量 / 文件）。

三条纪律：

1. **生产缺绑定就拒绝**，不静默退回进程级全局 token——那等于把所有租户的
   权限又合到一起。开发模式（`TOOL_POLICY_ENFORCED=false`）才允许回退，
   而且必须**显式**、且在回执里留痕；
2. **密钥只在传输层短时使用**：不进模型、不进步骤状态、不进事件、不进日志、
   不进快照；
3. **轮换必须同主体**：同一主体换一把新钥匙是允许的，换成别人的钥匙不是。
"""
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 明文密钥的进程内缓存：{cache_key: (secret, expires_at)}。
# 缓存是**性能必需**（一次任务几十个请求，全部查库会把 DB 打满），
# 但 TTL 必须是有限值，且键必须包含主体与绑定版本。
_CACHE: dict[str, tuple[str, float]] = {}


class CredentialMissing(RuntimeError):
    """没有可用凭据。

    **不能**降级成"用全局 token 试试"——那会让租户隔离在生产里静默失效。
    """


class CredentialMismatch(RuntimeError):
    """凭据的主体与调用主体不一致（例如拿 A 用户的钥匙去替 B 用户执行）。"""


@dataclass(frozen=True)
class CredentialBinding:
    """一条凭据绑定的**元信息**（不含明文）。"""

    binding_id: str
    tenant_id: str
    subject_kind: str
    subject_id: str
    server_binding_id: str
    credential_ref: str
    scope: dict
    revision: int
    status: str = 'active'

    def matches(self, tenant_id: str, subject_id: str, platform_id: str = '') -> bool:
        """这条绑定能不能用于这个主体。

        `subject_kind='tenant'` 的绑定对该租户的所有主体可用；`subject_kind=
        'user'` 的只给那一个用户。租户必须**完全相等**——跨租户共用一把钥匙
        是设计上的错误，不是配置错误。
        """
        if self.tenant_id != tenant_id:
            return False
        if self.status != 'active':
            return False
        platforms = (self.scope or {}).get('platforms')
        if platform_id and platforms is None:
            return False
        if platforms is not None and (not isinstance(platforms, list)
                                      or platform_id not in platforms):
            return False
        if self.subject_kind == 'tenant':
            return True
        return self.subject_id == subject_id

    def scope_allows(self, tool_key: str) -> bool:
        """这把钥匙允许调这个工具吗？

        `scope` 里没有 `tools` 键表示"该绑定上的全部工具"；有的话就是**白名单**。
        缺白名单不等于放行一切——只是"未限制"，具体放行由政策层决定。
        """
        tools = (self.scope or {}).get('tools')
        if not tools:
            return True
        return tool_key in list(tools)


@dataclass(frozen=True)
class SecretHandle:
    """一次传输用的密钥。

    `secret` 是明文，但这个对象**不允许**被序列化进状态或事件——
    `__repr__` 被刻意改掉了，日志里打出来只会看到 `SecretHandle(...)`。
    """

    binding_id: str
    revision: int
    subject_id: str
    secret: str = ''

    def __repr__(self) -> str:                          # pragma: no cover
        return f'SecretHandle(binding_id={self.binding_id!r}, secret=<hidden>)'

    def __str__(self) -> str:                           # pragma: no cover
        return self.__repr__()


def _cache_key(binding: CredentialBinding) -> str:
    return f'{binding.binding_id}:{binding.revision}'


def materialize_credential(binding: CredentialBinding, expected_subject: str = '',
                           *, ttl: float = 30.0) -> SecretHandle:
    """按绑定取明文密钥。

    首版只用**受控环境映射**：`credential_ref` 形如 `env:MCP_TOKEN_xxx`
    或 `file:/path/to/token`。不强制特定密钥管理产品——但绝不从库里读明文。

    `expected_subject` 不匹配时抛 `CredentialMismatch`：拿 A 用户的钥匙替 B
    用户执行是最典型的越权，而且从回执上完全看不出来。
    """
    if expected_subject and binding.subject_kind == 'user' \
            and binding.subject_id != expected_subject:
        raise CredentialMismatch(
            f'凭据绑定 {binding.binding_id} 属于用户 {binding.subject_id}，'
            f'不能用于 {expected_subject}')
    cached = _CACHE.get(_cache_key(binding))
    now = time.time()
    if cached and cached[1] > now:
        return SecretHandle(binding_id=binding.binding_id,
                            revision=binding.revision,
                            subject_id=binding.subject_id, secret=cached[0])
    secret = _read_secret(binding.credential_ref)
    if ttl > 0:
        _CACHE[_cache_key(binding)] = (secret, now + ttl)
    return SecretHandle(binding_id=binding.binding_id, revision=binding.revision,
                        subject_id=binding.subject_id, secret=secret)


def _read_secret(ref: str) -> str:
    """按引用取明文。引用形态：`env:NAME` / `file:/path` / `literal:xxx`。"""
    text = str(ref or '').strip()
    if not text:
        raise CredentialMissing('凭据绑定没有填 credential_ref')
    if text.startswith('env:'):
        name = text[4:].strip()
        value = os.getenv(name, '')
        if not value:
            raise CredentialMissing(f'环境变量 {name} 没有值')
        return value
    if text.startswith('file:'):
        path = text[5:].strip()
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return handle.read().strip()
        except OSError as exc:
            raise CredentialMissing(f'读取凭据文件失败：{type(exc).__name__}') from exc
    if text.startswith('literal:'):
        # 明文直接写在库里是**部署配置**的做法，不是生产做法；但它让本地
        # 联调不必先建环境变量。生产应当用 env:/file:。
        return text[8:]
    raise CredentialMissing('credential_ref 只能是 env: / file: / literal:')


def invalidate_cache(binding_id: str = '') -> None:
    """绑定变更或撤销时清缓存（否则旧密钥会继续被用）。"""
    if not binding_id:
        _CACHE.clear()
        return
    for key in [k for k in _CACHE if k.startswith(f'{binding_id}:')]:
        _CACHE.pop(key, None)


def fingerprint(secret: str) -> str:
    """凭据指纹：用于审计与"是不是同一把钥匙"的判断。

    只存摘要，不存明文；摘要带盐（租户+绑定）后也不能反推密钥。
    """
    return hashlib.sha256(('cred:' + (secret or '')).encode('utf-8')).hexdigest()[:16]


def scope_of(binding: CredentialBinding) -> dict:
    """绑定的范围（用于审计与政策匹配）。"""
    try:
        scope = binding.scope if isinstance(binding.scope, dict) else {}
    except Exception:                                # noqa: BLE001
        scope = {}
    return dict(scope)


def parse_scope(raw) -> dict:
    """把库里存的 scope 文本解析成 dict。"""
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
