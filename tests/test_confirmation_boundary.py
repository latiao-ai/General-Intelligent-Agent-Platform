"""确认边界：提议与提交不能塞进同一片段。

真踩过的形状（2026-10-30，画布「吉林」）：计划把 canvas.generation.propose 与
canvas.generation.submit 放进同一片段，模型把"用户确认后提交"写进 why 就当作确认
已经发生。服务端按合同拒绝（production_authorization_required），中台又把这次拒绝
判成"结果未定"，任务落到 needs_reconcile——用户看到的是"执行失败、需要核实"。

规则由服务端配置声明（confirmation_tools / proposal_tools），内核不认识业务名字；
这里测的是"提议后必须换片段、提交片段里不许再有写操作"这一条确定性判决。
"""
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from webpage_agent.agent import nodes
from webpage_agent.capabilities.registry import Tool


async def _noop(args, state):
    return {}


def make(name, key, effect='read', server='studio'):
    return Tool(name, name, name, {'type': 'object'}, _noop, tool_key=key,
                effect=effect, server_id=server)


SERVER = {'id': 'studio', 'transport': 'stdio', 'stdio_command': '/bin/true',
          'confirmation_tools': ['canvas.generation.submit'],
          'proposal_tools': ['canvas.generation.propose']}


def catalog(server='studio'):
    return [
        make(f'mcp__{server}__canvas.get', f'mcp:{server}:canvas.get', server=server),
        make(f'mcp__{server}__canvas.node.prompt.update',
             f'mcp:{server}:canvas.node.prompt.update', 'write', server=server),
        make(f'mcp__{server}__canvas.generation.propose',
             f'mcp:{server}:canvas.generation.propose', server=server),
        make(f'mcp__{server}__canvas.generation.submit',
             f'mcp:{server}:canvas.generation.submit', 'write', server=server),
    ]


class ConfirmationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {'MCP_SERVERS': json.dumps([SERVER])})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    @staticmethod
    def steps(*names):
        return [{'tool': name, 'step_id': f's{index + 1}'} for index, name in enumerate(names)]

    def test_propose_and_submit_in_one_slice_is_blocked(self):
        steps = self.steps('mcp__studio__canvas.get',
                           'mcp__studio__canvas.generation.propose',
                           'mcp__studio__canvas.generation.submit')
        issues = nodes.check_confirmation_boundary(steps, catalog())
        self.assertEqual([i['code'] for i in issues], ['confirmation_bypasses_user'])
        self.assertEqual(issues[0]['step_id'], 's3')
        self.assertIn('提议与提交必须分属不同片段', issues[0]['detail'])

    def test_write_before_submit_in_one_slice_is_blocked(self):
        """没写提议、但片段里还有写操作：同样是自导自演（先造状态再提交）。"""
        steps = self.steps('mcp__studio__canvas.get',
                           'mcp__studio__canvas.node.prompt.update',
                           'mcp__studio__canvas.generation.submit')
        issues = nodes.check_confirmation_boundary(steps, catalog())
        self.assertEqual([i['code'] for i in issues], ['confirmation_bypasses_user'])
        self.assertIn('必须单独成段执行', issues[0]['detail'])

    def test_confirmed_segment_with_readback_is_allowed(self):
        """确认之后的片段：只读核对 + 提交，是合法形状，不许误杀。"""
        steps = self.steps('mcp__studio__canvas.get',
                           'mcp__studio__canvas.generation.submit')
        self.assertEqual(nodes.check_confirmation_boundary(steps, catalog()), [])

    def test_preparation_slice_that_stops_at_proposal_is_allowed(self):
        steps = self.steps('mcp__studio__canvas.get',
                           'mcp__studio__canvas.node.prompt.update',
                           'mcp__studio__canvas.generation.propose')
        self.assertEqual(nodes.check_confirmation_boundary(steps, catalog()), [])

    def test_proposal_slice_must_end_at_the_confirmation_boundary(self):
        """真踩过（2026-11-01 实测）：片段以提议结尾却标 verify，审查器判"没覆盖"，
        规划器就去补 submit——等于被逼着走越权那条路。所以提议片段必须是 observe。"""
        steps = self.steps('mcp__studio__canvas.get',
                           'mcp__studio__canvas.generation.propose')
        issues = nodes.check_confirmation_boundary(
            steps, catalog(), after_segment='verify')
        self.assertEqual([i['code'] for i in issues], ['proposal_needs_user_confirmation'])
        self.assertIn('after_segment 设为 observe', issues[0]['detail'])

    def test_proposal_slice_with_observe_and_goal_is_allowed(self):
        steps = self.steps('mcp__studio__canvas.get',
                           'mcp__studio__canvas.generation.propose')
        self.assertEqual(nodes.check_confirmation_boundary(
            steps, catalog(), after_segment='observe',
            observation_goal='核对提议已登记，等待用户在平台确认生成'), [])

    def test_proposal_slice_without_observation_goal_is_blocked(self):
        steps = self.steps('mcp__studio__canvas.generation.propose')
        issues = nodes.check_confirmation_boundary(
            steps, catalog(), after_segment='observe', observation_goal='  ')
        self.assertEqual([i['code'] for i in issues], ['proposal_needs_user_confirmation'])

    def test_steps_after_the_proposal_belong_to_the_next_slice(self):
        steps = self.steps('mcp__studio__canvas.generation.propose',
                           'mcp__studio__canvas.node.prompt.update')
        issues = nodes.check_confirmation_boundary(
            steps, catalog(), after_segment='observe',
            observation_goal='等待用户在平台确认生成')
        self.assertEqual([i['code'] for i in issues], ['proposal_needs_user_confirmation'])
        self.assertIn('留到确认后的下一次片段', issues[0]['detail'])

    def test_proposal_rule_stays_quiet_without_segment_metadata(self):
        steps = self.steps('mcp__studio__canvas.generation.propose')
        self.assertEqual(nodes.check_confirmation_boundary(steps, catalog()), [])

    def test_other_servers_writes_do_not_trip_the_rule(self):
        steps = self.steps('mcp__other__thing.write',
                           'mcp__studio__canvas.get',
                           'mcp__studio__canvas.generation.submit')
        other = [make('mcp__other__thing.write', 'mcp:other:thing.write', 'write', server='other')]
        self.assertEqual(nodes.check_confirmation_boundary(steps, catalog() + other), [])

    def test_unconfigured_server_stays_inert(self):
        with patch.dict(os.environ, {'MCP_SERVERS': '[]'}):
            steps = self.steps('mcp__studio__canvas.generation.propose',
                               'mcp__studio__canvas.generation.submit')
            self.assertEqual(nodes.check_confirmation_boundary(steps, catalog()), [])

    def test_validate_plan_reports_the_bypass(self):
        """接进规划校验：坏片段要在计划阶段就被挡住，不进入执行。"""
        raw = [
            {'tool': 'mcp__studio__canvas.get', 'step_id': 's1', 'args': {}},
            {'tool': 'mcp__studio__canvas.generation.propose', 'step_id': 's2',
             'args': {}, 'depends_on': ['s1']},
            {'tool': 'mcp__studio__canvas.generation.submit', 'step_id': 's3',
             'args': {}, 'depends_on': ['s2']},
        ]
        steps, issues = nodes.validate_plan(raw, catalog())
        self.assertEqual(len(steps), 3)
        self.assertIn('confirmation_bypasses_user', [issue['code'] for issue in issues])
        blocking = [issue for issue in issues if issue.get('severity') != 'warning']
        self.assertEqual([issue['code'] for issue in blocking], ['confirmation_bypasses_user'])

    async def test_planner_sends_the_bypass_back_instead_of_executing_it(self):
        """规划节点：第一次给出"提议+提交同片段"，被挡回；修正成 observe 片段后才放行。"""
        from webpage_agent.tools import core as tools
        from webpage_agent.core.schemas import ExecutionPlan, RequestReview

        rounds = {'plan': 0}
        repair_payloads = []

        async def model(schema, system, user, **kwargs):
            if schema is ExecutionPlan:
                rounds['plan'] += 1
                if rounds['plan'] > 1:
                    repair_payloads.append(json.loads(user))
                if rounds['plan'] == 1:
                    return ExecutionPlan(
                        steps=[{'tool': 'mcp__studio__canvas.get', 'step_id': 's1'},
                               {'tool': 'mcp__studio__canvas.generation.propose', 'step_id': 's2',
                                'depends_on': ['s1']},
                               {'tool': 'mcp__studio__canvas.generation.submit', 'step_id': 's3',
                                'depends_on': ['s2']}],
                        result_locus='platform_change', after_segment='verify'), {}
                return ExecutionPlan(
                    steps=[{'tool': 'mcp__studio__canvas.get', 'step_id': 's1'},
                           {'tool': 'mcp__studio__canvas.generation.propose', 'step_id': 's2',
                            'depends_on': ['s1']}],
                    result_locus='platform_change', after_segment='observe',
                    observation_goal='核对提议已登记，等待用户在平台确认生成后再提交'), {}
            repair_payloads.append(json.loads(user))
            return RequestReview(complete=True), {}

        with patch.object(tools, 'catalog', AsyncMock(return_value=catalog())), \
                patch.object(nodes, 'structured_call', model):
            result = await nodes.plan({'user_input': '生成视频', 'usage': [],
                                       'scene': {'verified': True, 'context': {'id': 'c1'}}})

        self.assertEqual(result['status'], 'planned')
        self.assertEqual([step['tool'] for step in result['plan']['steps']],
                         ['mcp__studio__canvas.get', 'mcp__studio__canvas.generation.propose'])
        self.assertEqual(result['plan']['after_segment'], 'observe')
        repair = [payload for payload in repair_payloads if '校验错误' in payload]
        self.assertTrue(repair, '第一次的越权计划必须被挡回并附上原因')
        errors = json.dumps(repair[0]['校验错误'], ensure_ascii=False)
        self.assertIn('confirmation_bypasses_user', errors)
        self.assertIn('after_segment=observe', errors)

    async def test_planner_is_pushed_from_verify_to_observe_not_towards_submit(self):
        """实测那条路的回归：提议片段标 verify 被挡回，模型改成 observe 才放行。

        以前它被审查器的"必须覆盖全部要求"逼着去补 submit，于是撞上越权；
        现在确定性规则在它补 submit 之前就要求改成 observe。
        """
        from webpage_agent.tools import core as tools
        from webpage_agent.core.schemas import ExecutionPlan, RequestReview

        rounds = {'plan': 0}
        repair_payloads = []

        async def model(schema, system, user, **kwargs):
            if schema is ExecutionPlan:
                rounds['plan'] += 1
                if rounds['plan'] > 1:
                    repair_payloads.append(json.loads(user))
                steps = [{'tool': 'mcp__studio__canvas.get', 'step_id': 's1'},
                         {'tool': 'mcp__studio__canvas.generation.propose', 'step_id': 's2',
                          'depends_on': ['s1']}]
                if rounds['plan'] == 1:
                    return ExecutionPlan(steps=steps, result_locus='platform_change',
                                         after_segment='verify'), {}
                return ExecutionPlan(steps=steps, result_locus='platform_change',
                                     after_segment='observe',
                                     observation_goal='等待用户在平台确认生成本次提议'), {}
            return RequestReview(complete=True), {}

        with patch.object(tools, 'catalog', AsyncMock(return_value=catalog())), \
                patch.object(nodes, 'structured_call', model):
            result = await nodes.plan({'user_input': '生成一张图片', 'usage': [],
                                       'scene': {'verified': True, 'context': {'id': 'c1'}}})

        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['plan']['after_segment'], 'observe')
        self.assertTrue(repair_payloads, 'verify 结尾的提议片段必须被挡回')
        errors = json.dumps(repair_payloads[0]['校验错误'], ensure_ascii=False)
        self.assertIn('proposal_needs_user_confirmation', errors)


if __name__ == '__main__':
    unittest.main()
