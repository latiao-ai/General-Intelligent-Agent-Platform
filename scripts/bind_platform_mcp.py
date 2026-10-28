"""Register one platform's shared MCP access; no per-user setup required."""
import argparse
import asyncio

from app.config import settings
from app.integrations import mcp_servers
from app.pg_store import PostgresStore


async def bind_platform(store, *, tenant: str, platform: str, server: str,
                        credential_ref: str):
    definition = mcp_servers.by_id(server)
    if definition is None:
        raise ValueError('server must name an existing MCP_SERVERS entry')
    if not tenant or not platform:
        raise ValueError('tenant and platform are required')
    binding = await store.upsert_server_binding(
        tenant_id=tenant, server_id=server, transport=definition.transport)
    rows = await store.credential_bindings(
        tenant_id=tenant, server_binding_id=binding['binding_id'])
    # Re-running the command must not create competing active credentials.
    existing = next((row for row in rows
                     if row['subject_kind'] == 'tenant'
                     and row.get('scope', {}).get('platforms') == [platform]
                     and row.get('status') == 'active'), None)
    if existing and existing['credential_ref'] != credential_ref:
        raise ValueError('platform already bound with a different credential; rotate it explicitly')
    if existing is None:
        await store.upsert_credential_binding(
            tenant_id=tenant, subject_kind='tenant', subject_id=tenant,
            server_binding_id=binding['binding_id'], credential_ref=credential_ref,
            scope={'platforms': [platform]})
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
    args = parser.parse_args()
    store = PostgresStore(settings.database_url)
    try:
        await store.ready()
        await bind_platform(store, tenant=args.tenant, platform=args.platform,
                            server=args.server, credential_ref=args.credential_ref)
        print(f'Bound platform {args.platform} to MCP server {args.server}; all platform customers enabled.')
    finally:
        await store.close()


if __name__ == '__main__':
    asyncio.run(main())
