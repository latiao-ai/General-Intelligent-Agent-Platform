"""第七阶段：版本化 Pi 快照的严格读写。"""
import unittest

from webpage_agent.agent import pi_snapshot
from webpage_agent.agent import runtime_identity


class PiSnapshotTests(unittest.TestCase):
    def test_new_snapshot_has_required_fields(self):
        snap = pi_snapshot.new_snapshot(user_input='hi')
        self.assertEqual(snap['state_schema'], pi_snapshot.current_schema())
        self.assertEqual(snap['phase'], 'started')
        self.assertEqual(snap['checkpoint_seq'], 0)
        self.assertEqual(snap['messages'], [])
        self.assertEqual(snap['logical_call_seq'], 0)
        self.assertEqual(snap['pending_calls'], [])
        self.assertEqual(snap['committed_tool_results'], [])
        self.assertIsNone(snap['final_answer_candidate'])
        self.assertEqual(snap['user_input'], 'hi')

    def test_serialize_strips_non_snapshot_keys(self):
        """权限/租约/宿主字段不进快照。"""
        state = pi_snapshot.new_snapshot()
        state['lease_owner'] = 'hacker'
        state['api_key'] = 'secret'
        state['phase'] = 'running'
        cleaned = pi_snapshot.serialize(state)
        self.assertNotIn('lease_owner', cleaned)
        self.assertNotIn('api_key', cleaned)
        self.assertEqual(cleaned['phase'], 'running')

    def test_parse_roundtrip(self):
        state = pi_snapshot.new_snapshot(phase='awaiting_tools', checkpoint_seq=3)
        state['messages'] = [
            {'role': 'assistant', 'content': 'x'},
            {'role': 'toolCall', 'content': 'call'},
            {'role': 'toolResult', 'content': 'result'},
        ]
        parsed = pi_snapshot.parse(pi_snapshot.serialize(state))
        self.assertEqual(parsed['phase'], 'awaiting_tools')
        self.assertEqual(parsed['checkpoint_seq'], 3)
        self.assertEqual(len(parsed['messages']), 3)

    def test_parse_missing_raises(self):
        with self.assertRaises(pi_snapshot.SnapshotError):
            pi_snapshot.parse(None)

    def test_parse_corrupt_raises(self):
        with self.assertRaises(pi_snapshot.SnapshotError):
            pi_snapshot.parse('not a dict')

    def test_parse_incompatible_schema_raises(self):
        state = pi_snapshot.new_snapshot()
        state['state_schema'] = 999
        with self.assertRaises(runtime_identity.IncompatibleRuntimeVersion):
            pi_snapshot.parse(state)

    def test_messages_ok_requires_pairing_roles(self):
        self.assertTrue(pi_snapshot.messages_ok([
            {'role': 'assistant', 'content': 'a'},
            {'role': 'toolCall', 'content': 'c'},
            {'role': 'toolResult', 'content': 'r'},
        ]))
        self.assertFalse(pi_snapshot.messages_ok([
            {'role': 'bogus', 'content': 'x'},
        ]))
        self.assertFalse(pi_snapshot.messages_ok('nope'))


if __name__ == '__main__':
    unittest.main()
