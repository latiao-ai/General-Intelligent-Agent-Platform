"""公开 URL、资料落盘、检索后端、Markdown/HTML 与上下文预算。"""
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.core_support import patch_all_emits
from webpage_agent.core.services import clean_public_url


class CoreTests(unittest.TestCase):
    def test_reject_private_url(self):
        for url in ['http://127.0.0.1/', 'http://192.168.1.1/',
                    'http://localhost/', 'file:///etc/passwd', 'http://[::1]/']:
            with self.assertRaises(ValueError):
                clean_public_url(url)


class SourceStoreTests(unittest.TestCase):
    """P0：正文落盘，state 里只留元数据。"""

    def test_text_roundtrip_and_metadata(self):
        from webpage_agent.core import sources
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
        from webpage_agent.core import sources
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
        from webpage_agent.core import sources
        from webpage_agent.capabilities import web as web_cap

        async def fake_search(query):
            return {'results': [{'url': 'https://example.com/a', 'title': '甲文',
                                 'content': '摘要', 'raw_content': '正文' * 400}],
                    'usage': {}}

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sources, 'settings',
                              SimpleNamespace(artifact_dir=Path(folder))), \
                    patch.object(web_cap.search_backend, 'search', fake_search), \
                    patch_all_emits(lambda e: None):
                result = await web_cap._web_search(
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
        from webpage_agent.core import sources
        from webpage_agent.capabilities import web as web_cap

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
                result = await web_cap._fetch_pages(
                    {'urls': ['https://example.com/a']},
                    {'task_id': 't1', 'user_input': '阅读 https://example.com/a',
                         'documents': [doc], 'errors': [],
                         'usage': []})
        self.assertEqual(result['documents'][0]['status'], 'success')
        self.assertGreater(result['documents'][0]['chars'], 0)

    async def test_fetch_pages_is_only_for_urls_in_the_user_input(self):
        """调用时机只有一条：输入里有 URL。

        "为没有正文的搜索结果补抓"是个不存在的场景（检索接口自带正文），
        规则里留着它只会让规划器以为还有第二条路。
        """
        from webpage_agent.capabilities import web as web_cap
        from webpage_agent.tools import core as tool_module
        rules = web_cap.PROMPT_RULES
        self.assertIn('用户输入里出现了 URL', rules)
        self.assertNotIn('补抓', rules)
        self.assertNotIn('补抓', tool_module.BUILTIN_BY_NAME['fetch_pages'].description)

    async def test_fetch_requires_user_supplied_urls_before_network(self):
        from webpage_agent.capabilities import web as web_cap
        from webpage_agent.capabilities.base import ToolError

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
        from webpage_agent.core import sources
        from webpage_agent.capabilities import web as web_cap

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
        from webpage_agent.core import sources
        from webpage_agent.capabilities import web as web_cap

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

    async def test_select_sources_reads_text_from_disk(self):
        from webpage_agent.core import sources
        from webpage_agent.tools import core as tool_module
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
        from webpage_agent.core import md
        out = md.to_html('# 标题\n\n正文\n\n- a\n- b\n\n1. x\n2. y\n')
        self.assertIn('<h1>标题</h1>', out)
        self.assertIn('<p>正文</p>', out)
        self.assertIn('<ul><li>a</li><li>b</li></ul>', out)
        self.assertIn('<ol><li>x</li><li>y</li></ol>', out)

    def test_inline_and_code_fence(self):
        from webpage_agent.core import md
        out = md.to_html('**粗** 与 `code`\n\n```py\nprint(1)\n```')
        self.assertIn('<strong>粗</strong>', out)
        self.assertIn('<code>code</code>', out)
        self.assertIn('language-py', out)
        self.assertIn('print(1)', out)

    def test_handles_backslash_escapes(self):
        """DeepSeek 的 fetch 返回大量反斜杠转义，必须还原成字面字符。"""
        from webpage_agent.core import md
        out = md.to_html(r'\# 1\. Overview 与 \*字面星号\*')
        self.assertIn('# 1. Overview', out)
        self.assertIn('*字面星号*', out)
        self.assertNotIn('\\#', out)

    def test_escapes_untrusted_html(self):
        """来源是任意网页，正文必须先转义——否则我们等于在替它执行脚本。"""
        from webpage_agent.core import md
        out = md.to_html('<script>alert(1)</script>\n\n[点我](javascript:alert(1))')
        self.assertNotIn('<script>', out)
        self.assertIn('&lt;script&gt;', out)
        # javascript: 伪协议不该被拼成可点链接
        self.assertNotIn('href="javascript', out)


class RuntimeKeyCheckTests(unittest.TestCase):
    """启动自检要提示缺哪个 key，而不是等到第一次检索才 401。"""

    def test_missing_deepseek_key_is_reported(self):
        from webpage_agent.core import config as config_module
        from webpage_agent.core.config import Settings
        fake = Settings()                       # 字段来自当前 .env
        object.__setattr__(fake, 'deepseek_api_key', '')
        with patch.object(config_module, 'settings', fake):
            with self.assertRaises(RuntimeError) as ctx:
                config_module.check_runtime_keys()
        self.assertIn('DEEPSEEK_API_KEY', str(ctx.exception))


class SearchBackendTests(unittest.IsolatedAsyncioTestCase):
    """检索后端：DeepSeek 官方接口归一化成 tools 使用的形状。"""

    async def test_deepseek_search_normalises_payload(self):
        from webpage_agent.integrations import search
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
        from webpage_agent.integrations import search

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
        from webpage_agent.core import sources
        from webpage_agent.capabilities import export as export_cap
        with tempfile.TemporaryDirectory() as folder:
            fake = SimpleNamespace(artifact_dir=Path(folder))
            with patch.object(sources, 'settings', fake), \
                    patch.object(export_cap, 'settings', fake), \
                    patch_all_emits(lambda e: None):
                doc = sources.metadata('t1', 's1', url='https://a/1', title='甲文',
                                       text='# 小标题\n\n正文内容')
                result = await export_cap._render_sources_html(
                    {'title': '我的资料'}, {'task_id': 't1', 'documents': [doc]})
                html = Path(result['artifact']['html_path']).read_text('utf-8')
        self.assertEqual(result['status'], 'rendered')
        self.assertEqual(result['sources_page']['count'], 1)
        self.assertEqual(result['artifact']['kind'], 'sources')
        self.assertIn('我的资料', html)
        self.assertIn('<h1>小标题</h1>', html)     # Markdown 被真正转换了
        self.assertIn('https://a/1', html)

    async def test_refuses_without_documents(self):
        from webpage_agent.tools import core as tool_module
        from webpage_agent.capabilities import export as export_cap
        with self.assertRaises(tool_module.ToolError):
            await export_cap._render_sources_html({}, {'documents': []})


class ContextBuilderTests(unittest.TestCase):
    """P1：按 token 预算装配上下文。"""

    def test_estimate_tokens_prefers_conservative(self):
        from webpage_agent.agent import context
        self.assertGreater(context.estimate_tokens('中文' * 100), 100)
        self.assertGreater(context.estimate_tokens('a' * 100), 0)
        self.assertEqual(context.estimate_tokens(''), 0)

    def test_select_trims_to_budget_but_keeps_one(self):
        from webpage_agent.agent import context
        history = [{'role': 'user', 'content': '很长的内容' * 800}
                   for _ in range(30)]
        picked = context.select(history, 'plan')
        self.assertLess(len(picked), 30)
        self.assertGreaterEqual(len(picked), 1)
        # 只留一条也不会因为超预算就返回空
        huge = [{'role': 'user', 'content': 'x' * 100000}]
        self.assertEqual(len(context.select(huge, 'plan')), 1)

    def test_memory_text_and_system_injection(self):
        from webpage_agent.agent import context
        memory = {'summary': '用户在减脂', 'facts': {'目标': '降到15%'}, 'upto': 12}
        block = context.memory_text(memory)
        self.assertIn('会话摘要', block)
        self.assertIn('降到15%', block)
        self.assertEqual(context.memory_text({}), '')
        merged = context.system_with_memory('基础提示', memory)
        self.assertTrue(merged.startswith('基础提示'))
        self.assertIn('会话摘要', merged)
        self.assertEqual(context.system_with_memory('基础提示', {}), '基础提示')


class ContextBudgetTests(unittest.TestCase):
    """完整上下文预算与截断披露（文档 §11）。"""

    def test_budget_report_lists_every_section(self):
        from webpage_agent.agent import context
        built = context.build(purpose='final_answer', rules='规则',
                              request='问题', tools='工具', evidence='证据')
        sections = built['report']['sections']
        for key in ('rules', 'request', 'tools', 'evidence', 'memory', 'history'):
            self.assertIn(key, sections)
        self.assertEqual(built['report']['purpose'], 'final_answer')

    def test_history_is_trimmed_and_flagged(self):
        from webpage_agent.agent import context
        history = [{'role': 'user', 'content': '很长的历史' * 500}
                   for _ in range(8)]
        built = context.build(purpose='plan', rules='x', history=history)
        self.assertLess(len(built['messages']), 8)
        self.assertIn('history', built['report']['truncated'])

    def test_oversized_rules_are_reported_not_silently_cut(self):
        """规则+工具 schema 已超预算时必须报出来，不能悄悄截断关键约束。"""
        from webpage_agent.agent import context
        built = context.build(purpose='plan', rules='约束' * 3000,
                              tools='工具' * 3000)
        self.assertTrue(built['report']['over_budget'])

    def test_each_purpose_has_its_own_budget(self):
        from webpage_agent.agent import context
        self.assertGreater(context.PURPOSE_BUDGETS['transform'],
                           context.PURPOSE_BUDGETS['plan'])
        for purpose in context.PURPOSES:
            self.assertIn(purpose, context.PURPOSE_BUDGETS)


if __name__ == '__main__':
    unittest.main()
