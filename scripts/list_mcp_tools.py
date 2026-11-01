"""逐个服务端发现工具：接新服务端时的第一站。

这个脚本回答三个问题：连不连得上、暴露了哪些工具、参数长什么样。拿到之后再
去配置里决定 deploy_tool 或写规划规则。

```bash
python -m scripts.list_mcp_tools                     # 探测全部服务端
python -m scripts.list_mcp_tools --subject alice      # 探测要求登录主体的服务端
```

一个服务端挂掉只影响它自己：其余服务端的结果照常打印；只要有任意一个服务端
失败，进程退出码就是 1（全部成功才算通过，便于放进自检流程）。

`forward_subject: true` 的服务端要求"已验证的登录来源主体"。真实执行时这个主体
来自账本里的运行行，探测脚本没有运行行，所以必须由 `--subject` 显式给出——不给
就直说缺什么，而不是抛一句看不出原因的错。
"""
import argparse
import asyncio
import json
from contextlib import ExitStack, contextmanager, nullcontext
# 直接跑脚本时（python scripts/xxx.py）sys.path[0] 是 scripts/，项目根不在其中，
# 导入 app.* 会失败。这里显式补上，让"直接跑"和"python -m scripts.xxx"都能用。
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from webpage_agent.core.config import settings
from webpage_agent.integrations.mcp_adapter import describe_error, list_server_tools
from webpage_agent.integrations import mcp_servers


class _ProbeStore:
    """探测用仓储：只回答 `run()`。

    主体由命令行给出，不伪造真实运行行——真跑时的主体永远来自数据库。
    """

    def __init__(self, subject: str) -> None:
        self.subject = subject

    async def run(self, run_id: str) -> dict:
        return {'run_id': run_id, 'tenant_id': 'probe',
                'owner_user_id': self.subject, 'platform_subject': self.subject,
                'platform_id': ''}


@contextmanager
def _probe_context(subject: str):
    """给 `forward_subject` 服务端装一个显式声明的执行主体。

    传输层（`mcp_adapter._borrow_stdio`）从账本取 `platform_subject`，取不到
    就拒绝建连。这里只服务本脚本，不改变任何线上调用路径。
    """
    from webpage_agent.governance import execution_control
    from webpage_agent.store import ledger
    context = execution_control.ExecutionContext('probe', lease_epoch=1)
    book = ledger.ActionLedger(_ProbeStore(subject), 'probe', 1, context=context)
    with ExitStack() as stack:
        stack.enter_context(execution_control.use(context))
        stack.enter_context(ledger.use_ledger(book))
        yield


async def _one(server) -> dict:
    """发现单个服务端。失败也把 transport/地址带上，否则排障时分不清是谁挂了。"""
    head = {'transport': server.transport,
            'url': server.url or server.stdio_command}
    try:
        return {**head, 'tools': await list_server_tools(server)}
    except Exception as exc:                       # noqa: BLE001
        # anyio 会把真实原因包进 ExceptionGroup，这里拆开再报
        return {**head, 'error': describe_error(exc)}


async def discover_all(subject: str = '') -> dict:
    """逐个服务端发现工具。一个服务端挂掉不影响其他服务端的结果。"""
    out = {}
    pending = []
    for server in mcp_servers.servers():
        if getattr(server, 'forward_subject', False) and not subject:
            out[server.id] = {
                'transport': server.transport,
                'url': server.url or server.stdio_command,
                'error': '该服务端要求已验证的登录来源主体（forward_subject）；'
                         '探测脚本没有运行行，加 --subject <主体名> 再试',
            }
            continue
        pending.append(server)
    if not pending:
        return out
    with _probe_context(subject) if subject else nullcontext():
        for server in pending:
            out[server.id] = await _one(server)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='探测 MCP 服务端暴露的工具')
    parser.add_argument('--subject', default='',
                        help='已验证的登录来源主体；探测 forward_subject 服务端时必填')
    args = parser.parse_args(argv)
    if not mcp_servers.servers():
        print('未配置 MCP 服务端：设置 MCP_SERVERS 或 MCP_URL')
        return 1
    result = asyncio.run(discover_all(args.subject))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if any('error' in item for item in result.values()) else 0


if __name__ == '__main__':
    raise SystemExit(main())
