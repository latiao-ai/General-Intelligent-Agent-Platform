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

# 条目归属。这决定"谁能看见它"以及"工具名要不要带前缀"。
#   ops      —— `.env` 的 MCP_SERVERS，运维配置，全站可见
#   platform —— 控制台发布的平台连接器，全站可见
#   shared   —— 旧的全局单文件（MCP_USER_CONFIG_FILE），全站可见（兼容期）
#   user     —— 某个登录主体自己的连接器，只有他自己看得见
SCOPE_OPS = 'ops'
SCOPE_PLATFORM = 'platform'
SCOPE_SHARED = 'shared'
SCOPE_USER = 'user'

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
    # ---------- 归属 ----------
    # `scope` 见文件头的 SCOPE_* 常量；`owner_key` 只在 scope=user 时有值，
    # 形如 `tenant__user`。两者一起决定"这条配置属于谁"：只有 user 条目按
    # 登录主体隔离，其余都是全站共享。
    scope: str = SCOPE_OPS
    owner_key: str = ''


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


def _from_entry(entry: dict, *, scope: str = SCOPE_OPS,
                owner_key: str = '') -> Server | None:
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
        scope=scope,
        owner_key=owner_key,
    )



# ---------------------------------------------------------------- 用户自定义远程 MCP
#
# 与 WorkBuddy 的 ~/.workbuddy/mcp.json 保持同一种文件形态：
#   {"mcpServers": {"name": {"type": "streamable-http", "url": "...", "headers": {}}}}
# 中台只接受 HTTP/SSE；stdio 等本地进程由运维通过 MCP_SERVERS 配置，避免
# 浏览器里的一条 JSON 变成任意命令执行。
#
# **一个主体一个文件**：`<MCP_USER_CONFIG_DIR>/<tenant>__<user>.json`。
# 历史包袱是这里原来只有一个固定路径（`profile/mcp.json`），于是"用户自定义"
# 实际是"部署级自定义"——A 加一条，B 的对话里也会出现，B 还能用 A 的鉴权头
# 去调 A 的私有网关。改成按主体分文件之后，`servers(owner_key=...)` 才是
# 某个用户真正看得见的集合。
_USER_CONFIG_ENV = 'MCP_USER_CONFIG_FILE'      # 旧的全局单文件（兼容期仍认）
_USER_DIR_ENV = 'MCP_USER_CONFIG_DIR'         # 按主体隔离的目录
_REPO_ROOT = Path(__file__).resolve().parents[3]
_OWNER_UNSAFE = re.compile(r'[^0-9A-Za-z_.-]')
# 单用户部署（AUTH_MODE=off）与老配置迁移都落在这个主体名下。
DEFAULT_OWNER = 'default__default'


def owner_key_for_values(tenant_id: str = '', user_id: str = '') -> str:
    """把 (租户, 用户) 压成一个能当文件名的 key。

    写法与 `api/connectors_api._key` 一致：连接配置和连接状态（开关）必须落在
    同一个主体名下，否则会出现"配置是他的、开关是别人的"。
    """
    tenant = str(tenant_id or 'default').strip() or 'default'
    user = str(user_id or 'default').strip() or 'default'
    return _OWNER_UNSAFE.sub('_', f'{tenant}__{user}')[:120]


def owner_key_for(principal) -> str:
    """从请求主体取 owner_key。没有主体 → 空串（只看共享配置）。"""
    if principal is None:
        return ''
    return owner_key_for_values(getattr(principal, 'tenant_id', ''),
                                getattr(principal, 'user_id', ''))


def legacy_config_path() -> Path | None:
    """旧的全局单文件。**只有显式配了 MCP_USER_CONFIG_FILE 才算数。**

    不显式配就不读：否则部署机上遗留的 `profile/mcp.json` 会继续变成
    "所有人的连接器"，这正是要修掉的那个问题。
    """
    raw = os.getenv(_USER_CONFIG_ENV, '').strip()
    return Path(raw).expanduser() if raw else None


def user_config_dir() -> Path:
    raw = os.getenv(_USER_DIR_ENV, '').strip()
    if raw:
        return Path(raw).expanduser()
    return _REPO_ROOT / 'profile' / 'mcp-users'


def user_config_path(owner_key: str = '') -> Path:
    """某个主体自己的配置文件（**写入**用这个路径）。

    没有主体上下文时（运维脚本、CLI）落回旧单文件，保持原来的行为。
    """
    key = str(owner_key or '').strip()
    if not key:
        legacy = legacy_config_path()
        return legacy if legacy is not None else user_config_dir() / f'{DEFAULT_OWNER}.json'
    return user_config_dir() / f'{key}.json'


def _read_server_map(path: Path) -> dict:
    """读一个 `{"mcpServers": {...}}` 文件。坏文件只记错，不炸整张表。"""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        global LAST_ERROR
        LAST_ERROR = f'MCP 连接器配置读取失败（{path.name}）：{type(exc).__name__}'
        logger.warning(LAST_ERROR)
        return {}
    raw = data.get('mcpServers') if isinstance(data, dict) else None
    return raw if isinstance(raw, dict) else {}


def _entries_from_map(raw: dict, *, scope: str,
                      owner_key: str = '') -> list[Server]:
    out: list[Server] = []
    for name, entry in raw.items():
        if not isinstance(entry, dict) or entry.get('disabled') is True:
            continue
        if (str(entry.get('command') or '').strip()
                or str(entry.get('type') or '').lower() == 'stdio'):
            logger.warning('MCP %s（%s）使用 stdio，中台只接受 HTTP/SSE，已跳过',
                           name, owner_key or scope)
            continue
        kind = str(entry.get('type') or 'streamable-http').strip().lower()
        transport = 'sse' if kind == 'sse' else 'streamable_http'
        url = str(entry.get('url') or '').strip()
        if not url:
            logger.warning('MCP %s 缺少 url，已跳过', name)
            continue
        headers = entry.get('headers')
        headers = headers if isinstance(headers, dict) else {}
        server = _from_entry({
            'id': str(name), 'transport': transport, 'url': url,
            'headers': {str(k): str(v) for k, v in headers.items()},
            'title': str(entry.get('title') or name),
        }, scope=scope, owner_key=owner_key)
        if server:
            out.append(server)
    return out


def _shared_server_entries() -> list[Server]:
    """兼容期的全局单文件。仍对全体用户可见，优先级低于平台连接器。"""
    path = legacy_config_path()
    if path is None:
        return []
    return _entries_from_map(_read_server_map(path), scope=SCOPE_SHARED)


def _user_server_entries(owner_key: str = '') -> list[Server]:
    """某个主体自己的连接器。owner_key 为空 = 没有主体上下文，返回空。

    以前这里读的是一个固定路径，所以"谁改一次、全站生效"。现在每个主体
    一个文件，路径里带 `tenant__user`，用户之间不可能互相看见。
    """
    key = str(owner_key or '').strip()
    if not key:
        return []
    path = user_config_path(key)
    if not path.exists() and key == DEFAULT_OWNER and legacy_config_path() is None:
        # 迁移：老部署把用户连接器写在 `profile/mcp.json`。给 default 主体读
        # 一次存量，别人拿不到；这个主体一旦有了自己的文件就不再回看。
        # 显式配了 MCP_USER_CONFIG_FILE 的部署不做这步——那已经声明了"我就用
        # 全局单文件"，再往某个主体名下暗送一份会变成两套来源打架。
        legacy = _REPO_ROOT / 'profile' / 'mcp.json'
        if legacy.exists():
            path = legacy
    return _entries_from_map(_read_server_map(path), scope=SCOPE_USER,
                             owner_key=key)



def remove_user_server(owner_key: str, server_id: str) -> bool:
    """从某个主体的配置里删掉一条。返回是否真的删了。

    只动 user 作用域的那个文件：运维（ops）、平台（platform）、兼容期共享层
    的条目不该被用户页面上的一个请求改掉——那些不归他管。
    """
    key = str(owner_key or '').strip()
    if not key:
        return False
    path = user_config_path(key)
    raw = _read_server_map(path)
    if str(server_id) not in raw:
        return False
    raw.pop(str(server_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'mcpServers': raw}, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return True


def _platform_server_entries() -> list[Server]:
    """控制台发布的平台连接器（`store/platform_catalog`），对全体用户可见。

    它们和 `MCP_SERVERS` 里的条目是同一类东西——真实的服务端连接参数——所以
    只在这里转成 `Server` 就够了：工具发现、连接开关、OAuth 授权都不必另写分支。
    """
    from webpage_agent.store import platform_catalog

    out: list[Server] = []
    for row in platform_catalog.enabled_connectors():
        oauth = {'issuer': row.get('oauth_issuer') or '',
                 'scopes': row.get('oauth_scopes') or ()}
        server = _from_entry({
            'id': row.get('id'), 'transport': row.get('transport') or DEFAULT_TRANSPORT,
            'url': row.get('url'), 'headers': row.get('headers') or {},
            'title': row.get('name') or row.get('id'),
            'auth': row.get('auth') or '', 'oauth': oauth,
        }, scope=SCOPE_PLATFORM)
        if server is not None:
            out.append(server)
    return out


def _ops_server_entries() -> list[Server]:
    """运维配置：`MCP_SERVERS` / `MCP_SERVERS_FILE` / 旧的 `MCP_*` 单项变量。"""
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
    return unique


def servers(*, owner_key: str = '') -> list[Server]:
    """当前调用者能看见的服务端集合。

    `owner_key` 为空 = 没有登录主体（运维脚本、健康检查、CLI）：只有全站共享的
    那几类。给了 `owner_key` 才并上**这个主体自己的**连接器。

    优先级：MCP_SERVERS（运维）> 平台目录（控制台）> 兼容期全局单文件 > 用户自建。
    用户永远不能覆盖上面三层——同名静默替换等于把「同一个工具」换成另一个服务端。

    每次读文件、不做缓存：解析成本是一次小 JSON，换来的是"改配置立刻生效"，
    测试里也不用清缓存。
    """
    merged: list[Server] = []
    seen: set[str] = set()
    for server in (_ops_server_entries()
                   + _platform_server_entries()
                   + _shared_server_entries()
                   + _user_server_entries(owner_key)):
        if server.id in seen:
            logger.warning('MCP 连接器 %s（%s）与更高优先级的配置重名，已忽略',
                           server.id, server.scope)
            continue
        seen.add(server.id)
        merged.append(server)
    return merged


def servers_view(owner_key: str = '') -> list[Server]:
    """不带主体时走**无参调用** `servers()`。

    看起来只是省一个关键字，实际是给调用方留的形状：老代码、测试桩、运维脚本
    里大量存在 `servers()` 这种零参替换（`patch.object(..., lambda: [...])`）。
    把它们全部改成必须传 `owner_key` 是一次没有收益的破坏；而没有主体时
    "只看共享层"本来就是 `servers()` 的默认语义。
    """
    return servers(owner_key=owner_key) if owner_key else servers()


def by_id(server_id: str, *, owner_key: str = '') -> Server | None:
    """按 id 取服务端。**默认只查共享层**，用户自建的必须带 owner_key。

    这个默认值是刻意的：`by_id('github')` 不带主体时，两个用户各有一个
    `github` 就无从判断该返回谁。宁可返回 None（上层走既有兜底），
    也不能随机拿到别人的地址和鉴权头。
    """
    return next((s for s in servers(owner_key=owner_key) if s.id == server_id), None)


def deploy_server() -> Server | None:
    """承接部署类适配器工具的服务端：显式标 default 的优先，否则取第一个。"""
    items = servers()
    for server in items:
        if server.default:
            return server
    return items[0] if items else None


def tool_name(server: Server, name: str, total: int | None = None,
              *, owner_key: str = '') -> str:
    """生成对模型暴露的工具名。

    单服务端不加前缀，是为了不破坏已保存的计划与既有提示词；多服务端必须加，
    否则 `mcp__delete_site` 属于谁无法判断。

    **用户自建的一律带前缀**：两个用户各自有一个叫 `github` 的连接器是常态，
    走"只有一个就不加前缀"的捷径会让 `mcp__search` 这种名字根本回指不到主人
    （拆分时拿不到 server，只能落到默认部署服务端上）。
    """
    if getattr(server, 'scope', SCOPE_OPS) == SCOPE_USER:
        return f'{MCP_PREFIX}{server.id}__{name}'
    count = len(servers_view(owner_key)) if total is None else total
    if count <= 1:
        return f'{MCP_PREFIX}{name}'
    return f'{MCP_PREFIX}{server.id}__{name}'


def split_tool_name(name: str, *, owner_key: str = '') -> tuple[Server | None, str]:
    """把工具名拆回 (服务端, 原始工具名)；没带前缀或没权限时服务端为 None。"""
    body = name[len(MCP_PREFIX):] if name.startswith(MCP_PREFIX) else name
    if '__' in body:
        head, rest = body.split('__', 1)
        server = by_id(head, owner_key=owner_key)
        if server is not None:
            return server, rest
    return None, body


def status() -> dict:
    """健康检查用：全站共享的那几层。用户自建的不进这里（不按主体出报表）。"""
    items = servers()
    return {'servers': [{'id': s.id, 'transport': s.transport,
                         'scope': getattr(s, 'scope', SCOPE_OPS),
                         'default': s.default or (i == 0 and len(items) == 1)}
                        for i, s in enumerate(items)],
            'count': len(items), 'error': LAST_ERROR}
