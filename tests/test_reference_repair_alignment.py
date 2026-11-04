"""引用修复的护栏不能把**合法修复**当成"想重做创建"。

真实故障：画布上的节点（桂林）已经建好、提示词也写进去了，run 却停在

    先停在这里：引用修复未通过校验，已完成的创建不会重做，请核对后继续。

触发点是一个引用解析失败（`E_INPUT_REFERENCE`）之后的"引用修复"：模型被要求
只更正未执行步骤的 `ref.pointer`，可它返回的步骤编号常常是省略的（`PlanStep.step_id`
本来就允许省略）、或者从 `s1` 重新数、或者只写"失败的那一步"。护栏按**字面
step_id 完全相等**比对，把这些修复全判成"不安全"，于是平台上的对象明明建好了，
用户却只能看到一句"请核对后继续"。

修法：先按 id/位置把候选步骤**对齐**到原计划（补编号、补依赖、没动的步骤原样
保留），再比工具、依赖和参数——不变式还是那条：只能改失败步骤的 pointer，
不能重做创建、不能换工具、不能改字面参数、不能略掉计划里的步骤。
"""
import copy
import tempfile
import unittest

from unittest.mock import patch

from webpage_agent.agent import nodes
from webpage_agent.core.schemas import ExecutionPlan, ObservationDecision

from tests.exec_env import (Remote, install_main, install_tools, new_store,
                            override_settings, reset_db, seed_run)


# 两个引用：expectedRevision 指错（真会失败），nodeName 指对（不许被动）
PLAN_STEPS = [
    {'step_id': 'p2_s1', 'tool': 'canvas_get', 'args': {}},
    {'step_id': 'p2_s2', 'tool': 'nodes_create',
     'args': {'name': '桂林',
              'expectedRevision': {'ref': {'step_id': 'p2_s1',
                                           'pointer': '/not_there'}},
              'nodeName': {'ref': {'step_id': 'p2_s1',
                                   'pointer': '/data/name'}}},
     'depends_on': ['p2_s1']},
    {'step_id': 'p2_s3', 'tool': 'prompt_update',
     'args': {'prompt': '桂林山水'}, 'depends_on': ['p2_s2']},
]

FIXED_WRITE = {'step_id': 'p2_s2', 'tool': 'nodes_create',
               'args': {'name': '桂林',
                        'expectedRevision': {'ref': {'step_id': 'p2_s1',
                                                     'pointer': '/data/revision'}},
                        'nodeName': {'ref': {'step_id': 'p2_s1',
                                             'pointer': '/data/name'}}},
               'depends_on': ['p2_s1']}
TAIL = {'step_id': 'p2_s3', 'tool': 'prompt_update',
        'args': {'prompt': '桂林山水'}, 'depends_on': ['p2_s2']}


def state_with_failure():
    """s1 成功、s2 的引用解析失败——正是触发引用修复的现场。"""
    return {'plan': {'steps': copy.deepcopy(PLAN_STEPS)},
            'step_outputs': {'p2_s1': {'__status__': 'succeeded',
                                       'data': {'revision': 7, 'name': '桂林'}}},
            'errors': [{'error_code': 'E_INPUT_REFERENCE', 'step_id': 'p2_s2',
                        'detail': '引用不到值'}],
            'artifact_dir': ''}


class AlignTests(unittest.TestCase):
    """对齐：补编号/补依赖/补回没动的步骤，多出来的判不合格。"""

    def align(self, candidate):
        return nodes._align_repair_steps(candidate, PLAN_STEPS[1:])

    def test_step_ids_are_optional(self):
        aligned = self.align([{k: v for k, v in FIXED_WRITE.items() if k != 'step_id'},
                              {k: v for k, v in TAIL.items() if k != 'step_id'}])
        self.assertEqual([step['step_id'] for step in aligned], ['p2_s2', 'p2_s3'])

    def test_step_ids_may_be_renumbered(self):
        aligned = self.align([dict(FIXED_WRITE, step_id='s1'),
                              dict(TAIL, step_id='s2')])
        self.assertEqual([step['step_id'] for step in aligned], ['p2_s2', 'p2_s3'])

    def test_steps_may_come_back_in_another_order(self):
        aligned = self.align([dict(TAIL), dict(FIXED_WRITE)])
        self.assertEqual([step['step_id'] for step in aligned], ['p2_s2', 'p2_s3'])

    def test_untouched_steps_are_filled_back_in(self):
        aligned = self.align([FIXED_WRITE])
        self.assertEqual([step['step_id'] for step in aligned], ['p2_s2', 'p2_s3'])
        self.assertEqual(aligned[1], PLAN_STEPS[2], '没动过的步骤要原样保留')

    def test_missing_depends_on_is_restored(self):
        aligned = self.align([{k: v for k, v in FIXED_WRITE.items()
                               if k != 'depends_on'}, TAIL])
        self.assertEqual(aligned[0]['depends_on'], ['p2_s1'])

    def test_extra_step_is_rejected(self):
        self.assertIsNone(self.align([dict(PLAN_STEPS[1], step_id='new'),
                                      FIXED_WRITE, TAIL]))

    def test_empty_candidate_is_rejected(self):
        self.assertIsNone(self.align([]))


class GuardTests(unittest.TestCase):
    """对齐之后的安全不变式：该拒的还得拒。"""

    def issues(self, candidate):
        return nodes._prepared_reference_steps(state_with_failure(), candidate)[1]

    def test_legitimate_repairs_pass(self):
        for candidate in ([FIXED_WRITE, TAIL],
                          [{k: v for k, v in FIXED_WRITE.items() if k != 'step_id'},
                           {k: v for k, v in TAIL.items() if k != 'step_id'}],
                          [FIXED_WRITE]):
            self.assertEqual(self.issues(candidate), [], candidate)

    def test_recreating_the_node_is_rejected(self):
        self.assertTrue(self.issues([dict(PLAN_STEPS[1], step_id='new'),
                                     FIXED_WRITE, TAIL]))

    def test_swapping_the_tool_is_rejected(self):
        self.assertTrue(self.issues([dict(FIXED_WRITE, tool='nodes_create_v2'), TAIL]))

    def test_changing_a_literal_argument_is_rejected(self):
        self.assertTrue(self.issues([
            dict(FIXED_WRITE, args={**FIXED_WRITE['args'], 'name': '柳州'}), TAIL]))

    def test_rewriting_a_healthy_reference_is_rejected(self):
        bad = dict(FIXED_WRITE, args={
            **FIXED_WRITE['args'],
            'nodeName': {'ref': {'step_id': 'p2_s1', 'pointer': '/data/revision'}}})
        self.assertTrue(self.issues([bad, TAIL]))

    def test_pointer_must_resolve_after_repair(self):
        bad = dict(FIXED_WRITE, args={
            **FIXED_WRITE['args'],
            'expectedRevision': {'ref': {'step_id': 'p2_s1', 'pointer': '/missing'}}})
        codes = [item['code'] for item in self.issues([bad, TAIL])]
        self.assertEqual(codes, ['bad_reference_repair'])

    def test_changing_dependencies_is_rejected(self):
        self.assertTrue(self.issues([dict(FIXED_WRITE, depends_on=['p2_s3']), TAIL]))

    def test_omitted_dependencies_fall_back_to_the_original(self):
        # 省略（空列表）与"没写"等价：沿用原依赖，绝不放宽顺序门禁
        self.assertEqual(self.issues([{k: v for k, v in FIXED_WRITE.items()
                                       if k != 'depends_on'}, TAIL]), [])


class ReferenceRepairGraphTests(unittest.IsolatedAsyncioTestCase):
    """整条图：引用失败 → 模型给一份"编号省略"的修复 → 平台调用照常完成。"""

    async def asyncSetUp(self):
        await reset_db()

    def tools(self):
        remote = Remote()
        return remote, {
            'canvas_get': remote.tool(
                'canvas_get', effect='read', tool_key='mcp:beeftv:canvas.get',
                output={'data': {'revision': 7, 'name': '桂林'}}),
            'nodes_create': remote.tool(
                'nodes_create', effect='write',
                tool_key='mcp:beeftv:canvas.nodes.create',
                output={'node': {'id': 'n1'}}),
            'prompt_update': remote.tool(
                'prompt_update', effect='write',
                tool_key='mcp:beeftv:canvas.node.prompt.update',
                output={'ok': True}),
        }

    def plan(self):
        return {'result_locus': 'platform_change',
                'completion_criteria': [
                    {'kind': 'tool_succeeded', 'target': 'p2_s2'},
                    {'kind': 'tool_succeeded', 'target': 'p2_s3'}],
                'steps': copy.deepcopy(PLAN_STEPS),
                'summary': '读取画布后创建桂林节点并写提示词'}

    async def run_case(self, steps):
        import scripts.run_worker as worker
        from webpage_agent.api.api_hub import Hub

        remote, tools = self.tools()
        decisions = [{'kind': 'revise', 'steps': steps,
                      'reason': '按真实回执修正引用'}]

        async def model(schema, system, user, **kwargs):
            if schema is ExecutionPlan:
                return ExecutionPlan.model_validate(self.plan()), {}
            if schema is ObservationDecision:
                return ObservationDecision.model_validate(decisions[0]), {}
            raise AssertionError(schema)

        store = new_store()
        await seed_run(store, status='queued', message='创建桂林节点')
        hub = Hub(store, owner_id='w1')
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), \
                patch.object(nodes, 'structured_call', model), \
                override_settings(observe_enabled=True):
            await worker.run_once(hub, 'w1', run_id='run1')
        run = await store.run('run1')
        result = (remote, run, await store.actions_of('run1'))
        await store.close()
        return result

    async def test_repair_with_omitted_step_ids_still_finishes(self):
        remote, run, actions = await self.run_case([
            {k: v for k, v in FIXED_WRITE.items() if k != 'step_id'},
            {k: v for k, v in TAIL.items() if k != 'step_id'}])
        self.assertEqual(run['status'], 'succeeded', run.get('error'))
        self.assertEqual([name for name, _ in remote.calls],
                         ['canvas_get', 'nodes_create', 'prompt_update'])
        self.assertEqual([row['step_id'] for row in actions],
                         ['p2_s1', 'p2_s2', 'p2_s3'])
        self.assertEqual(remote.calls[1][1]['expectedRevision'], 7)
        self.assertEqual(remote.calls[1][1]['nodeName'], '桂林')

    async def test_repair_that_only_fixes_the_broken_step_keeps_the_rest(self):
        remote, run, actions = await self.run_case([
            {k: v for k, v in FIXED_WRITE.items() if k != 'step_id'}])
        self.assertEqual(run['status'], 'succeeded', run.get('error'))
        self.assertEqual([name for name, _ in remote.calls],
                         ['canvas_get', 'nodes_create', 'prompt_update'],
                         '没动的步骤要原样补回，不能被丢掉')

    async def test_repair_that_recreates_the_node_is_still_refused(self):
        remote, run, actions = await self.run_case([
            dict(PLAN_STEPS[1], step_id='new'), FIXED_WRITE, TAIL])
        self.assertEqual(run['status'], 'waiting_input')
        self.assertIn('引用修复未通过校验', run['error'])
        self.assertEqual([name for name, _ in remote.calls], ['canvas_get'],
                         '拒绝之后不能有新的远端写调用')


if __name__ == '__main__':
    unittest.main()
