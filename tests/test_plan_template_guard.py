"""跨步骤取值的字面模板必须在**规划阶段**被拦下。

真实故障：修订后的计划把跨步骤取值写成了字面模板
`"expectedRevision": "{{p2_s1.revision}}"`。`validate_revision` 只看步骤 ID、工具和
依赖；`validate_plan` 当时也只认 `{"ref": ...}` 对象，字符串模板一路放行。坏计划
进了执行，最后在远端调用前炸成「本次未完成：MCP·canvas.nodes.create 失败：参数
expectedRevision 还是未解析的占位符」——本该退回模型改一行的规划错误，变成了用户
看到的一次失败，而且平台侧已经准备好要发请求了。

两处修补：

1. `validate_plan` 把 `{{...}}` 字面模板当成阻断级错误（`unresolved_template`），
   嵌套对象/数组里也查；
2. `revise_plan` 不再把 `validate_plan` 的阻断级错误当"记一笔就往下走"，而是走
   退回修正：第一版交给模型改一次，改不好才停在等人，一步都不执行。
"""
import json
import tempfile
import unittest

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.agent import nodes
from webpage_agent.agent import run_inputs as run_inputs_module
from webpage_agent.core.schemas import ExecutionPlan, RevisedPlan, ToolSelection

from tests.exec_env import (Remote, install_main, install_tools, new_store,
                            reset_db, seed_run)
from tests.test_revision_repair import (GOAL, SUPPLEMENT, catalog, corrected)


TEMPLATE = '{{p2_s1.revision}}'

TEMPLATE_STEPS = [
    {'step_id': 'p2_s1', 'tool': 'canvas_get', 'args': {}, 'depends_on': []},
    {'step_id': 'p2_s2', 'tool': 'nodes_create',
     'args': {'name': '厦门', 'expectedRevision': TEMPLATE},
     'depends_on': ['p2_s1']},
    {'step_id': 'p2_s3', 'tool': 'prompt_update',
     'args': {'prompt': '鼓浪屿'}, 'depends_on': ['p2_s2']},
]

REF_STEPS = [
    {'step_id': 'p2_s1', 'tool': 'canvas_get', 'args': {}, 'depends_on': []},
    {'step_id': 'p2_s2', 'tool': 'nodes_create',
     'args': {'name': '厦门',
              'expectedRevision': {'ref': {'step_id': 'p2_s1',
                                           'pointer': '/revision'}}},
     'depends_on': ['p2_s1']},
    {'step_id': 'p2_s3', 'tool': 'prompt_update',
     'args': {'prompt': '鼓浪屿'}, 'depends_on': ['p2_s2']},
]

CRITERIA = [{'kind': 'tool_succeeded', 'target': 'nodes_create'},
            {'kind': 'tool_succeeded', 'target': 'prompt_update'}]


def _renumber(steps):
    """把 p2_s* 改写成 p1_*，同时改 depends_on 和 ref 里的 step_id。"""
    mapping = {step['step_id']: f'p1_{index}'
               for index, step in enumerate(steps, 1)}
    out = []
    for step in steps:
        item = json.loads(json.dumps(step))
        item['step_id'] = mapping[item['step_id']]
        item['depends_on'] = [mapping.get(dep, dep)
                              for dep in item.get('depends_on') or []]
        bindings = item.get('args', {}).get('expectedRevision')
        if isinstance(bindings, dict) and 'ref' in bindings:
            bindings['ref']['step_id'] = mapping.get(bindings['ref']['step_id'],
                                                     bindings['ref']['step_id'])
        out.append(item)
    return out


def templated():
    return RevisedPlan.model_validate({
        'steps': TEMPLATE_STEPS, 'result_locus': 'platform_change',
        'completion_criteria': CRITERIA, 'summary': '读取画布后创建厦门节点'})

def with_ref():
    return RevisedPlan.model_validate({
        'steps': REF_STEPS, 'result_locus': 'platform_change',
        'completion_criteria': CRITERIA, 'summary': '读取画布后创建厦门节点'})


def both_wrong():
    """截图两次分别出现的两种错，出现在同一份计划里。"""
    return RevisedPlan.model_validate({
        'steps': TEMPLATE_STEPS, 'result_locus': 'platform_state',
        'completion_criteria': CRITERIA, 'summary': '读取画布后创建厦门节点'})


class TemplateValidationTests(unittest.TestCase):
    """纯校验：模板、绑定、路径各一条。"""

    def test_literal_template_is_a_blocking_issue(self):
        steps, issues = nodes.validate_plan(TEMPLATE_STEPS, catalog())
        codes = [item['code'] for item in issues]
        self.assertIn('unresolved_template', codes)
        detail = next(i['detail'] for i in issues
                      if i['code'] == 'unresolved_template')
        self.assertIn('expectedRevision', detail)
        self.assertIn('ref', detail, '错误里要写清正确语法')
        # 步骤本身留着：退回修正要模型看到自己写了什么
        self.assertEqual([s['step_id'] for s in steps], ['p2_s1', 'p2_s2', 'p2_s3'])

    def test_nested_template_is_found(self):
        _, issues = nodes.validate_plan(
            [{'tool': 'nodes_create', 'step_id': 's1',
              'args': {'patch': {'prompt': {'text': '{{s0.text}}'}}}}], catalog())
        detail = next(i['detail'] for i in issues
                      if i['code'] == 'unresolved_template')
        self.assertIn('patch.prompt.text', detail)

    def test_ref_binding_is_not_a_template(self):
        _, issues = nodes.validate_plan(REF_STEPS, catalog())
        self.assertNotIn('unresolved_template', [i['code'] for i in issues])

    def test_rules_teach_the_binding_syntax(self):
        rules = run_inputs_module.revision_rules()
        self.assertIn('"ref"', rules)
        self.assertIn('不要写', rules)


class RevisionTemplateRepairTests(unittest.IsolatedAsyncioTestCase):
    """修订环节：模板被当成可修复错误交回模型。"""

    def store(self):
        return SimpleNamespace(
            run_intent=AsyncMock(return_value={
                'intent_version': 2, 'processed_intent_version': 1,
                'plan_version': 0}),
            pending_run_inputs=AsyncMock(return_value=[
                {'input_seq': 1, 'message': SUPPLEMENT, 'intent_version': 2}]),
            actions_of=AsyncMock(return_value=[]),
            plan=AsyncMock(return_value={}),
            run_snapshot=AsyncMock(return_value={}))

    async def revise(self, answers):
        answers = list(answers)
        requests = []

        async def model(schema, system, user, **kwargs):
            requests.append(json.loads(user))
            if not answers:
                raise AssertionError('模型被多问了一次')
            return answers.pop(0), {}

        with patch.object(nodes, '_store_and_run',
                          AsyncMock(return_value=(self.store(), 'r1'))), \
                patch.object(nodes, '_tool_context', AsyncMock(return_value=None)), \
                patch.object(nodes.tool_module, 'catalog',
                             AsyncMock(return_value=catalog())), \
                patch.object(nodes, 'structured_call', model):
            result = await nodes.revise_plan(
                {'original_goal': GOAL, 'user_input': GOAL, 'usage': [],
                 'intent_version': 1})
        return result, requests

    async def test_template_in_revision_is_sent_back_for_repair(self):
        result, requests = await self.revise([templated(), with_ref()])
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(len(requests), 2)
        repair = json.dumps(requests[1]['校验错误'], ensure_ascii=False)
        self.assertIn('unresolved_template', repair)
        self.assertEqual(requests[1]['上次的调整']['steps'][1]['args']['expectedRevision'],
                         TEMPLATE, '模型要看到自己写的那一行')
        self.assertEqual(result['plan']['steps'][1]['args']['expectedRevision'],
                         {'ref': {'step_id': 'p2_s1', 'pointer': '/revision'}})

    async def test_template_twice_stops_without_executing(self):
        result, requests = await self.revise([templated(), templated()])
        self.assertEqual(len(requests), 2)
        self.assertEqual(result['status'], 'waiting_input')
        self.assertIn('unresolved_template', result['failure'])
        self.assertNotIn('plan', result)

    async def test_all_three_kinds_of_error_are_reported_at_once(self):
        """同一份计划里既有落点不一致又有字面模板：一次报全，模型一次改全。"""
        result, requests = await self.revise([both_wrong(), with_ref()])
        codes = {item['code'] for item in requests[1]['校验错误']}
        self.assertEqual(codes, {'unresolved_template',
                                 'platform_state_cannot_write'})
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['plan']['result_locus'], 'platform_change')


class TemplateGraphTests(unittest.IsolatedAsyncioTestCase):
    """整条图：模板修好之后真的发得出去，修不好就一个请求都不发。"""

    async def asyncSetUp(self):
        await reset_db()

    def tools(self):
        remote = Remote()
        return remote, {
            'canvas_get': remote.tool(
                'canvas_get', effect='read', tool_key='mcp:beeftv:canvas.get',
                output={'revision': 7}),
            'nodes_create': remote.tool(
                'nodes_create', effect='write',
                tool_key='mcp:beeftv:canvas.nodes.create',
                output={'node': {'id': 'n1'}}),
            'prompt_update': remote.tool(
                'prompt_update', effect='write',
                tool_key='mcp:beeftv:canvas.node.prompt.update',
                output={'ok': True}),
        }

    async def run_worker(self, store, answers, *, supplement=True):
        import scripts.run_worker as worker
        from webpage_agent.api.api_hub import Hub

        remote, tools = self.tools()
        answers = list(answers)
        requests = []

        async def model(schema, system, user, **kwargs):
            if getattr(schema, '__name__', '') == 'ToolSelection':
                return ToolSelection(tools=[0, 1, 2]), {}
            requests.append(json.loads(user))
            if not answers:
                raise AssertionError('模型被多问了一次')
            return answers.pop(0), {}

        if supplement:
            await store.append_run_input(run_id='run1', request_id='req-1',
                                         message=SUPPLEMENT)
        hub = Hub(store, owner_id='w1')
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), \
                patch.object(nodes, 'structured_call', model):
            await worker.run_once(hub, 'w1', run_id='run1')
        return (remote, await store.run('run1'), await store.plan('run1'),
                await store.actions_of('run1'), requests)

    async def test_repaired_template_resolves_and_the_write_goes_out(self):
        store = new_store()
        await seed_run(store, status='queued', message=GOAL)
        remote, run, plan, actions, requests = await self.run_worker(
            store, [templated(), with_ref()])
        self.assertEqual(run['status'], 'succeeded', run.get('error'))
        self.assertEqual(len(requests), 2)
        self.assertEqual([a['status'] for a in actions], ['succeeded'] * 3)
        # 真正发出去的参数必须是解析后的值，而不是花括号
        create = [args for name, args in remote.calls if name == 'nodes_create']
        self.assertEqual(create, [{'name': '厦门', 'expectedRevision': 7}])
        await store.close()

    async def test_template_twice_makes_no_remote_call(self):
        store = new_store()
        await seed_run(store, status='queued', message=GOAL)
        remote, run, plan, actions, requests = await self.run_worker(
            store, [templated(), templated()])
        self.assertEqual(run['status'], 'waiting_input')
        self.assertIn('unresolved_template', run['error'])
        self.assertEqual(remote.calls, [], '坏计划一步都不能发出去')
        self.assertEqual(actions, [])
        self.assertFalse(plan)
        await store.close()

    async def test_initial_planning_repairs_the_same_error(self):
        """首次规划走自己的两轮：同样的模板也要在提交前被改掉。"""
        store = new_store()
        await seed_run(store, status='queued', message=GOAL)
        first = ExecutionPlan.model_validate({
            'steps': _renumber(TEMPLATE_STEPS), 'result_locus': 'platform_change',
            'completion_criteria': CRITERIA, 'summary': '创建厦门节点'})
        second = ExecutionPlan.model_validate({
            'steps': _renumber(REF_STEPS), 'result_locus': 'platform_change',
            'completion_criteria': CRITERIA, 'summary': '创建厦门节点'})
        remote, run, plan, actions, requests = await self.run_worker(
            store, [first, second], supplement=False)
        self.assertEqual(run['status'], 'succeeded', run.get('error'))
        self.assertIn('unresolved_template',
                      json.dumps(requests[1]['校验错误'], ensure_ascii=False))
        self.assertEqual([a['status'] for a in actions], ['succeeded'] * 3)
        await store.close()


if __name__ == '__main__':
    unittest.main()
