"""按租户解析「这次 MCP 调用用哪把钥匙」。

连接定义仍在 `webpage_agent/integrations/mcp_servers.py`（地址、传输、工具名）。本模块只回答：
当前主体在这个 `server_id` 上有没有凭据绑定。明文只在返回前短时取出，
不放进可序列化的连接对象。

- 有绑定：用这把钥匙，不再读进程级 `MCP_BEARER_TOKEN`。
- 没有绑定且 `TOOL_POLICY_ENFORCED`：拒绝，远端调用数为 0。
- 没有绑定且未强制：返回 None，调用方沿用服务端配置里的 token（本地 Demo）。
"""
from webpage_agent.core import credentials as credentials_module
from webpage_agent.tools import tool_policy as policy_module
from webpage_agent.core.config import settings


def server_id_of(tool_key: str) -> str:
    """`mcp:<server>:<tool>` → server。本地工具返回空串。"""
    parts = str(tool_key or '').split(':')
    if len(parts) >= 3 and parts[0] == 'mcp' and parts[1]:
        return parts[1]
    return ''


async def binding_for_server(store, *, tenant_id: str, user_id: str,
                              server_id: str, platform_id: str = ''):
    """只取**这个服务端**的绑定。取不到别的服务端的钥匙来顶。

    通用服务（部署 / 本地生成这类"谁都能用"的服务端）的绑定挂在
    `SHARED_TENANT` 上：租户内没有这条服务端绑定，就回退到共享行，
    再把**归属租户**（查凭据）和**调用租户**（判断租户维度）分开传下去。
    没有 `tenants: ['*']` 的共享行仍然会被 `matches()` 拒掉。
    """
    if store is None or not server_id:
        return None
    row = await store.server_binding(tenant_id, server_id)
    owner_tenant = tenant_id
    if (not row or not row.get('enabled')) and tenant_id != credentials_module.SHARED_TENANT:
        shared = await store.server_binding(credentials_module.SHARED_TENANT, server_id)
        if shared and shared.get('enabled'):
            row, owner_tenant = shared, credentials_module.SHARED_TENANT
    if not row or not row.get('enabled'):
        return None
    from webpage_agent.tools import tool_authorization as authorization_module
    return await authorization_module.resolve_binding(
        store, tenant_id=tenant_id, user_id=user_id,
        platform_id=platform_id, owner_tenant_id=owner_tenant,
        server_binding_id=str(row.get('binding_id') or ''))


async def bearer_for(store, *, tenant_id: str, user_id: str,
                     server_id: str, platform_id: str = '') -> str | None:
    """这次连接要用的明文。

    `None` = 调用方沿用服务端 / 环境 token。
    非空 = 必须用这把，禁止再退回全局 token。

    绑定上标了 `scope.auth == 'oauth'` 时，`credential_ref` 指向的是一份
    OAuth 凭据结构而不是一行 token，所以走 `oauth_store`：它会先判断过期、
    必要时刷新，再返回 access_token。
    """
    binding = await binding_for_server(
        store, tenant_id=tenant_id, user_id=user_id, server_id=server_id,
        platform_id=platform_id)
    if binding is None:
        if policy_module.enforcement_enabled():
            raise credentials_module.CredentialMissing(
                f'租户 {tenant_id} 没有服务端 {server_id} 的可用凭据，拒绝调用')
        return None
    if str(credentials_module.scope_of(binding).get('auth') or '') == 'oauth':
        from webpage_agent.core import oauth_store
        from webpage_agent.integrations import mcp_servers
        server = mcp_servers.by_id(server_id)
        if server is None:
            raise credentials_module.CredentialMissing(
                f'服务端 {server_id} 已不在 MCP_SERVERS 配置里，OAuth 凭据无从刷新')
        return await oauth_store.ensure_access_token(
            server=server, tenant_id=tenant_id, user_id=user_id,
            server_id=server_id)
    handle = credentials_module.materialize_credential(
        binding, expected_subject=user_id,
        ttl=float(settings.credential_cache_ttl or 0))
    return handle.secret


async def current_subject():
    """当前执行链上的可信主体：`(store, tenant_id, user_id, platform_id)`。

    没有账本、没有执行上下文、或取不到运行行时返回 `None`——调用点据此决定
    「沿用配置」还是「拒绝」，避免在这里悄悄编一个主体出来。
    """
    from webpage_agent.governance import execution_control
    from webpage_agent.store import ledger as ledger_module
    book = ledger_module.current_ledger()
    context = execution_control.require()
    store = getattr(book, 'store', None) if book is not None else None
    if store is None or context is None:
        return None
    run = await store.run(context.run_id)
    if not run:
        return None
    return (store,
            str(run.get('tenant_id') or 'default'),
            str(run.get('owner_user_id') or '') or 'default',
            str(run.get('platform_id') or ''))


async def bearer_for_current(server) -> str | None:
    """从当前执行链上的 run 取主体，再解析这把钥匙。

    没有账本（测试、CLI、未强制的本地 Demo）时返回 None，传输层沿用配置。
    强制模式下没有主体或没有绑定，直接拒绝，不发请求。
    """
    if server is None:
        if policy_module.enforcement_enabled():
            raise credentials_module.CredentialMissing(
                '强制模式下不能用未指定服务端的全局 token')
        return None
    subject = await current_subject()
    if subject is None:
        if policy_module.enforcement_enabled():
            raise credentials_module.CredentialMissing(
                '强制模式下缺少执行主体，拒绝调用远端')
        return None
    store, tenant_id, user_id, platform_id = subject
    return await bearer_for(
        store, tenant_id=tenant_id, user_id=user_id, server_id=server.id,
        platform_id=platform_id)


async def inner_allowlist_for_current(server) -> list[str] | None:
    """通用绑定的**内层工具白名单**；`None` = 不限制（平台私有绑定 / 无账本）。

    为什么要在本地挡：这类服务端只有一个 `run_tool(name=…)` 入口，外层工具名
    全一样，真正执行什么由参数决定。通用绑定把入口开放给所有人之后，
    内层名就是唯一的权限边界，不能指望服务端自己拦。
    """
    if server is None:
        return None
    subject = await current_subject()
    if subject is None:
        return None
    store, tenant_id, user_id, platform_id = subject
    binding = await binding_for_server(
        store, tenant_id=tenant_id, user_id=user_id,
        server_id=getattr(server, 'id', ''), platform_id=platform_id)
    if binding is None or not binding.universal:
        return None
    return binding.inner_tools()


async def discoverable_servers(servers):
    """Filter before opening any transport, including stdio processes."""
    from webpage_agent.store.ledger import current_ledger
    from webpage_agent.governance import execution_control
    book = current_ledger()
    context = execution_control.require()
    if book is None or context is None:
        raise credentials_module.CredentialMissing('工具发现缺少可信执行主体')
    run = await book.store.run(context.run_id)
    if not run:
        raise credentials_module.CredentialMissing('工具发现缺少有效运行主体')
    allowed = []
    for server in servers:
        binding = await binding_for_server(
            book.store, tenant_id=str(run.get('tenant_id') or 'default'),
            user_id=str(run.get('owner_user_id') or 'default'),
            platform_id=str(run.get('platform_id') or ''), server_id=server.id)
        if binding is not None:
            allowed.append(server)
    return allowed


async def discovery_scope(servers) -> tuple:
    """目录缓存的身份与凭据版本，不含密钥；每次命中前重新核对。"""
    from webpage_agent.store.ledger import current_ledger
    from webpage_agent.governance import execution_control
    book = current_ledger()
    context = execution_control.require()
    if book is None or context is None:
        raise credentials_module.CredentialMissing('工具发现缺少可信执行主体')
    run = await book.store.run(context.run_id)
    if not run:
        raise credentials_module.CredentialMissing('工具发现缺少有效运行主体')
    tenant = str(run.get('tenant_id') or 'default')
    user = str(run.get('owner_user_id') or 'default')
    scope = []
    for server in servers:
        binding = await binding_for_server(book.store, tenant_id=tenant,
                                           user_id=user, server_id=server.id,
                                           platform_id=str(run.get('platform_id') or ''))
        if binding is None:
            raise credentials_module.CredentialMissing('工具发现缺少有效凭据绑定')
        scope.append((server.id, binding.binding_id, binding.revision,
                      credentials_module.parse_scope(binding.scope)))
    return tenant, user, scope, str(run.get('platform_id') or '')
