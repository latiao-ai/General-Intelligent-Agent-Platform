import os
import json
import time
import base64
import asyncio
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError
from app.schemas import ExecutionPlan, PlanStep
from app.services import clean_public_url
from app.files import save_json
from app.integrations import mcp_adapter
from app import nodes


from contextlib import ExitStack


def patch_all_emits(sink):
    """能力包各自持有 emit 引用，测试里需要一起打桩。"""
    from app.capabilities import answers, export, publishing, web
    stack = ExitStack()
    for module in (answers, export, publishing, web):
        stack.enter_context(patch.object(module, 'emit', sink))
    return stack


class CoreTests(unittest.TestCase):
    def test_reject_private_url(self):
        for url in ['http://127.0.0.1/', 'http://192.168.1.1/',
                    'http://localhost/', 'file:///etc/passwd', 'http://[::1]/']:
            with self.assertRaises(ValueError):
                clean_public_url(url)

class SourceStoreTests(unittest.TestCase):
    """P0：正文落盘，state 里只留元数据。"""

    def test_text_roundtrip_and_metadata(self):
        from app import sources
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sources, 'settings',
                              SimpleNamespace(artifact_dir=Path(folder))):
                doc = sources.metadata('t1', 's1', url='https://a/1', title='甲',
                                       snippet='摘要' * 300, text='正文' * 500)
                self.assertEqual(doc['chars'], 1000)
                self.assertEqual(doc['status'], 'success')
                self.assertNotIn('raw_content', doc)      # 正文不进 state
                self.assertLessEqual(len(doc['snippet']), 400)
                self.assertEqual(len(sources.load_text(doc)), 1000)
                self.assertTrue(sources.has_text(doc))

    def test_missing_text_and_legacy_compat(self):
        from app import sources
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sources, 'settings',
                              SimpleNamespace(artifact_dir=Path(folder))):
                # 没有正文的登记项
                doc = sources.metadata('t1', 's2', url='https://a/2', title='乙')
                self.assertEqual(doc['chars'], 0)
                self.assertFalse(sources.has_text(doc))
                self.assertEqual(sources.load_text(doc), '')
                # 旧数据里还带 raw_content 的也要能读
                legacy = {'source_id': 's9', 'task_id': 't1', 'url': 'https://a/9',
                          'title': '旧', 'raw_content': '旧正文', 'status': 'pending'}
                self.assertEqual(sources.load_text(legacy), '旧正文')
                self.assertTrue(sources.has_text(legacy))


class WorkingSetTests(unittest.IsolatedAsyncioTestCase):
    """P0：检索/成稿都走落盘正文，state 不再背几百 KB。"""

    async def test_search_keeps_text_out_of_state(self):
        from app import sources, tools as tool_module

        async def fake_search(query):
            return {'results': [{'url': 'https://example.com/a', 'title': '甲文',
                                 'content': '摘要', 'raw_content': '正文' * 400}],
                    'usage': {}}

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sources, 'settings',
                              SimpleNamespace(artifact_dir=Path(folder))), \
                    patch.object(tool_module.search_backend, 'search', fake_search), \
                    patch_all_emits(lambda e: None):
                result = await tool_module._web_search(
                    {'query': 'q'}, {'task_id': 't1', 'documents': [],
                                     'errors': [], 'usage': []})
        doc = result['documents'][0]
        self.assertNotIn('raw_content', doc)
        self.assertEqual(doc['chars'], 800)
        self.assertEqual(doc['status'], 'success')
        self.assertEqual(doc['task_id'], 't1')       # 正文在哪个任务目录下

    async def test_fetch_pages_marks_sources_ready(self):
        """fetch_pages 的成功路径必须真的跑一遍。

        能力包拆分时漏了 datetime 导入，单测只覆盖搜索路径所以没发现，
        是实测一次真实任务才暴露的（NameError in fetch_pages）。
        """
        from app import sources, tools as tool_module
        from app.capabilities import web as web_cap

        async def fake_fetch(urls):
            return {'results': [{'url': urls[0], 'raw_content': '抓到的正文'}],
                    'failed_results': [], 'usage': {}}

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sources, 'settings',
                              SimpleNamespace(artifact_dir=Path(folder))), \
                    patch.object(web_cap.search_backend, 'fetch', fake_fetch), \
                    patch_all_emits(lambda e: None):
                doc = sources.metadata('t1', 's1', url='https://example.com/a',
                                       title='甲', text='')
                result = await tool_module._fetch_pages(
                    {'urls': ['https://example.com/a']},
                    {'task_id': 't1', 'user_input': '阅读 https://example.com/a',
                         'documents': [doc], 'errors': [],
                         'usage': []})
        self.assertEqual(result['documents'][0]['status'], 'success')
        self.assertGreater(result['documents'][0]['chars'], 0)

    def test_fetch_pages_is_only_for_urls_in_the_user_input(self):
        """调用时机只有一条：输入里有 URL。

        "为没有正文的搜索结果补抓"是个不存在的场景（检索接口自带正文），
        规则里留着它只会让规划器以为还有第二条路。
        """
        from app.capabilities import web as web_cap
        from app import tools as tool_module
        rules = web_cap.PROMPT_RULES
        self.assertIn('用户输入里出现了 URL', rules)
        self.assertNotIn('补抓', rules)
        self.assertNotIn('补抓', tool_module.BUILTIN_BY_NAME['fetch_pages'].description)

    async def test_fetch_requires_user_supplied_urls_before_network(self):
        from app.capabilities import web as web_cap
        from app.capabilities.base import ToolError

        # 即使搜索已登记了缺正文的 URL，也不能省略参数或把它当用户链接抓取。
        state = {'user_input': '查找王林的资料', 'documents': [
            {'url': 'https://example.com/search-result', 'status': 'pending'}]}
        with patch.object(web_cap.search_backend, 'fetch', new_callable=AsyncMock) as fetch:
            for args in ({}, {'urls': []},
                         {'urls': ['https://example.com/search-result']}):
                with self.subTest(args=args), self.assertRaises(ToolError):
                    await web_cap._fetch_pages(args, state)
            fetch.assert_not_awaited()

    async def test_fetch_only_updates_requested_user_url(self):
        from app import sources
        from app.capabilities import web as web_cap

        target = 'https://example.com/user-page'
        fetch = AsyncMock(return_value={
            'results': [{'url': target, 'raw_content': '用户链接的正文'}],
            'failed_results': [], 'usage': {}})
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sources, 'settings',
                              SimpleNamespace(artifact_dir=Path(folder))), \
                    patch.object(web_cap.search_backend, 'fetch', fetch), \
                    patch_all_emits(lambda e: None):
                unrelated = sources.metadata('t1', 's1',
                    url='https://example.com/search-result', title='搜索结果')
                result = await web_cap._fetch_pages(
                    {'urls': [target]}, {'task_id': 't1',
                        'user_input': f'阅读 {target}', 'documents': [unrelated]})
                fetch.assert_awaited_once_with([target])
                self.assertEqual(result['documents'][0], unrelated)
                self.assertFalse(sources.text_path('t1', 's1').exists())
                self.assertEqual(sources.load_text(result['documents'][1]), '用户链接的正文')

    async def test_fetch_reuses_existing_text_for_user_url(self):
        from app import sources
        from app.capabilities import web as web_cap

        target = 'https://example.com/user-page'
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sources, 'settings',
                              SimpleNamespace(artifact_dir=Path(folder))), \
                    patch.object(web_cap.search_backend, 'fetch', new_callable=AsyncMock) as fetch, \
                    patch_all_emits(lambda e: None):
                doc = sources.metadata('t1', 's1', url=target, title='已有正文', text='正文')
                result = await web_cap._fetch_pages(
                    {'urls': [target]}, {'task_id': 't1',
                        'user_input': f'阅读 {target}', 'documents': [doc]})
                fetch.assert_not_awaited()
                self.assertEqual(result['documents'], [doc])

    def test_select_sources_reads_text_from_disk(self):
        from app import sources, tools as tool_module
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sources, 'settings',
                              SimpleNamespace(artifact_dir=Path(folder))):
                doc = sources.metadata('t1', 's1', url='https://a/1', title='甲',
                                       text='一' * 5000)
                selected, truncated = tool_module._select_sources(
                    {}, [doc])
        self.assertEqual(selected[0]['source_id'], 's1')
        self.assertEqual(len(selected[0]['text']), 3000)   # max_source_chars
        self.assertTrue(truncated)


class MarkdownRenderTests(unittest.TestCase):
    """资料转 HTML 的底座：Markdown 转换。"""

    def test_basic_blocks(self):
        from app import md
        out = md.to_html('# 标题\n\n正文\n\n- a\n- b\n\n1. x\n2. y\n')
        self.assertIn('<h1>标题</h1>', out)
        self.assertIn('<p>正文</p>', out)
        self.assertIn('<ul><li>a</li><li>b</li></ul>', out)
        self.assertIn('<ol><li>x</li><li>y</li></ol>', out)

    def test_inline_and_code_fence(self):
        from app import md
        out = md.to_html('**粗** 与 `code`\n\n```py\nprint(1)\n```')
        self.assertIn('<strong>粗</strong>', out)
        self.assertIn('<code>code</code>', out)
        self.assertIn('language-py', out)
        self.assertIn('print(1)', out)

    def test_handles_backslash_escapes(self):
        """DeepSeek 的 fetch 返回大量反斜杠转义，必须还原成字面字符。"""
        from app import md
        out = md.to_html(r'\# 1\. Overview 与 \*字面星号\*')
        self.assertIn('# 1. Overview', out)
        self.assertIn('*字面星号*', out)
        self.assertNotIn('\\#', out)

    def test_escapes_untrusted_html(self):
        """来源是任意网页，正文必须先转义——否则我们等于在替它执行脚本。"""
        from app import md
        out = md.to_html('<script>alert(1)</script>\n\n[点我](javascript:alert(1))')
        self.assertNotIn('<script>', out)
        self.assertIn('&lt;script&gt;', out)
        # javascript: 伪协议不该被拼成可点链接
        self.assertNotIn('href="javascript', out)


class RuntimeKeyCheckTests(unittest.TestCase):
    """启动自检要提示缺哪个 key，而不是等到第一次检索才 401。"""

    def test_missing_deepseek_key_is_reported(self):
        from app import config as config_module
        from app.config import Settings
        fake = Settings()                       # 字段来自当前 .env
        object.__setattr__(fake, 'deepseek_api_key', '')
        with patch.object(config_module, 'settings', fake):
            with self.assertRaises(RuntimeError) as ctx:
                config_module.check_runtime_keys()
        self.assertIn('DEEPSEEK_API_KEY', str(ctx.exception))


class SearchBackendTests(unittest.IsolatedAsyncioTestCase):
    """检索后端：DeepSeek 官方接口归一化成 tools 使用的形状。"""

    async def test_deepseek_search_normalises_payload(self):
        from app.integrations import search
        captured = {}

        class FakeResponse:
            def raise_for_status(self): pass
            def json(self):
                return {'results': [{
                    'url': 'https://example.com/a', 'title': '甲文',
                    'snippet': '短摘要', 'summary': '长摘要',
                    'content': '正文 markdown', 'site_name': '示例站',
                    'date': '2026-01-01T00:00:00Z', 'rerank_score': 2.4,
                    'authority': 3}]}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured['url'] = url
                captured['json'] = json
                return FakeResponse()

        with patch.object(search, 'httpx', SimpleNamespace(AsyncClient=FakeClient)), \
                patch.object(search, 'settings',
                             SimpleNamespace(deepseek_api_key='sk-test',
                                             deepseek_base_url='https://api.deepseek.com',
                                             max_pages=4)):
            data = await search.search('SQLite WAL')
        self.assertTrue(captured['url'].endswith('/v1/tools/search'))
        self.assertTrue(captured['json']['need_content'])   # 不开这个拿不到正文
        self.assertEqual(captured['json']['count'], 4)
        item = data['results'][0]
        self.assertEqual(item['raw_content'], '正文 markdown')
        self.assertEqual(item['content'], '短摘要')
        self.assertEqual(item['site_name'], '示例站')

    async def test_deepseek_fetch_collects_failures(self):
        from app.integrations import search

        class FakeResponse:
            def __init__(self, ok): self.ok = ok
            def raise_for_status(self):
                if not self.ok: raise RuntimeError('boom')
            def json(self): return {'title': 'T', 'content': '# 正文'}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                return FakeResponse(json['url'].endswith('/ok'))

        with patch.object(search, 'httpx', SimpleNamespace(AsyncClient=FakeClient)), \
                patch.object(search, 'settings',
                             SimpleNamespace(deepseek_api_key='sk-test',
                                             deepseek_base_url='https://api.deepseek.com',
                                             max_pages=4)):
            data = await search.fetch(['https://a/ok', 'https://a/bad'])
        self.assertEqual([r['url'] for r in data['results']], ['https://a/ok'])
        self.assertEqual(len(data['failed_results']), 1)


class SourcesHtmlToolTests(unittest.IsolatedAsyncioTestCase):
    """资料转 HTML：不调模型，把原始资料直接排版成网页。"""

    async def test_renders_page_from_disk_markdown(self):
        from app import sources, tools as tool_module
        with tempfile.TemporaryDirectory() as folder:
            fake = SimpleNamespace(artifact_dir=Path(folder))
            with patch.object(sources, 'settings', fake), \
                    patch.object(tool_module, 'settings', fake), \
                    patch_all_emits(lambda e: None):
                doc = sources.metadata('t1', 's1', url='https://a/1', title='甲文',
                                       text='# 小标题\n\n正文内容')
                result = await tool_module._render_sources_html(
                    {'title': '我的资料'}, {'task_id': 't1', 'documents': [doc]})
                html = Path(result['artifact']['html_path']).read_text('utf-8')
        self.assertEqual(result['status'], 'rendered')
        self.assertEqual(result['sources_page']['count'], 1)
        self.assertEqual(result['artifact']['kind'], 'sources')
        self.assertIn('我的资料', html)
        self.assertIn('<h1>小标题</h1>', html)     # Markdown 被真正转换了
        self.assertIn('https://a/1', html)

    async def test_refuses_without_documents(self):
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        with self.assertRaises(tool_module.ToolError):
            await tool_module._render_sources_html({}, {'documents': []})


class ContextBuilderTests(unittest.TestCase):
    """P1：按 token 预算装配上下文。"""

    def test_estimate_tokens_prefers_conservative(self):
        from app import context
        self.assertGreater(context.estimate_tokens('中文' * 100), 100)
        self.assertGreater(context.estimate_tokens('a' * 100), 0)
        self.assertEqual(context.estimate_tokens(''), 0)

    def test_select_trims_to_budget_but_keeps_one(self):
        from app import context
        history = [{'role': 'user', 'content': '很长的内容' * 800}
                   for _ in range(30)]
        picked = context.select(history, 'plan')
        self.assertLess(len(picked), 30)
        self.assertGreaterEqual(len(picked), 1)
        # 只留一条也不会因为超预算就返回空
        huge = [{'role': 'user', 'content': 'x' * 100000}]
        self.assertEqual(len(context.select(huge, 'plan')), 1)

    def test_memory_text_and_system_injection(self):
        from app import context
        memory = {'summary': '用户在减脂', 'facts': {'目标': '降到15%'}, 'upto': 12}
        block = context.memory_text(memory)
        self.assertIn('会话摘要', block)
        self.assertIn('降到15%', block)
        self.assertEqual(context.memory_text({}), '')
        merged = context.system_with_memory('基础提示', memory)
        self.assertTrue(merged.startswith('基础提示'))
        self.assertIn('会话摘要', merged)
        self.assertEqual(context.system_with_memory('基础提示', {}), '基础提示')


class SessionMemoryTests(unittest.IsolatedAsyncioTestCase):
    """P2：滚动摘要 + 关键事实。"""

    def test_parse_and_render_facts(self):
        from app import memory
        self.assertEqual(memory.parse_facts('目标：减脂\n- 已否决: 节食\n\n'),
                         {'目标': '减脂', '已否决': '节食'})
        self.assertEqual(memory.render_facts({'目标': '减脂'}), '目标：减脂')

    def test_needs_compaction_thresholds(self):
        from app import memory
        few = [{'content': '短'} for _ in range(3)]
        self.assertFalse(memory.needs_compaction(few))
        many = [{'content': '短'} for _ in range(memory.MAX_MESSAGES + 1)]
        self.assertTrue(memory.needs_compaction(many))
        fat = [{'content': '很长的内容' * 400} for _ in range(6)]
        self.assertTrue(memory.needs_compaction(fat))

    async def test_compact_folds_old_messages_into_summary(self):
        from app import memory
        from app.schemas import SessionMemory
        from app.store import Store
        store = Store(':memory:')
        for i in range(memory.MAX_MESSAGES + 4):
            store.append_message('s1', 'user' if i % 2 == 0 else 'assistant',
                                 f'第{i}条消息')

        async def fake_call(schema, system, user, **kw):
            return SessionMemory(summary='压缩后的摘要', facts={'目标': '减脂'}), None

        with patch.object(memory, 'structured_call', fake_call):
            result = await memory.compact(store, 's1')
        self.assertTrue(result['compacted'])
        self.assertEqual(result['folded'], memory.MAX_MESSAGES + 4 - memory.KEEP_RECENT)
        saved = store.memory('s1')
        self.assertEqual(saved['summary'], '压缩后的摘要')
        self.assertEqual(saved['facts'], {'目标': '减脂'})
        # 未摘要部分只剩最近几条，不会越压越少
        self.assertEqual(len(store.messages_after('s1', saved['upto'])),
                         memory.KEEP_RECENT)

    async def test_compact_failure_is_silent(self):
        from app import memory
        from app.store import Store
        store = Store(':memory:')
        for i in range(memory.MAX_MESSAGES + 4):
            store.append_message('s1', 'user', f'第{i}条')

        async def boom(*a, **k):
            raise RuntimeError('模型不可用')

        with patch.object(memory, 'structured_call', boom):
            result = await memory.compact(store, 's1')
        # 压缩是优化，失败不能把任务搞挂
        self.assertFalse(result['compacted'])
        self.assertEqual(store.memory('s1')['summary'], '')


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    """Planner-Executor：计划决定走哪条路，执行器逐步落实。"""

    def test_next_step_follows_the_cursor(self):
        from app import nodes
        plan = {'steps': [{'tool': 'web_search'}, {'tool': 'render_html'}]}
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 0}), 'run_step')
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 1}), 'run_step')
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 2}), 'end')
        self.assertEqual(nodes.next_step({}), 'end')

    def test_final_status_is_generic_and_label_is_derived(self):
        """状态机只存通用状态；answered / local_ready 是**派生**出来的展示标签。"""
        from app import nodes, outcome
        self.assertEqual(nodes.final_status({'answer': 'hi'}), 'succeeded')
        self.assertEqual(nodes.final_status(
            {'deployment': {'status': 'success'}}), 'succeeded')
        # 回执看不懂 ≠ 失败：写请求可能真的成功了
        self.assertEqual(nodes.final_status({'deployment': {'status': 'pending'}}),
                         'needs_reconcile')
        self.assertEqual(nodes.final_status({'artifact': {'html_path': 'x'}}),
                         'succeeded')
        self.assertEqual(nodes.final_status({}), 'failed')
        self.assertEqual(nodes.final_status({'plan': {'kind': 'compensation'}}),
                         'succeeded')
        self.assertEqual(nodes.final_status(
            {'plan': {'kind': 'compensation'}, 'failure': 'boom'}), 'failed')

        self.assertEqual(outcome.label('succeeded', {'answer': 'hi'}), 'answered')
        self.assertEqual(outcome.label('succeeded',
                                       {'artifact': {'html_path': 'x'}}),
                         'local_ready')
        self.assertEqual(outcome.label('succeeded',
                                       {'deployment': {'status': 'success'},
                                        'artifact': {'html_path': 'x'}}),
                         'deployed')
        self.assertEqual(outcome.label('needs_reconcile',
                                       {'deployment': {'status': 'pending'}}),
                         'deploy_needs_check')
        # 没有部署的未核实场景用通用词，别硬套部署的说法
        self.assertEqual(outcome.label('needs_reconcile', {}), 'needs_reconcile')
        self.assertEqual(outcome.label('failed', {}), 'failed')

    def test_valid_steps_reports_unknown_tools(self):
        """未知工具要**回报**而不是静默过滤——计划引用不存在的工具属于规划错误。"""
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        kept, dropped = nodes._valid_steps(
            [{'tool': 'web_search', 'args': {'query': 'q'}},
             {'tool': 'not_a_tool', 'args': {}},
             {'tool': 'render_sources_html', 'args': 'not-a-dict'}],
            tool_module.BUILTIN_TOOLS)
        self.assertEqual([s['tool'] for s in kept], ['web_search', 'render_sources_html'])
        self.assertEqual(kept[1]['args'], {})
        self.assertEqual(dropped, ['not_a_tool'])

    def test_fallback_does_not_guess_business(self):
        """能力裁剪后只剩一条业务链，兜底不再替用户拼它，直接返回空计划。"""
        from app import nodes
        for text in ('整理一份 SQLite 报告', '查询如何降低体脂', '你好'):
            self.assertEqual(nodes.fallback_steps({'user_input': text}), [], text)

    async def test_plan_asks_the_model_and_keeps_its_steps(self):
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        captured = {}

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            captured['user'] = user
            return ExecutionPlan.model_validate({'steps': [
                {'tool': 'web_search', 'args': {'query': 'sqlite'},
                 'why': '需要外部资料'},
                {'tool': 'render_sources_html', 'args': {}, 'why': '产出网页'},
            ], 'summary': '先搜再渲染'}), {'total_tokens': 3}

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            result = await nodes.plan({'user_input': '整理 SQLite 报告', 'usage': []})

        tools_used = [s['tool'] for s in result['plan']['steps']]
        self.assertEqual(tools_used, ['web_search', 'render_sources_html'])
        self.assertEqual(result['step_index'], 0)
        self.assertEqual(result['usage'][-1]['stage'], 'plan')
        # 工具清单必须真的进了提示词，否则模型只能瞎猜
        self.assertIn('web_search', captured['user'])
        self.assertIn('render_sources_html', captured['user'])
        # 必须告诉模型手上还有多少料，否则它会重复检索、或空手去整理
        self.assertIn('已有资料', captured['user'])

    async def test_plan_falls_back_when_the_model_fails(self):
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def boom(*a, **k):
            raise RuntimeError('模型不可用')

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', boom):
            result = await nodes.plan({'user_input': '整理 SQLite 报告', 'usage': []})
        # 规划失败就如实降级：没有可执行步骤，交给 final_status 报失败
        self.assertEqual(result['plan']['steps'], [])


    async def test_run_step_runs_exactly_one_tool_and_advances(self):
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        from app.tools import Tool
        seen = []

        async def runner(args, state):
            seen.append(args)
            return {'answer': '好的', 'status': 'answered'}

        fake = Tool('render_sources_html', '资料转 HTML', 'd', {}, runner)
        plan = {'steps': [{'tool': 'render_sources_html',
                           'args': {'title': 'x'}, 'step_id': 's1'}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'render_sources_html': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertEqual(seen, [{'title': 'x'}])
        self.assertEqual(result['step_index'], 1)
        self.assertEqual(result['step_label'], '资料转 HTML')

    async def test_run_step_aborts_remaining_steps_on_error(self):
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        from app.tools import Tool

        async def boom(args, state):
            raise tool_module.ToolError('搜不到正文')

        fake = Tool('web_search', '检索来源', 'd', {}, boom)
        plan = {'steps': [{'tool': 'web_search', 'args': {'query': 'q'}},
                          {'tool': 'render_html', 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'web_search': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['step_index'], len(plan['steps']))  # 剩余步骤被跳过
        self.assertIn('搜不到正文', result['failure'])
        self.assertEqual(result['errors'][0]['stage'], 'web_search')

    async def test_run_step_skips_unknown_tool_without_failing(self):
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap

        async def no_mcp():
            return []

        plan = {'steps': [{'tool': 'mcp__gone', 'args': {}},
                          {'tool': 'render_html', 'args': {}}]}
        with patch.object(tool_module, 'mcp_tools', no_mcp):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        # 计划里点名的工具消失了（MCP 掉线）：跳过这一步继续，别让整轮失败
        self.assertEqual(result['step_index'], 1)
        self.assertNotEqual(result.get('status'), 'failed')


class ActionLedgerTests(unittest.IsolatedAsyncioTestCase):
    """动作账本：已成功不重发、结果不确定不重试（文档 §8）。"""

    def ledger(self, store):
        from app.ledger import ActionLedger
        return ActionLedger(store, 'run1', 1)

    def test_succeeded_action_is_replayed_not_resent(self):
        from app.store import Store
        store = Store(':memory:')
        book = self.ledger(store)
        first = book.begin(step_id='s1', tool_key='local:publishing:deploy',
                           effect='write', args={'a': 1})
        self.assertIsNone(first['replay'])
        attempt = book.in_flight(first['action_id'])
        book.succeed(first['action_id'], attempt, {'public_url': 'https://x/1'})

        # 同一个步骤、同样的参数再跑一次：必须直接复用回执
        again = book.begin(step_id='s1', tool_key='local:publishing:deploy',
                           effect='write', args={'a': 1})
        self.assertEqual(again['action_id'], first['action_id'])
        self.assertEqual(again['replay'], {'public_url': 'https://x/1'})
        # 只该有一次真实尝试
        self.assertEqual(len(store.attempts_of(first['action_id'])), 1)

    def test_changed_args_is_a_different_action(self):
        from app.store import Store
        store = Store(':memory:')
        book = self.ledger(store)
        a = book.begin(step_id='s1', tool_key='t', effect='write', args={'a': 1})
        b = book.begin(step_id='s1', tool_key='t', effect='write', args={'a': 2})
        self.assertNotEqual(a['action_id'], b['action_id'])

    def test_unknown_result_blocks_blind_retry(self):
        """结果不确定时不能当成失败去重发，也不能当成功复用。"""
        from app.store import Store
        store = Store(':memory:')
        book = self.ledger(store)
        first = book.begin(step_id='s1', tool_key='mcp:publishing:deploy_site',
                           effect='write', args={})
        attempt = book.in_flight(first['action_id'])
        book.mark_unknown(first['action_id'], attempt, 'TimeoutError')

        again = book.begin(step_id='s1', tool_key='mcp:publishing:deploy_site',
                           effect='write', args={})
        self.assertIsNone(again['replay'])          # 没有可复用回执
        self.assertEqual(store.action(first['action_id'])['status'], 'unknown')

    def test_failure_records_error_type(self):
        from app.store import Store
        store = Store(':memory:')
        book = self.ledger(store)
        first = book.begin(step_id='s1', tool_key='t', effect='read', args={})
        attempt = book.in_flight(first['action_id'])
        book.fail(first['action_id'], attempt, 'ValueError')
        self.assertEqual(store.action(first['action_id'])['status'], 'failed')
        self.assertEqual(store.attempts_of(first['action_id'])[0]['error_type'],
                         'ValueError')

    def test_no_ledger_means_noop(self):
        from app import ledger as ledger_module
        self.assertIsNone(ledger_module.current_ledger())


class RunLeaseTests(unittest.TestCase):
    """租约与 fencing：两个 Worker 抢同一个 run，只有一个能领到。"""

    def test_claim_is_exclusive_and_epoch_increments(self):
        from app.store import Store
        store = Store(':memory:')
        store.create_run('run1', 'task1')
        first = store.claim_run('w1', lease_seconds=30)
        self.assertEqual(first['run_id'], 'run1')
        self.assertEqual(first['lease_epoch'], 1)
        self.assertIsNone(store.claim_run('w2'))          # 租约未过期，抢不到
        # 租约过期后另一个 Worker 可以接手，代次递增
        later = store.claim_run('w2', now_ts=10 ** 12)
        self.assertEqual(later['lease_epoch'], 2)
        # 旧 Worker 续租失败，不能再覆盖状态
        self.assertFalse(store.renew_lease('run1', 'w1'))

    def test_jobs_are_deduped(self):
        from app.store import Store
        store = Store(':memory:')
        self.assertTrue(store.enqueue_job('j1', 'summary', 'sess:1'))
        self.assertFalse(store.enqueue_job('j2', 'summary', 'sess:1'))


class ContextBudgetTests(unittest.TestCase):
    """完整上下文预算与截断披露（文档 §11）。"""

    def test_budget_report_lists_every_section(self):
        from app import context
        built = context.build(purpose='final_answer', rules='规则',
                              request='问题', tools='工具', evidence='证据')
        sections = built['report']['sections']
        for key in ('rules', 'request', 'tools', 'evidence', 'memory', 'history'):
            self.assertIn(key, sections)
        self.assertEqual(built['report']['purpose'], 'final_answer')

    def test_history_is_trimmed_and_flagged(self):
        from app import context
        history = [{'role': 'user', 'content': '很长的历史' * 500}
                   for _ in range(8)]
        built = context.build(purpose='plan', rules='x', history=history)
        self.assertLess(len(built['messages']), 8)
        self.assertIn('history', built['report']['truncated'])

    def test_oversized_rules_are_reported_not_silently_cut(self):
        """规则+工具 schema 已超预算时必须报出来，不能悄悄截断关键约束。"""
        from app import context
        built = context.build(purpose='plan', rules='约束' * 3000,
                              tools='工具' * 3000)
        self.assertTrue(built['report']['over_budget'])

    def test_each_purpose_has_its_own_budget(self):
        from app import context
        self.assertGreater(context.PURPOSE_BUDGETS['transform'],
                           context.PURPOSE_BUDGETS['plan'])
        for purpose in context.PURPOSES:
            self.assertIn(purpose, context.PURPOSE_BUDGETS)


class MemoryScopeTests(unittest.TestCase):
    """长期记忆：作用域、版本替代、遗忘墓碑（文档 §12）。"""

    def store(self):
        from app.store import Store
        return Store(':memory:')

    def test_value_update_supersedes_previous(self):
        store = self.store()
        store.remember(scope_type='user', scope_id='default', subject='偏好',
                       predicate='回答', value='简洁')
        store.remember(scope_type='user', scope_id='default', subject='偏好',
                       predicate='回答', value='详细')
        active = store.recall('user', 'default')
        self.assertEqual([(m['value'], m['version']) for m in active],
                         [('详细', 2)])

    def test_scopes_do_not_leak(self):
        store = self.store()
        store.remember(scope_type='user', scope_id='u1', subject='偏好',
                       predicate='回答', value='简洁')
        self.assertEqual(store.recall('user', 'u2'), [])

    def test_forget_blocks_regeneration_from_same_source(self):
        """只删不拦没用：后台任务会把已删事实重新提取回来。"""
        store = self.store()
        store.remember(scope_type='user', scope_id='default', subject='电话',
                       predicate='号码', value='139')
        store.forget(scope_type='user', scope_id='default', subject='电话',
                     predicate='号码')
        self.assertEqual(store.recall('user', 'default'), [])
        self.assertTrue(store.is_forgotten('user', 'default', '电话', '号码'))
        # 墓碑生效：同一事实不能再被写回来
        self.assertIsNone(store.remember(scope_type='user', scope_id='default',
                                         subject='电话', predicate='号码',
                                         value='139'))


class AnswerCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """基础回答能力：简单问题不该被拖去检索（对应用户指出的流程）。"""

    def test_registry_has_the_five_local_capabilities(self):
        """本地的只剩"检索 / 读用户给的 URL / 转 HTML / 两种作答"。

        部署不在里面：它由 MCP 发现得到（身份 mcp:<server>:<tool>），
        见 `DeployGateTests`。本地身份只发给"纯计算/纯回答"的工具。
        """
        from app import tools as tool_module
        self.assertEqual(sorted(tool_module.BUILTIN_BY_NAME),
                         ['answer_directly', 'answer_with_sources',
                          'fetch_pages', 'render_sources_html', 'web_search'])
        keys = {name: tool.tool_key
                for name, tool in tool_module.BUILTIN_BY_NAME.items()}
        self.assertEqual(keys['web_search'], 'local:web:search')
        self.assertEqual(keys['answer_directly'], 'local:answers:direct')
        self.assertEqual(keys['render_sources_html'],
                         'local:export:sources_html')
        self.assertTrue(all(key.startswith('local:')
                            for key in keys.values()))

    async def test_small_talk_skips_planning_but_still_answers(self):
        """闲聊不花规划调用，但**回答仍由模型给出**（不是固定文案）。"""
        from app import nodes
        from app import tools as tool_module

        async def forbidden(*a, **k):
            raise AssertionError('闲聊不该调用模型做规划')

        async def forbidden_catalog():
            raise AssertionError('闲聊不该去发现工具')

        with patch.object(tool_module, 'catalog', forbidden_catalog), \
                patch.object(nodes, 'structured_call', forbidden):
            result = await nodes.plan({'user_input': '你好', 'usage': []})
        self.assertEqual([s['tool'] for s in result['plan']['steps']],
                         ['answer_directly'])
        self.assertEqual(result.get('usage', []), [])

    async def test_answer_directly_never_touches_documents(self):
        from app import tools as tool_module
        from app.capabilities import answers as answers_cap

        async def fake_chat(system, messages, model=None, on_delta=None):
            return 'ACID 是事务的四个特性。'

        with patch.object(answers_cap, 'chat_reply', fake_chat):
            result = await tool_module._answer_directly(
                {'question': '什么是 ACID'},
                {'user_input': '什么是 ACID', 'history': []})
        self.assertEqual(result['status'], 'answered')
        self.assertIn('ACID', result['answer'])

    async def test_answer_with_sources_cites_documents(self):
        from app import sources, tools as tool_module
        from app.capabilities import answers as answers_cap
        with tempfile.TemporaryDirectory() as folder:
            fake = SimpleNamespace(artifact_dir=Path(folder))
            with patch.object(sources, 'settings', fake):
                doc = sources.metadata('t1', 's1', url='https://a/1',
                                       title='甲文', text='正文内容')

                async def fake_chat(system, messages, model=None, on_delta=None):
                    return '结论 [s1]。'

                with patch.object(answers_cap, 'chat_reply', fake_chat), \
                        patch_all_emits(lambda e: None):
                    result = await tool_module._answer_with_sources(
                        {'question': '问题'}, {'documents': [doc], 'usage': []})
        self.assertEqual(result['status'], 'answered')
        self.assertIn('参考来源', result['answer'])
        self.assertIn('https://a/1', result['answer'])

    async def test_answer_with_sources_requires_material(self):
        from app import tools as tool_module
        with self.assertRaises(tool_module.ToolError):
            await tool_module._answer_with_sources(
                {'question': 'q'}, {'documents': [], 'usage': []})


class PlanContractTests(unittest.IsolatedAsyncioTestCase):
    """计划合同：步骤 ID、依赖、输入绑定与结构化校验（文档 §6）。"""

    def test_step_ids_assigned_and_dependencies_validated(self):
        from app import nodes, tools as tm
        steps, issues = nodes.validate_plan([
            {'tool': 'web_search', 'args': {'query': 'x'}},
            {'tool': 'render_sources_html', 'depends_on': ['s1']},
        ], tm.BUILTIN_TOOLS)
        self.assertEqual([s['step_id'] for s in steps], ['s1', 's2'])
        self.assertEqual(issues, [])
        self.assertEqual(steps[0]['tool_key'], 'local:web:search')

    def test_forward_or_missing_dependency_is_an_error(self):
        from app import nodes, tools as tm
        _, issues = nodes.validate_plan([
            {'tool': 'web_search', 'args': {'query': 'x'}, 'step_id': 'a'},
            {'tool': 'render_sources_html', 'depends_on': ['b'], 'step_id': 'c'},
        ], tm.BUILTIN_TOOLS)
        self.assertIn('bad_dependency', [i['code'] for i in issues])

    def test_duplicate_step_id_is_an_error(self):
        from app import nodes, tools as tm
        _, issues = nodes.validate_plan([
            {'tool': 'web_search', 'args': {}, 'step_id': 'a'},
            {'tool': 'render_sources_html', 'step_id': 'a'},
        ], tm.BUILTIN_TOOLS)
        self.assertIn('duplicate_step_id', [i['code'] for i in issues])

    def test_input_binding_resolves_previous_output(self):
        from app import nodes
        args = nodes.resolve_inputs(
            {'args': {'artifact_id': {'ref': {'step_id': 's1',
                                              'pointer': '/artifact/id'}},
                      'title': {'literal': '活动页'},
                      'plain': 'as-is'}},
            {'s1': {'artifact': {'id': 'art_1'}}})
        self.assertEqual(args, {'artifact_id': 'art_1', 'title': '活动页',
                                'plain': 'as-is'})

    def test_missing_binding_resolves_to_none(self):
        from app import nodes
        args = nodes.resolve_inputs(
            {'args': {'x': {'ref': {'step_id': 'nope', 'pointer': '/a'}}}}, {})
        self.assertIsNone(args['x'])

    async def test_run_step_blocks_when_dependency_not_succeeded(self):
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        from app.tools import Tool

        async def should_not_run(args, state):
            raise AssertionError('依赖未完成时不应执行')

        fake = Tool('render_sources_html', '资料转 HTML', 'd', {}, should_not_run,
                    tool_key='local:reports:render', effect='write')
        plan = {'steps': [{'tool': 'render_sources_html', 'step_id': 's2',
                           'depends_on': ['s1'], 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'render_sources_html': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q',
                 'step_outputs': {'s1': {'__status__': 'failed'}}})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('依赖', result['failure'])

    async def test_run_step_records_output_for_downstream_binding(self):
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        from app.tools import Tool

        async def run(args, state):
            return {'artifact': {'id': 'art_9'}}

        fake = Tool('render_sources_html', '资料转 HTML', 'd', {}, run,
                    tool_key='local:reports:render', effect='write')
        plan = {'steps': [{'tool': 'render_sources_html', 'step_id': 's1', 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'render_sources_html': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertEqual(result['step_outputs']['s1']['artifact']['id'], 'art_9')
        self.assertEqual(result['step_outputs']['s1']['__status__'], 'succeeded')


class DeployGateTests(unittest.IsolatedAsyncioTestCase):
    """部署：工具身份来自 MCP，闸门与回执归一份在能力包适配器里。

    以前的 `deploy_site` 是内置工具、自己直连 MCP：清单里同时出现内置与
    `mcp__deploy_site` 两份，账本记成 `local:publishing:deploy`（看不出依赖哪个
    服务端），`mcp_calls` 里也没有它。现在它就是一个带适配器的 MCP 工具，
    这组用例把这四条一起钉住。
    """

    @staticmethod
    def server():
        from app.integrations import mcp_servers
        return mcp_servers.Server(id='publishing', url='https://a.example/mcp',
                                  deploy_tool='deploy_site', default=True)

    def tool(self, effect='write'):
        from app import tools as tool_module
        return tool_module._deploy_tool(
            self.server(), {'name': 'deploy_site', 'effect_type': effect})

    async def test_tool_takes_its_identity_from_mcp(self):
        from app import tools as tool_module
        tool = self.tool(effect='destructive')
        self.assertEqual(tool.name, 'deploy_site')
        self.assertEqual(tool.tool_key, 'mcp:publishing:deploy_site')
        self.assertEqual(tool.effect, 'destructive')      # 用服务端的 annotations
        self.assertEqual(tool.capability, 'publishing')
        self.assertEqual(tool.server_id, 'publishing')
        self.assertIn('【发布】', tool.prompt_rules)

    async def test_requires_an_artifact_before_touching_the_server(self):
        from app import tools as tool_module
        call = AsyncMock()
        with patch.object(tool_module.mcp_adapter, 'call_tool', call), \
                patch_all_emits(lambda e: None):
            with self.assertRaises(tool_module.ToolError) as ctx:
                await self.tool().run({}, {})
        self.assertIn('还没有 HTML 产物', ctx.exception.safe_message)
        call.assert_not_awaited()

    async def test_requires_global_enable(self):
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
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
        from app import tools as tool_module
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
        from app import tools as tool_module
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
        from app import tools as tool_module
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
        from app.capabilities import publishing as publishing_cap
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
        from app import tools as tool_module

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
        from app import tools as tool_module
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
                              SimpleNamespace(deploy_enabled=False)):
                tool_module._MCP_CACHE.update(at=0.0, tools=[], signature=())
                off = await tool_module.catalog()
        self.assertNotIn('deploy_site', [t.name for t in off])
        self.assertIn('mcp__list_sites', [t.name for t in off])

    async def test_placeholder_keeps_the_tool_visible_when_server_is_down(self):
        """服务端掉线时留占位工具：报错要清楚，而不是当作未知工具静默跳过。"""
        from app import tools as tool_module
        from app.integrations import mcp_servers
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


class StoreTests(unittest.TestCase):
    """SQLite 持久化层。"""

    @staticmethod
    def store():
        from app.store import Store
        return Store(':memory:')

    def test_messages_roundtrip(self):
        store = self.store()
        store.append_message('sess1', 'user', '你好')
        store.append_message('sess1', 'assistant', '收到')
        store.append_message('sess2', 'user', '另一个会话')
        self.assertEqual([m['role'] for m in store.messages('sess1')],
                         ['user', 'assistant'])
        self.assertEqual(len(store.messages('sess2')), 1)

    def test_list_sessions_orders_by_recent_and_uses_first_user_message(self):
        store = self.store()
        store.append_message('s1', 'user', '帮我查一下 SQLite 的适用场景和限制以及和 Postgres 的区别')
        store.append_message('s1', 'assistant', '好的')
        store.append_message('s2', 'user', '你好')
        sessions = store.list_sessions()
        self.assertEqual([s['session_id'] for s in sessions], ['s2', 's1'])
        by_id = {s['session_id']: s for s in sessions}
        self.assertTrue(by_id['s1']['title'].startswith('帮我查一下'))
        self.assertTrue(by_id['s1']['title'].endswith('…'))
        self.assertEqual(len(by_id['s1']['title']), 25)   # 24 字 + 省略号
        self.assertEqual(by_id['s1']['message_count'], 2)
        self.assertEqual(by_id['s2']['title'], '你好')
        # 只有助手消息（没有用户消息）时回退到「新会话」。
        store2 = self.store()
        store2.append_message('sess-empty', 'assistant', '请问有什么可以帮您？')
        self.assertEqual(store2.list_sessions()[0]['title'], '新会话')

    def test_task_lifecycle_with_json_state(self):
        store = self.store()
        store.create_task('t1', 'sess1', '做点事')
        self.assertEqual(store.task('t1')['status'], 'running')
        store.update_task('t1', status='local_ready', artifact_id='a1',
                          state={'timings': [{'stage': 'parse_request'}]})
        data = store.task('t1')
        self.assertEqual(data['status'], 'local_ready')
        self.assertEqual(data['artifact_id'], 'a1')
        self.assertEqual(data['state']['timings'][0]['stage'], 'parse_request')
        self.assertEqual(len(store.tasks_of('sess1')), 1)

    def test_event_seq_is_monotonic_and_isolated_per_task(self):
        store = self.store()
        first = [store.append_event('t1', {'type': 'action', 'label': f'e{i}'})
                 for i in range(3)]
        self.assertEqual(first, [1, 2, 3])
        # 序号按任务独立计数，订阅去重依赖这个性质。
        self.assertEqual(store.append_event('t2', {'type': 'action'}), 1)
        stored = store.events('t1')
        self.assertEqual([seq for seq, _ in stored], [1, 2, 3])
        self.assertEqual(stored[1][1]['label'], 'e1')

    def test_orphan_running_task_marked_interrupted_on_reopen(self):
        from app.store import Store
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'agent.db'
            first = Store(path)
            first.create_task('t1', 's1', '中断前的任务')
            first.close()
            # 重新打开：遗留的 running 任务必须被标记，否则前端会一直等下去。
            second = Store(path)
            self.assertEqual(second.task('t1')['status'], 'interrupted')
            self.assertEqual(second.interrupted, 1)
            second.close()

    def test_missing_task_returns_none(self):
        self.assertIsNone(self.store().task('nope'))


class HubTests(unittest.IsolatedAsyncioTestCase):
    """任务中枢：事件广播、多轮历史、取消、重启恢复。"""

    @staticmethod
    def hub():
        from app.api_hub import Hub
        from app.store import Store
        return Hub(Store(':memory:'))

    @staticmethod
    def events_of(record):
        return [event for _, event in record.store.events(record.task_id)]

    @staticmethod
    def stub(captured=None, delay=0):
        async def fake_run_task(text, progress=None, on_event=None,
                                history=None, on_start=None,
                                prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            if captured is not None:
                captured['history'] = history
            if on_start:
                on_start('artifact123')
            if on_event:
                on_event({'type': 'action', 'icon': '🌐', 'label': '已搜索网页',
                          'detail': '测试'})
            if delay:
                await asyncio.sleep(delay)
            return {'task_id': 'artifact123', 'status': 'succeeded',
                    'artifact': {'html_path': '/tmp/artifact123/sources.html',
                                 'kind': 'sources'},
                    'timings': []}
        return fake_run_task

    async def test_start_broadcasts_events_and_records_messages(self):
        import app.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub()):
            record = hub.start('测试需求')
            await asyncio.wait_for(record._task, timeout=10)
        self.assertEqual(record.status, 'succeeded')            # 通用状态
        self.assertEqual(record.summary()['status'], 'local_ready')  # 展示标签
        self.assertEqual(record.summary()['run_status'], 'succeeded')
        self.assertEqual(record.artifact_id, 'artifact123')
        labels = [e.get('label') for e in self.events_of(record)]
        self.assertIn('已搜索网页', labels)
        # 会话里要留下 user + assistant 两条，下一轮才能还原指代型要求。
        self.assertEqual([m['role'] for m in hub.history(record.session_id)],
                         ['user', 'assistant'])
        # 状态也要落库，重启后才能查询。
        # 落库的是通用状态：状态机词汇不随业务能力增长
        self.assertEqual(hub.store.task(record.task_id)['status'], 'succeeded')

    async def test_late_subscriber_still_gets_history(self):
        import app.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub()):
            record = hub.start('测试需求')
            await asyncio.wait_for(record._task, timeout=10)
            # 任务结束后才订阅，仍要能拿到完整过程（SSE 端点依赖此语义）。
            _, history = record.subscribe()
        self.assertTrue(any(e.get('label') == '已搜索网页' for _, e in history))
        self.assertTrue(record.done)

    async def test_record_restored_from_db_after_restart(self):
        """模拟进程重启：新 Hub 打开同一个库，应能查到旧任务并补发事件。"""
        import app.api_hub as hub_module
        from app.api_hub import Hub
        from app.store import Store
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'agent.db'
            first = Hub(Store(path))
            with patch.object(hub_module, 'run_task', self.stub()):
                record = first.start('老任务')
                await asyncio.wait_for(record._task, timeout=10)
            first.store.close()

            second = Hub(Store(path))
            restored = second.get(record.task_id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.status, 'succeeded')
            self.assertEqual(restored.summary()['status'], 'local_ready')
            self.assertFalse(restored.live)
            self.assertFalse(second.cancel(record.task_id))    # 重启后无法取消
            _, history = restored.subscribe()
            self.assertTrue(any(e.get('label') == '已搜索网页' for _, e in history))
            second.store.close()

    async def test_multi_turn_history_is_passed(self):
        import app.api_hub as hub_module
        hub = self.hub()
        captured = {}
        with patch.object(hub_module, 'run_task', self.stub(captured)):
            first = hub.start('找还珠格格资料')
            await asyncio.wait_for(first._task, timeout=10)
            second = hub.start('我只要第一部第三集', first.session_id)
            await asyncio.wait_for(second._task, timeout=10)
        self.assertEqual(captured['history'][0]['content'], '找还珠格格资料')
        self.assertEqual(captured['history'][1]['role'], 'assistant')

    async def test_cancel_marks_cancelled_and_releases_subscribers(self):
        import app.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=30)):
            record = hub.start('慢任务')
            queue, _ = record.subscribe()
            await asyncio.sleep(0.3)
            self.assertTrue(hub.cancel(record.task_id))
            with self.assertRaises(asyncio.CancelledError):
                await record._task
        self.assertEqual(record.status, 'cancelled')
        self.assertEqual(hub.store.task(record.task_id)['status'], 'cancelled')
        self.assertTrue(any(e.get('status') == 'cancelled'
                            for e in self.events_of(record)))
        # 取消后订阅者必须被唤醒，否则 SSE 连接会一直挂着。
        received = []
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=2)
            if item is hub_module.CLOSE:
                break
            received.append(item)
        self.assertTrue(received)

    async def test_instruction_stage_is_first_and_closes_on_search(self):
        """「收到指令 → 开始执行」应排在阶段表第一行，并在真正开始干活时补齐耗时。"""
        import app.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            on_event({'type': 'stage_start', 'stage': 'plan'})
            on_event({'type': 'stage_end', 'stage': 'plan',
                      'seconds': 1.0, 'status': 'planned'})
            # 这一条到达时，"收到指令"应当被补齐
            on_event({'type': 'stage_start', 'stage': 'web_search'})
            return {'task_id': 'a1', 'status': 'succeeded',
                    'artifact': {'html_path': '/tmp/a1/sources.html',
                                 'kind': 'sources'},
                    'timings': []}

        with patch.object(hub_module, 'run_task', fake):
            record = hub.start('测试')
            await asyncio.wait_for(record._task, timeout=10)

        events = self.events_of(record)
        starts = [e for e in events if e['type'] == 'stage_start']
        self.assertEqual(starts[0]['stage'], 'instruction')
        self.assertEqual(starts[0]['label'], '收到指令 → 开始执行')
        ends = [e for e in events
                if e['type'] == 'stage_end' and e['stage'] == 'instruction']
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]['status'], '规划完成')
        search_at = next(i for i, e in enumerate(events)
                         if e['type'] == 'stage_start' and e['stage'] == 'web_search')
        self.assertLess(events.index(ends[0]), search_at)
        # 也要进持久化的 timings，state.json 与 CLI 汇总表才能看到
        self.assertEqual(record.state['timings'][0]['stage'], 'instruction')

    async def test_instruction_stage_closed_on_cancel(self):
        """中断时也要补齐，否则那一行会一直停在"进行中"。"""
        import app.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=30)):
            record = hub.start('慢任务')
            await asyncio.sleep(0.3)
            hub.cancel(record.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await record._task
        ends = [e for e in self.events_of(record)
                if e['type'] == 'stage_end' and e['stage'] == 'instruction']
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]['status'], '已中断')

    async def test_cancel_leaves_assistant_marker_in_history(self):
        """中断也要留下助手回复，否则下一轮上下文是两条连续 user 消息。"""
        import app.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=30)):
            record = hub.start('慢任务')
            await asyncio.sleep(0.3)
            hub.cancel(record.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await record._task
        self.assertEqual([m['role'] for m in hub.history(record.session_id)],
                         ['user', 'assistant'])

    async def test_send_while_running_starts_new_turn_same_session(self):
        """运行中直接发第二条消息：同会话开新一轮，历史照常带上。"""
        import app.api_hub as hub_module
        hub = self.hub()
        captured = {}
        with patch.object(hub_module, 'run_task', self.stub(delay=30)):
            first = hub.start('第一轮要求')
            await asyncio.sleep(0.3)
            # 模拟前端"打断并发送"：取消旧任务后立刻用同一会话开新一轮
            hub.cancel(first.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await first._task
        with patch.object(hub_module, 'run_task', self.stub(captured)):
            second = hub.start('第二轮要求', first.session_id)
            await asyncio.wait_for(second._task, timeout=10)
        contents = [m['content'] for m in captured['history']]
        self.assertEqual(contents[0], '第一轮要求')
        self.assertIn('（上一轮被用户中断）', contents)

    async def test_cancel_closes_in_flight_stage(self):
        """取消时必须兜底关闭未完成的阶段。

        节点自己发的 stage_end 会丢：它写进 LangGraph 的流队列，而 run_task 的
        消费循环此刻已被取消，没人来取。所以中枢必须直接补发，否则前端那一行
        会永远停在"进行中"、耗时一路涨。
        """
        import app.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            on_event({'type': 'stage_start', 'stage': 'plan'})
            on_event({'type': 'stage_end', 'stage': 'plan',
                      'seconds': 1.0, 'status': 'planned'})
            on_event({'type': 'stage_start', 'stage': 'discover_sources'})
            on_event({'type': 'stage_start', 'stage': 'compose_report'})
            await asyncio.sleep(30)          # 卡在整理报告
            return {}

        with patch.object(hub_module, 'run_task', fake):
            record = hub.start('慢任务')
            await asyncio.sleep(0.3)
            hub.cancel(record.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await record._task

        ends = {e['stage']: e['status'] for e in self.events_of(record)
                if e['type'] == 'stage_end'}
        self.assertEqual(ends.get('compose_report'), '已中断')
        self.assertIsNone(record.open_stage)

    async def test_cancel_before_search_closes_instruction(self):
        """在检索之前就被打断：合成阶段也要补上结束事件。"""
        import app.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            on_event({'type': 'stage_start', 'stage': 'plan'})
            await asyncio.sleep(30)
            return {}

        with patch.object(hub_module, 'run_task', fake):
            record = hub.start('慢任务')
            await asyncio.sleep(0.3)
            hub.cancel(record.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await record._task

        ends = {e['stage']: e['status'] for e in self.events_of(record)
                if e['type'] == 'stage_end'}
        self.assertEqual(ends.get('instruction'), '已中断')
        # 卡住的那一步（规划）也要被补上结束事件
        self.assertEqual(ends.get('plan'), '已中断')

    async def test_failed_task_closes_in_flight_stage(self):
        import app.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            # 用规划阶段做"还没开始干活"的代表：它不在 FIRST_WORK_STAGES 里，
            # 因此指令阶段仍是打开的，失败时应当由失败路径补上结束事件。
            on_event({'type': 'stage_start', 'stage': 'plan'})
            raise RuntimeError('boom')

        with patch.object(hub_module, 'run_task', fake):
            record = hub.start('坏任务')
            await asyncio.wait_for(record._task, timeout=10)
        ends = {e['stage']: e['status'] for e in self.events_of(record)
                if e['type'] == 'stage_end'}
        self.assertEqual(ends.get('plan'), '已失败')
        self.assertEqual(ends.get('instruction'), '已失败')

    async def test_stop_command_cancels_instead_of_starting_a_task(self):
        """说"暂停"要停流程，而不是拿"暂停"去检索。"""
        import app.api_hub as hub_module
        hub = self.hub()
        started = {'n': 0}

        async def slow(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            started['n'] += 1
            await asyncio.sleep(30)
            return {}

        with patch.object(hub_module, 'run_task', slow):
            first = hub.start('整理一份 SQLite 报告')
            await asyncio.sleep(0.2)
            record = hub.start('暂停', first.session_id)
            with self.assertRaises(asyncio.CancelledError):
                await first._task

        self.assertEqual(started['n'], 1, '暂停不应该再启动一次模型流程')
        self.assertEqual(record.status, 'cancelled')
        details = [e.get('detail') for e in self.events_of(record)
                   if e.get('type') == 'status']
        self.assertTrue(any('已停止当前任务' in (d or '') for d in details))

    async def test_stop_command_with_nothing_running(self):
        import app.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub()):
            record = hub.start('暂停')
        self.assertEqual(record.status, 'cancelled')
        details = [e.get('detail') for e in self.events_of(record)
                   if e.get('type') == 'status']
        self.assertTrue(any('没有正在执行的任务' in (d or '') for d in details))

    async def test_prior_context_carries_artifact_and_documents(self):
        """多轮承接：产物和正文都要能从上一轮交过来。"""
        hub = self.hub()
        hub.store.create_task('t1', 'sess', '第一轮')
        hub.store.update_task('t1', status='local_ready', state={
            'artifact': {'html_path': '/x/index.html'},
            'documents': [{'source_id': 's1', 'url': 'https://a/1',
                           'status': 'success'},
                          {'source_id': 's2', 'url': 'https://b/2',
                           'status': 'failed'}],
        })
        ctx = hub.prior_context('sess')
        self.assertEqual(ctx['artifact']['html_path'], '/x/index.html')
        # 只接成功的来源：报告要给它们生成参考资料，失败的没有正文
        self.assertEqual([d['source_id'] for d in ctx['documents']], ['s1'])
        self.assertEqual(hub.prior_context('nope'), {})

    async def test_cancel_done_task_is_noop(self):
        import app.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub()):
            record = hub.start('快任务')
            await asyncio.wait_for(record._task, timeout=10)
        self.assertFalse(hub.cancel(record.task_id))


class RunLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """run 生命周期与断点续跑（文档 §7、§8）。"""

    def hub(self):
        from app.api_hub import Hub
        from app.store import Store
        return Hub(Store(':memory:'))

    async def test_finishing_a_run_persists_plan_and_steps(self):
        import app.api_hub as hub_module
        hub = self.hub()
        # 真实链路里 validate_plan 会给每步补 tool_key；这里按同一形状构造
        plan = {'steps': [{'tool': 'web_search', 'step_id': 's1', 'args': {},
                           'tool_key': 'local:web:search'},
                          {'tool': 'render_html', 'step_id': 's2', 'args': {},
                           'tool_key': 'local:reports:render'}],
                'summary': '先搜后渲染'}

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            return {'task_id': 'a1', 'status': 'local_ready', 'timings': [],
                    'deployment': {}, 'plan': plan,
                    'artifact': {'kind': 'sources', 'html_path': '/tmp/x.html'}}

        with patch.object(hub_module, 'run_task', fake):
            record = hub.start('做点事')
            await asyncio.wait_for(record._task, timeout=10)

        run = hub.store.run(record.run_id)
        self.assertEqual(run['status'], 'succeeded')
        saved = hub.store.plan(record.run_id)
        self.assertEqual([s['step_id'] for s in saved['steps']], ['s1', 's2'])
        self.assertEqual([s['tool_key'] for s in hub.store.steps_of(record.run_id)],
                         ['local:web:search', 'local:reports:render'])

    async def test_resume_reuses_saved_plan(self):
        """续跑沿用已存计划，不重新规划（§7：已完成步骤不可改写）。"""
        import app.api_hub as hub_module
        hub = self.hub()
        seen = {}

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            seen['resume_plan'] = resume_plan
            return {'task_id': 'a1', 'status': 'local_ready', 'timings': [],
                    'deployment': {},
                    'plan': {'steps': [{'tool': 'render_html', 'step_id': 's1',
                                        'args': {}}], 'summary': 'x'}}

        with patch.object(hub_module, 'run_task', fake):
            record = hub.start('第一次')
            await asyncio.wait_for(record._task, timeout=10)
            resumed = hub.resume(record.run_id)
            self.assertIsNotNone(resumed)
            await asyncio.wait_for(resumed._task, timeout=10)
        self.assertEqual([s['step_id'] for s in seen['resume_plan']['steps']],
                         ['s1'])

    async def test_reconcile_marks_inflight_actions_unknown(self):
        """重启时不能说"一律中断"——已发出未回执的调用必须保持 unknown。"""
        hub = self.hub()
        hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        hub.store.create_run('r1', 't1', status='running')
        hub.store.create_action(
            'a1', run_id='r1', plan_version=1, step_id='s1',
            tool_key='mcp:publishing:deploy_site', args_hash='h',
            idempotency_key='k1', effect='write')
        hub.store.update_action('a1', status='in_flight')
        # 另一条只到 prepared（请求根本没发出去）：可以安全重试，不该标 unknown
        hub.store.create_action(
            'a2', run_id='r1', plan_version=1, step_id='s2',
            tool_key='mcp:publishing:update_site', args_hash='h2',
            idempotency_key='k2', effect='write')

        stats = hub.reconcile()
        self.assertEqual(stats['runs'], 1)
        self.assertEqual(stats['unknown_actions'], 1)
        self.assertEqual(hub.store.action('a1')['status'], 'unknown')
        self.assertEqual(hub.store.action('a2')['status'], 'prepared')  # 可重试
        self.assertEqual(hub.store.run('r1')['status'], 'reconciling')

    async def test_unknown_action_blocks_instead_of_resending(self):
        """已发出但结果未定的动作，续跑时必须停下来而不是再发一次。"""
        from app import nodes
        from app import tools as tool_module
        from app.ledger import ActionLedger, use_ledger
        from app.store import Store
        from app.tools import Tool

        store = Store(':memory:')
        book = ActionLedger(store, 'run1', 1)
        first = book.begin(step_id='s1', tool_key='local:publishing:deploy',
                           effect='write', args={})
        attempt = book.in_flight(first['action_id'])
        book.mark_unknown(first['action_id'], attempt, 'TimeoutError')

        async def should_not_run(args, state):
            raise AssertionError('结果未定的动作不能重发')

        fake = Tool('deploy_site', '部署站点', 'd', {}, should_not_run,
                    tool_key='local:publishing:deploy', effect='write')
        plan = {'steps': [{'tool': 'deploy_site', 'step_id': 's1', 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'deploy_site': fake}):
            with use_ledger(book):
                result = await nodes.run_step(
                    {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertIn('结果未确定', result['failure'])

    def test_pause_only_affects_active_runs(self):
        hub = self.hub()
        hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        hub.store.create_run('r1', 't1', status='running')
        self.assertTrue(hub.pause('r1'))
        self.assertEqual(hub.store.run('r1')['pause_requested'], 1)
        hub.store.update_run('r1', status='succeeded')
        self.assertFalse(hub.pause('r1'))


class PolicyTests(unittest.TestCase):
    """身份与权限缝：单用户恒通过，跨用户必须被挡住。"""

    def test_same_owner_passes(self):
        from app import policies
        principal = policies.Principal(tenant_id='t1', user_id='u1')
        resource = {'tenant_id': 't1', 'owner_user_id': 'u1'}
        self.assertEqual(policies.ensure_owner(resource, principal, '运行'),
                         resource)

    def test_cross_tenant_is_hidden_as_not_found(self):
        from app import policies
        from fastapi import HTTPException
        principal = policies.Principal(tenant_id='t1', user_id='u1')
        other = {'tenant_id': 't2', 'owner_user_id': 'u1'}
        with self.assertRaises(HTTPException) as ctx:
            policies.ensure_owner(other, principal, '运行')
        # 不泄漏"存在但无权"，统一按不存在处理
        self.assertEqual(ctx.exception.status_code, 404)

    def test_anonymous_default_principal_is_permissive(self):
        from app import policies
        principal = policies.Principal()
        self.assertTrue(principal.anonymous)
        resource = {'tenant_id': 'default', 'owner_user_id': 'default'}
        self.assertEqual(policies.ensure_owner(resource, principal), resource)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    """独立 Worker：领取租约、消费 outbox。"""

    def hub(self):
        from app.api_hub import Hub
        from app.store import Store
        return Hub(Store(':memory:'))

    async def test_worker_claims_and_reports_nothing_to_do(self):
        from scripts.run_worker import run_once
        hub = self.hub()
        self.assertFalse(await run_once(hub, 'w1'))

    async def test_jobs_drain_is_idempotent(self):
        from scripts.run_worker import drain_jobs
        hub = self.hub()
        hub.store.enqueue_job('j1', 'session_summary', 's1:r1',
                              {'session_id': 's1'})
        hub.store.enqueue_job('j2', 'session_summary', 's1:r1',
                              {'session_id': 's1'})
        done = await drain_jobs(hub, 'w1')
        self.assertEqual(done, 1)       # 去重键相同，只有一个作业
        self.assertEqual(await drain_jobs(hub, 'w1'), 0)


PG_DSN = os.getenv('PG_TEST_DSN', '')


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 一致性测试')
class PostgresParityTests(unittest.TestCase):
    """在**真实 PostgreSQL** 上验证仓储实现。

    ⚠️ 每个用例开头都会 TRUNCATE 全部业务表——**PG_TEST_DSN 必须指向一次性
    测试库**（如 agent_platform_test），绝不能指向生产库。曾经因为拿生产库
    跑这套用例，把刚迁好的数据清空过一次。
    """

    def setUp(self):
        from app.pg_store import PostgresStore
        if 'test' not in PG_DSN:
            raise unittest.SkipTest('PG_TEST_DSN 必须指向测试库（库名含 test）')
        self.store = PostgresStore(PG_DSN)
        tables = ('memory_tombstones', 'memories', 'background_jobs', 'artifacts',
                  'action_attempts', 'actions', 'steps', 'plans', 'runs',
                  'tasks_meta', 'events', 'tasks', 'session_memory',
                  'session_summaries', 'messages', 'sessions')
        with self.store._conn.cursor() as cur:
            for table in tables:
                cur.execute(f'TRUNCATE TABLE {table} CASCADE')
        self.store._conn.commit()

    def tearDown(self):
        self.store.close()

    def test_messages_and_sessions(self):
        store = self.store
        store.append_message('s1', 'user', '帮我查一下 SQLite 的适用场景和限制以及和 Postgres 的区别')
        store.append_message('s1', 'assistant', '好的')
        store.append_message('s2', 'user', '你好')
        self.assertEqual([m['role'] for m in store.messages('s1')],
                         ['user', 'assistant'])
        sessions = store.list_sessions()
        self.assertEqual([s['session_id'] for s in sessions], ['s2', 's1'])
        self.assertEqual(sessions[1]['message_count'], 2)
        self.assertTrue(sessions[1]['title'].endswith('…'))
        self.assertEqual(len(store.messages_after('s1', 0)), 2)

    def test_memory_and_tombstone(self):
        store = self.store
        store.set_memory('s1', summary='摘要', facts={'目标': '减脂'}, upto=5)
        self.assertEqual(store.memory('s1')['facts'], {'目标': '减脂'})
        store.clear_memory('s1')
        self.assertEqual(store.memory('s1')['summary'], '')

        store.remember(scope_type='user', scope_id='default', subject='偏好',
                       predicate='回答', value='简洁')
        store.remember(scope_type='user', scope_id='default', subject='偏好',
                       predicate='回答', value='详细')
        self.assertEqual([(m['value'], m['version'])
                          for m in store.recall('user', 'default')], [('详细', 2)])
        store.forget(scope_type='user', scope_id='default', subject='偏好',
                     predicate='回答')
        self.assertEqual(store.recall('user', 'default'), [])
        self.assertIsNone(store.remember(scope_type='user', scope_id='default',
                                         subject='偏好', predicate='回答',
                                         value='又回来'))

    def test_runs_leases_and_actions(self):
        store = self.store
        store.create_task('t1', 's1', '做点事')
        store.ensure_task_meta('t1', session_id='s1', goal='做点事')
        store.create_run('r1', 't1')
        first = store.claim_run('w1', lease_seconds=30)
        self.assertEqual(first['lease_epoch'], 1)
        self.assertIsNone(store.claim_run('w2'))
        self.assertEqual(store.claim_run('w2', now_ts=10 ** 12)['lease_epoch'], 2)
        self.assertFalse(store.renew_lease('r1', 'w1'))

        plan = {'steps': [{'step_id': 's1', 'tool': 'web_search',
                           'tool_key': 'local:web:search', 'args': {}}]}
        store.save_plan('r1', 1, goal='g', steps=plan['steps'], reason='why')
        store.save_steps('r1', 1, plan['steps'])
        self.assertEqual(store.plan('r1')['steps'][0]['step_id'], 's1')
        self.assertEqual(len(store.steps_of('r1')), 1)

        action = store.create_action('a1', run_id='r1', plan_version=1,
                                     step_id='s1', tool_key='local:web:search',
                                     args_hash='h', idempotency_key='k1')
        self.assertEqual(store.find_action('k1')['action_id'], 'a1')
        store.update_action('a1', status='in_flight')
        store.add_attempt('att1', 'a1')
        store.finish_attempt('att1', error_type='None')
        self.assertEqual(len(store.attempts_of('a1')), 1)
        self.assertEqual(len(store.actions_of('r1')), 1)

    def test_reconcile_and_jobs(self):
        store = self.store
        store.create_task('t1', 's1', 'g')
        store.ensure_task_meta('t1', session_id='s1', goal='g')
        store.create_run('r1', 't1', status='running')
        store.create_action('a1', run_id='r1', plan_version=1, step_id='s1',
                            tool_key='t', args_hash='h', idempotency_key='k1')
        store.update_action('a1', status='in_flight')
        stats = store.reconcile_orphan_runs()
        self.assertEqual(stats['unknown_actions'], 1)
        self.assertEqual(store.action('a1')['status'], 'unknown')
        self.assertEqual(store.run('r1')['status'], 'reconciling')

        self.assertTrue(store.enqueue_job('j1', 'session_summary', 's1:r1',
                                          {'session_id': 's1'}))
        self.assertFalse(store.enqueue_job('j2', 'session_summary', 's1:r1',
                                           {'session_id': 's1'}))
        job = store.claim_job('w1')
        self.assertEqual(job['kind'], 'session_summary')
        store.finish_job(job['job_id'])
        self.assertEqual(store.job(job['job_id'])['status'], 'done')

    def test_artifacts_and_events(self):
        store = self.store
        store.create_task('t1', 's1', 'g')
        store.add_artifact('art1', run_id='r1', kind='sources',
                           storage_key='/tmp/x.html', size=245823, sha256='abc')
        meta = store.artifact('art1')
        self.assertEqual(meta['kind'], 'sources')
        self.assertEqual(store.append_event('t1', {'type': 'action'}), 1)
        self.assertEqual(store.append_event('t1', {'type': 'action'}), 2)
        self.assertEqual([s for s, _ in store.events('t1')], [1, 2])

    def test_task_roundtrip(self):
        store = self.store
        store.create_task('t1', 's1', '消息')
        self.assertEqual(store.task('t1')['status'], 'running')
        store.update_task('t1', status='local_ready', artifact_id='a1',
                          state={'timings': [{'stage': 'plan'}]})
        data = store.task('t1')
        self.assertEqual(data['status'], 'local_ready')
        self.assertEqual(data['state']['timings'][0]['stage'], 'plan')


class RepositoryContractTests(unittest.TestCase):
    """仓储接口：换实现前先保证方法齐备（文档 §9.1）。"""

    def test_sqlite_store_satisfies_repository_protocol(self):
        from app.persistence import Repository, check_repository
        from app.store import Store
        store = Store(':memory:')
        self.assertEqual(check_repository(store), [])
        self.assertIsInstance(store, Repository)

    def test_missing_methods_are_reported(self):
        from app.persistence import check_repository

        class Partial:
            def append_message(self, *a, **k): ...

        self.assertIn('claim_run', check_repository(Partial()))


class PostgresMigrationTests(unittest.TestCase):
    """迁移脚本：DDL 翻译正确（**未在真实 PostgreSQL 上验证**）。"""

    def test_ddl_translation(self):
        from app.store import SCHEMA
        from scripts.migrate_to_postgres import to_postgres_ddl
        ddl = to_postgres_ddl(SCHEMA)
        self.assertIn('BIGSERIAL PRIMARY KEY', ddl)
        self.assertIn('DOUBLE PRECISION', ddl)
        self.assertNotIn('AUTOINCREMENT', ddl)


class ApiTests(unittest.TestCase):
    """HTTP 接口与 SSE 端点。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        import app.api as api_module
        from app.api_hub import Hub
        from app.store import Store
        # 每个测试用独立的内存库：互不污染，也不去碰真实的 agent.db
        api_module.hub = Hub(Store(':memory:'))
        self.api_module = api_module
        self.client = TestClient(api_module.app)

    def test_index_serves_chat_ui(self):
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('智能体执行过程', res.text)
        self.assertIn('EventSource', res.text)
        # 阶段耗时面板与实时累加逻辑必须存在
        self.assertIn('阶段耗时', res.text)
        self.assertIn('stage_start', res.text)
        self.assertIn('setInterval', res.text)
        # 只保留一个发送按钮：不再有"停止"和"打断并发送"
        self.assertNotIn('id="stop"', res.text)
        self.assertNotIn('打断并发送', res.text)
        self.assertIn('id="send"', res.text)
        # 回车即发送；Shift+Enter 换行；中文输入法选词时不能误发
        self.assertIn('isComposing', res.text)
        self.assertIn('!e.shiftKey', res.text)
        self.assertNotIn('(e.metaKey || e.ctrlKey)', res.text)

    def test_unknown_task_returns_404(self):
        self.assertEqual(self.client.get('/api/tasks/nope').status_code, 404)
        self.assertEqual(self.client.post('/api/tasks/nope/cancel').status_code, 404)
        self.assertEqual(self.client.get('/api/tasks/nope/artifact').status_code, 404)

    def test_create_task_streams_events_and_ends(self):
        import app.api_hub as hub_module

        async def fake_run_task(text, progress=None, on_event=None,
                                history=None, on_start=None,
                                prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            if on_start:
                on_start('artifact9')
            if on_event:
                on_event({'type': 'action', 'icon': '🌐', 'label': '已搜索网页',
                          'detail': 'SQLite 适用场景'})
            return {'task_id': 'artifact9', 'status': 'local_ready',
                    'deployment': {'status': 'skipped'}, 'timings': []}

        with patch.object(hub_module, 'run_task', fake_run_task):
            res = self.client.post('/api/tasks', json={'message': '测试'})
            self.assertEqual(res.status_code, 200)
            payload = res.json()
            self.assertTrue(payload['task_id'] and payload['session_id'])
            with self.client.stream(
                    'GET', f"/api/tasks/{payload['task_id']}/events") as stream:
                body = ''.join(stream.iter_text())
        # 过程事件与结束标记都要通过 SSE 送达。
        self.assertIn('已搜索网页', body)
        self.assertIn('SQLite 适用场景', body)
        self.assertIn('"type": "end"', body)
        self.assertIn('data: ', body)

    def test_keepalive_is_a_real_event_not_a_comment(self):
        """心跳必须是 data 事件。

        SSE 注释（": keepalive"）不会触发浏览器 onmessage，前端就无从判断连接
        是否还活着：后端一被杀，那一行会永远"进行中"、耗时一直涨。
        """
        import inspect
        import app.api as api_module

        source = inspect.getsource(api_module.task_events)
        self.assertIn("sse({'type': 'ping'})", source)
        self.assertNotIn("': keepalive", source)
        # ping 必须走标准 SSE 帧，浏览器 onmessage 才收得到。
        frame = api_module.sse({'type': 'ping'})
        self.assertTrue(frame.startswith('data: '))
        self.assertTrue(frame.endswith('\n\n'))

    def test_session_messages_endpoint(self):
        import app.api_hub as hub_module

        async def fake_run_task(text, progress=None, on_event=None,
                                history=None, on_start=None,
                                prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            return {'task_id': 'a1', 'status': 'succeeded',
                    'artifact': {'html_path': '/tmp/a1/sources.html',
                                 'kind': 'sources'},
                    'timings': []}

        with patch.object(hub_module, 'run_task', fake_run_task):
            created = self.client.post('/api/tasks', json={'message': '你好'}).json()
        messages = self.client.get(
            f"/api/sessions/{created['session_id']}/messages").json()['messages']
        self.assertEqual(messages[0]['role'], 'user')
        self.assertEqual(messages[0]['content'], '你好')

    def test_session_timeline_replays_execution_steps(self):
        """切换会话时要能重建执行过程：接口必须带回每轮持久化的事件。"""
        import app.api_hub as hub_module

        async def fake_run_task(text, progress=None, on_event=None,
                                history=None, on_start=None,
                                prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            if on_start:
                on_start('a1')
            if on_event:
                on_event({'type': 'stage_start', 'stage': 'compose_report',
                          'label': '整理报告', 'at': 10.0})
                on_event({'type': 'action', 'icon': '🌐', 'label': '已搜索网页'})
                on_event({'type': 'stage_end', 'stage': 'compose_report',
                          'label': '整理报告', 'seconds': 2.5, 'status': '完成'})
            return {'task_id': 'a1', 'status': 'succeeded',
                    'artifact': {'html_path': '/tmp/a1/sources.html',
                                 'kind': 'sources'},
                    'timings': []}

        with patch.object(hub_module, 'run_task', fake_run_task):
            created = self.client.post('/api/tasks', json={'message': '查一下'}).json()
            record = self.api_module.hub.get(created['task_id'])
            for _ in range(100):          # 后台任务跑完再取，事件才都落库
                if record.done:
                    break
                time.sleep(0.02)
        self.assertTrue(record.done, '假任务应当在超时前结束')

        data = self.client.get(
            f"/api/sessions/{created['session_id']}/timeline").json()
        self.assertEqual(data['messages'][0]['content'], '查一下')
        self.assertEqual(len(data['turns']), 1)
        turn = data['turns'][0]
        # 库里存的是通用状态，回放时按 outcome 派生成展示标签
        self.assertEqual(turn['run_status'], 'succeeded')
        self.assertEqual(turn['status'], 'local_ready')
        self.assertEqual(turn['message'], '查一下')
        types = [(e['type'], e.get('stage')) for e in turn['events']]
        self.assertIn(('stage_start', 'compose_report'), types)
        self.assertIn(('stage_end', 'compose_report'), types)
        ends = [e for e in turn['events'] if e['type'] == 'stage_end'
                and e['stage'] == 'compose_report']
        self.assertEqual(ends[0]['seconds'], 2.5)

    def test_profile_can_be_read_and_rewritten(self):
        """画像必须能在界面上改：读回原文、整体覆盖、清空留备份。"""
        from app import profile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'USER.md'
            with patch.object(profile, 'settings',
                              SimpleNamespace(profile_path=path)):
                profile.merge({'身高': '173cm', '体重': '50kg'})

                got = self.client.get('/api/profile').json()
                self.assertEqual(got['facts'], {'身高': '173cm', '体重': '50kg'})
                self.assertIn('身高', got['text'])

                # 整体覆盖：改一条、加一条、删一条
                res = self.client.put('/api/profile', json={'text':
                    '身高：174cm\n饮食：低糖\n'})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()['facts'],
                                 {'身高': '174cm', '饮食': '低糖'})
                self.assertEqual(profile.load(), {'身高': '174cm', '饮食': '低糖'})

                # 清空要留 .bak，手滑也能捞回来
                self.client.delete('/api/profile')
                self.assertEqual(profile.load(), {})
                backup = Path(str(path) + '.bak')
                self.assertTrue(backup.exists())
                self.assertIn('174cm', backup.read_text(encoding='utf-8'))

    def test_profile_rejects_oversized_text(self):
        self.assertEqual(
            self.client.put('/api/profile', json={'text': 'x' * 20001}).status_code,
            422)

    def test_artifact_endpoint_serves_whatever_was_produced(self):
        """产物可能是报告页也可能是资料页，接口不能把文件名写死成 index.html。

        早先硬编码 index.html，"资料转 HTML" 产出的 sources.html 一律 404。
        """
        import app.api_hub as hub_module
        with tempfile.TemporaryDirectory() as folder:
            sources_html = Path(folder) / 'sources.html'
            sources_html.write_text('<h1>资料页</h1>', encoding='utf-8')

            async def fake_run_task(text, progress=None, on_event=None, history=None,
                                    on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
                return {'task_id': 'a1', 'status': 'local_ready', 'timings': [],
                        'deployment': {},
                        'artifact': {'kind': 'sources',
                                     'html_path': str(sources_html)}}

            with patch.object(hub_module, 'run_task', fake_run_task):
                created = self.client.post('/api/tasks', json={'message': '存成网页'}).json()
                record = self.api_module.hub.get(created['task_id'])
                for _ in range(100):
                    if record.done:
                        break
                    time.sleep(0.02)
            res = self.client.get(f"/api/tasks/{created['task_id']}/artifact")
        self.assertEqual(res.status_code, 200)
        self.assertIn('资料页', res.text)

    def test_session_memory_endpoints(self):
        """会话记忆要能在界面上读、改、清空。"""
        hub = self.api_module.hub
        hub.store.append_message('s1', 'user', '你好')

        empty = self.client.get('/api/sessions/s1/memory').json()
        self.assertEqual(empty['summary'], '')
        self.assertEqual(empty['facts'], {})

        saved = self.client.put('/api/sessions/s1/memory', json={
            'summary': '用户在减脂', 'facts': '目标：降到15%\n已否决：节食'}).json()
        self.assertEqual(saved['facts'], {'目标': '降到15%', '已否决': '节食'})

        got = self.client.get('/api/sessions/s1/memory').json()
        self.assertEqual(got['summary'], '用户在减脂')
        self.assertIn('目标：降到15%', got['facts_text'])

        self.client.delete('/api/sessions/s1/memory')
        self.assertEqual(self.client.get('/api/sessions/s1/memory').json()['summary'], '')

    def test_session_timeline_orders_turns_oldest_first(self):
        """回放必须按发生顺序，否则步骤看起来是倒着走的。"""
        from app.store import Store
        store = Store(':memory:')
        store.append_message('s1', 'user', '第一问')
        store.create_task('t1', 's1', '第一问')
        store.append_message('s1', 'user', '第二问')
        store.create_task('t2', 's1', '第二问')
        self.assertEqual([t['task_id'] for t in store.session_tasks('s1')],
                         ['t1', 't2'])


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


class MessageTests(unittest.TestCase):
    """助手回执与停止提示的文案（CLI / API 共用）。"""

    def test_reply_mentions_deployed_url(self):
        from app.messages import reply_text
        text = reply_text({'status': 'deployed',
                           'deployment': {'public_url': 'http://x/y/'}})
        self.assertIn('http://x/y/', text)

    def test_reply_for_local_ready_mentions_deploy_option(self):
        from app.messages import reply_text
        text = reply_text({'status': 'succeeded', 'task_id': 'abc',
                           'artifact': {'html_path': '/tmp/a/index.html',
                                        'kind': 'sources'}})
        self.assertIn('部署', text)

    def test_reply_for_failed_is_explainable(self):
        from app.messages import reply_text
        self.assertIn('没有产出可用结果', reply_text({'status': 'failed'}))
        # 有具体失败原因时要透出来，而不是只说"失败了"
        with_reason = reply_text({'status': 'failed',
                                  'failure': '检索来源失败：搜索失败'})
        self.assertIn('检索来源失败', with_reason)

    def test_cancel_note_warns_when_already_deployed(self):
        import json
        from pathlib import Path as P
        from app.config import settings
        from app.messages import cancel_note
        with tempfile.TemporaryDirectory() as folder:
            # cancel_note 会在 artifact_dir 下再拼一层 task_id
            task_dir = P(folder) / 'task1'
            task_dir.mkdir()
            (task_dir / 'deployment.json').write_text(
                json.dumps({'public_url': 'http://x/deployed/'}), encoding='utf-8')
            original = settings.artifact_dir
            # settings 是 frozen dataclass，patch.object 会抛 FrozenInstanceError
            object.__setattr__(settings, 'artifact_dir', P(folder))
            try:
                note = cancel_note('task1')
            finally:
                object.__setattr__(settings, 'artifact_dir', original)
        # 停止是"取消"不是"撤销"，必须把已发布的站点地址告诉用户。
        self.assertIn('http://x/deployed/', note)
        self.assertIn('不会撤销', note)

    def test_cancel_note_without_artifact(self):
        from app.messages import cancel_note
        self.assertIn('已停止', cancel_note(None))



class TimingLabelTests(unittest.TestCase):
    def test_every_graph_node_has_a_chinese_label(self):
        from app.graph import NODE_NAMES
        from app.main import STAGE_LABELS
        missing = [name for name in NODE_NAMES if name not in STAGE_LABELS]
        self.assertEqual(missing, [])

    def test_format_timings_includes_label_and_seconds(self):
        from app.main import format_timings
        lines = format_timings([{'stage': 'render_sources_html', 'seconds': 8.4,
                                 'status': 'composed'}])
        self.assertEqual(len(lines), 1)
        self.assertIn('资料转 HTML', lines[0])
        self.assertIn('8.40', lines[0])

    def test_format_timings_tolerates_empty(self):
        from app.main import format_timings
        self.assertEqual(format_timings(None), [])
        self.assertEqual(format_timings([]), [])


class ModelOverrideTests(unittest.TestCase):
    """整理报告阶段可以用独立（更快）的模型。"""

    def test_default_uses_main_model(self):
        from app.config import settings
        from app.services import new_llm
        self.assertEqual(new_llm().model_name, settings.llm_model)

    def test_explicit_override_wins(self):
        from app.services import new_llm
        self.assertEqual(new_llm('some-fast-model').model_name, 'some-fast-model')

    def test_compose_model_never_blank(self):
        # 未配置 LLM_COMPOSE_MODEL 时必须回退到 LLM_MODEL，不能是空字符串。
        from app.config import settings
        self.assertTrue(settings.llm_compose_model)


class ThinkingControlTests(unittest.IsolatedAsyncioTestCase):
    """推理开关：只有解析/规划阶段可以关，整理报告必须保留。"""

    def test_disable_thinking_sets_thinking_disabled(self):
        import app.services as services
        with patch.object(services, 'ChatOpenAI') as fake:
            services.new_llm(disable_thinking=True)
        kwargs = fake.call_args.kwargs
        # 只有 thinking.disabled 能真正关掉推理（low/minimal 会被网关忽略）
        self.assertEqual(kwargs['extra_body'], {'thinking': {'type': 'disabled'}})

    def test_default_keeps_thinking_enabled(self):
        import app.services as services
        with patch.object(services, 'ChatOpenAI') as fake:
            services.new_llm()
        kwargs = fake.call_args.kwargs
        self.assertNotIn('extra_body', kwargs)
        self.assertNotIn('reasoning_effort', kwargs)

    async def test_planner_passes_the_flag(self):
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        captured = {}

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            captured.update(kw)
            return ExecutionPlan.model_validate(
                {'steps': [{'tool': 'render_sources_html', 'args': {}}]}), None

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            await nodes.plan({'user_input': '介绍一下 SQLite', 'usage': []})
        from app.config import settings
        self.assertEqual(captured['disable_thinking'],
                         settings.llm_parse_disable_thinking)


class ProfileTests(unittest.TestCase):
    """用户画像：Markdown 文件 + 原子写 + 版本守卫。"""

    def setUp(self):
        from app.config import settings
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = settings.profile_path
        # settings 是 frozen dataclass，用 object.__setattr__ 绕过冻结
        object.__setattr__(settings, 'profile_path',
                           Path(self._tmp.name) / 'USER.md')

    def tearDown(self):
        from app.config import settings
        object.__setattr__(settings, 'profile_path', self._orig)
        self._tmp.cleanup()

    def test_merge_and_reload_roundtrip(self):
        from app import profile
        profile.merge({'姓名': '张三', '电话': '13800001111'})
        self.assertEqual(profile.load()['姓名'], '张三')
        self.assertEqual(profile.load()['电话'], '13800001111')
        # 再次合并是增量，不丢旧字段
        profile.merge({'职业': '后端工程师'})
        self.assertEqual(len(profile.load()), 3)
        # 文件确实落在磁盘上，且是人可读的 Markdown
        text = Path(profile_file()).read_text(encoding='utf-8')
        self.assertIn('# User Profile', text)
        self.assertIn('姓名：张三', text)

    def test_version_increments(self):
        from app import profile
        self.assertEqual(profile.version(), 0)
        profile.merge({'a': 'b'})
        self.assertEqual(profile.version(), 1)
        profile.merge({'c': 'd'})
        self.assertEqual(profile.version(), 2)

    def test_block_and_with_profile(self):
        from app import profile
        profile.merge({'偏好': '简洁'})
        self.assertIn('[用户画像]', profile.block())
        self.assertIn('偏好：简洁', profile.block())
        out = profile.with_profile('指令ABC')
        self.assertTrue(out.startswith('指令ABC'))
        self.assertIn('[用户画像]', out)
        # 空画像时原样返回
        profile.clear()
        self.assertEqual(profile.with_profile('指令ABC'), '指令ABC')

    def test_merge_reports_only_real_changes(self):
        """merge 只返回变化量，调用方才能区分"学到新的"和"把已有的抄一遍"。"""
        from app import profile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'USER.md'
            with patch.object(profile, 'settings', SimpleNamespace(profile_path=path)):
                profile.merge({'身高': '173cm', '体重': '50kg'})
                # 原样重复：什么都没变
                self.assertEqual(profile.merge({'身高': '173cm'}), {})
                # 一部分变化：只报变化的那部分
                changed = profile.merge({'身高': '173cm', '体重': '52kg',
                                         '新项': 'x'})
                self.assertEqual(changed, {'体重': '52kg', '新项': 'x'})
                self.assertEqual(profile.load()['身高'], '173cm')

    def test_clear(self):
        from app import profile
        profile.merge({'x': 'y'})
        profile.clear()
        self.assertEqual(profile.load(), {})
        self.assertEqual(profile.version(), 0)

    def test_backup_kept_on_save(self):
        from app import profile
        from app.config import settings
        profile.merge({'旧': '值'})
        profile.merge({'新': '值'})
        bak = settings.profile_path.parent / 'USER.md.bak'
        self.assertTrue(bak.exists())
        self.assertIn('旧：值', bak.read_text(encoding='utf-8'))


def profile_file():
    from app.config import settings
    return str(settings.profile_path)


class PlannerProfileTests(unittest.IsolatedAsyncioTestCase):
    """规划器也会记用户信息：这条能力不能随重写丢掉。"""

    async def test_plan_merges_profile_updates(self):
        from app import nodes
        from app import profile
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            return ExecutionPlan.model_validate({
                'steps': [{'tool': 'render_sources_html', 'args': {}}],
                'profile_updates': {'电话': '13900000000'},
            }), None

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'USER.md'
            # settings 是 frozen dataclass，只能整体替换而不能改字段
            with patch.object(profile, 'settings', SimpleNamespace(profile_path=path)):
                with patch.object(tool_module, 'catalog', fake_catalog), \
                        patch.object(nodes, 'structured_call', fake_call):
                    await nodes.plan({'user_input': '我电话13900000000', 'usage': []})
                self.assertTrue(path.exists(), '画像应被写入文件')
                self.assertIn('13900000000', path.read_text(encoding='utf-8'))


class SmallTalkFilterTests(unittest.IsolatedAsyncioTestCase):
    """本地寒暄筛选：明显是寒暄就直接回答，省掉一次规划调用。"""

    def test_recognises_plain_small_talk(self):
        from app.services import is_small_talk
        for text in ['你好', '您好！', '你好呀', '在吗', '谢谢', '好的谢谢',
                     '嗯嗯', 'ok', 'OK!', 'hello', 'thanks', 'thank you',
                     '早上好', '晚安', '再见', '好的，谢谢啦', '收到']:
            self.assertTrue(is_small_talk(text), text)

    def test_leaves_real_tasks_to_the_model(self):
        from app.services import is_small_talk
        # 误把任务当寒暄，会让该检索的请求变成闲聊；比多花一次调用糟得多，所以宁可漏判。
        for text in ['帮我查一下 SQLite 的适用场景',
                     '你好，帮我查一下 SQLite 的适用场景',
                     '谢谢，请写一份报告',
                     '什么是 ACID',
                     'https://example.com',
                     'higher', 'okr',
                     '谢谢你帮我写个报告']:
            self.assertFalse(is_small_talk(text), text)

    async def test_link_is_never_treated_as_small_talk(self):
        """带链接就必须走规划与检索，不能因为句子短就被当成寒暄。"""
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        called = {}

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            called['hit'] = True
            return ExecutionPlan.model_validate({'steps': [
                {'tool': 'fetch_pages', 'args': {'urls': ['https://example.com']}}]}), None

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            result = await nodes.plan(
                {'user_input': 'hi https://example.com', 'usage': []})
        self.assertTrue(called.get('hit'), '带 URL 时不能跳过规划')
        self.assertEqual(result['plan']['steps'][0]['tool'], 'fetch_pages')


class GraphShapeTests(unittest.IsolatedAsyncioTestCase):
    """整图跑通：寒暄不检索，规划出来的步骤被逐步执行。"""

    async def test_small_talk_answers_without_searching(self):
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        from app.graph import build_graph
        from app.tools import Tool
        touched = {'searched': False}

        async def fake_answer(args, state):
            return {'answer': '你好！', 'status': 'answered'}

        async def fake_search(args, state):
            touched['searched'] = True
            return {'documents': []}

        table = dict(tool_module.BUILTIN_BY_NAME)
        table['answer_directly'] = Tool('answer_directly', '直接回答', 'd', {}, fake_answer)
        table['web_search'] = Tool('web_search', '检索来源', 'd', {}, fake_search)

        async def fake_catalog():
            return list(table.values())

        with patch.dict(tool_module.BUILTIN_BY_NAME, table), \
                patch.object(tool_module, 'catalog', fake_catalog):
            result = await build_graph().ainvoke({
                'task_id': 'x', 'user_input': '你好', 'documents': [],
                'errors': [], 'usage': [], 'step_index': 0, 'mcp_calls': []})
        from app import outcome
        self.assertEqual(result['status'], 'succeeded')     # 状态机里的通用值
        self.assertEqual(outcome.label(result['status'], result), 'answered')
        self.assertEqual(result['outcome']['kind'], 'answer')
        self.assertFalse(touched['searched'])

    async def test_plan_steps_run_in_order(self):
        from app import nodes
        from app import tools as tool_module
        from app.capabilities import publishing as publishing_cap
        from app.capabilities import export as export_cap
        from app.capabilities import web as web_cap
        from app.graph import build_graph
        from app.tools import Tool
        order = []

        def make(name, label):
            async def run(args, state):
                order.append(name)
                return {}
            return Tool(name, label, 'd', {}, run)

        table = {'web_search': make('web_search', '检索来源'),
                 'render_html': make('render_html', '渲染 HTML')}

        async def fake_catalog():
            return list(table.values())

        async def fake_plan(state):
            return {'plan': {'steps': [{'tool': 'web_search', 'args': {}},
                                       {'tool': 'render_html', 'args': {}}]},
                    'step_index': 0, 'status': 'planned'}

        with patch.dict(tool_module.BUILTIN_BY_NAME, table), \
                patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'plan', fake_plan):
            await build_graph().ainvoke({
                'task_id': 'x', 'user_input': 'q', 'documents': [],
                'errors': [], 'usage': [], 'step_index': 0, 'mcp_calls': []})
        self.assertEqual(order, ['web_search', 'render_html'])


class StructuredRetryTests(unittest.IsolatedAsyncioTestCase):
    """结构化调用：流式累积、渐进草稿、解析失败重试、usage 合并。"""

    class FakeChunk:
        """模拟 AIMessageChunk：支持 + 累积，带 tool_calls / tool_call_chunks / usage。"""

        def __init__(self, tool_calls=None, usage=None, tool_call_chunks=None):
            self.tool_calls = tool_calls or []
            self.usage_metadata = usage
            self.tool_call_chunks = tool_call_chunks or []

        def __add__(self, other):
            return StructuredRetryTests.FakeChunk(
                tool_calls=other.tool_calls or self.tool_calls,
                usage=other.usage_metadata or self.usage_metadata,
                tool_call_chunks=self.tool_call_chunks + other.tool_call_chunks)

    def fake_llm(self, outcomes, calls):
        class FakeLLM:
            def bind_tools(self, tools, tool_choice=None):
                calls.append(('bind', tool_choice))
                return self

            async def astream(self, messages):
                for chunk in outcomes[min(len(calls) - 1, len(outcomes) - 1)]:
                    yield chunk
        return FakeLLM()

    def bad(self, tokens=5):
        # topic 为空过不了 Plan 的 min_length 校验
        return [self.FakeChunk(tool_calls=[{'name': 'ExecutionPlan', 'args': {'steps': []}}],
                               usage={'total_tokens': tokens})]

    def good(self, tokens=7):
        return [self.FakeChunk(
            tool_calls=[{'name': 'ExecutionPlan',
                         'args': {'steps': [{'tool': 'web_search',
                                             'args': {'query': 't'}}]}}],
            usage={'total_tokens': tokens})]

    async def call(self, outcomes, calls=None, **kw):
        """在 patch 生效期间完成调用，返回 ((parsed, usage), calls)。

        await 必须在 with 块里面：否则协程在 patch 退出后才执行，mock 失效、
        直接打到真实 API（这个坑踩过一次，表现为测试里 usage 大得离谱）。
        """
        from app import services
        calls = calls if calls is not None else []
        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False:
                          self.fake_llm(outcomes, calls)):
            result = await services.structured_call(ExecutionPlan, 's', 'u', **kw)
        return result, calls

    async def test_retries_once_then_succeeds(self):
        (parsed, _), calls = await self.call([self.bad(), self.good()])
        self.assertEqual(parsed.steps[0].tool, 'web_search')
        self.assertEqual(len(calls), 2)

    async def test_gives_up_after_max_attempts(self):
        calls = []
        with self.assertRaises(RuntimeError):
            await self.call([self.bad()], calls=calls)
        self.assertEqual(len(calls), 2)

    async def test_usage_sums_failed_and_successful_attempts(self):
        (_, usage), _ = await self.call([self.bad(5), self.good(7)])
        # 失败的尝试同样消耗 token，必须计入成本统计
        self.assertEqual(usage['total_tokens'], 12)

    async def test_single_attempt_returns_its_own_usage(self):
        (_, usage), _ = await self.call([self.good(9)], attempts=1)
        self.assertEqual(usage, {'total_tokens': 9})

    async def test_tool_name_is_forced(self):
        _, calls = await self.call([self.good()], attempts=1)
        # tool_choice 必须钉死工具名，否则模型可能不调用工具
        self.assertEqual(calls[0], ('bind', 'ExecutionPlan'))

    async def test_attempt_timeout_fails_without_retry(self):
        """单次尝试超时立刻失败，不再试第二次。

        重试只会把等待时间翻倍，而超时通常意味着模型/网关本身慢，重试收益低。
        """
        from app import services
        calls = []

        class HangingLLM:
            def bind_tools(self, tools, tool_choice=None):
                calls.append(('bind', tool_choice))
                return self

            async def astream(self, messages):
                await asyncio.sleep(30)      # 永远等不到分片
                yield self.FakeChunk()

        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False: HangingLLM()):
            with self.assertRaises(services.AttemptTimeout):
                await services.structured_call(
                    ExecutionPlan, 's', 'u', attempts=2, attempt_timeout=0.05)
        self.assertEqual(len(calls), 1, '超时不应该重试')

    async def test_attempt_timeout_zero_disables_the_limit(self):
        """attempt_timeout<=0 表示不限制，走正常路径。"""
        (parsed, _), calls = await self.call([self.good()], attempts=1,
                                             attempt_timeout=0)
        self.assertEqual(parsed.steps[0].tool, 'web_search')
        self.assertEqual(len(calls), 1)

    async def test_timeout_does_not_swallow_user_cancel(self):
        """外部取消（用户点停止）必须继续抛 CancelledError，不能被当成超时。"""
        from app import services

        class BlockingLLM:
            def bind_tools(self, tools, tool_choice=None):
                return self

            async def astream(self, messages):
                await asyncio.sleep(30)
                yield self.FakeChunk()

        async def run():
            await services.structured_call(
                ExecutionPlan, 's', 'u', attempts=1, attempt_timeout=30)

        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False: BlockingLLM()):
            task = asyncio.create_task(run())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_draft_callback_clears_then_streams_partials(self):
        from app import services
        calls, drafts = [], []
        partial = self.FakeChunk(
            tool_call_chunks=[{'args':
                '{"steps": [{"tool": "web_search", "args": {"query": "半成品"}}]}'}])
        full = self.FakeChunk(
            tool_calls=[{'name': 'ExecutionPlan',
                         'args': {'steps': [{'tool': 'web_search',
                                             'args': {'query': '完整'}}]}}],
            usage={'total_tokens': 3})
        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False:
                          self.fake_llm([[partial, full]], calls)):
            parsed, _ = await services.structured_call(
                ExecutionPlan, 's', 'u', attempts=1, on_draft=drafts.append, draft_interval=0)
        self.assertEqual(parsed.steps[0].args['query'], '完整')
        # 首次回调必须是 None（清空）：重试时也一样，避免两次内容混在一起
        self.assertIsNone(drafts[0])
        # 中途要能收到半成品（第一步已到、其余还在生成）
        self.assertTrue(any(d is not None and d.steps
                            and d.steps[0].args.get('query') == '半成品'
                            for d in drafts))
        # 最终结果也要推一次，保证前端拿到完整版
        self.assertEqual(drafts[-1].steps[0].args['query'], '完整')

    async def test_throttle_advances_even_when_draft_unparsable(self):
        """解析失败也必须推进节流时间戳。

        否则字段还没凑齐时会退化成"每个分片都重试一次"，对不断增长的缓冲区
        反复做解析与校验，O(n²) 会把事件循环堵死（这个 bug 真在服务里卡死过一次）。
        """
        from app import services
        calls, drafts = [], []
        # 只有分片、没有完整 tool_calls —— 每次解析都失败，最终也失败
        unparsable = [self.FakeChunk(tool_call_chunks=[{'args': '{"topic": ' + 'x' * 200}])
                      for _ in range(40)]
        parsed_count = {'n': 0}
        real_parse = services._draft_of

        def counting_parse(buf, schema):
            parsed_count['n'] += 1
            return real_parse(buf, schema)

        with patch.object(services, '_draft_of', counting_parse), \
             patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False:
                          self.fake_llm([unparsable], calls)):
            with self.assertRaises(RuntimeError):
                await services.structured_call(
                    ExecutionPlan, 's', 'u', attempts=1, on_draft=drafts.append,
                    draft_interval=3600)      # 节流窗口远大于测试时长
        # 40 个分片只允许解析一次（节流生效），而不是 40 次
        self.assertEqual(parsed_count['n'], 1)

    async def test_draft_clear_before_each_retry(self):
        from app import services
        calls, drafts = [], []
        partial = self.FakeChunk(
            tool_call_chunks=[{'args': '{"steps": [{"tool": "web_search",'
                                        ' "args": {"query": "第一版的残留"}}]}'}])
        bad = self.FakeChunk(
            tool_calls=[{'name': 'ExecutionPlan', 'args': {'steps': []}}],
            tool_call_chunks=[{'args': '{"steps": [{"tool": "web_search"}]}'}])
        good = self.FakeChunk(
            tool_calls=[{'name': 'ExecutionPlan',
                         'args': {'steps': [{'tool': 'answer_directly',
                                             'args': {'question': '第二版'}}]}}])
        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False:
                          self.fake_llm([[partial, bad], [good]], calls)):
            await services.structured_call(
                ExecutionPlan, 's', 'u', on_draft=drafts.append, draft_interval=0)
        # 两次尝试各清空一次，第一次尝试的残留不会留在界面上
        self.assertEqual([d is None for d in drafts].count(True), 2)


class ExecutionPathTests(unittest.IsolatedAsyncioTestCase):
    """单一执行路径：就地执行也必须持有租约（评审 P0-1 / P0-5 / P0-6 / P0-7）。

    这些用例守的是同一件事：**同一份计划不能被执行两次**。一旦破防，部署类
    写操作会真的建出第二个站点，模型费用也会翻倍。
    """

    @staticmethod
    def hub(store=None):
        from app.api_hub import Hub
        from app.store import Store
        return Hub(store if store is not None else Store(':memory:'))

    @staticmethod
    def stub(delay=0.0, status='local_ready'):
        async def fake_run_task(text, progress=None, on_event=None, history=None,
                                on_start=None, prior=None, memory=None,
                                ledger=None, run_id=None, resume_plan=None):
            if delay:
                await asyncio.sleep(delay)
            return {'task_id': 'a1', 'status': status, 'timings': [],
                    'deployment': {}}
        return fake_run_task

    async def test_inline_execution_holds_a_lease_so_worker_cannot_steal(self):
        """API 就地执行也要占租约，否则 Worker 会把这条 running 当孤儿再跑一遍。"""
        import app.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=0.2)):
            record = hub.start('做点事')
            run = hub.store.run(record.run_id)
            self.assertEqual(run['status'], 'running')
            self.assertTrue(run['lease_owner'], '就地执行也必须写 lease_owner')
            self.assertIsNone(hub.store.claim_run('w-other'),
                              '租约未过期时 Worker 不该领到同一个 run')
            await asyncio.wait_for(record._task, timeout=5)
        # 跑完要交还租约，否则这条 run 在过期前都无法被续跑
        self.assertIsNone(hub.store.run(record.run_id)['lease_owner'])

    async def test_reconcile_spares_runs_held_by_another_instance(self):
        """第二个副本启动时不能替别人正在跑的 run 判死。"""
        import app.api_hub as hub_module
        first = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=0.3)):
            record = first.start('长任务')
            second = self.hub(first.store)
            stats = second.reconcile(owner='other-instance')
            self.assertEqual(stats['runs'], 0)
            self.assertEqual(first.store.run(record.run_id)['status'], 'running')
            await asyncio.wait_for(record._task, timeout=5)

    def test_reconcile_requeues_runs_without_inflight_actions(self):
        """没有 in_flight 动作的遗留 run 应回到队列，而不是挂着等人核实。"""
        hub = self.hub()
        hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        hub.store.create_run('r1', 't1', status='running')
        stats = hub.reconcile()
        self.assertEqual(stats['requeued'], 1)
        self.assertEqual(hub.store.run('r1')['status'], 'queued')

    def test_cross_process_cancel_writes_a_flag_and_is_honest(self):
        """取消要真的送到执行者，且已经结束的运行不能假装取消成功。"""
        hub = self.hub()
        hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        hub.store.create_run('r1', 't1', status='running')
        self.assertTrue(hub.cancel_run('r1'))
        self.assertIsNotNone(hub.store.run('r1')['cancel_requested_at'])
        hub.store.update_run('r1', status='succeeded')
        self.assertFalse(hub.cancel_run('r1'))

    async def test_resume_works_after_restart(self):
        """杀进程 → 新 Hub → 续跑：resume 必须回库，而不是只查内存。"""
        import app.api_hub as hub_module
        from app.store import Store
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'a.db'
            first = self.hub(Store(path))
            with patch.object(hub_module, 'run_task', self.stub()):
                record = first.start('第一次')
                await asyncio.wait_for(record._task, timeout=5)
            # 模拟"计划已存、执行没跑完"
            first.store.save_plan(
                record.run_id, 1, goal='g',
                steps=[{'tool': 'render_html', 'step_id': 's1', 'args': {},
                        'tool_key': 'local:reports:render'}])
            first.store.update_run(record.run_id, status='failed')
            task_id, run_id = record.task_id, record.run_id
            first.store.close()

            second = self.hub(Store(path))
            seen = {}

            async def fake(text, progress=None, on_event=None, history=None,
                           on_start=None, prior=None, memory=None, ledger=None,
                           run_id=None, resume_plan=None):
                seen['plan'] = resume_plan
                seen['history'] = history
                return {'task_id': 'a1', 'status': 'local_ready', 'timings': [],
                        'deployment': {}}

            with patch.object(hub_module, 'run_task', fake):
                resumed = second.resume(run_id)
                self.assertIsNotNone(resumed, '重启后必须还能续跑')
                await asyncio.wait_for(resumed._task, timeout=5)
            self.assertEqual([s['step_id'] for s in seen['plan']['steps']], ['s1'])
            # 续跑要带上历史，否则续出来的结果接不上前文
            self.assertTrue(any(m['content'] == '第一次'
                                for m in (seen['history'] or [])))
            self.assertEqual(second.store.task(task_id)['task_id'], task_id)


class BackendJobTests(unittest.IsolatedAsyncioTestCase):
    """后台作业：必须真的 await，失败要退避重排而不是一次终结。"""

    def hub(self):
        from app.api_hub import Hub
        from app.store import Store
        return Hub(Store(':memory:'))

    async def test_failed_job_is_requeued_with_backoff(self):
        from app import memory as memory_module
        from scripts.run_worker import drain_jobs
        hub = self.hub()
        hub.store.enqueue_job('j1', 'session_summary', 's1:r1',
                              {'session_id': 's1'})

        async def boom(store, session_id):
            raise RuntimeError('模型不可用')

        with patch.object(memory_module, 'compact', boom):
            self.assertEqual(await drain_jobs(hub, 'w1'), 0)
        job = hub.store.job('j1')
        self.assertEqual(job['status'], 'queued', '一次失败不能当终态')
        self.assertEqual(job['last_error'], 'RuntimeError')
        self.assertGreater(job['lease_expires_at'], time.time(), '应处于退避中')

    async def test_summary_job_actually_compacts_memory(self):
        """以前 asyncio.run() 在运行中的事件循环里必炸，摘要从来没成功过。"""
        from app import memory as memory_module
        from scripts.run_worker import drain_jobs
        hub = self.hub()
        for index in range(14):
            hub.store.append_message('s1', 'user', f'第{index}条')
        hub.store.enqueue_job('j1', 'session_summary', 's1:r1',
                              {'session_id': 's1'})
        seen = {}

        async def fake_compact(store, session_id):
            seen['session'] = session_id
            return {'compacted': True, 'folded': 8}

        with patch.object(memory_module, 'compact', fake_compact):
            self.assertEqual(await drain_jobs(hub, 'w1'), 1)
        self.assertEqual(seen['session'], 's1')
        self.assertEqual(hub.store.job('j1')['status'], 'done')


class KernelCorrectnessTests(unittest.IsolatedAsyncioTestCase):
    """内核层：截断声明、坏计划、提示词分包（评审 P0-3 / P0-4 / 通用性）。"""


    async def test_bad_plan_stops_instead_of_executing(self):
        """两轮修不好就停：不能带着坏依赖去执行一份残缺计划。"""
        from app import nodes
        from app import tools as tool_module

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            return ExecutionPlan.model_validate({'steps': [
                {'tool': 'not_a_tool', 'args': {}}]}), None

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            result = await nodes.plan({'user_input': '做点事', 'usage': []})
        self.assertEqual(result['status'], 'waiting_input')
        self.assertEqual(result['plan']['steps'], [])
        self.assertIn('unknown_tool', result['failure'])
        # 图必须直接收尾，不能因为"计划为空"又走进 run_step
        self.assertEqual(nodes.next_step(result), 'end')
        # 停下来的任务也要给用户一句人话
        from app.messages import reply_text
        self.assertIn('先停在这里', reply_text(result))

    def test_planner_prompt_is_split_by_capability(self):
        """业务规则归能力包：没有部署工具时，提示词里就不该教怎么部署。"""
        from app import tools as tool_module
        from app.integrations import mcp_servers
        deploy = tool_module._deploy_tool(
            mcp_servers.Server(id='publishing', url='https://a/mcp',
                               deploy_tool='deploy_site', default=True),
            {'name': 'deploy_site', 'effect_type': 'write'})
        catalog = tool_module.BUILTIN_TOOLS + [deploy]
        text = nodes.planner_system(catalog)
        for needle in ('联网检索', '导出网页', '发布'):
            self.assertIn(needle, text, needle)
        self.assertIn('step_id', text)
        without = [tool for tool in catalog if tool.name != 'deploy_site']
        self.assertNotIn('【发布】', nodes.planner_system(without))


class ApiOwnershipTests(unittest.TestCase):
    """接口归属校验：知道 session_id 不等于有权限（评审 P0-8）。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        import app.api as api_module
        from app.api_hub import Hub
        from app.store import Store
        api_module.hub = Hub(Store(':memory:'))
        self.api_module = api_module
        self.client = TestClient(api_module.app)

    def _create(self, message='你好'):
        import app.api_hub as hub_module

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            return {'task_id': 'a1', 'status': 'answered', 'timings': [],
                    'deployment': {}, 'answer': '好的'}

        with patch.object(hub_module, 'run_task', fake):
            return self.client.post('/api/tasks', json={'message': message},
                                    headers={'X-DSH-User': 'u1'}).json()

    def test_other_user_cannot_read_a_session(self):
        created = self._create()
        session_id = created['session_id']
        as_owner = self.client.get(f'/api/sessions/{session_id}/messages',
                                   headers={'X-DSH-User': 'u1'})
        self.assertEqual(as_owner.status_code, 200)
        # 只把 ID 换成别人的身份：一律按"不存在"处理，不泄漏对象是否存在
        for path in (f'/api/sessions/{session_id}/messages',
                     f'/api/sessions/{session_id}/timeline',
                     f'/api/sessions/{session_id}/memory',
                     f"/api/tasks/{created['task_id']}",
                     f"/api/tasks/{created['task_id']}/artifact"):
            res = self.client.get(path, headers={'X-DSH-User': 'u2'})
            self.assertEqual(res.status_code, 404, path)

    def test_profile_is_scoped_per_user(self):
        from app import profile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'USER.md'
            with patch.object(profile, 'settings',
                              SimpleNamespace(profile_path=path)):
                self.client.put('/api/profile', json={'text': '姓名：甲'},
                                headers={'X-DSH-User': 'u1'})
                self.client.put('/api/profile', json={'text': '姓名：乙'},
                                headers={'X-DSH-User': 'u2'})
                first = self.client.get('/api/profile',
                                        headers={'X-DSH-User': 'u1'}).json()
                second = self.client.get('/api/profile',
                                         headers={'X-DSH-User': 'u2'}).json()
                self.assertEqual(first['facts'], {'姓名': '甲'})
                self.assertEqual(second['facts'], {'姓名': '乙'})
                self.assertFalse(path.exists(), '默认用户不该被别人的画像覆盖')

    def test_health_exposes_queue_state(self):
        data = self.client.get('/api/health').json()
        self.assertTrue(data['ok'])
        for key in ('runs_queued', 'runs_running', 'actions_unknown',
                    'jobs_queued'):
            self.assertIn(key, data)


class IdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """创建任务幂等：客户端重试不能变成"两个任务、两次部署"。"""

    def principal(self, user='u1'):
        return SimpleNamespace(tenant_id='default', user_id=user)

    def test_same_key_and_body_replays_the_same_task(self):
        from app import idempotency
        from app.store import Store
        store = Store(':memory:')
        body = {'message': '做个报告', 'session_id': None}
        first = idempotency.reserve(store, principal=self.principal(),
                                    endpoint='tasks.create', client_key='k1',
                                    payload=body, ttl_seconds=3600)
        self.assertFalse(first.replayed)
        first.complete({'task_id': 't1', 'session_id': 's1', 'run_id': 'r1'},
                       task_id='t1', session_id='s1', run_id='r1')

        again = idempotency.reserve(store, principal=self.principal(),
                                    endpoint='tasks.create', client_key='k1',
                                    payload=body, ttl_seconds=3600)
        self.assertTrue(again.replayed)
        self.assertEqual(again.response['task_id'], 't1')

    def test_same_key_with_different_body_conflicts(self):
        from app import idempotency
        from app.store import Store
        store = Store(':memory:')
        idempotency.reserve(store, principal=self.principal(),
                            endpoint='tasks.create', client_key='k1',
                            payload={'message': '甲'}, ttl_seconds=3600)
        with self.assertRaises(idempotency.IdempotencyConflict):
            idempotency.reserve(store, principal=self.principal(),
                                endpoint='tasks.create', client_key='k1',
                                payload={'message': '乙'}, ttl_seconds=3600)

    def test_concurrent_duplicate_is_reported_as_in_flight(self):
        """并发的重复请求只能有一个赢家；另一个不能顺手又建一个任务。"""
        from app import idempotency
        from app.store import Store
        store = Store(':memory:')
        idempotency.reserve(store, principal=self.principal(),
                            endpoint='tasks.create', client_key='k1',
                            payload={'message': '甲'}, ttl_seconds=3600)
        with self.assertRaises(idempotency.IdempotencyInFlight):
            idempotency.reserve(store, principal=self.principal(),
                                endpoint='tasks.create', client_key='k1',
                                payload={'message': '甲'}, ttl_seconds=3600)

    def test_key_is_scoped_per_user(self):
        """同一个键给不同用户用，不该互相顶掉。"""
        from app import idempotency
        from app.store import Store
        store = Store(':memory:')
        idempotency.reserve(store, principal=self.principal('u1'),
                            endpoint='tasks.create', client_key='k1',
                            payload={'message': '甲'}, ttl_seconds=3600)
        other = idempotency.reserve(store, principal=self.principal('u2'),
                                    endpoint='tasks.create', client_key='k1',
                                    payload={'message': '乙'}, ttl_seconds=3600)
        self.assertFalse(other.replayed)

    def test_expired_key_can_be_reused(self):
        from app.store import Store
        store = Store(':memory:')
        store.reserve_request_key('scope', client_key='k1', tenant_id='default',
                                  owner_user_id='u1', endpoint='tasks.create',
                                  fingerprint='old', expires_at=time.time() - 5)
        row = store.reserve_request_key('scope', client_key='k1',
                                        tenant_id='default', owner_user_id='u1',
                                        endpoint='tasks.create',
                                        fingerprint='new', expires_at=time.time() + 60)
        self.assertEqual(row['fingerprint'], 'new')

    def test_release_allows_retry_after_failure(self):
        from app import idempotency
        from app.store import Store
        store = Store(':memory:')
        reservation = idempotency.reserve(
            store, principal=self.principal(), endpoint='tasks.create',
            client_key='k1', payload={'message': '甲'}, ttl_seconds=3600)
        reservation.release()
        again = idempotency.reserve(
            store, principal=self.principal(), endpoint='tasks.create',
            client_key='k1', payload={'message': '甲'}, ttl_seconds=3600)
        self.assertFalse(again.replayed, '失败后必须允许用同一个键重试')


class ApiIdempotencyTests(unittest.TestCase):
    """接口层：重试同一个键只建一个任务；换内容返回 409。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        import app.api as api_module
        from app.api_hub import Hub
        from app.store import Store
        api_module.hub = Hub(Store(':memory:'))
        self.api_module = api_module
        self.client = TestClient(api_module.app)

    def _post(self, message='做个报告', key='k1', user='u1'):
        import app.api_hub as hub_module

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            return {'task_id': 'a1', 'status': 'answered', 'timings': [],
                    'deployment': {}, 'answer': '好的'}

        headers = {'X-DSH-User': user}
        if key:
            headers['Idempotency-Key'] = key
        with patch.object(hub_module, 'run_task', fake):
            return self.client.post('/api/tasks', json={'message': message},
                                    headers=headers)

    def test_retry_with_same_key_returns_the_same_task(self):
        first = self._post().json()
        second = self._post().json()
        self.assertEqual(first['task_id'], second['task_id'])
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertTrue(second['idempotent_replay'])
        tasks = self.api_module.hub.store.session_tasks(first['session_id'])
        self.assertEqual(len(tasks), 1, '重试不能建出第二个任务')

    def test_same_key_with_another_message_is_rejected(self):
        self._post()
        res = self._post(message='换个题目')
        self.assertEqual(res.status_code, 409)
        self.assertIn('幂等键', res.json()['detail'])

    def test_without_key_each_request_creates_its_own_task(self):
        first = self._post(key=None).json()
        second = self._post(key=None).json()
        self.assertNotEqual(first['task_id'], second['task_id'])


class CompensationTests(unittest.IsolatedAsyncioTestCase):
    """补偿步骤（文档 §8.4）：撤销是有副作用动作，要单独执行、单独记账。"""

    @staticmethod
    def _tools(calls):
        from app import tools as tool_module

        async def deploy(args, state):
            calls.append(('deploy', dict(args)))
            return {'deployment': {'site_id': 'site-1'}}

        async def explode(args, state):
            raise RuntimeError('第二步炸了')

        async def undo(args, state):
            calls.append(('undo', dict(args)))
            return {}

        return {
            'deploy': tool_module.Tool(
                'deploy', '部署', '', {}, deploy,
                tool_key='local:publishing:deploy', effect='write'),
            'explode': tool_module.Tool(
                'explode', '会失败的步骤', '', {}, explode,
                tool_key='local:x:boom', effect='write'),
            'undo': tool_module.Tool(
                'undo', '撤销部署', '', {}, undo,
                tool_key='local:publishing:undo', effect='destructive'),
        }

    def test_unknown_compensation_is_a_warning_not_a_blocker(self):
        """补偿工具写错不该把整份计划判死，但必须留痕。"""
        from app import tools as tool_module
        steps, issues = nodes.validate_plan([
            {'tool': 'render_sources_html', 'step_id': 's1', 'args': {},
             'compensate': {'tool': 'ghost_tool', 'args': {}}},
        ], tool_module.BUILTIN_TOOLS)
        self.assertEqual([s['step_id'] for s in steps], ['s1'])
        self.assertNotIn('compensate', steps[0])
        self.assertEqual(issues[0]['code'], 'unknown_compensation')
        self.assertEqual(issues[0]['severity'], 'warning')

    def test_compensation_is_kept_and_bound_to_a_real_tool(self):
        from app import tools as tool_module
        steps, issues = nodes.validate_plan([
            {'tool': 'web_search', 'step_id': 's1', 'args': {'query': 'q'},
             'compensate': {'tool': 'render_sources_html', 'args': {}}},
        ], tool_module.BUILTIN_TOOLS)
        self.assertEqual(issues, [])
        self.assertEqual(steps[0]['compensate']['tool_key'],
                         'local:export:sources_html')

    async def test_failure_rolls_back_succeeded_writes_in_reverse(self):
        """前面成功、后面失败 → 逆序撤销已成功的写操作，并把明细写进状态。"""
        from app import tools as tool_module
        calls = []
        tools = self._tools(calls)
        plan = {'steps': [
            {'tool': 'deploy', 'step_id': 's1', 'args': {},
             'compensate': {'tool': 'undo', 'args': {
                 'site_id': {'ref': {'step_id': 's1',
                                     'pointer': '/deployment/site_id'}}}}},
            {'tool': 'explode', 'step_id': 's2', 'args': {},
             'depends_on': ['s1']},
        ]}
        state = {'plan': plan, 'step_index': 0, 'step_outputs': {},
                 'user_input': '做个页面并发布'}

        async def fake_find(name):
            return tools.get(name)

        with patch.object(tool_module, 'find_tool', fake_find), \
                patch.object(nodes, 'emit', lambda event: None):
            state.update(await nodes.run_step(state))
            result = await nodes.run_step(state)

        self.assertEqual(calls[0][0], 'deploy')
        self.assertEqual(calls[1], ('undo', {'site_id': 'site-1'}),
                         '补偿必须拿到部署返回的站点 ID')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['compensations'][0]['status'], 'succeeded')
        self.assertIn('已回滚 1 步', result['failure'])

    async def test_failed_rollback_is_reported_as_partial(self):
        """撤销失败不能只说"失败"：线上可能还留着一个站点。"""
        from app import tools as tool_module
        calls = []
        tools = self._tools(calls)

        async def broken_undo(args, state):
            raise RuntimeError('删除站点被拒绝')

        tools['undo'] = replace(tools['undo'], run=broken_undo)
        plan = {'steps': [
            {'tool': 'deploy', 'step_id': 's1', 'args': {},
             'compensate': {'tool': 'undo', 'args': {}}},
            {'tool': 'explode', 'step_id': 's2', 'args': {}},
        ]}
        state = {'plan': plan, 'step_index': 0, 'step_outputs': {},
                 'user_input': 'x'}

        async def fake_find(name):
            return tools.get(name)

        with patch.object(tool_module, 'find_tool', fake_find), \
                patch.object(nodes, 'emit', lambda event: None):
            state.update(await nodes.run_step(state))
            result = await nodes.run_step(state)
        self.assertEqual(result['status'], 'partially_succeeded')
        self.assertIn('回滚失败', result['failure'])

    async def test_unknown_action_is_not_compensated_blindly(self):
        """结果未定的写操作不能自动去撤：那是基于猜测再发一次写请求。"""
        from app import tools as tool_module
        from app import ledger as ledger_module
        from app.store import Store
        calls = []
        tools = self._tools(calls)
        store = Store(':memory:')
        book = ledger_module.ActionLedger(store, 'r1', 1)
        prepared = book.begin(step_id='s2', tool_key='local:x:boom',
                              effect='write', args={})
        book.in_flight(prepared['action_id'])
        store.update_action(prepared['action_id'], status='unknown')

        plan = {'steps': [
            {'tool': 'deploy', 'step_id': 's1', 'args': {},
             'compensate': {'tool': 'undo', 'args': {}}},
            {'tool': 'explode', 'step_id': 's2', 'args': {}},
        ]}
        state = {'plan': plan, 'step_index': 1,
                 'step_outputs': {'s1': {'__status__': 'succeeded'}},
                 'user_input': 'x'}

        async def fake_find(name):
            return tools.get(name)

        with patch.object(tool_module, 'find_tool', fake_find), \
                patch.object(nodes, 'emit', lambda event: None), \
                ledger_module.use_ledger(book):
            result = await nodes.run_step(state)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['needs_reconciliation'])
        self.assertFalse([c for c in calls if c[0] == 'undo'],
                         '结果未定时不能自动撤销')

    async def test_manual_compensation_creates_its_own_run(self):
        """手工补偿是一次独立运行，原运行只记 compensated_at。"""
        import app.api_hub as hub_module
        from app.api_hub import Hub
        from app.store import Store
        hub = Hub(Store(':memory:'))
        hub.store.create_task('t1', 's1', '发布页面')
        hub.store.ensure_task_meta('t1', session_id='s1', goal='发布页面')
        hub.store.create_run('r1', 't1', status='succeeded')
        step = {'tool': 'deploy_site', 'step_id': 's1', 'args': {},
                'tool_key': 'local:publishing:deploy', 'effect': 'write',
                'compensate': {'tool': 'undo_site', 'args': {'site_id': 'x'},
                               'tool_key': 'local:publishing:undo',
                               'effect': 'destructive'}}
        hub.store.save_plan('r1', 1, goal='发布页面', steps=[step])
        hub.store.save_steps('r1', 1, [step])
        hub.store.update_step('r1', 1, 's1', status='succeeded')

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            assert resume_plan['kind'] == 'compensation'
            # 真实 run_task 会把 resume_plan 落成 plan（含 kind），
            # outcome 就是靠它认出"这是一次补偿"
            return {'task_id': 'a1', 'status': 'succeeded', 'timings': [],
                    'plan': {'kind': 'compensation', 'steps': []},
                    'compensations': [
                        {'step_id': 's1', 'tool': 'undo_site',
                         'status': 'succeeded'}]}

        with patch.object(hub_module, 'run_task', fake):
            result = await hub.compensate('r1')

        self.assertIsNotNone(result)
        self.assertNotEqual(result['run_id'], 'r1')
        self.assertEqual(hub.store.run('r1')['compensated_at'] is not None, True)
        self.assertEqual(result['status'], 'compensated')
        # 再补一次要被拒绝：撤销两次可能误删重建的东西
        with self.assertRaises(RuntimeError):
            await hub.compensate('r1')

    async def test_compensation_skips_steps_that_never_succeeded(self):
        from app.api_hub import Hub
        from app.store import Store
        hub = Hub(Store(':memory:'))
        hub.store.create_task('t1', 's1', 'g')
        hub.store.ensure_task_meta('t1', session_id='s1', goal='g')
        hub.store.create_run('r1', 't1', status='failed')
        step = {'tool': 'deploy_site', 'step_id': 's1', 'args': {},
                'compensate': {'tool': 'undo_site', 'args': {}}}
        hub.store.save_plan('r1', 1, goal='g', steps=[step])
        hub.store.save_steps('r1', 1, [step])
        hub.store.update_step('r1', 1, 's1', status='failed')
        self.assertEqual(hub.compensation_steps('r1'), [])


class McpRoutingTests(unittest.IsolatedAsyncioTestCase):
    """多 MCP Server：工具命名、路由、失败隔离。"""

    MULTI = json.dumps([
        {'id': 'publishing', 'url': 'https://a.example/mcp',
         'deploy_tool': 'deploy_site', 'default': True},
        {'id': 'ops', 'url': 'https://b.example/mcp'},
    ])

    def test_single_server_keeps_legacy_tool_names(self):
        from app.integrations import mcp_servers
        with patch.dict(os.environ, {'MCP_SERVERS': '',
                                     'MCP_URL': 'https://a.example/mcp'}, clear=False):
            servers = mcp_servers.servers()
            self.assertEqual(len(servers), 1)
            self.assertEqual(mcp_servers.tool_name(servers[0], 'list_sites'),
                             'mcp__list_sites')
            self.assertTrue(mcp_servers.deploy_server().default)

    def test_multiple_servers_qualify_tool_names(self):
        from app.integrations import mcp_servers
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

    def test_invalid_entries_are_skipped_not_fatal(self):
        from app.integrations import mcp_servers
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
        from app import tools as tool_module
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
        from app import tools as tool_module
        from app.integrations import mcp_servers
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
        from app import tools as tool_module
        legacy = tool_module.Tool('mcp__ops__list_sites', 'MCP·list_sites',
                                  '', {}, None, tool_key='mcp:ops:list_sites')

        async def fake_tools(force=False):
            return [legacy]

        with patch.object(tool_module, 'mcp_tools', fake_tools):
            found = await tool_module.find_tool('mcp__list_sites')
        self.assertIs(found, legacy)


class StatusVocabularyTests(unittest.IsolatedAsyncioTestCase):
    """状态机词汇封闭：业务结果进 outcome，展示词只出现在派生标签里。

    这条约束是上一轮讨论的落点——"接 N 个 MCP Server 就要加 N 个状态值"
    之所以不成立，全靠这里钉住：加一个业务终态会先让这组用例红。
    """

    def test_run_status_vocabulary_is_closed(self):
        from app import outcome
        # 冻结契约：要加状态值，就得连这份清单一起改，是有意识的决定
        self.assertEqual(sorted(outcome.RUN_STATUSES), [
            'cancelled', 'failed', 'interrupted', 'needs_reconcile',
            'partially_succeeded', 'queued', 'reconciling', 'running',
            'succeeded', 'waiting_approval', 'waiting_input'])
        for word in ('answered', 'local_ready', 'deployed', 'deploy_needs_check',
                     'compensated'):
            self.assertNotIn(word, outcome.RUN_STATUSES, word)
            # 这些词只允许作为"历史值"存在，不再参与状态机
            self.assertIn(word, outcome.LEGACY_STATUS, word)

    def test_label_is_the_only_place_with_display_words(self):
        from app import outcome
        cases = [
            ('succeeded', {'answer': 'hi'}, 'answered'),
            ('succeeded', {'artifact': {'html_path': '/x.html'}}, 'local_ready'),
            ('succeeded', {'deployment': {'status': 'success'}}, 'deployed'),
            ('succeeded', {'plan': {'kind': 'compensation'}}, 'compensated'),
            ('needs_reconcile', {'deployment': {'status': 'pending'}},
             'deploy_needs_check'),
            # 没有部署的"未核实"不该硬套部署的说法
            ('needs_reconcile', {}, 'needs_reconcile'),
            ('failed', {}, 'failed'),
            ('cancelled', {}, 'cancelled'),
        ]
        for status, state, expected in cases:
            self.assertEqual(outcome.label(status, state), expected, status)

    def test_legacy_business_status_normalizes(self):
        from app import outcome
        self.assertEqual(outcome.normalize_status('local_ready'), 'succeeded')
        self.assertEqual(outcome.normalize_status('deploy_needs_check'),
                         'needs_reconcile')
        self.assertEqual(outcome.normalize_status('succeeded'), 'succeeded')
        # 未知值不猜：原样交给上层，由 check_run_status 报错
        self.assertEqual(outcome.normalize_status('ticket_created'),
                         'ticket_created')

    def test_out_of_vocabulary_status_is_loud_not_silent(self):
        """以前 `RUN_STATUS_OF.get(status, status)` 会把业务词静默写进状态机。"""
        from app import outcome
        with self.assertLogs('app.outcome', level='ERROR'):
            self.assertEqual(outcome.check_run_status('ticket_created'), 'failed')
        self.assertEqual(outcome.check_run_status('succeeded'), 'succeeded')

    def test_outcome_records_produced_and_effects(self):
        from app import outcome
        state = {
            'artifact': {'html_path': '/x/sources.html', 'kind': 'sources',
                         'sha256': 'abc', 'size_bytes': 10},
            'deployment': {'status': 'pending'},
            'plan': {'steps': [
                {'tool': 'web_search', 'step_id': 's1',
                 'tool_key': 'local:web:search', 'effect': 'read'},
                {'tool': 'render_sources_html', 'step_id': 's2',
                 'tool_key': 'local:export:sources_html', 'effect': 'write'},
                {'tool': 'deploy_site', 'step_id': 's3',
                 'tool_key': 'mcp:publishing:deploy_site', 'effect': 'write'},
                {'tool': 'render_sources_html', 'step_id': 's4',
                 'tool_key': 'local:export:sources_html', 'effect': 'write'},
            ]},
            'step_outputs': {
                's1': {'__status__': 'succeeded'},
                's2': {'__status__': 'succeeded',
                       'artifact': {'html_path': '/x/sources.html'}},
                's3': {'__status__': 'succeeded',
                       'deployment': {'status': 'pending'}},
                's4': {'__status__': 'failed'},
            },
        }
        record = outcome.build(state)
        self.assertEqual(record['kind'], 'deployment')
        self.assertEqual([item['path'] for item in record['produced']],
                         ['/x/sources.html'])
        # 只记**成功**的有副作用调用：read 不算，失败那步也不算
        self.assertEqual([item['tool_key'] for item in record['effects']],
                         ['local:export:sources_html',
                          'mcp:publishing:deploy_site'])
        # render 落地成文件即视为已核实；部署回执看不懂 → 未核实
        self.assertTrue(record['effects'][0]['verified'])
        self.assertEqual(record['effects'][0]['target_ref'], '')
        self.assertFalse(record['effects'][1]['verified'])
        self.assertTrue(record['unverified'])
        self.assertEqual(outcome.final_status(state), 'needs_reconcile')

    async def test_tool_status_is_a_stage_marker_not_a_run_status(self):
        """工具自报的 status 是阶段小结（searched/answered），不是 run 终态。"""
        from app import nodes
        from app import tools as tool_module
        from app.tools import Tool

        async def fake(args, state):
            return {'answer': '你好', 'status': 'answered'}

        tool = Tool('answer_directly', '直接回答', 'd', {}, fake,
                    tool_key='local:answers:direct', effect='read')
        plan = {'steps': [{'tool': 'answer_directly', 'step_id': 's1', 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'answer_directly': tool}), \
                patch_all_emits(lambda e: None):
            result = await nodes.run_step({'plan': plan, 'step_index': 0,
                                           'user_input': '你好'})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['outcome']['kind'], 'answer')
        # 阶段小结仍留在这一步的输出与耗时行上，但没写进 run 终态
        self.assertEqual(result['step_outputs']['s1']['status'], 'answered')

    async def test_hub_never_persists_a_business_status(self):
        """就算执行层交出旧词，落库的也必须是通用状态。"""
        import app.api_hub as hub_module
        from app.api_hub import Hub
        from app.store import Store

        async def fake_run_task(text, progress=None, on_event=None, history=None,
                                on_start=None, prior=None, memory=None,
                                ledger=None, run_id=None, resume_plan=None):
            if on_start:
                on_start('a1')
            return {'task_id': 'a1', 'status': 'local_ready',   # 旧词
                    'artifact': {'html_path': '/tmp/a1/sources.html',
                                 'kind': 'sources'},
                    'timings': []}

        hub = Hub(Store(':memory:'))
        with patch.object(hub_module, 'run_task', fake_run_task):
            record = hub.start('存成网页')
            await asyncio.wait_for(record._task, timeout=10)

        self.assertEqual(record.status, 'succeeded')
        self.assertEqual(hub.store.task(record.task_id)['status'], 'succeeded')
        self.assertEqual(hub.store.run(record.run_id)['status'], 'succeeded')
        # 展示标签仍然说得清"本地已生成"
        self.assertEqual(record.summary()['status'], 'local_ready')
        self.assertEqual(record.summary()['run_status'], 'succeeded')

    def test_reply_for_needs_reconcile_shows_the_url_but_does_not_claim_success(self):
        from app.messages import reply_text
        text = reply_text({'status': 'needs_reconcile',
                           'deployment': {'status': 'needs_check',
                                          'public_url': 'http://x/1/'}})
        self.assertIn('http://x/1/', text)
        self.assertIn('确认', text)
        self.assertNotIn('部署完成', text)

    def test_migration_script_rewrites_legacy_rows(self):
        """历史数据迁移：业务终态 → 通用状态，且可重复执行。"""
        import importlib
        from app.store import Store
        from types import SimpleNamespace as NS
        migrate = importlib.import_module('scripts.migrate_status_vocabulary')

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.db'
            store = Store(path)
            store.create_task('t1', 's1', '问个问题')
            store.update_task('t1', status='answered')
            store.create_task('t2', 's1', '存成网页')
            store.update_task('t2', status='local_ready')
            store.create_task('t3', 's1', '部署')
            store.update_task('t3', status='deploy_needs_check')
            store.create_run('r1', 't1', status='succeeded')
            store.create_run('r2', 't2', status='succeeded')
            store.create_run('r3', 't3', status='failed')      # 当年被折成失败
            store.close()

            with patch.object(migrate, 'settings',
                              NS(database_url='', db_path=path)):
                self.assertEqual(migrate.main(['--apply']), 0)

            store = Store(path)
            self.assertEqual(store.task('t1')['status'], 'succeeded')
            self.assertEqual(store.task('t2')['status'], 'succeeded')
            self.assertEqual(store.task('t3')['status'], 'needs_reconcile')
            # 部署待确认不是失败：对应 run 要纠正回来
            self.assertEqual(store.run('r3')['status'], 'needs_reconcile')
            store.close()


if __name__ == '__main__':
    unittest.main()
