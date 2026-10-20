"""工具注册表：把能力包提供的工具汇总成模型可选的清单。

实现已经按业务拆到 app/capabilities/（web / reports / publishing / answers），
这里只做三件事：

1. 汇总内置工具并补上稳定身份（tool_key）与动作性质（effect）；
2. 动态发现远端 MCP 工具，保留服务端给的完整能力描述与 annotations；
3. 给规划器渲染工具清单。

文件末尾保留一批名字别名（_web_search、_compose_report 等），让旧调用点和
既有测试在迁移期继续可用——文档 §14 要求的"过渡期薄兼容层"。
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace

from app import mcp_adapter, mcp_servers
from app.capabilities.base import (ToolError, error_entry, tool_results_text,
                                   usable_documents, _select_sources, _with_text)
from app.capabilities.web import _fetch_pages, _web_search
from app.capabilities.reports import (COMPOSE_SYSTEM, MAX_PAGE_SOURCE_CHARS,
                                      SOURCES_PAGE_CSS, _compose_report,
                                      _render_html, _render_sources_html,
                                      _sources_page)
from app.capabilities.publishing import _deploy_site
from app.capabilities.answers import (ANSWER_SYSTEM,
                                      ANSWER_USING_TOOL_RESULTS_SYSTEM,
                                      ANSWER_WITH_SOURCES_SYSTEM,
                                      _answer_directly, _answer_with_sources,
                                      _source_line)
from app.capabilities import web as web_capability
from app.capabilities import answers as answers_capability
from app.capabilities import reports as reports_capability
from app.capabilities import publishing as publishing_capability
from app.config import settings

# 旧代码里 image: tool_module.search_backend / chat_reply / emit 的引用保留
from app import search as search_backend
from app.events import action, answer_delta, draft, emit
from app.services import chat_reply

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
         '抓取指定链接的正文；不传 urls 时为所有"还没有正文"的来源补抓。给了 URL 时优先用它。',
         {'type': 'object',
          'properties': {'urls': {'type': 'array', 'items': {'type': 'string'}}}},
         _fetch_pages),
    Tool('compose_report', '整理报告',
         '把已取得正文的来源整合成结构化中文报告。必须在有正文之后调用。',
         {'type': 'object',
          'properties': {'topic': {'type': 'string'},
                         'required_sections': {'type': 'array',
                                               'items': {'type': 'string'}}}},
         _compose_report),
    Tool('render_html', '渲染 HTML',
         '把当前报告渲染成 HTML 产物并校验。必须在 compose_report 之后调用。',
         {'type': 'object', 'properties': {}}, _render_html),
    Tool('render_sources_html', '资料转 HTML',
         '把已检索到的原始资料（网页 JSON 元信息 + Markdown 正文）直接排版成一份 HTML，'
         '不调用模型、不做摘要改写。用户说"把资料/搜索结果存成网页"时用它；'
         '需要提炼总结成报告请用 compose_report。',
         {'type': 'object', 'properties': {'title': {'type': 'string'}}},
         _render_sources_html),
    Tool('deploy_site', '部署站点',
         '把当前 HTML 产物部署到服务器并返回访问地址。必须在 render_html 之后、'
         '且用户明确要求发布时调用。',
         {'type': 'object', 'properties': {}}, _deploy_site),
    Tool('answer_with_sources', '联网作答',
         '基于已检索到的正文在聊天里回答问题，并附来源链接。用户想"问个明白"但没要'
         '文档时用它；必须在 web_search / fetch_pages 之后调用。',
         {'type': 'object', 'properties': {'question': {'type': 'string'}}},
         _answer_with_sources),
    Tool('answer_directly', '直接回答',
         '不检索、不出报告地作答。寒暄、确认、一般性知识问答用它；'
         '也用于"先调工具取数据、再作答"计划里的收尾一步——它会看到前面工具的真实返回。'
         '若问题需要外部事实而你又没检索过，应该用 web_search 而不是它。',
         {'type': 'object', 'properties': {'question': {'type': 'string'}}},
         _answer_directly),
]

# 内置工具的身份与动作性质：tool_key 形如 local:<能力包>:<动作>，
# capability 便于以后按业务拆文件，也便于界面分组。
_LOCAL_META = {
    'web_search': ('local:web:search', 'read', 'web'),
    'fetch_pages': ('local:web:fetch', 'read', 'web'),
    'compose_report': ('local:reports:compose', 'read', 'reports'),
    'render_html': ('local:reports:render', 'write', 'reports'),
    'render_sources_html': ('local:reports:render_sources', 'write', 'reports'),
    'deploy_site': ('local:publishing:deploy', 'write', 'publishing'),
    'answer_with_sources': ('local:answers:with_sources', 'read', 'answers'),
    'answer_directly': ('local:answers:direct', 'read', 'answers'),
}

# 每个能力包自带一段"什么时候用我"的规划规则。工具注册时按 capability 取，
# 规划器只拼接**本轮候选工具实际覆盖到的**能力——工具多起来时提示词不会膨胀。
CAPABILITY_RULES = {
    'web': web_capability.PROMPT_RULES,
    'reports': reports_capability.PROMPT_RULES,
    'publishing': publishing_capability.PROMPT_RULES,
    'answers': answers_capability.PROMPT_RULES,
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
    'deploy_site': '与内置 deploy_site 等价。**优先用内置那个**——它负责把 HTML 组'
                   '装成服务端要的 base64 文件内容，直接调原始工具容易传错格式。',
    'delete_site': '⚠️ 不可逆。删除线上站点前必须先向用户复述将要删除的站点 ID 并确认。',
    'update_site': '⚠️ 会改动线上站点内容，执行前先说明改什么。',
    'list_sites': '不带 keyword 时返回全量站点；带了 keyword 只是筛选结果，不能当全量用。',
}
# 每次任务都去 discover 一次太慢（一次可能几十秒），进程内缓存一小段时间。
# 缓存要跟"服务端配置签名"绑定：改了 MCP_SERVERS 立刻重新发现，不能等 TTL。
_MCP_CACHE: dict = {'at': 0.0, 'tools': [], 'errors': {}, 'signature': ()}
_MCP_TTL = 300.0


def _signature() -> tuple:
    return tuple((s.id, s.transport, s.url or s.stdio_command)
                 for s in mcp_servers.servers())


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
      自动填充——模型只需要给出其余业务参数。
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
    _MCP_CACHE.update(at=now, tools=tools, errors=errors, signature=signature)
    return tools


def mcp_status() -> dict:
    """MCP 连接态势：给 /api/health 与排障用。"""
    return {'tools': len(_MCP_CACHE['tools']),
            'errors': dict(_MCP_CACHE.get('errors') or {}),
            'servers': mcp_servers.status()}


def _make_mcp_runner(server_tool_name: str, server=None):
    async def run(args: dict, state: dict) -> dict:
        artifact = state.get('artifact')
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
        return {'mcp_calls': list(state.get('mcp_calls', [])) + [
            {'tool': server_tool_name, 'args': dict(args or {}),
             'server': getattr(server, 'id', '') or '',
             'payload': payload}]}
    return run


async def catalog() -> list[Tool]:
    """本轮可用的全部工具：内置 + MCP 发现。"""
    tools = list(BUILTIN_TOOLS)
    if not settings.deploy_enabled:
        # 关掉部署时干脆不给这个工具，免得计划里出现注定失败的一步。
        tools = [t for t in tools if t.name != 'deploy_site']
    tools.extend(await mcp_tools())
    return tools


async def find_tool(name: str):
    """按名字取工具：内置 → MCP 精确名 → 去掉服务端前缀后再匹配。

    最后那步是为"续跑旧计划"服务的：工具名从 `mcp__delete_site` 变成了
    `mcp__ops__delete_site` 之后，旧计划里的名字不该直接失效。只有唯一命中
    才算数——两个服务端都有这个工具时不做猜测。
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
               or t.name.endswith(f'__{bare}')]
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
