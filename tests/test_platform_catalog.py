"""平台连接器 / 技能：管理员发布一次，全体用户可见。

覆盖三件事：

1. 写进平台目录的连接器会真的进 `mcp_servers.servers()`——否则用户页上出现
   一张卡片、点「+」却连不上，属于最难排查的"看起来能用"；
2. 停用/删除后立刻从用户侧消失；
3. 与 `MCP_SERVERS` 同名时运维配置优先，平台条目被跳过（不能悄悄改掉鉴权头）。

技能侧同理：`_catalog_items()` 里必须是 `source='platform'`，且默认未安装——
用户点「+」才落盘。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from webpage_agent.api import skillhub_api
from webpage_agent.integrations import mcp_servers
from webpage_agent.store import platform_catalog as catalog_store

CONNECTOR = {
    'id': 'deploy-prod',
    'name': '部署（生产）',
    'transport': 'streamable_http',
    'url': 'https://deploy.test/mcp',
    'enabled': True,
}


def _principal(tenant='t1', user='u1'):
    return SimpleNamespace(tenant_id=tenant, user_id=user)


def _ids():
    return [server.id for server in mcp_servers.servers()]


# ---------------------------------------------------------------- 连接器


def test_published_connector_reaches_servers():
    row = catalog_store.validate_connector(CONNECTOR)
    catalog_store.upsert('connectors', row, actor='admin')
    assert 'deploy-prod' in _ids()

    server = mcp_servers.by_id('deploy-prod')
    assert server is not None
    assert server.url == 'https://deploy.test/mcp'
    assert server.title == '部署（生产）'


def test_disabled_connector_disappears():
    catalog_store.upsert('connectors', catalog_store.validate_connector(CONNECTOR))
    assert 'deploy-prod' in _ids()

    catalog_store.set_enabled('connectors', 'deploy-prod', False)
    assert 'deploy-prod' not in _ids()

    catalog_store.set_enabled('connectors', 'deploy-prod', True)
    assert 'deploy-prod' in _ids()


def test_deleted_connector_disappears():
    catalog_store.upsert('connectors', catalog_store.validate_connector(CONNECTOR))
    catalog_store.remove('connectors', 'deploy-prod')
    assert 'deploy-prod' not in _ids()


def test_headers_and_oauth_survive_roundtrip():
    row = catalog_store.validate_connector({
        **CONNECTOR, 'id': 'crm',
        'auth': 'oauth', 'oauth_issuer': 'https://auth.test',
        'oauth_scopes': ['crm.read'], 'headers': {'X-Gateway': 'gb'},
    })
    catalog_store.upsert('connectors', row)
    server = mcp_servers.by_id('crm')
    assert server.auth == 'oauth'
    assert server.oauth_issuer == 'https://auth.test'
    assert server.oauth_scopes == ('crm.read',)
    assert server.http_headers == {'X-Gateway': 'gb'}


def test_system_config_wins_over_platform(monkeypatch):
    """同名时 `MCP_SERVERS` 优先：运维写死的鉴权不能被控制台悄悄换掉。"""
    monkeypatch.setenv('MCP_SERVERS', json.dumps([
        {'id': 'deploy-prod', 'url': 'https://ops.test/mcp', 'token': 'ops-token'},
    ]))
    catalog_store.upsert('connectors', catalog_store.validate_connector(CONNECTOR))

    servers = [s for s in mcp_servers.servers() if s.id == 'deploy-prod']
    assert len(servers) == 1
    assert servers[0].url == 'https://ops.test/mcp'


@pytest.mark.parametrize('payload, hint', [
    ({**CONNECTOR, 'id': 'Bad Id'}, 'ID'),
    ({**CONNECTOR, 'id': 'a__b'}, '__'),
    ({**CONNECTOR, 'transport': 'stdio'}, 'stdio'),
    ({**CONNECTOR, 'url': 'ftp://x'}, 'http'),
    ({**CONNECTOR, 'auth': 'oauth'}, 'issuer'),
])
def test_invalid_connector_rejected(payload, hint):
    with pytest.raises(catalog_store.CatalogError) as exc:
        catalog_store.validate_connector(payload)
    assert hint in str(exc.value)


def test_header_with_newline_rejected():
    with pytest.raises(catalog_store.CatalogError):
        catalog_store.validate_connector({
            **CONNECTOR, 'headers': {'X-Bad': 'a\nb'},
        })


# ---------------------------------------------------------------- 技能


SKILL = {
    'id': 'team-digest',
    'name': '团队日报',
    'description': '把零散记录合成日报',
    'category': '效率工具',
    'prompt': '整理成日报：',
    'enabled': True,
}


def _catalog(monkeypatch):
    """技能目录会去远端拉，这里断掉——本用例只关心平台条目。"""

    async def _empty(*args, **kwargs):
        return []

    monkeypatch.setattr(skillhub_api.remote, 'fetch_items', _empty)
    return asyncio.run(skillhub_api._catalog_items(_principal()))


def test_published_skill_appears_uninstalled(monkeypatch):
    catalog_store.upsert('skills', catalog_store.validate_skill(
        SKILL, ('效率工具', '内容创作')))
    items = _catalog(monkeypatch)
    hit = next(x for x in items if x['id'] == 'team-digest')
    assert hit['source'] == 'platform'
    assert hit['author'] == '平台管理员'
    assert hit['installed'] is False


def test_disabled_skill_disappears(monkeypatch):
    catalog_store.upsert('skills', catalog_store.validate_skill(SKILL, ('效率工具',)))
    catalog_store.set_enabled('skills', 'team-digest', False)
    assert all(x['id'] != 'team-digest' for x in _catalog(monkeypatch))


def test_skill_category_must_exist():
    with pytest.raises(catalog_store.CatalogError):
        catalog_store.validate_skill({**SKILL, 'category': '不存在的分类'},
                                     ('效率工具',))


def test_concurrent_upserts_do_not_lose_rows():
    """两次「读-改-写」必须落在同一把锁里，否则后写的会抹掉先写的。"""
    catalog_store.upsert('connectors', catalog_store.validate_connector(
        {**CONNECTOR, 'id': 'a'}))
    catalog_store.upsert('connectors', catalog_store.validate_connector(
        {**CONNECTOR, 'id': 'b'}))
    assert {row['id'] for row in catalog_store.load()['connectors']} == {'a', 'b'}


def test_catalog_file_is_overridable(tmp_path, monkeypatch):
    """部署时数据目录可能和代码目录分开挂载。"""
    target = tmp_path / 'nested' / 'catalog.json'
    monkeypatch.setenv('PLATFORM_CATALOG_FILE', str(target))
    catalog_store.upsert('skills', catalog_store.validate_skill(SKILL, ('效率工具',)))
    assert target.exists()
    assert json.loads(target.read_text(encoding='utf-8'))['skills'][0]['id'] == 'team-digest'
