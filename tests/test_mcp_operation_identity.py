import unittest

from webpage_agent.capabilities.registry import Tool
from webpage_agent.integrations.mcp_servers import Server
from webpage_agent.store.ledger import ActionLedger
from webpage_agent.tools import core as tools
from tests.test_mcp_rejected_reasons import is_error
from webpage_agent.integrations import mcp_adapter


class OperationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tool = Tool('create', 'create', '', {
            'type': 'object', 'properties': {'canvasId': {'type': 'string'},
                                           'operationId': {'type': 'string'}},
            'required': ['canvasId', 'operationId']}, None,
            tool_key='mcp:platform:create', effect='write',
            idempotency_arg='operationId')
        self.args = {'canvasId': 'canvas-a', 'operationId': 'model-fixed-id'}

    def bind(self, run='run-a', version=1, step='s2', args=None):
        return tools.execution_arguments(
            self.tool, self.args if args is None else args, step,
            ActionLedger(None, run, version))

    def test_different_runs_do_not_collide_even_when_model_reuses_id(self):
        self.assertNotEqual(self.bind()['operationId'],
                            self.bind(run='run-b')['operationId'])

    def test_retry_keeps_id_and_business_payload(self):
        first = self.bind()
        self.assertEqual(first, self.bind(args=first))
        self.assertEqual(first['canvasId'], self.args['canvasId'])
        self.assertEqual(self.args['operationId'], 'model-fixed-id')

    def test_changed_payload_step_or_version_gets_new_identity(self):
        first = self.bind()['operationId']
        for changed in (self.bind(args={**self.args, 'canvasId': 'canvas-b'}),
                        self.bind(step='s3'), self.bind(version=2)):
            self.assertNotEqual(first, changed['operationId'])

    def test_model_cannot_change_retry_identity(self):
        self.assertEqual(self.bind(), self.bind(args={**self.args, 'operationId': 'another'}))

    def test_planning_schema_hides_runtime_identity_without_mutating_transport(self):
        schema = tools.planning_schema(self.tool)
        self.assertNotIn('operationId', schema['properties'])
        self.assertNotIn('operationId', schema['required'])
        self.assertIn('operationId', self.tool.args['required'])

    def test_missing_ledger_fails_before_remote_dispatch(self):
        with self.assertRaises(RuntimeError):
            tools.execution_arguments(self.tool, self.args, 's2', None)

    def test_configured_collision_is_confirmed_rejection(self):
        server = Server('platform', deterministic_reasons=(
            'operation_id_reused_with_different_payload',))
        with self.assertRaises(mcp_adapter.MCPToolFailure) as caught:
            mcp_adapter.result_payload(is_error('operation_id_reused_with_different_payload'), server)
        self.assertEqual(mcp_adapter.certainty_of_error(caught.exception), 'failed_confirmed')
