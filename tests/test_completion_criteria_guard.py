"""完成条件必须能和**这份计划真能产出的东西**核对。

真实故障：在对方产品上创建图片节点的计划，声明了 `artifact_exists`（"图片节点"）。
步骤全是远端调用 + 一句作答，本地不会产出任何文件，核对阶段必然 unmet：

    本次未完成：还有没满足的完成条件（artifact_exists:没有声明产物）

节点其实已经在平台建好了（远端调用成功、回读也成功），用户却收到"未完成"。这类
条件不是安全网，是一句错话：丢掉并留痕，别拿它判死一份已经做完的计划。

反过来的边界也要守住：**不能因此放过没有证据的计划**——平台写操作仍然必须有
`tool_succeeded` 指向真正的写步骤，缺了就照旧退回修正。
"""
import json
import tempfile
import unittest

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.agent import nodes
from webpage_agent.capabilities.registry import Tool
from webpage_agent.core.schemas import RevisedPlan, ToolSelection

from tests.exec_env import (Remote, install_host_loop, install_main, install_tools, new_store,
                            reset_db, seed_run)


GOAL = '帮我生成一个图片的节点，名称叫做海南，并且帮我生成相关提示词'
SUPPLEMENT = '提示词最好是关于三亚的'

STEPS = [
    {'step_id': 'p2_s1', 'tool': 'canvas_get', 'args': {}, 'depends_on': []},
    {'step_id': 'p2_s2', 'tool': 'nodes_create', 'args': {'name': '海南'},
     'depends_on': ['p2_s1']},
    {'step_id': 'p2_s3', 'tool': 'canvas_get', 'args': {}, 'depends_on': ['p2_s2']},
    {'step_id': 'p2_s4', 'tool': 'answer_directly', 'args': {'text': '已创建'},
     'depends_on': ['p2_s3']},
]


def local_tools():
    """本用例的本地工具：一个纯作答、一个资料转 HTML（会落产物的能力包）。"""
    async def answer(args, state):
        return {'answer': args.get('text') or '好了'}

    async def to_html(args, state):
        return {'artifact': {'path': '/tmp/x.html', 'html_path': '/tmp/x.html'}}

    return [Tool('answer_directly', '作答', '直接回答', {'type': 'object'}, answer,
                 tool_key='local:answers:direct', effect='read'),
            Tool('sources_html', '资料转 HTML', '把资料排成 HTML',
                 {'type': 'object'}, to_html,
                 tool_key='local:export:sources_html', effect='write',
                 scope='public', local_output=True)]


def catalog(*, with_local=True):
    remote = [Tool('canvas_get', '读取画布', '读取当前画布', {'type': 'object'}, None,
                   tool_key='mcp:beeftv:canvas.get', effect='read', server_id='beeftv'),
              Tool('nodes_create', '创建节点', '创建一个图片节点', {'type': 'object'},
                   None, tool_key='mcp:beeftv:canvas.nodes.create',
                   effect='write', server_id='beeftv')]
    return remote + (local_tools() if with_local else [])


class LocalOutputTests(unittest.TestCase):
    """哪些工具算"本地会产出东西"——由能力包声明，不按名字猜。"""

    def test_answer_directly_is_not_a_producer(self):
        tool = next(t for t in local_tools() if t.name == 'answer_directly')
        self.assertFalse(nodes.produces_local_output(tool))

    def test_remote_tool_is_not_a_producer(self):
        tool = next(t for t in catalog() if t.name == 'nodes_create')
        self.assertFalse(nodes.produces_local_output(tool))

    def test_export_capability_is_a_producer(self):
        tool = next(t for t in local_tools() if t.name == 'sources_html')
        self.assertTrue(nodes.produces_local_output(tool))

    def test_registered_export_tool_declares_local_output(self):
        """真实能力包必须自己声明，否则这条链会以为本地没有产物来源。"""
        from webpage_agent.capabilities import registry
        spec = {tool.name: tool for tool in registry.load()}.get(
            'render_sources_html')
        if spec is None:      # 能力包没启用时跳过
            self.skipTest('未注册 render_sources_html')
        self.assertTrue(spec.local_output)


class DropCriteriaTests(unittest.TestCase):
    """掉条件与保留条件的边界。"""

    def test_artifact_criterion_is_dropped_for_platform_only_plans(self):
        kept, warnings = nodes.check_completion_criteria(
            [{'kind': 'tool_succeeded', 'target': 'nodes_create'},
             {'kind': 'artifact_exists', 'target': '图片节点'}],
            STEPS, catalog(), locus='platform_change')
        self.assertEqual([item['kind'] for item in kept], ['tool_succeeded'])
        self.assertEqual(warnings[0]['code'], 'criterion_not_applicable')
        self.assertEqual(warnings[0]['severity'], 'warning')
        self.assertIn('tool_succeeded', warnings[0]['detail'], '要给出正确写法')

    def test_file_written_is_dropped_too(self):
        kept, warnings = nodes.check_completion_criteria(
            [{'kind': 'file_written', 'target': 'a.md'}], STEPS, catalog(),
            locus='platform_state')
        self.assertEqual(kept, [])
        self.assertEqual(len(warnings), 1)

    def test_criterion_is_kept_when_a_local_producer_exists(self):
        steps = [STEPS[0], STEPS[1],
                 {'step_id': 'p2_s3', 'tool': 'sources_html', 'args': {},
                  'depends_on': ['p2_s2']}]
        kept, warnings = nodes.check_completion_criteria(
            [{'kind': 'artifact_exists', 'target': 'HTML 页面'}], steps,
            catalog(), locus='platform_change')
        self.assertEqual(len(kept), 1)
        self.assertEqual(warnings, [])

    def test_write_evidence_is_kept_and_missing_producers_block(self):
        kept, issues = nodes.check_completion_criteria(
            [{'kind': 'tool_succeeded', 'target': 'nodes_create'},
             {'kind': 'deployment_receipt', 'target': 'https://x.test'}],
            STEPS, catalog(), locus='platform_change')
        self.assertEqual([item['kind'] for item in kept], ['tool_succeeded'])
        self.assertEqual([item['code'] for item in issues],
                         ['criterion_needs_deploy_step'])

    def test_deployment_criteria_survive_when_a_deploy_step_exists(self):
        deploy = Tool('deploy_site', '部署', '把页面部署上去', {'type': 'object'},
                      None, tool_key='mcp:pub:deploy_site', effect='write',
                      server_id='pub', deployment_output=True)
        steps = STEPS + [{'step_id': 'p2_s9', 'tool': 'deploy_site', 'args': {},
                          'depends_on': ['p2_s4']}]
        kept, issues = nodes.check_completion_criteria(
            [{'kind': 'deployment_receipt', 'target': 'https://x.test'}],
            steps, catalog() + [deploy], locus='platform_change')
        self.assertEqual(len(kept), 1)
        self.assertEqual(issues, [])

    def test_evidence_target_must_exist_in_the_plan(self):
        for target, code in [('s99', 'criterion_target_unknown'),
                             ('', 'criterion_without_target')]:
            kept, issues = nodes.check_completion_criteria(
                [{'kind': 'tool_succeeded', 'target': target}],
                STEPS, catalog(), locus='platform_change')
            self.assertEqual(kept, [])
            self.assertEqual(issues[0]['code'], code)

    def test_artifact_criterion_blocks_instead_of_failing_later(self):
        """本地/作答计划声明本地产物：阻断交回模型，而不是执行后报未完成。"""
        kept, issues = nodes.check_completion_criteria(
            [{'kind': 'artifact_exists', 'target': 'HTML 页面'}],
            STEPS, catalog(), locus='chat')
        self.assertEqual(kept, [])
        self.assertEqual(issues[0]['code'], 'criterion_needs_local_step')
        self.assertNotEqual(issues[0].get('severity'), 'warning')

    def test_answer_criterion_needs_an_answer_step(self):
        kept, issues = nodes.check_completion_criteria(
            [{'kind': 'answer_present', 'target': ''}],
            [step for step in STEPS if step['tool'] != 'answer_directly'],
            catalog(), locus='platform_change')
        self.assertEqual(kept, [])
        self.assertEqual(issues[0]['code'], 'criterion_needs_answer_step')
        kept, issues = nodes.check_completion_criteria(
            [{'kind': 'answer_present', 'target': ''}], STEPS, catalog(),
            locus='platform_change')
        self.assertEqual(len(kept), 1)
        self.assertEqual(issues, [])


class CompletedCriteriaIdentityTests(unittest.TestCase):
    def test_revision_retains_completed_tool_criteria_by_all_identities(self):
        completed = [{'step_id': 's2', 'tool': 'mcp__beeftv__canvas.nodes.create',
                      'tool_key': 'mcp:beeftv:canvas.nodes.create'}]
        # The next segment only updates an existing node. Discovery need not
        # include the old create tool for its recorded success to remain valid.
        for target in ('s2', 'mcp__beeftv__canvas.nodes.create',
                       'mcp:beeftv:canvas.nodes.create'):
            with self.subTest(target=target):
                criteria = [{'kind': 'tool_succeeded', 'target': target}]
                kept, issues = nodes.check_completion_criteria(
                    criteria, [], [], 'platform_change', completed_steps=completed)
                self.assertEqual(kept, criteria)
                self.assertEqual(issues, [])

    def test_unexecuted_old_tool_still_blocks(self):
        kept, issues = nodes.check_completion_criteria(
            [{'kind': 'tool_succeeded', 'target': 'mcp__beeftv__canvas.nodes.create'}],
            [], catalog(), 'platform_change')
        self.assertEqual(kept, [])
        self.assertEqual(issues[0]['code'], 'criterion_target_unknown')


class RevisionCriteriaTests(unittest.IsolatedAsyncioTestCase):
    """修订环节：条件被丢掉之后，计划里的条件只剩能核对的那些。"""

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

    async def revise(self, answer):
        async def model(schema, system, user, **kwargs):
            return answer, {}

        with patch.object(nodes, '_store_and_run',
                          AsyncMock(return_value=(self.store(), 'r1'))), \
                patch.object(nodes, '_tool_context', AsyncMock(return_value=None)), \
                patch.object(nodes.tool_module, 'catalog',
                             AsyncMock(return_value=catalog())), \
                patch.object(nodes, 'structured_call', model):
            return await nodes.revise_plan(
                {'original_goal': GOAL, 'user_input': GOAL, 'usage': [],
                 'intent_version': 1})

    async def test_artifact_criterion_does_not_survive_the_revision(self):
        result = await self.revise(RevisedPlan.model_validate({
            'steps': STEPS, 'result_locus': 'platform_change',
            'completion_criteria': [{'kind': 'tool_succeeded',
                                     'target': 'nodes_create'},
                                    {'kind': 'artifact_exists',
                                     'target': '图片节点'}],
            'summary': '创建海南节点'}))
        self.assertEqual(result['status'], 'planned')
        self.assertEqual([item['kind'] for item in
                          result['plan']['completion_criteria']], ['tool_succeeded'])
        self.assertIn('criterion_not_applicable',
                      [item['code'] for item in result['revision_issues']])


class CriteriaGraphTests(unittest.IsolatedAsyncioTestCase):
    """整条图：截图那份计划现在能正常收尾，缺证据的那种仍然停住。"""

    async def asyncSetUp(self):
        await reset_db()

    def tools(self):
        remote = Remote()
        async def answer(args, state):
            return {'answer': args.get('text') or '好了'}
        return remote, {
            'canvas_get': remote.tool('canvas_get', effect='read',
                                      tool_key='mcp:beeftv:canvas.get',
                                      output={'revision': 7}),
            'nodes_create': remote.tool('nodes_create', effect='write',
                                        tool_key='mcp:beeftv:canvas.nodes.create',
                                        output={'node': {'id': 'n1'}}),
            'answer_directly': Tool('answer_directly', '作答', '直接回答',
                                    {'type': 'object'}, answer,
                                    tool_key='local:answers:direct', effect='read'),
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
            # 最后一版会一直被重复给出：模拟"模型改不动、坚持这么排"
            return (answers.pop(0) if len(answers) > 1 else answers[0]), {}

        if supplement:
            await store.append_run_input(run_id='run1', request_id='req-1',
                                         message=SUPPLEMENT)
        hub = Hub(store, owner_id='w1')
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), install_host_loop(), \
                patch.object(nodes, 'structured_call', model):
            await worker.run_once(hub, 'w1', run_id='run1')
        return (remote, await store.run('run1'),
                await store.run_snapshot('run1') or {}, requests)

    async def test_platform_plan_with_an_artifact_criterion_finishes(self):
        """截图那份计划：本地没有产物，平台上的节点建好了就该算完成。"""
        store = new_store()
        await seed_run(store, status='queued', message=GOAL)
        remote, run, snapshot, requests = await self.run_worker(
            store, [RevisedPlan.model_validate({
                'steps': STEPS, 'result_locus': 'platform_change',
                'completion_criteria': [{'kind': 'tool_succeeded',
                                         'target': 'nodes_create'},
                                        {'kind': 'artifact_exists',
                                         'target': '图片节点'}],
                'summary': '读取画布基线后创建海南节点并回执'})])
        self.assertEqual(run['status'], 'succeeded', run.get('error'))
        self.assertEqual(len(requests), 1, '不需要退回修正')
        self.assertEqual([name for name, _ in remote.calls],
                         ['canvas_get', 'nodes_create', 'canvas_get'])
        criteria = (snapshot.get('state') or {}).get('plan', {}).get(
            'completion_criteria') or []
        self.assertEqual([item['kind'] for item in criteria], ['tool_succeeded'])
        state = (snapshot.get('state') or {}).get('criteria_state') or {}
        self.assertTrue(state.get('ok'), state)
        self.assertEqual(state.get('unmet'), [])
        await store.close()

    async def test_platform_plan_without_write_evidence_still_stops(self):
        """丢掉错条件之后，平台写操作仍必须有 tool_succeeded 证据。"""
        store = new_store()
        await seed_run(store, status='queued', message=GOAL)
        remote, run, snapshot, requests = await self.run_worker(
            store, [RevisedPlan.model_validate({
                'steps': STEPS, 'result_locus': 'platform_change',
                'completion_criteria': [{'kind': 'artifact_exists',
                                         'target': '图片节点'}],
                'summary': '创建海南节点'})])
        self.assertEqual(run['status'], 'waiting_input')
        self.assertIn('platform_change_needs_evidence', run['error'])
        self.assertEqual(len(requests), 2, '退回修正一次后仍不合法')
        self.assertEqual(remote.calls, [], '一个远端调用都不发')
        await store.close()


if __name__ == '__main__':
    unittest.main()
