"""MCP 客户端：连接、发现、调用。

只负责"怎么跟一个 MCP 服务端说话"，**不负责选哪个服务端**——那是
`app/mcp_servers.py` 的事。所有函数都接受一个 `server` 参数：

- 传 `Server`：按它给的地址/传输/参数连；
- 传 `None`：退回旧的单项环境变量（`MCP_URL` 等），保持既有调用点与测试可用。
"""
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
from app.integrations import mcp_servers


def _cfg(server, field: str, env: str, default: str = '') -> str:
    """取配置：显式传入的服务端优先，否则退回环境变量。

    这样"多服务端"与"老的单项环境变量"共用同一套连接代码，不需要两套实现。
    """
    if server is not None:
        value = getattr(server, field, '') or ''
        if value:
            return str(value)
    return os.getenv(env, default)


def _extra_args(server) -> dict:
    if server is not None and server.extra_args:
        return dict(server.extra_args)
    raw = os.getenv('MCP_EXTRA_ARGS', '{}')
    value = json.loads(raw or '{}')
    if not isinstance(value, dict):
        raise ValueError('MCP_EXTRA_ARGS 必须是 JSON 对象')
    return value


@asynccontextmanager
async def connect_session(server=None):
    transport = _cfg(server, 'transport', 'MCP_TRANSPORT', 'streamable_http')
    token = _cfg(server, 'token', 'MCP_BEARER_TOKEN', '')
    headers = {'Authorization': 'Bearer ' + token} if token else None
    if transport == 'stdio':
        command = _cfg(server, 'stdio_command', 'MCP_STDIO_COMMAND')
        if not command:
            raise ValueError('stdio 传输缺少启动命令')
        raw_args = getattr(server, 'stdio_args', ()) if server is not None else ()
        args = list(raw_args) if raw_args else json.loads(
            os.getenv('MCP_STDIO_ARGS', '[]'))
        params = StdioServerParameters(command=command, args=args)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == 'sse':
        url = _cfg(server, 'url', 'MCP_URL')
        if not url:
            raise ValueError('SSE 传输缺少 url')
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == 'streamable_http':
        url = _cfg(server, 'url', 'MCP_URL')
        if not url:
            raise ValueError('streamable_http 传输缺少 url')
        async with httpx.AsyncClient(headers=headers, timeout=90) as http_client:
            async with streamable_http_client(
                url, http_client=http_client
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    else:
        raise ValueError(f'未知 MCP_TRANSPORT：{transport}')


async def all_tools(session):
    tools = []
    cursor = None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools


def _annotations(tool) -> dict:
    """把 MCP annotations 摊平成普通 dict。

    这几个标记决定了"能不能自动重试""要不要先确认"，是安全策略的输入，
    只靠工具名字去猜（比如看到 delete 就认为危险）是不可靠的。
    """
    raw = getattr(tool, 'annotations', None)
    if raw is None:
        return {}
    keys = ('title', 'readOnlyHint', 'destructiveHint',
            'idempotentHint', 'openWorldHint')
    out = {}
    for key in keys:
        value = getattr(raw, key, None)
        if value is not None:
            out[key] = value
    return out


def effect_type(tool) -> str:
    """按 annotations 判定动作性质：read / destructive / write / unknown。

    服务端没说就别猜（unknown）——文档要求"缺少行为信息时保持 unknown，
    不根据名字猜测可重试性"。
    """
    notes = _annotations(tool)
    if notes.get('destructiveHint') is True:
        return 'destructive'
    if notes.get('readOnlyHint') is True:
        return 'read'
    if notes.get('readOnlyHint') is False or notes.get('idempotentHint') is not None:
        return 'write'
    return 'unknown'


async def list_server_tools(server=None):
    """发现服务端工具，尽量把能力信息完整带回。

    早先只保留 name 与 inputSchema，description/annotations 全被丢掉——
    规划器只能靠工具名猜用途（`list_sites` 还猜得出来，`get_upload_instructions`
    这类就完全看不出）。能力描述必须在规划前就到手。
    """
    async with asyncio.timeout(30):
        async with connect_session(server) as session:
            tools = await all_tools(session)
            return [{
                'name': t.name,
                'title': getattr(t, 'title', None) or '',
                'description': getattr(t, 'description', None) or '',
                'inputSchema': t.inputSchema,
                'outputSchema': getattr(t, 'outputSchema', None),
                'annotations': _annotations(t),
                'effect_type': effect_type(t),
            } for t in tools]


class MCPToolFailure(RuntimeError):
    """MCP 调用在**业务层**失败：服务端回了 `status: error` 这类结构化失败。

    很多 MCP 服务端不用协议层的 `isError`，而是正常返回一条
    `{"status": "error", "code": "E_...", "message": "..."}`——把它当成功继续
    往下走，就会出现"一路显示完成、最后什么也没有"。所以这里必须在拿到回执
    的那一刻就判失败。
    """

    def __init__(self, message: str, code: str = '') -> None:
        # 错误码必须出现在文案里：用户看到的就这一句，丢码等于丢掉唯一线索
        if code and code not in message:
            message = f'{message}（{code}）'
        super().__init__(message)
        self.code = code
        self.safe_message = message
        # 服务端**回了回执**并说这次失败：执行确定性是 failed_confirmed，
        # 不是 unknown。这条标注决定了账本允许重试、也不需要人工核实（§6.1）。
        self.certainty = 'failed_confirmed'
        self.phase = 'decode'
        self.error_code = code or 'E_MCP_TOOL_FAILURE'


# 服务端用来表示"这次调用失败"的状态词（各家说法不一，都收）
FAILURE_STATUSES = frozenset({'error', 'failed', 'failure', 'errored', 'fatal'})


def failure_of(payload) -> str:
    """回执里的业务失败信息；成功就返回空串。

    只认**明确**的失败信号：`status` 是失败词、`ok/success` 为 False、
    或者 `error` 字段有内容。没有这些信号就按成功处理——不猜。
    """
    if not isinstance(payload, dict):
        return ''
    status = str(payload.get('status') or '').strip().lower()
    code = str(payload.get('code') or '').strip()
    if status in FAILURE_STATUSES:
        detail = payload.get('message') or payload.get('error') or '服务端返回失败'
        return f'{detail}' + (f'（{code}）' if code else '')
    if code.startswith('E_') and status in {'', 'error'}:
        return f'{payload.get("message") or code}（{code}）'
    for flag in ('ok', 'success'):
        if flag in payload and payload[flag] is False:
            return str(payload.get('message') or f'{flag}=false')
    error = payload.get('error')
    if error:
        if isinstance(error, dict):
            error = error.get('message') or error.get('code') or '服务端返回 error'
        return str(error)
    return ''


def summarize(payload, limit: int = 120) -> str:
    """回执的一句话摘要，给事件流看。失败优先，其次取有意义的字段。"""
    failed = failure_of(payload)
    if failed:
        return f'失败：{failed}'[:limit]
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False, default=str)[:limit]
    for key in ('status', 'job_id', 'project_id', 'site_id', 'public_url',
                'url', 'path', 'output_path'):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return f'{key}={value}'[:limit]
    keys = ', '.join(list(payload)[:6])
    return f'返回 {keys}'[:limit]


def result_payload(result):
    if getattr(result, 'isError', False):
        # 不将可能包含内部信息的服务端报错内容直接送到页面。
        raise RuntimeError('MCP 工具返回 isError，请在服务端查看日志')
    payload = getattr(result, 'structuredContent', None)
    if payload is None:
        for block in result.content:
            if getattr(block, 'type', None) == 'text':
                try:
                    value = json.loads(block.text)
                    if isinstance(value, dict):
                        payload = value
                        break
                except (ValueError, TypeError):
                    continue
    if payload is None:
        raise RuntimeError('未找到 JSON 结果，请按真实返回格式修改 result_payload')
    # 协议层成功 ≠ 业务成功：服务端可能正常返回一条 status:error
    failed = failure_of(payload)
    if failed:
        raise MCPToolFailure(failed, str(payload.get('code') or ''))
    return payload


def certainty_of_error(exc: BaseException) -> str:
    '''本次失败的执行确定性。

    - 服务端明确回了业务失败 → failed_confirmed（它执行了并拒绝/报错）；
    - 连接都建立不起来 → not_executed；
    - 其余（超时、断流、解析失败）→ unknown，因为请求可能已经送达并产生了副作用。
    '''
    if isinstance(exc, MCPToolFailure):
        return 'failed_confirmed'
    name = type(exc).__name__
    if name in {'ConnectError', 'ConnectTimeout', 'ConnectionRefusedError',
                'ClientConnectorError', 'LeaseLost'}:
        return 'not_executed'
    return 'unknown'


def dotted_get(payload, key):
    value = payload
    for part in key.split('.'):
        value = value[part]
    return value


def payload_mode(tool, server=None) -> str:
    """决定文件内容怎么传给部署工具。

    auto（默认）：工具 inputSchema 若是 files[{path,content}] 数组就用 files_base64，
    否则按纯文本字段传。可用服务端配置的 payload_mode 或 MCP_PAYLOAD_MODE 显式锁定。
    """
    mode = _cfg(server, 'payload_mode', 'MCP_PAYLOAD_MODE', 'auto').lower()
    if mode in {'text', 'files_base64'}:
        return mode
    if mode != 'auto':
        raise ValueError('payload_mode 只能是 auto / text / files_base64')
    properties = (getattr(tool, 'inputSchema', None) or {}).get('properties') or {}
    files_schema = properties.get('files') or {}
    if files_schema.get('type') == 'array':
        item_properties = (files_schema.get('items') or {}).get('properties') or {}
        if 'path' in item_properties and 'content' in item_properties:
            return 'files_base64'
    return 'text'


def build_arguments(tool, artifact: dict, server=None) -> dict:
    arguments = _extra_args(server)
    if payload_mode(tool, server) == 'files_base64':
        # 服务端会把 content 当 base64 解码，且多数实现不校验合法性：
        # 传原始 HTML 文本会静默落成乱码站点，所以必须传真实的 base64 字节。
        raw = Path(artifact['html_path']).read_bytes()
        entry = _cfg(server, 'entry_filename', 'MCP_ENTRY_FILENAME', 'index.html')
        if not isinstance(arguments.setdefault('files', [{
            'path': entry,
            'content': base64.b64encode(raw).decode('ascii'),
        }]), list):
            raise ValueError('MCP_EXTRA_ARGS 中的 files 必须是数组')
    else:
        field = _cfg(server, 'html_field', 'MCP_HTML_FIELD', 'html')
        arguments[field] = Path(artifact['html_path']).read_text(encoding='utf-8')
    return arguments


def result_url(payload, server=None) -> str:
    """按配置字段取访问地址，配置项缺失时回退到常见字段名。"""
    configured = _cfg(server, 'result_url_field', 'MCP_RESULT_URL_FIELD', '').strip()
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
    raise RuntimeError('工具返回的访问地址无效，请核对 result_url_field 配置')


async def call_tool(tool_name: str, arguments: dict, artifact: dict | None = None,
                    server=None):
    """按真实 inputSchema 调用任意 MCP 工具。

    供动态编排使用：模型自己挑工具、自己给业务参数，但它给不出文件字节，
    所以当工具的 inputSchema 需要文件内容（files[{path,content}]）时，
    由这里用当前产物 `artifact` 自动补上，模型给的参数优先级更高（可覆盖）。

    `server` 不传时按工具名前缀路由（多服务端），再退回默认服务端——这样
    "旧调用点只给工具名"仍然能用。
    """
    if server is None:
        routed, bare = mcp_servers.split_tool_name(tool_name)
        server = routed or mcp_servers.deploy_server()
        tool_name = bare
    async with asyncio.timeout(90):
        async with connect_session(server) as session:
            tools = await all_tools(session)
            tool = next((t for t in tools if t.name == tool_name), None)
            if tool is None:
                raise RuntimeError(f'MCP 工具 {tool_name} 不存在')
            merged = dict(arguments or {})
            if artifact is not None and payload_mode(tool, server) == 'files_base64':
                merged = {**build_arguments(tool, artifact, server), **merged}
            validate(instance=merged, schema=tool.inputSchema)
            result = await session.call_tool(tool_name, arguments=merged)
            return result_payload(result)
