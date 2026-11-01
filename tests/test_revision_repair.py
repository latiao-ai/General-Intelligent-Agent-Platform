"""修订计划的两处修补（回归用例）。

真实故障：用户先要「创建一个图片节点，名称叫西湖，并生成相关提示词」，紧接着补充
「最好是下雨天的西湖」。补充在候选计划提交前到达，候选计划被 CAS 作废；进入修订
时库里从来没有过计划，模型对「结果要落在哪」只能盲猜，把它标成 platform_state，
却仍然排了创建与写入步骤，校验器拦住，run 停在 waiting_input——用户只看到一句
「调整后的计划没有通过校验」。

两个缺口，用例分三组钉住：

1. 作废的候选计划连 `result_locus` 一起丢了（`persist_plan` → `discarded_plan`）；
2. 修订一次不合法就停下来等人，不像首次规划那样把校验错误交回模型修一轮；
3. 整条图跑一遍：修好之后步骤真的执行了，修不好就一步都不执行。
"""
import json
import tempfile
import unittest

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.governance import execution_control
from webpage_agent.agent import graph
from webpage_agent.store import ledger as ledger_module
from webpage_agent.agent import nodes
from webpage_agent.agent import run_inputs as run_inputs_module
from webpage_agent.capabilities.registry import Tool
from webpage_agent.core.schemas import RevisedPlan, ToolSelection

from tests.exec_env import (Remote, install_main, install_tools, new_store,
                            reset_db, seed_run)


GOAL = '帮我创建一个图片节点，名称叫做西湖，并且帮我生成相关的提示词'
SUPPLEMENT = '最好是下雨天的西湖'

WRITE_STEPS = [
    {'step_id': 'p2_s1', 'tool': 'canvas_get', 'args': {}, 'depends_on': []},
    {'step_id': 'p2_s2', 'tool': 'nodes_create', 'args': {'name': '西湖'},
     'depends_on': ['p2_s1']},
    {'step_id': 'p2_s3', 'tool': 'prompt_update', 'args': {'prompt': '下雨天的西湖'},
     'depends_on': ['p2_s2']},
]
WRITE_CRITERIA = [{'kind': 'tool_succeeded', 'target': 'nodes_create'},
                  {'kind': 'tool_succeeded', 'target': 'prompt_update'}]


def catalog():
    """一个平台服务端：读一个、写两个。"""
    return [Tool('canvas_get', '读取画布', '读取当前画布', {'type': 'object'}, None,
                 tool_key='mcp:beeftv:canvas.get', effect='read', server_id='beeftv'),
            Tool('nodes_create', '创建节点', '创建一个图片节点', {'type': 'object'}, None,
                 tool_key='mcp:beeftv:canvas.nodes.create', effect='write',
                 server_id='beeftv'),
            Tool('prompt_update', '写提示词', '写入节点提示词', {'type': 'object'}, None,
                 tool_key='mcp:beeftv:canvas.node.prompt.update', effect='write',
                 server_id='beeftv')]


def contradictory():
    """截图里那份矛盾计划：落点标成只读，步骤却在写。"""
    return RevisedPlan.model_validate({
        'steps': WRITE_STEPS, 'result_locus': 'platform_state',
        'summary': '读取画布后创建西湖节点并写入下雨天提示词'})


def corrected():
    return RevisedPlan.model_validate({
        'steps': WRITE_STEPS, 'result_locus': 'platform_change',
        'completion_criteria': WRITE_CRITERIA,
        'summary': '读取画布后创建西湖节点并写入下雨天提示词'})


class RevisionRepairTests(unittest.IsolatedAsyncioTestCase):
    """修订环节本身：退回修正、作废锚点。"""

    def store(self, *, snapshot=None):
        return SimpleNamespace(
            run_intent=AsyncMock(return_value={
                'intent_version': 2, 'processed_intent_version': 1,
                'plan_version': 0}),
            pending_run_inputs=AsyncMock(return_value=[
                {'input_seq': 1, 'message': SUPPLEMENT, 'intent_version': 2}]),
            actions_of=AsyncMock(return_value=[]),
            plan=AsyncMock(return_value={}),
            run_snapshot=AsyncMock(return_value=snapshot or {}))

    async def revise(self, answers, *, state=None, snapshot=None):
        """按顺序喂给模型多版答案，返回 (结果, 每次请求的 payload)。"""
        answers = list(answers)
        requests = []

        async def model(schema, system, user, **kwargs):
            requests.append(json.loads(user))
            if not answers:
                raise AssertionError('模型被多问了一次')
            return answers.pop(0), {}

        with patch.object(nodes, '_store_and_run',
                          AsyncMock(return_value=(self.store(snapshot=snapshot), 'r1'))), \
                patch.object(nodes, '_tool_context', AsyncMock(return_value=None)), \
                patch.object(nodes.tool_module, 'catalog',
                             AsyncMock(return_value=catalog())), \
                patch.object(nodes, 'structured_call', model):
            result = await nodes.revise_plan(
                {'original_goal': GOAL, 'user_input': GOAL, 'usage': [],
                 'intent_version': 1, **(state or {})})
        return result, requests

    async def test_invalid_revision_is_repaired_in_the_same_round(self):
        """第一次标错落点：把校验错误交回模型修一次，然后按新计划继续。"""
        result, requests = await self.revise([contradictory(), corrected()])
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(len(requests), 2, '退回修正只多一次模型调用')
        self.assertEqual(result['plan']['result_locus'], 'platform_change')
        self.assertEqual([step['step_id'] for step in result['plan']['steps']],
                         ['p2_s1', 'p2_s2', 'p2_s3'])
        # 修复请求必须带上错误本身和上一版调整，否则模型只能重新猜
        repair = requests[1]
        self.assertIn('platform_state_cannot_write',
                      json.dumps(repair['校验错误'], ensure_ascii=False))
        self.assertEqual(repair['上次的调整']['result_locus'], 'platform_state')
        self.assertEqual(repair['原目标'], GOAL, '原目标不能在修复请求里丢')
        self.assertEqual(graph._next_after_revise(result), 'persist_plan')

    async def test_two_invalid_revisions_stop_with_the_same_message(self):
        """修不好才停：只多问一次，不循环修，也不带着坏计划往下走。"""
        result, requests = await self.revise([contradictory(), contradictory()])
        self.assertEqual(len(requests), 2)
        self.assertEqual(result['status'], 'waiting_input')
        self.assertIn('platform_state_cannot_write', result['failure'])
        self.assertNotIn('plan', result, '没通过校验的计划不能往下走')
        self.assertFalse(result['intent_changed'])

    async def test_valid_revision_still_takes_one_call(self):
        """正常路径不变：第一版就合法时只问一次。"""
        result, requests = await self.revise([corrected()])
        self.assertEqual(len(requests), 1)
        self.assertEqual(result['status'], 'planned')

    async def test_discarded_candidate_locus_reaches_the_revision(self):
        """作废的候选计划带着落点进修订输入，模型不用再从零猜。"""
        anchor = {'result_locus': 'platform_change',
                  'summary': '读取画布后创建西湖节点并写入提示词'}
        _, requests = await self.revise(
            [corrected()], state={'discarded_plan': anchor})
        self.assertEqual(requests[0]['被作废的候选计划'], anchor)
        # 「当前计划」确实是空的：没有已提交计划可沿用
        self.assertEqual(requests[0]['当前计划']['plan_version'], 0)
        self.assertEqual(requests[0]['当前计划']['steps'], [])

    async def test_anchor_survives_in_the_snapshot(self):
        """执行结束再续跑时，锚点从快照里恢复，不依赖内存里的 state。"""
        anchor = {'result_locus': 'platform_change', 'summary': '创建节点'}
        _, requests = await self.revise(
            [corrected()], snapshot={'state': {'discarded_plan': anchor}})
        self.assertEqual(requests[0]['被作废的候选计划'], anchor)

    async def test_revision_without_step_ids_is_accepted(self):
        """模型只写"做什么"、不写编号：补号照跑，不能判成"没有可执行的步骤"。"""
        plan = RevisedPlan.model_validate({
            'steps': [{'tool': 'canvas_get', 'args': {}},
                      {'tool': 'nodes_create', 'args': {'name': '西湖'}},
                      {'tool': 'prompt_update', 'args': {'prompt': '下雨天'}}],
            'result_locus': 'platform_change',
            'completion_criteria': WRITE_CRITERIA,
            'summary': '创建西湖节点并写入提示词'})
        result, requests = await self.revise([plan])
        self.assertEqual(result['status'], 'planned', result.get('failure'))
        self.assertEqual(len(requests), 1, '这不该触发退回修正')
        self.assertEqual([step['step_id'] for step in result['plan']['steps']],
                         ['p1_s1', 'p1_s2', 'p1_s3'])

    async def test_no_anchor_means_no_extra_content(self):
        """没有作废过候选计划时，payload 里不多出内容。"""
        _, requests = await self.revise([corrected()])
        self.assertEqual(requests[0]['被作废的候选计划'], {})


class AnchorTests(unittest.IsolatedAsyncioTestCase):
    """作废时的锚点：只留落点，不能把没跑过的步骤当成计划。"""

    async def test_persist_plan_keeps_the_anchor_when_the_commit_loses_the_race(self):
        store = SimpleNamespace(commit_plan=AsyncMock(
            side_effect=execution_control.IntentChanged(
                run_id='r1', expected=1, actual=2)))
        book = ledger_module.ActionLedger(store, 'r1', 1)
        candidate = {'steps': WRITE_STEPS, 'version': 1, 'intent_version': 1,
                     'result_locus': 'platform_change', 'summary': '创建西湖节点'}
        with ledger_module.use_ledger(book):
            result = await nodes.persist_plan({
                'plan': candidate, 'original_goal': GOAL, 'intent_version': 1,
                'artifact_dir': ''})
        self.assertTrue(result['intent_changed'])
        self.assertEqual(result['plan'], {}, '没提交的计划不能被当成当前计划')
        self.assertEqual(result['intent_version'], 2, '新版本必须写回状态')
        self.assertEqual(result['discarded_plan'],
                         {'result_locus': 'platform_change', 'summary': '创建西湖节点'})

    async def test_anchor_never_carries_steps_or_criteria(self):
        anchor = nodes._plan_anchor({
            'result_locus': 'platform_change', 'summary': '创建节点',
            'steps': WRITE_STEPS, 'completion_criteria': WRITE_CRITERIA})
        self.assertEqual(set(anchor), {'result_locus', 'summary'})

    async def test_nothing_to_anchor_is_an_empty_dict(self):
        self.assertEqual(nodes._plan_anchor({}), {})
        self.assertEqual(nodes._plan_anchor({'steps': WRITE_STEPS}), {})

    async def test_snapshot_keeps_the_anchor(self):
        snapshot = nodes._snapshot_state({
            'discarded_plan': {'result_locus': 'platform_change', 'summary': '创建节点'},
            'documents': []})
        self.assertEqual(snapshot['discarded_plan']['result_locus'], 'platform_change')


class RevisionRulesTests(unittest.TestCase):
    def test_rules_point_at_the_discarded_candidate(self):
        rules = run_inputs_module.revision_rules()
        self.assertIn('被作废的候选计划', rules)
        self.assertIn('platform_change', rules)
        # 原有约定不能丢
        self.assertIn('增量修改', rules)
        self.assertIn('不要再问第二遍', rules)


class RevisionRepairGraphTests(unittest.IsolatedAsyncioTestCase):
    """整条图跑一遍：补充在规划期间到达、候选计划作废、修订修好、步骤真的执行。"""

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

    async def run_worker(self, store, answers, *, first_plan=None):
        """跑一次 worker；answers 是修订环节依次给出的候选计划。

        `first_plan` 非空时，用它替换规划节点：它先把补充落库（模拟"计划刚排完、
        还没提交用户就补了一句"），再交出一份带 result_locus 的候选计划。
        """
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

        hub = Hub(store, owner_id='w1')
        with tempfile.TemporaryDirectory() as folder, install_main(folder), \
                install_tools(tools), \
                patch.object(nodes, 'structured_call', model):
            if first_plan is None:
                await worker.run_once(hub, 'w1', run_id='run1')
            else:
                async def fake_plan(state):
                    # 与真实 plan 节点同形：计划放在 plan 键里
                    await store.append_run_input(run_id='run1',
                                                 request_id='req-1',
                                                 message=SUPPLEMENT)
                    return {'plan': dict(first_plan), 'step_index': 0,
                            'status': 'planned'}
                with patch.object(nodes, 'plan', fake_plan):
                    await worker.run_once(hub, 'w1', run_id='run1')
        return (await store.run('run1'), await store.plan('run1'),
                await store.actions_of('run1'), requests)

    async def test_supplement_during_planning_keeps_the_candidate_locus(self):
        """首次计划被作废时，它的落点要跟着走进修订，而不是被清空。"""
        store = new_store()
        await seed_run(store, status='queued', message=GOAL)
        first = {'steps': [{'step_id': 'p1_s1', 'tool': 'canvas_get', 'args': {}},
                           {'step_id': 'p1_s2', 'tool': 'nodes_create',
                            'args': {'name': '西湖'}}],
                 'summary': '读取画布后创建西湖节点', 'result_locus': 'platform_change',
                 'version': 1, 'intent_version': 1}
        run, _, actions, requests = await self.run_worker(
            store, [corrected()], first_plan=first)
        self.assertEqual(run['status'], 'succeeded')
        self.assertEqual([a['status'] for a in actions], ['succeeded'] * 3)
        self.assertEqual(len(requests), 1, '锚点到位时第一版修订就该合法')
        self.assertEqual(requests[0]['被作废的候选计划']['result_locus'],
                         'platform_change')
        self.assertEqual(requests[0]['当前计划']['steps'], [],
                         '作废的候选计划不能伪装成已提交的计划')
        await store.close()

    async def test_contradictory_plan_is_repaired_and_the_steps_run(self):
        """截图那一步：第一版标错落点，修一次之后三步真的跑完。"""
        store = new_store()
        await seed_run(store, status='queued', message=GOAL)
        await store.append_run_input(run_id='run1', request_id='req-1',
                                     message=SUPPLEMENT)
        run, plan, actions, requests = await self.run_worker(
            store, [contradictory(), corrected()])
        self.assertEqual(run['status'], 'succeeded')
        self.assertEqual(len(requests), 2)
        self.assertEqual([a['status'] for a in actions], ['succeeded'] * 3)
        self.assertEqual([a['step_id'] for a in actions],
                         ['p2_s1', 'p2_s2', 'p2_s3'])
        self.assertEqual(plan['version'], 1)
        self.assertEqual(run['processed_intent_version'], run['intent_version'])
        inputs = await store.run_inputs('run1')
        self.assertEqual(inputs[0]['processing_status'], 'applied')
        await store.close()

    async def test_two_contradictory_plans_touch_nothing(self):
        """修不好就一步都不跑：平台侧零调用，补充留着等人。"""
        store = new_store()
        await seed_run(store, status='queued', message=GOAL)
        await store.append_run_input(run_id='run1', request_id='req-1',
                                     message=SUPPLEMENT)
        run, plan, actions, requests = await self.run_worker(
            store, [contradictory(), contradictory()])
        self.assertEqual(len(requests), 2)
        self.assertEqual(run['status'], 'waiting_input')
        self.assertIn('platform_state_cannot_write', run['error'])
        self.assertEqual(actions, [], '没有准入就一个远端调用都不该发')
        self.assertFalse(plan)
        self.assertEqual(run['plan_version'], 0)
        inputs = await store.run_inputs('run1')
        self.assertEqual(inputs[0]['processing_status'], 'accepted',
                         '没纳入计划的补充不能被标成已处理')
        await store.close()


if __name__ == '__main__':
    unittest.main()
