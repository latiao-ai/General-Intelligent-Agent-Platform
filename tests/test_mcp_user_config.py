import json
from pathlib import Path

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
