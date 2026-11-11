"""按租户解析「这次 MCP 调用用哪把钥匙」。

连接定义仍在 `webpage_agent/integrations/mcp_servers.py`（地址、传输、工具名）。本模块只回答：
当前主体在这个 `server_id` 上有没有凭据绑定。明文只在返回前短时取出，
不放进可序列化的连接对象。

- 有绑定：用这把钥匙，不再读进程级 `MCP_BEARER_TOKEN`。
- 没有绑定且 `TOOL_POLICY_ENFORCED`：拒绝，远端调用数为 0。
- 没有绑定且未强制：返回 None，调用方沿用服务端配置里的 token（本地 Demo）。

例外是 `auth == 'oauth'` 的连接器：它**根本没有静态 token**，所以钥匙必须按
主体解析，与是否强制执行无关。`token_for()` 是给所有调用点用的统一入口，
`bearer_for_current()` 仍是其中"有执行主体"那条分支的实现。
"""
import logging

from webpage_agent.core import credentials as credentials_module
from webpage_agent.tools import tool_policy as policy_module
from webpage_agent.core.config import settings

logger = logging.getLogger(__name__)


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


async def token_for(server, *, tenant_id: str = '', user_id: str = '') -> str | None:
    """这次 MCP 调用用哪把钥匙。**发现工具与执行调用共用这一条解析。**

    和 `bearer_for_current` 只差一处：`auth == 'oauth'` 的服务端没有静态
    token，钥匙只能按主体解析。以前这条解析只挂在"强制执行"分支上，本地单用户
    模式（`TOOL_POLICY_ENFORCED=false`）于是永远裸连——OAuth 连接器每次发现都
    401，再被上层静默降级成"只有本地三个只读工具"。表现就是：连接器面板显示
    授权成功、70 个工具都在，会话里的模型却说自己只拿到 list_dir/read_file/
    web_search（真实事故）。

    解析顺序：

    1. 强制执行模式：完全沿用绑定表，生产语义不变；
    2. 有执行主体（业务 run）：仍优先走绑定表（凭据轮换、撤销都靠它）；
    3. 没有执行主体（聊天 turn、连接器面板、CLI 探测）：用调用方显式给的
       `(tenant_id, user_id)` 直接读本机凭据文件；
    4. 都没有：抛 `CredentialMissing`，让上层把原因写进日志与提示词，
       **不静默降级**。
    """
    if server is None:
        return await bearer_for_current(server)
    server_id = str(getattr(server, 'id', '') or '')
    if str(getattr(server, 'auth', '') or '').strip().lower() != 'oauth':
        # 静态 token 的连接器：语义与以前完全一致（强制模式下走绑定表）。
        return await bearer_for_current(server)

    if policy_module.enforcement_enabled():
        return await bearer_for_current(server)

    subject = None
    try:
        subject = await current_subject()
    except Exception:                                # noqa: BLE001
        logger.debug('取 MCP 执行主体失败，改用显式主体', exc_info=True)
    if subject is not None:
        store, run_tenant, run_user, platform_id = subject
        try:
            token = await bearer_for(
                store, tenant_id=run_tenant, user_id=run_user,
                server_id=server_id, platform_id=platform_id)
        except credentials_module.CredentialMissing:
            token = None
        except Exception:                            # noqa: BLE001
            logger.debug('按绑定表解析 OAuth 钥匙失败，改用本机凭据', exc_info=True)
            token = None
        if token:
            return token
        tenant_id = tenant_id or run_tenant
        user_id = user_id or run_user

    if not (tenant_id and user_id):
        raise credentials_module.CredentialMissing(
            f'连接器 {server_id} 需要用户授权，但这次调用没有登录主体，拿不到凭据')

    from webpage_agent.core import oauth_store
    return await oauth_store.ensure_access_token(
        server=server, server_id=server_id,
        tenant_id=tenant_id, user_id=user_id)


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
