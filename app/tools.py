"""工具清单：把**本地能力包注册的工具**与**运行时发现的 MCP 工具**汇总成一份。

这里只做三件事，**不认识任何具体业务**：

1. 从 `app.capabilities.registry` 取本地工具（能力包自己用 `@tool` 声明）；
2. 动态发现远端 MCP 工具，保留服务端给的完整能力描述与 annotations；
   部署工具也走这条（它是带平台适配器的 MCP 工具，不是另一条通道）；
3. 给规划器渲染工具清单。

两条来源必须分开：本地能力是进程内的静态声明（import 即注册），MCP 工具是运行时
发现（有 TTL、掉线、服务端前缀命名）。把它们混成一个注册表，"谁负责刷新"就说不清了。

加一个本地能力 = 在 `app/capabilities/` 下加一个文件，这个文件一个字都不用改。
"""
import asyncio
import json
import logging
import time

from app.integrations import mcp_adapter, mcp_servers
from app.capabilities import publishing as publishing_capability
from app.capabilities import registry
from app.capabilities.base import (ToolError, error_entry, tool_results_text,
                                   usable_documents, _select_sources, _with_text)
# Tool 定义搬到了注册器里（内核与能力包共用一份）；这里重新导出，
# 旧调用点与既有用例 `from app.tools import Tool` 继续可用。
from app.capabilities.registry import Tool
from app.config import settings

# 旧代码里 tool_module.search_backend / chat_reply / emit 的引用保留
from app.integrations import search as search_backend
from app.events import action, answer_delta, emit

logger = logging.getLogger(__name__)

# 本地工具：由能力包通过 @tool 声明，这里只做加载与导出
BUILTIN_TOOLS = registry.load()
BUILTIN_BY_NAME = {tool.name: tool for tool in BUILTIN_TOOLS}


def describe_tool(tool: Tool) -> str:
    """工具在规划器眼里的样子：名称 + 动作性质 + 描述 + 参数。

    动作性质必须一起给：规划器据此判断"这类操作要不要先向用户确认"，
    只靠名字（比如看到 delete）去猜是不可靠的。
    """
    return (f'- {tool.name}［{tool.effect}］：{tool.description}\n'
            f'  参数：{json.dumps(tool.args, ensure_ascii=False)}')

# MCP 是"外来能力"，规则只能给通用纪律，不能替它编业务语义
MCP_PROMPT_RULES = (
    '【MCP 工具】调用查询类 MCP 工具时，只有不带过滤参数（如 keyword）的结果才是'
    '全量清单；删改类操作（delete_*/update_*）之后若要告诉用户"还剩什么/还剩几个"，'
    '必须再加一步**不带过滤参数**的查询（如 mcp__list_sites 且参数为空），'
    '绝不能拿操作前的过滤结果或操作返回体去推断。'
    '【先发现，再调用】服务端要是有"清单/描述"类工具（名字或描述里带 list/describe/'
    'capabilities），先调它把真实能力读出来；通用调用类工具（如 run_tool）名字参数里的'
    '内部工具名**只能取自清单**，清单里没有就是没有，不要凭经验编。'
    '清单里标为不可用（缺依赖/缺 key/未安装）的工具不要调度。'
    '【创作内容自己写】剧本、分镜、旁白、文案这类内容由你自己产出，再用 write_file'
    '落盘；不要假设服务端有"生成脚本/生成分镜"的接口——多数服务端只提供执行动作。'
    '【封装工具也有依赖】像 render_video 这类"封装型"执行工具，内部还要用到清单里的'
    '工具（合成、字幕等）：调度前先确认它们可用；不可用就别调度，直接说清缺什么。'
    '【业务失败要认】工具返回 status: error / failed、或 code 以 E_ 开头，就是这一步'
    '失败：不要当成功继续，也不要换个参数重试同一个不存在的名字；如实报告失败原因'
    '与缺失的能力，并把已经产出的文件说清楚。'
)


# ---------- MCP 动态工具 ----------

MCP_PREFIX = mcp_servers.MCP_PREFIX

# 原始 MCP 工具仍全部暴露（保留最大能力），但重复/不可逆的那几个补一句说明，
# 让模型优先走有保障的路径，而不是靠删功能来限制它。
MCP_HINTS = {
    'delete_site': '⚠️ 不可逆。删除线上站点前必须先向用户复述将要删除的站点 ID 并确认。',
    'update_site': '⚠️ 会改动线上站点内容，执行前先说明改什么。',
    'list_sites': '不带 keyword 时返回全量站点；带了 keyword 只是筛选结果，不能当全量用。',
}
# 每次任务都去 discover 一次太慢（一次可能几十秒），进程内缓存一小段时间。
# 缓存要跟"服务端配置签名"绑定：改了 MCP_SERVERS 立刻重新发现，不能等 TTL。
_MCP_CACHE: dict = {'at': 0.0, 'tools': [], 'errors': {}, 'signature': (),
                    'capabilities': {}}
_MCP_TTL = 300.0


def _signature() -> tuple:
    # 部署开关也进签名：关掉部署后缓存里那个部署工具（或占位工具）必须立刻失效。
    return (settings.deploy_enabled,
            tuple((s.id, s.transport, s.url or s.stdio_command)
                  for s in mcp_servers.servers()))


async def _discover(server) -> tuple[list[dict], str]:
    """发现单个服务端的工具。**失败只影响它自己**，不拖垮其他服务端。"""
    try:
        return await mcp_adapter.list_server_tools(server), ''
    except Exception as exc:                       # noqa: BLE001
        logger.warning('MCP 服务端 %s 工具发现失败：%s', server.id,
                       type(exc).__name__, exc_info=True)
        return [], f'{type(exc).__name__}: {str(exc)[:160]}'


async def _discover_capabilities(server) -> dict:
    """读服务端的**内部能力清单**（例如 list_capabilities）。

    有些服务端把动作藏在**一个**通用工具后面（`run_tool(name="tts_selector")`）。
    这时只看到 `run_tool` 是没用的——模型只能猜内部名字，猜错就是一堆
    E_RESOURCE_NOT_FOUND。所以发现阶段顺带把清单读回来：既能让规划器看到真实
    名字，也能在调用前校验（见 `_check_inner_name`）。

    返回 {名字: {'status': .., 'description': ..}}；服务端不支持就返回空。
    """
    tool_name = getattr(server, 'capability_tool', '') or ''
    if not tool_name:
        return {}
    try:
        payload = await mcp_adapter.call_tool(tool_name, {}, server=server)
    except Exception as exc:                       # noqa: BLE001
        logger.warning('MCP 服务端 %s 的能力清单读取失败：%s', server.id,
                       type(exc).__name__)
        return {}
    items = payload.get('tools') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {}
    out = {}
    for item in items:
        if isinstance(item, dict) and item.get('name'):
            out[str(item['name'])] = {
                'status': str(item.get('status') or '').upper(),
                'description': str(item.get('description') or '')[:80],
                'capability': str(item.get('capability') or ''),
                # 服务端通常会说清"缺什么/怎么装"，透出来才能告诉用户
                'install': str(item.get('install_instructions') or '')[:160],
            }
    return out


def _is_available(status: str) -> bool:
    """服务端的状态字符串五花八门：`ToolStatus.AVAILABLE`、`AVAILABLE`、`available`…

    统一判断成"能用/不能用"，别拿全等去比——真踩过：带枚举前缀的字符串全被
    当成不可用，于是整台服务端被判成"什么都不能做"。
    """
    text = str(status or '').upper()
    if not text:
        return True                     # 服务端没报状态，按能用处理
    return 'AVAILABLE' in text and 'UNAVAILABLE' not in text


def _capability_line(capabilities: dict, limit: int = 30) -> str:
    """把内部能力清单压成一行，塞进工具描述里给规划器看。

    真实服务端可能有几十上百个内部工具，全列出来会把规划提示词撑爆，所以各自
    截断并标出总数——"还有 N 个"比把名字全塞进去更有用。
    """
    if not capabilities:
        return ''
    usable = [name for name, info in capabilities.items()
              if _is_available(info.get('status'))]
    unusable = [name for name in capabilities if name not in usable]
    parts = []
    if usable:
        parts.append('可用（只能从这里选）：' + '、'.join(usable[:limit])
                     + (f' 等 {len(usable)} 个' if len(usable) > limit else ''))
    else:
        parts.append('当前没有任何可用内部工具（缺依赖/缺 key/未安装）')
    if unusable:
        parts.append('不可用（别调度）：' + '、'.join(unusable[:limit])
                     + (f' 等 {len(unusable)} 个' if len(unusable) > limit else ''))
    return '；'.join(parts)


async def mcp_tools(force: bool = False) -> list[Tool]:
    """发现所有 MCP 服务端的工具，包装成可编排的 Tool。

    - 并发发现，单个服务端超时/掉线不影响其他服务端；
    - 多服务端时工具名带服务端前缀，避免同名工具互相覆盖；
    - 部署类工具需要文件内容，模型给不出 base64，执行时由 runner 用当前产物
      自动填充——模型只需要给出其余业务参数；
    - **部署工具也在这里注册**：它由服务端声明、身份是 `mcp:<server>:<tool>`，
      只是多挂了一个由 publishing 能力包提供的适配器（闸门 + 回执归一）。
    """
    now = time.monotonic()
    signature = _signature()
    cached = (_MCP_CACHE['tools'] and _MCP_CACHE.get('signature') == signature
              and now - _MCP_CACHE['at'] < _MCP_TTL)
    if cached and not force:
        return _MCP_CACHE['tools']

    servers = mcp_servers.servers()
    discovered = await asyncio.gather(*(_discover(server) for server in servers)) \
        if servers else []
    # 有"通用调用 + 内部工具名"设计的服务端，顺带把内部能力清单读回来：
    # 规划器要能看到真实名字，执行前也要能校验（见 _check_inner_name）
    capability_lists = await asyncio.gather(
        *(_discover_capabilities(server) for server in servers)) if servers else []
    capabilities = {server.id: caps
                    for server, caps in zip(servers, capability_lists) if caps}
    total = len(servers)
    tools: list[Tool] = []
    errors: dict[str, str] = {}
    for server, (items, error) in zip(servers, discovered):
        if error:
            errors[server.id] = error
            continue
        inner = capabilities.get(server.id) or {}
        for item in items:
            if publishing_capability.claims(server, item['name']):
                tools.append(_deploy_tool(server, item))
                continue
            name = mcp_servers.tool_name(server, item['name'], total)
            schema = item.get('inputSchema') or {'type': 'object', 'properties': {}}
            # 用服务端给的真实描述，而不是拿工具名猜用途
            title = item.get('title') or item['name']
            detail = (item.get('description') or '').strip()
            description = f'{title}（MCP 服务端 {server.id} 的工具 {item["name"]}）'
            if detail:
                description = f'{description}：{detail}'
            hint = MCP_HINTS.get(item['name'], '')
            if hint:
                description = f'{description} {hint}'
            if inner and item['name'] == getattr(server, 'capability_tool', ''):
                # 清单工具本身没什么可编排的，真正的价值是把名字告诉规划器
                line = _capability_line(inner)
                if line:
                    description = f'{description}；本服务端能力清单 → {line}'
            tools.append(Tool(
                name, f'MCP·{item["name"]}', description, schema,
                _make_mcp_runner(item['name'], server),
                tool_key=f'mcp:{server.id}:{item["name"]}',
                effect=item.get('effect_type') or 'unknown',
                capability='mcp',
                prompt_rules=MCP_PROMPT_RULES,
                server_id=server.id,
            ))
    if settings.deploy_enabled and not _has_publishing(tools):
        # 配置了部署工具，却没有从服务端发现到它（掉线、或服务端没声明这个工具）。
        # 留一个占位工具，让"部署到服务器"得到一句明确报错，而不是被当成未知
        # 工具静默跳过——那会变成"任务成功但什么都没发生"。
        stub = _deploy_placeholder(errors)
        if stub is not None:
            tools.append(stub)
    _MCP_CACHE.update(at=now, tools=tools, errors=errors, signature=signature,
                      capabilities=capabilities)
    return tools


def unusable_reason(server_id: str) -> str:
    """这个服务端当前"一个能用的内部工具都没有"时，返回一句人话原因。

    有清单、且清单里没有任何 AVAILABLE —— 说明缺依赖/缺 key/没装技能。
    这时候**不该**再调度它的执行类工具：跑了必然失败，只是在浪费步骤和时间。
    """
    caps = known_inner_names(server_id)
    if not caps:
        return ''
    usable = [name for name, item in caps.items()
              if _is_available(item.get('status'))]
    if usable:
        return ''
    hints = []
    for name, item in caps.items():
        install = (item.get('install') or '').strip()
        if install and install not in hints:
            hints.append(install)
        if len(hints) >= 2:
            break
    reason = (f'服务端 {server_id} 的 {len(caps)} 个内部工具当前全不可用'
              f'（缺依赖 / 缺 key / 未安装），执行类工具没法跑')
    if hints:
        reason += '；安装提示：' + '；'.join(hints)
    return reason


def known_inner_names(server_id: str) -> dict:
    """某个服务端上一次发现到的内部能力清单（没有就空）。"""
    return dict((_MCP_CACHE.get('capabilities') or {}).get(server_id) or {})


def _check_inner_name(server, args: dict, capabilities: dict) -> None:
    """调用**之前**校验内部工具名（run_tool.name 必须在发现结果里）。

    这类服务端只有一个通用调用工具，真正的动作名放在参数里。名字不存在时
    服务端只会回一条 E_RESOURCE_NOT_FOUND——对用户来说就是"跑了一分多钟
    什么也没有"。所以在本地先挡住，并把**真实可用的名字**写进报错里，
    让重规划有据可依。
    """
    arg = getattr(server, 'tool_name_arg', '') or ''
    if not arg:
        return
    wanted = str((args or {}).get(arg) or '').strip()
    if not wanted or not capabilities:
        return
    info = capabilities.get(wanted)
    if info is None:
        usable = [name for name, item in capabilities.items()
                  if _is_available(item.get('status'))]
        raise ToolError(
            f'内部工具名 {wanted} 不在服务端的能力清单里。'
            f'真实存在的是：{"、".join(sorted(capabilities)[:20])}'
            + (f'；其中当前可用：{"、".join(usable[:12])}' if usable
               else '；且它们当前都不可用'))
    status = (info.get('status') or '').upper()
    if not _is_available(info.get('status')):
        usable = [name for name, item in capabilities.items()
                  if _is_available(item.get('status'))]
        raise ToolError(
            f'内部工具 {wanted} 当前不可用（{status}）。'
            + (f'可用的只有：{"、".join(usable[:12])}' if usable
               else '该服务端当前没有任何可用工具，请如实说明能力缺失'))


def _has_publishing(tools) -> bool:
    return any(t.capability == publishing_capability.CAPABILITY for t in tools)


def _deploy_tool(server, item: dict) -> Tool:
    """把服务端的部署工具包装成平台认识的 `deploy_site`。

    参数 schema 故意只暴露空对象：文件内容由平台从当前产物注入（模型写不出
    base64），对外声明它只会引导模型编造一个 `files` 数组。
    """
    return Tool(
        publishing_capability.TOOL_NAME,
        publishing_capability.LABEL,
        (f'{publishing_capability.DESCRIPTION}'
         f'（MCP 服务端 {server.id} 的工具 {item["name"]}）'),
        {'type': 'object', 'properties': {}},
        _make_mcp_runner(item['name'], server,
                         adapter=publishing_capability.adapter(server, item['name'])),
        tool_key=f'mcp:{server.id}:{item["name"]}',
        effect=item.get('effect_type') or 'unknown',
        capability=publishing_capability.CAPABILITY,
        prompt_rules=publishing_capability.PROMPT_RULES,
        server_id=server.id,
    )


def _deploy_placeholder(errors: dict):
    """部署工具当前不可用时的占位工具（身份仍然是那个 MCP 工具）。"""
    for server in mcp_servers.servers():
        name = publishing_capability.configured_tool(server)
        if not name:
            continue
        reason = errors.get(server.id) or f'服务端 {server.id} 未提供该工具'
        return _unavailable_deploy(server, name, reason)
    return None


def _unavailable_deploy(server, tool_name: str, reason: str) -> Tool:
    async def run(args: dict, state: dict) -> dict:
        raise ToolError(f'部署不可用：{reason}。请检查 MCP 配置与服务端状态后重试')

    return Tool(
        publishing_capability.TOOL_NAME, publishing_capability.LABEL,
        f'{publishing_capability.DESCRIPTION}（当前不可用：{reason}）',
        {'type': 'object', 'properties': {}}, run,
        tool_key=f'mcp:{server.id}:{tool_name}',
        # 服务端没回话，动作性质只能按配置的用途记 write，不假装是 read。
        effect='write',
        capability=publishing_capability.CAPABILITY,
        prompt_rules=publishing_capability.PROMPT_RULES,
        server_id=server.id,
    )


def mcp_status() -> dict:
    """MCP 连接态势：给 /api/health 与排障用。"""
    return {'tools': len(_MCP_CACHE['tools']),
            'errors': dict(_MCP_CACHE.get('errors') or {}),
            'servers': mcp_servers.status()}


# 这些键属于图状态本身，回执里的同名字段不能覆盖它们
_STATE_KEYS = frozenset({'status', 'answer', 'artifact', 'documents', 'plan',
                         'errors', 'usage', 'deployment', 'mcp_calls', 'outcome',
                         'failure', 'step_outputs', 'step_index', 'documents'})


def _unwrap_error(exc: BaseException, depth: int = 0) -> BaseException:
    """拆掉 anyio 任务组包的 ExceptionGroup。

    MCP 客户端（anyio）经常抛 `ExceptionGroup`，直接透出去就是"ExceptionGroup"
    三个字，等于没说；真正的业务失败（比如 MCPToolFailure）在里面。
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions and depth < 4:
        exc = exc.exceptions[0]
        depth += 1
    return exc


def _describe_error(exc: BaseException) -> str:
    leaf = _unwrap_error(exc)
    text = str(leaf).strip() or type(leaf).__name__
    return f'{type(leaf).__name__}: {text}'[:200]


def _make_mcp_runner(server_tool_name: str, server=None, adapter=None):
    """所有远端工具共用的一条调用通道。

    `adapter` 是可选的**平台侧适配器**（目前只有部署用）：它在调用前后各插一个
    钩子，但传输、参数注入、`mcp_calls` 记录仍然走这里——这样"某个远端工具
    需要平台额外解释回执"不会变成"某个远端工具有一条自己的调用通道"。
    """
    async def run(args: dict, state: dict) -> dict:
        artifact = state.get('artifact')
        # 有"通用调用 + 内部工具名"设计的服务端：名字必须来自发现结果，
        # 否则直接停在这一步，并把真实可用的名字写进报错（不烧一次远端往返）。
        if server is not None:
            _check_inner_name(server, args or {},
                              known_inner_names(getattr(server, 'id', '')))
        if adapter is not None:
            precheck = getattr(adapter, 'precheck', None)
            if precheck is not None:
                precheck(dict(args or {}), state)
            pick = getattr(adapter, 'artifact', None)
            if pick is not None:
                artifact = pick(state)
            before = getattr(adapter, 'before', None)
            if before is not None:
                before(dict(args or {}), state)
        label = (f'{server.id}·{server_tool_name}' if server is not None
                 else server_tool_name)
        emit(action('调用 MCP 工具', label, '🔧'))
        try:
            # 不带 server 时保持旧调用形式：既兼容既有调用点，也让工具名自己
            # 带前缀时能走路由（mcp_servers.split_tool_name）。
            if server is None:
                payload = await mcp_adapter.call_tool(
                    server_tool_name, dict(args or {}), artifact=artifact)
            else:
                payload = await mcp_adapter.call_tool(
                    server_tool_name, dict(args or {}), artifact=artifact,
                    server=server)
        except Exception as exc:
            leaf = _unwrap_error(exc)
            if isinstance(leaf, mcp_adapter.MCPToolFailure):
                # 业务层失败（status: error / E_*）：必须当成失败停下，而且要把
                # 服务端原话透出来，否则用户只看到"没有产出可用结果"。
                # 但只取前两行：服务端常把整段 traceback 塞进 message，直接展示
                # 既看不懂又泄漏内部路径。
                detail = ' '.join(str(leaf).split())
                raise ToolError(
                    f'MCP 工具 {server_tool_name} 失败：{detail[:240]}') from exc
            raise ToolError(
                f'MCP 工具 {server_tool_name} 调用失败'
                f'（{_describe_error(exc)}）') from exc
        emit(action('MCP 工具返回', mcp_adapter.summarize(payload), '📦'))
        # 参数必须一起记：过滤类工具（如 list_sites 的 keyword）传不传参数，
        # 返回语义完全不同——不记下来就分不清"列表只有 1 个"和"只匹配到 1 个"。
        # 服务端也要记：多服务端时同名工具的行为可能完全不同。
        updates = {'mcp_calls': list(state.get('mcp_calls', [])) + [
            {'tool': server_tool_name, 'args': dict(args or {}),
             'server': getattr(server, 'id', '') or '',
             'payload': payload}]}
        # 回执里的标量也提到**步骤输出**上：下游要用
        # {"ref": {"step_id": "s1", "pointer": "/project_id"}} 取值，
        # 埋在 payload 里就取不到，只能编一个占位符。
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in _STATE_KEYS or not isinstance(value, (str, int, float, bool)):
                    continue
                updates.setdefault(key, value)
        interpret = getattr(adapter, 'interpret', None)
        if interpret is not None:
            updates.update(interpret(payload, dict(args or {}), state) or {})
        return updates
    return run


async def catalog() -> list[Tool]:
    """本轮可用的全部工具：本地能力包（注册来的）+ MCP 发现。"""
    tools = list(registry.registered())
    tools.extend(await mcp_tools())
    if not settings.deploy_enabled:
        # 关掉部署时连占位工具都不给，免得计划里出现注定失败的一步。
        tools = [t for t in tools
                 if t.capability != publishing_capability.CAPABILITY]
    return tools


async def find_tool(name: str):
    """按名字取工具：内置 → MCP 精确名 → 去掉服务端前缀后按**身份**再匹配。

    最后那步是为"续跑旧计划"服务的：工具名从 `mcp__delete_site` 变成了
    `mcp__ops__delete_site`、或者部署工具从 `mcp__deploy_site` 变成了平台的
    `deploy_site`，旧计划里的名字都不该直接失效。匹配不了名字就退到 tool_key
    的尾部——身份才是稳定的，名字只是对模型的展示形式。只有唯一命中才算数，
    两个服务端都有这个工具时不做猜测。
    """
    # 先看模块级快照（既有用例会 patch 它注入假工具），再看注册表的实时视图：
    # 这样"新增一个能力包文件"不需要动任何清单，名字也能解析到。
    tool = BUILTIN_BY_NAME.get(name) or registry.snapshot().get(name)
    if tool is not None:
        return tool
    tools = await mcp_tools()
    exact = next((t for t in tools if t.name == name), None)
    if exact is not None:
        return exact
    _, bare = mcp_servers.split_tool_name(name)
    matches = [t for t in tools if t.name == f'{MCP_PREFIX}{bare}'
               or t.name.endswith(f'__{bare}')
               or t.tool_key.endswith(f':{bare}')]
    return matches[0] if len(matches) == 1 else None


def planner_rules(catalog_tools) -> list[str]:
    """按候选工具导出**去重后**的能力包规则块（顺序稳定，便于测试与 diff）。"""
    blocks, seen = [], set()
    for tool in catalog_tools:
        rules = (tool.prompt_rules or '').strip()
        if not rules or rules in seen:
            continue
        seen.add(rules)
        blocks.append(rules)
    return blocks
