"""MCP Server 注册表与工具路由。

以前只接一个服务端：连接参数从 `MCP_URL` / `MCP_BEARER_TOKEN` 这些环境变量
直接读，工具身份写死成 `mcp:<MCP_SERVER_ID>:<name>`。接第二个服务端时，
同名工具会互相覆盖——`delete_site` 到底删哪个站，只能靠猜。

这里把"接哪些服务端"变成配置：

```json
MCP_SERVERS=[
  {"id": "publishing", "url": "https://a/mcp", "deploy_tool": "deploy_site", "default": true},
  {"id": "ops", "url": "https://b/mcp", "token": "..."}
]
```

规则：

- **工具名**：只有一个服务端时保持 `mcp__<name>`（向后兼容，已存的计划还能跑）；
  有多个时加服务端前缀 `mcp__<server_id>__<name>`，避免跨服务端重名。
- **路由**：工具名 → 服务端，由名字里的前缀决定；调用时显式带上服务端，
  不再"全局唯一一个连接"。
- **部署**：`default: true` 的那个服务端承接 `deploy_site` 这类适配器工具；
  没标就取第一个。
- **旧配置**：没设置 `MCP_SERVERS` 时，按原来的 `MCP_*` 变量合成一个服务端，
  行为与之前完全一致。
"""
import json
import logging
import os
import re
from pathlib import Path
from dataclasses import dataclass, field

# 本模块只读 `os.environ`，而 `.env` 的加载发生在 `webpage_agent.core.config` 导入时。
# 不在这里显式导入，任何先 import 本模块的脚本（运维脚本、独立探针）都会
# 读到空环境 → 解析出 0 个服务端，而且 `LAST_ERROR` 仍为空串，属于最难查的
# 那类"静默为空"。生产不受影响（APP_ENV=production 本来就不读 .env），
# 但开发/联调用例会被这一行差异坑到。见 tests/test_mcp_env_loading.py。
from webpage_agent.core.config import settings  # noqa: F401  # 导入即触发 .env 加载

logger = logging.getLogger(__name__)

MCP_PREFIX = 'mcp__'
DEFAULT_TRANSPORT = 'streamable_http'
VALID_TRANSPORTS = {'streamable_http', 'sse', 'stdio'}
# 连接器授权方式：空 = 静态 token；oauth = 按登录主体走授权码流程。
VALID_AUTH_MODES = {'', 'oauth'}

# 配置解析失败时的最后一次错误，供 /api/health 与排障看
LAST_ERROR: str = ''


@dataclass(frozen=True)
class Server:
    """一个 MCP 服务端的连接与适配参数。"""
    id: str
    transport: str = DEFAULT_TRANSPORT
    url: str = ''
    token: str = ''
    stdio_command: str = ''
    stdio_args: tuple = ()
    # 以下只在"这个服务端承接部署"时有意义
    deploy_tool: str = ''
    payload_mode: str = ''
    html_field: str = ''
    entry_filename: str = ''
    extra_args: dict = field(default_factory=dict)
    result_url_field: str = ''
    default: bool = False
    title: str = ''
    # 有些服务端把真正的动作藏在**一个**通用工具后面（例如 run_tool，名字放参数里）。
    # 这时必须先从清单工具读出"有哪些名字"再规划，否则模型只能编：
    #   capability_tool：清单工具名（如 list_capabilities）
    #   tool_name_arg  ：调用时承载内部工具名的参数（如 name / tool_name）
    capability_tool: str = ''
    tool_name_arg: str = ''
    # 某个工具在本机的特殊用法（例如本地 TTS 要传模型绝对路径）。
    # 键可以是 MCP 工具名，也可以是 run_tool 的内部工具名。
    # 这些"本机知识"放在配置里，而不是让模型去猜。
    tool_hints: dict = field(default_factory=dict)
    # 这家服务端自己的规划规则。只在它的工具进入本轮清单时拼进规划提示词。
    # 内核不再替某一家编写业务语义（删站点、生成视频、画布节点这类话写在这里）。
    prompt_rules: str = ''
    stdio_env: dict = field(default_factory=dict)
    # 用户自定义远程 MCP 的静态请求头（例如私有网关注入的鉴权头）。
    http_headers: dict = field(default_factory=dict)
    # `forward_subject` 在 stdio 上用环境变量传主体（子进程是我们自己起的，可信）。
    # HTTP/SSE 是跨机器的通道，明文主体头既不能被对端当依据、又会被"拿到 token 的人"伪造，
    # 所以改为签发短期签名断言：密钥引用走 env:/file:/literal:，密钥本身不进配置。
    # 见 webpage_agent/integrations/mcp_subject.py。
    forward_subject: bool = False
    subject_secret_ref: str = ''
    subject_header: str = ''            # 空 = mcp_subject.DEFAULT_HEADER
    subject_ttl_seconds: int = 60
    # 对端自己的"请求被拒、没落地"原因词（如 stale_revision）。只加词，不改变
    # 默认判定：不在这里、也不像 invalid_/stale_ 的原因仍按"结果未定"处理。
    deterministic_reasons: tuple = ()
    planning_tools: tuple = ()
    idempotency_arg: str = ''
    # 需用户在外部平台确认后才能执行的工具（付费下单、下发/提交类动作）。
    # 以及触发那次确认的**提议**工具（只登记提议、不执行）。
    # 内核不认识任何业务名字：哪些动作属于这两类，由该服务端自己在配置里声明；
    # 规划校验只据此判"提议和提交是否被塞进了同一片段"。
    confirmation_tools: tuple = ()
    proposal_tools: tuple = ()
    # ---------- 连接器授权 ----------
    # 空串：用本条目 / 环境里的静态 token（历史行为，不感知用户）。
    # `oauth`：按登录主体走授权码流程，token 由 `core/oauth_store.py` 解析。
    # 只有 HTTP/SSE 能走 OAuth —— stdio 没有 Authorization 头，凭据只能来自
    # 进程环境变量（见 `forward_subject` 那段注释）。
    auth: str = ''
    oauth_issuer: str = ''
    oauth_scopes: tuple = ()
    oauth_client_name: str = ''
    oauth_resource: str = ''
    oauth_redirect_path: str = ''
    # 预注册模式用：有些企业级 server 不给动态注册端点，要求先在对方后台报备。
    # 这两个值属于"应用级"配置（全中台一份），不是用户凭据。
    oauth_client_id: str = ''
    oauth_client_secret_ref: str = ''


def _legacy_server() -> Server | None:
    """从旧的单项环境变量合成一个服务端（保持向后兼容）。"""
    url = os.getenv('MCP_URL', '').strip()
    command = os.getenv('MCP_STDIO_COMMAND', '').strip()
    if not url and not command:
        return None
    return Server(
        id=os.getenv('MCP_SERVER_ID', 'publishing').strip() or 'publishing',
        transport=os.getenv('MCP_TRANSPORT', DEFAULT_TRANSPORT).strip(),
        url=url,
        token=os.getenv('MCP_BEARER_TOKEN', '').strip(),
        stdio_command=command,
        stdio_args=tuple(json.loads(os.getenv('MCP_STDIO_ARGS', '[]') or '[]')),
        deploy_tool=os.getenv('MCP_TOOL_NAME', '').strip(),
        payload_mode=os.getenv('MCP_PAYLOAD_MODE', '').strip(),
        html_field=os.getenv('MCP_HTML_FIELD', '').strip(),
        entry_filename=os.getenv('MCP_ENTRY_FILENAME', '').strip(),
        extra_args=_json_object(os.getenv('MCP_EXTRA_ARGS', '') or '{}') or {},
        result_url_field=os.getenv('MCP_RESULT_URL_FIELD', '').strip(),
        capability_tool=os.getenv('MCP_CAPABILITY_TOOL', '').strip(),
        tool_name_arg=os.getenv('MCP_TOOL_NAME_ARG', '').strip(),
        default=True,
        title='默认服务端（MCP_* 单项配置）',
    )


def _prompt_rules(raw) -> str:
    """服务端自带的规划规则。只接受一段文字，别的形态当作没配。"""
    if not isinstance(raw, str):
        return ''
    return raw.strip()


def _positive_int(raw, default: int) -> int:
    """把配置里的正整数取出来；非法/缺失就用默认值。

    断言有效期是"安全参数"，写成字符串 `'60'`、负数或 `0` 都不该让它变成
    永不过期或立即过期——那种失败发生在对端，最难查。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _json_object(raw: str) -> dict | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _from_entry(entry: dict) -> Server | None:
    if not isinstance(entry, dict):
        logger.warning('MCP_SERVERS 里有非对象条目，已跳过：%r', entry)
        return None
    server_id = str(entry.get('id') or '').strip()
    if not server_id:
        logger.warning('MCP_SERVERS 有条目缺少 id，已跳过')
        return None
    if '__' in server_id:
        # 工具名用 __ 分隔服务端与工具名，id 里再出现会切错
        logger.warning('MCP 服务端 id %r 不能包含 "__"，已跳过', server_id)
        return None
    transport = str(entry.get('transport') or DEFAULT_TRANSPORT).strip()
    if transport not in VALID_TRANSPORTS:
        logger.warning('MCP 服务端 %s 的 transport 无效（%s），已跳过',
                       server_id, transport)
        return None
    url = str(entry.get('url') or '').strip()
    command = str(entry.get('stdio_command') or '').strip()
    if transport != 'stdio' and not url:
        logger.warning('MCP 服务端 %s 缺少 url，已跳过', server_id)
        return None
    if transport == 'stdio' and not command:
        logger.warning('MCP 服务端 %s 缺少 stdio_command，已跳过', server_id)
        return None
    oauth_raw = entry.get('oauth')
    oauth = oauth_raw if isinstance(oauth_raw, dict) else {}
    auth_mode = str(entry.get('auth') or '').strip().lower()
    if auth_mode and auth_mode not in VALID_AUTH_MODES:
        logger.warning('MCP 服务端 %s 的 auth 无效（%s），只能是 %s，已跳过',
                       server_id, auth_mode, ' / '.join(sorted(VALID_AUTH_MODES)))
        return None
    oauth_issuer = str(oauth.get('issuer') or '').strip()
    if auth_mode == 'oauth':
        # 这两种配错都是"看起来能用、实际上永远连不上"，必须在加载时就拒掉：
        # stdio 没有请求头，OAuth 的 Bearer 无处可挂；没有 issuer 则无法发现
        # 授权端点，只能靠猜地址。
        if transport == 'stdio':
            logger.warning('MCP 服务端 %s 配了 oauth 但 transport 是 stdio：'
                           'stdio 没有 Authorization 头，已跳过', server_id)
            return None
        if not oauth_issuer:
            logger.warning('MCP 服务端 %s 配了 oauth 但 oauth.issuer 为空，已跳过',
                           server_id)
            return None
    return Server(
        id=server_id,
        transport=transport,
        url=url,
        token=str(entry.get('token') or '').strip(),
        stdio_command=command,
        stdio_args=tuple(entry.get('stdio_args') or ()),
        deploy_tool=str(entry.get('deploy_tool') or '').strip(),
        payload_mode=str(entry.get('payload_mode') or '').strip(),
        html_field=str(entry.get('html_field') or '').strip(),
        entry_filename=str(entry.get('entry_filename') or '').strip(),
        extra_args=dict(entry.get('extra_args') or {}),
        result_url_field=str(entry.get('result_url_field') or '').strip(),
        capability_tool=str(entry.get('capability_tool') or '').strip(),
        tool_name_arg=str(entry.get('tool_name_arg') or '').strip(),
        tool_hints=dict(entry.get('tool_hints') or {}),
        prompt_rules=_prompt_rules(entry.get('prompt_rules')),
        stdio_env={str(k): str(v) for k, v in (entry.get('stdio_env') or {}).items()},
        http_headers={str(k): str(v) for k, v in (entry.get('headers') or {}).items()},
        forward_subject=bool(entry.get('forward_subject', False)),
        subject_secret_ref=str(entry.get('subject_secret_ref') or '').strip(),
        subject_header=str(entry.get('subject_header') or '').strip(),
        subject_ttl_seconds=_positive_int(entry.get('subject_ttl_seconds'), 60),
        deterministic_reasons=tuple(str(x) for x in (entry.get('deterministic_reasons') or ())),
        planning_tools=tuple(str(x) for x in (entry.get('planning_tools') or [])),
        idempotency_arg=str(entry.get('idempotency_arg') or '').strip(),
        confirmation_tools=tuple(str(x) for x in (entry.get('confirmation_tools') or [])),
        proposal_tools=tuple(str(x) for x in (entry.get('proposal_tools') or [])),
        auth=auth_mode,
        oauth_issuer=oauth_issuer,
        oauth_scopes=tuple(str(x) for x in (oauth.get('scopes') or ()) if str(x).strip()),
        oauth_client_name=str(oauth.get('client_name') or '').strip() or '智能体中台',
        oauth_resource=str(oauth.get('resource') or '').strip(),
        oauth_redirect_path=str(oauth.get('redirect_path') or '').strip(),
        oauth_client_id=str(oauth.get('client_id') or '').strip(),
        oauth_client_secret_ref=str(oauth.get('client_secret_ref') or '').strip(),
        default=bool(entry.get('default')),
        title=str(entry.get('title') or ''),
    )



# ---------------------------------------------------------------- 用户自定义远程 MCP
#
# 与 WorkBuddy 的 ~/.workbuddy/mcp.json 保持同一种文件形态：
#   {"mcpServers": {"name": {"type": "streamable-http", "url": "...", "headers": {}}}}
# 中台只接受 HTTP/SSE；stdio 等本地进程由运维通过 MCP_SERVERS 配置，避免
# 浏览器里的一条 JSON 变成任意命令执行。
_USER_CONFIG_ENV = 'MCP_USER_CONFIG_FILE'
_REPO_ROOT = Path(__file__).resolve().parents[3]


def user_config_path() -> Path:
    raw = os.getenv(_USER_CONFIG_ENV, '').strip()
    if raw:
        return Path(raw).expanduser()
    return _REPO_ROOT / 'profile' / 'mcp.json'


def _user_server_entries() -> list[Server]:
    path = user_config_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        global LAST_ERROR
        LAST_ERROR = f'用户 MCP 配置读取失败：{type(exc).__name__}'
        logger.warning(LAST_ERROR)
        return []
    raw = data.get('mcpServers') if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return []
    out: list[Server] = []
    for name, entry in raw.items():
        if not isinstance(entry, dict) or entry.get('disabled') is True:
            continue
        if str(entry.get('command') or '').strip() or str(entry.get('type') or '').lower() == 'stdio':
            logger.warning('用户 MCP %s 使用 stdio，中台只接受 HTTP/SSE，已跳过', name)
            continue
        kind = str(entry.get('type') or 'streamable-http').strip().lower()
        transport = 'sse' if kind == 'sse' else 'streamable_http'
        url = str(entry.get('url') or '').strip()
        if not url:
            logger.warning('用户 MCP %s 缺少 url，已跳过', name)
            continue
        headers = entry.get('headers')
        headers = headers if isinstance(headers, dict) else {}
        server = _from_entry({
            'id': str(name), 'transport': transport, 'url': url,
            'headers': {str(k): str(v) for k, v in headers.items()},
            'title': str(entry.get('title') or name),
        })
        if server:
            out.append(server)
    return out



def servers() -> list[Server]:
    """当前配置的服务端列表。

    每次读环境变量、不做缓存：解析成本是一次小 JSON，换来的是"改配置立刻生效"，
    测试里也不用清缓存。
    """
    global LAST_ERROR
    raw = os.getenv('MCP_SERVERS', '').strip()
    filename = os.getenv('MCP_SERVERS_FILE', '').strip()
    if filename:
        try:
            raw = Path(filename).read_text(encoding='utf-8')
            def expand(match):
                value = os.getenv(match[1])
                if value is None or not value.strip():
                    raise ValueError('Missing environment variable: ' + match[1])
                # Expand inside JSON strings, with JSON escaping.
                return json.dumps(value)[1:-1]
            raw = re.sub(r'\$\{([A-Z][A-Z0-9_]*)\}', expand, raw)
        except (OSError, ValueError):
            LAST_ERROR = 'MCP_SERVERS_FILE cannot be read or has unresolved variables'
            return []
    if not raw:
        LAST_ERROR = ''
        legacy = _legacy_server()
        return [legacy] if legacy else []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError) as exc:
        LAST_ERROR = f'MCP_SERVERS 不是合法 JSON：{type(exc).__name__}'
        logger.error('%s，退回 MCP_* 单项配置', LAST_ERROR)
        if filename or os.getenv('APP_ENV', '').strip().lower() == 'production':
            return []
        legacy = _legacy_server()
        return [legacy] if legacy else []
    if not isinstance(items, list):
        LAST_ERROR = 'MCP_SERVERS 必须是数组'
        if filename or os.getenv('APP_ENV', '').strip().lower() == 'production':
            return []
        logger.error('%s，退回 MCP_* 单项配置', LAST_ERROR)
        legacy = _legacy_server()
        return [legacy] if legacy else []
    LAST_ERROR = ''
    out = [server for server in (_from_entry(item) for item in items) if server]
    if len(out) != len(items):
        LAST_ERROR = 'MCP_SERVERS contains invalid entries'
        if filename or os.getenv('APP_ENV', '').strip().lower() == 'production':
            return []
    # id 重复会让工具前缀产生歧义，后者覆盖前者没有意义，直接保留第一个
    seen, unique = set(), []
    for server in out:
        if server.id in seen:
            LAST_ERROR = 'MCP server IDs must be unique'
            if filename or os.getenv('APP_ENV', '').strip().lower() == 'production':
                return []
            logger.warning('MCP 服务端 id %s 重复，已忽略后一个', server.id)
            continue
        seen.add(server.id)
        unique.append(server)
    for server in _user_server_entries():
        # 系统配置优先；用户不能覆盖运维配置，避免同名静默替换鉴权。
        if server.id in seen:
            logger.warning('用户 MCP %s 与系统配置重名，已忽略用户项', server.id)
            continue
        seen.add(server.id)
        unique.append(server)
    return unique


def by_id(server_id: str) -> Server | None:
    return next((s for s in servers() if s.id == server_id), None)


def deploy_server() -> Server | None:
    """承接部署类适配器工具的服务端：显式标 default 的优先，否则取第一个。"""
    items = servers()
    for server in items:
        if server.default:
            return server
    return items[0] if items else None


def tool_name(server: Server, name: str, total: int | None = None) -> str:
    """生成对模型暴露的工具名。

    单服务端不加前缀，是为了不破坏已保存的计划与既有提示词；多服务端必须加，
    否则 `mcp__delete_site` 属于谁无法判断。
    """
    count = len(servers()) if total is None else total
    if count <= 1:
        return f'{MCP_PREFIX}{name}'
    return f'{MCP_PREFIX}{server.id}__{name}'


def split_tool_name(name: str) -> tuple[Server | None, str]:
    """把工具名拆回 (服务端, 原始工具名)；没带前缀时服务端为 None。"""
    body = name[len(MCP_PREFIX):] if name.startswith(MCP_PREFIX) else name
    if '__' in body:
        head, rest = body.split('__', 1)
        server = by_id(head)
        if server is not None:
            return server, rest
    return None, body


def status() -> dict:
    items = servers()
    return {'servers': [{'id': s.id, 'transport': s.transport,
                         'default': s.default or (i == 0 and len(items) == 1)}
                        for i, s in enumerate(items)],
            'count': len(items), 'error': LAST_ERROR}
