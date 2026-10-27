"""工具清单：把**本地能力包注册的工具**与**运行时发现的 MCP 工具**汇总成一份。

这里只做三件事，**不认识任何具体业务**：

1. 从 `app.capabilities.registry` 取本地工具（能力包自己用 `@tool` 声明）；
2. 动态发现远端 MCP 工具，保留服务端给的完整能力描述与 annotations；
   部署工具也走这条（它是带平台适配器的 MCP 工具，不是另一条通道）；
3. 给规划器渲染工具清单。

两条来源必须分开：本地能力是进程内的静态声明（import 即注册），MCP 工具是运行时
发现（有 TTL、掉线、服务端前缀命名）。把它们混成一个注册表，"谁负责刷新"就说不清了。

加一个本地能力 = 在 `app/capabilities/` 下加一个文件，这个文件一个字都不用改。
远端工具若需要平台侧适配（调用前闸门、回执翻译），由该能力包的
`adapt_mcp_tool` 认领，内核不点名是哪一种业务。
"""
import asyncio
import json
import logging
import time

from app.integrations import mcp_adapter, mcp_servers
from app.capabilities import registry
from app import tool_policy as tool_policy_module
# 这几个是既有调用点仍在用的：`nodes` 用 usable_documents 判断"手上有没有料"，
# 测试用 _select_sources 校验预算截断。
from app.capabilities.base import (ToolError, usable_documents,  # noqa: F401
                                   _select_sources)
# Tool 定义搬到了注册器里（内核与能力包共用一份）；这里重新导出，
# 旧调用点与既有用例 `from app.tools import Tool` 继续可用。
from app.capabilities.registry import Tool
from app.config import settings

from app.events import action, emit

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

# MCP 是外来能力。这里只留任何远端工具都适用的纪律。
# 某一家的业务说法（删哪个对象、生成还是分析、完成条件指向哪个工具名）
# 写在该服务端配置的 prompt_rules 里，随那一家的工具进规划提示词。
MCP_PROMPT_RULES = (
    '【MCP 工具】查询如果带了过滤参数，返回的就只是筛选结果，不能当成全量。'
    '删改之后若要告诉用户还剩什么，必须再调一次不带过滤参数的查询，'
    '不能拿操作前的筛选结果或操作返回体去推断。'
    '【先发现，再调用】服务端要是有清单或描述类工具（名字或描述里带 list、describe、'
    'capabilities），先调它把真实能力读出来。通用调用类工具（如 run_tool）的内部工具名'
    '只能取自这份清单，清单里没有就是没有，不要凭经验编。'
    '工具描述里的清单可能只展示了一部分：被省略的工具仍然可用，要用就先按类别再查一次，'
    '不要拿名单开头那几个凑数。'
    '清单里标为不可用（缺依赖、缺 key、未安装）的工具不要调度。'
    '【封装工具先看依赖】一次做完的执行工具，内部往往还要调用清单里的其它工具。'
    '调度前先确认那些依赖可用；不可用就别调度，直接说清缺什么。'
    '调用时必须带上该内部工具的全部必填参数，缺了就不要调用。'
    '【失败要认】工具返回 status 为 error 或 failed，或 code 以 E_ 开头，这一步就是失败：'
    '不要当成功继续，也不要换个参数重试同一个不存在的名字。如实报告失败原因和缺的能力，'
    '并把已经产出的内容说清楚。'
    '【写操作必须收尾】计划里只要有写或删除，最后一步用 answer_directly，'
    '说清改了什么、对象的 id 是什么，让用户收到明确回执。'
    '【远端产物不是本地文件】MCP 工具改的是远端平台上的状态，本地通常没有对应文件。'
    '完成条件用 tool_succeeded，指向这次真正执行的调用。'
    '不要声明 artifact_exists：它只核对本地文件，远端已经做完也会被判成未完成。'
)


# ---------- MCP 动态工具 ----------

MCP_PREFIX = mcp_servers.MCP_PREFIX

# 每次任务都去 discover 一次太慢（一次可能几十秒），进程内缓存一小段时间。
# 缓存要跟"服务端配置签名"绑定：改了 MCP_SERVERS 立刻重新发现，不能等 TTL。
_MCP_CACHE: dict = {'at': 0.0, 'tools': [], 'errors': {}, 'signature': (),
                    'capabilities': {}}
_MCP_TTL = 300.0


def _signature() -> tuple:
    # 部署开关、各家规划规则和本机用法都进签名：改了配置，缓存里的工具说明必须立刻失效。
    return (settings.deploy_enabled,
            tuple((s.id, s.transport, s.url or s.stdio_command, s.prompt_rules,
                   tuple(sorted((s.tool_hints or {}).items())))
                  for s in mcp_servers.servers()))


async def _subject_token(server):
    """当前主体在这个服务端上的钥匙。None = 沿用服务端配置。"""
    from app import mcp_connections
    return await mcp_connections.bearer_for_current(server)


async def _discover(server) -> tuple[list[dict], str]:
    """发现单个服务端的工具。**失败只影响它自己**，不拖垮其他服务端。"""
    try:
        token = None
        if getattr(settings, 'tool_policy_enforced', False):
            token = await _subject_token(server)
        if token is None:
            return await mcp_adapter.list_server_tools(server), ''
        return await mcp_adapter.list_server_tools(server, token=token), ''
    except Exception as exc:                       # noqa: BLE001
        logger.warning('MCP 服务端 %s 工具发现失败：%s', server.id,
                       type(exc).__name__, exc_info=True)
        return [], f'{type(exc).__name__}: {str(exc)[:160]}'


def _type_brief(schema: dict, depth: int = 0) -> str:
    """把 JSON Schema 压成一眼能看懂的短类型：`string` / `[ {query:string} ]`。

    只给参数名是不够的：`queries` 是"字符串数组"还是"对象数组"，差别就是一次
    注定失败的调用（真实报错：`'xxx' is not of type 'object'`）。
    """
    if not isinstance(schema, dict) or depth > 2:
        return ''
    kind = str(schema.get('type') or '')
    if kind == 'array':
        item = _type_brief(schema.get('items') or {}, depth + 1)
        return f'[{item or "any"}]'
    if kind == 'object':
        required = [str(key) for key in (schema.get('required') or [])][:4]
        if required:
            return '{' + ','.join(required) + '}'
        properties = list((schema.get('properties') or {}))[:4]
        return '{' + ','.join(properties) + '}' if properties else 'object'
    return kind or 'any'


def _signature_brief(schema: dict, limit: int = 4) -> str:
    """内部工具的参数签名：**只列必填参数的类型**，可选的用 `+N` 概括。

    规划器要的是"我必须传什么、传成什么形状"，不是完整 schema。把 36 个工具的
    全部字段都铺开会把提示词撑到几千字，反而淹掉关键信息。
    """
    properties = (schema or {}).get('properties') or {}
    required = [str(key) for key in ((schema or {}).get('required') or [])]
    parts = []
    for name in required[:limit]:
        brief = _type_brief(properties.get(name) or {})
        parts.append(f'*{name}:{brief}')
    if not parts:
        # 没有必填参数也要有个提示，否则模型不知道该传什么
        return '无必填'
    optional = len(properties) - len(required)
    if optional > 0:
        parts.append(f'+{optional}可选')
    return ','.join(parts)


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
        token = None
        if getattr(settings, 'tool_policy_enforced', False):
            token = await _subject_token(server)
        extra = {} if token is None else {'token': token}
        payload = await mcp_adapter.call_tool(tool_name, {}, server=server, **extra)
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
            schema = item.get('input_schema') or {}
            properties = schema.get('properties') or {}
            out[str(item['name'])] = {
                'status': str(item.get('status') or '').upper(),
                'description': str(item.get('description') or '')[:80],
                'capability': str(item.get('capability') or ''),
                # 服务端通常会说清"缺什么/怎么装"，透出来才能告诉用户
                'install': str(item.get('install_instructions') or '')[:160],
                # **参数也必须带给规划器**：只知道名字、不知道要传什么，调用就会以
                # "input validation failed: 'x' is a required property" 收场
                'required': [str(key) for key in (schema.get('required') or [])][:6],
                'params': [str(key) for key in list(properties)[:8]],
                # 类型形状：数组元素是对象还是字符串，决定这次调用成不成立
                'signature': _signature_brief(schema)[:150],
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


def _capability_rank(info: dict) -> int:
    """截断后的清单里，分析类能力排在后面。

    目录顺序经常把 analysis 放在最前。名单只展示开头几项时，模型会拿分析工具
    去完成生成请求。这里不点名任何一家的能力名，只把分析类挪到后面。
    """
    name = str((info or {}).get('capability') or '').lower()
    if name == 'analysis' or name.startswith('analy'):
        return 100
    return 50


def _capability_line(capabilities: dict, limit: int = 26,
                     max_chars: int = 1800) -> str:
    """把内部能力清单压成一行，塞进工具描述里给规划器看。

    真实服务端可能有几十上百个内部工具，全列出来会把规划提示词撑爆，所以各自
    截断并标出总数。    截断不能说成「只能从这里选」——被省略的工具仍然可用，
    否则模型只会在名单开头那几项里凑一个来用。
    """
    if not capabilities:
        return ''
    usable = [name for name, info in capabilities.items()
              if _is_available(info.get('status'))]
    usable.sort(key=lambda name: (_capability_rank(capabilities[name]), name))
    unusable = [name for name in capabilities if name not in usable]
    def describe(name: str) -> str:
        info = capabilities[name]
        kind = str(info.get('capability') or '')
        prefix = f'{name}[{kind}]' if kind else name
        signature = info.get('signature') or ''
        if signature:
            return f'{prefix}({signature})'
        required = info.get('required') or []
        return (f'{prefix}(必填:{",".join(required)})' if required
                else f'{prefix}(无必填)')

    parts = []
    if usable:
        shown = '、'.join(describe(name) for name in usable[:limit])
        tail = (f' 等 {len(usable)} 个（被省略的仍然可用，先按类别再查清单）'
                if len(usable) > limit else '')
        parts.append('可用（括号里是必填参数）：' + shown + tail)
    else:
        parts.append('当前没有任何可用内部工具（缺依赖/缺 key/未安装）')
    if unusable:
        parts.append('不可用（别调度）：' + '、'.join(unusable[:limit])
                     + (f' 等 {len(unusable)} 个' if len(unusable) > limit else ''))
    line = '；'.join(parts)
    return line if len(line) <= max_chars else line[:max_chars] + '…'


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
            adapted = registry.adapt_mcp_tool(server, item, _make_mcp_runner)
            if adapted is not None:
                tools.append(adapted)
                continue
            name = mcp_servers.tool_name(server, item['name'], total)
            schema = item.get('inputSchema') or {'type': 'object', 'properties': {}}
            # 用服务端给的真实描述，而不是拿工具名猜用途
            title = item.get('title') or item['name']
            detail = (item.get('description') or '').strip()
            description = f'{title}（MCP 服务端 {server.id} 的工具 {item["name"]}）'
            if detail:
                description = f'{description}：{detail}'
            if inner and item['name'] == getattr(server, 'capability_tool', ''):
                # 清单工具本身没什么可编排的，真正的价值是把名字告诉规划器
                line = _capability_line(inner)
                if line:
                    description = f'{description}；本服务端能力清单 → {line}'
            hints = getattr(server, 'tool_hints', {}) or {}
            own_hint = hints.get(item['name'])
            if own_hint:
                description = f'{description}；本机用法：{own_hint}'
            elif inner:
                # 通用调用工具的 hint：把内部工具的"本机知识"一并带上
                inner_hints = [f'{name}：{text}' for name, text in hints.items()
                               if name in inner]
                if inner_hints:
                    description = f'{description}；本机用法：' + '；'.join(
                        inner_hints[:4])
            tools.append(Tool(
                name, f'MCP·{item["name"]}', description, schema,
                _make_mcp_runner(item['name'], server),
                tool_key=f'mcp:{server.id}:{item["name"]}',
                effect=item.get('effect_type') or 'unknown',
                capability='mcp',
                prompt_rules=(getattr(server, 'prompt_rules', '') or '').strip(),
                server_id=server.id,
            ))
    adapted_caps = {getattr(module, 'CAPABILITY', '')
                    for module in registry.capability_modules()
                    if getattr(module, 'adapt_mcp_tool', None)}
    if not any(tool.capability in adapted_caps for tool in tools):
        # 能力包声明要承接某个远端工具，发现时却没有它。留占位，调用时说明原因，
        # 而不是被当成未知工具静默跳过。
        stub = registry.mcp_placeholder(servers, errors)
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
               else '；且它们当前都不可用'),
            certainty='not_executed', phase='validate')
    # 参数也不许瞎写：服务端只会回一句 "input validation failed: 'x' is a
    # required property"，用户看不出该传什么。这里本地就挡住并列出参数名。
    inputs = (args or {}).get('inputs')
    required = info.get('required') or []
    if required and isinstance(inputs, (dict, type(None))):
        missing = [key for key in required if key not in (inputs or {})]
        if missing:
            raise ToolError(
                f'内部工具 {wanted} 缺少必填参数：{"、".join(missing)}。'
                f'它的参数有：{"、".join(info.get("params") or required)}'
                f'（相对路径会按项目目录解析）',
                certainty='not_executed', phase='validate')
    status = (info.get('status') or '').upper()
    if not _is_available(info.get('status')):
        usable = [name for name, item in capabilities.items()
                  if _is_available(item.get('status'))]
        raise ToolError(
            f'内部工具 {wanted} 当前不可用（{status}）。'
            + (f'可用的只有：{"、".join(usable[:12])}' if usable
               else '该服务端当前没有任何可用工具，请如实说明能力缺失'),
            certainty='not_executed', phase='validate')


def _deploy_tool(server, item: dict) -> Tool:
    """测试与旧调用点仍用这个名字。真正的包装在认领它的能力包里。"""
    tool = registry.adapt_mcp_tool(server, item, _make_mcp_runner)
    if tool is None:
        raise RuntimeError(f'{getattr(server, "id", "")} 的 {item.get("name")} 没有能力包认领')
    return tool


def _deploy_placeholder(errors: dict):
    """发现失败时的占位工具。开关关掉的能力包不会留占位。"""
    return registry.mcp_placeholder(mcp_servers.servers(), errors or {},
                                    respect_switch=False)


def mcp_status() -> dict:
    """MCP 连接态势：给 /api/health 与排障用。"""
    return {'tools': len(_MCP_CACHE['tools']),
            'errors': dict(_MCP_CACHE.get('errors') or {}),
            'servers': mcp_servers.status()}


# 这些键属于图状态本身，回执里的同名字段不能覆盖它们
_STATE_KEYS = frozenset({'status', 'answer', 'artifact', 'documents', 'plan',
                         'errors', 'usage', 'deployment', 'mcp_calls', 'outcome',
                         'failure', 'step_outputs', 'step_index', 'documents'})


# 这些字样说明失败原因是"服务端缺依赖/缺资源"，而不是我们的参数写错。
# 服务端自检常常报 AVAILABLE（只查命令在不在），真跑起来才炸——所以只能从
# 失败文本里认出来，并明确告诉用户"这是环境问题，不是调度问题"。
_DEPENDENCY_SIGNS = (
    ('unable to find voice', 'Piper 缺语音模型：先 python -m piper.download_voices '
                             '<voice> --data-dir <目录>，再把 model 传成绝对路径'),
    ('piper failed', 'Piper 语音合成失败：确认语音模型文件存在且完整'),
    ('ffmpeg', 'FFmpeg 没装或不在 PATH：brew install ffmpeg'),
    ('no such file', '服务端缺文件（模型/素材/中间产物）：按它提示的路径补齐后再跑'),
    ('not installed', '服务端缺依赖：按 install_instructions 安装后重试'),
)


def _dependency_note(text: str) -> str:
    lowered = (text or '').lower()
    # 参数校验失败是**调用没写对**，不是环境问题：服务端 schema 的描述里常出现
    # "ffmpeg" 之类字样，不加这道判断就会误报"装 ffmpeg"。
    if 'validation failed' in lowered or 'required property' in lowered:
        return ''
    for needle, note in _DEPENDENCY_SIGNS:
        if needle in lowered:
            return note
    return ''


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


async def _stage_result(payload: dict, *, tool_key: str = '') -> str:
    """把 JSON 回执落到该企业自己的结果目录，并登记到当前用户名下。

    读接口只认登记行。没有运行归属、或登记失败时不返回 result_id：
    调用回执仍然保留，但这份落盘不能被别人凭编号读走。
    """
    try:
        from app import execution_control
        from app import ledger as ledger_module
        from app import tool_results
        book = ledger_module.current_ledger()
        context = execution_control.require()
        store = getattr(book, 'store', None) if book is not None else None
        run = None
        if store is not None and context is not None and hasattr(store, 'run'):
            run = await store.run(context.run_id)
        if not run or not hasattr(store, 'commit_tool_result'):
            logger.warning('结果信封未登记：缺少运行归属或登记接口')
            return ''
        tenant_id = str(run.get('tenant_id') or 'default')
        owner_user_id = str(run.get('owner_user_id') or 'default')
        captured = tool_results.CapturedResult(
            structured=payload,
            parts=[tool_results.ResultPart(
                ordinal=0, kind='json', content_type='application/json',
                text=json.dumps(payload, ensure_ascii=False, default=str))])
        staged = tool_results.stage_result(
            settings.artifact_dir, captured, tenant_id=tenant_id)
        envelope = tool_results.envelope_v2(
            result_id=str(staged.get('result_id') or ''), tool_key=tool_key,
            structured_ref=str(staged.get('raw_ref') or ''),
            complete=bool((staged.get('completeness') or {}).get('complete', True)),
            completeness=staged.get('completeness') or {})
        await store.commit_tool_result({
            'tenant_id': tenant_id,
            'owner_user_id': owner_user_id,
            'run_id': str(context.run_id or ''),
        }, staged, envelope)
        return str(staged.get('result_id') or '')
    except Exception:                              # noqa: BLE001
        logger.warning('结果信封落盘失败', exc_info=True)
        return ''


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
            token = await _subject_token(server)
            # 不带 server 时保持旧调用形式：既兼容既有调用点，也让工具名自己
            # 带前缀时能走路由（mcp_servers.split_tool_name）。
            # token 为 None 时不把这个参数传进去：沿用服务端自己的钥匙。
            extra = {} if token is None else {'token': token}
            if server is None:
                payload = await mcp_adapter.call_tool(
                    server_tool_name, dict(args or {}), artifact=artifact,
                    **extra)
            else:
                payload = await mcp_adapter.call_tool(
                    server_tool_name, dict(args or {}), artifact=artifact,
                    server=server, **extra)
        except Exception as exc:
            leaf = _unwrap_error(exc)
            # 确定性必须在**这一层**标注：再往外包装就只剩 ToolError，类型名丢掉，
            # 按异常类名猜必然猜错（文档 §6.1）。
            certainty = mcp_adapter.certainty_of_error(leaf)
            phase = 'connect' if certainty == 'not_executed' else 'receive'
            if isinstance(leaf, mcp_adapter.MCPToolFailure):
                # 业务层失败（status: error / E_*）：必须当成失败停下，而且要把
                # 服务端原话透出来，否则用户只看到"没有产出可用结果"。
                # 但只取前两行：服务端常把整段 traceback 塞进 message，直接展示
                # 既看不懂又泄漏内部路径。
                detail = ' '.join(str(leaf).split())
                note = _dependency_note(detail)
                raise ToolError(
                    f'MCP 工具 {server_tool_name} 失败：{detail[:240]}'
                    + (f'｜{note}' if note else ''),
                    certainty=certainty, phase=phase,
                    error_code=getattr(leaf, 'error_code', '') or 'E_MCP_TOOL_FAILURE',
                ) from exc
            raise ToolError(
                f'MCP 工具 {server_tool_name} 调用失败'
                f'（{_describe_error(exc)}）',
                certainty=certainty, phase=phase) from exc
        emit(action('MCP 工具返回', mcp_adapter.summarize(payload), '📦'))
        from app import async_contracts
        server_id = getattr(server, 'id', '') if server is not None else ''
        tool_key = (f'mcp:{server_id}:{server_tool_name}' if server_id
                    else server_tool_name)
        acceptance = async_contracts.acceptance(
            tool_key, payload if isinstance(payload, dict) else None)
        result_id = ''
        if getattr(settings, 'result_envelope_v2', False) and isinstance(payload, dict):
            result_id = await _stage_result(payload, tool_key=tool_key)
        # 参数必须一起记：过滤类工具（如 list_sites 的 keyword）传不传参数，
        # 返回语义完全不同——不记下来就分不清"列表只有 1 个"和"只匹配到 1 个"。
        # 服务端也要记：多服务端时同名工具的行为可能完全不同。
        updates = {'mcp_calls': list(state.get('mcp_calls', [])) + [
            {'tool': server_tool_name, 'args': dict(args or {}),
             'server': getattr(server, 'id', '') or '',
             'payload': payload}]}
        updates.update(acceptance)
        if result_id:
            updates['result_id'] = result_id
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


async def catalog(context=None) -> list[Tool]:
    """本轮可用的全部工具：本地能力包（注册来的）+ MCP 发现。

    `context` 是**可信执行主体**（`app/tool_context.ToolCallContext`）。给了它，
    就按主体过滤：模型**看不见**未授权的工具——不是"调用时被拒"，是连名字
    都拿不到（文档 §7 P2：上游偷偷加一个 delete_*，规划器看到了就会用）。

    没有 `context` 时行为与旧版一致（本地能力包 + 全部 MCP 工具），
    以便单元测试与 CLI 直调继续可用。
    """
    tools = list(registry.registered())
    tools.extend(await mcp_tools())
    hidden = registry.suppressed_capabilities()
    if hidden:
        # 关掉的能力连占位都不留，免得计划里出现注定失败的一步。
        tools = [tool for tool in tools if tool.capability not in hidden]
    if context is None:
        return tools
    return await visible_tools(tools, context)


async def visible_tools(tools, context) -> list[Tool]:
    """按主体过滤工具清单。**被隐藏的工具连名字都不给模型**。"""
    if context is None:
        return list(tools)
    from app import tool_authorization as authorization_module
    store = _current_store()
    policies = await authorization_module.load_policies(store, context.tenant_id)
    visible, hidden = tool_policy_module.filter_catalog(
        tools, policies, tenant_id=context.tenant_id,
        role=context.role, user_id=context.user_id)
    if hidden:
        logger.info('按主体隐藏了 %s 个未授权工具（tenant=%s）',
                    len(hidden), context.tenant_id)
    return visible


def _current_store():
    """当前执行链上的仓储（账本持有）。没有账本时返回 None。"""
    try:
        from app import ledger as ledger_module
        book = ledger_module.current_ledger()
        return getattr(book, 'store', None) if book is not None else None
    except Exception:                                # noqa: BLE001
        return None


async def find_tool(name: str, context=None):
    """按名字取工具：内置 → MCP 精确名 → 去掉服务端前缀后按**身份**再匹配。

    最后那步是为"续跑旧计划"服务的：工具名从 `mcp__delete_site` 变成了
    `mcp__ops__delete_site`、或者部署工具从 `mcp__deploy_site` 变成了平台的
    `deploy_site`，旧计划里的名字都不该直接失效。匹配不了名字就退到 tool_key
    的尾部——身份才是稳定的，名字只是对模型的展示形式。只有唯一命中才算数，
    两个服务端都有这个工具时不做猜测。

    `context` 给了就按主体过滤：**无权使用的工具在执行点也取不到**，
    这样"计划里写了但不该跑的"会走"工具不可用"分支，而不是绕过授权发出去。
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
    found = matches[0] if len(matches) == 1 else None
    if found is None or context is None:
        return found
    visible = await visible_tools([found], context)
    return visible[0] if visible else None


def planner_rules(catalog_tools) -> list[str]:
    """按候选工具导出**去重后**的规则块（顺序稳定，便于测试与 diff）。

    远端工具在场时，最后补上一段通用 MCP 纪律。这段不点名任何一家的工具。
    某一家自己的说法写在该服务端的 prompt_rules 上，只跟那一家的工具走。
    """
    blocks, seen = [], set()
    remote = False
    for tool in catalog_tools:
        if getattr(tool, 'server_id', ''):
            remote = True
        rules = (tool.prompt_rules or '').strip()
        if not rules or rules in seen:
            continue
        seen.add(rules)
        blocks.append(rules)
    generic = MCP_PROMPT_RULES.strip()
    if remote and generic and generic not in seen:
        blocks.append(generic)
    return blocks
