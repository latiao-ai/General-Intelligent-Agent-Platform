import os
import json
import base64
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from jsonschema import validate
from mcp import ClientSession
import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client, StdioServerParameters

# 本模块直接读 os.environ，导入 config 触发 load_dotenv，
# 避免单独调用 connect_session 时出现 KeyError: 'MCP_URL'。
from app import config as _config  # noqa: F401


@asynccontextmanager
async def connect_session():
    transport = os.getenv('MCP_TRANSPORT', 'streamable_http')
    token = os.getenv('MCP_BEARER_TOKEN', '')
    headers = {'Authorization': 'Bearer ' + token} if token else None
    if transport == 'stdio':
        command = os.environ['MCP_STDIO_COMMAND']
        args = json.loads(os.getenv('MCP_STDIO_ARGS', '[]'))
        params = StdioServerParameters(command=command, args=args)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == 'sse':
        async with sse_client(os.environ['MCP_URL'], headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == 'streamable_http':
        async with httpx.AsyncClient(headers=headers, timeout=90) as http_client:
            async with streamable_http_client(
                os.environ['MCP_URL'], http_client=http_client
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    else:
        raise ValueError('未知 MCP_TRANSPORT')


async def all_tools(session):
    tools = []
    cursor = None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools


async def list_server_tools():
    async with asyncio.timeout(30):
        async with connect_session() as session:
            tools = await all_tools(session)
            return [
                {'name': t.name, 'inputSchema': t.inputSchema}
                for t in tools
            ]


def result_payload(result):
    if getattr(result, 'isError', False):
        # 不将可能包含内部信息的服务端报错内容直接送到页面。
        raise RuntimeError('MCP 工具返回 isError，请在服务端查看日志')
    payload = getattr(result, 'structuredContent', None)
    if payload is not None:
        return payload
    for block in result.content:
        if getattr(block, 'type', None) == 'text':
            try:
                value = json.loads(block.text)
                if isinstance(value, dict):
                    return value
            except (ValueError, TypeError):
                continue
    raise RuntimeError('未找到 JSON 部署结果，请按真实返回格式修改 result_payload')


def dotted_get(payload, key):
    value = payload
    for part in key.split('.'):
        value = value[part]
    return value


def payload_mode(tool) -> str:
    """决定文件内容怎么传给部署工具。

    auto（默认）：工具 inputSchema 若是 files[{path,content}] 数组就用 files_base64，
    否则按纯文本字段传。可用 MCP_PAYLOAD_MODE 显式锁定 text / files_base64。
    """
    mode = os.getenv('MCP_PAYLOAD_MODE', 'auto').strip().lower()
    if mode in {'text', 'files_base64'}:
        return mode
    if mode != 'auto':
        raise ValueError('MCP_PAYLOAD_MODE 只能是 auto / text / files_base64')
    properties = (getattr(tool, 'inputSchema', None) or {}).get('properties') or {}
    files_schema = properties.get('files') or {}
    if files_schema.get('type') == 'array':
        item_properties = (files_schema.get('items') or {}).get('properties') or {}
        if 'path' in item_properties and 'content' in item_properties:
            return 'files_base64'
    return 'text'


def build_arguments(tool, artifact: dict) -> dict:
    arguments = json.loads(os.getenv('MCP_EXTRA_ARGS', '{}'))
    if not isinstance(arguments, dict):
        raise ValueError('MCP_EXTRA_ARGS 必须是 JSON 对象')
    if payload_mode(tool) == 'files_base64':
        # 服务端会把 content 当 base64 解码，且多数实现不校验合法性：
        # 传原始 HTML 文本会静默落成乱码站点，所以必须传真实的 base64 字节。
        raw = Path(artifact['html_path']).read_bytes()
        if not isinstance(arguments.setdefault('files', [{
            'path': os.getenv('MCP_ENTRY_FILENAME', 'index.html'),
            'content': base64.b64encode(raw).decode('ascii'),
        }]), list):
            raise ValueError('MCP_EXTRA_ARGS 中的 files 必须是数组')
    else:
        arguments[os.getenv('MCP_HTML_FIELD', 'html')] = Path(
            artifact['html_path']
        ).read_text(encoding='utf-8')
    return arguments


def result_url(payload) -> str:
    """按配置字段取访问地址，配置项缺失时回退到常见字段名。"""
    configured = os.getenv('MCP_RESULT_URL_FIELD', '').strip()
    candidates = ([configured] if configured else []) + ['public_url', 'url', 'entry_url']
    for candidate in dict.fromkeys(candidates):
        try:
            value = dotted_get(payload, candidate)
        except (KeyError, TypeError, IndexError):
            continue
        if isinstance(value, str) and value.strip():
            parsed = urlsplit(value)
            if parsed.scheme in {'http', 'https'} and parsed.hostname:
                return value
    raise RuntimeError('工具返回的访问地址无效，请核对 MCP_RESULT_URL_FIELD 字段映射')


async def call_tool(tool_name: str, arguments: dict, artifact: dict | None = None):
    """按真实 inputSchema 调用任意 MCP 工具。

    供动态编排使用：模型自己挑工具、自己给业务参数，但它给不出文件字节，
    所以当工具的 inputSchema 需要文件内容（files[{path,content}]）时，
    由这里用当前产物 `artifact` 自动补上，模型给的参数优先级更高（可覆盖）。
    返回解析后的 payload。
    """
    async with asyncio.timeout(90):
        async with connect_session() as session:
            tools = await all_tools(session)
            tool = next((t for t in tools if t.name == tool_name), None)
            if tool is None:
                raise RuntimeError(f'MCP 工具 {tool_name} 不存在')
            merged = dict(arguments or {})
            if artifact is not None and payload_mode(tool) == 'files_base64':
                merged = {**build_arguments(tool, artifact), **merged}
            validate(instance=merged, schema=tool.inputSchema)
            result = await session.call_tool(tool_name, arguments=merged)
            return result_payload(result)


async def deploy_html(artifact: dict):
    tool_name = os.environ['MCP_TOOL_NAME']
    # 先发现工具、按真实 inputSchema 组装参数；文件上传/服务端路径需另行改造。
    payload = await call_tool(tool_name, {}, artifact=artifact)
    url = result_url(payload)
    site_id = payload.get('site_id') if isinstance(payload, dict) else None
    return {
        'status': 'success',
        'deployment_id': site_id,
        'public_url': url,
        'content_hash': artifact['sha256'],
    }
