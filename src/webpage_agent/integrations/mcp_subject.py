"""把「这次调用是替谁发起的」以**签名断言**的形式交给远端 MCP 服务端。

为什么不能直接发明文主体头（这是踩过的设计坑）：

- 远端服务端在另一台机器上，**它没有我们的账本**，一个明文 `X-MCP-Subject: alice`
  对它来说只是"调用方自己说的"，既不能当审计依据，也不能当授权依据；
- 一旦对方真的拿它做身份判断，危险程度比不发还高：任何拿到那个租户级
  `token` 的人都能把 subject 改成任意值——这跟"把 hub token 当万能钥匙"
  是同一类错误（见 `runtime.go` 里 hub 分支只比较 token、`auth.go` 拿到
  subject 就查 `UserByID`）。

所以这里发的是**短期、可验签、带上下文的断言**：

    v1.<base64url(payload)>.<base64url(HMAC-SHA256(secret, "v1." + payload))>

`payload` 是 JSON：`sub / tenant / platform / run / iat / exp / jti`。

对端能确认的是「这条断言由持有该平台共享密钥的一方签发，且没被改过、没过期」；
它**不能**代替对端自己的用户映射与授权——对端仍应把 `sub` 映射到自己的用户、
检查状态，再决定放不放行。密钥本身走 `env:` / `file:` / `literal:`，不进配置文件、
不进日志、不进模型上下文。
"""
import base64
import hashlib
import hmac
import json
import time
import uuid

# 断言版本前缀。换算法/换载荷结构时改这里，对端可以按前缀分支兼容旧版。
VERSION = 'v1'

# 远端服务端默认读这个头。可被服务端配置的 `subject_header` 覆盖（对端已有既定头名时用）。
DEFAULT_HEADER = 'X-MCP-Subject-Assertion'

# 断言有效期上限（秒）。它只在一次调用里用，不需要长命；给重试留一点余量即可。
MAX_TTL_SECONDS = 300
DEFAULT_TTL_SECONDS = 60

# 允许的对端时钟偏差（秒）。两台机器时钟差几分钟是常态，不设偏移量会把正常请求判成过期。
DEFAULT_LEEWAY_SECONDS = 30


class SubjectAssertionError(RuntimeError):
    """断言无效：格式、签名、时效任一不过。

    文案里只放"哪一环不对"，不放密钥、不放完整载荷——这些东西会进日志。
    """


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64decode(text: str) -> bytes:
    padding = '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def build(subject: str, *, secret: str, tenant_id: str = '', platform_id: str = '',
          run_id: str = '', ttl_seconds: int = DEFAULT_TTL_SECONDS,
          now: float | None = None) -> str:
    """签发一条断言。

    `subject` 必须是**服务端推导出来的**主体（`runs.platform_subject`），
    不是模型给的参数——调用方在 `mcp_adapter.current_subject()` 里已经保证了这一点。
    """
    subject = str(subject or '').strip()
    secret = str(secret or '')
    if not subject:
        raise SubjectAssertionError('断言缺少主体')
    if not secret:
        raise SubjectAssertionError('断言密钥为空')
    ttl = max(1, min(MAX_TTL_SECONDS, int(ttl_seconds or DEFAULT_TTL_SECONDS)))
    issued = time.time() if now is None else float(now)
    payload = {
        'sub': subject,
        'tenant': str(tenant_id or ''),
        'platform': str(platform_id or ''),
        'run': str(run_id or ''),
        'iat': int(issued),
        'exp': int(issued) + ttl,
        'jti': uuid.uuid4().hex,
    }
    body = _b64encode(json.dumps(payload, separators=(',', ':'),
                                 ensure_ascii=False, sort_keys=True).encode('utf-8'))
    signing_input = f'{VERSION}.{body}'
    signature = hmac.new(secret.encode('utf-8'), signing_input.encode('ascii'),
                         hashlib.sha256).digest()
    return f'{signing_input}.{_b64encode(signature)}'


def verify(token: str, secret: str, *, now: float | None = None,
           leeway: int = DEFAULT_LEEWAY_SECONDS) -> dict:
    """验签并取回载荷。**给对端使用**（本端只负责签发）。

    校验顺序：结构 → 签名（常量时间比较）→ 时效。签名先于时效检查，
    否则伪造者能靠"过期"刷出与"签名错"不同的错误码，反过来探测密钥。
    """
    text = str(token or '').strip()
    if not text:
        raise SubjectAssertionError('断言为空')
    parts = text.split('.')
    if len(parts) != 3 or parts[0] != VERSION:
        raise SubjectAssertionError('断言格式不正确')
    body, signature = parts[1], parts[2]
    expected = hmac.new(str(secret or '').encode('utf-8'),
                        f'{VERSION}.{body}'.encode('ascii'),
                        hashlib.sha256).digest()
    try:
        supplied = _b64decode(signature)
    except Exception:                                    # noqa: BLE001
        raise SubjectAssertionError('断言签名无法解码') from None
    if not hmac.compare_digest(expected, supplied):
        raise SubjectAssertionError('断言签名不匹配')
    try:
        payload = json.loads(_b64decode(body).decode('utf-8'))
    except Exception:                                    # noqa: BLE001
        raise SubjectAssertionError('断言载荷无法解析') from None
    if not isinstance(payload, dict) or not str(payload.get('sub') or '').strip():
        raise SubjectAssertionError('断言载荷缺少主体')
    moment = time.time() if now is None else float(now)
    expires = payload.get('exp')
    if not isinstance(expires, (int, float)) or moment > float(expires) + leeway:
        raise SubjectAssertionError('断言已过期')
    issued = payload.get('iat')
    if isinstance(issued, (int, float)) and float(issued) > moment + leeway:
        raise SubjectAssertionError('断言的签发时间在未来')
    return payload
