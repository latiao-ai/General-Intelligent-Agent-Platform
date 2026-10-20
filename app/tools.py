"""工具注册表：把能力包提供的工具汇总成模型可选的清单。

实现已经按业务拆到 app/capabilities/（web / export / publishing / answers），
这里只做三件事：

1. 汇总内置工具并补上稳定身份（tool_key）与动作性质（effect）；
2. 动态发现远端 MCP 工具，保留服务端给的完整能力描述与 annotations；
   **部署工具也在这里注册**，它只是"带平台适配器的 MCP 工具"，不是另一条通道；
3. 给规划器渲染工具清单。

文件末尾保留一批名字别名（_web_search、_render_sources_html 等），让旧调用点和
既有测试在迁移期继续可用——文档 §14 要求的"过渡期薄兼容层"。
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace

from app.integrations import mcp_adapter, mcp_servers
from app.capabilities.base import (ToolError, error_entry, tool_results_text,
                                   usable_documents, _select_sources, _with_text)
from app.capabilities.web import _fetch_pages, _web_search
from app.capabilities.answers import (_answer_directly, _answer_with_sources)
from app.capabilities.export import (_render_sources_html, _sources_page)
from app.capabilities import answers as answers_capability
from app.capabilities import export as export_capability
from app.capabilities import publishing as publishing_capability
from app.capabilities import web as web_capability
from app.config import settings

# 旧代码里 image: tool_module.search_backend / chat_reply / emit 的引用保留
from app.integrations import search as search_backend
from app.events import action, answer_delta, emit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    name: str
    label: str          # 中文阶段名，前端耗时表用它
    description: str    # 给规划器看：这个工具干什么
    args: dict          # 参数 JSON Schema，给规划器看
    run: object         # async def run(args, state) -> dict（state 增量）
    # 稳定身份：local:<能力包>:<动作> 或 mcp:<server_id>:<名称>。
    # 模型只看 name，但记录、审计、幂等键都应挂在 tool_key 上。
    tool_key: str = ''
    # 动作性质：read / write / destructive / unknown。
    # MCP 侧取自服务端 annotations，本地写死；没信息就是 unknown，不靠名字猜。
    effect: str = 'unknown'
    capability: str = ''
    # 规划规则：由**能力包自带**，规划器提示词按候选工具所属能力拼接。
    # 以前这些规则写死在 nodes.PLANNER_SYSTEM 里，新增一个工单/数据库能力
    # 必须改内核提示词——跟"业务提示词归能力包"是反的（文档 §4）。
    prompt_rules: str = ''
    # 远端工具来自哪个服务端（本地工具为空）。多服务端时靠它做路由与排障。
    server_id: str = ''


def describe_tool(tool: Tool) -> str:
    """工具在规划器眼里的样子：名称 + 动作性质 + 描述 + 参数。

    动作性质必须一起给：规划器据此判断"这类操作要不要先向用户确认"，
    只靠名字（比如看到 delete）去猜是不可靠的。
    """
    return (f'- {tool.name}［{tool.effect}］：{tool.description}\n'
            f'  参数：{json.dumps(tool.args, ensure_ascii=False)}')


BUILTIN_TOOLS = [
    Tool('web_search', '检索来源',
         '联网搜索，把命中的网页登记为来源（含摘要，部分自带正文）。需要外部/实时事实时先调它。',
         {'type': 'object', 'properties': {'query': {'type': 'string'}},
          'required': ['query']},
         _web_search),
    Tool('fetch_pages', '抓取正文',
         '读取**本轮用户输入里给出的 URL** 的正文（Markdown）。输入里没有 URL 就不要调用：'
         '搜索结果自带的正文已经够用，不需要再逐条抓一遍。',
         {'type': 'object',
          'properties': {'urls': {'type': 'array', 'minItems': 1,
                                  'items': {'type': 'string'}}},
          'required': ['urls']},
         _fetch_pages),

    Tool('render_sources_html', '资料转 HTML',
         '把已检索到的原始资料（网页 JSON 元信息 + Markdown 正文）直接排版成一份 HTML，'
         '不调用模型、不做摘要改写。用户说"把资料/搜索结果存成网页"时用它；'
         '要提炼总结请另外接入相应的 MCP 能力。',
         {'type': 'object', 'properties': {'title': {'type': 'string'}}},
         _render_sources_html),
    Tool('answer_with_sources', '联网作答',
         '基于已检索到的正文在聊天里回答问题，并附来源链接。用户想"问个明白"但没要'
         '文件时用它；必须在 web_search / fetch_pages 之后调用。',
         {'type': 'object', 'properties': {'question': {'type': 'string'}}},
         _answer_with_sources),
    Tool('answer_directly', '直接回答',
         '不检索、不出文件，用模型自己的知识回答。概念解释、原理、常识、闲聊、'
         '对上一轮的追问都用它——不要为了保险先跑一次检索。',
         {'type': 'object', 'properties': {'question': {'type': 'string'}}},
         _answer_directly),
]


# 内置工具的身份与动作性质：tool_key 形如 local:<能力包>:<动作>，
# capability 便于以后按业务拆文件，也便于界面分组。
#
# 注意：**部署不在这张表里**。部署工具来自 MCP 发现（见 mcp_tools），身份是
# mcp:<server_id>:<tool>，这里只给"本地纯计算/纯回答"的工具发本地身份。
_LOCAL_META = {
    'web_search': ('local:web:search', 'read', 'web'),
    'fetch_pages': ('local:web:fetch', 'read', 'web'),
    'render_sources_html': ('local:export:sources_html', 'write', 'export'),
    'answer_with_sources': ('local:answers:with_sources', 'read', 'answers'),
    'answer_directly': ('local:answers:direct', 'read', 'answers'),
}

# 每个能力包自带一段"什么时候用我"的规划规则。工具注册时按 capability 取，
# 规划器只拼接**本轮候选工具实际覆盖到的**能力——工具多起来时提示词不会膨胀。
CAPABILITY_RULES = {
    'web': web_capability.PROMPT_RULES,
    'answers': answers_capability.PROMPT_RULES,
    'export': export_capability.PROMPT_RULES,
    # publishing 的规则挂在 MCP 发现出来的部署工具上（它由能力包适配），
    # 所以这里不需要再登记一份。
}

# MCP 是"外来能力"，规则只能给通用纪律，不能替它编业务语义
MCP_PROMPT_RULES = (
    '【MCP 工具】调用查询类 MCP 工具时，只有不带过滤参数（如 keyword）的结果才是'
    '全量清单；删改类操作（delete_*/update_*）之后若要告诉用户"还剩什么/还剩几个"，'
    '必须再加一步**不带过滤参数**的查询（如 mcp__list_sites 且参数为空），'
    '绝不能拿操作前的过滤结果或操作返回体去推断。'
)

BUILTIN_TOOLS = [
    replace(tool, tool_key=_LOCAL_META[tool.name][0],
            effect=_LOCAL_META[tool.name][1], capability=_LOCAL_META[tool.name][2],
            prompt_rules=CAPABILITY_RULES.get(_LOCAL_META[tool.name][2], ''))
    for tool in BUILTIN_TOOLS
]

BUILTIN_BY_NAME = {tool.name: tool for tool in BUILTIN_TOOLS}


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
_MCP_CACHE: dict = {'at': 0.0, 'tools': [], 'errors': {}, 'signature': ()}
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
    total = len(servers)
    tools: list[Tool] = []
    errors: dict[str, str] = {}
    for server, (items, error) in zip(servers, discovered):
        if error:
            errors[server.id] = error
            continue
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
    _MCP_CACHE.update(at=now, tools=tools, errors=errors, signature=signature)
    return tools


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


def _make_mcp_runner(server_tool_name: str, server=None, adapter=None):
    """所有远端工具共用的一条调用通道。

    `adapter` 是可选的**平台侧适配器**（目前只有部署用）：它在调用前后各插一个
    钩子，但传输、参数注入、`mcp_calls` 记录仍然走这里——这样"某个远端工具
    需要平台额外解释回执"不会变成"某个远端工具有一条自己的调用通道"。
    """
    async def run(args: dict, state: dict) -> dict:
        artifact = state.get('artifact')
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
            raise ToolError(f'MCP 工具 {server_tool_name} 调用失败') from exc
        emit(action('MCP 工具返回', json.dumps(
            payload, ensure_ascii=False)[:120], '📦'))
        # 参数必须一起记：过滤类工具（如 list_sites 的 keyword）传不传参数，
        # 返回语义完全不同——不记下来就分不清"列表只有 1 个"和"只匹配到 1 个"。
        # 服务端也要记：多服务端时同名工具的行为可能完全不同。
        updates = {'mcp_calls': list(state.get('mcp_calls', [])) + [
            {'tool': server_tool_name, 'args': dict(args or {}),
             'server': getattr(server, 'id', '') or '',
             'payload': payload}]}
        interpret = getattr(adapter, 'interpret', None)
        if interpret is not None:
            updates.update(interpret(payload, dict(args or {}), state) or {})
        return updates
    return run


async def catalog() -> list[Tool]:
    """本轮可用的全部工具：内置 + MCP 发现。"""
    tools = list(BUILTIN_TOOLS)
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
    tool = BUILTIN_BY_NAME.get(name)
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
