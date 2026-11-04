"""启动时核对进程能否真的干活。

三件事：模型与检索凭据是否齐全（除了 test 环境）、正式模式是否把治理链配齐、
结果目录是否可写。任何一条不满足都抛在启动阶段，而不是等第一个请求进来才炸。

本地单用户（AUTH_MODE=off）保持兼容路径。ticket 模式且未开开发兼容时，
三个开关已在配置里打开；这里再核对一次，避免进程里被改回去之后仍对外接待。
"""
from webpage_agent.core.config import settings


def credential_issues() -> list[str]:
    """模型/检索凭据缺失或仍是占位符。

    以前这份判定只写在 run_task() 里：POST /api/tasks 先回 200 + running，任务
    随后 failed，用户看到的是"跑起来了又失败"，日志里一坨 traceback。现在
    提到启动阶段，配错的进程根本起不来，问题在部署那一刻就暴露。

    test 环境不带真实凭据、也不该连真模型，所以由 issues() 按环境决定是否调用，
    这里不做环境判断——那样调用方就没法单独测这条规则了。
    """
    from webpage_agent.core.config import missing_runtime_keys
    missing = missing_runtime_keys()
    if not missing:
        return []
    return ['缺少或仍是占位符的凭据：' + ', '.join(missing)]


def issues() -> list[str]:
    found = []
    # 凭据是"能不能干活"的前提，与治理链无关，非 test 环境都要查。
    # test 环境（APP_ENV=test）不查：用例本来就不带真 key，查了会让整套测试在
    # 启动阶段就红，而那不是它们要验证的东西。
    if settings.app_env != 'test':
        found.extend(credential_issues())
    if settings.app_env == 'production':
        found.extend(production_issues())
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
            import tempfile
            with tempfile.TemporaryFile(dir=settings.artifact_dir) as probe:
                probe.write(b'ok')
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


def transport_issue(url: str, *, allow_loopback=False) -> bool:
    from urllib.parse import urlsplit
    import ipaddress
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        if parsed.username or parsed.password or not parsed.hostname or any(c.isspace() for c in parsed.hostname):
            return True
        if parsed.scheme == 'https':
            return False
        if allow_loopback and parsed.scheme == 'http':
            host = parsed.hostname
            if host == 'localhost' or ipaddress.ip_address(host).is_loopback:
                return False
    except ValueError:
        pass
    return True


def production_issues() -> list[str]:
    from webpage_agent.integrations import mcp_servers
    import os
    found = []
    required = {
        'AUTH_MODE=ticket': settings.auth_mode == 'ticket',
        'AUTH_DEV_COMPAT=false': not settings.auth_dev_compat,
        'AUTH_COOKIE_SECURE=true': settings.auth_cookie_secure,
        'POLICY_LEGACY_VISIBLE=false': not settings.policy_legacy_visible,
        'DB_AUTO_MIGRATE=false': not settings.db_auto_migrate,
        'API_RATE_LIMIT_ENABLED=true': settings.api_rate_limit_enabled,
        'TENANT_BUDGET_ENABLED=true': settings.tenant_budget_enabled,
        'AUTH_ALLOWED_ORIGINS (HTTPS, no wildcard)': bool(settings.auth_allowed_origins) and all(
            o.startswith('https://') and '*' not in o for o in settings.auth_allowed_origins),
        'AUTH_COOKIE_SAMESITE': settings.auth_cookie_samesite in ('lax', 'strict', 'none'),
        'positive provider pricing and tenant budget': min(
            settings.tenant_daily_cost_micros,
            settings.llm_input_price_micros_per_million,
            settings.llm_output_price_micros_per_million,
            settings.search_request_cost_micros) > 0,
        'METRICS_FORMAT=json|prometheus': settings.metrics_format in ('json', 'prometheus'),
    }
    try:
        from webpage_agent.api.auth_connections import load_connections
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        connections = [c for c in load_connections() if c.enabled]
        required['enabled platform authentication connection'] = bool(connections)
        for connection in connections:
            for public_key in connection.keys.values():
                load_pem_public_key(public_key.encode())
    except Exception:
        required['valid platform authentication connections'] = False
    for label, ok in required.items():
        if not ok:
            found.append('Production requires ' + label)
    for name, url in (('LLM_BASE_URL', settings.llm_base_url),
                      ('DEEPSEEK_BASE_URL', settings.deepseek_base_url)):
        if transport_issue(url):
            found.append(name + ' requires HTTPS')
    servers = mcp_servers.servers()
    if mcp_servers.LAST_ERROR:
        found.append('Invalid MCP server configuration')
    allow_local = os.getenv('MCP_ALLOW_LOOPBACK_HTTP', 'false').strip().lower() == 'true'
    for server in servers:
        if server.transport != 'stdio' and transport_issue(server.url, allow_loopback=allow_local):
            found.append('MCP server ' + server.id + ' requires HTTPS or an explicitly allowed loopback tunnel')
    return found
