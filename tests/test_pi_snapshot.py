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

    def test_parse_row_extracts_state_from_db_wrapper(self):
        """parse_row 从 run_snapshot() 的 DB 行结构（state + schema + 列）解析。"""
        inner = pi_snapshot.new_snapshot(phase='awaiting_tools', checkpoint_seq=5)
        row = {
            'run_id': 'r1',
            'snapshot_schema': pi_snapshot.current_schema(),
            'state_json': '{}',
            'state': inner,
            'schema': pi_snapshot.current_schema(),
        }
        parsed = pi_snapshot.parse_row(row)
        self.assertEqual(parsed['phase'], 'awaiting_tools')
        self.assertEqual(parsed['checkpoint_seq'], 5)

    def test_parse_row_missing_state_raises(self):
        """行存在但 state 为空 = 损坏，不能退回空快照。"""
        row = {'run_id': 'r1', 'state': None, 'schema': pi_snapshot.current_schema()}
        with self.assertRaises(pi_snapshot.SnapshotError):
            pi_snapshot.parse_row(row)

    def test_parse_row_none_raises(self):
        with self.assertRaises(pi_snapshot.SnapshotError):
            pi_snapshot.parse_row(None)

    def test_parse_row_incompatible_schema_raises(self):
        inner = pi_snapshot.new_snapshot()
        inner['state_schema'] = 888
        row = {'run_id': 'r1', 'state': inner, 'schema': 888}
        with self.assertRaises(runtime_identity.IncompatibleRuntimeVersion):
            pi_snapshot.parse_row(row)

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


class CheckpointProtocolTests(unittest.TestCase):
    """第七阶段：checkpoint 消息校验（Node → Python 的版本化上下文导出）。"""

    def _checkpoint(self, **over):
        from webpage_agent.agent import pi_protocol as proto
        msg = {
            'protocol_version': proto.PROTOCOL_VERSION,
            'execution_id': 'exec-1',
            'type': 'checkpoint',
            'checkpoint_seq': 1,
            'messages': [{'role': 'assistant', 'content': 'x'}],
            'pending_calls': [{'call_id': 'c1', 'tool_name': 'web_search'}],
        }
        msg.update(over)
        return msg

    def test_valid_checkpoint_passes(self):
        from webpage_agent.agent import pi_protocol as proto
        proto.validate_checkpoint(self._checkpoint(), 'exec-1')

    def test_checkpoint_requires_seq(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_checkpoint(self._checkpoint(checkpoint_seq='x'), 'exec-1')

    def test_checkpoint_rejects_non_list_messages(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_checkpoint(self._checkpoint(messages='nope'), 'exec-1')

    def test_checkpoint_ack_requires_ok(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_checkpoint_ack({
                'protocol_version': proto.PROTOCOL_VERSION,
                'execution_id': 'exec-1', 'type': 'checkpoint_ack',
                'checkpoint_seq': 1}, 'exec-1')


if __name__ == '__main__':
    unittest.main()
