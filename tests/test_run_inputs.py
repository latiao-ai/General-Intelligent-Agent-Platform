"""运行中补充输入：接受、阻断、修订、收尾门禁（`harness-live-input` §9）。

三条机制各有一组用例：

1. **区分补充与新任务**：`POST /api/runs/{id}/inputs` 只升版本，不建 task/run；
2. **先持久化再消费**：接受是一次事务（日志 + 版本 + 事件），消费靠数据库；
3. **旧要求的新动作不准入**：准入事务比较版本，旧计划的下一个动作被拦下。

时点全部用 `asyncio.Event` 固定（工具"已进入/已放行"），不靠 sleep 猜时序；
远端调用次数是唯一可信的"到底有没有发出去"的判据。
"""
import asyncio
import tempfile
import unittest

from unittest.mock import patch

from app import execution_control
from app import run_inputs as run_inputs_module
from tests.exec_env import (Remote, install_main, install_plan, install_tools,
                            new_store, reset_db, seed_run)


class RunInputPureTests(unittest.IsolatedAsyncioTestCase):
    """组装逻辑是纯函数，先钉住它，再接数据库与图。"""

    async def test_inputs_are_ordered_by_server_sequence(self):
        items = [{'input_seq': 2, 'message': '第二条', 'intent_version': 3},
                 {'input_seq': 1, 'message': '第一条', 'intent_version': 2}]
        text = run_inputs_module.inputs_text(items)
        self.assertLess(text.index('第一条'), text.index('第二条'))
        self.assertEqual(run_inputs_module.latest_intent_version(items), 3)

    async def test_too_many_inputs_are_reported_not_truncated(self):
        items = [{'input_seq': i, 'message': f'第{i}条', 'intent_version': i}
                 for i in range(1, 6)]
        text = run_inputs_module.inputs_text(items, limit=3)
        self.assertIn('第3条', text)
        self.assertNotIn('第5条', text)
        # 超限时必须说出来，不能静默截断后假装全都读到了
        self.assertIn('2 条补充未列出', text)

    async def test_decision_input_freezes_content_and_version(self):
        decision = run_inputs_module.build_decision_input(
            original_goal='做个活动页', inputs=[
                {'input_seq': 1, 'message': '改成竖屏', 'intent_version': 2}],
            plan={'version': 1, 'intent_version': 1, 'steps': [
                {'step_id': 's1', 'tool': 'render_html', 'status': 'succeeded'}]},
            actions=[{'step_id': 's1', 'status': 'succeeded', 'effect': 'write'},
                     {'step_id': 's2', 'status': 'unknown', 'effect': 'write'}],
            intent_version=2, processed_intent_version=1)
        self.assertEqual(decision['决策依据版本'], 2)
        self.assertEqual(decision['已处理版本'], 1)
        self.assertEqual(decision['按序补充'][0]['要求'], '改成竖屏')
        self.assertEqual([a['step_id'] for a in decision['已完成动作']], ['s1'])
        # 结果未定的动作要单独列出：修订计划不能假设它没发生
        self.assertEqual([a['step_id'] for a in decision['在途或未知动作']], ['s2'])

    def test_revision_rejects_rewriting_executed_steps(self):
        kept, issues = run_inputs_module.validate_revision(
            [{'tool': 'render_html', 'step_id': 's1'},
             {'tool': 'deploy_site', 'step_id': 'p2_s1'}],
            plan_version=2, done_steps={'s1'}, unknown_steps=[],
            known_tools={'render_html', 'deploy_site'})
        self.assertEqual([step['step_id'] for step in kept], ['p2_s1'])
        self.assertEqual(issues[0]['code'], 'overwrites_executed_step')

    def test_revision_rejects_steps_depending_on_unknown_actions(self):
        kept, issues = run_inputs_module.validate_revision(
            [{'tool': 'deploy_site', 'step_id': 'p2_s1', 'depends_on': ['s1']}],
            plan_version=2, done_steps=set(), unknown_steps=[{'step_id': 's1'}],
            known_tools={'deploy_site'})
        self.assertEqual(kept, [])
        self.assertEqual(issues[0]['code'], 'depends_on_unknown_action')


class AcceptInputTests(unittest.IsolatedAsyncioTestCase):
    """接受事务：一次提交 = 一条日志 + 一版要求 + 一条事件。"""

    async def asyncSetUp(self):
        await reset_db()

    def hub(self, store, **kwargs):
        from app.api_hub import Hub
        return Hub(store, **kwargs)

    async def test_accepting_an_input_raises_the_intent_version_once(self):
        store = new_store()
        await seed_run(store, status='running')
        hub = self.hub(store)

        result = await hub.append_run_input('run1', request_id='req-1',
                                            message='改成竖屏')
        self.assertEqual(result['intent_version'], 2)
        self.assertEqual(result['input_seq'], 1)
        self.assertFalse(result['replayed'])
        run = await store.run('run1')
        self.assertEqual(run['intent_version'], 2)
        self.assertEqual(run['processed_intent_version'], 0, '还没纳入任何计划')
        # 事件与日志在同一个事务里
        inputs = await store.run_inputs('run1')
        self.assertEqual([i['message'] for i in inputs], ['改成竖屏'])
        self.assertEqual(inputs[0]['processing_status'], 'accepted')
        events = await store.events('task1')
        self.assertTrue(any(e.get('input_received') for _, e in events))
        # 身份不变：没有新建 task/run
        self.assertEqual((await store.run('run1'))['task_id'], 'task1')
        await store.close()

    async def test_same_request_id_is_replayed_not_counted_twice(self):
        """响应丢失后客户端重试：一条输入、一次版本增长。"""
        store = new_store()
        await seed_run(store, status='running')
        hub = self.hub(store)
        first = await hub.append_run_input('run1', request_id='req-1',
                                            message='改成竖屏')
        again = await hub.append_run_input('run1', request_id='req-1',
                                            message='改成竖屏')
        self.assertTrue(again['replayed'])
        self.assertEqual(again['input_id'], first['input_id'])
        self.assertEqual(again['intent_version'], first['intent_version'],
                         '重放不能把接受版本改成最新值')
        self.assertEqual((await store.run('run1'))['intent_version'], 2)
        self.assertEqual(len(await store.run_inputs('run1')), 1)
        await store.close()

    async def test_concurrent_retries_of_one_request_grow_the_version_once(self):
        store = new_store()
        await seed_run(store, status='running')
        hub = self.hub(store)
        results = await asyncio.gather(*[
            hub.append_run_input('run1', request_id='req-x', message='同一句话')
            for _ in range(4)])
        self.assertEqual(len({r['input_id'] for r in results}), 1)
        self.assertEqual((await store.run('run1'))['intent_version'], 2)
        self.assertEqual(len(await store.run_inputs('run1')), 1)
        await store.close()

    async def test_same_request_id_with_other_text_conflicts(self):
        store = new_store()
        await seed_run(store, status='running')
        hub = self.hub(store)
        await hub.append_run_input('run1', request_id='req-1', message='改成竖屏')
        with self.assertRaises(run_inputs_module.InputConflict):
            await hub.append_run_input('run1', request_id='req-1',
                                        message='改成横屏')
        await store.close()

    async def test_sequences_follow_commit_order(self):
        store = new_store()
        await seed_run(store, status='running')
        hub = self.hub(store)
        first = await hub.append_run_input('run1', request_id='req-1',
                                            message='第一句')
        second = await hub.append_run_input('run1', request_id='req-2',
                                             message='第二句')
        self.assertEqual((first['input_seq'], second['input_seq']), (1, 2))
        self.assertEqual((first['intent_version'], second['intent_version']), (2, 3))
        items = await store.run_inputs('run1')
        self.assertEqual([i['message'] for i in items], ['第一句', '第二句'])
        await store.close()

    async def test_finished_or_cancelled_runs_reject_new_inputs(self):
        store = new_store()
        await seed_run(store, status='running')
        hub = self.hub(store)
        for status, code in (('succeeded', 'run_finished'),
                             ('failed', 'run_finished'),
                             ('cancelled', 'run_cancelled')):
            await store.update_run('run1', status=status)
            with self.assertRaises(run_inputs_module.InputRejected) as ctx:
                await hub.append_run_input('run1', request_id=f'req-{status}',
                                            message='还来得及吗')
            self.assertEqual(ctx.exception.code, code)
        # 拒绝时零写入：没有日志、没有版本增长
        self.assertEqual(await store.run_inputs('run1'), [])
        self.assertEqual((await store.run('run1'))['intent_version'], 1)
        await store.close()

    async def test_an_old_run_of_the_same_task_is_rejected(self):
        """旧 run 上的输入不能被悄悄转发到新 run（文档 §2.2 最后一行）。"""
        store = new_store()
        await seed_run(store, task_id='t1', run_id='r1', status='running')
        await store.create_run('r2', 't1', status='running')     # 新的一次执行尝试
        hub = self.hub(store)
        with self.assertRaises(run_inputs_module.InputRejected) as ctx:
            await hub.append_run_input('r1', request_id='req-old', message='补一句')
        self.assertEqual(ctx.exception.code, 'run_not_current')
        self.assertEqual((await store.run('r1'))['intent_version'], 1)
        await store.close()

    async def test_backlog_limit_rejects_instead_of_dropping(self):
        store = new_store()
        await seed_run(store, status='running')
        hub = self.hub(store)
        with patch.object(store, 'append_run_input',
                          wraps=store.append_run_input):
            for index in range(2):
                await hub.append_run_input('run1', request_id=f'req-{index}',
                                            message=f'要求 {index}')
        # 上限是构造参数：用仓储层直接验一次（Hub 从配置取值）
        with self.assertRaises(run_inputs_module.InputBacklog):
            await store.append_run_input(run_id='run1', request_id='req-many',
                                         message='再来一条', max_pending=2)
        self.assertEqual(len(await store.run_inputs('run1')), 2)
        await store.close()


class IntentBlockingTests(unittest.IsolatedAsyncioTestCase):
    """机制三：新要求提交后，旧计划的下一个动作**零准入**。"""

    async def asyncSetUp(self):
        await reset_db()

    def hub(self, store, **kwargs):
        from app.api_hub import Hub
        return Hub(store, **kwargs)

    async def test_input_committed_before_admission_means_zero_remote_calls(self):
        """任务跑到一步之间收到补充：旧计划的下一步不会发出去。"""
        import scripts.run_worker as worker

        store = new_store()
        await seed_run(store)
        hub = self.hub(store, owner_id='w1')
        remote = Remote()
        tools = {'first': remote.tool('first', tool_key='local:test:first'),
                 'second': remote.tool('second', tool_key='local:test:second')}
        steps = [{'tool': 'first', 'step_id': 's1', 'args': {}},
                 {'tool': 'second', 'step_id': 's2', 'args': {}}]
        plan_patch, _ = install_plan(steps)

        revised = {'steps': [{'tool': 'second', 'step_id': 'p2_s1', 'args': {},
                              'superseded': True}],
                   'summary': '按新要求只跑第二步'}

        async def fake_revise(schema, system, user, **kwargs):
            return __import__('app.schemas', fromlist=['x']).RevisedPlan \
                .model_validate(revised), None

        from app import nodes
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), plan_patch, \
                patch.object(nodes, 'structured_call', fake_revise):
            job = asyncio.create_task(worker.run_once(hub, 'w1', run_id='run1'))
            await asyncio.wait_for(remote.enter('first').wait(), timeout=10)
            # 第一步已经准入并在执行：此时用户补一句要求
            await hub.append_run_input('run1', request_id='req-1',
                                        message='别跑第二步，改成只跑新版第二步')
            remote.gate('first').set()
            await asyncio.wait_for(job, timeout=20)

        # 旧计划的 s2 一次都没发出去；新计划 p2_s1 用的是第二个工具
        self.assertEqual(remote.name_count('second'), 1,
                         '旧步骤不派发，新计划的那一步照常执行')
        inputs = await store.run_inputs('run1')
        self.assertEqual(inputs[0]['processing_status'], 'applied')
        run = await store.run('run1')
        self.assertEqual(run['intent_version'], 2)
        self.assertEqual(run['processed_intent_version'], 2)
        self.assertEqual(run['plan_version'], 2, '修订后的计划是 v2')
        await store.close()

    async def test_admission_rejects_a_stale_intent_version(self):
        """仓储层的门禁：计划依据版本与当前要求版本不等就不给准入。"""
        store = new_store()
        await seed_run(store, status='queued')
        claimed = await store.claim_run('w1', 30.0)
        context = execution_control.ExecutionContext.from_claim(claimed)
        await store.update_run('run1', intent_version=3)

        with self.assertRaises(execution_control.IntentChanged) as ctx:
            await store.admit_action(
                context, run_id='run1', plan_version=1, step_id='s1',
                tool_key='local:test:first', args_hash='h', idempotency_key='k1',
                action_id='a1', attempt_id='at1', expected_intent_version=1)
        self.assertEqual((ctx.exception.expected, ctx.exception.actual), (1, 3))
        # 没有留下任何动作记录：这次调用根本没发出去
        self.assertEqual(await store.actions_of('run1'), [])
        await store.close()

    async def test_settlement_still_records_a_real_effect(self):
        """已准入的动作照常结算：用户改主意不能把真实效果从账本里抹掉。"""
        store = new_store()
        await seed_run(store, status='queued')
        claimed = await store.claim_run('w1', 30.0)
        context = execution_control.ExecutionContext.from_claim(claimed)
        admitted = await store.admit_action(
            context, run_id='run1', plan_version=1, step_id='s1',
            tool_key='local:test:first', args_hash='h', idempotency_key='k1',
            action_id='a1', attempt_id='at1', expected_intent_version=1)
        self.assertEqual(admitted['status'], 'admitted')
        # 准入之后用户补了一句：版本变了
        await store.append_run_input(run_id='run1', request_id='req-1',
                                     message='补充一句')
        await store.settle_action(
            context, run_id='run1', plan_version=1, step_id='s1',
            action_id='a1', attempt_id='at1', status='succeeded',
            receipt={'deployment': {'status': 'success'}},
            step_status='succeeded')
        action = (await store.actions_of('run1'))[0]
        self.assertEqual(action['status'], 'succeeded')
        self.assertEqual(action['decision_intent_version'], 1,
                         '动作要记下它依据的要求版本')
        await store.close()

    async def test_plan_commit_is_a_compare_and_set(self):
        store = new_store()
        await seed_run(store, status='queued')
        claimed = await store.claim_run('w1', 30.0)
        context = execution_control.ExecutionContext.from_claim(claimed)
        steps = [{'tool': 'first', 'step_id': 's1', 'args': {}}]
        await store.commit_plan(context, run_id='run1', version=1, goal='做个页面',
                                steps=steps, state={'plan': {'steps': steps}},
                                expected_intent_version=1, intent_version=1)
        await store.append_run_input(run_id='run1', request_id='req-1',
                                     message='改一句')
        with self.assertRaises(execution_control.IntentChanged):
            await store.commit_plan(context, run_id='run1', version=2,
                                    goal='做个页面', steps=steps,
                                    state={'plan': {'steps': steps}},
                                    expected_intent_version=1, intent_version=1)
        # 失败的计划提交不能留下第二份计划
        self.assertEqual([p['version'] for p in
                          [(await store.plan('run1'))]][0], 1)
        await store.close()

    async def test_finish_gate_refuses_while_an_input_is_unprocessed(self):
        """收尾门禁：有已接受但没纳入计划的补充时，不许声称结束。"""
        store = new_store()
        await seed_run(store, status='queued')
        claimed = await store.claim_run('w1', 30.0)
        context = execution_control.ExecutionContext.from_claim(claimed)
        await store.append_run_input(run_id='run1', request_id='req-1',
                                     message='还有个要求')
        with self.assertRaises(execution_control.IntentChanged):
            await store.finish_run_owned(context, run_id='run1', status='succeeded',
                                         expected_intent_version=2,
                                         require_no_pending_inputs=True)
        self.assertEqual((await store.run('run1'))['status'], 'running',
                         '门禁拦下时不能写终态')
        await store.close()


class ResumeWithInputTests(unittest.IsolatedAsyncioTestCase):
    """与暂停/恢复/未知的衔接（文档 §7）。"""

    async def asyncSetUp(self):
        await reset_db()

    def hub(self, store, **kwargs):
        from app.api_hub import Hub
        return Hub(store, **kwargs)

    async def test_input_while_paused_does_not_resume_automatically(self):
        import scripts.run_worker as worker

        store = new_store()
        await seed_run(store)
        hub = self.hub(store, owner_id='w1')
        remote = Remote()
        tools = {'first': remote.tool('first', tool_key='local:test:first'),
                 'second': remote.tool('second', tool_key='local:test:second')}
        steps = [{'tool': 'first', 'step_id': 's1', 'args': {}},
                 {'tool': 'second', 'step_id': 's2', 'args': {}}]
        plan_patch, _ = install_plan(steps)
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), plan_patch:
            job = asyncio.create_task(worker.run_once(hub, 'w1', run_id='run1'))
            await asyncio.wait_for(remote.enter('first').wait(), timeout=10)
            await hub.pause('run1')
            remote.gate('first').set()
            await asyncio.wait_for(job, timeout=20)
            self.assertEqual((await store.run('run1'))['status'], 'paused')

            # 暂停期间补一句：接受，但**不**自动恢复
            accepted = await hub.append_run_input('run1', request_id='req-1',
                                                   message='顺便改名')
            self.assertEqual(accepted['status'], 'accepted')
            self.assertEqual((await store.run('run1'))['status'], 'paused')

            revised = {'steps': [{'tool': 'second', 'step_id': 'p2_s1', 'args': {}}],
                       'summary': '按新要求继续'}
            from app import nodes
            from app.schemas import RevisedPlan

            async def fake_revise(schema, system, user, **kwargs):
                return RevisedPlan.model_validate(revised), None

            with patch.object(nodes, 'structured_call', fake_revise):
                resumed = await hub.resume('run1')
                self.assertIsNotNone(resumed, '暂停的 run 应该能续跑')
                await asyncio.wait_for(resumed._task, timeout=20)

        # 续跑先把补充纳入计划（v2），再执行新计划的步骤
        run = await store.run('run1')
        self.assertEqual(run['plan_version'], 2)
        self.assertEqual(run['processed_intent_version'], 2)
        self.assertEqual(remote.name_count('second'), 1)
        await store.close()


if __name__ == '__main__':
    unittest.main()


class RunInputApiTests(unittest.IsolatedAsyncioTestCase):
    """HTTP 层：202 / 错误映射 / 读回 / 功能开关 / 归属。"""

    async def asyncSetUp(self):
        await reset_db()
        import httpx
        import app.api as api_module
        from app.api_hub import Hub
        api_module.hub = Hub(new_store())
        self.api_module = api_module
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url='http://test')
        await seed_run(api_module.hub.store, status='running')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.api_module.hub.store.close()

    async def _post(self, message='改成竖屏', request_id='req-1', run_id='run1'):
        return await self.client.post(
            f'/api/runs/{run_id}/inputs',
            json={'request_id': request_id, 'message': message})

    async def test_post_returns_202_and_versions(self):
        res = await self._post()
        self.assertEqual(res.status_code, 202)
        data = res.json()
        self.assertEqual(data['status'], 'accepted')
        self.assertEqual(data['intent_version'], 2)
        self.assertEqual(data['processed_intent_version'], 0)
        self.assertEqual(data['input_seq'], 1)
        self.assertEqual(data['run_id'], 'run1')
        self.assertFalse(data['replayed'])

    async def test_retry_returns_the_same_input(self):
        first = (await self._post()).json()
        again = await self._post()
        self.assertEqual(again.status_code, 202)
        body = again.json()
        self.assertTrue(body['replayed'])
        self.assertEqual(body['input_id'], first['input_id'])
        self.assertEqual(body['intent_version'], first['intent_version'])

    async def test_conflicting_retry_is_409(self):
        await self._post(message='改成竖屏')
        res = await self._post(message='改成横屏')
        self.assertEqual(res.status_code, 409)
        self.assertIn('input_request_conflict', res.json()['detail'])

    async def test_finished_run_is_409_with_a_clear_code(self):
        await self.api_module.hub.store.update_run('run1', status='succeeded')
        res = await self._post()
        self.assertEqual(res.status_code, 409)
        self.assertIn('run_finished', res.json()['detail'])

    async def test_get_reads_back_processing_status(self):
        await self._post(message='第一句', request_id='req-1')
        await self._post(message='第二句', request_id='req-2')
        res = await self.client.get('/api/runs/run1/inputs')
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual([i['message'] for i in body['inputs']],
                         ['第一句', '第二句'])
        self.assertEqual(body['intent_version'], 3)
        self.assertEqual(body['pending_input_count'], 2)
        after = (await self.client.get(
            '/api/runs/run1/inputs?after_seq=1')).json()
        self.assertEqual([i['message'] for i in after['inputs']], ['第二句'])

    async def test_unknown_run_is_404(self):
        res = await self._post(run_id='nope')
        self.assertEqual(res.status_code, 404)

    async def test_bad_request_id_is_422(self):
        res = await self._post(request_id='bad id!')
        self.assertEqual(res.status_code, 422)

    async def test_feature_flag_off_hides_the_endpoint(self):
        from dataclasses import replace
        from unittest.mock import patch
        from app.config import settings
        with patch.object(self.api_module, 'settings',
                          replace(settings, run_inputs_enabled=False)):
            res = await self._post()
        self.assertEqual(res.status_code, 404)
        self.assertIn('未开放', res.json()['detail'])

    async def test_other_owner_cannot_send_inputs(self):
        """别人的 run 不能被补要求：按项目约定回 404，不泄漏存在性。"""
        store = self.api_module.hub.store
        await store.ensure_task_meta('task1', session_id='sess1', goal='g',
                                     owner_user_id='alice')
        res = await self.client.post(
            '/api/runs/run1/inputs',
            json={'request_id': 'req-9', 'message': '插一句'},
            headers={'X-DSH-User': 'bob'})
        self.assertEqual(res.status_code, 404)
