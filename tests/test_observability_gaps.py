"""降级路径的可观测性：吞掉异常可以，但必须留下证据。

分组理由：这些 `except Exception` 都是**有意**的降级（读不到现场就按没有现场
规划、读不到控制请求就放行），重构它们会把一次数据库抖动放大成整次运行失败。
真正的问题是"吞干净"——日志里没有堆栈、state 里没有记录，于是出问题时既不能
从界面看出来，也不能从日志追溯。这里逐条盯"有没有留痕"。

同时覆盖两项相邻的小修：`zip()` 加 `strict=`（长度不等不再静默截断），
以及观察层那处循环内闭包的显式绑定。
"""
import unittest
from unittest.mock import AsyncMock, patch

from webpage_agent.agent import nodes
from webpage_agent.observe import observation


class SilentDegradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scene_load_failure_is_logged(self):
        store = AsyncMock()
        store.run = AsyncMock(side_effect=ConnectionError('db gone'))
        with self.assertLogs('webpage_agent.agent.nodes', level='ERROR') as logs:
            scene = await nodes._load_scene(store, 'r1')
        self.assertEqual(scene, {})                      # 降级保留
        self.assertIn('读取 run 失败', '\n'.join(logs.output))
        # 必须带堆栈：只有一行 warning 等于换个地方"看不见"
        self.assertTrue(any(record.exc_info for record in logs.records))

    async def test_scene_context_failure_is_logged(self):
        store = AsyncMock()
        store.run = AsyncMock(return_value={'task_id': 't1'})
        store.scene_context = AsyncMock(side_effect=RuntimeError('boom'))
        with self.assertLogs('webpage_agent.agent.nodes', level='ERROR') as logs:
            self.assertEqual(await nodes._load_scene(store, 'r1'), {})
        self.assertTrue(any(r.exc_info for r in logs.records))

    async def test_missing_scene_support_is_not_an_error(self):
        """仓储没有 scene_context（旧实现）：这是能力缺失，不是故障，不该报错。"""
        store = AsyncMock(spec=['run'])
        store.run = AsyncMock(return_value={'task_id': 't1'})
        self.assertEqual(await nodes._load_scene(store, 'r1'), {})

    async def test_observation_records_control_read_failure_in_state(self):
        """读不到控制状态时必须写进 state['errors']，否则事后无法解释路由。"""
        book = AsyncMock()
        book.store.control_state = AsyncMock(side_effect=ConnectionError('db gone'))
        book.store.actions_of = AsyncMock(return_value=[])
        with patch.object(nodes.ledger_module, 'current_ledger', return_value=book), \
                patch.object(nodes.execution_control, 'require',
                             return_value=AsyncMock(run_id='r1')), \
                self.assertLogs('webpage_agent.agent.nodes', level='ERROR'):
            result = await nodes.observe_result({'plan': {'steps': [], 'version': 1},
                                                 'step_index': 0})
        errors = result.get('errors') or []
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['stage'], 'observe')
        self.assertEqual(errors[0]['type'], 'ConnectionError')

    async def test_boundary_records_control_read_failure_in_state(self):
        """安全边界读不到控制请求：放行，但必须留下"可能忽略了取消"的记录。"""
        book = AsyncMock()
        book.store.control_state = AsyncMock(side_effect=ConnectionError('db gone'))
        state = {'plan': {'steps': [], 'version': 1}, 'errors': []}
        with patch.object(nodes.ledger_module, 'current_ledger', return_value=book), \
                patch.object(nodes.execution_control, 'require',
                             return_value=AsyncMock(run_id='r1')), \
                self.assertLogs('webpage_agent.agent.nodes', level='ERROR'):
            result = await nodes._check_boundary(state)
        self.assertEqual(result, '')                     # 仍然放行
        self.assertEqual(state['errors'][-1]['stage'], 'boundary')
        self.assertIn('取消/暂停可能被忽略', state['errors'][-1]['message'])


class ZipStrictnessTests(unittest.TestCase):
    """长度不等不再静默截断——这些位置两边的长度是**推导出来必然相等**的。"""

    def test_alignment_mismatch_fails_loudly_instead_of_truncating(self):
        """对齐结果比原步骤短时，必须炸而不是"只比前面几个就通过"。

        通过 patch 掉 `_align_repair_steps` 制造"对齐逻辑返回了短列表"这种坏状态
        （正常路径不会产生它），再看 `_prepared_reference_steps` 是静默放过还是炸。
        """
        plan_steps = [{'step_id': 's1', 'tool': 'a', 'args': {}},
                      {'step_id': 's2', 'tool': 'b', 'args': {}}]
        state = {
            'plan': {'steps': plan_steps, 'version': 1},
            'errors': [{'error_code': 'E_INPUT_REFERENCE', 'step_id': 's2',
                        'resolved': False}],
            'step_outputs': {},
        }
        with patch.object(nodes, '_align_repair_steps',
                          return_value=[{'step_id': 's1', 'tool': 'a', 'args': {}}]):
            with self.assertRaises(ValueError):
                nodes._prepared_reference_steps(state, [])

    def test_binding_identity_check_rejects_a_shorter_candidate(self):
        self.assertFalse(nodes._same_binding_arguments([{'literal': 1}], []))

    def test_same_length_pairs_still_work(self):
        self.assertTrue(nodes._same_binding_arguments(
            [{'literal': 1}, {'literal': 2}], [{'literal': 1}, {'literal': 2}]))


class ClosureBindingTests(unittest.TestCase):
    """`observation._matches` 的循环变量改用默认参数绑定，行为必须完全不变。"""

    def _state(self, target: str, results: list) -> dict:
        return observation.completion_state(
            [{'kind': 'tool_succeeded', 'target': target}],
            state={}, results=results)

    def test_tool_succeeded_matches_only_its_own_target(self):
        results = [{'step_id': 's1', 'tool_key': 'local:web:search',
                    'tool_name': 'web_search', 'execution_status': 'succeeded'}]
        ok = self._state('web_search', results)
        self.assertEqual(len(ok['verified']), 1)
        miss = self._state('other_tool', results)
        self.assertEqual(len(miss['unmet']), 1)

    def test_each_criterion_uses_its_own_target(self):
        """两个条件、两个不同 target：各自只看自己那个（绑定没串味）。"""
        results = [{'step_id': 's1', 'tool_key': 'local:web:search',
                    'tool_name': 'web_search', 'execution_status': 'succeeded'}]
        outcome = observation.completion_state(
            [{'kind': 'tool_succeeded', 'target': 'web_search'},
             {'kind': 'tool_succeeded', 'target': 'missing_tool'}],
            state={}, results=results)
        self.assertEqual(len(outcome['verified']), 1)
        self.assertEqual(len(outcome['unmet']), 1)
        self.assertEqual(outcome['unmet'][0]['target'], 'missing_tool')
