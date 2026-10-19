import os
import json
import time
import base64
import asyncio
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pydantic import ValidationError
from app.schemas import ExecutionPlan, PlanStep, Report
from app.services import clean_public_url
from app.rendering import report_errors, render_report
from app import mcp_adapter, nodes


def report_fixture():
    return Report.model_validate({
        'title': '测试报告', 'summary': '资料摘要', 'summary_source_ids': ['s1'],
        'sections': [{'heading': '特点', 'paragraphs': [
            {'text': '<script>alert(1)</script>', 'source_ids': ['s1']}
        ]}],
        'comparison_table': {'columns': ['名称'], 'rows': [
            {'cells': ['示例'], 'source_ids': ['s1']}
        ]}, 'limitations': [],
    })


def source_fixture():
    return [{'source_id': 's1', 'url': 'https://example.com/',
             'title': '示例', 'status': 'success', 'fetched_at': 'test',
             'raw_content': '用于测试的正文'}]


class CoreTests(unittest.TestCase):
    def test_reject_private_url(self):
        for url in ['http://127.0.0.1/', 'http://192.168.1.1/',
                    'http://localhost/', 'file:///etc/passwd', 'http://[::1]/']:
            with self.assertRaises(ValueError):
                clean_public_url(url)

    def test_unknown_citation(self):
        report = report_fixture()
        report.sections[0].paragraphs[0].source_ids = ['unknown']
        self.assertTrue(report_errors(report, source_fixture()))

    def test_table_shape(self):
        report = report_fixture()
        report.comparison_table.rows[0].cells = ['a', 'b']
        self.assertTrue(report_errors(report, source_fixture()))

    def test_autoescape(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('app.rendering.settings') as config:
                config.artifact_dir = Path(folder)
                artifact = render_report('testtask', report_fixture(), source_fixture())
                html = Path(artifact['html_path']).read_text(encoding='utf-8')
                self.assertNotIn('<script>alert(1)</script>', html)
                self.assertIn('&lt;script&gt;', html)

    def test_source_budget_keeps_multiple_sources(self):
        """总量预算不能只够装一个来源，否则报告退化成单一来源。"""
        from app import tools as tool_module
        from app.config import settings
        docs = [{'source_id': f's{i}', 'title': f't{i}', 'status': 'success',
                 'raw_content': 'x' * 20000} for i in range(1, 5)]
        selected, truncated = tool_module._select_sources({'documents': docs})
        self.assertGreaterEqual(len(selected), 2)
        self.assertLessEqual(sum(len(s['text']) for s in selected),
                             settings.max_total_chars)
        self.assertTrue(truncated)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    """Planner-Executor：计划决定走哪条路，执行器逐步落实。"""

    def test_next_step_follows_the_cursor(self):
        from app import nodes
        plan = {'steps': [{'tool': 'web_search'}, {'tool': 'render_html'}]}
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 0}), 'run_step')
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 1}), 'run_step')
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 2}), 'end')
        self.assertEqual(nodes.next_step({}), 'end')

    def test_final_status_maps_internal_states(self):
        from app import nodes
        self.assertEqual(nodes.final_status({'answer': 'hi'}), 'answered')
        self.assertEqual(nodes.final_status(
            {'deployment': {'status': 'success'}}), 'deployed')
        self.assertEqual(nodes.final_status({'deployment': {'status': 'pending'}}),
                         'deploy_needs_check')
        self.assertEqual(nodes.final_status({'artifact': {'html_path': 'x'}}),
                         'local_ready')
        self.assertEqual(nodes.final_status({}), 'failed')

    def test_valid_steps_drops_unknown_tools_and_bad_args(self):
        from app import nodes
        from app import tools as tool_module
        kept = nodes._valid_steps(
            [{'tool': 'web_search', 'args': {'query': 'q'}},
             {'tool': 'not_a_tool', 'args': {}},
             {'tool': 'render_html', 'args': 'not-a-dict'}],
            tool_module.BUILTIN_TOOLS)
        self.assertEqual([s['tool'] for s in kept], ['web_search', 'render_html'])
        self.assertEqual(kept[1]['args'], {})

    def test_fallback_plan_respects_given_urls(self):
        from app import nodes
        from app import tools as tool_module
        with_url = nodes.fallback_steps({'user_input': '看看 https://example.com/a'})
        self.assertEqual(with_url[0]['tool'], 'fetch_pages')
        self.assertEqual(with_url[0]['args']['urls'], ['https://example.com/a'])
        research = nodes.fallback_steps({'user_input': '整理一份 SQLite 调研报告'})
        self.assertEqual(research[0]['tool'], 'web_search')
        # 兜底计划里的工具必须真实存在，否则整轮会被跳过
        known = {t.name for t in tool_module.BUILTIN_TOOLS}
        self.assertTrue({s['tool'] for s in with_url} <= known)
        self.assertTrue({s['tool'] for s in research} <= known)

    def test_fallback_ladder_matches_requested_output(self):
        """兜底要分清"问个明白"和"要一份文档"，两者的收尾工具不同。"""
        from app import nodes
        ask = nodes.fallback_steps({'user_input': '查询如何降低体脂'})
        self.assertEqual([s['tool'] for s in ask],
                         ['web_search', 'fetch_pages', 'answer_with_sources'])
        report = nodes.fallback_steps({'user_input': '生成一份降低体脂的 HTML 报告'})
        self.assertEqual([s['tool'] for s in report],
                         ['web_search', 'fetch_pages', 'compose_report', 'render_html'])
        # 已经给了链接就别再重复抓一次
        link = nodes.fallback_steps({'user_input': '看看 https://example.com/a 说了什么'})
        self.assertEqual([s['tool'] for s in link],
                         ['fetch_pages', 'answer_with_sources'])

    def test_fallback_does_not_research_unrelated_input(self):
        """兜底计划不能一律走检索。

        早先这里无条件检索：用户发一句"暂停"，规划又恰好失败，就会去搜"暂停"
        并出一份报告——比答得不好糟得多。
        """
        from app import nodes
        for text in ['暂停', '你好', '介绍一下你自己', '现在是几点']:
            steps = nodes.fallback_steps({'user_input': text})
            self.assertEqual([s['tool'] for s in steps], ['answer_directly'], text)

    async def test_plan_asks_the_model_and_keeps_its_steps(self):
        from app import nodes
        from app import tools as tool_module
        captured = {}

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            captured['user'] = user
            return ExecutionPlan.model_validate({'steps': [
                {'tool': 'web_search', 'args': {'query': 'sqlite'},
                 'why': '需要外部资料'},
                {'tool': 'render_html', 'args': {}, 'why': '产出 HTML'},
            ], 'summary': '先搜再渲染'}), {'total_tokens': 3}

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            result = await nodes.plan({'user_input': '整理 SQLite 报告', 'usage': []})

        tools_used = [s['tool'] for s in result['plan']['steps']]
        self.assertEqual(tools_used, ['web_search', 'render_html'])
        self.assertEqual(result['step_index'], 0)
        self.assertEqual(result['usage'][-1]['stage'], 'plan')
        # 工具清单必须真的进了提示词，否则模型只能瞎猜
        self.assertIn('web_search', captured['user'])
        self.assertIn('answer_directly', captured['user'])
        self.assertIn('answer_with_sources', captured['user'])
        # 必须告诉模型手上还有多少料，否则它会重复检索、或空手去整理
        self.assertIn('已有资料', captured['user'])

    async def test_plan_falls_back_when_the_model_fails(self):
        from app import nodes
        from app import tools as tool_module

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def boom(*a, **k):
            raise RuntimeError('模型不可用')

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', boom):
            result = await nodes.plan({'user_input': '整理 SQLite 报告', 'usage': []})
        # 规划失败不能让任务空手而归：要有可执行的兜底计划
        self.assertGreaterEqual(len(result['plan']['steps']), 2)
        self.assertEqual(result['plan']['steps'][0]['tool'], 'web_search')

    async def test_plan_skips_the_model_for_small_talk(self):
        from app import nodes
        from app import tools as tool_module

        async def forbidden(*a, **k):
            raise AssertionError('寒暄不应调用模型')

        async def fake_catalog():
            raise AssertionError('寒暄不应去发现工具')

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', forbidden):
            result = await nodes.plan({'user_input': '你好', 'usage': []})
        self.assertEqual([s['tool'] for s in result['plan']['steps']],
                         ['answer_directly'])
        self.assertEqual(result.get('usage', []), [])   # 没调模型就没有用量

    async def test_run_step_runs_exactly_one_tool_and_advances(self):
        from app import nodes
        from app import tools as tool_module
        from app.tools import Tool
        seen = []

        async def runner(args, state):
            seen.append(args)
            return {'answer': '好的', 'status': 'answered'}

        fake = Tool('answer_directly', '直接回答', 'd', {}, runner)
        plan = {'steps': [{'tool': 'answer_directly', 'args': {'question': 'q'}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'answer_directly': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertEqual(seen, [{'question': 'q'}])
        self.assertEqual(result['step_index'], 1)
        self.assertEqual(result['step_label'], '直接回答')
        # 最后一步要把内部状态归一成前端认识的终态
        self.assertEqual(result['status'], 'answered')

    async def test_run_step_aborts_remaining_steps_on_error(self):
        from app import nodes
        from app import tools as tool_module
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


class DeployGateTests(unittest.IsolatedAsyncioTestCase):
    """部署闸门：没有产物、或全局关闭时都不许部署。"""

    async def test_requires_an_artifact(self):
        from app import tools as tool_module
        with self.assertRaises(tool_module.ToolError):
            await tool_module._deploy_site({}, {})

    async def test_requires_global_enable(self):
        from app import tools as tool_module
        with patch.object(tool_module, 'settings',
                          SimpleNamespace(deploy_enabled=False)):
            with self.assertRaises(tool_module.ToolError):
                await tool_module._deploy_site({}, {'artifact': {'html_path': 'x'}})

    async def test_deploys_and_marks_status(self):
        from app import tools as tool_module
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<html></html>', encoding='utf-8')
            artifact = {'html_path': str(html), 'sha256': 'abc'}

            async def fake_deploy(_artifact):
                return {'status': 'success', 'public_url': 'https://x/1'}

            with patch.object(tool_module, 'settings',
                              SimpleNamespace(deploy_enabled=True)), \
                    patch.object(tool_module.mcp_adapter, 'deploy_html', fake_deploy):
                result = await tool_module._deploy_site({}, {'artifact': artifact})
            self.assertEqual(result['status'], 'deployed')
            saved = json.loads((Path(folder) / 'deployment.json').read_text('utf-8'))
            self.assertEqual(saved['public_url'], 'https://x/1')

    async def test_deploy_falls_back_to_prior_artifact(self):
        """界面没有部署按钮了，用户单说一句"部署到服务器"也要能落到上一轮的产物上。"""
        from app import tools as tool_module
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<html></html>', encoding='utf-8')
            prior = {'html_path': str(html), 'sha256': 'p'}

            async def fake_deploy(_artifact):
                return {'status': 'success', 'public_url': 'https://x/9'}

            seen = []
            with patch.object(tool_module, 'settings',
                              SimpleNamespace(deploy_enabled=True)), \
                    patch.object(tool_module.mcp_adapter, 'deploy_html', fake_deploy), \
                    patch.object(tool_module, 'emit', seen.append):
                result = await tool_module._deploy_site({}, {'prior_artifact': prior})
            self.assertEqual(result['status'], 'deployed')
            # 必须明确告知复用了上一轮产物，避免误以为这轮重新生成过
            self.assertTrue(any(e.get('label') == '复用上一轮产物' for e in seen))

    async def test_needs_check_when_tool_reports_otherwise(self):
        from app import tools as tool_module
        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<html></html>', encoding='utf-8')

            async def fake_deploy(_artifact):
                return {'status': 'pending', 'public_url': 'https://x/2'}

            with patch.object(tool_module, 'settings',
                              SimpleNamespace(deploy_enabled=True)), \
                    patch.object(tool_module.mcp_adapter, 'deploy_html', fake_deploy):
                result = await tool_module._deploy_site(
                    {}, {'artifact': {'html_path': str(html), 'sha256': 'a'}})
            self.assertEqual(result['status'], 'deploy_needs_check')


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
                                prior=None):
            if captured is not None:
                captured['history'] = history
            if on_start:
                on_start('artifact123')
            if on_event:
                on_event({'type': 'action', 'icon': '🌐', 'label': '已搜索网页',
                          'detail': '测试'})
            if delay:
                await asyncio.sleep(delay)
            return {'task_id': 'artifact123', 'status': 'local_ready',
                    'deployment': {'status': 'skipped'}, 'timings': []}
        return fake_run_task

    async def test_start_broadcasts_events_and_records_messages(self):
        import app.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub()):
            record = hub.start('测试需求')
            await asyncio.wait_for(record._task, timeout=10)
        self.assertEqual(record.status, 'local_ready')
        self.assertEqual(record.artifact_id, 'artifact123')
        labels = [e.get('label') for e in self.events_of(record)]
        self.assertIn('已搜索网页', labels)
        # 会话里要留下 user + assistant 两条，下一轮才能还原指代型要求。
        self.assertEqual([m['role'] for m in hub.history(record.session_id)],
                         ['user', 'assistant'])
        # 状态也要落库，重启后才能查询。
        self.assertEqual(hub.store.task(record.task_id)['status'], 'local_ready')

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
            self.assertEqual(restored.status, 'local_ready')
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
                       on_start=None, prior=None):
            on_event({'type': 'stage_start', 'stage': 'plan'})
            on_event({'type': 'stage_end', 'stage': 'plan',
                      'seconds': 1.0, 'status': 'planned'})
            # 这一条到达时，"收到指令"应当被补齐
            on_event({'type': 'stage_start', 'stage': 'web_search'})
            return {'task_id': 'a1', 'status': 'local_ready',
                    'deployment': {}, 'timings': []}

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
                       on_start=None, prior=None):
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
                       on_start=None, prior=None):
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
                       on_start=None, prior=None):
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
                       on_start=None, prior=None):
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
                                prior=None):
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
                                prior=None):
            return {'task_id': 'a1', 'status': 'local_ready',
                    'deployment': {}, 'timings': []}

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
                                prior=None):
            if on_start:
                on_start('a1')
            if on_event:
                on_event({'type': 'stage_start', 'stage': 'compose_report',
                          'label': '整理报告', 'at': 10.0})
                on_event({'type': 'action', 'icon': '🌐', 'label': '已搜索网页'})
                on_event({'type': 'stage_end', 'stage': 'compose_report',
                          'label': '整理报告', 'seconds': 2.5, 'status': '完成'})
            return {'task_id': 'a1', 'status': 'local_ready',
                    'deployment': {}, 'timings': []}

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
        self.assertEqual(turn['status'], 'local_ready')
        self.assertEqual(turn['message'], '查一下')
        types = [(e['type'], e.get('stage')) for e in turn['events']]
        self.assertIn(('stage_start', 'compose_report'), types)
        self.assertIn(('stage_end', 'compose_report'), types)
        ends = [e for e in turn['events'] if e['type'] == 'stage_end'
                and e['stage'] == 'compose_report']
        self.assertEqual(ends[0]['seconds'], 2.5)

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
        text = reply_text({'status': 'local_ready', 'task_id': 'abc',
                           'deployment': {'status': 'skipped'}})
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
        lines = format_timings([{'stage': 'compose_report', 'seconds': 8.4,
                                 'status': 'composed'}])
        self.assertEqual(len(lines), 1)
        self.assertIn('整理报告', lines[0])
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
        captured = {}

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            captured.update(kw)
            return ExecutionPlan.model_validate(
                {'steps': [{'tool': 'answer_directly', 'args': {}}]}), None

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            await nodes.plan({'user_input': '介绍一下 SQLite', 'usage': []})
        from app.config import settings
        self.assertEqual(captured['disable_thinking'],
                         settings.llm_parse_disable_thinking)


class ComposeToolTests(unittest.IsolatedAsyncioTestCase):
    """整理报告工具：模型选择、推理开关、来源预算。"""

    async def test_compose_never_disables_thinking(self):
        from app import tools as tool_module
        captured = {}

        async def fake_call(schema, system, user, **kw):
            captured.update(kw)
            return Report.model_validate({
                'title': 't', 'summary': 's', 'summary_source_ids': ['s1'],
                'sections': [{'heading': 'h', 'paragraphs': [
                    {'text': 'x', 'source_ids': ['s1']}]}]}), None

        state = {'documents': [{'source_id': 's1', 'title': 't', 'status': 'success',
                                'url': 'https://e.com/', 'raw_content': '正文'}],
                 'usage': []}
        with patch.object(tool_module, 'structured_call', fake_call):
            await tool_module._compose_report({}, state)
        # 关掉推理会让整理阶段产不出合法的 tool_call，这里必须保持 False
        self.assertFalse(captured.get('disable_thinking', False))
        from app.config import settings
        self.assertEqual(captured['model'], settings.llm_compose_model)

    async def test_compose_records_model_name(self):
        from app import tools as tool_module
        from app.config import settings

        async def fake_call(schema, system, user, **kw):
            return Report.model_validate({
                'title': 't', 'summary': 's', 'summary_source_ids': ['s1'],
                'sections': [{'heading': 'h', 'paragraphs': [
                    {'text': 'x', 'source_ids': ['s1']}]}]}), {'total_tokens': 1}

        state = {'documents': [{'source_id': 's1', 'title': 't', 'status': 'success',
                                'url': 'https://example.com/', 'raw_content': '正文'}],
                 'usage': []}
        with patch.object(tool_module, 'structured_call', fake_call):
            result = await tool_module._compose_report({}, state)
        self.assertEqual(result['usage'][-1]['model_name'], settings.llm_compose_model)

    def test_search_supplied_bodies_count_as_usable(self):
        """web_search 常直接带回正文，此时 status 仍是 pending。

        只认 success 会让"搜索 → 作答/成稿"这种少一步的计划误报"还没有可用正文"
        ——实测就因此让一轮查询整条失败（0.01s 就报错）。
        """
        from app import tools as tool_module
        docs = [{'source_id': 's1', 'title': 't', 'url': 'https://a/1',
                 'status': 'pending', 'raw_content': '检索时就带回来的正文'}]
        usable, origin = tool_module.usable_documents({'documents': docs})
        self.assertEqual(len(usable), 1)
        # 用的时候要提升成 success，否则 render_html 不会把它列进参考资料
        self.assertEqual(usable[0]['status'], 'success')
        # 没有正文的不算
        empty, _ = tool_module.usable_documents({'documents': [
            {'source_id': 's2', 'status': 'pending', 'raw_content': ''}]})
        self.assertEqual(empty, [])
        failed, _ = tool_module.usable_documents({'documents': [
            {'source_id': 's3', 'status': 'failed', 'raw_content': '正文'}]})
        self.assertEqual(failed, [])

    async def test_compose_reuses_prior_documents(self):
        """"把上面的内容整理成 HTML"：本轮没检索，要能接住上一轮的正文。"""
        from app import tools as tool_module
        captured = {}

        async def fake_call(schema, system, user, **kw):
            captured['user'] = user
            return Report.model_validate({
                'title': 't', 'summary': 's', 'summary_source_ids': ['s1'],
                'sections': [{'heading': 'h', 'paragraphs': [
                    {'text': 'x', 'source_ids': ['s1']}]}]}), None

        state = {'documents': [], 'usage': [], 'prior_documents': [
            {'source_id': 's1', 'title': '甲文', 'url': 'https://a.example/1',
             'status': 'success', 'raw_content': '上一轮的正文'}]}
        with patch.object(tool_module, 'structured_call', fake_call):
            result = await tool_module._compose_report({}, state)
        self.assertIn('上一轮的正文', captured['user'])
        # 用到的来源必须写回 state，否则 render_html 解析不到 source_id
        self.assertEqual([d['source_id'] for d in result['documents']], ['s1'])

    def test_compose_prompt_bounds_the_output_size(self):
        """篇幅约束是整理阶段的主要耗时开关。

        实测同一份输入的输出 token：不约束 14400（64.7s）→ 约束后 6084（28.6s）。
        这条指令被删掉，整理阶段就会重新变慢并撞上单次超时。
        """
        from app import tools as tool_module
        self.assertIn('章节 3~5 个', tool_module.COMPOSE_SYSTEM)
        self.assertIn('不超过 120 字', tool_module.COMPOSE_SYSTEM)
        self.assertIn('对比表最多 6 行', tool_module.COMPOSE_SYSTEM)

    async def test_compose_refuses_without_sources(self):
        from app import tools as tool_module
        with self.assertRaises(tool_module.ToolError):
            await tool_module._compose_report({}, {'documents': [], 'usage': []})

    async def test_render_requires_a_report(self):
        from app import tools as tool_module
        with self.assertRaises(tool_module.ToolError):
            await tool_module._render_html({}, {'task_id': 'x', 'documents': []})


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

        async def fake_catalog():
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            return ExecutionPlan.model_validate({
                'steps': [{'tool': 'answer_directly', 'args': {'question': 'q'}}],
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


class AnswerWithSourcesTests(unittest.IsolatedAsyncioTestCase):
    """联网作答：基于正文回答并附来源，这是"直答"与"出报告"之间的那条路。"""

    DOCS = [
        {'source_id': 's1', 'title': '甲文', 'url': 'https://a.example/1',
         'status': 'success', 'raw_content': '正文一'},
        {'source_id': 's2', 'title': '乙文', 'url': 'https://b.example/2',
         'status': 'success', 'raw_content': '正文二'},
    ]

    async def test_refuses_without_sources(self):
        from app import tools as tool_module
        with self.assertRaises(tool_module.ToolError):
            await tool_module._answer_with_sources(
                {'question': 'q'}, {'documents': [], 'usage': []})

    async def test_lists_only_cited_sources(self):
        from app import tools as tool_module

        async def fake_chat(system, messages, model=None, on_delta=None):
            return '结论一 [s1]。'

        with patch.object(tool_module, 'chat_reply', fake_chat):
            result = await tool_module._answer_with_sources(
                {'question': '问题'}, {'documents': self.DOCS, 'usage': []})
        self.assertEqual(result['status'], 'answered')
        self.assertIn('参考来源', result['answer'])
        self.assertIn('https://a.example/1', result['answer'])
        # 没被引用的来源不该混进来
        self.assertNotIn('https://b.example/2', result['answer'])

    async def test_lists_all_when_model_cites_nothing(self):
        from app import tools as tool_module

        async def fake_chat(system, messages, model=None, on_delta=None):
            return '一句没有任何标注的结论。'

        with patch.object(tool_module, 'chat_reply', fake_chat):
            result = await tool_module._answer_with_sources(
                {'question': '问题'}, {'documents': self.DOCS, 'usage': []})
        # 一条都没标注时退回全列，别让"附来源"变成空话
        self.assertIn('https://a.example/1', result['answer'])
        self.assertIn('https://b.example/2', result['answer'])

    async def test_streams_answer_deltas(self):
        from app import tools as tool_module
        seen = []

        async def fake_chat(system, messages, model=None, on_delta=None):
            if on_delta:
                on_delta('增量')
            return '增量'

        with patch.object(tool_module, 'chat_reply', fake_chat), \
                patch.object(tool_module, 'emit', seen.append):
            await tool_module._answer_with_sources(
                {'question': '问题'}, {'documents': self.DOCS, 'usage': []})
        self.assertTrue(any(e.get('type') == 'answer_delta' for e in seen))


class AnswerToolTests(unittest.IsolatedAsyncioTestCase):
    """直接回答工具：流式增量 + 多轮上下文。"""

    async def test_streams_deltas(self):
        from app import tools as tool_module
        pieces = ('你好', '，有什么', '可以帮你？')

        async def fake_chat(system, messages, model=None, on_delta=None):
            for piece in pieces:
                if on_delta:
                    on_delta(piece)
            return ''.join(pieces)

        seen = []
        with patch.object(tool_module, 'chat_reply', fake_chat), \
                patch.object(tool_module, 'emit', seen.append):
            result = await tool_module._answer_directly(
                {'question': '你好'}, {'user_input': '你好', 'history': []})
        self.assertEqual(result['answer'], ''.join(pieces))
        self.assertEqual(result['status'], 'answered')
        self.assertTrue(any(e.get('type') == 'answer_delta' for e in seen))

    async def test_tool_calls_record_their_arguments(self):
        """参数必须记录下来。

        list_sites 带 keyword 时只返回匹配项；不记参数就分不清"列表只有 1 个"
        和"只匹配到 1 个"——这个坑导致过一条错误的"删除后剩余站点"汇报。
        """
        from app import tools as tool_module

        async def fake_call(tool_name, arguments, artifact=None):
            return {'deleted': tool_name}

        state = {'usage': []}
        with patch.object(tool_module.mcp_adapter, 'call_tool', fake_call), \
                patch.object(tool_module, 'emit', lambda e: None):
            runner = tool_module._make_mcp_runner('delete_site')
            result = await runner({'site_id': 'y6XbM-lg'}, state)
        call = result['mcp_calls'][0]
        self.assertEqual(call['tool'], 'delete_site')
        self.assertEqual(call['args'], {'site_id': 'y6XbM-lg'})
        self.assertIn('deleted', call['payload'])

    def test_tool_results_show_args_and_order(self):
        from app import tools as tool_module
        state = {'mcp_calls': [
            {'tool': 'list_sites', 'args': {'keyword': 'y6XbM-lg'},
             'payload': {'total': 1}},
            {'tool': 'delete_site', 'args': {'site_id': 'y6XbM-lg'},
             'payload': {'status': 'deleted'}}]}
        text = tool_module.tool_results_text(state)
        self.assertIn('keyword', text)
        self.assertIn('第1次工具调用', text)
        self.assertIn('第2次工具调用', text)

    async def test_uses_tool_results_from_earlier_steps(self):
        """"先调 MCP 取数据、再作答"：第二步必须看得见第一步的返回。

        否则模型会如实回答"你没提供给我"——这个坑在查询服务器状态时必现。
        """
        from app import tools as tool_module
        captured = {}

        async def fake_chat(system, messages, model=None, on_delta=None):
            captured['system'] = system
            captured['content'] = messages[-1]['content']
            return '已部署 3 个站点。'

        state = {'user_input': '服务器上部署了几个站点', 'history': [],
                 'mcp_calls': [{'tool': 'list_sites', 'payload': {
                     'sites': [{'site_id': 'a'}, {'site_id': 'b'}, {'site_id': 'c'}]}}]}
        with patch.object(tool_module, 'chat_reply', fake_chat):
            await tool_module._answer_directly({'question': '几个站点'}, state)
        self.assertIn('list_sites', captured['content'])
        self.assertIn('"site_id": "a"', captured['content'])
        # 系统提示必须切换成"基于工具数据作答"，否则模型仍会说自己没能力
        self.assertIn('工具返回', captured['system'])
        # 还要堵住"把过滤结果当全量"这个坑
        self.assertIn('不是全量清单', captured['system'])

    async def test_without_tool_results_keeps_plain_prompt(self):
        from app import tools as tool_module
        captured = {}

        async def fake_chat(system, messages, model=None, on_delta=None):
            captured['system'] = system
            captured['content'] = messages[-1]['content']
            return 'ok'

        with patch.object(tool_module, 'chat_reply', fake_chat):
            await tool_module._answer_directly(
                {'question': '你好'}, {'user_input': '你好', 'history': []})
        self.assertNotIn('工具返回的数据', captured['content'])

    def test_tool_results_are_capped(self):
        from app import tools as tool_module
        huge = {'tool': 'list_sites', 'payload': {'sites': [{'x': 'y' * 5000}]}}
        text = tool_module.tool_results_text({'mcp_calls': [huge]})
        self.assertLessEqual(len(text), 4200)

    async def test_passes_history(self):
        from app import tools as tool_module
        captured = {}

        async def fake_chat(system, messages, model=None, on_delta=None):
            captured['messages'] = messages
            return 'ok'

        history = [{'role': 'user', 'content': '上一问'},
                   {'role': 'assistant', 'content': '上一答'}]
        with patch.object(tool_module, 'chat_reply', fake_chat):
            await tool_module._answer_directly(
                {'question': '追问'}, {'user_input': '追问', 'history': history})
        contents = [m['content'] for m in captured['messages']]
        self.assertEqual(contents, ['上一问', '上一答', '追问'])


class GraphShapeTests(unittest.IsolatedAsyncioTestCase):
    """整图跑通：寒暄不检索，规划出来的步骤被逐步执行。"""

    async def test_small_talk_answers_without_searching(self):
        from app import tools as tool_module
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
        self.assertEqual(result['status'], 'answered')
        self.assertFalse(touched['searched'])

    async def test_plan_steps_run_in_order(self):
        from app import nodes
        from app import tools as tool_module
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


if __name__ == '__main__':
    unittest.main()
