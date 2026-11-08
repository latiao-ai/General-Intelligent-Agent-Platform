"""受控 MCP 测试服务端（stdio transport）。

第九阶段协议验收用：一个真正走 MCP 协议的本地服务端，暴露两类工具，
让受控测试能经 `mcp_adapter.connect_session` 的**真实传输链路**验证
「只读工具同步执行」与「固定合同异步任务」两条路径，而不依赖任何外部供应商。

工具：

- `read_only_probe`：只读（`readOnlyHint=true`），回显确定性结果并记录调用
  次数，用于验证同步只读工具经完整 MCP transport 的调用、主体隔离与结果归一。
- `async_submit`：声明 `taskSupport=required`（只能走任务通道），用标准 MCP
  Tasks 原语创建后台任务，记录提交次数；后台工作完成时把带唯一标记的结果
  交给 `tasks/result`。用于验证「提交一次 → 等待 → tasks/get 查询 → 结果交付」
  的异步全链路。

计数通过 JSON 文件对外暴露（`--state-file`），跨进程可读：测试端据此断言
「远端提交恰好一次」「查询次数」「结果内容」，杜绝「模拟答复与远端结果硬编码
相同文本」这类证据不足。

用法：
    python -m tests.mcp_probe_server --state-file /tmp/probe.json

协议：stdio，一行一 JSON。`tasks/get`、`tasks/result`、`tasks/cancel` 由
`server.experimental.enable_tasks()` 自动注册默认处理器。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server

# 远端结果里带的唯一标记：测试端用它证明「模型答复消费的是真实远端结果」。
RESULT_MARKER = 'probe-远端结果-唯一标记'


class _State:
    """跨进程可见的远端计数与结果（JSON 文件）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.read_only_calls: int = 0
        self.async_submissions: int = 0
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                data = {}
            self.read_only_calls = int(data.get('read_only_calls') or 0)
            self.async_submissions = int(data.get('async_submissions') or 0)

    def flush(self) -> None:
        self.path.write_text(json.dumps({
            'read_only_calls': self.read_only_calls,
            'async_submissions': self.async_submissions,
        }, ensure_ascii=False), encoding='utf-8')


def read_state(path: str | Path) -> dict:
    """测试端读取远端计数（不依赖进程内状态，跨进程一致）。"""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def build_server(state_file: str) -> Server:
    state = _State(Path(state_file))
    server = Server('probe')
    # 启用标准 MCP Tasks 原语：自动注册 tasks/get、tasks/result、tasks/cancel。
    server.experimental.enable_tasks()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name='read_only_probe',
                description='只读探针：回显查询，返回带唯一标记的确定性结果',
                inputSchema={'type': 'object',
                             'properties': {'query': {'type': 'string'}},
                             'required': ['query']},
                annotations=types.ToolAnnotations(readOnlyHint=True),
            ),
            types.Tool(
                name='async_submit',
                description='异步探针：声明 taskSupport=required，只能走任务通道',
                inputSchema={'type': 'object',
                             'properties': {'payload': {'type': 'string'}},
                             'required': ['payload']},
                execution=types.ToolExecution(taskSupport='required'),
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name == 'read_only_probe':
            state.read_only_calls += 1
            state.flush()
            query = str(arguments.get('query') or '')
            return [types.TextContent(
                type='text',
                text=json.dumps({'ok': True, 'echo': query,
                                 'marker': RESULT_MARKER},
                                ensure_ascii=False))]

        if name == 'async_submit':
            state.async_submissions += 1
            state.flush()
            payload = str(arguments.get('payload') or '')
            ctx = server.request_context

            async def work(task_ctx):
                # 模拟远端异步执行：先标 working，稍后完成并返回结果。
                await task_ctx.update_status('处理中')
                await asyncio.sleep(0.05)
                structured = {
                    'ok': True,
                    'payload': payload,
                    'marker': RESULT_MARKER,
                    'completed_at': time.time(),
                }
                return types.CallToolResult(
                    content=[types.TextContent(
                        type='text', text=json.dumps(structured, ensure_ascii=False))],
                    structuredContent=structured,
                    isError=False,
                )

            return await ctx.experimental.run_task(work)

        raise RuntimeError(f'未知工具：{name}')

    return server


async def _run(state_file: str) -> None:
    from mcp.server.stdio import stdio_server
    server = build_server(state_file)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description='受控 MCP 测试服务端')
    parser.add_argument('--state-file', required=True,
                        help='计数与结果状态文件路径')
    args = parser.parse_args()
    asyncio.run(_run(args.state_file))


if __name__ == '__main__':
    main()
