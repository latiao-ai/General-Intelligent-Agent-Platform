"""存量引擎改挂 / 拒收，以及 Pi 工具循环上的确认边界与完成核对。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class LegacyResumeTests(unittest.TestCase):
    def test_no_side_effects_can_move_to_pi(self):
        from webpage_agent.agent.runtime_identity import legacy_resume_safe
        self.assertTrue(legacy_resume_safe({
            'actions': [], 'steps': [{'status': 'pending'}],
            'snapshot_state': {}, 'waiting_external': 0,
        }))

    def test_actions_or_external_wait_are_not_safe(self):
        from webpage_agent.agent.runtime_identity import legacy_resume_safe
        self.assertFalse(legacy_resume_safe({'actions': [{'id': 'a'}]}))
        self.assertFalse(legacy_resume_safe({'waiting_external': 1}))
        self.assertFalse(legacy_resume_safe({
            'steps': [{'status': 'succeeded'}]}))
        self.assertFalse(legacy_resume_safe({
            'snapshot_state': {'step_outputs': {'s1': {}}}}))


class PrepareExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, actions):
        class Store:
            def __init__(self):
                self.adopted = False

            async def runtime_identity(self, run_id):
                return {'kind': 'langgraph', 'version': 1, 'state_schema': 1}

            async def run(self, run_id):
                return {'run_id': run_id, 'waiting_external': 0, 'status': 'queued'}

            async def actions_of(self, run_id):
                return actions

            async def steps_of(self, run_id):
                return []

            async def run_snapshot(self, run_id):
                return None

            async def adopt_pi_runtime(self, run_id, *, lease_owner, lease_epoch):
                self.adopted = True
                return True

        return Store()

    async def test_safe_legacy_run_is_adopted(self):
        from webpage_agent.agent.runtime_identity import prepare_execution
        store = self._store([])
        kind = await prepare_execution(store, 'r1', lease_owner='w', lease_epoch=1)
        self.assertEqual(kind, 'pi')
        self.assertTrue(store.adopted)

    async def test_legacy_run_with_actions_fails_closed(self):
        from webpage_agent.agent.runtime_identity import RetiredRuntime, prepare_execution
        store = self._store([{'action_id': 'a1'}])
        with self.assertRaises(RetiredRuntime) as caught:
            await prepare_execution(store, 'r1', lease_owner='w', lease_epoch=1)
        self.assertIn('待核实', str(caught.exception))
        self.assertFalse(store.adopted)

    async def test_pi_run_is_unchanged(self):
        from webpage_agent.agent.runtime_identity import prepare_execution
        store = self._store([])

        async def identity(run_id):
            return {'kind': 'pi', 'version': 1, 'state_schema': 2}

        store.runtime_identity = identity
        kind = await prepare_execution(store, 'r1', lease_owner='w', lease_epoch=1)
        self.assertEqual(kind, 'pi')
        self.assertFalse(store.adopted)


class ProductLoopTests(unittest.TestCase):
    def test_confirmation_boundary_blocks_submit_after_propose(self):
        from webpage_agent.agent.pi_product import confirmation_block

        class Server:
            confirmation_tools = ('submit',)
            proposal_tools = ('propose',)

        propose = SimpleNamespace(
            name='propose', tool_key='mcp:canvas:propose', server_id='canvas',
            effect='write', scope='public')
        submit = SimpleNamespace(
            name='submit', tool_key='mcp:canvas:submit', server_id='canvas',
            effect='write', scope='public')
        with patch('webpage_agent.integrations.mcp_servers.by_id', return_value=Server()):
            detail = confirmation_block(
                [{'tool': 'propose', 'step_id': 'c1'}], submit, [propose, submit])
        self.assertIn('确认', detail)

    def test_repeat_stops_after_the_tolerance(self):
        from webpage_agent.agent.pi_product import note_repeat, repeat_block, repeat_key
        counts = {}
        key = repeat_key('web_search', {'query': '同一句'})
        self.assertEqual(repeat_block(counts, key, 2), '')
        note_repeat(counts, key)
        note_repeat(counts, key)
        self.assertIn('没有新进展', repeat_block(counts, key, 2))

    def test_missing_artifact_blocks_completion(self):
        from webpage_agent.agent.pi_product import completion_block
        text = completion_block({
            'answer': '做好了',
            'artifact': {'html_path': '/tmp/giap-missing-artifact.html'},
        })
        self.assertIn('不存在', text)


if __name__ == '__main__':
    unittest.main()
