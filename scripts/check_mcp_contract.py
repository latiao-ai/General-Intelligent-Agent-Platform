"""只读 MCP 接入验收：仅 initialize / tools/list，不调用业务工具。"""
import argparse
import asyncio
import json

from app import mcp_contract
from app.integrations import mcp_adapter, mcp_servers


async def check(server) -> dict:
    try:
        tools = await mcp_adapter.list_server_tools(server)
        entries = [{'name': item['name'], 'issues': mcp_contract.inspect_tool(item)}
                   for item in tools]
        errors = sum(issue['level'] == 'error' for entry in entries
                     for issue in entry['issues'])
        if not entries:
            return {'server': server.id, 'ok': False, 'error': '服务端未提供工具', 'tools': []}
        return {'server': server.id, 'ok': errors == 0, 'error_count': errors,
                'tools': entries}
    except Exception as exc:
        # 不打印带认证参数的连接串或上游异常正文。
        return {'server': server.id, 'ok': False, 'error': type(exc).__name__, 'tools': []}


async def run(server_id: str = '') -> tuple[dict, int]:
    servers = mcp_servers.servers()
    if server_id:
        servers = [server for server in servers if server.id == server_id]
    try:
        reports = [await check(server) for server in servers]
        ok = bool(reports) and all(report['ok'] for report in reports)
        return {'profile': 'agent-hub-mcp-v1', 'ok': ok, 'servers': reports}, 0 if ok else 1
    finally:
        await mcp_adapter.close_stdio_sessions()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-id', default='')
    args = parser.parse_args()
    report, code = asyncio.run(run(args.server_id))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(code)


if __name__ == '__main__':
    main()
