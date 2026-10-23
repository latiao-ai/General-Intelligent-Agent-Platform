"""本地 MCP Server（仅用于验证客户端链路，不是真实部署服务）。

启动：
    python -m scripts.local_mock_mcp_server
默认监听 http://127.0.0.1:8765/mcp（streamable HTTP）。

用途：在远端 Server 不可用（如 API Key 失效）时，验证本项目 MCP 客户端
的传输、工具发现、inputSchema 校验与调用链路是否正常。它不会真正部署文件，
只把 HTML 写到本地 artifacts/_mock_sites/ 下。
"""
import hashlib
from pathlib import Path

from mcp.server.fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = PROJECT_ROOT / 'artifacts' / '_mock_sites'

mcp = FastMCP('local-mock-deploy', host='127.0.0.1', port=8765)


@mcp.tool()
def publish_report(html_content: str, filename: str = 'index.html') -> dict:
    """发布一份 HTML 报告，返回可访问地址。

    Args:
        html_content: 完整的 HTML 文本。
        filename: 目标文件名。
    """
    if not html_content.strip():
        raise ValueError('html_content 不能为空')
    safe_name = Path(filename).name or 'index.html'
    digest = hashlib.sha256(html_content.encode('utf-8')).hexdigest()
    site_id = digest[:12]
    folder = SITE_DIR / site_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / safe_name).write_text(html_content, encoding='utf-8')
    return {
        'status': 'success',
        'deployment_id': site_id,
        'public_url': f'http://127.0.0.1:8765/sites/{site_id}/{safe_name}',
        'content_hash': digest,
    }


@mcp.tool()
def list_reports() -> dict:
    """列出已经发布过的报告。"""
    if not SITE_DIR.exists():
        return {'sites': []}
    return {'sites': sorted(p.name for p in SITE_DIR.iterdir() if p.is_dir())}


@mcp.tool()
def get_server_info() -> dict:
    """返回这个 MCP Server 的基本信息。"""
    return {'name': 'local-mock-deploy', 'version': '0.1.0', 'transport': 'streamable_http'}


if __name__ == '__main__':
    mcp.run(transport='streamable-http')
