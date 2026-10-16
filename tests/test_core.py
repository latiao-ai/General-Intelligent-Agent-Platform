import os
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
            return {'plan': {'mode': 'topic', 'topic': 'test', 'queries': ['test'], 'urls': []}}
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
        with self.assertRaises(ValidationError):
            Plan.model_validate({'topic': '对比数据库的部署方式'})
        plan = Plan.model_validate({'topic': '对比数据库的部署方式', 'auto_deploy': False})
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


class ComposeModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_compose_passes_compose_model_and_records_it(self):
        from app.config import settings
        captured = {}

        async def fake_call(schema, system, user, model=None):
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

    async def test_parse_stage_keeps_main_model(self):
        # 解析阶段不受影响，仍用主模型。
        captured = {}

        async def fake_call(schema, system, user, model=None):
            captured['model'] = model
            return Plan.model_validate({'topic': 't', 'auto_deploy': False}), None

        with patch.object(nodes, 'structured_call', fake_call), \
             patch.object(nodes, 'urls_from_input', lambda text: []):
            await nodes.parse_request({'user_input': 't', 'usage': []})
        self.assertIsNone(captured['model'])


class StructuredRetryTests(unittest.IsolatedAsyncioTestCase):
    """快模型偶发产出不合法结构时，结构化调用会重试而不是让整个任务失败。"""

    def fake_llm(self, outcomes, calls):
        class FakeLLM:
            def with_structured_output(self, *args, **kwargs):
                return self

            async def ainvoke(self, messages):
                calls.append(1)
                return outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        return FakeLLM()

    @staticmethod
    def bad(tokens=5):
        return {'parsed': None, 'parsing_error': 'boom',
                'raw': SimpleNamespace(usage_metadata={'total_tokens': tokens})}

    @staticmethod
    def good(tokens=7):
        return {'parsed': 'GOOD', 'parsing_error': None,
                'raw': SimpleNamespace(usage_metadata={'total_tokens': tokens})}

    async def test_retries_once_then_succeeds(self):
        from app import services
        calls = []
        with patch.object(services, 'new_llm',
                          lambda model=None: self.fake_llm([self.bad(), self.good()], calls)):
            parsed, _ = await services.structured_call(object(), 's', 'u')
        self.assertEqual(parsed, 'GOOD')
        self.assertEqual(len(calls), 2)

    async def test_gives_up_after_max_attempts(self):
        from app import services
        calls = []
        with patch.object(services, 'new_llm',
                          lambda model=None: self.fake_llm([self.bad()], calls)):
            with self.assertRaises(RuntimeError):
                await services.structured_call(object(), 's', 'u')
        self.assertEqual(len(calls), 2)

    async def test_usage_sums_failed_and_successful_attempts(self):
        from app import services
        calls = []
        with patch.object(services, 'new_llm',
                          lambda model=None: self.fake_llm([self.bad(5), self.good(7)], calls)):
            _, usage = await services.structured_call(object(), 's', 'u')
        # 失败的尝试同样消耗 token，必须计入成本统计。
        self.assertEqual(usage['total_tokens'], 12)

    async def test_single_attempt_returns_its_own_usage(self):
        from app import services
        calls = []
        with patch.object(services, 'new_llm',
                          lambda model=None: self.fake_llm([self.good(9)], calls)):
            _, usage = await services.structured_call(object(), 's', 'u', attempts=1)
        self.assertEqual(usage, {'total_tokens': 9})


class InstrumentTests(unittest.IsolatedAsyncioTestCase):
    """节点包装层发出的阶段事件——前端实时计时的数据来源。"""

    async def test_sync_node_emits_start_and_end(self):
        from app import graph as graph_module
        seen = []
        with patch.object(graph_module, 'emit', seen.append):
            wrapped = graph_module.instrument(
                'render_html', lambda state: {'status': 'rendered'})
            result = await wrapped({})
        self.assertEqual(result, {'status': 'rendered'})
        self.assertEqual([e['type'] for e in seen], ['stage_start', 'stage_end'])
        self.assertEqual(seen[0]['label'], '渲染 HTML')
        self.assertEqual(seen[1]['status'], 'rendered')
        self.assertIn('at', seen[0])          # 前端按服务端开始时刻实时累加
        self.assertGreaterEqual(seen[1]['seconds'], 0)

    async def test_async_node_emits_start_and_end(self):
        from app import graph as graph_module
        seen = []

        async def node(state):
            return {'status': 'planned'}

        with patch.object(graph_module, 'emit', seen.append):
            result = await graph_module.instrument('parse_request', node)({})
        self.assertEqual(result['status'], 'planned')
        self.assertEqual([e['type'] for e in seen], ['stage_start', 'stage_end'])

    async def test_failed_node_still_closes_the_stage(self):
        from app import graph as graph_module
        seen = []

        def boom(state):
            raise ValueError('x')

        with patch.object(graph_module, 'emit', seen.append):
            with self.assertRaises(ValueError):
                await graph_module.instrument('render_html', boom)({})
        # 异常路径也要发结束事件，否则前端那一行会永远停在"进行中"。
        self.assertEqual(seen[-1]['type'], 'stage_end')
        self.assertEqual(seen[-1]['status'], 'failed')

    async def test_wrapper_is_async_even_for_sync_node(self):
        # LangGraph 会把同步节点丢进线程池，而 stream writer 依赖 contextvars；
        # 统一包成 async 才能保证事件不丢。
        import inspect
        from app import graph as graph_module
        wrapped = graph_module.instrument('render_html', lambda state: {})
        self.assertTrue(inspect.iscoroutinefunction(wrapped))

    def test_all_stage_labels_are_chinese(self):
        from app.events import STAGE_LABELS
        for stage, label in STAGE_LABELS.items():
            self.assertNotEqual(stage, label, f'{stage} 缺少中文名')


if __name__ == '__main__':
    unittest.main()
