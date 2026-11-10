"""任务中枢、HTTP/SSE、权限归属、创建幂等与回执文案。"""
import asyncio
import tempfile
import time
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.core_support import new_store, reset_db


class HubTests(unittest.IsolatedAsyncioTestCase):
    """任务中枢：事件广播、多轮历史、取消、重启恢复。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    @staticmethod
    def hub():
        from webpage_agent.api.api_hub import Hub
        return Hub(new_store())

    @staticmethod
    async def events_of(record):
        return [event for _, event in await record.store.events(record.task_id)]

    @staticmethod
    def stub(captured=None, delay=0):
        async def fake_run_task(text, progress=None, on_event=None,
                                history=None, on_start=None,
                                prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            if captured is not None:
                captured['history'] = history
            if on_start:
                await on_start('artifact123')
            if on_event:
                await on_event({'type': 'action', 'icon': '🌐', 'label': '已搜索网页',
                          'detail': '测试'})
            if delay:
                await asyncio.sleep(delay)
            return {'task_id': 'artifact123', 'status': 'succeeded',
                    'artifact': {'html_path': '/tmp/artifact123/sources.html',
                                 'kind': 'sources'},
                    'timings': []}
        return fake_run_task

    async def test_start_broadcasts_events_and_records_messages(self):
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub()):
            record = await hub.start('测试需求')
            await asyncio.wait_for(record._task, timeout=10)
        self.assertEqual(record.status, 'succeeded')            # 通用状态
        self.assertEqual(record.summary()['status'], 'local_ready')  # 展示标签
        self.assertEqual(record.summary()['run_status'], 'succeeded')
        self.assertEqual(record.artifact_id, 'artifact123')
        labels = [e.get('label') for e in await self.events_of(record)]
        self.assertIn('已搜索网页', labels)
        # 会话里要留下 user + assistant 两条，下一轮才能还原指代型要求。
        self.assertEqual([m['role'] for m in await hub.history(record.session_id)],
                         ['user', 'assistant'])
        # 状态也要落库，重启后才能查询。
        # 落库的是通用状态：状态机词汇不随业务能力增长
        self.assertEqual((await hub.store.task(record.task_id))['status'], 'succeeded')

    async def test_late_subscriber_still_gets_history(self):
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub()):
            record = await hub.start('测试需求')
            await asyncio.wait_for(record._task, timeout=10)
            # 任务结束后才订阅，仍要能拿到完整过程（SSE 端点依赖此语义）。
            _, history = await record.subscribe()
        self.assertTrue(any(e.get('label') == '已搜索网页' for _, e in history))
        self.assertTrue(record.done)

    async def test_record_restored_from_db_after_restart(self):
        """模拟进程重启：新 Hub 打开同一个库，应能查到旧任务并补发事件。"""
        import webpage_agent.api.api_hub as hub_module
        from webpage_agent.api.api_hub import Hub
        # 库由 PG_TEST_DSN 提供，folder 是 SQLite 时代的残留，从未被使用。
        first = Hub(new_store())
        with patch.object(hub_module, 'run_task', self.stub()):
            record = await first.start('老任务')
            await asyncio.wait_for(record._task, timeout=10)
        await first.store.close()

        second = Hub(new_store())
        restored = await second.get(record.task_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.status, 'succeeded')
        self.assertEqual(restored.summary()['status'], 'local_ready')
        self.assertFalse(restored.live)
        self.assertFalse(await second.cancel(record.task_id))   # 重启后无法取消
        _, history = await restored.subscribe()
        self.assertTrue(any(e.get('label') == '已搜索网页' for _, e in history))
        await second.store.close()

    async def test_multi_turn_history_is_passed(self):
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        captured = {}
        with patch.object(hub_module, 'run_task', self.stub(captured)):
            first = await hub.start('找还珠格格资料')
            await asyncio.wait_for(first._task, timeout=10)
            second = await hub.start('我只要第一部第三集', first.session_id)
            await asyncio.wait_for(second._task, timeout=10)
        self.assertEqual(captured['history'][0]['content'], '找还珠格格资料')
        self.assertEqual(captured['history'][1]['role'], 'assistant')

    async def test_cancel_marks_cancelled_and_releases_subscribers(self):
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=30)):
            record = await hub.start('慢任务')
            queue, _ = await record.subscribe()
            await asyncio.sleep(0.3)
            self.assertTrue(await hub.cancel(record.task_id))
            with self.assertRaises(asyncio.CancelledError):
                await record._task
        self.assertEqual(record.status, 'cancelled')
        self.assertEqual((await hub.store.task(record.task_id))['status'], 'cancelled')
        self.assertTrue(any(e.get('status') == 'cancelled'
                            for e in await self.events_of(record)))
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
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            await on_event({'type': 'stage_start', 'stage': 'plan'})
            await on_event({'type': 'stage_end', 'stage': 'plan',
                      'seconds': 1.0, 'status': 'planned'})
            # 这一条到达时，"收到指令"应当被补齐
            await on_event({'type': 'stage_start', 'stage': 'web_search'})
            return {'task_id': 'a1', 'status': 'succeeded',
                    'artifact': {'html_path': '/tmp/a1/sources.html',
                                 'kind': 'sources'},
                    'timings': []}

        with patch.object(hub_module, 'run_task', fake):
            record = await hub.start('测试')
            await asyncio.wait_for(record._task, timeout=10)

        events = await self.events_of(record)
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
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=30)):
            record = await hub.start('慢任务')
            await asyncio.sleep(0.3)
            await hub.cancel(record.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await record._task
        ends = [e for e in await self.events_of(record)
                if e['type'] == 'stage_end' and e['stage'] == 'instruction']
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]['status'], '已中断')

    async def test_cancel_leaves_assistant_marker_in_history(self):
        """中断也要留下助手回复，否则下一轮上下文是两条连续 user 消息。"""
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub(delay=30)):
            record = await hub.start('慢任务')
            await asyncio.sleep(0.3)
            await hub.cancel(record.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await record._task
        self.assertEqual([m['role'] for m in await hub.history(record.session_id)],
                         ['user', 'assistant'])

    async def test_send_while_running_starts_new_turn_same_session(self):
        """运行中直接发第二条消息：同会话开新一轮，历史照常带上。"""
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        captured = {}
        with patch.object(hub_module, 'run_task', self.stub(delay=30)):
            first = await hub.start('第一轮要求')
            await asyncio.sleep(0.3)
            # 模拟前端"打断并发送"：取消旧任务后立刻用同一会话开新一轮
            await hub.cancel(first.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await first._task
        with patch.object(hub_module, 'run_task', self.stub(captured)):
            second = await hub.start('第二轮要求', first.session_id)
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
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            await on_event({'type': 'stage_start', 'stage': 'plan'})
            await on_event({'type': 'stage_end', 'stage': 'plan',
                      'seconds': 1.0, 'status': 'planned'})
            await on_event({'type': 'stage_start', 'stage': 'discover_sources'})
            await on_event({'type': 'stage_start', 'stage': 'compose_report'})
            await asyncio.sleep(30)          # 卡在整理报告
            return {}

        with patch.object(hub_module, 'run_task', fake):
            record = await hub.start('慢任务')
            await asyncio.sleep(0.3)
            await hub.cancel(record.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await record._task

        ends = {e['stage']: e['status'] for e in await self.events_of(record)
                if e['type'] == 'stage_end'}
        self.assertEqual(ends.get('compose_report'), '已中断')
        self.assertIsNone(record.open_stage)

    async def test_cancel_before_search_closes_instruction(self):
        """在检索之前就被打断：合成阶段也要补上结束事件。"""
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            await on_event({'type': 'stage_start', 'stage': 'plan'})
            await asyncio.sleep(30)
            return {}

        with patch.object(hub_module, 'run_task', fake):
            record = await hub.start('慢任务')
            await asyncio.sleep(0.3)
            await hub.cancel(record.task_id)
            with self.assertRaises(asyncio.CancelledError):
                await record._task

        ends = {e['stage']: e['status'] for e in await self.events_of(record)
                if e['type'] == 'stage_end'}
        self.assertEqual(ends.get('instruction'), '已中断')
        # 卡住的那一步（规划）也要被补上结束事件
        self.assertEqual(ends.get('plan'), '已中断')

    async def test_failed_task_closes_in_flight_stage(self):
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            # 用规划阶段做"还没开始干活"的代表：它不在 FIRST_WORK_STAGES 里，
            # 因此指令阶段仍是打开的，失败时应当由失败路径补上结束事件。
            await on_event({'type': 'stage_start', 'stage': 'plan'})
            raise RuntimeError('boom')

        with patch.object(hub_module, 'run_task', fake):
            record = await hub.start('坏任务')
            await asyncio.wait_for(record._task, timeout=10)
        ends = {e['stage']: e['status'] for e in await self.events_of(record)
                if e['type'] == 'stage_end'}
        self.assertEqual(ends.get('plan'), '已失败')
        self.assertEqual(ends.get('instruction'), '已失败')

    async def test_stop_command_cancels_instead_of_starting_a_task(self):
        """说"暂停"要停流程，而不是拿"暂停"去检索。"""
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        started = {'n': 0}

        async def slow(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            started['n'] += 1
            await asyncio.sleep(30)
            return {}

        with patch.object(hub_module, 'run_task', slow):
            first = await hub.start('整理一份 SQLite 报告')
            await asyncio.sleep(0.2)
            record = await hub.start('暂停', first.session_id)
            with self.assertRaises(asyncio.CancelledError):
                await first._task

        self.assertEqual(started['n'], 1, '暂停不应该再启动一次模型流程')
        self.assertEqual(record.status, 'cancelled')
        details = [e.get('detail') for e in await self.events_of(record)
                   if e.get('type') == 'status']
        self.assertTrue(any('已停止当前任务' in (d or '') for d in details))

    async def test_stop_command_with_nothing_running(self):
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub()):
            record = await hub.start('暂停')
        self.assertEqual(record.status, 'cancelled')
        details = [e.get('detail') for e in await self.events_of(record)
                   if e.get('type') == 'status']
        self.assertTrue(any('没有正在执行的任务' in (d or '') for d in details))

    async def test_prior_context_carries_artifact_and_documents(self):
        """多轮承接：产物和正文都要能从上一轮交过来。"""
        hub = self.hub()
        await hub.store.create_task('t1', 'sess', '第一轮')
        await hub.store.update_task('t1', status='local_ready', state={
            'artifact': {'html_path': '/x/index.html'},
            'documents': [{'source_id': 's1', 'url': 'https://a/1',
                           'status': 'success'},
                          {'source_id': 's2', 'url': 'https://b/2',
                           'status': 'failed'}],
        })
        ctx = await hub.prior_context('sess')
        self.assertEqual(ctx['artifact']['html_path'], '/x/index.html')
        # 只接成功的来源：报告要给它们生成参考资料，失败的没有正文
        self.assertEqual([d['source_id'] for d in ctx['documents']], ['s1'])
        self.assertEqual(await hub.prior_context('nope'), {})

    async def test_cancel_done_task_is_noop(self):
        import webpage_agent.api.api_hub as hub_module
        hub = self.hub()
        with patch.object(hub_module, 'run_task', self.stub()):
            record = await hub.start('快任务')
            await asyncio.wait_for(record._task, timeout=10)
        self.assertFalse(await hub.cancel(record.task_id))


class PolicyTests(unittest.TestCase):
    """身份与权限缝：单用户恒通过，跨用户必须被挡住。"""

    def test_same_owner_passes(self):
        from webpage_agent.governance import policies
        principal = policies.Principal(tenant_id='t1', user_id='u1')
        resource = {'tenant_id': 't1', 'owner_user_id': 'u1'}
        self.assertEqual(policies.ensure_owner(resource, principal, '运行'),
                         resource)

    def test_cross_tenant_is_hidden_as_not_found(self):
        from webpage_agent.governance import policies
        from fastapi import HTTPException
        principal = policies.Principal(tenant_id='t1', user_id='u1')
        other = {'tenant_id': 't2', 'owner_user_id': 'u1'}
        with self.assertRaises(HTTPException) as ctx:
            policies.ensure_owner(other, principal, '运行')
        # 不泄漏"存在但无权"，统一按不存在处理
        self.assertEqual(ctx.exception.status_code, 404)

    def test_anonymous_default_principal_is_permissive(self):
        from webpage_agent.governance import policies
        principal = policies.Principal()
        self.assertTrue(principal.anonymous)
        resource = {'tenant_id': 'default', 'owner_user_id': 'default'}
        self.assertEqual(policies.ensure_owner(resource, principal), resource)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    """HTTP 接口与 SSE 端点。"""

    async def asyncSetUp(self):
        await reset_db()
        import httpx
        import webpage_agent.api.routes as api_module
        from webpage_agent.api.api_hub import Hub
        # 每个测试用独立的内存库：互不污染，也不去碰真实的 agent.db
        self._saved_hub = api_module.hub
        api_module.hub = Hub(new_store())
        self.api_module = api_module
        # 用 ASGITransport 在**当前**事件循环里发请求。TestClient 会为每个请求
        # 另起一条循环，而仓储/连接池是绑循环的，两边共用必然挂死。
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        replacement = self.api_module.hub
        self.api_module.hub = self._saved_hub
        close = getattr(replacement, 'close', None)
        if callable(close) and replacement is not self._saved_hub:
            try:
                await close()
            except Exception:
                pass

    async def _wait_settled(self, task_id: str, timeout: float = 5.0):
        """等任务落定（完成/取消/失败都算）。

        `TestClient` 每个请求起一个事件循环，请求返回后其后台 `asyncio.Task`
        会被取消；不先等它落定就去订阅 SSE，就会一直等在 queue.get() 上。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            record = await self.api_module.hub.get(task_id)
            if record is None or record.done:
                return record
            await asyncio.sleep(0.02)
        return await self.api_module.hub.get(task_id)

    async def test_index_serves_chat_ui(self):
        res = await self.client.get('/')
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

    async def test_unknown_task_returns_404(self):
        self.assertEqual((await self.client.get('/api/tasks/nope')).status_code, 404)
        self.assertEqual((await self.client.post('/api/tasks/nope/cancel')).status_code, 404)
        self.assertEqual((await self.client.get('/api/tasks/nope/artifact')).status_code, 404)

    async def test_create_task_streams_events_and_ends(self):
        import webpage_agent.api.api_hub as hub_module

        async def fake_run_task(text, progress=None, on_event=None,
                                history=None, on_start=None,
                                prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            if on_start:
                await on_start('artifact9')
            if on_event:
                await on_event({'type': 'action', 'icon': '🌐', 'label': '已搜索网页',
                          'detail': 'SQLite 适用场景'})
            return {'task_id': 'artifact9', 'status': 'local_ready',
                    'deployment': {'status': 'skipped'}, 'timings': []}

        with patch.object(hub_module, 'run_task', fake_run_task):
            res = await self.client.post('/api/tasks', json={'message': '测试'})
            self.assertEqual(res.status_code, 200)
            payload = res.json()
            self.assertTrue(payload['task_id'] and payload['session_id'])
            await self._wait_settled(payload['task_id'])
            async with self.client.stream(
                    'GET', f"/api/tasks/{payload['task_id']}/events") as stream:
                body = ''.join([chunk async for chunk in stream.aiter_text()])
        # 过程事件与结束标记都要通过 SSE 送达。
        self.assertIn('已搜索网页', body)
        self.assertIn('SQLite 适用场景', body)
        self.assertIn('"type": "end"', body)
        self.assertIn('data: ', body)

    async def test_keepalive_is_a_real_event_not_a_comment(self):
        """心跳必须是 data 事件。

        SSE 注释（": keepalive"）不会触发浏览器 onmessage，前端就无从判断连接
        是否还活着：后端一被杀，那一行会永远"进行中"、耗时一直涨。
        """
        import inspect
        import webpage_agent.api.routes as api_module

        source = inspect.getsource(api_module.task_events)
        self.assertIn("sse({'type': 'ping'})", source)
        self.assertNotIn("': keepalive", source)
        # ping 必须走标准 SSE 帧，浏览器 onmessage 才收得到。
        frame = api_module.sse({'type': 'ping'})
        self.assertTrue(frame.startswith('data: '))
        self.assertTrue(frame.endswith('\n\n'))

    async def test_session_messages_endpoint(self):
        import webpage_agent.api.api_hub as hub_module

        async def fake_run_task(text, progress=None, on_event=None,
                                history=None, on_start=None,
                                prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            return {'task_id': 'a1', 'status': 'succeeded',
                    'artifact': {'html_path': '/tmp/a1/sources.html',
                                 'kind': 'sources'},
                    'timings': []}

        with patch.object(hub_module, 'run_task', fake_run_task):
            created = (await self.client.post('/api/tasks', json={'message': '你好'})).json()
        messages = (await self.client.get(
            f"/api/sessions/{created['session_id']}/messages")).json()['messages']
        self.assertEqual(messages[0]['role'], 'user')
        self.assertEqual(messages[0]['content'], '你好')

    async def test_session_timeline_replays_execution_steps(self):
        """切换会话时要能重建执行过程：接口必须带回每轮持久化的事件。"""
        import webpage_agent.api.api_hub as hub_module

        async def fake_run_task(text, progress=None, on_event=None,
                                history=None, on_start=None,
                                prior=None, memory=None, ledger=None, run_id=None, resume_plan=None):
            if on_start:
                await on_start('a1')
            if on_event:
                await on_event({'type': 'stage_start', 'stage': 'compose_report',
                          'label': '整理报告', 'at': 10.0})
                await on_event({'type': 'action', 'icon': '🌐', 'label': '已搜索网页'})
                await on_event({'type': 'stage_end', 'stage': 'compose_report',
                          'label': '整理报告', 'seconds': 2.5, 'status': '完成'})
            return {'task_id': 'a1', 'status': 'succeeded',
                    'artifact': {'html_path': '/tmp/a1/sources.html',
                                 'kind': 'sources'},
                    'timings': []}

        with patch.object(hub_module, 'run_task', fake_run_task):
            created = (await self.client.post('/api/tasks', json={'message': '查一下'})).json()
            record = await self.api_module.hub.get(created['task_id'])
            for _ in range(100):          # 后台任务跑完再取，事件才都落库
                if record.done:
                    break
                await asyncio.sleep(0.02)
        self.assertTrue(record.done, '假任务应当在超时前结束')

        data = (await self.client.get(
            f"/api/sessions/{created['session_id']}/timeline")).json()
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

    async def test_profile_can_be_read_and_rewritten(self):
        """画像必须能在界面上改：读回原文、整体覆盖、清空留备份。"""
        from webpage_agent.core import profile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'USER.md'
            with patch.object(profile, 'settings',
                              SimpleNamespace(profile_path=path)):
                profile.merge({'身高': '173cm', '体重': '50kg'})

                got = (await self.client.get('/api/profile')).json()
                self.assertEqual(got['facts'], {'身高': '173cm', '体重': '50kg'})
                self.assertIn('身高', got['text'])

                # 整体覆盖：改一条、加一条、删一条
                res = await self.client.put('/api/profile', json={'text':
                    '身高：174cm\n饮食：低糖\n'})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()['facts'],
                                 {'身高': '174cm', '饮食': '低糖'})
                self.assertEqual(profile.load(), {'身高': '174cm', '饮食': '低糖'})

                # 清空要留 .bak，手滑也能捞回来
                await self.client.delete('/api/profile')
                self.assertEqual(profile.load(), {})
                backup = Path(str(path) + '.bak')
                self.assertTrue(backup.exists())
                self.assertIn('174cm', backup.read_text(encoding='utf-8'))

    async def test_profile_rejects_oversized_text(self):
        self.assertEqual(
            (await self.client.put('/api/profile', json={'text': 'x' * 20001})).status_code,
            422)

    async def test_artifact_endpoint_serves_whatever_was_produced(self):
        """产物可能是报告页也可能是资料页，接口不能把文件名写死成 index.html。

        早先硬编码 index.html，"资料转 HTML" 产出的 sources.html 一律 404。
        """
        import webpage_agent.api.api_hub as hub_module
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
                created = (await self.client.post('/api/tasks', json={'message': '存成网页'})).json()
                record = await self.api_module.hub.get(created['task_id'])
                for _ in range(100):
                    if record.done:
                        break
                    await asyncio.sleep(0.02)
            res = await self.client.get(f"/api/tasks/{created['task_id']}/artifact")
        self.assertEqual(res.status_code, 200)
        self.assertIn('资料页', res.text)

    async def test_session_memory_endpoints(self):
        """会话记忆要能在界面上读、改、清空。"""
        hub = self.api_module.hub
        await hub.store.append_message('s1', 'user', '你好')

        empty = (await self.client.get('/api/sessions/s1/memory')).json()
        self.assertEqual(empty['summary'], '')
        self.assertEqual(empty['facts'], {})

        saved = (await self.client.put('/api/sessions/s1/memory', json={
            'summary': '用户在减脂', 'facts': '目标：降到15%\n已否决：节食'})).json()
        self.assertEqual(saved['facts'], {'目标': '降到15%', '已否决': '节食'})

        got = (await self.client.get('/api/sessions/s1/memory')).json()
        self.assertEqual(got['summary'], '用户在减脂')
        self.assertIn('目标：降到15%', got['facts_text'])

        await self.client.delete('/api/sessions/s1/memory')
        self.assertEqual((await self.client.get('/api/sessions/s1/memory')).json()['summary'], '')

    async def test_session_timeline_orders_turns_oldest_first(self):
        """回放必须按发生顺序，否则步骤看起来是倒着走的。"""
        store = new_store()
        await store.append_message('s1', 'user', '第一问')
        await store.create_task('t1', 's1', '第一问')
        await store.append_message('s1', 'user', '第二问')
        await store.create_task('t2', 's1', '第二问')
        self.assertEqual([t['task_id'] for t in await store.session_tasks('s1')],
                         ['t1', 't2'])


class MessageTests(unittest.TestCase):
    """助手回执与停止提示的文案（CLI / API 共用）。"""

    def test_reply_mentions_deployed_url(self):
        from webpage_agent.core.messages import reply_text
        text = reply_text({'status': 'deployed',
                           'deployment': {'public_url': 'http://x/y/'}})
        self.assertIn('http://x/y/', text)

    def test_reply_for_local_ready_mentions_deploy_option(self):
        from webpage_agent.core.messages import reply_text
        text = reply_text({'status': 'succeeded', 'task_id': 'abc',
                           'artifact': {'html_path': '/tmp/a/index.html',
                                        'kind': 'sources'}})
        self.assertIn('部署', text)

    def test_reply_for_failed_is_explainable(self):
        from webpage_agent.core.messages import reply_text
        self.assertIn('没有产出可用结果', reply_text({'status': 'failed'}))
        # 有具体失败原因时要透出来，而不是只说"失败了"
        with_reason = reply_text({'status': 'failed',
                                  'failure': '检索来源失败：搜索失败'})
        self.assertIn('检索来源失败', with_reason)

    def test_cancel_note_warns_when_already_deployed(self):
        import json
        from pathlib import Path as P
        from webpage_agent.core.config import settings
        from webpage_agent.core.messages import cancel_note
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
        from webpage_agent.core.messages import cancel_note
        self.assertIn('已停止', cancel_note(None))


class TimingLabelTests(unittest.TestCase):
    def test_every_graph_node_has_a_chinese_label(self):
        from webpage_agent.observe.events import ORCHESTRATION_STAGES
        from webpage_agent.api.main import STAGE_LABELS
        missing = [name for name in ORCHESTRATION_STAGES if name not in STAGE_LABELS]
        self.assertEqual(missing, [])

    def test_format_timings_includes_label_and_seconds(self):
        from webpage_agent.api.main import format_timings
        lines = format_timings([{'stage': 'render_sources_html', 'seconds': 8.4,
                                 'status': 'composed'}])
        self.assertEqual(len(lines), 1)
        self.assertIn('资料转 HTML', lines[0])
        self.assertIn('8.40', lines[0])

    def test_format_timings_tolerates_empty(self):
        from webpage_agent.api.main import format_timings
        self.assertEqual(format_timings(None), [])
        self.assertEqual(format_timings([]), [])


class ApiOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """接口归属校验：知道 session_id 不等于有权限（评审 P0-8）。"""

    async def asyncSetUp(self):
        await reset_db()
        import httpx
        import webpage_agent.api.routes as api_module
        from webpage_agent.api.api_hub import Hub
        self._saved_hub = api_module.hub
        api_module.hub = Hub(new_store())
        self.api_module = api_module
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        replacement = self.api_module.hub
        self.api_module.hub = self._saved_hub
        close = getattr(replacement, 'close', None)
        if callable(close) and replacement is not self._saved_hub:
            try:
                await close()
            except Exception:
                pass

    async def _create(self, message='你好'):
        import webpage_agent.api.api_hub as hub_module

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            return {'task_id': 'a1', 'status': 'answered', 'timings': [],
                    'deployment': {}, 'answer': '好的'}

        with patch.object(hub_module, 'run_task', fake):
            response = await self.client.post('/api/tasks', json={'message': message},
                                             headers={'X-DSH-User': 'u1'})
            data = response.json()
            record = await self.api_module.hub.get(data['task_id'])
            if record is not None and record._task is not None:
                await record._task
            return data

    async def test_other_user_cannot_read_a_session(self):
        created = await self._create()
        session_id = created['session_id']
        as_owner = await self.client.get(f'/api/sessions/{session_id}/messages',
                                   headers={'X-DSH-User': 'u1'})
        self.assertEqual(as_owner.status_code, 200)
        # 只把 ID 换成别人的身份：一律按"不存在"处理，不泄漏对象是否存在
        for path in (f'/api/sessions/{session_id}/messages',
                     f'/api/sessions/{session_id}/timeline',
                     f'/api/sessions/{session_id}/memory',
                     f"/api/tasks/{created['task_id']}",
                     f"/api/tasks/{created['task_id']}/artifact"):
            res = await self.client.get(path, headers={'X-DSH-User': 'u2'})
            self.assertEqual(res.status_code, 404, path)

    async def test_profile_is_scoped_per_user(self):
        from webpage_agent.core import profile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'USER.md'
            with patch.object(profile, 'settings',
                              SimpleNamespace(profile_path=path)):
                await self.client.put('/api/profile', json={'text': '姓名：甲'},
                                headers={'X-DSH-User': 'u1'})
                await self.client.put('/api/profile', json={'text': '姓名：乙'},
                                headers={'X-DSH-User': 'u2'})
                first = (await self.client.get('/api/profile',
                                        headers={'X-DSH-User': 'u1'})).json()
                second = (await self.client.get('/api/profile',
                                         headers={'X-DSH-User': 'u2'})).json()
                self.assertEqual(first['facts'], {'姓名': '甲'})
                self.assertEqual(second['facts'], {'姓名': '乙'})
                self.assertFalse(path.exists(), '默认用户不该被别人的画像覆盖')

    async def test_health_exposes_queue_state(self):
        data = (await self.client.get('/api/health')).json()
        self.assertTrue(data['ok'])
        for key in ('runs_queued', 'runs_running', 'actions_unknown',
                    'jobs_queued'):
            self.assertIn(key, data)


class IdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """创建任务幂等：客户端重试不能变成"两个任务、两次部署"。"""

    async def asyncSetUp(self):
        # 每个用例先清库：以前靠 SQLite :memory: 隔离
        await reset_db()

    def principal(self, user='u1'):
        return SimpleNamespace(tenant_id='default', user_id=user)

    async def test_same_key_and_body_replays_the_same_task(self):
        from webpage_agent.store import idempotency
        store = new_store()
        body = {'message': '做个报告', 'session_id': None}
        first = await idempotency.reserve(store, principal=self.principal(),
                                    endpoint='tasks.create', client_key='k1',
                                    payload=body, ttl_seconds=3600)
        self.assertFalse(first.replayed)
        await first.complete({'task_id': 't1', 'session_id': 's1', 'run_id': 'r1'},
                             task_id='t1', session_id='s1', run_id='r1')

        again = await idempotency.reserve(store, principal=self.principal(),
                                    endpoint='tasks.create', client_key='k1',
                                    payload=body, ttl_seconds=3600)
        self.assertTrue(again.replayed)
        self.assertEqual((await again.response)['task_id'], 't1')

    async def test_same_key_with_different_body_conflicts(self):
        from webpage_agent.store import idempotency
        store = new_store()
        await idempotency.reserve(store, principal=self.principal(),
                            endpoint='tasks.create', client_key='k1',
                            payload={'message': '甲'}, ttl_seconds=3600)
        with self.assertRaises(idempotency.IdempotencyConflict):
            await idempotency.reserve(store, principal=self.principal(),
                                endpoint='tasks.create', client_key='k1',
                                payload={'message': '乙'}, ttl_seconds=3600)

    async def test_concurrent_duplicate_is_reported_as_in_flight(self):
        """并发的重复请求只能有一个赢家；另一个不能顺手又建一个任务。"""
        from webpage_agent.store import idempotency
        store = new_store()
        await idempotency.reserve(store, principal=self.principal(),
                            endpoint='tasks.create', client_key='k1',
                            payload={'message': '甲'}, ttl_seconds=3600)
        with self.assertRaises(idempotency.IdempotencyInFlight):
            await idempotency.reserve(store, principal=self.principal(),
                                endpoint='tasks.create', client_key='k1',
                                payload={'message': '甲'}, ttl_seconds=3600)

    async def test_key_is_scoped_per_user(self):
        """同一个键给不同用户用，不该互相顶掉。"""
        from webpage_agent.store import idempotency
        store = new_store()
        await idempotency.reserve(store, principal=self.principal('u1'),
                            endpoint='tasks.create', client_key='k1',
                            payload={'message': '甲'}, ttl_seconds=3600)
        other = await idempotency.reserve(store, principal=self.principal('u2'),
                                    endpoint='tasks.create', client_key='k1',
                                    payload={'message': '乙'}, ttl_seconds=3600)
        self.assertFalse(other.replayed)

    async def test_expired_key_can_be_reused(self):
        store = new_store()
        await store.reserve_request_key('scope', client_key='k1', tenant_id='default',
                                  owner_user_id='u1', endpoint='tasks.create',
                                  fingerprint='old', expires_at=time.time() - 5)
        row = await store.reserve_request_key('scope', client_key='k1',
                                        tenant_id='default', owner_user_id='u1',
                                        endpoint='tasks.create',
                                        fingerprint='new', expires_at=time.time() + 60)
        self.assertEqual(row['fingerprint'], 'new')

    async def test_release_allows_retry_after_failure(self):
        from webpage_agent.store import idempotency
        store = new_store()
        reservation = await idempotency.reserve(
            store, principal=self.principal(), endpoint='tasks.create',
            client_key='k1', payload={'message': '甲'}, ttl_seconds=3600)
        await reservation.release()
        again = await idempotency.reserve(
            store, principal=self.principal(), endpoint='tasks.create',
            client_key='k1', payload={'message': '甲'}, ttl_seconds=3600)
        self.assertFalse(again.replayed, '失败后必须允许用同一个键重试')


class ApiIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """接口层：重试同一个键只建一个任务；换内容返回 409。"""

    async def asyncSetUp(self):
        await reset_db()
        import httpx
        import webpage_agent.api.routes as api_module
        from webpage_agent.api.api_hub import Hub
        self._saved_hub = api_module.hub
        api_module.hub = Hub(new_store())
        self.api_module = api_module
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        replacement = self.api_module.hub
        self.api_module.hub = self._saved_hub
        close = getattr(replacement, 'close', None)
        if callable(close) and replacement is not self._saved_hub:
            try:
                await close()
            except Exception:
                pass

    async def _post(self, message='做个报告', key='k1', user='u1'):
        import webpage_agent.api.api_hub as hub_module

        async def fake(text, progress=None, on_event=None, history=None,
                       on_start=None, prior=None, memory=None, ledger=None,
                       run_id=None, resume_plan=None):
            return {'task_id': 'a1', 'status': 'answered', 'timings': [],
                    'deployment': {}, 'answer': '好的'}

        headers = {'X-DSH-User': user}
        if key:
            headers['Idempotency-Key'] = key
        with patch.object(hub_module, 'run_task', fake):
            response = await self.client.post('/api/tasks', json={'message': message},
                                              headers=headers)
            if response.status_code == 200:
                record = await self.api_module.hub.get(response.json()['task_id'])
                if record is not None and record._task is not None:
                    await record._task
            return response

    async def test_retry_with_same_key_returns_the_same_task(self):
        first = (await self._post()).json()
        second = (await self._post()).json()
        self.assertEqual(first['task_id'], second['task_id'])
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertTrue(second['idempotent_replay'])
        tasks = await self.api_module.hub.store.session_tasks(first['session_id'])
        self.assertEqual(len(tasks), 1, '重试不能建出第二个任务')

    async def test_same_key_with_another_message_is_rejected(self):
        await self._post()
        res = await self._post(message='换个题目')
        self.assertEqual(res.status_code, 409)
        self.assertIn('幂等键', res.json()['detail'])

    async def test_without_key_each_request_creates_its_own_task(self):
        first = (await self._post(key=None)).json()
        second = (await self._post(key=None)).json()
        self.assertNotEqual(first['task_id'], second['task_id'])


class SessionSwitchTests(unittest.IsolatedAsyncioTestCase):
    """切会话不能停任务，切回来还要能看到已经产出的视频（用户报的两个体验问题）。"""

    async def asyncSetUp(self):
        await reset_db()

    async def test_switching_away_does_not_cancel_the_running_task(self):
        """切会话只是解绑前端事件流，服务端任务必须继续跑。"""
        import webpage_agent.api.api_hub as hub_module
        from webpage_agent.api.api_hub import Hub

        started = asyncio.Event()

        async def slow_run(text, progress=None, on_event=None, history=None,
                           on_start=None, prior=None, memory=None, ledger=None,
                           run_id=None, resume_plan=None):
            if on_start:
                await on_start('a1')
            started.set()
            await asyncio.sleep(0.4)             # 模拟"还在生成内容"
            return {'task_id': 'a1', 'status': 'succeeded',
                    'artifact': {'path': '/tmp/a1/final.mp4', 'kind': 'video'},
                    'timings': []}

        hub = Hub(new_store())
        with patch.object(hub_module, 'run_task', slow_run):
            record = await hub.start('做个视频')
            await asyncio.wait_for(started.wait(), timeout=5)
            # 前端"切走"：只是不再订阅，不该发取消
            queue, _ = await record.subscribe()
            record.unsubscribe(queue)
            await asyncio.wait_for(record._task, timeout=5)
        self.assertEqual(record.status, 'succeeded', '任务不该被切会话打断')

    async def test_timeline_exposes_artifact_so_it_can_be_replayed(self):
        """切回来要能重新渲染视频：时间线必须带产物信息，而不是只有事件。"""
        import webpage_agent.api.api_hub as hub_module
        from webpage_agent.api.api_hub import Hub

        with tempfile.TemporaryDirectory() as folder:
            mp4 = Path(folder) / 'final.mp4'
            mp4.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 64)

            async def fake_run(text, progress=None, on_event=None, history=None,
                               on_start=None, prior=None, memory=None, ledger=None,
                               run_id=None, resume_plan=None):
                if on_start:
                    await on_start('a1')
                return {'task_id': 'a1', 'status': 'succeeded',
                        'artifact': {'path': str(mp4), 'kind': 'video',
                                     'size_bytes': mp4.stat().st_size},
                        'timings': []}

            hub = Hub(new_store())
            with patch.object(hub_module, 'run_task', fake_run):
                record = await hub.start('做个视频')
                await asyncio.wait_for(record._task, timeout=5)
            timeline = await hub.session_timeline(record.session_id)
            await hub.close()

        turn = timeline['turns'][-1]
        self.assertEqual(turn['artifact']['kind'], 'video')
        self.assertEqual(turn['artifact']['filename'], 'final.mp4')
        self.assertTrue(turn['artifact']['exists'])
        self.assertFalse(turn['live'])
        self.assertEqual(timeline['live_task_id'], '')

    async def test_timeline_reports_the_live_task_of_the_session(self):
        """会话里还有在跑的任务时，前端要能拿到 task_id 重新接上事件流。"""
        import webpage_agent.api.api_hub as hub_module
        from webpage_agent.api.api_hub import Hub

        gate = asyncio.Event()

        async def slow_run(text, progress=None, on_event=None, history=None,
                           on_start=None, prior=None, memory=None, ledger=None,
                           run_id=None, resume_plan=None):
            if on_start:
                await on_start('a1')
            await gate.wait()
            return {'task_id': 'a1', 'status': 'succeeded', 'timings': []}

        hub = Hub(new_store())
        with patch.object(hub_module, 'run_task', slow_run):
            record = await hub.start('慢任务')
            await asyncio.sleep(0.1)
            timeline = await hub.session_timeline(record.session_id)
            self.assertEqual(timeline['live_task_id'], record.task_id)
            self.assertTrue(timeline['turns'][-1]['live'])
            gate.set()
            await asyncio.wait_for(record._task, timeout=5)
        await hub.close()

    def test_frontend_switch_only_detaches_the_stream(self):
        """前端源码级约束：切会话/新会话不许调用 cancel 接口。"""
        from pathlib import Path as _Path
        source = _Path('src/webpage_agent/api/web/index.html').read_text(encoding='utf-8')

        def body_of(name: str) -> str:
            start = source.index(f'async function {name}(')
            end = source.index('\n}', start)
            body = source[start:end]
            # 注释里会提到"以前这里会 POST /cancel"，只检查真代码
            return '\n'.join(line for line in body.splitlines()
                             if not line.strip().startswith('//'))

        for name in ('openSession', 'newSession'):
            body = body_of(name)
            self.assertNotIn('interrupt(', body, f'{name} 不该取消正在跑的任务')
            self.assertNotIn('/cancel', body, f'{name} 不该调用取消接口')
            self.assertIn('detachStream()', body, f'{name} 应当只解绑事件流')
        # 重新接上事件流：靠时间线给出的 live_task_id
        self.assertIn('live_task_id', body_of('openSession'))
        self.assertIn('openStream(', body_of('openSession'))
        # 产物回放要按那一轮的 task_id 取，不能沿用当前的
        self.assertIn('renderResult({artifact: withArtifact.artifact}',
                      body_of('openSession'))


if __name__ == '__main__':
    unittest.main()
