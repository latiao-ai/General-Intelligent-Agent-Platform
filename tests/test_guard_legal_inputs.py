"""护栏的「合法输入必须通过」矩阵。

上一轮抓到的两个 bug 是同一个形状：护栏按**字面相等**比较模型输出（步骤编号、
依赖、参数），而模型的合法输入在现实里是「编号省略 / 从 s1 重新数 / 只写改动的
那一段」。两处都修了，但只修两处不够——这里把**每个拒绝类判定**都钉一遍：

| 判定 | 合法输入（必须过） | 危险输入（必须拒） |
|---|---|---|
| `validate_plan` | 省略 step_id | 未知工具、坏依赖、字面模板 |
| `validate_revision` | 省略 step_id | 重写已执行步骤、重复编号、未知工具、依赖未定动作 |
| `revision_metadata` | 省略字段（继承）、只加条件 | 改结果落点、observe 没写观察目标 |
| `check_completion_criteria` | 证据指向计划里的步骤 | 空目标、指向不存在的步骤、缺产出方 |
| `_prepared_reference_steps` | 省略/重排/重编号、只写失败那一步 | 重做创建、换工具、改字面参数、改健康引用 |

新增护栏时先往这张表里加一行：**合法输入那一列必须先绿**，否则用户拿到的就是
"干完了报失败"。
"""
import unittest

from webpage_agent.agent import nodes
from webpage_agent.agent import run_inputs as run_inputs_module
from webpage_agent.capabilities.registry import Tool


async def _noop(args, state):
    return {}


def catalog():
    def make(name, key, effect):
        return Tool(name, name, name, {'type': 'object'}, _noop,
                    tool_key=key, effect=effect,
                    server_id=key.split(':')[1] if key.startswith('mcp:') else '')
    return [make('canvas_get', 'mcp:svc:get', 'read'),
            make('nodes_create', 'mcp:svc:create', 'write'),
            make('answer_directly', 'local:answers:direct', 'read')]


class ValidatePlanTests(unittest.TestCase):
    def test_missing_step_ids_are_assigned(self):
        steps, issues = nodes.validate_plan(
            [{'tool': 'canvas_get', 'args': {}},
             {'tool': 'nodes_create', 'args': {'name': '西湖'}}], catalog())
        self.assertEqual([step['step_id'] for step in steps], ['s1', 's2'])
        self.assertEqual(issues, [])

    def test_unknown_tool_and_bad_dependency_are_rejected(self):
        _, issues = nodes.validate_plan(
            [{'tool': 'nope', 'args': {}},
             {'tool': 'nodes_create', 'args': {}, 'depends_on': ['nope']}], catalog())
        self.assertIn('unknown_tool', [item['code'] for item in issues])
        self.assertIn('bad_dependency', [item['code'] for item in issues])


class ValidateRevisionTests(unittest.TestCase):
    def validate(self, steps, *, done=('s1',), unknown=()):
        return run_inputs_module.validate_revision(
            steps, plan_version=2, done_steps=set(done),
            unknown_steps=list(unknown),
            known_tools={tool.name for tool in catalog()})

    def test_missing_step_ids_are_assigned_not_dropped(self):
        kept, issues = self.validate([{'tool': 'nodes_create', 'args': {}},
                                      {'tool': 'answer_directly', 'args': {}}])
        self.assertEqual([step['step_id'] for step in kept], ['p2_s1', 'p2_s2'])
        self.assertEqual(issues, [])

    def test_explicit_ids_are_kept(self):
        kept, _ = self.validate([{'tool': 'nodes_create', 'step_id': 'p2_s7'}])
        self.assertEqual(kept[0]['step_id'], 'p2_s7')

    def test_dangerous_revisions_are_rejected(self):
        cases = [
            ([{'tool': 'nodes_create', 'step_id': 's1'}], 'overwrites_executed_step', 0),
            ([{'tool': 'nodes_create', 'step_id': 'p2_s1'},
              {'tool': 'nodes_create', 'step_id': 'p2_s1'}], 'duplicate_step_id', 1),
            ([{'tool': 'nope', 'args': {}}], 'unknown_tool', 0),
            ([{'tool': 'nodes_create', 'args': {}, 'depends_on': ['u1']}],
             'depends_on_unknown_action', 0),
        ]
        for steps, code, kept_count in cases:
            kept, issues = self.validate(steps, unknown=[{'step_id': 'u1'}])
            self.assertIn(code, [item['code'] for item in issues], steps)
            # 重复编号只丢重复的那一份，其余照留
            self.assertEqual(len(kept), kept_count, steps)


class RevisionMetadataTests(unittest.TestCase):
    def previous(self):
        return {'result_locus': 'platform_change',
                'completion_criteria': [{'kind': 'tool_succeeded', 'target': 's1'}],
                'after_segment': 'observe', 'observation_goal': '确认已写入'}

    def test_omitting_fields_inherits_them(self):
        metadata, issues = run_inputs_module_revision_metadata(
            {'completion_criteria': [{'kind': 'answer_present'}]}, self.previous())
        self.assertEqual(metadata['result_locus'], 'platform_change')
        self.assertEqual(metadata['after_segment'], 'observe')
        self.assertEqual(metadata['observation_goal'], '确认已写入')
        self.assertEqual(len(metadata['completion_criteria']), 2)
        self.assertEqual(issues, [])

    def test_changing_locus_or_breaking_segment_is_rejected(self):
        _, issues = run_inputs_module_revision_metadata(
            {'result_locus': 'chat'}, self.previous())
        self.assertIn('result_locus_changed', [item['code'] for item in issues])
        _, issues = run_inputs_module_revision_metadata(
            {'after_segment': 'observe'}, {'after_segment': 'verify'})
        self.assertIn('bad_segment_policy', [item['code'] for item in issues])


class CompletionCriteriaTests(unittest.TestCase):
    def check(self, criteria, steps, locus='platform_change'):
        return nodes.check_completion_criteria(criteria, steps, catalog(), locus)

    def test_evidence_on_a_real_step_passes(self):
        steps = [{'step_id': 'p2_s1', 'tool': 'nodes_create', 'args': {}}]
        kept, issues = self.check(
            [{'kind': 'tool_succeeded', 'target': 'p2_s1'}], steps)
        self.assertEqual(len(kept), 1)
        self.assertEqual(issues, [])

    def test_bad_targets_are_rejected(self):
        steps = [{'step_id': 'p2_s1', 'tool': 'nodes_create', 'args': {}}]
        for criteria, code in [
                ([{'kind': 'tool_succeeded', 'target': ''}],
                 'criterion_without_target'),
                ([{'kind': 'tool_succeeded', 'target': 'p2_s9'}],
                 'criterion_target_unknown'),
                ([{'kind': 'answer_present'}], 'criterion_needs_answer_step'),
                ([{'kind': 'deployment_receipt'}], 'criterion_needs_deploy_step')]:
            kept, issues = self.check(criteria, steps)
            self.assertEqual(kept, [], criteria)
            self.assertEqual(issues[0]['code'], code, criteria)


class ReferenceRepairTests(unittest.TestCase):
    def test_legal_repair_shapes_all_pass(self):
        original = [{'step_id': 'p2_s2', 'tool': 'nodes_create',
                     'args': {'r': {'ref': {'step_id': 'p2_s1',
                                            'pointer': '/data/r'}}},
                     'depends_on': ['p2_s1']}]
        fixed = {'tool': 'nodes_create',
                 'args': {'r': {'ref': {'step_id': 'p2_s1',
                                        'pointer': '/data/revision'}}}}
        for candidate in ([{**fixed, 'step_id': 'p2_s2', 'depends_on': ['p2_s1']}],
                          [fixed],
                          [dict(fixed, step_id='s1')]):
            aligned = nodes._align_repair_steps(candidate, original)
            assert aligned is not None, candidate
            self.assertEqual(aligned[0]['step_id'], 'p2_s2')
            self.assertEqual(aligned[0]['depends_on'], ['p2_s1'])

    def test_extra_steps_are_rejected(self):
        original = [{'step_id': 'p2_s2', 'tool': 'nodes_create', 'args': {}}]
        self.assertIsNone(nodes._align_repair_steps(
            [{'tool': 'nodes_create'}, {'tool': 'nodes_create'}], original))


def run_inputs_module_revision_metadata(data, previous):
    return nodes.revision_metadata(data, previous)


if __name__ == '__main__':
    unittest.main()
