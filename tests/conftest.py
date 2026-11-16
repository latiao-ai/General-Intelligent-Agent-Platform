"""测试全局夹具。

## 为什么要把这两份「机器上的配置」隔离掉

`mcp_servers.servers()` 会把四类条目合并：`MCP_SERVERS`（环境变量）、平台目录
（控制台发布）、`MCP_USER_CONFIG_FILE`（兼容期的全局单文件）、以及
`MCP_USER_CONFIG_DIR` 下**按主体分文件**的用户自建连接器。

后两者是**本机可变数据**：开发机上只要有人加过一个连接器，断言「配置里只有
这两个服务端」的用例就会凭空多出几条而失败。这种失败最容易误判——代码没问题，
是环境里多了数据，而报错看起来完全像代码坏了。

所以这里给每个用例一份空的、临时的配置。想测这三条来源的用例自己往里写即可；
`monkeypatch.setenv` 在用例内部依然会覆盖这里的默认值。
"""
import pytest


@pytest.fixture(autouse=True)
def isolated_platform_catalog(tmp_path, monkeypatch):
    # 用 tmp_path（每个用例一份）而不是 tmp_path_factory.mktemp(name)：
    # 后者对同名只创建一次，整个 session 会共用同一份文件，测试之间互相污染。
    path = tmp_path / 'platform_catalog' / 'catalog.json'
    monkeypatch.setenv('PLATFORM_CATALOG_FILE', str(path))
    return path


@pytest.fixture(autouse=True)
def isolated_user_mcp_config(tmp_path, monkeypatch):
    path = tmp_path / 'mcp.json'
    path.write_text('{"mcpServers": {}}', encoding='utf-8')
    monkeypatch.setenv('MCP_USER_CONFIG_FILE', str(path))
    return path


@pytest.fixture(autouse=True)
def isolated_user_mcp_dir(tmp_path, monkeypatch):
    """按主体隔离的连接器目录也要隔离。

    不隔离的话，「某个用户在哪台开发机上配过一个 github 连接器」就会漏进断言，
    失败的写法还特别像代码坏了（多了几条服务端）。想测多主体的用例自己往
    这个目录里写文件即可（`tests/test_mcp_user_isolation.py`）。
    """
    path = tmp_path / 'mcp-users'
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('MCP_USER_CONFIG_DIR', str(path))
    return path
