"""第七阶段：Pi 恢复决策表（按数据库事实）。"""
import unittest

from webpage_agent.agent import pi_recovery
from webpage_agent.agent import pi_snapshot


def _snap(**over):
    snap = pi_snapshot.new_snapshot()
    snap.update(over)
    return snap


class PiRecoveryDecisionTests(unittest.TestCase):
    def test_no_snapshot_is_first_run(self):
        d = pi_recovery.decide(None, [], 'running')
        self.assertEqual(d.kind, pi_recovery.RESUME_ADMIT)
        self.assertEqual(d.pending_calls, [])

    def test_final_answer_committed_run_not_finished(self):
        snap = _snap(final_answer_candidate='答案')
        d = pi_recovery.decide(snap, [], 'running')
        self.assertEqual(d.kind, pi_recovery.RESUME_FINISH)
        self.assertEqual(d.final_answer, '答案')

    def test_final_answer_but_run_already_finished(self):
        snap = _snap(final_answer_candidate='答案')
        d = pi_recovery.decide(snap, [], 'succeeded')
        self.assertEqual(d.kind, pi_recovery.RESUME_ADMIT)

    def test_in_flight_action_needs_reconcile(self):
        snap = _snap()
        actions = [{'action_id': 'a1', 'status': 'in_flight'}]
        d = pi_recovery.decide(snap, actions, 'running')
        self.assertEqual(d.kind, pi_recovery.RESUME_RECONCILE)

    def test_unknown_action_needs_reconcile(self):
        snap = _snap()
        actions = [{'action_id': 'a1', 'status': 'unknown', 'effect': 'write'}]
        d = pi_recovery.decide(snap, actions, 'running')
        self.assertEqual(d.kind, pi_recovery.RESUME_RECONCILE)

    def test_committed_result_not_absorbed_delivers(self):
        snap = _snap(
            committed_tool_results=[
                {'action_id': 'a1', 'step_id': 's1', 'data_ref': 'results/x'}],
            messages=[{'role': 'assistant', 'content': 'x'}])
        d = pi_recovery.decide(snap, [], 'running')
        self.assertEqual(d.kind, pi_recovery.RESUME_DELIVER)
        self.assertEqual(d.tool_results[0]['action_id'], 'a1')

    def test_committed_result_already_absorbed(self):
        snap = _snap(
            committed_tool_results=[
                {'action_id': 'a1', 'step_id': 's1', 'data_ref': 'results/x'}],
            messages=[
                {'role': 'assistant', 'content': 'x'},
                {'role': 'toolResult', 'action_id': 'a1'}])
        d = pi_recovery.decide(snap, [], 'running')
        # 已吸收 → 不是 RESUME_DELIVER；无其他未完成 → resume_admit 继续
        self.assertEqual(d.kind, pi_recovery.RESUME_ADMIT)

    def test_pending_calls_resume_admit(self):
        snap = _snap(pending_calls=[{'call_id': 'c1'}])
        d = pi_recovery.decide(snap, [], 'running')
        self.assertEqual(d.kind, pi_recovery.RESUME_ADMIT)
        self.assertEqual(len(d.pending_calls), 1)

    def test_conservative_block_read_unknown(self):
        self.assertTrue(pi_recovery.conservative_block(
            {'effect': 'read', 'status': 'unknown'}))
        self.assertFalse(pi_recovery.conservative_block(
            {'effect': 'read', 'status': 'failed'}))
        self.assertFalse(pi_recovery.conservative_block(
            {'effect': 'write', 'status': 'unknown'}))


if __name__ == '__main__':
    unittest.main()
