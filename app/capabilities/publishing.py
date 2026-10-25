"""publishing 能力包：部署工具的**平台侧适配**，不是一条独立的部署通道。

部署工具本身来自 MCP 发现——`deploy_site` 就是服务端声明的那个工具，`tool_key`
是 `mcp:<server_id>:<tool>`，调用走 `tools._make_mcp_runner` 这条统一通道：
账本（prepared/in_flight/succeeded）、`mcp_calls` 记录、事件、结果未定判定，
与其它远端工具完全一致。

这里只写平台**必须**自己负责的三件事：

1. `claims()`：认出哪个远端工具是这个服务端的部署工具（配置项 `deploy_tool`）；
2. `DeployAdapter.precheck()`：**调用前**的闸门（开关、有没有产物）；
3. `interpret_deploy()`：**调用后**把回执翻译成 `deployment`
   （访问地址按配置取并校验、content_hash 对齐、落 deployment.json）。

以前这三件事写在一个内置 `deploy_site` 里，它自己直连 MCP：同一个远端工具于是
在工具清单里出现两次（内置 + `mcp__*`），账本只记到 `local:publishing:deploy`
（看不出这轮依赖哪个服务端），`mcp_calls` 里也没有它，`DEPLOY_ENABLED=false`
时还能绕过闸门走 `mcp__deploy_site`。现在只有一份工具、一个身份。
"""
import os
from dataclasses import dataclass
from pathlib import Path

from app.capabilities.base import ToolError
from app.config import settings
from app.events import action, emit
from app.files import save_json
from app.integrations import mcp_adapter, mcp_servers

CAPABILITY = 'publishing'
# 对模型暴露的名字：保持 `deploy_site` 不变——已保存的计划、提示词、
# 前端阶段名都认这个名字，没必要为了"统一前缀"去改它。
TOOL_NAME = 'deploy_site'
LABEL = '部署站点'
DESCRIPTION = ('把当前 HTML 产物部署到服务器并返回访问地址。必须先有产物'
               '（如 render_sources_html 之后）、且用户明确要求发布时调用。')

# 本能力包自带的规划规则：发布是**有副作用**的动作，只在明确要求时出现。
PROMPT_RULES = (
    '【发布】只有用户明确要求发布到服务器/要访问地址时，才在最后加 deploy_site；'
    '若"部署"只是报告的研究主题（如"数据库的部署方式"），不要加。'
    '若工具清单里有删除站点的工具（delete_site 或带前缀的等价名称），'
    '把它写进 deploy_site 的 compensate，args 里用 ref 取部署返回的站点标识——'
    '这样后续步骤失败时系统能自动收回刚发布的站点。没有就别编造。'
)


# 回执里表示"还没做完"的状态词。**不要**改成"成功白名单"：各服务端的成功说法
# 不一样（实测线上服务端返回 deployed），拿白名单卡只会把真实成功判成待确认。
UNFINISHED_STATUSES = {'pending', 'queued', 'running', 'in_progress', 'processing',
                       'failed', 'failure', 'error', 'errored', 'partial'}


def configured_tool(server) -> str:
    """这个服务端声明的部署工具名；没声明返回空串。

    单项配置（`MCP_*`）时代只有 `MCP_TOOL_NAME` 一种说法，所以承接部署的那个
    服务端允许回退到它——其它服务端不许借这个环境变量"抢"部署工具。
    """
    name = (getattr(server, 'deploy_tool', '') or '').strip()
    if name:
        return name
    deploy = mcp_servers.deploy_server()
    if deploy is not None and deploy.id == getattr(server, 'id', None):
        return os.getenv('MCP_TOOL_NAME', '').strip()
    return ''


def claims(server, tool_name: str) -> bool:
    """这个远端工具是不是该服务端的部署工具。"""
    return bool(tool_name) and tool_name == configured_tool(server)


def adapter(server, tool_name: str) -> 'DeployAdapter':
    return DeployAdapter(server=server, tool_name=tool_name)


@dataclass(frozen=True)
class DeployAdapter:
    """执行链上部署工具的适配器：调用前闸门 + 调用后解释回执。

    适配器只碰"平台语义"，不碰传输：怎么连服务端、怎么把文件内容塞进参数，
    都在 `app/integrations/mcp_adapter.py` 里，其它远端工具走同一套。
    """
    server: object = None
    tool_name: str = ''

    def artifact(self, state: dict) -> dict | None:
        """要部署哪份产物：本轮没有就落到上一轮留下的那份。"""
        return state.get('artifact') or state.get('prior_artifact')

    def precheck(self, args: dict, state: dict) -> None:
        """调用**之前**的闸门。宁可在这里报错，也不要发一次注定无效的写请求。"""
        if not settings.deploy_enabled:
            raise ToolError('DEPLOY_ENABLED=false，部署已关闭')
        if not self.artifact(state):
            raise ToolError('还没有 HTML 产物，请先生成报告再部署')

    def before(self, args: dict, state: dict) -> None:
        if not state.get('artifact'):
            # 本轮没有重新渲染，部署的是上一轮留下的产物——明确告知，
            # 别让人误以为重新生成过。
            emit(action('复用上一轮产物', '本轮没有重新生成 HTML', '♻️'))
        emit(action('正在部署站点', '', '🚀'))

    def interpret(self, payload, args: dict, state: dict) -> dict:
        deployment = interpret_deploy(payload, self.artifact(state) or {}, self.server)
        emit(action('部署完成',
                    deployment.get('public_url') or '结果待确认',
                    '✅' if deployment.get('status') == 'success' else '⚠️'))
        # 这里返回的 `status` 只是**这一步的阶段小结**（执行器会把它从 run 状态
        # 里摘出来，只用于耗时行），run 终态由 app/outcome.py 统一判定。
        return {'deployment': deployment, 'status': marker_of(deployment)}


def marker_of(deployment: dict) -> str:
    return 'deployed' if (deployment or {}).get('status') == 'success' \
        else 'unverified'


def interpret_deploy(payload, artifact: dict, server=None) -> dict:
    """把部署工具的原始回执翻译成 `deployment` 记录（文档 §6.4 回执归一）。

    服务端字段名不统一（`public_url` / `url` / `entry_url`），所以按配置项
    `result_url_field` 取并校验；`content_hash` 用本地产物的 sha256，
    "线上这份到底是哪份产物"才说得清。

    成败判定以**能验证的访问地址**为准，再用回执里的状态兜底：各服务端的说法
    不一样（实测线上服务端返回的是 `deployed`，不是 `success`），拿白名单去卡
    状态名只会把成功判成"待确认"。反过来，明确表示未完成的状态（`pending`、
    `failed`…）即使带了地址也不能算部署成功。

    拿不到合法地址时**不报失败**：写请求可能真的成功了，只是回执看不懂。
    这种情况必须落在"待确认"，让用户去核站点列表，而不是谎称失败。
    """
    reported = ''
    if isinstance(payload, dict):
        reported = str(payload.get('status') or '').strip().lower()
    try:
        url = mcp_adapter.result_url(payload, server)
    except Exception:                                  # noqa: BLE001
        url = ''
    ok = bool(url) and reported not in UNFINISHED_STATUSES
    deployment = {
        'status': 'success' if ok else 'needs_check',
        'deployment_id': payload.get('site_id') if isinstance(payload, dict) else None,
        'public_url': url,
        'content_hash': (artifact or {}).get('sha256', ''),
        'reported_status': reported,
    }
    html_path = (artifact or {}).get('html_path')
    if html_path:
        save_json(Path(html_path).parent / 'deployment.json', deployment)
    return deployment


async def deploy_once(artifact: dict, server=None) -> dict:
    """**绕过执行链**的单次部署，只给手工补救脚本（`scripts.retry_deploy`）。

    正常部署走执行器：工具由 MCP 发现、经统一 runner 调用、有账本与事件。
    这里把同样的"发现 → 调用 → 解释回执"拼一次，是为了在"结果未定"之后能
    人工重放一次，而不必把整个计划（和模型调用）重跑一遍。
    """
    server = server or mcp_servers.deploy_server()
    if server is None:
        raise ToolError('没有配置任何 MCP 服务端，无法部署')
    tool_name = configured_tool(server)
    if not tool_name:
        raise ToolError('未配置部署工具名（MCP_TOOL_NAME / deploy_tool）')
    from app import mcp_connections
    payload = await mcp_adapter.call_tool(
        tool_name, {}, artifact=artifact, server=server,
        token=await mcp_connections.bearer_for_current(server))
    return interpret_deploy(payload, artifact, server)
