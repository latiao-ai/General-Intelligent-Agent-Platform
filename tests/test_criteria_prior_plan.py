"""修订时，继承下来的完成条件要认得**上一版计划已经做过的事**。

真实故障（LibTV 画布任务，run acb59ed37e70）：计划 4 步，第 3 步「读时间线」
失败触发修订。模型把新计划收缩成「读节点 + 作答」，但完成条件只能累加，旧的
两条还挂着 `read_project` / `read_timeline`。校验时"可用目标"只算了新计划的
步骤，于是两条旧条件被判成"指向不存在的工具"，任务卡死在一句

    调整后的计划没有通过校验（criterion_target_unknown: …）

两次报错分别来自两条路径，根因都是同一处：`completed_steps` 只从
`state['step_outputs']` 快照推导，而这份快照在恢复/观察路径里可能是空的。

这里的边界：
- 旧步骤**真的成功过** → 条件保留（它已经满足了），不能判成未知；
- 旧步骤**被放弃且只读** → 条件降级 warning 丢掉（永远不可能再满足，留着只会
  每一轮修订都撞同一堵墙；只读没有外部副作用）；
- 旧步骤**被放弃但是写操作** → 仍然阻断（不能让"没做"混成"做完了"）；
- 谁也不认识的 target → 仍然阻断（安全网不退让）。
"""
import unittest

from webpage_agent.agent.plan_validate import (_completed_step_definitions,
                                               check_completion_criteria)
from webpage_agent.capabilities.registry import Tool


def remote(name, key, *, effect='read'):
    return Tool(name, name, f'{name} 描述', {'type': 'object'}, None,
                tool_key=key, effect=effect, server_id='libtv')


def answer_tool():
    return Tool('answer_directly', '作答', '直接回答', {'type': 'object'}, None,
                tool_key='local:answers:direct', effect='read',
                capability='answers')


def catalog():
    return [remote('mcp__libtv__read_project', 'mcp:libtv:read_project'),
            remote('mcp__libtv__read_node', 'mcp:libtv:read_node'),
            remote('mcp__libtv__read_timeline', 'mcp:libtv:read_timeline'),
            remote('mcp__libtv__update_node', 'mcp:libtv:update_node', effect='write'),
            answer_tool()]


OLD_STEPS = [
    {'step_id': 's1', 'tool': 'mcp__libtv__read_project'},
    {'step_id': 's2', 'tool': 'mcp__libtv__read_node'},
    {'step_id': 's3', 'tool': 'mcp__libtv__read_timeline'},
    {'step_id': 's4', 'tool': 'answer_directly'},
]

RESULTS = [
    {'step_id': 's1', 'tool_key': 'mcp:libtv:read_project',
     'execution_status': 'succeeded'},
    {'step_id': 's2', 'tool_key': 'mcp:libtv:read_node',
     'execution_status': 'succeeded'},
    {'step_id': 's3', 'tool_key': 'mcp:libtv:read_timeline',
     'execution_status': 'failed'},
]


def new_plan():
    """修订后的计划只剩「读节点 + 作答」。"""
    return [{'step_id': 'p2_s1', 'tool': 'mcp__libtv__read_node'},
            {'step_id': 'p2_s2', 'tool': 'answer_directly'}]


def blocking(issues):
    return [i for i in issues if i.get('severity') != 'warning']


class CompletedStepDefinitionsTest(unittest.TestCase):

    def test_outputs_snapshot_alone_is_not_enough(self):
        """恢复后 `step_outputs` 是空的，动作/结果账本仍要能认出做过的步骤。"""
        defs = _completed_step_definitions({}, OLD_STEPS, RESULTS)
        self.assertEqual({d['step_id'] for d in defs}, {'s1', 's2'})

    def test_snapshot_and_records_are_unioned(self):
        outputs = {'s1': {'__status__': 'succeeded'},
                   's3': {'__status__': 'failed'}}
        records = [{'step_id': 's2', 'tool_key': 'mcp:libtv:read_node',
                    'status': 'succeeded'}]
        defs = _completed_step_definitions(outputs, OLD_STEPS, records)
        self.assertEqual({d['step_id'] for d in defs}, {'s1', 's2'})

    def test_public_tool_name_is_kept_with_the_definition(self):
        defs = _completed_step_definitions({}, OLD_STEPS, RESULTS)
        names = {d.get('tool') for d in defs}
        self.assertIn('mcp__libtv__read_project', names)


class InheritedCriteriaTest(unittest.TestCase):

    def setUp(self):
        self.completed = _completed_step_definitions({}, OLD_STEPS, RESULTS)

    def test_completed_prior_step_keeps_its_criterion(self):
        criteria = [{'kind': 'tool_succeeded', 'target': 'mcp__libtv__read_project'}]
        kept, issues = check_completion_criteria(
            criteria, new_plan(), catalog(), 'platform_state',
            completed_steps=self.completed, prior_steps=OLD_STEPS)
        self.assertEqual(kept, criteria)
        self.assertEqual(blocking(issues), [])

    def test_dropped_read_step_only_warns(self):
        """用户那次报错的另一半：被放弃的只读步骤不该再阻断。"""
        criteria = [{'kind': 'tool_succeeded', 'target': 'mcp__libtv__read_timeline'},
                    {'kind': 'answer_present', 'target': ''}]
        kept, issues = check_completion_criteria(
            criteria, new_plan(), catalog(), 'platform_state',
            completed_steps=self.completed, prior_steps=OLD_STEPS)
        self.assertEqual([c['kind'] for c in kept], ['answer_present'])
        self.assertEqual(blocking(issues), [])
        self.assertEqual([i['code'] for i in issues], ['criterion_dropped_read_step'])

    def test_dropped_write_step_still_blocks(self):
        """写步骤被放弃也放行，就会把「没做」混成「做完了」。"""
        old = [{'step_id': 's1', 'tool': 'mcp__libtv__update_node'}]
        criteria = [{'kind': 'tool_succeeded', 'target': 'mcp__libtv__update_node'}]
        kept, issues = check_completion_criteria(
            criteria, new_plan(), catalog(), 'platform_change',
            completed_steps=[], prior_steps=old)
        self.assertEqual(kept, [])
        self.assertEqual([i['code'] for i in blocking(issues)],
                         ['criterion_target_unknown'])

    def test_unknown_target_still_blocks(self):
        criteria = [{'kind': 'tool_succeeded', 'target': 'mcp__libtv__delete_node'}]
        kept, issues = check_completion_criteria(
            criteria, new_plan(), catalog(), 'platform_state',
            completed_steps=self.completed, prior_steps=OLD_STEPS)
        self.assertEqual(kept, [])
        self.assertEqual([i['code'] for i in blocking(issues)],
                         ['criterion_target_unknown'])

    def test_without_prior_steps_behaviour_is_unchanged(self):
        """老调用方不传 `prior_steps` 时，判据和以前完全一致。"""
        criteria = [{'kind': 'tool_succeeded', 'target': 'mcp__libtv__read_timeline'}]
        kept, issues = check_completion_criteria(
            criteria, new_plan(), catalog(), 'platform_state',
            completed_steps=self.completed)
        self.assertEqual(kept, [])
        self.assertEqual([i['code'] for i in issues], ['criterion_target_unknown'])

    def test_error_message_never_leaks_colon_identities(self):
        """提示里只出现能照抄的名字，冒号形态是内部身份。"""
        criteria = [{'kind': 'tool_succeeded', 'target': 'mcp__libtv__nope'}]
        _, issues = check_completion_criteria(
            criteria, new_plan(), catalog(), 'platform_state',
            completed_steps=self.completed, prior_steps=OLD_STEPS)
        detail = issues[0]['detail']
        self.assertNotIn('mcp:libtv:', detail)
        self.assertIn('mcp__libtv__read_node', detail)


if __name__ == '__main__':
    unittest.main()
