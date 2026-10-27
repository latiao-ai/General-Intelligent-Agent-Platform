"""启动时核对正式模式是否把治理链配齐。

本地单用户（AUTH_MODE=off）保持兼容路径。ticket 模式且未开开发兼容时，
三个开关已在配置里打开；这里再核对一次，避免进程里被改回去之后仍对外接待。
结果目录不可写时拒绝启动。
"""
from app.config import settings


def issues() -> list[str]:
    found = []
    if settings.auth_mode != 'ticket':
        return found
    if settings.auth_dev_compat:
        return found
    if not settings.tool_policy_enforced:
        found.append('ticket 模式必须打开 TOOL_POLICY_ENFORCED')
    if not settings.result_envelope_v2:
        found.append('ticket 模式必须打开 RESULT_ENVELOPE_V2')
    if not settings.async_tool_runtime_enabled:
        found.append('ticket 模式必须打开 ASYNC_TOOL_RUNTIME_ENABLED')
    if settings.result_envelope_v2:
        try:
            settings.artifact_dir.mkdir(parents=True, exist_ok=True)
            probe = settings.artifact_dir / '.write_probe'
            probe.write_text('ok', encoding='utf-8')
            probe.unlink()
        except OSError:
            found.append('结果目录不可写')
    return found


def origin_rejected(method: str, origin: str) -> bool:
    """浏览器带了 Origin 时，必须落在允许列表里。

    没有 Origin 的调用（curl、服务端）不拦。没配列表时，ticket 模式拒绝
    任何带 Origin 的写请求，避免用宽泛 CORS 代替来源校验。
    """
    if method.upper() not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return False
    origin = (origin or '').strip()
    if not origin:
        return False
    allowed = tuple(getattr(settings, 'auth_allowed_origins', ()) or ())
    if origin in allowed:
        return False
    if allowed:
        return True
    return settings.auth_mode == 'ticket' and not settings.auth_dev_compat
