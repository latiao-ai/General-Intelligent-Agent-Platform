"""publishing 能力包：把产物发布到 MCP 服务器。

部署专用逻辑（result_url 字段回退、base64 组装、哈希校验）都归这里，
通用 MCP 客户端不再替任意带 files 字段的工具注入 HTML（文档 §5.4）。
"""
from pathlib import Path

from app.integrations import mcp_adapter
from app.capabilities.base import ToolError
from app.config import settings
from app.events import action, emit
from app.files import save_json

# 本能力包自带的规划规则：发布是**有副作用**的动作，只在明确要求时出现。
PROMPT_RULES = (
    '【发布】只有用户明确要求发布到服务器/要访问地址时，才在最后加 deploy_site；'
    '若"部署"只是报告的研究主题（如"数据库的部署方式"），不要加。'
    '若工具清单里有删除站点的工具（delete_site 或带前缀的等价名称），'
    '把它写进 deploy_site 的 compensate，args 里用 ref 取部署返回的站点标识——'
    '这样后续步骤失败时系统能自动收回刚发布的站点。没有就别编造。'
)


async def _deploy_site(args: dict, state: dict) -> dict:
    if not settings.deploy_enabled:
        raise ToolError('DEPLOY_ENABLED=false，部署已关闭')
    artifact = state.get('artifact') or state.get('prior_artifact')
    if not artifact:
        raise ToolError('还没有 HTML 产物，请先生成报告再部署')
    if not state.get('artifact'):
        # 本轮没有重新渲染，部署的是上一轮留下的产物——明确告知，别让人误以为重新生成过。
        emit(action('复用上一轮产物', '本轮没有重新生成 HTML', '♻️'))
    emit(action('正在部署站点', '', '🚀'))
    try:
        result = await mcp_adapter.deploy_html(artifact)
    except Exception as exc:
        raise ToolError('部署失败，请核对 MCP 配置与服务端日志') from exc
    save_json(Path(artifact['html_path']).parent / 'deployment.json', result)
    emit(action('部署完成',
                result.get('public_url') or '结果待确认',
                '✅' if result.get('status') == 'success' else '⚠️'))
    return {'deployment': result,
            'status': 'deployed' if result.get('status') == 'success'
                      else 'deploy_needs_check'}
