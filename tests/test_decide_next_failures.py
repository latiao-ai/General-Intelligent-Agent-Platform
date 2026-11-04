"""`decide_next` 的异常分类：预算用尽、失租、真故障必须是三种不同的结果。

为什么值得单独一组：以前这三者被同一个 `except Exception` 收敛成"决策预算用尽"。
对用户是错误的原因（他明明还能补充要求），对运维是假信号（根本没有预算告警，
却看起来像预算问题），而真正的数据库故障连一条带堆栈的日志都没有。所以断言
落在"三种情况各自产生什么 status / failure / 是否向上抛"上，而不是只看不崩。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.agent import nodes
from webpage_agent.governance import execution_control
from webpage_agent.core.errors import DecisionBudgetExceeded


def _store(reserve):
    return SimpleNamespace(
        pending_run_inputs=AsyncMock(return_value=[]),
        run_intent=AsyncMock(return_value={'intent_version': 1}),
        observation_state=AsyncMock(return_value={'observation_seq': 1}),
        reserve_decision=AsyncMock(side_effect=reserve),
        finish_decision=AsyncMock(),
        consume_budget=AsyncMock())


class DecideNextFailureTests(unittest.IsolatedAsyncioTestCase):
    """每种异常一条用例，断言它们的可观察结果互不相同。"""

    async def _call(self, reserve, state=None):
        store = _store(reserve)
        with patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'r1'))), \
                patch.object(nodes.execution_control, 'require', return_value=None):
            return store, await nodes.decide_next(
                state or {'plan': {'version': 1, 'steps': []}, 'user_input': '做个页面'})

    async def test_budget_exhausted_stops_and_waits_for_input(self):
        store, result = await self._call(
            DecisionBudgetExceeded('本次运行已用完 12 次自主决策预算'))
        self.assertEqual(result['status'], 'waiting_input')
        self.assertEqual(result['next_route'], 'stop')
        self.assertIn('用完自主决策预算', result['failure'])
        self.assertIn('12 次', result['failure'])
        # 预算用尽是"停下来等人"，不是失败：不该被记成 planning_failure
        self.assertNotIn('planning_failure', result)
        store.finish_decision.assert_not_awaited()

    async def test_lease_lost_propagates_instead_of_looking_like_budget(self):
        with self.assertRaises(execution_control.LeaseLost):
            await self._call(execution_control.LeaseLost('r1', '持有者已变更'))

    async def test_control_stopped_propagates_instead_of_looking_like_budget(self):
        with self.assertRaises(execution_control.ControlStopped):
            await self._call(execution_control.ControlStopped('已请求暂停', 'paused'))

    async def test_database_failure_keeps_its_own_error_not_budget(self):
        store, result = await self._call(ConnectionError('connection refused'))
        # 数据库故障要落成失败，不能谎报成"预算用尽"
        self.assertEqual(result['status'], 'interrupted')
        self.assertNotIn('决策预算', str(result.get('failure') or ''))
        self.assertEqual(result['planning_failure']['code'],
                         'E_PLANNING_UNAVAILABLE')
        self.assertEqual(result['planning_failure']['stage'], 'observe')
        self.assertEqual(result['planning_failure']['recoverable'], True)
        # 失败要进 state['errors']，否则事后没人知道这一步为什么停
        self.assertEqual(result['errors'][-1]['type'], 'ConnectionError')
        # 决策没预留成功，不能去 finish 一个不存在的 decision
        store.finish_decision.assert_not_awaited()

    async def test_database_failure_honours_the_original_error_code(self):
        class Boom(RuntimeError):
            error_code = 'E_DB_UNAVAILABLE'
            safe_message = '数据库暂时不可用'

        _, result = await self._call(Boom())
        self.assertEqual(result['planning_failure']['code'], 'E_DB_UNAVAILABLE')
        self.assertEqual(result['failure'], '数据库暂时不可用')
        self.assertNotIn('预算', result['failure'])

    async def test_three_outcomes_are_actually_different(self):
        """一句话概括本项：三种异常不再收敛到同一个结果。"""
        _store1, budget = await self._call(DecisionBudgetExceeded('预算用尽'))
        _store2, failure = await self._call(ValueError('意外的编程错误'))
        self.assertNotEqual(budget['status'], failure['status'])
        self.assertNotEqual(budget['failure'], failure['failure'])
        self.assertIn('planning_failure', failure)
        self.assertNotIn('planning_failure', budget)
        with self.assertRaises(execution_control.LeaseLost):
            await self._call(execution_control.LeaseLost('r1'))

    async def test_reserved_decision_still_flows_through_normally(self):
        """回归护栏：正常预留（不抛异常）时 `reserved` 必须照旧可用。

        这一条盯的是"改动别把 `reserved` 弄成未定义"——新增的 except 分支都在
        赋值之前，正常路径必须完全不受影响。
        """
        store = _store(None)
        store.reserve_decision.side_effect = None
        store.reserve_decision.return_value = {'decision_id': 'd1', 'replay': True,
                                               'committed_plan_version': 1}
        with patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'r1'))), \
                patch.object(nodes.execution_control, 'require', return_value=None):
            result = await nodes.decide_next(
                {'plan': {'version': 1, 'steps': []}, 'user_input': '做个页面'})
        self.assertEqual(result['decision_id'], 'd1')
        self.assertEqual(result['next_route'], nodes.observation_module.ROUTE_STEP)
