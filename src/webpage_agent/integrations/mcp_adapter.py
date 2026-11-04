"""MCP 客户端：连接、发现、调用。

只负责"怎么跟一个 MCP 服务端说话"，**不负责选哪个服务端**——那是
`app/mcp_servers.py` 的事。所有函数都接受一个 `server` 参数：

- 传 `Server`：按它给的地址/传输/参数连；
- 传 `None`：退回旧的单项环境变量（`MCP_URL` 等），保持既有调用点与测试可用。
"""
import os
import re
import json
import base64
import asyncio
import logging
import importlib.metadata
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit
from jsonschema import ValidationError as JsonSchemaValidationError, validate
from mcp import ClientSession
import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client, StdioServerParameters

# 本模块直接读 os.environ，导入 config 触发 load_dotenv，
# 避免单独调用 connect_session 时出现 KeyError: 'MCP_URL'。
from webpage_agent.core.config import settings
from webpage_agent.integrations import mcp_servers, mcp_subject
from webpage_agent.core import credentials as credentials_module

logger = logging.getLogger(__name__)

# 错误文案的最长长度：够看出类型和原因，又不至于把整段 traceback 塞进界面。
ERROR_LIMIT = 200

# 唯一允许的 SDK 主版本。客户端按 1.x 的签名编写（见 all_tools / connect_session）。
SDK_MAX_MAJOR = 1


def unwrap_error(exc: BaseException, depth: int = 0) -> BaseException:
    """拆掉 anyio 任务组包的 `ExceptionGroup`，返回最里面的真实异常。

    MCP 客户端（anyio）连不上时抛的是 `ExceptionGroup: unhandled errors in a
    TaskGroup (1 sub-exception)`——真实原因（`httpx.ConnectError`、HTTP 401）
    在下一层。直接透出去等于没说，健康检查上也看不出是网络还是鉴权。
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions and depth < 4:
        exc = exc.exceptions[0]
        depth += 1
    return exc


def _redact_urls(text: str) -> str:
    """去掉文案里的地址：错误要能读，但不能把内网入口带出去。"""
    return re.sub(r'https?://[^\s\'")\]]+', '<url>', text)


def describe_error(exc: BaseException, limit: int = ERROR_LIMIT) -> str:
    """一句话错误：真实类型 + 原因（不含地址），供事件流与 /api/health 使用。

    HTTP 失败要带上状态码：`401` 和 `503` 的处理方式完全不同，而 SDK 抛出来的
    文案不一定都说清楚。
    """
    leaf = unwrap_error(exc)
    text = ' '.join(str(leaf).split()) or type(leaf).__name__
    status = getattr(getattr(leaf, 'response', None), 'status_code', None)
    if isinstance(status, int) and str(status) not in text:
        text = f'HTTP {status}：{text}'
    return f'{type(leaf).__name__}: {_redact_urls(text)}'[:limit]


_SDK_PROBLEM: str | None = None


class SDKIncompatible(RuntimeError):
    """装的 mcp SDK 主版本与本客户端的调用签名不匹配。

    独立成一个类型是因为它决定记账：版本不对时**一次远端都没发出去**，
    结果确定性是 `not_executed`，不该被当成"结果未知"去要人工核实。
    """


def sdk_problem() -> str:
    """装错 SDK 主版本时给一句能照做的提示；正常就返回空串。

    真踩过：用系统 python（mcp 2.x）跑探测脚本，`list_tools(cursor=...)` 报
    `TypeError`，看起来像"服务端连不上"，其实是调用签名换了一代。版本不对
    就不该让调用进到传输层再炸得莫名其妙。
    """
    global _SDK_PROBLEM
    if _SDK_PROBLEM is None:
        try:
            installed = importlib.metadata.version('mcp')
        except importlib.metadata.PackageNotFoundError:
            installed = ''
        match = re.match(r'(\d+)', str(installed or ''))
        major = int(match.group(1)) if match else 0
        if major > SDK_MAX_MAJOR:
            _SDK_PROBLEM = (
                f'已安装 mcp {installed}，本客户端按 {SDK_MAX_MAJOR}.x 的接口编写'
                '（requirements.txt 要求 mcp>=1.28,<2）。请用项目环境运行，'
                '或按 requirements.lock.txt 安装对应版本')
        else:
            _SDK_PROBLEM = ''
    return _SDK_PROBLEM


# 同一进程里按服务端复用 stdio 子进程。HTTP/SSE 每次调用仍然各自连接。
# 一把锁串行化同一个子进程上的借用，并让独立任务持有连接的整个生命周期。
_stdio_guard = asyncio.Lock()
_stdio_slots: dict[str, '_StdioSlot'] = {}
_stdio_idlers: set[asyncio.Task] = set()


class _StdioSlot:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.owner: asyncio.Task | None = None
        self.close_requested: asyncio.Event | None = None
        self.session = None
        self.generation = 0
        self.idle: asyncio.Task | None = None


def _stdio_key(server, command: str, args: list, token: str | None, env=None) -> str:
    server_id = getattr(server, 'id', '') or getattr(server, 'server_id', '') or ''
    return json.dumps([server_id, command, list(args), token or '', env or {}],
                      ensure_ascii=False)


def _session_broken(exc: BaseException) -> bool:
    """传输断了才丢掉子进程。业务失败（服务端回了 error）留着继续用。"""
    if isinstance(exc, BaseExceptionGroup):
        return any(_session_broken(item) for item in exc.exceptions)
    name = type(exc).__name__
    if name in {'BrokenResourceError', 'ClosedResourceError', 'EndOfStream',
                'ConnectionResetError', 'BrokenPipeError', 'ConnectionError'}:
        return True
    return isinstance(exc, (ConnectionError, EOFError, BrokenPipeError, TimeoutError))


async def _close_slot(slot: _StdioSlot) -> None:
    owner = slot.owner
    close_requested = slot.close_requested
    slot.session = None
    slot.owner = None
    slot.close_requested = None
    if owner is None:
        return
    if close_requested is not None:
        close_requested.set()
    try:
        await asyncio.shield(owner)
    except Exception:
        logger.warning('关闭 stdio 会话时出错', exc_info=True)


def _arm_idle(slot: _StdioSlot, generation: int) -> None:
    delay = settings.stdio_session_idle_seconds

    async def _later() -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with slot.lock:
            if slot.generation != generation or slot.session is None:
                return
            await _close_slot(slot)

    slot.idle = asyncio.create_task(_later())
    _stdio_idlers.add(slot.idle)
    slot.idle.add_done_callback(_stdio_idlers.discard)


async def _open_slot(slot: _StdioSlot, command: str, args: list, env=None) -> None:
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def own_session():
        # MCP/AnyIO 的取消作用域必须在同一个任务中进入和退出。
        async with AsyncExitStack() as stack:
            try:
                read, write = await stack.enter_async_context(
                    stdio_client(StdioServerParameters(command=command, args=args, env=env)))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                ready.set_result(session)
                await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise

    slot.close_requested = stop
    slot.owner = asyncio.create_task(own_session())
    try:
        slot.session = await asyncio.shield(ready)
    except BaseException:
        await _close_slot(slot)
        raise


async def current_run() -> dict:
    """当前执行链上的运行行（含 `platform_subject`）。

    主体**只能**来自这里：`runs.platform_subject` 由已验签的登录来源在
    `api_hub.create_goal` 里写入，模型参数、计划、MCP 配置都改不了它。
    stdio 用它塞环境变量，HTTP/SSE 用它签断言——两条传输共用一个来源，
    不会出现"这条通道的身份和那条不一样"。
    """
    from webpage_agent.governance import execution_control
    from webpage_agent.store import ledger
    context = execution_control.require()
    book = ledger.current_ledger()
    run = await book.store.run(context.run_id) if book and context else None
    subject = str((run or {}).get('platform_subject') or '')
    if not subject:
        raise RuntimeError('MCP 服务要求已验证的登录来源主体')
    return dict(run or {})


async def subject_assertion_headers(server) -> dict:
    """`forward_subject` 在 HTTP/SSE 上的形态：签名断言，不是明文主体。

    没配 `subject_secret_ref` 时**直接拒绝**，不退化成发明文头：
    明文头对端无法验真，一旦它真的拿来做身份判断，任何持有租户 token 的人
    都能冒充任意主体——这比"没传身份"更危险。
    """
    if not getattr(server, 'forward_subject', False):
        return {}
    secret_ref = str(getattr(server, 'subject_secret_ref', '') or '').strip()
    if not secret_ref:
        raise credentials_module.CredentialMissing(
            f'服务端 {getattr(server, "id", "")} 开了 forward_subject，但 HTTP/SSE '
            '传输需要 subject_secret_ref（env:/file:/literal:）：'
            '明文主体头对端无法验真，不能作为身份依据')
    secret = credentials_module.read_secret_ref(secret_ref)
    run = await current_run()
    token = mcp_subject.build(
        str(run.get('platform_subject') or ''),
        secret=secret,
        tenant_id=str(run.get('tenant_id') or ''),
        platform_id=str(run.get('platform_id') or ''),
        run_id=str(run.get('run_id') or ''),
        ttl_seconds=int(getattr(server, 'subject_ttl_seconds', 60) or 60),
    )
    header = str(getattr(server, 'subject_header', '') or '').strip() \
        or mcp_subject.DEFAULT_HEADER
    return {header: token}


@asynccontextmanager
async def _borrow_stdio(server, command: str, args: list, token: str | None):
    env = dict(getattr(server, 'stdio_env', {}) or {})
    if token is not None:
        env['MCP_BEARER_TOKEN'] = token
    if getattr(server, 'forward_subject', False):
        # stdio 的子进程是我们自己起的，环境变量就是可信通道；
        # HTTP/SSE 走跨机器通道，改用签名断言（见 subject_assertion_headers）。
        env['MCP_CALLER_SUBJECT'] = (await current_run()).get('platform_subject') or ''
    key = _stdio_key(server, command, args, token, env)
    async with _stdio_guard:
        slot = _stdio_slots.get(key)
        if slot is None:
            slot = _StdioSlot()
            _stdio_slots[key] = slot
    async with slot.lock:
        stale = slot.idle
        slot.idle = None
        if stale is not None and not stale.done():
            stale.cancel()
        slot.generation += 1
        if slot.owner is not None and slot.owner.done():
            await _close_slot(slot)
        if slot.session is None:
            await _open_slot(slot, command, args, env or None)
        try:
            yield slot.session
        except asyncio.CancelledError:
            await _close_slot(slot)
            raise
        except Exception as exc:
            if _session_broken(exc):
                await _close_slot(slot)
            raise
        finally:
            if slot.session is not None:
                slot.generation += 1
                if settings.stdio_session_idle_seconds <= 0:
                    await _close_slot(slot)
                else:
                    _arm_idle(slot, slot.generation)


async def close_stdio_sessions() -> None:
    """进程退出时关掉还开着的 stdio 子进程。"""
    async with _stdio_guard:
        slots = list(_stdio_slots.values())
        _stdio_slots.clear()
    for task in list(_stdio_idlers):
        task.cancel()
    for slot in slots:
        slot.idle = None
        async with slot.lock:
            slot.generation += 1
            await _close_slot(slot)
    for task in list(_stdio_idlers):
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def _cfg(server, field: str, env: str, default: str = '') -> str:
    """取配置：显式传入的服务端优先，否则退回环境变量。

    这样"多服务端"与"老的单项环境变量"共用同一套连接代码，不需要两套实现。
    """
    if server is not None:
        value = getattr(server, field, '') or ''
        if value:
            return str(value)
    return os.getenv(env, default)


def auth_headers(explicit: str | None, inherited: str) -> dict | None:
    """`explicit is None` 才沿用服务端 / 环境 token。显式传入（含空串）不再回退。"""
    token = inherited if explicit is None else explicit
    if not token:
        return None
    return {'Authorization': 'Bearer ' + token}


def _extra_args(server) -> dict:
    if server is not None and server.extra_args:
        return dict(server.extra_args)
    raw = os.getenv('MCP_EXTRA_ARGS', '{}')
    value = json.loads(raw or '{}')
    if not isinstance(value, dict):
        raise ValueError('MCP_EXTRA_ARGS 必须是 JSON 对象')
    return value


async def _secure_mcp_request(request):
    if settings.app_env != 'production':
        return
    from webpage_agent.observe.runtime_check import transport_issue
    if transport_issue(str(request.url), allow_loopback=os.getenv('MCP_ALLOW_LOOPBACK_HTTP', '').strip().lower() == 'true'):
        raise ValueError('Refusing insecure MCP endpoint or redirect')


def _sse_http_client(headers=None, timeout=None, auth=None):
    return httpx.AsyncClient(headers=headers, timeout=timeout or 90, auth=auth,
        follow_redirects=True, event_hooks={'request': [_secure_mcp_request]})


@asynccontextmanager
async def connect_session(server=None, *, token: str | None = None):
    """`token=None` 沿用服务端配置；传入字符串则只用它，不再读进程级 token。"""
    problem = sdk_problem()
    if problem:
        raise SDKIncompatible(problem)
    transport = _cfg(server, 'transport', 'MCP_TRANSPORT', 'streamable_http')
    headers = auth_headers(token, _cfg(server, 'token', 'MCP_BEARER_TOKEN', ''))
    if transport != 'stdio' and getattr(server, 'forward_subject', False):
        # 跨机器传输：身份用签名断言（对端可验），不是明文主体头。
        assertion = await subject_assertion_headers(server)
        if assertion:
            headers = {**(headers or {}), **assertion}
    if settings.app_env == 'production' and transport != 'stdio':
        from webpage_agent.observe.runtime_check import transport_issue
        url = _cfg(server, 'url', 'MCP_URL')
        if transport_issue(url, allow_loopback=os.getenv('MCP_ALLOW_LOOPBACK_HTTP', '').strip().lower() == 'true'):
            raise ValueError('Production MCP transport requires HTTPS or an allowed loopback tunnel')
    if transport == 'stdio':
        command = _cfg(server, 'stdio_command', 'MCP_STDIO_COMMAND')
        if not command:
            raise ValueError('stdio 传输缺少启动命令')
        raw_args = getattr(server, 'stdio_args', ()) if server is not None else ()
        args = list(raw_args) if raw_args else json.loads(
            os.getenv('MCP_STDIO_ARGS', '[]'))
        # stdio 的子进程本来就在本机，配置里的 token 就是给它用的：不传等于让
        # "配了鉴权的 stdio 服务端"静默变成未鉴权连接。显式传参优先（按租户解析
        # 出来的钥匙比配置更高），两者都没有才不带。
        stdio_token = token if token is not None else (getattr(server, 'token', '') or None)
        async with _borrow_stdio(server, command, args, stdio_token) as session:
            yield session
    elif transport == 'sse':
        url = _cfg(server, 'url', 'MCP_URL')
        if not url:
            raise ValueError('SSE 传输缺少 url')
        async with sse_client(url, headers=headers, httpx_client_factory=_sse_http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == 'streamable_http':
        url = _cfg(server, 'url', 'MCP_URL')
        if not url:
            raise ValueError('streamable_http 传输缺少 url')
        async with httpx.AsyncClient(headers=headers, timeout=90, event_hooks={'request': [_secure_mcp_request]}) as http_client:
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


async def list_server_tools(server=None, *, token: str | None = None):
    """发现服务端工具，尽量把能力信息完整带回。

    早先只保留 name 与 inputSchema，description/annotations 全被丢掉——
    规划器只能靠工具名猜用途（`list_sites` 还猜得出来，`get_upload_instructions`
    这类就完全看不出）。能力描述必须在规划前就到手。
    """
    async with asyncio.timeout(30):
        async with connect_session(server, token=token) as session:
            tools = await all_tools(session)
            return [{
                'name': t.name,
                'title': getattr(t, 'title', None) or '',
                'description': getattr(t, 'description', None) or '',
                'inputSchema': t.inputSchema,
                'outputSchema': getattr(t, 'outputSchema', None),
                'annotations': _annotations(t),
                'effect_type': effect_type(t),
                # 标准 MCP Tasks：工具自己声明能不能按任务执行。缺省 forbidden。
                # 这是"要不要写异步合同"的替代品——协议已经说清了，不必再猜。
                'task_support': task_support_of(t),
            } for t in tools]


def task_support_of(tool) -> str:
    """读 `Tool.execution.taskSupport`；缺省/非法一律 forbidden。"""
    execution = getattr(tool, 'execution', None)
    mode = getattr(execution, 'taskSupport', None) if execution is not None else None
    mode = str(mode or '').strip().lower()
    return mode if mode in ('forbidden', 'optional', 'required') else 'forbidden'


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


# 服务端明确「这次请求没有被应用」的原因词。
#
# 只有这类才能标成 failed_confirmed（账本允许直接重试、不需要人工核实）；
# 其余（超时、断流、解析失败）可能已经写进去了、只是回执丢了，必须保持 unknown。
# 判定标准只有一条：**服务端说的是"我没接受/没应用这次请求"**，不是"我应用了但结果不好"。
REJECTED_PREFIXES = ('invalid_', 'missing_', 'unsupported_', 'stale_')
REJECTED_SUFFIXES = ('_conflict', '_mismatch', '_rejected', '_forbidden',
                     '_unauthorized')
# 默认认两类：
#   1) 参数/前置条件本身不成立（invalid_argument / precondition_failed / revision_conflict …）
#   2) 入口鉴权类拒绝（forbidden / unauthorized / xxx_forbidden）——服务端在**动手之前**
#      就否掉了这次请求，重发既拿不到权、也谈不上重复写入。
# 故意不认裸 `conflict` / `not_found`：它们既可能是"根本没执行"，也可能是"改了一部分
# 才发现目标不对"，按确定性失败重试就会重复写入。确知对端语义时用该服务端的
# `deterministic_reasons` 白名单打开。
REJECTED_REASONS = frozenset({
    'invalid_argument', 'precondition_failed',
    'revision_conflict', 'node_kind_mismatch',
    'forbidden', 'unauthorized',
})


def rejected_without_apply(reason: str, server=None) -> bool:
    """这个原因是"请求被拒、没落地"吗？

    对端新增词表时不必改内核：在 `MCP_SERVERS` 条目上写
    `"deterministic_reasons": ["stale_revision"]` 就能扩展（配置只加词，
    不会把"可能已落地"的原因误判成确定性失败）。
    """
    text = str(reason or '').strip()
    if not text:
        return False
    extra = {str(item) for item in (getattr(server, 'deterministic_reasons', ()) or ())}
    if text in extra or text in REJECTED_REASONS:
        return True
    if text.startswith(REJECTED_PREFIXES):
        return True
    return text.endswith(REJECTED_SUFFIXES)


def result_payload(result, server=None):
    if getattr(result, 'isError', False):
        # MCP isError can carry a structured, user-facing validation error.
        # Preserve its reason so a rejected request is not mistaken for an
        # uncertain write. Unstructured/transport failures remain uncertain.
        error = getattr(result, 'structuredContent', None)
        if not isinstance(error, dict):
            for block in getattr(result, 'content', ()):
                if getattr(block, 'type', None) != 'text':
                    continue
                try:
                    error = json.loads(block.text)
                except (ValueError, TypeError):
                    continue
                if isinstance(error, dict):
                    break
        if isinstance(error, dict):
            reason = str(error.get('reason') or '').strip()
            message = error.get('message')
            # Only explicit parameter/precondition rejections establish that
            # the attempted write did not happen. Never expose raw traceback.
            if rejected_without_apply(reason, server) and isinstance(message, str) and message.strip():
                first_line = message.strip().splitlines()[0][:180]
                raise MCPToolFailure(first_line, reason)
        detail = ''
        if isinstance(error, dict) and error.get('reason'):
            detail = f'（服务端原因：{error.get("reason")}）'
        raise RuntimeError('MCP 工具返回 isError，执行结果待核对，请查看服务端日志' + detail)
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
    if isinstance(exc, JsonSchemaValidationError):
        return 'not_executed'
    name = type(exc).__name__
    if name in {'ConnectError', 'ConnectTimeout', 'ConnectionRefusedError',
                'ClientConnectorError', 'LeaseLost', 'CredentialMissing',
                'AuthorizationDenied', 'ApprovalRequired', 'SDKIncompatible'}:
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
                    server=None, *, token: str | None = None):
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
        async with connect_session(server, token=token) as session:
            tools = await all_tools(session)
            tool = next((t for t in tools if t.name == tool_name), None)
            if tool is None:
                raise RuntimeError(f'MCP 工具 {tool_name} 不存在')
            merged = dict(arguments or {})
            if artifact is not None and payload_mode(tool, server) == 'files_base64':
                merged = {**build_arguments(tool, artifact, server), **merged}
            validate(instance=merged, schema=tool.inputSchema)
            result = await session.call_tool(tool_name, arguments=merged)
            return result_payload(result, server)


def _task_payload(task) -> dict:
    """把协议 `Task` 归一成字典。字段名沿用协议原名，便于用 JSON Pointer 取值。"""
    return {
        'taskId': str(getattr(task, 'taskId', '') or ''),
        'status': str(getattr(task, 'status', '') or ''),
        'statusMessage': getattr(task, 'statusMessage', None),
        'pollInterval': getattr(task, 'pollInterval', None),
        'ttl': getattr(task, 'ttl', None),
    }


async def call_tool_as_task(tool_name: str, arguments: dict,
                            artifact: dict | None = None, server=None, *,
                            token: str | None = None, ttl_ms: int | None = None):
    """按标准 MCP Tasks 原语调用工具。

    与 `call_tool` 只差一点：请求带 `task={ttl}`，服务端回的不是
    `CallToolResult` 而是 `CreateTaskResult`（一个任务引用）。参数校验、
    产物注入、多服务端路由规则完全一致——**不存在第二条调用通道**。
    """
    if server is None:
        routed, bare = mcp_servers.split_tool_name(tool_name)
        server = routed or mcp_servers.deploy_server()
        tool_name = bare
    ttl = int(ttl_ms if ttl_ms is not None else settings.mcp_tasks_ttl_ms)
    async with asyncio.timeout(90):
        async with connect_session(server, token=token) as session:
            tools = await all_tools(session)
            tool = next((t for t in tools if t.name == tool_name), None)
            if tool is None:
                raise RuntimeError(f'MCP 工具 {tool_name} 不存在')
            merged = dict(arguments or {})
            if artifact is not None and payload_mode(tool, server) == 'files_base64':
                merged = {**build_arguments(tool, artifact, server), **merged}
            validate(instance=merged, schema=tool.inputSchema)
            result = await session.experimental.call_tool_as_task(
                tool_name, merged, ttl=ttl)
            return _task_payload(result.task)


async def task_status(task_id: str, server=None, *, token: str | None = None):
    """`tasks/get`：查协议任务状态；已完成时顺带把结果取回来。"""
    from mcp import types as mcp_types
    async with asyncio.timeout(90):
        async with connect_session(server, token=token) as session:
            payload = _task_payload(await session.experimental.get_task(str(task_id)))
            if payload['status'] == 'completed':
                try:
                    final = await session.experimental.get_task_result(
                        str(task_id), mcp_types.CallToolResult)
                    payload['result'] = result_payload(final)
                except Exception:  # noqa: BLE001  取结果失败不改变"已完成"这个事实
                    logger.warning('任务已完成但取结果失败 task=%s', task_id,
                                   exc_info=True)
            return payload


async def cancel_task(task_id: str, server=None, *,
                      token: str | None = None) -> dict:
    """`tasks/cancel`。取消是协作式的：服务端可能已经跑完了。"""
    async with asyncio.timeout(90):
        async with connect_session(server, token=token) as session:
            return _task_payload(
                await session.experimental.cancel_task(str(task_id)))
