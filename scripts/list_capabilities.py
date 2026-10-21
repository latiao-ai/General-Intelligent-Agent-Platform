"""列出每个 MCP 服务端**内部工具**的可用状态与安装提示。

用途：接到"通用调用型"服务端（一个 run_tool + 内部工具名）时，模型能调什么、
为什么调不了，一眼看清。比翻日志猜快得多。

```bash
python -m scripts.list_capabilities                    # 全部服务端
python -m scripts.list_capabilities --server openmontage
python -m scripts.list_capabilities --server openmontage --only-unavailable
```
"""
import argparse
import asyncio
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from app import tools                      # noqa: E402
from app.integrations import mcp_servers   # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', default='', help='只看某个服务端 id')
    parser.add_argument('--only-unavailable', action='store_true')
    args = parser.parse_args()

    await tools.mcp_tools(force=True)      # 触发发现：顺带读内部能力清单
    status = tools.mcp_status()
    for error_server, error in (status.get('errors') or {}).items():
        print(f'⚠️ 服务端 {error_server} 发现失败：{error}')

    targets = [s for s in mcp_servers.servers()
               if not args.server or s.id == args.server]
    if not targets:
        print('没有匹配的服务端（检查 .env 的 MCP_SERVERS）')
        return 1

    for server in targets:
        capabilities = tools.known_inner_names(server.id)
        print(f'\n=== {server.id} （{server.transport}）')
        if not capabilities:
            configured = getattr(server, 'capability_tool', '') or '（未配置）'
            print(f'  没有内部能力清单（capability_tool = {configured}）')
            continue
        usable = [n for n, i in capabilities.items()
                  if tools._is_available(i.get('status'))]
        unusable = [n for n in capabilities if n not in usable]
        print(f'  可用 {len(usable)} / {len(capabilities)}')
        for name in usable:
            if args.only_unavailable:
                continue
            info = capabilities[name]
            print(f'    ✅ {name:26} {info.get("signature") or ""}')
        for name in unusable:
            info = capabilities[name]
            hint = (info.get('install') or '').splitlines()[0][:70]
            print(f'    ⛔ {name:26} {info.get("status")}  {hint}')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
