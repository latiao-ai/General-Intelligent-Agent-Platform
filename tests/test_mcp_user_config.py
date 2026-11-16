import asyncio
import json
from types import SimpleNamespace

from webpage_agent.api import mcp_manager_api
from webpage_agent.integrations import mcp_servers


def test_user_remote_mcp_is_merged(monkeypatch, tmp_path):
    monkeypatch.setenv('MCP_SERVERS', json.dumps([
        {'id': 'system', 'url': 'https://system.example/mcp'},
    ]))
    path = tmp_path / 'mcp.json'
    path.write_text(json.dumps({'mcpServers': {
        'custom': {'type': 'streamable-http', 'url': 'https://custom.example/mcp',
                   'headers': {'X-Key': 'v'}},
        'local': {'type': 'stdio', 'command': 'node', 'args': ['server.js']},
    }}), encoding='utf-8')
    monkeypatch.setenv('MCP_USER_CONFIG_FILE', str(path))
    items = mcp_servers.servers()
    ids = [item.id for item in items]
    assert 'system' in ids
    assert 'custom' in ids
    assert 'local' not in ids
    custom = mcp_servers.by_id('custom')
    assert custom is not None
    assert custom.http_headers == {'X-Key': 'v'}


def test_system_name_wins(monkeypatch, tmp_path):
    monkeypatch.setenv('MCP_SERVERS', json.dumps([
        {'id': 'same', 'url': 'https://system.example/mcp'},
    ]))
    path = tmp_path / 'mcp.json'
    path.write_text(json.dumps({'mcpServers': {
        'same': {'type': 'streamable-http', 'url': 'https://user.example/mcp'},
    }}), encoding='utf-8')
    monkeypatch.setenv('MCP_USER_CONFIG_FILE', str(path))
    same = [item for item in mcp_servers.servers() if item.id == 'same']
    assert len(same) == 1
    assert same[0].url == 'https://system.example/mcp'


def test_servers_endpoint_can_return_only_mine(monkeypatch):
    """`include_system=0` 只回自己配的：前端「添加连接器」面板靠它过滤。

    那个面板操作的是用户自己那份配置文件，ops/平台下发的服务混进去既改不动、
    也不该出现在"我的 MCP"里。
    """
    monkeypatch.setenv('MCP_SERVERS', json.dumps([
        {'id': 'ops-db', 'url': 'https://ops.example/mcp'},
    ]))
    monkeypatch.delenv('MCP_USER_CONFIG_FILE', raising=False)
    principal = SimpleNamespace(tenant_id='acme', user_id='alice')
    path = mcp_servers.user_config_path(mcp_servers.owner_key_for(principal))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'mcpServers': {
        'mine': {'type': 'streamable-http', 'url': 'https://mine.example/mcp'},
    }}), encoding='utf-8')

    everything = asyncio.run(mcp_manager_api.list_servers(True, principal))
    assert {'ops-db', 'mine'} <= {item['id'] for item in everything['items']}

    only_mine = asyncio.run(mcp_manager_api.list_servers(False, principal))
    assert [item['id'] for item in only_mine['items']] == ['mine']
    assert only_mine['items'][0]['source'] == 'custom'
