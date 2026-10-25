"""结果观察与自主决策的确定性用例。

边界很清楚：

- **程序观察**：每个工具之后都跑，不花模型请求；
- **模型决策**：只有片段结束、或结果里有需要判断的新事实时才烧一次；
- **完成核对**：程序先查证据，模型说"完成"不算数。

因此这里的断言都盯着三件可数的事实：**模型请求次数**、**远端工具调用次数**、
**库里的版本/水位**。不靠"看起来跑通了"。
"""
import asyncio
import json
import tempfile
import unittest

from unittest.mock import patch

from app import observation as obs
from app import observation as observation_module
from app.schemas import ExecutionPlan, ObservationDecision, RevisedPlan
from tests.exec_env import (Remote, install_main, install_tools, new_store,
                            override_settings, reset_db, seed_run)


class FakeModel:
    """受控模型：按 schema 分流，并记录**每一次**真实请求。

    `structured_call` 是模型请求的唯一入口，所以这里数到的次数就是真实成本。
    """

    def __init__(self, *, plan=None, decisions=None, revisions=None):
        self.calls: list[str] = []
        self.plan = plan
        self.decisions = list(decisions or [])
        self.revisions = list(revisions or [])
        self.decision_inputs: list[str] = []

    async def __call__(self, schema, system, user, **kwargs):
        name = getattr(schema, '__name__', str(schema))
        self.calls.append(name)
        if schema is ExecutionPlan:
            return ExecutionPlan.model_validate(self.plan or {}), {'total_tokens': 10}
        if schema is RevisedPlan:
            return RevisedPlan.model_validate(self.revisions.pop(0)), {}
        if schema is ObservationDecision:
            self.decision_inputs.append(user)
            return ObservationDecision.model_validate(self.decisions.pop(0)), {}
        raise AssertionError(f'意外的模型调用：{name}')

    def count(self, name: str) -> int:
        return len([c for c in self.calls if c == name])


class ObservationPureTests(unittest.TestCase):
    """观察层是纯函数，先钉住合同。"""

    def test_summary_keeps_structure_and_length(self):
        payload = {'items': [{'id': f'shot-{i}', 'seconds': 3} for i in range(40)],
                   'total': 40}
        summary = obs.summarize(payload)
        # 只给前几项，但**必须**给出总数：否则模型会以为一共只有 3 个
        self.assertEqual(summary['items']['total_items'], 40)
        self.assertTrue(summary['items']['truncated'])
        self.assertEqual(len(summary['items']['items']), obs.SUMMARY_ITEMS)

    def test_pagination_signals_are_reported(self):
        page = obs.pagination({'items': [], 'total': 40, 'next_cursor': 'abc'})
        self.assertEqual(page['declared_total'], 40)
        self.assertEqual(page['next_read_ref'], 'abc')
        result = obs.build_result(step_id='s1', tool_key='t', payload={'items': [1],
                                                                      'has_more': True})
        self.assertFalse(result['complete'], '有下一页时不能声称拿到全部')

    def test_routing_prefers_control_and_unknown(self):
        base = {'segment_end': True, 'after_segment': 'observe'}
        self.assertEqual(obs.route_after_step({**base, 'paused': True})[0],
                         obs.ROUTE_CONTROL)
        self.assertEqual(obs.route_after_step({**base, 'intent_changed': True})[0],
                         obs.ROUTE_REVISE)
        self.assertEqual(obs.route_after_step({**base, 'unknown_write': True})[0],
                         obs.ROUTE_RECONCILE)
        self.assertEqual(obs.route_after_step({**base})[0], obs.ROUTE_DECIDE)

    def test_deterministic_segment_does_not_need_a_model(self):
        signals = {'segment_end': False, 'after_segment': 'verify'}
        self.assertEqual(obs.route_after_step(signals)[0], obs.ROUTE_STEP)

    def test_artifact_criterion_accepts_a_human_description(self):
        """完成条件的 target 常是人话（"HTML 页面"），不能拿它当路径去查文件。

        踩过的坑：真实模型写 `{'kind':'artifact_exists','target':'HTML 页面'}`，
        检查器把它当路径 → "产物路径不存在" → 明明产出了 HTML 却报部分完成。
        """
        state = {'artifact': {'html_path': 'artifacts/t1/sources.html'}}
        result = obs.completion_state(
            [{'kind': 'artifact_exists', 'target': 'HTML 页面'}],
            state=state, files_exist=lambda p: p.endswith('sources.html'))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['verified'][0]['evidence'],
                         ['artifact:artifacts/t1/sources.html'])
        self.assertFalse(obs.looks_like_path('HTML 页面'))
        self.assertTrue(obs.looks_like_path('artifacts/t1/sources.html'))

    def test_web_search_name_satisfies_tool_succeeded(self):
        """公开名 web_search 必须对上 local:web:search，末尾的 search 不算。"""
        result = {'step_id': 's1', 'tool_key': 'local:web:search',
                  'execution_status': 'succeeded'}
        hit = obs.completion_state(
            [{'kind': 'tool_succeeded', 'target': 'web_search'}],
            state={}, results=[result])
        self.assertEqual(hit['unmet'], [], hit)
        self.assertEqual(len(hit['verified']), 1)
        miss = obs.completion_state(
            [{'kind': 'tool_succeeded', 'target': 'search'}],
            state={}, results=[result])
        self.assertEqual(len(miss['unmet']), 1, miss)
        named = obs.completion_state(
            [{'kind': 'tool_succeeded', 'target': 'web_search'}],
            state={}, results=[{'step_id': 's1', 'tool_key': 'local:web:search',
                                'tool_name': 'web_search',
                                'execution_status': 'succeeded'}])
        self.assertTrue(named['ok'], named)

    def test_completion_requires_evidence(self):
        state = {'artifact': {'html_path': '/tmp/not-exists.html'}}
        criteria = obs.criteria_of({}, state)
        result = obs.completion_state(criteria, state=state,
                                      files_exist=lambda path: False)
        self.assertFalse(result['ok'])
        self.assertEqual(result['unmet'][0]['kind'], 'artifact_exists')
        # 需要看内容的条件只能标"未检查"，程序不替它下结论
        result2 = obs.completion_state(
            [{'kind': 'content_quality', 'target': '镜头内容'}], state=state,
            files_exist=lambda path: True)
        self.assertEqual(len(result2['unchecked']), 1)
        self.assertTrue(result2['ok'], '未检查不等于未满足')


class ObserveDecideTests(unittest.IsolatedAsyncioTestCase):
    """图上跑真链路：真实 PG、假模型、假工具。"""

    async def asyncSetUp(self):
        await reset_db()

    def hub(self, store, **kwargs):
        from app.api_hub import Hub
        return Hub(store, **kwargs)

    async def run_plan(self, *, plan, tools, decisions=None, revisions=None,
                       observe_enabled=True, steps_extra=None):
        """跑一条 run，返回 (store, remote, model)。"""
        import scripts.run_worker as worker

        store = new_store()
        await seed_run(store)
        remote = Remote()
        for name, tool in (tools or {}).items():
            pass
        model = FakeModel(plan=plan, decisions=decisions, revisions=revisions)
        hub = self.hub(store, owner_id='w1')
        stack = install_tools(tools or {})
        from app import nodes as nodes_module
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                stack, patch.object(nodes_module, 'structured_call', model), \
                override_settings(observe_enabled=observe_enabled):
            await worker.run_once(hub, 'w1', run_id='run1')
        return store, remote, model

    async def test_deterministic_chain_never_calls_the_observation_model(self):
        """读取已知产物 → 打包 → 导出：观察模型请求必须是 0。"""
        from app.tools import Tool

        calls = []

        async def first(args, state):
            calls.append('first')
            # 产物必须**真实存在**：完成核对会去查文件，编一个路径过不了关
            from pathlib import Path
            path = Path(state.get('artifact_dir') or '.') / 'out.html'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('<html>ok</html>', encoding='utf-8')
            return {'artifact': {'html_path': str(path)},
                    'written': {'relative': 'report.md'}}

        async def second(args, state):
            calls.append('second')
            return {'answer': '已导出'}

        tools = {
            'make': Tool('make', '生成', '', {}, first,
                         tool_key='local:test:make', effect='write'),
            'pack': Tool('pack', '打包导出', '', {}, second,
                         tool_key='local:test:pack', effect='write'),
        }
        plan = {'steps': [{'tool': 'make', 'step_id': 'p1_s1', 'args': {'path': 'x'}},
                          {'tool': 'pack', 'step_id': 'p1_s2', 'args': {},
                           'depends_on': ['p1_s1']}],
                'summary': '确定链路', 'after_segment': 'verify'}
        store, remote, model = await self.run_plan(plan=plan, tools=tools)

        self.assertEqual(calls, ['first', 'second'])
        self.assertEqual(model.count('ObservationDecision'), 0,
                         '确定步骤不该为观察付出模型请求')
        self.assertEqual(await store.run_decisions('run1'), [])
        self.assertEqual((await store.run('run1'))['status'], 'succeeded')
        await store.close()

    async def test_observe_segment_lets_the_model_pick_from_real_results(self):
        """先列清单、再挑片段：模型看到的是**真实返回**，不是它自己猜的 ID。"""
        from app.tools import Tool

        listing = {'shots': [{'id': 'shot-a', 'seconds': 4},
                             {'id': 'shot-b', 'seconds': 9}], 'total': 2}

        async def list_shots(args, state):
            return {'payload': listing}

        async def cut(args, state):
            return {'answer': f'剪好了 {args.get("pick")}'}

        tools = {
            'list_shots': Tool('list_shots', '列出素材', '', {}, list_shots,
                               tool_key='local:test:list', effect='read'),
            'cut': Tool('cut', '剪辑', '', {}, cut,
                        tool_key='local:test:cut', effect='write'),
        }
        plan = {'steps': [{'tool': 'list_shots', 'step_id': 'p1_s1', 'args': {}}],
                'summary': '先查清素材', 'after_segment': 'observe',
                'observation_goal': '根据真实清单决定读哪些素材'}
        decisions = [{'kind': 'revise', 'reason': '清单里有 2 个候选',
                      'steps': [{'tool': 'cut', 'step_id': 'p2_s1',
                                 'args': {'pick': {'ref': {'step_id': 'p1_s1',
                                                           'pointer': '/payload'}}}}],
                      'after_segment': 'verify'}]
        store, remote, model = await self.run_plan(plan=plan, tools=tools,
                                                   decisions=decisions)

        self.assertEqual(model.count('ObservationDecision'), 1)
        # 决策输入里必须有真实返回的摘要（含真实 ID 与总数）
        self.assertIn('shot-a', model.decision_inputs[0])
        self.assertIn('total_items', model.decision_inputs[0])
        run = await store.run('run1')
        self.assertEqual(run['plan_version'], 2, '观察后的新片段是 v2')
        self.assertEqual(run['intent_version'], 1, '自主调整不改用户要求版本')
        decisions_rows = await store.run_decisions('run1')
        self.assertEqual(len(decisions_rows), 1)
        self.assertEqual(decisions_rows[0]['decision_kind'], 'revise')
        self.assertEqual(decisions_rows[0]['status'], 'candidate')
        steps = await store.steps_of('run1', 2)
        self.assertEqual([s['step_id'] for s in steps], ['p2_s1'])
        await store.close()

    async def test_full_result_is_stored_and_readable_by_reference(self):
        """结果完整落盘：摘要只给前几项，细节按引用受控读取。"""
        from app import files as files_module
        from app.tools import Tool

        big = {'shots': [{'id': f'shot-{i}', 'seconds': i} for i in range(40)]}

        async def list_shots(args, state):
            return {'payload': big}

        tools = {'list_shots': Tool('list_shots', '列出素材', '', {}, list_shots,
                                    tool_key='local:test:list', effect='read')}
        plan = {'steps': [{'tool': 'list_shots', 'step_id': 'p1_s1', 'args': {}}],
                'summary': '查素材', 'after_segment': 'observe',
                'observation_goal': '决定下一步'}
        decisions = [{'kind': 'stop', 'reason': '先停下', 'stop_reason': '演示用'}]
        store, remote, model = await self.run_plan(plan=plan, tools=tools,
                                                   decisions=decisions)
        task = await store.task('task1')
        results = (task['state'] or {}).get('tool_results') or []
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result['data_ref'].startswith('results/'))
        self.assertEqual(result['summary']['shots']['total_items'], 40)
        folder = (task['state'] or {}).get('artifact_dir')
        chunk = files_module.read_result(folder, result['data_ref'], limit=200)
        self.assertEqual(chunk['ref'], result['data_ref'])
        self.assertLess(len(chunk['text']), chunk['total'])
        full = files_module.read_result(folder, result['data_ref'], limit=100000)
        self.assertIn('shot-39', full['text'])
        await store.close()

    async def test_empty_continue_is_rejected_instead_of_spinning(self):
        """片段结束又没有剩余步骤时，continue 就是空转：直接去核对完成条件。"""
        from app.tools import Tool

        async def only(args, state):
            return {'answer': '做完了'}

        tools = {'only': Tool('only', '做事', '', {}, only,
                              tool_key='local:test:only', effect='read')}
        plan = {'steps': [{'tool': 'only', 'step_id': 'p1_s1', 'args': {}}],
                'summary': '一步', 'after_segment': 'observe',
                'observation_goal': '看看要不要继续'}
        decisions = [{'kind': 'continue', 'reason': '继续'}]
        store, remote, model = await self.run_plan(plan=plan, tools=tools,
                                                   decisions=decisions)
        # 只问了一次模型；它说 continue 但没有剩余步骤，于是转完成核对
        self.assertEqual(model.count('ObservationDecision'), 1)
        run = await store.run('run1')
        self.assertEqual(run['status'], 'succeeded')
        self.assertEqual((await store.run_decisions('run1'))[0]['decision_kind'],
                         'continue')
        await store.close()

    async def test_wait_input_stops_and_asks_the_user(self):
        from app.tools import Tool

        async def listing(args, state):
            return {'payload': {'shots': []}}

        tools = {'list_shots': Tool('list_shots', '列出素材', '', {}, listing,
                                    tool_key='local:test:list', effect='read')}
        plan = {'steps': [{'tool': 'list_shots', 'step_id': 'p1_s1', 'args': {}}],
                'summary': '查素材', 'after_segment': 'observe',
                'observation_goal': '决定下一步'}
        decisions = [{'kind': 'wait_input', 'reason': '一个可用素材都没有',
                      'questions': ['要不要换一个项目？']}]
        store, remote, model = await self.run_plan(plan=plan, tools=tools,
                                                   decisions=decisions)
        run = await store.run('run1')
        self.assertEqual(run['status'], 'waiting_input')
        self.assertIn('换一个项目', run['error'])
        await store.close()

    async def test_finish_without_evidence_is_not_reported_as_success(self):
        """模型说"完成了"不算数：缺产物/回执时不能成功。"""
        from app.tools import Tool

        async def nothing(args, state):
            return {'status': 'empty'}

        tools = {'noop': Tool('noop', '空动作', '', {}, nothing,
                              tool_key='local:test:noop', effect='read')}
        plan = {'steps': [{'tool': 'noop', 'step_id': 'p1_s1', 'args': {}}],
                'summary': '一步', 'after_segment': 'observe',
                'observation_goal': '决定是否结束',
                'completion_criteria': [{'kind': 'file_written',
                                         'target': 'report.md'}]}
        decisions = [{'kind': 'finish', 'reason': '我觉得可以了'}]
        store, remote, model = await self.run_plan(plan=plan, tools=tools,
                                                   decisions=decisions)
        run = await store.run('run1')
        self.assertNotEqual(run['status'], 'succeeded',
                            '没有证据时不能因为模型说完成就成功')
        self.assertIn('file_written', run['error'])
        await store.close()

    async def test_observe_disabled_keeps_the_old_deterministic_behaviour(self):
        """开关关闭：观察点不再叫模型，直接核对完成条件。"""
        from app.tools import Tool

        async def listing(args, state):
            return {'payload': {'shots': [{'id': 'shot-a'}]}}

        tools = {'list_shots': Tool('list_shots', '列出素材', '', {}, listing,
                                    tool_key='local:test:list', effect='read')}
        plan = {'steps': [{'tool': 'list_shots', 'step_id': 'p1_s1', 'args': {}}],
                'summary': '查素材', 'after_segment': 'observe',
                'observation_goal': '决定下一步'}
        store, remote, model = await self.run_plan(plan=plan, tools=tools,
                                                   decisions=[],
                                                   observe_enabled=False)
        self.assertEqual(model.count('ObservationDecision'), 0)
        self.assertEqual(await store.run_decisions('run1'), [])
        await store.close()

    async def test_decision_budget_stops_instead_of_spinning(self):
        """决策预算用完：保存已完成事实并停下，不是无限重试。"""
        from app.tools import Tool

        async def listing(args, state):
            return {'payload': {'shots': [{'id': 'shot-a'}]}}

        tools = {'list_shots': Tool('list_shots', '列出素材', '', {}, listing,
                                    tool_key='local:test:list', effect='read')}
        plan = {'steps': [{'tool': 'list_shots', 'step_id': 'p1_s1', 'args': {}}],
                'summary': '查素材', 'after_segment': 'observe',
                'observation_goal': '决定下一步'}
        decisions = [{'kind': 'wait_input', 'reason': 'x', 'questions': ['q']}]
        store = new_store()
        await seed_run(store)
        remote = Remote()
        model = FakeModel(plan=plan, decisions=decisions)
        hub = self.hub(store, owner_id='w1')
        import scripts.run_worker as worker
        from app import nodes as nodes_module
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), \
                patch.object(nodes_module, 'structured_call', model), \
                override_settings(run_max_decisions=0):
            await worker.run_once(hub, 'w1', run_id='run1')
        run = await store.run('run1')
        self.assertEqual(run['status'], 'waiting_input')
        self.assertIn('预算', run['error'])
        self.assertEqual(model.count('ObservationDecision'), 0,
                         '预算为 0 时连一次模型请求都不该发')
        await store.close()

    async def test_unknown_write_is_reconciled_not_redecided(self):
        """写操作结果未定：先核实，绝不让模型猜成功或重发。"""
        store = new_store()
        await seed_run(store)
        signals = {'segment_end': True, 'after_segment': 'observe',
                   'unknown_write': True, 'unknown_actions': [
                       {'step_id': 's1', 'tool_key': 'mcp:x:deploy',
                        'effect': 'write'}]}
        route, why = obs.route_after_step(signals)
        self.assertEqual(route, obs.ROUTE_RECONCILE)
        self.assertIn('核实', why)
        await store.close()


class ProgressGuardTests(unittest.IsolatedAsyncioTestCase):
    """无进展保护：同样的事实反复出现不许继续烧预算。"""

    async def asyncSetUp(self):
        await reset_db()

    def hub(self, store, **kwargs):
        from app.api_hub import Hub
        return Hub(store, **kwargs)

    async def test_repeated_fingerprint_stops_the_loop(self):
        from app.tools import Tool

        store = new_store()
        await seed_run(store)
        remote = Remote()

        async def listing(args, state):
            return {'payload': {'shots': [{'id': 'shot-a'}]}}

        tools = {'list_shots': Tool('list_shots', '列出素材', '', {}, listing,
                                    tool_key='local:test:list', effect='read')}
        # 模型每次都给"再查一次同样的东西"：这正是要拦住的无进展循环
        plan = {'steps': [{'tool': 'list_shots', 'step_id': 'p1_s1', 'args': {}}],
                'summary': '查素材', 'after_segment': 'observe',
                'observation_goal': '决定下一步'}
        decisions = [{'kind': 'revise', 'reason': '再查一次',
                      'steps': [{'tool': 'list_shots', 'step_id': f'p{v}_s1',
                                 'args': {}}], 'after_segment': 'observe',
                      'observation_goal': '再决定'} for v in (2, 3, 4)]
        model = FakeModel(plan=plan, decisions=decisions)
        hub = self.hub(store, owner_id='w1')
        import scripts.run_worker as worker
        from app import nodes as nodes_module
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), \
                patch.object(nodes_module, 'structured_call', model), \
                override_settings(run_max_no_progress=1):
            await worker.run_once(hub, 'w1', run_id='run1')
        run = await store.run('run1')
        self.assertEqual(run['status'], 'waiting_input')
        self.assertIn('没有带来新事实', run['error'])
        self.assertLessEqual(model.count('ObservationDecision'), 3,
                            '无进展保护必须在预算耗尽之前就拦住')
        await store.close()


if __name__ == '__main__':
    unittest.main()
