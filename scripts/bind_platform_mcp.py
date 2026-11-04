"""Register one platform's shared MCP access; no per-user setup required.

两种受众：

- `--audience platform`（默认）：只有该平台的登录用户能用这个服务端；
- `--audience global`：**通用服务**（部署、本地生成这类"谁都能用"的能力），
  绑定挂在 `SHARED_TENANT` 下，任意平台、任意租户的运行都能解析到它。
  这是一条运维专属通道，必须显式设置 `ALLOW_GLOBAL_BIND=true`，
  管理 API 明确拒绝它（`webpage_agent/api/routes.py::_reject_reserved_scope`）。

通用服务仍可收敛范围：`--tools` 限制外层工具，`--inner-tools` 限制
`run_tool(name=…)` 的内层能力（不写 = 一个都不许，fail-closed）。
"""
import argparse
import asyncio
import os
import sys as _sys
from pathlib import Path as _Path

# 直接跑脚本时（python scripts/bind_platform_mcp.py）sys.path[0] 是 scripts/，
# 项目根不在其中，`import app.*` 会失败。这里显式补上，
# 让"直接跑"和"python -m scripts.bind_platform_mcp"都能用。
_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT / 'src'))

from webpage_agent.core import credentials
from webpage_agent.core.config import settings
from webpage_agent.integrations import mcp_servers
from webpage_agent.store.pg_store import PostgresStore


def _split(raw: str) -> list:
    return [item.strip() for item in str(raw or '').split(',') if item.strip()]


async def bind_platform(store, *, tenant: str, platform: str, server: str,
                        credential_ref: str, audience: str = 'platform',
                        tools: list | None = None,
                        inner_tools: list | None = None):
    definition = mcp_servers.by_id(server)
    if definition is None:
        raise ValueError('server must name an existing MCP_SERVERS entry')
    if audience not in ('platform', 'global'):
        raise ValueError('audience must be platform or global')
    if audience == 'global':
        tenant = credentials.SHARED_TENANT
        platform = '*'
    if not tenant or not platform:
        raise ValueError('tenant and platform are required')
    binding = await store.upsert_server_binding(
        tenant_id=tenant, server_id=server, transport=definition.transport)
    rows = await store.credential_bindings(
        tenant_id=tenant, server_binding_id=binding['binding_id'])
    scope = {'platforms': [platform]}
    if audience == 'global':
        # 跨租户是**显式**的：没有这个键，共享行只会对 `__shared__` 自己生效。
        scope['tenants'] = ['*']
        if tools:
            scope['tools'] = list(tools)
        if inner_tools is not None:
            scope['inner_tools'] = list(inner_tools)
    # Re-running the command must not create competing active credentials.
    existing = next((row for row in rows
                     if row['subject_kind'] == 'tenant'
                     and row.get('scope', {}).get('platforms') == [platform]
                     and row.get('status') == 'active'), None)
    if existing and existing['credential_ref'] != credential_ref:
        raise ValueError('platform already bound with a different credential; rotate it explicitly')
    if existing is None or existing.get('scope') != scope:
        # 首次登记，或作用域变了（例如补了内层白名单）：原地更新那一条，
        # 不留第二条 active 绑定（两条同时 active 时解析顺序就成了运气）。
        row = await store.upsert_credential_binding(
            tenant_id=tenant, subject_kind='tenant', subject_id=tenant,
            server_binding_id=binding['binding_id'], credential_ref=credential_ref,
            scope=scope, binding_id=str((existing or {}).get('binding_id') or ''))
        credentials.invalidate_cache(str(row.get('binding_id') or ''))
    if audience != 'global':
        # 通用服务不需要逐租户政策（`authorize_call` 会补内置默认放行）。
        await store.upsert_tool_policy(
            tenant_id=tenant, tool_key=f'mcp:{server}:*', approval_mode='auto')
    await store.upsert_tool_policy(
        tenant_id=tenant, tool_key='local:answers:direct', effect='read', approval_mode='auto')


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--server', required=True)
    parser.add_argument('--credential-ref', required=True,
                        help='env:NAME / file:/path; literal: for an unauthenticated server')
    parser.add_argument('--audience', choices=('platform', 'global'),
                        default='platform')
    parser.add_argument('--tools', default='',
                        help='逗号分隔的外层工具白名单，例如 mcp:publishing:deploy_site')
    parser.add_argument('--inner-tools', default=None,
                        help='逗号分隔的内层能力白名单（run_tool 的 name）；'
                             '通用服务不写＝一个都不许')
    args = parser.parse_args()
    if args.audience == 'global' and os.getenv('ALLOW_GLOBAL_BIND', '').strip().lower() not in (
            '1', 'true', 'yes'):
        parser.error('全局绑定需要显式设置 ALLOW_GLOBAL_BIND=true（运维专属通道）')
    store = PostgresStore(settings.database_url)
    try:
        await store.ready()
        await bind_platform(
            store, tenant=args.tenant, platform=args.platform, server=args.server,
            credential_ref=args.credential_ref, audience=args.audience,
            tools=_split(args.tools),
            inner_tools=None if args.inner_tools is None else _split(args.inner_tools))
        if args.audience == 'global':
            print(f'Bound universal MCP server {args.server}; visible to every platform and tenant.')
        else:
            print(f'Bound platform {args.platform} to MCP server {args.server}; all platform customers enabled.')
    finally:
        await store.close()


if __name__ == '__main__':
    asyncio.run(main())
