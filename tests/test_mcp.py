"""能力包与 MCP：部署闸门、参数、路由、业务失败、结果落点。"""
import base64
import json
import os
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.core_support import patch_all_emits
from webpage_agent.integrations import mcp_adapter


class DeployGateTests(unittest.IsolatedAsyncioTestCase):
    """部署：工具身份来自 MCP，闸门与回执归一份在能力包适配器里。

    以前的 `deploy_site` 是内置工具、自己直连 MCP：清单里同时出现内置与
    `mcp__deploy_site` 两份，账本记成 `local:publishing:deploy`（看不出依赖哪个
    服务端），`mcp_calls` 里也没有它。现在它就是一个带适配器的 MCP 工具，
    这组用例把这四条一起钉住。
    """

    def setUp(self):
        from webpage_agent.capabilities import publishing
        fixture = patch.object(publishing, 'settings', replace(publishing.settings, deploy_enabled=True))
        fixture.start()
        self.addCleanup(fixture.stop)

    @staticmethod
    def server():
        from webpage_agent.integrations import mcp_servers
        return mcp_servers.Server(id='publishing', url='https://a.example/mcp',
                                  deploy_tool='deploy_site', default=True)

    def tool(self, effect='write'):
        from webpage_agent.tools import core as tool_module
        return tool_module._deploy_tool(
            self.server(), {'name': 'deploy_site', 'effect_type': effect})

    async def test_tool_takes_its_identity_from_mcp(self):
        tool = self.tool(effect='destructive')
        self.assertEqual(tool.name, 'deploy_site')
        self.assertEqual(tool.tool_key, 'mcp:publishing:deploy_site')
        self.assertEqual(tool.effect, 'destructive')      # 用服务端的 annotations
        self.assertEqual(tool.capability, 'publishing')
        self.assertEqual(tool.server_id, 'publishing')
        self.assertIn('【发布】', tool.prompt_rules)

    async def test_requires_an_artifact_before_touching_the_server(self):
        from webpage_agent.tools import core as tool_module
        call = AsyncMock()
        with patch.object(tool_module.mcp_adapter, 'call_tool', call), \
                patch_all_emits(lambda e: None):
            with self.assertRaises(tool_module.ToolError) as ctx:
                await self.tool().run({}, {})
        self.assertIn('还没有 HTML 产物', ctx.exception.safe_message)
        call.assert_not_awaited()

    async def test_requires_global_enable(self):
        from webpage_agent.tools import core as tool_module
        from webpage_agent.capabilities import publishing as publishing_cap
        call = AsyncMock()
        with patch.object(publishing_cap, 'settings',
                          SimpleNamespace(deploy_enabled=False)), \
                patch.object(tool_module.mcp_adapter, 'call_tool', call), \
                patch_all_emits(lambda e: None):
            with self.assertRaises(tool_module.ToolError) as ctx:
                await self.tool().run({}, {'artifact': {'html_path': 'x'}})
        self.assertIn('部署已关闭', ctx.exception.safe_message)
        call.assert_not_awaited()

    async def test_deploys_records_mcp_call_and_status(self):
        from webpage_agent.tools import core as tool_module
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<html></html>', encoding='utf-8')
            artifact = {'html_path': str(html), 'sha256': 'abc'}

            async def fake_call(name, arguments, artifact=None, server=None):
                self.assertEqual(name, 'deploy_site')
                self.assertEqual(server.id, 'publishing')
                return {'status': 'success', 'site_id': 'site_7',
                        'url': 'https://x/1'}

            seen = []
            with patch.object(tool_module.mcp_adapter, 'call_tool', fake_call), \
                    patch_all_emits(seen.append):
                result = await self.tool().run({}, {'artifact': artifact})

            self.assertEqual(result['status'], 'deployed')   # 阶段小结，不是 run 终态
            self.assertEqual(result['deployment']['public_url'], 'https://x/1')
            self.assertEqual(result['deployment']['deployment_id'], 'site_7')
            self.assertEqual(result['deployment']['content_hash'], 'abc')
            # 和其它 MCP 工具一样留下调用记录（含服务端），否则审计查不到这条写操作
            self.assertEqual(result['mcp_calls'][0]['tool'], 'deploy_site')
            self.assertEqual(result['mcp_calls'][0]['server'], 'publishing')
            saved = json.loads((Path(folder) / 'deployment.json').read_text('utf-8'))
            self.assertEqual(saved['public_url'], 'https://x/1')
            self.assertTrue(any(e.get('label') == '部署完成' for e in seen))

    async def test_deploy_falls_back_to_prior_artifact(self):
        """用户单说一句"部署到服务器"也要能落到上一轮的产物上。"""
        from webpage_agent.tools import core as tool_module
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<html></html>', encoding='utf-8')
            prior = {'html_path': str(html), 'sha256': 'p'}

            async def fake_call(name, arguments, artifact=None, server=None):
                self.assertEqual(artifact, prior)      # 注入的就是上一轮那份
                return {'status': 'success', 'url': 'https://x/9'}

            seen = []
            with patch.object(tool_module.mcp_adapter, 'call_tool', fake_call), \
                    patch_all_emits(seen.append):
                result = await self.tool().run({}, {'prior_artifact': prior})
            self.assertEqual(result['status'], 'deployed')   # 阶段小结，不是 run 终态
            # 必须明确告知复用了上一轮产物，避免误以为这轮重新生成过
            self.assertTrue(any(e.get('label') == '复用上一轮产物' for e in seen))

    async def test_unreadable_receipt_is_needs_check_not_failure(self):
        """回执看不懂不等于失败：站点可能真的建出来了，不能说"没部署"。"""
        from webpage_agent.tools import core as tool_module
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<html></html>', encoding='utf-8')

            async def fake_call(name, arguments, artifact=None, server=None):
                return {'status': 'pending'}           # 既没地址也没成功标记

            with patch.object(tool_module.mcp_adapter, 'call_tool', fake_call), \
                    patch_all_emits(lambda e: None):
                result = await self.tool().run(
                    {}, {'artifact': {'html_path': str(html), 'sha256': 'a'}})
            self.assertEqual(result['status'], 'unverified')
            self.assertEqual(result['deployment']['status'], 'needs_check')
            self.assertEqual(result['deployment']['public_url'], '')

    async def test_live_server_payload_counts_as_success(self):
        """线上服务端回的是 `status: deployed`，不是 success——不能判成待确认。"""
        from webpage_agent.capabilities import publishing as publishing_cap
        payload = {'status': 'deployed', 'site_id': 'n6HRJXd8',
                   'name': 'site-n6HRJXd8',
                   'url': 'http://42.194.146.23:3456/sites/n6HRJXd8/'}
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<html></html>', encoding='utf-8')
            deployment = publishing_cap.interpret_deploy(
                payload, {'html_path': str(html), 'sha256': 'h'}, self.server())
        self.assertEqual(deployment['status'], 'success')
        self.assertEqual(deployment['public_url'], payload['url'])
        self.assertEqual(deployment['deployment_id'], 'n6HRJXd8')
        self.assertEqual(publishing_cap.marker_of(deployment), 'deployed')

    async def test_remote_failure_surfaces_as_tool_error(self):
        from webpage_agent.tools import core as tool_module

        async def boom(name, arguments, artifact=None, server=None):
            raise RuntimeError('502')

        with patch.object(tool_module.mcp_adapter, 'call_tool', boom), \
                patch_all_emits(lambda e: None):
            with self.assertRaises(tool_module.ToolError) as ctx:
                await self.tool().run({}, {'artifact': {'html_path': 'x',
                                                       'sha256': 'a'}})
        self.assertIn('deploy_site', ctx.exception.safe_message)

    async def test_discovered_deploy_tool_is_the_only_deploy_entry(self):
        """清单里只留一份部署工具：旧的内置那份已经没了，旧名字仍能解析。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.capabilities import publishing as publishing_cap
        server = self.server()

        async def fake_list(_server):
            return [{'name': 'deploy_site', 'inputSchema': {'type': 'object'},
                     'effect_type': 'write'},
                    {'name': 'list_sites', 'inputSchema': {'type': 'object'},
                     'effect_type': 'read'}]

        with patch.object(tool_module.mcp_adapter, 'list_server_tools', fake_list), \
                patch.object(tool_module.mcp_servers, 'servers', lambda: [server]), \
                patch.object(tool_module, 'settings',
                            SimpleNamespace(deploy_enabled=True)):
            tool_module._MCP_CACHE.update(at=0.0, tools=[], signature=())
            tools = await tool_module.mcp_tools(force=True)
            self.assertEqual([t.name for t in tools],
                             ['deploy_site', 'mcp__list_sites'])
            deploy = tools[0]
            self.assertEqual(deploy.tool_key, 'mcp:publishing:deploy_site')
            # 新名字、旧名字都解析到同一个工具（旧计划续跑不会失效）
            self.assertIs(await tool_module.find_tool('deploy_site'), deploy)
            self.assertIs(await tool_module.find_tool('mcp__deploy_site'), deploy)
            # DEPLOY_ENABLED=false：连占位都不给，计划里不会出现注定失败的一步
            with patch.object(tool_module, 'settings',
                              SimpleNamespace(deploy_enabled=False)), \
                    patch.object(publishing_cap, 'settings',
                                 SimpleNamespace(deploy_enabled=False)):
                tool_module._MCP_CACHE.update(at=0.0, tools=[], signature=())
                off = await tool_module.catalog()
        self.assertNotIn('deploy_site', [t.name for t in off])
        self.assertIn('mcp__list_sites', [t.name for t in off])

    async def test_placeholder_keeps_the_tool_visible_when_server_is_down(self):
        """服务端掉线时留占位工具：报错要清楚，而不是当作未知工具静默跳过。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_servers
        server = self.server()
        with patch.object(mcp_servers, 'servers', lambda: [server]), \
                patch.object(mcp_servers, 'deploy_server', lambda: server):
            tool = tool_module._deploy_placeholder(
                {'publishing': 'RuntimeError: 502'})
        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, 'deploy_site')
        self.assertEqual(tool.tool_key, 'mcp:publishing:deploy_site')
        with patch_all_emits(lambda e: None), \
                self.assertRaises(tool_module.ToolError) as ctx:
            await tool.run({}, {'artifact': {'html_path': 'x'}})
        self.assertIn('部署不可用', ctx.exception.safe_message)
        self.assertIn('502', ctx.exception.safe_message)


FILES_SCHEMA = {
    'properties': {
        'files': {'type': 'array', 'items': {'properties': {'path': {}, 'content': {}}}}
    }
}
TEXT_SCHEMA = {'properties': {'html': {'type': 'string'}}}


class McpPayloadTests(unittest.TestCase):
    """按工具真实 inputSchema 组装参数。"""

    def tool(self, schema):
        return SimpleNamespace(inputSchema=schema)

    def test_detect_files_base64(self):
        self.assertEqual(mcp_adapter.payload_mode(self.tool(FILES_SCHEMA)), 'files_base64')

    def test_detect_text(self):
        self.assertEqual(mcp_adapter.payload_mode(self.tool(TEXT_SCHEMA)), 'text')

    def test_explicit_mode_overrides_detection(self):
        with patch.dict(os.environ, {'MCP_PAYLOAD_MODE': 'text'}):
            self.assertEqual(mcp_adapter.payload_mode(self.tool(FILES_SCHEMA)), 'text')

    def test_build_arguments_base64_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<h1>你好</h1>', encoding='utf-8')
            with patch.dict(os.environ, {'MCP_EXTRA_ARGS': '{}'}, clear=False):
                args = mcp_adapter.build_arguments(
                    self.tool(FILES_SCHEMA), {'html_path': str(html)})
        entry = args['files'][0]
        self.assertEqual(entry['path'], 'index.html')
        # 必须是真的 base64 字节，否则服务端会静默落成乱码站点。
        self.assertEqual(base64.b64decode(entry['content']).decode('utf-8'), '<h1>你好</h1>')

    def test_build_arguments_text_mode_passes_raw_html(self):
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<h1>hi</h1>', encoding='utf-8')
            with patch.dict(os.environ, {'MCP_EXTRA_ARGS': '{}'}, clear=False):
                args = mcp_adapter.build_arguments(
                    self.tool(TEXT_SCHEMA), {'html_path': str(html)})
        self.assertEqual(args['html'], '<h1>hi</h1>')

    def test_extra_args_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<h1>hi</h1>', encoding='utf-8')
            with patch.dict(os.environ, {'MCP_EXTRA_ARGS': '{"ttl":"7d"}'}, clear=False):
                args = mcp_adapter.build_arguments(
                    self.tool(FILES_SCHEMA), {'html_path': str(html)})
        self.assertEqual(args['ttl'], '7d')

    def test_result_url_falls_back_across_field_names(self):
        with patch.dict(os.environ, {'MCP_RESULT_URL_FIELD': 'public_url'}, clear=False):
            self.assertEqual(mcp_adapter.result_url({'public_url': 'https://a/b'}), 'https://a/b')
            # 服务端实际返回的是 url，配置项对不上时也要能取到。
            self.assertEqual(mcp_adapter.result_url({'url': 'https://c/d'}), 'https://c/d')
            self.assertEqual(mcp_adapter.result_url({'entry_url': 'https://e/f'}), 'https://e/f')
            with self.assertRaises(RuntimeError):
                mcp_adapter.result_url({'nope': 'https://x/y'})
            with self.assertRaises(RuntimeError):
                mcp_adapter.result_url({'url': 'ftp://bad'})


class McpPromptRuleTests(unittest.IsolatedAsyncioTestCase):
    """MCP 规划规则不点名某一家的工具。业务说法随那一家的配置走。"""

    async def test_kernel_rules_stay_generic_until_a_server_brings_its_own(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.capabilities.registry import Tool
        from webpage_agent.integrations import mcp_servers

        plain = mcp_servers.Server(id='inventory', transport='streamable_http',
                                   url='https://inventory.example/mcp')
        montage = mcp_servers.Server(
            id='openmontage', transport='streamable_http',
            url='https://montage.example/mcp',
            prompt_rules='生成视频用 video_generation，不要用 video_analyzer。')

        async def fake_list(_server):
            return [{'name': 'list_items', 'inputSchema': {'type': 'object'},
                     'description': '列出库存'}]

        with patch.object(tool_module.mcp_adapter, 'list_server_tools', fake_list), \
                patch.object(tool_module.mcp_servers, 'servers', lambda: [plain]):
            tool_module._MCP_CACHE.update(at=0.0, tools=[], signature=(),
                                          capabilities={})
            tools = await tool_module.mcp_tools(force=True)
        listed = tools[0]
        self.assertNotIn('站点', listed.description)
        self.assertNotIn('delete_site', listed.prompt_rules)
        self.assertNotIn('video_generation', listed.prompt_rules)
        local = [tool for tool in tool_module.BUILTIN_TOOLS
                 if tool.name != 'deploy_site']
        plain_prompt = nodes.planner_system(local + [listed])
        self.assertIn('远端产物不是本地文件', plain_prompt)
        self.assertNotIn('video_generation', plain_prompt)
        self.assertNotIn('delete_site', plain_prompt)
        self.assertNotIn('canvas_nodes_create', plain_prompt)

        video = Tool('mcp__run_tool', 'MCP·run_tool', '生成', {'type': 'object'},
                     None, capability='mcp', server_id='openmontage',
                     prompt_rules=montage.prompt_rules)
        video_prompt = nodes.planner_system([video])
        self.assertIn('video_generation', video_prompt)
        self.assertIn('远端产物不是本地文件', video_prompt)
        inventory_only = nodes.planner_system([listed])
        self.assertNotIn('video_analyzer', inventory_only)

    def test_prompt_rules_come_from_server_config(self):
        from webpage_agent.integrations import mcp_servers
        raw = json.dumps([{
            'id': 'openmontage', 'url': 'https://montage.example/mcp',
            'prompt_rules': '  生成用 video_generation。  ',
        }, {
            'id': 'inventory', 'url': 'https://inventory.example/mcp',
            'prompt_rules': {'not': 'a string'},
        }])
        with patch.dict(os.environ, {'MCP_SERVERS': raw, 'MCP_URL': ''},
                        clear=False):
            servers = {server.id: server for server in mcp_servers.servers()}
        self.assertEqual(servers['openmontage'].prompt_rules, '生成用 video_generation。')
        self.assertEqual(servers['inventory'].prompt_rules, '')


class ResultLocusTests(unittest.TestCase):
    """结果落点：公开网页、平台上的数据、平台上的修改，三条路不能混用。"""

    def catalog(self):
        from webpage_agent.capabilities.registry import Tool
        return [
            Tool('web_search', '检索', 'd', {'type': 'object'}, None,
                 scope='public', effect='read'),
            Tool('answer_directly', '回答', 'd', {'type': 'object'}, None,
                 effect='read'),
            Tool('canvas_get', '读取画布', 'd', {'type': 'object'}, None,
                 tool_key='mcp:inventory:canvas_get', effect='read',
                 server_id='inventory'),
            Tool('canvas_update', '修改画布', 'd', {'type': 'object'}, None,
                 tool_key='mcp:inventory:canvas_update', effect='write',
                 server_id='inventory'),
        ]

    def codes(self, locus, steps, criteria=None):
        from webpage_agent.agent import nodes
        return {item['code'] for item in nodes.check_result_locus(
            locus, steps, self.catalog(), criteria or [])}

    def test_chat_can_search_and_cannot_write_the_platform(self):
        self.assertEqual(self.codes('chat', [{'tool': 'web_search'}]), set())
        self.assertEqual(
            self.codes('chat', [{'tool': 'canvas_update'}]),
            {'locus_mismatch'})

    def test_platform_state_cannot_use_the_public_web(self):
        self.assertEqual(
            self.codes('platform_state', [{'tool': 'web_search'}]),
            {'public_web_for_platform_state', 'platform_state_needs_read'})
        self.assertEqual(
            self.codes('platform_state', [{'tool': 'canvas_get'}]), set())
        self.assertEqual(
            self.codes('platform_state', [{'tool': 'answer_directly'}]), set())

    def test_platform_change_needs_a_write_and_its_receipt(self):
        self.assertIn(
            'platform_change_needs_write',
            self.codes('platform_change', [{'tool': 'web_search'}]))
        self.assertEqual(
            self.codes('platform_change', [{'tool': 'answer_directly'}]), set())
        self.assertEqual(
            self.codes('platform_change', [{'tool': 'canvas_update'}],
                       [{'kind': 'tool_succeeded', 'target': 'canvas_update'}]),
            set())
        self.assertEqual(
            self.codes('platform_change', [{'tool': 'canvas_update'}]),
            {'platform_change_needs_evidence'})

    def test_public_web_without_a_locus_is_rejected(self):
        self.assertEqual(self.codes('', [{'tool': 'web_search'}]),
                         {'bad_result_locus'})
        self.assertEqual(self.codes('', [{'tool': 'answer_directly'}]), set())

    def test_scene_brief_uses_the_signed_context(self):
        from webpage_agent.agent import nodes
        self.assertEqual(nodes._scene_brief(None), '无')
        self.assertEqual(nodes._scene_brief({'context': {}}), '无')
        text = nodes._scene_brief({
            'verified': True,
            'context': {'project_id': 'p1', 'selected': 'shot-3'}})
        self.assertIn('平台已签名', text)
        self.assertIn('shot-3', text)


class McpRoutingTests(unittest.IsolatedAsyncioTestCase):
    """多 MCP Server：工具命名、路由、失败隔离。"""

    def setUp(self):
        from webpage_agent.capabilities import publishing
        fixture = patch.object(publishing, 'settings', replace(publishing.settings, deploy_enabled=True))
        fixture.start()
        self.addCleanup(fixture.stop)

    MULTI = json.dumps([
        {'id': 'publishing', 'url': 'https://a.example/mcp',
         'deploy_tool': 'deploy_site', 'default': True},
        {'id': 'ops', 'url': 'https://b.example/mcp'},
    ])

    async def test_single_server_keeps_legacy_tool_names(self):
        from webpage_agent.integrations import mcp_servers
        with patch.dict(os.environ, {'MCP_SERVERS': '',
                                     'MCP_URL': 'https://a.example/mcp'}, clear=False):
            servers = mcp_servers.servers()
            self.assertEqual(len(servers), 1)
            self.assertEqual(mcp_servers.tool_name(servers[0], 'list_sites'),
                             'mcp__list_sites')
            self.assertTrue(mcp_servers.deploy_server().default)

    async def test_multiple_servers_qualify_tool_names(self):
        from webpage_agent.integrations import mcp_servers
        with patch.dict(os.environ, {'MCP_SERVERS': self.MULTI,
                                     'MCP_URL': ''}, clear=False):
            servers = mcp_servers.servers()
            self.assertEqual([s.id for s in servers], ['publishing', 'ops'])
            self.assertEqual(mcp_servers.tool_name(servers[0], 'list_sites'),
                             'mcp__publishing__list_sites')
            self.assertEqual(mcp_servers.tool_name(servers[1], 'delete_site'),
                             'mcp__ops__delete_site')
            server, bare = mcp_servers.split_tool_name('mcp__ops__delete_site')
            self.assertEqual((server.id, bare), ('ops', 'delete_site'))
            self.assertEqual(mcp_servers.deploy_server().id, 'publishing')

    async def test_invalid_entries_are_skipped_not_fatal(self):
        from webpage_agent.integrations import mcp_servers
        raw = json.dumps([
            {'id': 'ok', 'url': 'https://a/mcp'},
            {'id': '', 'url': 'https://b/mcp'},
            {'id': 'no_url'},
            {'id': 'bad__id', 'url': 'https://c/mcp'},
        ])
        with patch.dict(os.environ, {'MCP_SERVERS': raw, 'MCP_URL': ''},
                        clear=False):
            self.assertEqual([s.id for s in mcp_servers.servers()], ['ok'])

    async def test_discovery_is_isolated_per_server(self):
        from webpage_agent.tools import core as tool_module
        seen = []

        async def fake_list(server):
            seen.append(server.id)
            if server.id == 'ops':
                raise RuntimeError('ops 掉线')
            return [{'name': 'list_sites', 'inputSchema': {'type': 'object'}}]

        with patch.dict(os.environ, {'MCP_SERVERS': self.MULTI,
                                     'MCP_URL': ''}, clear=False), \
                patch.object(tool_module.mcp_adapter, 'list_server_tools', fake_list):
            tool_module._MCP_CACHE.update(at=0.0, tools=[], signature=())
            discovered = await tool_module.mcp_tools(force=True)

        self.assertEqual(sorted(seen), ['ops', 'publishing'])
        # 掉线的 ops 一个工具都不贡献，publishing 的 list_sites 照常可用
        mcp_only = [t for t in discovered if t.capability == 'mcp']
        self.assertEqual([t.name for t in mcp_only],
                         ['mcp__publishing__list_sites'])
        self.assertIn('ops', tool_module.mcp_status()['errors'])
        self.assertEqual(mcp_only[0].server_id, 'publishing')
        # 部署工具没发现到：不是消失，而是留占位（报错要说清楚）
        deploy = [t for t in discovered if t.capability == 'publishing']
        self.assertEqual([t.name for t in deploy], ['deploy_site'])

    async def test_runner_calls_the_server_it_belongs_to(self):
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_servers
        with patch.dict(os.environ, {'MCP_SERVERS': self.MULTI,
                                     'MCP_URL': ''}, clear=False):
            target = mcp_servers.by_id('ops')
        captured = {}

        async def fake_call(name, arguments, artifact=None, server=None):
            captured['name'] = name
            captured['server'] = getattr(server, 'id', None)
            return {'ok': True}

        with patch.object(tool_module.mcp_adapter, 'call_tool', fake_call):
            runner = tool_module._make_mcp_runner('delete_site', target)
            result = await runner({'site_id': 'x'}, {})

        self.assertEqual(captured, {'name': 'delete_site', 'server': 'ops'})
        self.assertEqual(result['mcp_calls'][0]['server'], 'ops')

    async def test_old_tool_name_resolves_after_prefix_change(self):
        """旧计划里只有 mcp__<tool>，服务端变多后要能按尾部唯一匹配回去。"""
        from webpage_agent.tools import core as tool_module
        legacy = tool_module.Tool('mcp__ops__list_sites', 'MCP·list_sites',
                                  '', {}, None, tool_key='mcp:ops:list_sites')

        async def fake_tools(force=False):
            return [legacy]

        with patch.object(tool_module, 'mcp_tools', fake_tools):
            found = await tool_module.find_tool('mcp__list_sites')
        self.assertIs(found, legacy)


class McpBusinessFailureTests(unittest.IsolatedAsyncioTestCase):
    """MCP 的"业务失败"必须被认出来（需求 4），内部工具名必须校验（需求 2）。

    背景：真实事故里服务端正常返回
    `{"status": "error", "code": "E_RESOURCE_NOT_FOUND", "message": "tool ... not found"}`，
    协议层没有 isError，于是被当成成功一路往下走，最后用户只看到"没有产出可用结果"。
    """

    def test_failure_envelope_is_detected(self):
        from webpage_agent.integrations import mcp_adapter
        self.assertTrue(mcp_adapter.failure_of(
            {'status': 'error', 'code': 'E_RESOURCE_NOT_FOUND',
             'message': 'tool generate_script not found'}))
        self.assertTrue(mcp_adapter.failure_of({'status': 'failed'}))
        self.assertTrue(mcp_adapter.failure_of({'ok': False}))
        self.assertTrue(mcp_adapter.failure_of({'error': 'boom'}))
        # 成功的回执不能被误判
        self.assertEqual(mcp_adapter.failure_of({'status': 'deployed'}), '')
        self.assertEqual(mcp_adapter.failure_of({'status': 'success', 'url': 'x'}), '')
        self.assertEqual(mcp_adapter.failure_of({'tools': []}), '')

    def test_result_payload_raises_on_error_envelope(self):
        from types import SimpleNamespace as NS
        from webpage_agent.integrations import mcp_adapter

        block = NS(type='text', text='{"status": "error", '
                                    '"code": "E_RESOURCE_NOT_FOUND", '
                                    '"message": "tool generate_script not found"}')
        result = NS(isError=False, structuredContent=None, content=[block])
        with self.assertRaises(mcp_adapter.MCPToolFailure) as ctx:
            mcp_adapter.result_payload(result)
        self.assertIn('generate_script', str(ctx.exception))

        ok = NS(isError=False, structuredContent={'status': 'ok', 'job_id': 'j1'},
                content=[])
        self.assertEqual(mcp_adapter.result_payload(ok)['job_id'], 'j1')

    def test_mcp_iserror_preserves_confirmed_parameter_rejection(self):
        from types import SimpleNamespace as NS
        from webpage_agent.integrations import mcp_adapter

        rejected = NS(isError=True, structuredContent=None, content=[NS(
            type='text', text='{"reason":"invalid_parameter",'
                              '"message":"audioPitch 的值无效"}')])
        with self.assertRaises(mcp_adapter.MCPToolFailure) as ctx:
            mcp_adapter.result_payload(rejected)
        self.assertIn('audioPitch', str(ctx.exception))
        self.assertEqual(mcp_adapter.certainty_of_error(ctx.exception), 'failed_confirmed')

        unknown = NS(isError=True, structuredContent=None, content=[NS(
            type='text', text='{"reason":"operation_failed",'
                              '"message":"Traceback: private server path"}')])
        with self.assertRaises(RuntimeError) as ctx:
            mcp_adapter.result_payload(unknown)
        self.assertNotIn('private server path', str(ctx.exception))
        self.assertEqual(mcp_adapter.certainty_of_error(ctx.exception), 'unknown')

        from jsonschema import ValidationError as JsonSchemaValidationError
        self.assertEqual(mcp_adapter.certainty_of_error(
            JsonSchemaValidationError('invalid parameter')), 'not_executed')

    def test_enum_prefixed_status_is_parsed_correctly(self):
        """`ToolStatus.AVAILABLE` 这种带枚举前缀的状态不能当成"不可用"。

        真踩过：拿全等去比，于是整台服务端被判成"什么都不能做"，计划里的执行
        步骤被全部拦掉。
        """
        from webpage_agent.tools import core as tool_module
        self.assertTrue(tool_module._is_available('ToolStatus.AVAILABLE'))
        self.assertTrue(tool_module._is_available('available'))
        self.assertTrue(tool_module._is_available(''))
        self.assertFalse(tool_module._is_available('ToolStatus.UNAVAILABLE'))
        self.assertFalse(tool_module._is_available('UNAVAILABLE'))
        # 只要还有能用的工具，就不该把整台服务端判死
        tool_module._MCP_CACHE['capabilities'] = {'montage': {
            'video_compose': {'status': 'ToolStatus.AVAILABLE'},
            'video_stitch': {'status': 'ToolStatus.UNAVAILABLE'}}}
        self.assertEqual(tool_module.unusable_reason('montage'), '')
        tool_module._MCP_CACHE['capabilities'] = {'montage': {
            'video_compose': {'status': 'ToolStatus.UNAVAILABLE',
                              'install': 'Install FFmpeg: https://ffmpeg.org'}}}
        reason = tool_module.unusable_reason('montage')
        self.assertIn('全不可用', reason)
        self.assertIn('FFmpeg', reason)
        tool_module._MCP_CACHE['capabilities'] = {}

    async def test_runner_turns_business_failure_into_step_failure(self):
        """status:error 的回执 → ToolError（带着服务端原话），不是"成功"。"""
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_adapter, mcp_servers
        from webpage_agent.tools.core import Tool

        server = mcp_servers.Server(id='montage', transport='streamable_http',
                                    url='https://a.example/mcp')

        # 真实链路上是 result_payload 抛 MCPToolFailure（服务端正常返回一条 error）
        async def fake_call(name, arguments, artifact=None, server=None):
            raise mcp_adapter.MCPToolFailure(
                f'tool {arguments.get("name")} not found', 'E_RESOURCE_NOT_FOUND')

        tool = Tool('mcp__run_tool', 'MCP·run_tool', '', {'type': 'object',
                                                          'properties': {}},
                    tool_module._make_mcp_runner('run_tool', server),
                    tool_key='mcp:montage:run_tool', effect='write',
                    capability='mcp', server_id='montage')
        plan = {'steps': [{'tool': 'mcp__run_tool', 'step_id': 's1',
                           'args': {'name': 'generate_script', 'inputs': {}}},
                          {'tool': 'mcp__run_tool', 'step_id': 's2',
                           'args': {'name': 'generate_storyboard', 'inputs': {}},
                           'depends_on': ['s1']}]}
        with patch.object(tool_module, 'find_tool', AsyncMock(return_value=tool)), \
                patch.object(tool_module.mcp_adapter, 'call_tool', fake_call), \
                patch_all_emits(lambda e: None):
            result = await nodes.run_step({'plan': plan, 'step_index': 0,
                                           'user_input': '做个视频'})
        # 失败步骤：整份计划停下（依赖它的 s2 不会跑）
        self.assertEqual(result['failure'] and True, True)
        self.assertIn('E_RESOURCE_NOT_FOUND', result['failure'])
        self.assertIn('generate_script', result['failure'])
        self.assertEqual(result['step_index'], len(plan['steps']))
        self.assertEqual(nodes.next_step({**result, 'plan': plan}), 'stop')

    async def test_inner_tool_name_must_come_from_discovery(self):
        """run_tool.name 不在发现结果里 → 本地就挡住，并把真实名字给出来。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_servers

        server = mcp_servers.Server(id='montage', transport='streamable_http',
                                    url='https://a.example/mcp',
                                    capability_tool='list_capabilities',
                                    tool_name_arg='name')
        capabilities = {'tts_selector': {'status': 'AVAILABLE'},
                        'subtitle_gen': {'status': 'AVAILABLE'},
                        'video_compose': {'status': 'UNAVAILABLE'}}
        tool_module._MCP_CACHE['capabilities'] = {'montage': capabilities}

        # 1) 编造的名字：直接失败，报错里有真实清单
        with self.assertRaises(tool_module.ToolError) as ctx:
            tool_module._check_inner_name(
                server, {'name': 'generate_script'}, capabilities)
        message = ctx.exception.safe_message
        self.assertIn('generate_script', message)
        self.assertIn('tts_selector', message)

        # 2) 存在但不可用的工具：明确说不可用，别去调度
        with self.assertRaises(tool_module.ToolError) as ctx:
            tool_module._check_inner_name(
                server, {'name': 'video_compose'}, capabilities)
        self.assertIn('不可用', ctx.exception.safe_message)

        # 3) 清单里的可用工具：放行
        tool_module._check_inner_name(server, {'name': 'tts_selector'}, capabilities)
        tool_module._MCP_CACHE['capabilities'] = {}

    async def test_discovery_reads_the_capability_list(self):
        """发现阶段顺带读内部能力清单，并把名字告诉规划器（需求 1）。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_servers

        server = mcp_servers.Server(id='montage', transport='streamable_http',
                                    url='https://a.example/mcp',
                                    capability_tool='list_capabilities',
                                    tool_name_arg='name')

        async def fake_list(_server):
            return [{'name': 'list_capabilities', 'inputSchema': {'type': 'object'},
                     'description': '列能力'},
                    {'name': 'run_tool', 'inputSchema': {'type': 'object'},
                     'description': '执行任意工具'}]

        async def fake_call(name, arguments, artifact=None, server=None):
            self.assertEqual(name, 'list_capabilities')
            return {'tools': [{'name': 'tts_selector', 'status': 'AVAILABLE',
                               'capability': 'tts'},
                              {'name': 'video_compose', 'status': 'UNAVAILABLE',
                               'capability': 'video_post'}]}

        with patch.object(tool_module.mcp_adapter, 'list_server_tools', fake_list), \
                patch.object(tool_module.mcp_adapter, 'call_tool', fake_call), \
                patch.object(tool_module.mcp_servers, 'servers', lambda: [server]):
            tool_module._MCP_CACHE.update(at=0.0, tools=[], signature=(),
                                          capabilities={})
            tools = await tool_module.mcp_tools(force=True)

        names = {tool.name: tool for tool in tools}
        self.assertIn('mcp__list_capabilities', names)
        # 规划器能在描述里直接看到真实名字（可用的与不可用的分开说）
        description = names['mcp__list_capabilities'].description
        self.assertIn('tts_selector', description)
        self.assertIn('video_compose', description)
        self.assertIn('不可用', description)
        # 校验用的清单也进了缓存
        self.assertIn('tts_selector', tool_module.known_inner_names('montage'))
        tool_module._MCP_CACHE['capabilities'] = {}


class CapabilityRegistryTests(unittest.IsolatedAsyncioTestCase):
    """能力包注册化：机制在内核，声明归能力包。

    这一组回答一个问题——"加一个新能力要不要改内核？"答案必须是不要。
    """

    async def test_registry_is_idempotent(self):
        from webpage_agent.capabilities import registry
        first = [tool.name for tool in registry.load()]
        second = [tool.name for tool in registry.load()]
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(set(first)))
        # 顺序稳定（按能力包名 + 工具名），提示词与 diff 才稳定
        specs = registry.snapshot()
        self.assertEqual(first, sorted(first,
                                       key=lambda n: (specs[n].capability, n)))

    async def test_every_registered_tool_has_a_valid_spec(self):
        from webpage_agent.capabilities import registry
        for spec in registry.load():
            with self.subTest(tool=spec.name):
                self.assertTrue(spec.label.strip())
                self.assertTrue(spec.description.strip())
                self.assertIn(spec.effect, registry.VALID_EFFECTS)
                self.assertEqual(spec.args.get('type'), 'object')
                self.assertTrue(spec.tool_key.startswith('local:'))
                self.assertIn(spec.capability, spec.tool_key)
                self.assertTrue(callable(spec.run))

    async def test_tool_keys_are_stable_identity(self):
        """tool_key 已经写进账本与幂等键，换注册方式不能顺带改了身份。"""
        from webpage_agent.tools import core as tool_module
        self.assertEqual({name: spec.tool_key
                          for name, spec in tool_module.BUILTIN_BY_NAME.items()},
                         {'web_search': 'local:web:search',
                          'fetch_pages': 'local:web:fetch',
                          'render_sources_html': 'local:export:sources_html',
                          'answer_with_sources': 'local:answers:with_sources',
                          'answer_directly': 'local:answers:direct'})

    async def test_kernel_does_not_import_business_packages(self):
        """内核（tools/core.py）不许 import 具体业务能力包——那是一切耦合的源头。"""
        import ast
        source = Path('src/webpage_agent/tools/core.py').read_text(encoding='utf-8')
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ''
                if module == 'webpage_agent.capabilities':
                    # from webpage_agent.capabilities import web as x
                    imported.update(alias.name for alias in node.names)
                elif module.startswith('webpage_agent.capabilities.'):
                    imported.add(module.split('.')[-1])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith('webpage_agent.capabilities.'):
                        imported.add(alias.name.split('.')[-1])
        # 只允许注册器与共享底座。具体能力包通过钩子接入，内核不 import 它们。
        self.assertEqual(imported, {'registry', 'base'})

    async def test_a_new_capability_is_just_a_new_file(self):
        """模拟"加一个能力包"：只写一个文件，内核与清单一个字都不改。

        用 exec 一个独立模块来模拟 `webpage_agent/capabilities/demo.py`：它自己声明
        CAPABILITY / PROMPT_RULES / @tool，注册器立刻能看见它。
        """
        import sys
        import types
        from webpage_agent.tools import core as tool_module
        from webpage_agent.capabilities import registry

        source = (
            "from webpage_agent.capabilities.registry import tool\n"
            "CAPABILITY = 'demo'\n"
            "PROMPT_RULES = '【演示】用户要演示时才用 demo_ping。'\n"
            "@tool(name='demo_ping', label='演示工具', description='回一个 pong',\n"
            "      args={'type': 'object', 'properties': {}}, effect='read')\n"
            "async def demo_ping(args, state):\n"
            "    return {'answer': 'pong'}\n"
        )
        module = types.ModuleType('webpage_agent.capabilities.demo')
        module.__package__ = 'webpage_agent.capabilities'
        sys.modules['webpage_agent.capabilities.demo'] = module
        try:
            exec(compile(source, '<demo capability>', 'exec'), module.__dict__)

            spec = registry.snapshot()['demo_ping']
            self.assertEqual(spec.tool_key, 'local:demo:demo_ping')   # 身份自动生成
            self.assertEqual(spec.capability, 'demo')
            # 规则跟着包走：规划器只拼"候选工具所属能力"的规则
            self.assertEqual(tool_module.planner_rules([spec]),
                             ['【演示】用户要演示时才用 demo_ping。'])
            # 执行层能按名字找到它，不需要任何清单改动
            found = await tool_module.find_tool('demo_ping')
            self.assertIsNotNone(found)
            self.assertEqual(found.tool_key, 'local:demo:demo_ping')
        finally:
            sys.modules.pop('webpage_agent.capabilities.demo', None)


class InnerToolParamGuardTests(unittest.IsolatedAsyncioTestCase):
    """内部工具的参数也要读出来并校验（用户报的第三次失败：'output_dir' is a required property）。"""

    async def asyncSetUp(self):
        from webpage_agent.tools import core as tool_module
        tool_module._MCP_CACHE['capabilities'] = {}
        self.addCleanup(lambda: tool_module._MCP_CACHE.update(capabilities={}))

    def test_required_params_are_exposed_to_the_planner(self):
        from webpage_agent.tools import core as tool_module
        capabilities = {
            'direct_clip_search': {'status': 'ToolStatus.AVAILABLE',
                                   'required': ['output_dir', 'queries'],
                                   'params': ['output_dir', 'queries', 'max_results']},
            'tts_selector': {'status': 'ToolStatus.AVAILABLE', 'required': ['text']},
        }
        line = tool_module._capability_line(capabilities)
        # 规划器必须看得到"要传什么"，否则调用必然因为缺必填参数失败
        self.assertIn('direct_clip_search(必填:output_dir,queries)', line)
        self.assertIn('tts_selector(必填:text)', line)

    def test_missing_required_input_fails_before_the_call(self):
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_servers

        server = mcp_servers.Server(id='montage', transport='streamable_http',
                                    url='https://a.example/mcp',
                                    capability_tool='list_capabilities',
                                    tool_name_arg='name')
        capabilities = {'direct_clip_search': {
            'status': 'ToolStatus.AVAILABLE',
            'required': ['output_dir', 'queries'],
            'params': ['output_dir', 'queries', 'max_results']}}

        with self.assertRaises(tool_module.ToolError) as ctx:
            tool_module._check_inner_name(
                server, {'name': 'direct_clip_search', 'inputs': {'queries': ['city']}},
                capabilities)
        message = ctx.exception.safe_message
        self.assertIn('output_dir', message)
        self.assertIn('缺少必填参数', message)
        self.assertEqual(ctx.exception.certainty, 'not_executed')

        tool_module._check_inner_name(
            server, {'name': 'direct_clip_search',
                     'inputs': {'output_dir': 'clips', 'queries': ['city']}},
            capabilities)

    def test_generation_tools_are_listed_before_analysis(self):
        from webpage_agent.tools import core as tool_module
        capabilities = {
            'video_analyzer': {'status': 'AVAILABLE', 'capability': 'analysis',
                               'required': ['source']},
            'grok_video': {'status': 'AVAILABLE', 'capability': 'video_generation',
                           'required': ['prompt']},
        }
        line = tool_module._capability_line(capabilities, limit=1)
        self.assertLess(line.find('grok_video'), line.find('video_analyzer')
                        if 'video_analyzer' in line else len(line))
        self.assertIn('grok_video', line)
        self.assertIn('被省略的仍然可用', line)

    async def test_discovery_records_required_and_params(self):
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_servers

        server = mcp_servers.Server(id='montage', transport='streamable_http',
                                    url='https://a.example/mcp',
                                    capability_tool='list_capabilities',
                                    tool_name_arg='name')

        async def fake_list(_server):
            return [{'name': 'run_tool', 'inputSchema': {'type': 'object'}}]

        async def fake_call(name, arguments, artifact=None, server=None):
            return {'tools': [{
                'name': 'direct_clip_search', 'status': 'ToolStatus.AVAILABLE',
                'input_schema': {'type': 'object', 'required': ['output_dir', 'queries'],
                                 'properties': {'output_dir': {'type': 'string'},
                                                'queries': {'type': 'array'},
                                                'max_results': {'type': 'integer'}}}}]}

        with patch.object(tool_module.mcp_adapter, 'list_server_tools', fake_list), \
                patch.object(tool_module.mcp_adapter, 'call_tool', fake_call), \
                patch.object(tool_module.mcp_servers, 'servers', lambda: [server]):
            tool_module._MCP_CACHE.update(at=0.0, tools=[], signature=(),
                                          capabilities={})
            await tool_module.mcp_tools(force=True)
        info = tool_module.known_inner_names('montage')['direct_clip_search']
        self.assertEqual(info['required'], ['output_dir', 'queries'])
        self.assertIn('queries', info['params'])

    def test_signature_shows_the_shape_not_just_the_name(self):
        """参数形状必须给出来：`queries` 是"对象数组"还是"字符串数组"决定成败。

        真实报错：`'foggy city street' is not of type 'object'`——模型知道要传
        queries，但不知道每项得是 {query: "..."}。
        """
        from webpage_agent.tools import core as tool_module
        schema = {'type': 'object', 'required': ['output_dir', 'queries'],
                  'properties': {
                      'output_dir': {'type': 'string'},
                      'queries': {'type': 'array',
                                  'items': {'type': 'object',
                                            'required': ['query'],
                                            'properties': {'query': {'type': 'string'}}}},
                      'clips_per_query': {'type': 'integer'},
                  }}
        signature = tool_module._signature_brief(schema)
        self.assertIn('*output_dir:string', signature)
        self.assertIn('*queries:[{query}]', signature)   # 形状在这里
        self.assertIn('+1可选', signature)                # 可选参数不铺开

    def test_validation_failure_does_not_claim_a_missing_dependency(self):
        """参数写错时别甩一句"装 ffmpeg"——schema 描述里就带 ffmpeg 字样。"""
        from webpage_agent.tools import core as tool_module
        text = ("input validation failed: 'output_dir' is a required property "
                "Failed validating 'required' in schema: {'description': "
                "'Directory where clips are written; requires FFmpeg'}")
        self.assertEqual(tool_module._dependency_note(text), '')
        # 真正的运行时依赖缺失仍然要提示
        self.assertIn('语音模型',
                      tool_module._dependency_note('Piper failed: Unable to find voice'))


if __name__ == '__main__':
    unittest.main()
