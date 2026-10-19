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
from app.schemas import Plan, Report
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

    def test_research_bound(self):
        state = {'documents': [], 'plan': {'mode': 'topic'}, 'research_round': 0}
        self.assertEqual(nodes.assess_coverage(state), 'discover_sources')
        state['research_round'] = 1
        self.assertEqual(nodes.assess_coverage(state), 'finish_failed')
        state['plan']['mode'] = 'urls'
        state['research_round'] = 0
        self.assertEqual(nodes.assess_coverage(state), 'finish_failed')


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_sources_never_deploys(self):
        from app.graph import build_graph
        async def parse(state):
            return {'plan': {'mode': 'topic', 'topic': 'test', 'queries': ['test'],
                             'urls': [], 'need_research': True}}
        async def search(query):
            return {'results': [], 'usage': {'credits': 1}}
        with patch.object(nodes, 'parse_request', parse), \
             patch.object(nodes, 'search_query', search), \
             patch.object(nodes, 'deploy_report') as deploy:
            result = await build_graph().ainvoke({
                'task_id': 'testtask', 'user_input': 'test', 'documents': [],
                'errors': [], 'usage': [], 'research_round': 0, 'searched': False,
            })
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['research_round'], 1)
            deploy.assert_not_called()

FILES_SCHEMA = {
    'properties': {
        'files': {'type': 'array', 'items': {'properties': {'path': {}, 'content': {}}}}
    }
}
TEXT_SCHEMA = {'properties': {'html': {'type': 'string'}}}


class DeployGateTests(unittest.IsolatedAsyncioTestCase):
    """部署只在用户明确要求且总开关打开时才自动执行。"""

    def test_auto_deploy_must_be_explicit(self):
        # 必须是必填字段：带默认值时模型会省略该字段，被默认值静默兜成 False，
        # 导致"生成报告后部署到服务器"这类明确诉求永远识别不出来。
        self.assertTrue(Plan.model_fields['auto_deploy'].is_required())
        # need_research 同理：带默认值会被模型省略，导致"该搜的没搜"
        self.assertTrue(Plan.model_fields['need_research'].is_required())
        with self.assertRaises(ValidationError):
            Plan.model_validate({'topic': '对比数据库的部署方式'})
        plan = Plan.model_validate({'topic': '对比数据库的部署方式', 'auto_deploy': False,
                                 'need_research': True})
        self.assertFalse(plan.auto_deploy)

    async def test_skipped_when_not_requested(self):
        with patch.object(nodes, 'settings', SimpleNamespace(deploy_enabled=True)):
            result = await nodes.deploy_report(
                {'artifact': {}, 'plan': {'auto_deploy': False}})
        self.assertEqual(result['status'], 'local_ready')
        self.assertEqual(result['deployment'],
                         {'status': 'skipped', 'reason': 'not_requested'})

    async def test_skipped_when_globally_disabled(self):
        with patch.object(nodes, 'settings', SimpleNamespace(deploy_enabled=False)):
            result = await nodes.deploy_report(
                {'artifact': {}, 'plan': {'auto_deploy': True}})
        self.assertEqual(result['status'], 'local_ready')
        self.assertEqual(result['deployment']['reason'], 'deploy_disabled')

    async def test_runs_when_requested(self):
        calls = []

        async def fake_deploy(artifact):
            calls.append(artifact)
            return {'status': 'success', 'public_url': 'https://example.com/s/',
                    'content_hash': 'h'}

        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<html></html>', encoding='utf-8')
            with patch.object(nodes, 'settings', SimpleNamespace(deploy_enabled=True)), \
                 patch.object(nodes, 'deploy_html', fake_deploy):
                result = await nodes.deploy_report({
                    'artifact': {'html_path': str(html)}, 'plan': {'auto_deploy': True}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'deployed')
        self.assertEqual(result['deployment']['public_url'], 'https://example.com/s/')

    async def test_failure_marks_needs_check(self):
        async def boom(artifact):
            raise RuntimeError('连接中断')

        with tempfile.TemporaryDirectory() as folder:
            html = Path(folder) / 'index.html'
            html.write_text('<html></html>', encoding='utf-8')
            with patch.object(nodes, 'settings', SimpleNamespace(deploy_enabled=True)), \
                 patch.object(nodes, 'deploy_html', boom):
                result = await nodes.deploy_report({
                    'artifact': {'html_path': str(html)}, 'plan': {'auto_deploy': True}})
        self.assertEqual(result['status'], 'deploy_needs_check')
        self.assertEqual(result['deployment']['status'], 'needs_check')


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
                                history=None, on_start=None):
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
        """「收到指令 → 开始搜索」应排在阶段表第一行，并在检索开始时补齐耗时。"""
        import app.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None, on_start=None):
            on_event({'type': 'stage_start', 'stage': 'parse_request'})
            on_event({'type': 'stage_end', 'stage': 'parse_request',
                      'seconds': 1.0, 'status': 'planned'})
            # 这一条到达时，"收到指令"应当被补齐
            on_event({'type': 'stage_start', 'stage': 'discover_sources'})
            return {'task_id': 'a1', 'status': 'local_ready',
                    'deployment': {}, 'timings': []}

        with patch.object(hub_module, 'run_task', fake):
            record = hub.start('测试')
            await asyncio.wait_for(record._task, timeout=10)

        events = self.events_of(record)
        starts = [e for e in events if e['type'] == 'stage_start']
        self.assertEqual(starts[0]['stage'], 'instruction')
        self.assertEqual(starts[0]['label'], '收到指令 → 开始搜索')
        ends = [e for e in events
                if e['type'] == 'stage_end' and e['stage'] == 'instruction']
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]['status'], '解析完成')
        search_at = next(i for i, e in enumerate(events)
                         if e['type'] == 'stage_start' and e['stage'] == 'discover_sources')
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

        async def fake(text, progress=None, on_event=None, history=None, on_start=None):
            on_event({'type': 'stage_start', 'stage': 'parse_request'})
            on_event({'type': 'stage_end', 'stage': 'parse_request',
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

        async def fake(text, progress=None, on_event=None, history=None, on_start=None):
            on_event({'type': 'stage_start', 'stage': 'parse_request'})
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
        self.assertEqual(ends.get('parse_request'), '已中断')

    async def test_failed_task_closes_in_flight_stage(self):
        import app.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None, on_start=None):
            on_event({'type': 'stage_start', 'stage': 'compose_report'})
            raise RuntimeError('boom')

        with patch.object(hub_module, 'run_task', fake):
            record = hub.start('坏任务')
            await asyncio.wait_for(record._task, timeout=10)
        ends = {e['stage']: e['status'] for e in self.events_of(record)
                if e['type'] == 'stage_end'}
        self.assertEqual(ends.get('compose_report'), '已失败')
        self.assertEqual(ends.get('instruction'), '已失败')

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

    def test_unknown_task_returns_404(self):
        self.assertEqual(self.client.get('/api/tasks/nope').status_code, 404)
        self.assertEqual(self.client.post('/api/tasks/nope/cancel').status_code, 404)
        self.assertEqual(self.client.get('/api/tasks/nope/artifact').status_code, 404)

    def test_create_task_streams_events_and_ends(self):
        import app.api_hub as hub_module

        async def fake_run_task(text, progress=None, on_event=None,
                                history=None, on_start=None):
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
                                history=None, on_start=None):
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
                                history=None, on_start=None):
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
        self.assertIn('没有生成报告', reply_text({'status': 'failed'}))

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
    """解析阶段关推理：实测约 2 倍提速，且不影响 auto_deploy 判断。"""

    def test_disable_thinking_sets_thinking_disabled(self):
        from app.services import new_llm
        llm = new_llm('m', disable_thinking=True)
        # langchain 把它放进 model_kwargs / extra_body，两者都检查
        body = getattr(llm, 'extra_body', None) or llm.model_kwargs.get('extra_body')
        self.assertEqual(body, {'thinking': {'type': 'disabled'}})

    def test_default_keeps_thinking_enabled(self):
        from app.services import new_llm
        llm = new_llm('m')
        body = getattr(llm, 'extra_body', None) or llm.model_kwargs.get('extra_body')
        # 整理报告阶段依赖推理：默认不能带关闭参数，否则结构化输出会失败
        self.assertIsNone(body)

    async def test_parse_passes_the_flag(self):
        from app.config import settings
        from app import nodes
        captured = {}

        async def fake_call(schema, system, user, model=None, attempts=2,
                            disable_thinking=False, on_draft=None,
                            draft_interval=1.0):
            captured['disable_thinking'] = disable_thinking
            return Plan.model_validate({'topic': 't', 'auto_deploy': False,
                                 'need_research': True}), None

        with patch.object(nodes, 'structured_call', fake_call), \
             patch.object(nodes, 'urls_from_input', lambda text: []):
            await nodes.parse_request({'user_input': 't', 'usage': []})
        self.assertEqual(captured['disable_thinking'],
                         settings.llm_parse_disable_thinking)

    async def test_compose_never_disables_thinking(self):
        from app import nodes
        captured = {}

        async def fake_call(schema, system, user, model=None, attempts=2,
                            disable_thinking=False, on_draft=None,
                            draft_interval=1.0):
            captured['disable_thinking'] = disable_thinking
            return Report.model_validate({
                'title': 't', 'summary': 's', 'summary_source_ids': ['s1'],
                'sections': [{'heading': 'h', 'paragraphs': [
                    {'text': 'x', 'source_ids': ['s1']}]}]}), None

        state = {'documents': [{'source_id': 's1', 'title': 't', 'status': 'success',
                                'url': 'https://e.com/', 'raw_content': '正文'}],
                 'plan': {'mode': 'topic', 'topic': 't'}, 'usage': []}
        with patch.object(nodes, 'structured_call', fake_call):
            await nodes.compose_report(state)
        # 关掉推理会让整理阶段产不出合法的 tool_call（实测 output token 有、
        # 但既无 content 也无 tool_call），所以这里必须是 False
        self.assertFalse(captured['disable_thinking'])

    async def test_compose_keeps_multiple_sources_within_budget(self):
        """单篇上限必须明显小于总量。

        两者相等时第一个来源就把预算吃光，后面的来源 limit<=0 被 break 掉，
        报告退化成单一来源——正好丢掉了交叉引用这个核心价值。
        """
        from app import nodes
        from app.config import settings
        captured = {}

        async def fake_call(schema, system, user, model=None, attempts=2,
                            disable_thinking=False, on_draft=None,
                            draft_interval=1.0, attempt_timeout=None):
            captured['payload'] = json.loads(user)
            return Report.model_validate({
                'title': 't', 'summary': 's', 'summary_source_ids': ['s1'],
                'sections': [{'heading': 'h', 'paragraphs': [
                    {'text': 'x', 'source_ids': ['s1']}]}]}), None

        documents = [{'source_id': f's{i}', 'title': f't{i}', 'status': 'success',
                      'url': f'https://example.com/{i}', 'raw_content': 'x' * 20000}
                     for i in range(1, 5)]
        state = {'documents': documents,
                 'plan': {'mode': 'topic', 'topic': 't'}, 'usage': []}
        with patch.object(nodes, 'structured_call', fake_call):
            await nodes.compose_report(state)

        selected = captured['payload']['资料']
        total = sum(len(item['text']) for item in selected)
        self.assertGreaterEqual(len(selected), 2, '至少要装下两个来源才能交叉引用')
        self.assertLessEqual(total, settings.max_total_chars)
        for item in selected:
            self.assertLessEqual(len(item['text']), settings.max_source_chars)


class ComposeModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_compose_passes_compose_model_and_records_it(self):
        from app.config import settings
        captured = {}

        async def fake_call(schema, system, user, model=None, attempts=2,
                            disable_thinking=False, on_draft=None,
                            draft_interval=1.0):
            captured['model'] = model
            return Report.model_validate({
                'title': 't', 'summary': 's', 'summary_source_ids': ['s1'],
                'sections': [{'heading': 'h', 'paragraphs': [
                    {'text': 'x', 'source_ids': ['s1']}]}],
            }), {'total_tokens': 1}

        state = {
            'documents': [{'source_id': 's1', 'title': 't', 'status': 'success',
                           'url': 'https://example.com/', 'raw_content': '正文内容'}],
            'plan': {'mode': 'topic', 'topic': 't'}, 'usage': [],
        }
        with patch.object(nodes, 'structured_call', fake_call):
            result = await nodes.compose_report(state)
        self.assertEqual(captured['model'], settings.llm_compose_model)
        # usage 里要能看出这一阶段用的是哪个模型，便于排查耗时。
        self.assertEqual(result['usage'][-1]['model_name'], settings.llm_compose_model)

    async def test_parse_records_model_name(self):
        """两个阶段的 usage 都要记模型名，才能从运行记录核查各步用的哪个模型。"""
        from app.config import settings

        async def fake_call(schema, system, user, model=None, attempts=2,
                            disable_thinking=False, on_draft=None,
                            draft_interval=1.0):
            return Plan.model_validate({'topic': 't', 'auto_deploy': False,
                                 'need_research': True}), None

        with patch.object(nodes, 'structured_call', fake_call), \
             patch.object(nodes, 'urls_from_input', lambda text: []):
            result = await nodes.parse_request({'user_input': 't', 'usage': []})
        self.assertEqual(result['usage'][-1]['model_name'], settings.llm_model)

    async def test_parse_stage_keeps_main_model(self):
        # 解析阶段不受影响，仍用主模型。
        captured = {}

        async def fake_call(schema, system, user, model=None, attempts=2,
                            disable_thinking=False, on_draft=None,
                            draft_interval=1.0):
            captured['model'] = model
            return Plan.model_validate({'topic': 't', 'auto_deploy': False,
                                 'need_research': True}), None

        with patch.object(nodes, 'structured_call', fake_call), \
             patch.object(nodes, 'urls_from_input', lambda text: []):
            await nodes.parse_request({'user_input': 't', 'usage': []})
        self.assertIsNone(captured['model'])


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


class ProfileNodeTests(unittest.IsolatedAsyncioTestCase):
    """parse_request 记住用户信息并发出提示。"""

    async def test_parse_merges_profile_updates(self):
        from app import nodes, profile
        from app.config import settings
        import tempfile as _tf
        with _tf.TemporaryDirectory() as folder:
            original = settings.profile_path
            object.__setattr__(settings, 'profile_path',
                               Path(folder) / 'USER.md')
            try:
                async def fake_call(schema, system, user, model=None, attempts=2,
                                    disable_thinking=False, on_draft=None,
                                    draft_interval=1.0):
                    return Plan.model_validate({
                        'topic': 't', 'auto_deploy': False,
                        'need_research': True,
                        'profile_updates': {'电话': '13900000000'}}), None
                seen = []
                with patch.object(nodes, 'structured_call', fake_call), \
                     patch.object(nodes, 'urls_from_input', lambda text: []), \
                     patch.object(nodes, 'emit', seen.append):
                    await nodes.parse_request({'user_input': '我叫张三，电话13900000000',
                                               'usage': []})
                self.assertEqual(profile.load().get('电话'), '13900000000')
                labels = [e.get('label') for e in seen]
                self.assertIn('已记住', labels)
            finally:
                object.__setattr__(settings, 'profile_path', original)


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

    async def test_parse_request_skips_the_model_for_small_talk(self):
        from app import nodes

        async def forbidden(*args, **kwargs):
            raise AssertionError('寒暄不应该调用模型')

        with patch.object(nodes, 'structured_call', forbidden):
            result = await nodes.parse_request({'user_input': '你好', 'usage': []})
        self.assertEqual(result['status'], 'planned')
        self.assertFalse(result['plan']['need_research'])
        self.assertEqual(result['usage'], [])        # 没调模型就没有用量
        self.assertEqual(nodes.route_intent(result), 'answer_directly')

    async def test_link_is_never_small_talk(self):
        """带链接就必须走检索，不能因为句子短就被当成寒暄。"""
        from app import nodes
        called = {}

        async def fake_call(schema, system, user, **kw):
            called['hit'] = True
            return Plan.model_validate({'topic': 't', 'auto_deploy': False,
                                        'need_research': True}), None

        with patch.object(nodes, 'structured_call', fake_call):
            result = await nodes.parse_request(
                {'user_input': 'hi https://example.com', 'usage': []})
        self.assertTrue(called.get('hit'), '带 URL 时不能跳过模型')
        self.assertTrue(result['plan']['need_research'])


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    """按 need_research 分流：不需要联网时直接回答，不搜索、不生成报告。"""

    def test_route_intent(self):
        from app import nodes
        self.assertEqual(nodes.route_intent({'plan': {'need_research': True}}),
                         'discover_sources')
        self.assertEqual(nodes.route_intent({'plan': {'need_research': False}}),
                         'answer_directly')

    def test_route_intent_defaults_to_research(self):
        # 缺字段时保守走检索：该搜没搜会让用户拿到没有来源的回答
        from app import nodes
        self.assertEqual(nodes.route_intent({'plan': {}}), 'discover_sources')
        self.assertEqual(nodes.route_intent({}), 'discover_sources')

    async def test_direct_answer_streams_deltas(self):
        from app import nodes
        pieces = ('你好', '，有什么', '可以帮你？')

        async def fake_chat(system, messages, model=None, on_delta=None):
            for piece in pieces:
                if on_delta:
                    on_delta(piece)
            return ''.join(pieces)

        seen = []
        with patch.object(nodes, 'chat_reply', fake_chat), \
             patch.object(nodes, 'emit', seen.append):
            result = await nodes.answer_directly(
                {'user_input': '你好', 'history': []})
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(result['answer'], '你好，有什么可以帮你？')
        deltas = [e['text'] for e in seen if e.get('type') == 'answer_delta']
        self.assertEqual(''.join(deltas), '你好，有什么可以帮你？')

    async def test_direct_answer_passes_history(self):
        from app import nodes
        captured = {}

        async def fake_chat(system, messages, model=None, on_delta=None):
            captured['messages'] = messages
            return 'ok'

        history = [{'role': 'user', 'content': '前一句'},
                   {'role': 'assistant', 'content': '前一句的回答'}]
        with patch.object(nodes, 'chat_reply', fake_chat), \
             patch.object(nodes, 'emit', lambda e: None):
            await nodes.answer_directly(
                {'user_input': '接着问', 'history': history})
        # 多轮上下文要带上，否则"接着问"这类指代没法理解
        self.assertEqual([m['content'] for m in captured['messages']],
                         ['前一句', '前一句的回答', '接着问'])

    async def test_graph_routes_to_direct_answer_without_searching(self):
        """need_research=False 时不应触发任何检索。"""
        from app import nodes
        from app.graph import build_graph
        touched = {}

        async def fake_parse(state):
            return {'plan': {'mode': 'topic', 'topic': 't', 'queries': ['q'],
                             'urls': [], 'need_research': False},
                    'status': 'planned'}

        async def fake_answer(state):
            return {'answer': '你好！', 'status': 'answered'}

        async def should_not_run(*a, **k):
            touched['searched'] = True
            return {}

        with patch.object(nodes, 'parse_request', fake_parse), \
             patch.object(nodes, 'answer_directly', fake_answer), \
             patch.object(nodes, 'discover_sources', should_not_run):
            result = await build_graph().ainvoke({
                'task_id': 'x', 'user_input': '你好', 'documents': [],
                'errors': [], 'usage': [], 'research_round': 0, 'searched': False})
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(result['answer'], '你好！')
        self.assertNotIn('searched', touched)


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
        return [self.FakeChunk(tool_calls=[{'name': 'Plan', 'args': {'topic': ''}}],
                               usage={'total_tokens': tokens})]

    def good(self, tokens=7):
        return [self.FakeChunk(
            tool_calls=[{'name': 'Plan',
                         'args': {'topic': 't', 'auto_deploy': False,
                                  'need_research': True}}],
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
            result = await services.structured_call(Plan, 's', 'u', **kw)
        return result, calls

    async def test_retries_once_then_succeeds(self):
        (parsed, _), calls = await self.call([self.bad(), self.good()])
        self.assertEqual(parsed.topic, 't')
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
        self.assertEqual(calls[0], ('bind', 'Plan'))

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
                    Plan, 's', 'u', attempts=2, attempt_timeout=0.05)
        self.assertEqual(len(calls), 1, '超时不应该重试')

    async def test_attempt_timeout_zero_disables_the_limit(self):
        """attempt_timeout<=0 表示不限制，走正常路径。"""
        (parsed, _), calls = await self.call([self.good()], attempts=1,
                                             attempt_timeout=0)
        self.assertEqual(parsed.topic, 't')
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
                Plan, 's', 'u', attempts=1, attempt_timeout=30)

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
                '{"topic": "半成品", "auto_deploy": false, "need_research": true}'}])
        full = self.FakeChunk(
            tool_calls=[{'name': 'Plan',
                         'args': {'topic': '完整', 'auto_deploy': False,
                                  'need_research': True}}],
            usage={'total_tokens': 3})
        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False:
                          self.fake_llm([[partial, full]], calls)):
            parsed, _ = await services.structured_call(
                Plan, 's', 'u', attempts=1, on_draft=drafts.append, draft_interval=0)
        self.assertEqual(parsed.topic, '完整')
        # 首次回调必须是 None（清空）：重试时也一样，避免两次内容混在一起
        self.assertIsNone(drafts[0])
        # 中途要能收到半成品（标题已到、其余还在生成）
        self.assertTrue(any(d is not None and d.topic == '半成品' for d in drafts))
        # 最终结果也要推一次，保证前端拿到完整版
        self.assertEqual(drafts[-1].topic, '完整')

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
                    Plan, 's', 'u', attempts=1, on_draft=drafts.append,
                    draft_interval=3600)      # 节流窗口远大于测试时长
        # 40 个分片只允许解析一次（节流生效），而不是 40 次
        self.assertEqual(parsed_count['n'], 1)

    async def test_draft_clear_before_each_retry(self):
        from app import services
        calls, drafts = [], []
        partial = self.FakeChunk(
            tool_call_chunks=[{'args': '{"topic": "第一版的残留", "auto_deploy": false,'
                                    ' "need_research": true}'}])
        bad = self.FakeChunk(tool_calls=[{'name': 'Plan', 'args': {'topic': ''}}],
                             tool_call_chunks=[{'args': '{"topic": "第一版的残留"}'}])
        good = self.FakeChunk(
            tool_calls=[{'name': 'Plan',
                         'args': {'topic': '第二版', 'auto_deploy': False,
                                  'need_research': True}}])
        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False:
                          self.fake_llm([[partial, bad], [good]], calls)):
            await services.structured_call(
                Plan, 's', 'u', on_draft=drafts.append, draft_interval=0)
        # 两次尝试各清空一次，第一次尝试的残留不会留在界面上
        self.assertEqual([d is None for d in drafts].count(True), 2)


if __name__ == '__main__':
    unittest.main()
