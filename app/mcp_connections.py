"""按租户解析「这次 MCP 调用用哪把钥匙」。

连接定义仍在 `app/mcp_servers.py`（地址、传输、工具名）。本模块只回答：
当前主体在这个 `server_id` 上有没有凭据绑定。明文只在返回前短时取出，
不放进可序列化的连接对象。

- 有绑定：用这把钥匙，不再读进程级 `MCP_BEARER_TOKEN`。
- 没有绑定且 `TOOL_POLICY_ENFORCED`：拒绝，远端调用数为 0。
- 没有绑定且未强制：返回 None，调用方沿用服务端配置里的 token（本地 Demo）。
"""
from app import credentials as credentials_module
from app import tool_policy as policy_module
from app.config import settings


def server_id_of(tool_key: str) -> str:
    """`mcp:<server>:<tool>` → server。本地工具返回空串。"""
    parts = str(tool_key or '').split(':')
    if len(parts) >= 3 and parts[0] == 'mcp' and parts[1]:
        return parts[1]
    return ''


async def binding_for_server(store, *, tenant_id: str, user_id: str,
                              server_id: str):
    """只取**这个服务端**的绑定。取不到别的服务端的钥匙来顶。"""
    if store is None or not server_id:
        return None
    row = await store.server_binding(tenant_id, server_id)
    if not row or not row.get('enabled'):
        return None
    from app import tool_authorization as authorization_module
    return await authorization_module.resolve_binding(
        store, tenant_id=tenant_id, user_id=user_id,
        server_binding_id=str(row.get('binding_id') or ''))


async def bearer_for(store, *, tenant_id: str, user_id: str,
                     server_id: str) -> str | None:
    """这次连接要用的明文。

    `None` = 调用方沿用服务端 / 环境 token。
    非空 = 必须用这把，禁止再退回全局 token。
    """
    binding = await binding_for_server(
        store, tenant_id=tenant_id, user_id=user_id, server_id=server_id)
    if binding is None:
        if policy_module.enforcement_enabled():
            raise credentials_module.CredentialMissing(
                f'租户 {tenant_id} 没有服务端 {server_id} 的可用凭据，拒绝调用')
        return None
    handle = credentials_module.materialize_credential(
        binding, expected_subject=user_id,
        ttl=float(settings.credential_cache_ttl or 0))
    return handle.secret


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
    from app import execution_control
    from app import ledger as ledger_module
    book = ledger_module.current_ledger()
    context = execution_control.require()
    store = getattr(book, 'store', None) if book is not None else None
    if store is None or context is None:
        if policy_module.enforcement_enabled():
            raise credentials_module.CredentialMissing(
                '强制模式下缺少执行主体，拒绝调用远端')
        return None
    run = await store.run(context.run_id)
    tenant_id = str((run or {}).get('tenant_id') or 'default')
    user_id = str((run or {}).get('owner_user_id') or '') or 'default'
    return await bearer_for(
        store, tenant_id=tenant_id, user_id=user_id, server_id=server.id)
