"""规划与编排：计划合同、寒暄、整图、结构化调用、用户画像。"""
import asyncio
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.core_support import patch_all_emits
from webpage_agent.agent import nodes
from webpage_agent.core.schemas import ExecutionPlan


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    """Planner-Executor：计划决定走哪条路，执行器逐步落实。"""

    async def test_next_step_follows_the_cursor(self):
        from webpage_agent.agent import nodes
        plan = {'steps': [{'tool': 'web_search'}, {'tool': 'render_html'}]}
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 0}), 'run_step')
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 1}), 'run_step')
        # 计划走完不是"直接结束"，而是进收尾门禁（那里还要问一次有没有新补充）
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 2}),
                         'finalize')
        self.assertEqual(nodes.next_step({}), 'finalize')

    async def test_next_step_stops_on_states_that_need_a_human(self):
        """暂停/待核实/失败：本次执行直接退出，不要继续派发下一步。"""
        from webpage_agent.agent import nodes
        plan = {'steps': [{'tool': 'web_search'}, {'tool': 'render_html'}]}
        for status in ('paused', 'needs_reconcile', 'failed',
                       'partially_succeeded', 'cancelled', 'waiting_input'):
            self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 0,
                                              'status': status}), 'stop', status)

    async def test_next_step_prefers_revision_when_intent_changed(self):
        """用户补了要求：先去修订剩余计划，而不是把旧计划的下一步跑掉。"""
        from webpage_agent.agent import nodes
        plan = {'steps': [{'tool': 'web_search'}, {'tool': 'render_html'}]}
        self.assertEqual(nodes.next_step({'plan': plan, 'step_index': 0,
                                          'intent_changed': True}),
                         'revise_plan')

    async def test_final_status_is_generic_and_label_is_derived(self):
        """状态机只存通用状态；answered / local_ready 是**派生**出来的展示标签。"""
        from webpage_agent.agent import nodes
        from webpage_agent.core import outcome
        self.assertEqual(nodes.final_status({'answer': 'hi'}), 'succeeded')
        self.assertEqual(nodes.final_status(
            {'deployment': {'status': 'success'}}), 'succeeded')
        # 回执看不懂 ≠ 失败：写请求可能真的成功了
        self.assertEqual(nodes.final_status({'deployment': {'status': 'pending'}}),
                         'needs_reconcile')
        self.assertEqual(nodes.final_status({'artifact': {'html_path': 'x'}}),
                         'succeeded')
        self.assertEqual(nodes.final_status({}), 'failed')
        self.assertEqual(nodes.final_status({'plan': {'kind': 'compensation'}}),
                         'succeeded')
        self.assertEqual(nodes.final_status(
            {'plan': {'kind': 'compensation'}, 'failure': 'boom'}), 'failed')

        self.assertEqual(outcome.label('succeeded', {'answer': 'hi'}), 'answered')
        self.assertEqual(outcome.label('succeeded',
                                       {'artifact': {'html_path': 'x'}}),
                         'local_ready')
        self.assertEqual(outcome.label('succeeded',
                                       {'deployment': {'status': 'success'},
                                        'artifact': {'html_path': 'x'}}),
                         'deployed')
        self.assertEqual(outcome.label('needs_reconcile',
                                       {'deployment': {'status': 'pending'}}),
                         'deploy_needs_check')
        # 没有部署的未核实场景用通用词，别硬套部署的说法
        self.assertEqual(outcome.label('needs_reconcile', {}), 'needs_reconcile')
        self.assertEqual(outcome.label('failed', {}), 'failed')

    async def test_valid_steps_reports_unknown_tools(self):
        """未知工具要**回报**而不是静默过滤——计划引用不存在的工具属于规划错误。"""
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        kept, dropped = nodes._valid_steps(
            [{'tool': 'web_search', 'args': {'query': 'q'}},
             {'tool': 'not_a_tool', 'args': {}},
             {'tool': 'render_sources_html', 'args': 'not-a-dict'}],
            tool_module.BUILTIN_TOOLS)
        self.assertEqual([s['tool'] for s in kept], ['web_search', 'render_sources_html'])
        self.assertEqual(kept[1]['args'], {})
        self.assertEqual(dropped, ['not_a_tool'])

    async def test_fallback_does_not_guess_business(self):
        """能力裁剪后只剩一条业务链，兜底不再替用户拼它，直接返回空计划。"""
        from webpage_agent.agent import nodes
        for text in ('整理一份 SQLite 报告', '查询如何降低体脂', '你好'):
            self.assertEqual(nodes.fallback_steps({'user_input': text}), [], text)

    async def test_plan_asks_the_model_and_keeps_its_steps(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        captured = {}

        async def fake_catalog(context=None):
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            captured['user'] = user
            return ExecutionPlan.model_validate({'steps': [
                {'tool': 'web_search', 'args': {'query': 'sqlite'},
                 'why': '需要外部资料'},
                {'tool': 'render_sources_html', 'args': {}, 'why': '产出网页'},
            ], 'summary': '先搜再渲染', 'result_locus': 'chat'}), {'total_tokens': 3}

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            result = await nodes.plan({'user_input': '整理 SQLite 报告', 'usage': []})

        tools_used = [s['tool'] for s in result['plan']['steps']]
        self.assertEqual(tools_used, ['web_search', 'render_sources_html'])
        self.assertEqual(result['step_index'], 0)
        self.assertEqual(result['usage'][-1]['stage'], 'plan')
        # 工具清单必须真的进了提示词，否则模型只能瞎猜
        self.assertIn('web_search', captured['user'])
        self.assertIn('render_sources_html', captured['user'])
        # 必须告诉模型手上还有多少料，否则它会重复检索、或空手去整理
        self.assertIn('已有资料', captured['user'])
        self.assertIn('上一轮已产出 HTML', captured['user'])
        self.assertNotIn('部署已开启', captured['user'])
        self.assertIn('现场', captured['user'])
        self.assertEqual(result['plan']['result_locus'], 'chat')

    async def test_planner_brief_omits_facts_from_absent_capabilities(self):
        """网页资料和 HTML 不是内核的默认世界。清单里没有这些能力，简报里就没有这些字段。"""
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.capabilities.registry import Tool
        captured = {}

        async def fake_catalog(context=None):
            return [Tool('mcp__list_items', '列库存', '列出库存',
                         {'type': 'object', 'properties': {}}, None,
                         capability='mcp', server_id='inventory', effect='read')]

        async def fake_call(schema, system, user, **kw):
            captured['user'] = user
            return ExecutionPlan.model_validate({'steps': [
                {'tool': 'mcp__list_items', 'args': {}, 'why': '看库存'}],
                'summary': '列库存'}), None

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            await nodes.plan({'user_input': '还有多少库存', 'usage': []})
        for absent in ('已有资料', '上一轮已产出 HTML', '部署已开启'):
            self.assertNotIn(absent, captured['user'], absent)
        self.assertIn('现场', captured['user'])
        self.assertIn('mcp__list_items', captured['user'])

    async def test_plan_falls_back_when_the_model_fails(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module

        async def fake_catalog(context=None):
            return tool_module.BUILTIN_TOOLS

        async def boom(*a, **k):
            raise RuntimeError('模型不可用')

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', boom):
            result = await nodes.plan({'user_input': '整理 SQLite 报告', 'usage': []})
        # 规划失败就如实降级：没有可执行步骤，交给 final_status 报失败
        self.assertEqual(result['plan']['steps'], [])


    async def test_run_step_runs_exactly_one_tool_and_advances(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool
        seen = []

        async def runner(args, state):
            seen.append(args)
            return {'answer': '好的', 'status': 'answered'}

        fake = Tool('render_sources_html', '资料转 HTML', 'd', {}, runner)
        plan = {'steps': [{'tool': 'render_sources_html',
                           'args': {'title': 'x'}, 'step_id': 's1'}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'render_sources_html': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertEqual(seen, [{'title': 'x'}])
        self.assertEqual(result['step_index'], 1)
        self.assertEqual(result['step_label'], '资料转 HTML')

    async def test_run_step_aborts_remaining_steps_on_error(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool

        async def boom(args, state):
            # 本地检索在派发前就失败：显式标注"确认没执行"（文档 §6.1）
            raise tool_module.ToolError('搜不到正文', certainty='not_executed',
                                        phase='dispatch')

        fake = Tool('web_search', '检索来源', 'd', {}, boom)
        plan = {'steps': [{'tool': 'web_search', 'args': {'query': 'q'}},
                          {'tool': 'render_html', 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'web_search': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['step_index'], len(plan['steps']))  # 剩余步骤被跳过
        self.assertIn('搜不到正文', result['failure'])
        self.assertEqual(result['errors'][0]['stage'], 'web_search')
        self.assertEqual(result['errors'][0]['certainty'], 'not_executed')
        self.assertFalse(result['needs_reconciliation'])

    async def test_unlabelled_failure_is_treated_as_unknown(self):
        """没标注确定性的异常按 unknown 处理：宁可让人核实，也不猜着重发。"""
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool

        async def murky(args, state):
            raise RuntimeError('连接被重置')

        fake = Tool('web_search', '检索来源', 'd', {}, murky)
        plan = {'steps': [{'tool': 'web_search', 'args': {'query': 'q'}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'web_search': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertEqual(result['status'], 'needs_reconcile')
        self.assertTrue(result['needs_reconciliation'])
        self.assertEqual(result['errors'][0]['certainty'], 'unknown')

    async def test_run_step_fails_when_a_planned_tool_disappeared(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module

        async def no_mcp():
            return []

        plan = {'steps': [{'tool': 'mcp__gone', 'args': {}},
                          {'tool': 'render_html', 'args': {}}]}
        with patch.object(tool_module, 'mcp_tools', no_mcp):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        # 计划里点名的工具消失了（MCP 掉线）：**不能静默跳过**——后续步骤会
        # 拿不到输入，最后却可能因为别的产物而"成功"（文档 §7.4）
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['tool_unavailable'], 'mcp__gone')
        self.assertIn('后续依赖它的步骤没有执行', result['failure'])
        self.assertEqual(result['step_index'], len(plan['steps']))


class AnswerCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """基础回答能力：简单问题不该被拖去检索（对应用户指出的流程）。"""

    async def test_registry_has_the_local_capabilities(self):
        """本地能力只剩三件事：检索正文、把正文转成 HTML、以及基础回答。

        部署不在里面：它由 MCP 发现得到（身份 mcp:<server>:<tool>），
        见 `DeployGateTests`。本地身份只发给"纯计算/纯回答"的工具。
        """
        from webpage_agent.tools import core as tool_module
        self.assertEqual(sorted(tool_module.BUILTIN_BY_NAME),
                         ['answer_directly', 'answer_with_sources',
                          'fetch_pages', 'render_sources_html', 'web_search'])
        keys = {name: tool.tool_key
                for name, tool in tool_module.BUILTIN_BY_NAME.items()}
        self.assertEqual(keys['web_search'], 'local:web:search')
        self.assertEqual(keys['answer_directly'], 'local:answers:direct')
        self.assertEqual(keys['render_sources_html'],
                         'local:export:sources_html')
        self.assertTrue(all(key.startswith('local:')
                            for key in keys.values()))

    async def test_small_talk_skips_planning_but_still_answers(self):
        """闲聊不花规划调用，但**回答仍由模型给出**（不是固定文案）。"""
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module

        async def forbidden(*a, **k):
            raise AssertionError('闲聊不该调用模型做规划')

        async def forbidden_catalog():
            raise AssertionError('闲聊不该去发现工具')

        with patch.object(tool_module, 'catalog', forbidden_catalog), \
                patch.object(nodes, 'structured_call', forbidden):
            result = await nodes.plan({'user_input': '你好', 'usage': []})
        self.assertEqual([s['tool'] for s in result['plan']['steps']],
                         ['answer_directly'])
        self.assertEqual(result.get('usage', []), [])

    async def test_answer_directly_never_touches_documents(self):
        from webpage_agent.capabilities import answers as answers_cap

        async def fake_chat(system, messages, model=None, on_delta=None):
            return 'ACID 是事务的四个特性。'

        with patch.object(answers_cap, 'chat_reply', fake_chat):
            result = await answers_cap._answer_directly(
                {'question': '什么是 ACID'},
                {'user_input': '什么是 ACID', 'history': []})
        self.assertEqual(result['status'], 'answered')
        self.assertIn('ACID', result['answer'])

    async def test_answer_with_sources_returns_the_reply_verbatim(self):
        from webpage_agent.core import sources
        from webpage_agent.capabilities import answers as answers_cap
        with tempfile.TemporaryDirectory() as folder:
            fake = SimpleNamespace(artifact_dir=Path(folder))
            with patch.object(sources, 'settings', fake):
                doc = sources.metadata('t1', 's1', url='https://a/1',
                                       title='甲文', text='正文内容')

                async def fake_chat(system, messages, model=None, on_delta=None):
                    return '结论 [s1]。'

                with patch.object(answers_cap, 'chat_reply', fake_chat), \
                        patch_all_emits(lambda e: None):
                    result = await answers_cap._answer_with_sources(
                        {'question': '问题'}, {'documents': [doc], 'usage': []})
        self.assertEqual(result['status'], 'answered')
        # 检索接口返回什么就呈现什么：平台**不再**追加"参考来源"清单，
        # 也不要求模型打 [s1] 这类编号
        self.assertEqual(result['answer'], '结论 [s1]。'.strip())
        self.assertNotIn('参考来源', result['answer'])

    async def test_answer_with_sources_requires_material(self):
        from webpage_agent.capabilities import answers as answers_cap
        from webpage_agent.capabilities.base import ToolError
        with self.assertRaises(ToolError):
            await answers_cap._answer_with_sources(
                {'question': 'q'}, {'documents': [], 'usage': []})


class PlanContractTests(unittest.IsolatedAsyncioTestCase):
    """计划合同：步骤 ID、依赖、输入绑定与结构化校验（文档 §6）。"""

    async def test_step_ids_assigned_and_dependencies_validated(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tm
        steps, issues = nodes.validate_plan([
            {'tool': 'web_search', 'args': {'query': 'x'}},
            {'tool': 'render_sources_html', 'depends_on': ['s1']},
        ], tm.BUILTIN_TOOLS)
        self.assertEqual([s['step_id'] for s in steps], ['s1', 's2'])
        self.assertEqual(issues, [])
        self.assertEqual(steps[0]['tool_key'], 'local:web:search')

    async def test_forward_or_missing_dependency_is_an_error(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tm
        _, issues = nodes.validate_plan([
            {'tool': 'web_search', 'args': {'query': 'x'}, 'step_id': 'a'},
            {'tool': 'render_sources_html', 'depends_on': ['b'], 'step_id': 'c'},
        ], tm.BUILTIN_TOOLS)
        self.assertIn('bad_dependency', [i['code'] for i in issues])

    async def test_duplicate_step_id_is_an_error(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tm
        _, issues = nodes.validate_plan([
            {'tool': 'web_search', 'args': {}, 'step_id': 'a'},
            {'tool': 'render_sources_html', 'step_id': 'a'},
        ], tm.BUILTIN_TOOLS)
        self.assertIn('duplicate_step_id', [i['code'] for i in issues])

    async def test_input_binding_resolves_previous_output(self):
        from webpage_agent.agent import nodes
        args = nodes.resolve_inputs(
            {'args': {'artifact_id': {'ref': {'step_id': 's1',
                                              'pointer': '/artifact/id'}},
                      'title': {'literal': '活动页'},
                      'plain': 'as-is'}},
            {'s1': {'artifact': {'id': 'art_1'}}})
        self.assertEqual(args, {'artifact_id': 'art_1', 'title': '活动页',
                                'plain': 'as-is'})

    async def test_missing_binding_fails_instead_of_feeding_none(self):
        """引用解析不到就报错——把 None 喂给远端只会换来一句看不出来的 400。"""
        from webpage_agent.agent import nodes
        from webpage_agent.capabilities.base import ToolError
        with self.assertRaises(ToolError) as ctx:
            nodes.resolve_inputs(
                {'step_id': 's9',
                 'args': {'x': {'ref': {'step_id': 'nope', 'pointer': '/a'}}}}, {})
        self.assertIn('引用不到值', ctx.exception.safe_message)
        # 字段存在就能正常解析
        args = nodes.resolve_inputs(
            {'args': {'x': {'ref': {'step_id': 's1', 'pointer': '/a'}}}},
            {'s1': {'a': 'ok'}})
        self.assertEqual(args['x'], 'ok')

    async def test_binding_inside_nested_object_is_resolved(self):
        """绑定落在里层参数也要解析。

        真踩过：`canvas.node.configure` 的 `patch.model` 里写了
        `{"ref": {"step_id": "s2", "pointer": "/models/0/id"}}`，只在最外层
        找 ref 的实现把它原样发给服务端，换回一句 JSON Schema 校验失败
        "`{'ref': ...}` is not of type 'string'"——看着像模型传错类型，
        其实是调度器没解析。
        """
        from webpage_agent.agent import nodes
        args = nodes.resolve_inputs(
            {'step_id': 's4',
             'args': {'canvasId': 'c1', 'kind': 'image',
                      'patch': {'model': {'ref': {'step_id': 's2',
                                                  'pointer': '/models/0/id'}},
                                'size': '1024x1024'}}},
            {'s2': {'models': [{'id': 'm-first'}, {'id': 'm-second'}]}})
        self.assertEqual(args['patch']['model'], 'm-first')
        self.assertEqual(args['patch']['size'], '1024x1024')
        self.assertEqual(args['canvasId'], 'c1')

    async def test_binding_inside_list_is_resolved(self):
        """数组元素里的绑定同样要解析。"""
        from webpage_agent.agent import nodes
        args = nodes.resolve_inputs(
            {'step_id': 's2',
             'args': {'nodeIds': [{'ref': {'step_id': 's1', 'pointer': '/ids/1'}},
                                  'literal-node']}},
            {'s1': {'ids': ['a', 'b']}})
        self.assertEqual(args['nodeIds'], ['b', 'literal-node'])

    async def test_ref_supports_list_index_and_whole_output(self):
        """指针要能下标取数组；空指针表示整份输出。"""
        from webpage_agent.agent import nodes
        outputs = {'s1': {'rows': [{'id': 'r0'}, {'id': 'r1'}]}}
        args = nodes.resolve_inputs(
            {'step_id': 's2',
             'args': {'first': {'ref': {'step_id': 's1', 'pointer': '/rows/1/id'}},
                      'all': {'ref': {'step_id': 's1'}}}},
            outputs)
        self.assertEqual(args['first'], 'r1')
        self.assertEqual(args['all'], outputs['s1'])

    async def test_nested_binding_failure_points_at_the_field(self):
        """嵌套引用解析不到时报错要指到具体字段，别只说参数名。"""
        from webpage_agent.agent import nodes
        from webpage_agent.capabilities.base import ToolError
        with self.assertRaises(ToolError) as ctx:
            nodes.resolve_inputs(
                {'step_id': 's4',
                 'args': {'patch': {'model': {'ref': {'step_id': 's2',
                                                      'pointer': '/models/9/id'}}}}},
                {'s2': {'models': [{'id': 'm'}]}})
        self.assertIn('patch.model', ctx.exception.safe_message)
        self.assertIn('不存在', ctx.exception.safe_message)

    async def test_plain_object_with_ref_like_field_is_left_alone(self):
        """普通参数里恰好有个 ref 字段（不是整份绑定）不能被改写。"""
        from webpage_agent.agent import nodes
        args = nodes.resolve_inputs(
            {'step_id': 's1',
             'args': {'filter': {'ref': 'inner-1', 'other': 2}}},
            {})
        self.assertEqual(args['filter'], {'ref': 'inner-1', 'other': 2})

    async def test_binding_can_read_saved_full_result(self):
        """嵌套结果没进步骤输出时，按落盘结果解析。

        步骤输出只保留标量，`result.created[0].id` 这类结构在
        `results/<step>.json` 里。模型写指针时对着的正是那层结构，所以解析
        必须能落到它上面，否则 `/nodes/0/id` 永远"引用不到值"。
        """
        from webpage_agent.agent import nodes
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / 'results'
            results.mkdir()
            (results / 's3-act.json').write_text(json.dumps({
                'payload': [{'tool': 'canvas_nodes_create',
                             'payload': {'op': 'canvas.nodes.create',
                                         'result': {
                                             'canvasId': 'c1',
                                             'created': [{'id': 'node-1'}],
                                             'nodes': [{'id': 'node-1'}]}}}]}),
                encoding='utf-8')
            outputs = {'s3': {'__status__': 'succeeded', 'revision': 2,
                              'tool_result': {'data_ref': 'results/s3-act.json'}}}
            args = nodes.resolve_inputs(
                {'step_id': 's4',
                 'args': {'nodeId': {'ref': {'step_id': 's3',
                                             'pointer': '/nodes/0/id'}}}},
                outputs, tmp)
            self.assertEqual(args['nodeId'], 'node-1')
            # 给了目录才允许读回结果；不给时保持原样（旧行为）
            with self.assertRaises(Exception):
                nodes.resolve_inputs(
                    {'step_id': 's4',
                     'args': {'nodeId': {'ref': {'step_id': 's3',
                                                 'pointer': '/nodes/0/id'}}}},
                    outputs)

    async def test_run_step_blocks_when_dependency_not_succeeded(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool

        async def should_not_run(args, state):
            raise AssertionError('依赖未完成时不应执行')

        fake = Tool('render_sources_html', '资料转 HTML', 'd', {}, should_not_run,
                    tool_key='local:reports:render', effect='write')
        plan = {'steps': [{'tool': 'render_sources_html', 'step_id': 's2',
                           'depends_on': ['s1'], 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'render_sources_html': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q',
                 'step_outputs': {'s1': {'__status__': 'failed'}}})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('依赖', result['failure'])

    async def test_run_step_records_output_for_downstream_binding(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool

        async def run(args, state):
            return {'artifact': {'id': 'art_9'}}

        fake = Tool('render_sources_html', '资料转 HTML', 'd', {}, run,
                    tool_key='local:reports:render', effect='write')
        plan = {'steps': [{'tool': 'render_sources_html', 'step_id': 's1', 'args': {}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'render_sources_html': fake}):
            result = await nodes.run_step(
                {'plan': plan, 'step_index': 0, 'user_input': 'q'})
        self.assertEqual(result['step_outputs']['s1']['artifact']['id'], 'art_9')
        self.assertEqual(result['step_outputs']['s1']['__status__'], 'succeeded')


class ModelOverrideTests(unittest.TestCase):
    """整理报告阶段可以用独立（更快）的模型。"""

    def setUp(self):
        from webpage_agent.core import services
        fixture = patch.object(services, 'settings', replace(services.settings,
            llm_key='isolated-unit-test-key', llm_model='unit-test-base-model'))
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_default_uses_main_model(self):
        from webpage_agent.core import services
        self.assertEqual(services.new_llm().model_name, services.settings.llm_model)

    def test_explicit_override_wins(self):
        from webpage_agent.core.services import new_llm
        self.assertEqual(new_llm('some-fast-model').model_name, 'some-fast-model')


class ThinkingControlTests(unittest.IsolatedAsyncioTestCase):
    """推理开关：解析/规划阶段可以关。"""

    async def test_disable_thinking_sets_thinking_disabled(self):
        import webpage_agent.core.services as services
        with patch.object(services, 'ChatOpenAI') as fake:
            services.new_llm(disable_thinking=True)
        kwargs = fake.call_args.kwargs
        # 只有 thinking.disabled 能真正关掉推理（low/minimal 会被网关忽略）
        self.assertEqual(kwargs['extra_body'], {'thinking': {'type': 'disabled'}})

    async def test_default_keeps_thinking_enabled(self):
        import webpage_agent.core.services as services
        with patch.object(services, 'ChatOpenAI') as fake:
            services.new_llm()
        kwargs = fake.call_args.kwargs
        self.assertNotIn('extra_body', kwargs)
        self.assertNotIn('reasoning_effort', kwargs)

    async def test_planner_passes_the_flag(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        captured = {}

        async def fake_catalog(context=None):
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            captured.update(kw)
            return ExecutionPlan.model_validate(
                {'steps': [{'tool': 'render_sources_html', 'args': {}}]}), None

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            await nodes.plan({'user_input': '介绍一下 SQLite', 'usage': []})
        from webpage_agent.core.config import settings
        self.assertEqual(captured['disable_thinking'],
                         settings.llm_parse_disable_thinking)


class ProfileTests(unittest.TestCase):
    """用户画像：Markdown 文件 + 原子写 + 版本守卫。"""

    def setUp(self):
        from webpage_agent.core.config import settings
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = settings.profile_path
        # settings 是 frozen dataclass，用 object.__setattr__ 绕过冻结
        object.__setattr__(settings, 'profile_path',
                           Path(self._tmp.name) / 'USER.md')

    def tearDown(self):
        from webpage_agent.core.config import settings
        object.__setattr__(settings, 'profile_path', self._orig)
        self._tmp.cleanup()

    def test_merge_and_reload_roundtrip(self):
        from webpage_agent.core import profile
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
        from webpage_agent.core import profile
        self.assertEqual(profile.version(), 0)
        profile.merge({'a': 'b'})
        self.assertEqual(profile.version(), 1)
        profile.merge({'c': 'd'})
        self.assertEqual(profile.version(), 2)

    def test_block_and_with_profile(self):
        from webpage_agent.core import profile
        profile.merge({'偏好': '简洁'})
        self.assertIn('[用户画像]', profile.block())
        self.assertIn('偏好：简洁', profile.block())
        out = profile.with_profile('指令ABC')
        self.assertTrue(out.startswith('指令ABC'))
        self.assertIn('[用户画像]', out)
        # 空画像时原样返回
        profile.clear()
        self.assertEqual(profile.with_profile('指令ABC'), '指令ABC')

    def test_merge_reports_only_real_changes(self):
        """merge 只返回变化量，调用方才能区分"学到新的"和"把已有的抄一遍"。"""
        from webpage_agent.core import profile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'USER.md'
            with patch.object(profile, 'settings', SimpleNamespace(profile_path=path)):
                profile.merge({'身高': '173cm', '体重': '50kg'})
                # 原样重复：什么都没变
                self.assertEqual(profile.merge({'身高': '173cm'}), {})
                # 一部分变化：只报变化的那部分
                changed = profile.merge({'身高': '173cm', '体重': '52kg',
                                         '新项': 'x'})
                self.assertEqual(changed, {'体重': '52kg', '新项': 'x'})
                self.assertEqual(profile.load()['身高'], '173cm')

    def test_clear(self):
        from webpage_agent.core import profile
        profile.merge({'x': 'y'})
        profile.clear()
        self.assertEqual(profile.load(), {})
        self.assertEqual(profile.version(), 0)

    def test_backup_kept_on_save(self):
        from webpage_agent.core import profile
        from webpage_agent.core.config import settings
        profile.merge({'旧': '值'})
        profile.merge({'新': '值'})
        bak = settings.profile_path.parent / 'USER.md.bak'
        self.assertTrue(bak.exists())
        self.assertIn('旧：值', bak.read_text(encoding='utf-8'))


def profile_file():
    from webpage_agent.core.config import settings
    return str(settings.profile_path)


class PlannerProfileTests(unittest.IsolatedAsyncioTestCase):
    """规划器也会记用户信息：这条能力不能随重写丢掉。"""

    async def test_plan_merges_profile_updates(self):
        from webpage_agent.agent import nodes
        from webpage_agent.core import profile
        from webpage_agent.tools import core as tool_module

        async def fake_catalog(context=None):
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            return ExecutionPlan.model_validate({
                'steps': [{'tool': 'render_sources_html', 'args': {}}],
                'profile_updates': {'电话': '13900000000'},
                'result_locus': 'chat',
            }), None

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'USER.md'
            # settings 是 frozen dataclass，只能整体替换而不能改字段
            with patch.object(profile, 'settings', SimpleNamespace(profile_path=path)):
                with patch.object(tool_module, 'catalog', fake_catalog), \
                        patch.object(nodes, 'structured_call', fake_call):
                    await nodes.plan({'user_input': '我电话13900000000', 'usage': []})
                self.assertTrue(path.exists(), '画像应被写入文件')
                self.assertIn('13900000000', path.read_text(encoding='utf-8'))


class SmallTalkFilterTests(unittest.IsolatedAsyncioTestCase):
    """本地寒暄筛选：明显是寒暄就直接回答，省掉一次规划调用。"""

    async def test_recognises_plain_small_talk(self):
        from webpage_agent.core.services import is_small_talk
        for text in ['你好', '您好！', '你好呀', '在吗', '谢谢', '好的谢谢',
                     '嗯嗯', 'ok', 'OK!', 'hello', 'thanks', 'thank you',
                     '早上好', '晚安', '再见', '好的，谢谢啦', '收到']:
            self.assertTrue(is_small_talk(text), text)

    async def test_leaves_real_tasks_to_the_model(self):
        from webpage_agent.core.services import is_small_talk
        # 误把任务当寒暄，会让该检索的请求变成闲聊；比多花一次调用糟得多，所以宁可漏判。
        for text in ['帮我查一下 SQLite 的适用场景',
                     '你好，帮我查一下 SQLite 的适用场景',
                     '谢谢，请写一份报告',
                     '什么是 ACID',
                     'https://example.com',
                     'higher', 'okr',
                     '谢谢你帮我写个报告']:
            self.assertFalse(is_small_talk(text), text)

    async def test_link_is_never_treated_as_small_talk(self):
        """带链接就必须走规划与检索，不能因为句子短就被当成寒暄。"""
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        called = {}

        async def fake_catalog(context=None):
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            called['hit'] = True
            return ExecutionPlan.model_validate({'steps': [
                {'tool': 'fetch_pages', 'args': {'urls': ['https://example.com']}}],
                'result_locus': 'chat'}), None

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            result = await nodes.plan(
                {'user_input': 'hi https://example.com', 'usage': []})
        self.assertTrue(called.get('hit'), '带 URL 时不能跳过规划')
        self.assertEqual(result['plan']['steps'][0]['tool'], 'fetch_pages')


class GraphShapeTests(unittest.IsolatedAsyncioTestCase):
    """整图跑通：寒暄不检索，规划出来的步骤被逐步执行。"""

    async def test_small_talk_answers_without_searching(self):
        from webpage_agent.tools import core as tool_module
        from webpage_agent.agent.graph import build_graph
        from webpage_agent.tools.core import Tool
        touched = {'searched': False}

        async def fake_answer(args, state):
            return {'answer': '你好！', 'status': 'answered'}

        async def fake_search(args, state):
            touched['searched'] = True
            return {'documents': []}

        table = dict(tool_module.BUILTIN_BY_NAME)
        table['answer_directly'] = Tool('answer_directly', '直接回答', 'd', {}, fake_answer)
        table['web_search'] = Tool('web_search', '检索来源', 'd', {}, fake_search)

        async def fake_catalog(context=None):
            return list(table.values())

        with patch.dict(tool_module.BUILTIN_BY_NAME, table), \
                patch.object(tool_module, 'catalog', fake_catalog):
            result = await build_graph().ainvoke({
                'task_id': 'x', 'user_input': '你好', 'documents': [],
                'errors': [], 'usage': [], 'step_index': 0, 'mcp_calls': []})
        from webpage_agent.core import outcome
        self.assertEqual(result['status'], 'succeeded')     # 状态机里的通用值
        self.assertEqual(outcome.label(result['status'], result), 'answered')
        self.assertEqual(result['outcome']['kind'], 'answer')
        self.assertFalse(touched['searched'])

    async def test_plan_steps_run_in_order(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module
        from webpage_agent.agent.graph import build_graph
        from webpage_agent.tools.core import Tool
        order = []

        def make(name, label):
            async def run(args, state):
                order.append(name)
                return {}
            return Tool(name, label, 'd', {}, run)

        table = {'web_search': make('web_search', '检索来源'),
                 'render_html': make('render_html', '渲染 HTML')}

        async def fake_catalog(context=None):
            return list(table.values())

        async def fake_plan(state):
            return {'plan': {'steps': [{'tool': 'web_search', 'args': {}},
                                       {'tool': 'render_html', 'args': {}}]},
                    'step_index': 0, 'status': 'planned'}

        with patch.dict(tool_module.BUILTIN_BY_NAME, table), \
                patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'plan', fake_plan):
            await build_graph().ainvoke({
                'task_id': 'x', 'user_input': 'q', 'documents': [],
                'errors': [], 'usage': [], 'step_index': 0, 'mcp_calls': []})
        self.assertEqual(order, ['web_search', 'render_html'])


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
        return [self.FakeChunk(tool_calls=[{'name': 'ExecutionPlan', 'args': {'steps': []}}],
                               usage={'total_tokens': tokens})]

    def good(self, tokens=7):
        return [self.FakeChunk(
            tool_calls=[{'name': 'ExecutionPlan',
                         'args': {'steps': [{'tool': 'web_search',
                                             'args': {'query': 't'}}]}}],
            usage={'total_tokens': tokens})]

    async def call(self, outcomes, calls=None, **kw):
        """在 patch 生效期间完成调用，返回 ((parsed, usage), calls)。

        await 必须在 with 块里面：否则协程在 patch 退出后才执行，mock 失效、
        直接打到真实 API（这个坑踩过一次，表现为测试里 usage 大得离谱）。
        """
        from webpage_agent.core import services
        calls = calls if calls is not None else []
        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False:
                          self.fake_llm(outcomes, calls)):
            result = await services.structured_call(ExecutionPlan, 's', 'u', **kw)
        return result, calls

    async def test_retries_once_then_succeeds(self):
        (parsed, _), calls = await self.call([self.bad(), self.good()])
        self.assertEqual(parsed.steps[0].tool, 'web_search')
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
        self.assertEqual(calls[0], ('bind', 'ExecutionPlan'))

    async def test_attempt_timeout_fails_without_retry(self):
        """单次尝试超时立刻失败，不再试第二次。

        重试只会把等待时间翻倍，而超时通常意味着模型/网关本身慢，重试收益低。
        """
        from webpage_agent.core import services
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
                    ExecutionPlan, 's', 'u', attempts=2, attempt_timeout=0.05)
        self.assertEqual(len(calls), 1, '超时不应该重试')

    async def test_attempt_timeout_zero_disables_the_limit(self):
        """attempt_timeout<=0 表示不限制，走正常路径。"""
        (parsed, _), calls = await self.call([self.good()], attempts=1,
                                             attempt_timeout=0)
        self.assertEqual(parsed.steps[0].tool, 'web_search')
        self.assertEqual(len(calls), 1)

    async def test_timeout_does_not_swallow_user_cancel(self):
        """外部取消（用户点停止）必须继续抛 CancelledError，不能被当成超时。"""
        from webpage_agent.core import services

        class BlockingLLM:
            def bind_tools(self, tools, tool_choice=None):
                return self

            async def astream(self, messages):
                await asyncio.sleep(30)
                yield self.FakeChunk()

        async def run():
            await services.structured_call(
                ExecutionPlan, 's', 'u', attempts=1, attempt_timeout=30)

        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False: BlockingLLM()):
            task = asyncio.create_task(run())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_draft_callback_clears_then_streams_partials(self):
        from webpage_agent.core import services
        calls, drafts = [], []
        partial = self.FakeChunk(
            tool_call_chunks=[{'args':
                '{"steps": [{"tool": "web_search", "args": {"query": "半成品"}}]}'}])
        full = self.FakeChunk(
            tool_calls=[{'name': 'ExecutionPlan',
                         'args': {'steps': [{'tool': 'web_search',
                                             'args': {'query': '完整'}}]}}],
            usage={'total_tokens': 3})
        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False:
                          self.fake_llm([[partial, full]], calls)):
            parsed, _ = await services.structured_call(
                ExecutionPlan, 's', 'u', attempts=1, on_draft=drafts.append, draft_interval=0)
        self.assertEqual(parsed.steps[0].args['query'], '完整')
        # 首次回调必须是 None（清空）：重试时也一样，避免两次内容混在一起
        self.assertIsNone(drafts[0])
        # 中途要能收到半成品（第一步已到、其余还在生成）
        self.assertTrue(any(d is not None and d.steps
                            and d.steps[0].args.get('query') == '半成品'
                            for d in drafts))
        # 最终结果也要推一次，保证前端拿到完整版
        self.assertEqual(drafts[-1].steps[0].args['query'], '完整')

    async def test_throttle_advances_even_when_draft_unparsable(self):
        """解析失败也必须推进节流时间戳。

        否则字段还没凑齐时会退化成"每个分片都重试一次"，对不断增长的缓冲区
        反复做解析与校验，O(n²) 会把事件循环堵死（这个 bug 真在服务里卡死过一次）。
        """
        from webpage_agent.core import services
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
                    ExecutionPlan, 's', 'u', attempts=1, on_draft=drafts.append,
                    draft_interval=3600)      # 节流窗口远大于测试时长
        # 40 个分片只允许解析一次（节流生效），而不是 40 次
        self.assertEqual(parsed_count['n'], 1)

    async def test_draft_clear_before_each_retry(self):
        from webpage_agent.core import services
        calls, drafts = [], []
        partial = self.FakeChunk(
            tool_call_chunks=[{'args': '{"steps": [{"tool": "web_search",'
                                        ' "args": {"query": "第一版的残留"}}]}'}])
        bad = self.FakeChunk(
            tool_calls=[{'name': 'ExecutionPlan', 'args': {'steps': []}}],
            tool_call_chunks=[{'args': '{"steps": [{"tool": "web_search"}]}'}])
        good = self.FakeChunk(
            tool_calls=[{'name': 'ExecutionPlan',
                         'args': {'steps': [{'tool': 'answer_directly',
                                             'args': {'question': '第二版'}}]}}])
        with patch.object(services, 'new_llm',
                          lambda model=None, disable_thinking=False:
                          self.fake_llm([[partial, bad], [good]], calls)):
            await services.structured_call(
                ExecutionPlan, 's', 'u', on_draft=drafts.append, draft_interval=0)
        # 两次尝试各清空一次，第一次尝试的残留不会留在界面上
        self.assertEqual([d is None for d in drafts].count(True), 2)


class KernelCorrectnessTests(unittest.IsolatedAsyncioTestCase):
    """内核层：截断声明、坏计划、提示词分包（评审 P0-3 / P0-4 / 通用性）。"""


    async def test_bad_plan_stops_instead_of_executing(self):
        """两轮修不好就停：不能带着坏依赖去执行一份残缺计划。"""
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module

        async def fake_catalog(context=None):
            return tool_module.BUILTIN_TOOLS

        async def fake_call(schema, system, user, **kw):
            return ExecutionPlan.model_validate({'steps': [
                {'tool': 'not_a_tool', 'args': {}}]}), None

        with patch.object(tool_module, 'catalog', fake_catalog), \
                patch.object(nodes, 'structured_call', fake_call):
            result = await nodes.plan({'user_input': '做点事', 'usage': []})
        self.assertEqual(result['status'], 'waiting_input')
        self.assertEqual(result['plan']['steps'], [])
        self.assertIn('unknown_tool', result['failure'])
        # 图必须直接收尾，不能因为"计划为空"又走进 run_step
        self.assertEqual(nodes.next_step(result), 'stop')
        # 停下来的任务也要给用户一句人话
        from webpage_agent.core.messages import reply_text
        self.assertIn('先停在这里', reply_text(result))

    async def test_planner_prompt_is_split_by_capability(self):
        """业务规则归能力包：没有部署工具时，提示词里就不该教怎么部署。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.integrations import mcp_servers
        deploy = tool_module._deploy_tool(
            mcp_servers.Server(id='publishing', url='https://a/mcp',
                               deploy_tool='deploy_site', default=True),
            {'name': 'deploy_site', 'effect_type': 'write'})
        catalog = tool_module.BUILTIN_TOOLS + [deploy]
        text = nodes.planner_system(catalog)
        for needle in ('联网检索', '导出网页', '发布'):
            self.assertIn(needle, text, needle)
        self.assertIn('step_id', text)
        without = [tool for tool in catalog if tool.name != 'deploy_site']
        bare = nodes.planner_system(without)
        self.assertNotIn('【发布】', bare)
        self.assertIn('result_locus', text)
        self.assertIn('platform_change', text)
        self.assertIn('delete_site', text)
        self.assertNotIn('delete_site', bare)
        self.assertNotIn('video_generation', text)
        self.assertNotIn('canvas_nodes_create', text)


class PlannerMistakeGuardTests(unittest.IsolatedAsyncioTestCase):
    """规划器的两类"看起来像服务端故障"的错误，必须在本地说清楚。

    1. 参数里留着 `{{s2.project_id}}` 字面模板（把跨步骤引用写成了字符串）；
    2. 通用调用工具的内部工具名是编的（见 McpBusinessFailureTests）。
    """

    async def test_unresolved_placeholder_fails_fast_with_a_hint(self):
        from webpage_agent.agent import nodes
        from webpage_agent.tools import core as tool_module

        called = []

        async def fake(args, state):
            called.append(args)
            return {}

        tool = tool_module.Tool('render_sources_html', '资料转 HTML', 'd', {}, fake,
                                tool_key='local:export:sources_html', effect='write')
        plan = {'steps': [{'tool': 'render_sources_html', 'step_id': 's1',
                           'args': {'path': 'a.md',
                                    'content': '{{s2.content}}'}}]}
        with patch.dict(tool_module.BUILTIN_BY_NAME, {'render_sources_html': tool}), \
                patch_all_emits(lambda e: None):
            result = await nodes.run_step({'plan': plan, 'step_index': 0,
                                           'user_input': '写个文件'})
        self.assertEqual(called, [], '参数不成立就不该调用工具')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('占位符', result['failure'])
        self.assertIn('ref', result['failure'])

    async def test_real_ref_binding_still_works(self):
        """正确写法（{"ref": {...}}）不受影响。"""
        from webpage_agent.agent import nodes
        outputs = {'s1': {'project_id': 'p-1', 'path': 'x'}}
        args = nodes.resolve_inputs(
            {'args': {'project_id': {'ref': {'step_id': 's1',
                                             'pointer': '/project_id'}}}},
            outputs)
        self.assertEqual(args['project_id'], 'p-1')
        nodes._check_placeholders(args, '工具')


if __name__ == '__main__':
    unittest.main()
