"""两个不同领域的协议测试服务端，共用协议，不需要中台业务适配器。"""
import asyncio
import json
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

server = Server('contract-fixture')
domain = os.getenv('TEST_DOMAIN', 'inventory')
records = {}


@server.list_tools()
async def list_tools():
    return [Tool(
        name='create_record', description=f'在 {domain} 新建记录，完成后返回 record_id。',
        inputSchema={'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'operation_id': {'type': 'string'}},
            'required': ['name', 'operation_id'], 'additionalProperties': False},
        outputSchema={'type': 'object', 'properties': {
            'record_id': {'type': 'string'}, 'operation_id': {'type': 'string'}},
            'required': ['record_id', 'operation_id']},
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                    idempotentHint=True),
        _meta={'agent_hub': {'version': 1, 'idempotency_field': 'operation_id'}})]


@server.call_tool()
async def call(name, arguments):
    if name != 'create_record':
        return CallToolResult(isError=True, content=[TextContent(type='text', text='unknown tool')])
    op = arguments['operation_id']
    if op not in records:
        records[op] = {'record_id': f'{domain}-{len(records) + 1}', 'operation_id': op}
    payload = records[op]
    return CallToolResult(structuredContent=payload,
                          content=[TextContent(type='text', text=json.dumps(payload))])


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(main())
