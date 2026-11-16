"""用户自建连接器必须按登录主体隔离。

回归的是这样一件事：连接器配置原来只有一个固定路径（`profile/mcp.json`），
于是"A 在页面上加了一个 github，B 的对话里也出现，还带着 A 的鉴权头"。
隔离的单位是 `tenant__user`，路径、连接状态、工具名三处必须同时对齐——
任何一处漏了，都会退化成"能看见但用不了"或者"用得了但不是自己的"。
"""
import asyncio
import json

from webpage_agent.integrations import mcp_servers


def _write(owner: str, servers: dict) -> None:
    path = mcp_servers.user_config_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'mcpServers': servers}), encoding='utf-8')


def _names(owner: str) -> list[str]:
    return [item.id for item in mcp_servers.servers(owner_key=owner)]


def test_each_owner_sees_only_own_servers():
    _write('acme__alice', {'alice-vault': {'url': 'https://alice.example/mcp'}})
    _write('acme__bob', {'bob-vault': {'url': 'https://bob.example/mcp'}})

    assert _names('acme__alice') == ['alice-vault']
    assert _names('acme__bob') == ['bob-vault']


def test_same_name_resolves_to_each_owners_own_url():
    """同名不算冲突：两个人都可以有一个叫 github 的连接器，各指各的地址。"""
    _write('acme__alice', {'github': {'url': 'https://alice.example/mcp',
                                      'headers': {'Authorization': 'Bearer a'}}})
    _write('acme__bob', {'github': {'url': 'https://bob.example/mcp',
                                    'headers': {'Authorization': 'Bearer b'}}})

    alice = mcp_servers.by_id('github', owner_key='acme__alice')
    bob = mcp_servers.by_id('github', owner_key='acme__bob')
    assert alice is not None and bob is not None
    assert alice.url == 'https://alice.example/mcp'
    assert bob.url == 'https://bob.example/mcp'
    assert bob.http_headers == {'Authorization': 'Bearer b'}
    # 不带主体就无从判断该给谁——必须是 None，不能随机挑一个。
    assert mcp_servers.by_id('github') is None


def test_no_owner_means_shared_layer_only():
    _write('acme__alice', {'alice-vault': {'url': 'https://alice.example/mcp'}})
    assert 'alice-vault' not in _names('')


def test_user_cannot_shadow_ops_config(monkeypatch):
    monkeypatch.setenv('MCP_SERVERS', json.dumps([
        {'id': 'github', 'url': 'https://ops.example/mcp'},
    ]))
    _write('acme__alice', {'github': {'url': 'https://alice.example/mcp'}})

    same = [item for item in mcp_servers.servers(owner_key='acme__alice')
            if item.id == 'github']
    assert len(same) == 1
    assert same[0].url == 'https://ops.example/mcp'
    assert same[0].scope == mcp_servers.SCOPE_OPS


def test_user_server_always_carries_prefix():
    """只有一个服务端也不走"省略前缀"的捷径。

    `mcp__search` 这种名字拆分时拿不到 server，只能落到默认部署服务端上——
    用户自建的连接器必须永远带前缀，才回指得到自己。
    """
    _write('acme__alice', {'vault': {'url': 'https://vault.example/mcp'}})
    server = mcp_servers.by_id('vault', owner_key='acme__alice')
    assert server is not None

    name = mcp_servers.tool_name(server, 'search', total=1,
                                 owner_key='acme__alice')
    assert name == 'mcp__vault__search'
    routed, bare = mcp_servers.split_tool_name(name, owner_key='acme__alice')
    assert routed is not None and routed.id == 'vault'
    assert bare == 'search'


def test_shared_single_server_keeps_legacy_name(monkeypatch):
    """共享层只有一个服务端时，工具名保持 `mcp__<tool>`，已存计划不受影响。"""
    monkeypatch.setenv('MCP_SERVERS', json.dumps([
        {'id': 'publishing', 'url': 'https://ops.example/mcp', 'default': True},
    ]))
    server = mcp_servers.by_id('publishing')
    assert server is not None
    assert mcp_servers.tool_name(server, 'deploy_site', total=1) == 'mcp__deploy_site'


def test_stdio_in_user_file_is_still_rejected():
    """按主体分文件不改变传输方式边界：stdio 依然只能在 MCP_SERVERS 里配。"""
    _write('acme__alice', {'local': {'type': 'stdio', 'command': 'node',
                                     'args': ['server.js']}})
    assert _names('acme__alice') == []


def test_remove_user_server_only_touches_own_file():
    _write('acme__alice', {'vault': {'url': 'https://alice.example/mcp'},
                           'keep': {'url': 'https://keep.example/mcp'}})
    _write('acme__bob', {'vault': {'url': 'https://bob.example/mcp'}})

    assert mcp_servers.remove_user_server('acme__alice', 'vault') is True
    assert _names('acme__alice') == ['keep']
    assert _names('acme__bob') == ['vault']
    # 已经不存在了，不能再报"删掉了"
    assert mcp_servers.remove_user_server('acme__alice', 'vault') is False


def test_legacy_global_file_is_shared_scope(monkeypatch, tmp_path):
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps({'mcpServers': {
        'legacy': {'type': 'streamable-http', 'url': 'https://legacy.example/mcp'},
    }}), encoding='utf-8')
    monkeypatch.setenv('MCP_USER_CONFIG_FILE', str(path))

    items = mcp_servers.servers(owner_key='acme__alice')
    legacy = next(item for item in items if item.id == 'legacy')
    assert legacy.scope == mcp_servers.SCOPE_SHARED


def test_owner_key_is_shared_with_connector_state():
    """配置的落盘名必须和连接状态（开关）用的是同一套 key。"""
    from webpage_agent.api.connectors_api import _key
    from types import SimpleNamespace

    principal = SimpleNamespace(tenant_id='acme', user_id='alice')
    assert _key(principal) == mcp_servers.owner_key_for(principal)


def test_tool_catalog_differs_per_owner(monkeypatch):
    """发现层也要按主体走：同一个进程里，两个人拿到的工具清单互不串。"""
    from webpage_agent.tools import core as tools_core

    _write('acme__alice', {'alice-vault': {'url': 'https://alice.example/mcp'}})
    _write('acme__bob', {'bob-vault': {'url': 'https://bob.example/mcp'}})

    async def fake_list(server, **kwargs):
        return [{'name': 'search', 'description': '搜索',
                 'inputSchema': {'type': 'object', 'properties': {}}}]

    async def no_caps(server):
        return {}

    monkeypatch.setattr(tools_core.mcp_adapter, 'list_server_tools', fake_list)
    monkeypatch.setattr(tools_core, '_discover_capabilities', no_caps)

    async def run():
        alice = await tools_core.mcp_tools(force=True, tenant_id='acme',
                                           user_id='alice')
        bob = await tools_core.mcp_tools(force=True, tenant_id='acme',
                                         user_id='bob')
        return alice, bob

    alice, bob = asyncio.run(run())
    assert [tool.name for tool in alice] == ['mcp__alice-vault__search']
    assert [tool.name for tool in bob] == ['mcp__bob-vault__search']
    assert {tool.server_id for tool in alice} == {'alice-vault'}
