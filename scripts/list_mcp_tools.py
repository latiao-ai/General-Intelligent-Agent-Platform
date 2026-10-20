import asyncio
import json
# 直接跑脚本时（python scripts/xxx.py）sys.path[0] 是 scripts/，项目根不在其中，
# 导入 app.* 会失败。这里显式补上，让"直接跑"和"python -m scripts.xxx"都能用。
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from app.config import settings  # noqa: F401  加载 .env
from app.mcp_adapter import list_server_tools
from app import mcp_servers


async def discover_all() -> dict:
    """逐个服务端发现工具。一个服务端挂掉不影响其他服务端的结果。

    这个脚本是接新服务端时的第一站：先看它到底暴露了什么工具、参数长什么样，
    再去配置里决定 deploy_tool 或写规划规则。
    """
    out = {}
    for server in mcp_servers.servers():
        try:
            out[server.id] = {
                'transport': server.transport,
                'url': server.url or server.stdio_command,
                'tools': await list_server_tools(server),
            }
        except Exception as exc:                   # noqa: BLE001
            out[server.id] = {'error': f'{type(exc).__name__}: {exc}'}
    return out


if __name__ == '__main__':
    if not mcp_servers.servers():
        print('未配置 MCP 服务端：设置 MCP_SERVERS 或 MCP_URL')
        raise SystemExit(1)
    print(json.dumps(asyncio.run(discover_all()), ensure_ascii=False, indent=2))
