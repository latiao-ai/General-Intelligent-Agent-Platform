"""Shared chat subscriptions must skip the previous segment's terminal event."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from webpage_agent.api.api_hub import Hub


class WorkspaceTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_timeline_exposes_resume_cursor_without_dropping_history(self):
        events = [(4, {'type': 'status', 'run_status': 'interrupted'}),
                  (5, {'type': 'action', 'resume_after_event_id': 4}),
                  (6, {'type': 'stage_start', 'stage': 'plan'})]
        store = SimpleNamespace(
            session_tasks=AsyncMock(return_value=[{'task_id': 't', 'status': 'running',
                'state': {}, 'message': 'continue', 'created_at': 'now'}]),
            task_meta=AsyncMock(return_value={'current_run_id': 'r'}),
            events=AsyncMock(return_value=events), messages=AsyncMock(return_value=[]))
        hub = Hub(store=store)
        timeline = await hub.session_timeline('s')
        self.assertEqual(timeline['live_task_id'], 't')
        self.assertEqual(timeline['live_after_event_id'], 4)
        self.assertEqual(timeline['turns'][0]['events'], [event for _, event in events])
        store.events.return_value = [(1, {'type': 'stage_start'})]
        self.assertEqual((await hub.session_timeline('s'))['live_after_event_id'], 0)

    async def test_resume_marks_boundary_before_execution_and_keeps_uncertainty_guard(self):
        store = SimpleNamespace(
            run=AsyncMock(return_value={'run_id': 'r', 'task_id': 't', 'status': 'interrupted'}),
            plan=AsyncMock(return_value={}), actions_of=AsyncMock(return_value=[]),
            update_run=AsyncMock(), events=AsyncMock(return_value=[(9, {'type': 'status'})]))
        hub = Hub(store=store)
        record = SimpleNamespace(live=False, task_id='t', publish=AsyncMock())
        hub.get = AsyncMock(return_value=record)
        hub._has_uncertain_actions = AsyncMock(return_value=False)
        hub._claim_and_arm = AsyncMock(return_value='started')
        self.assertIs(await hub.resume('r'), record)
        self.assertEqual(record.publish.await_args.args[0]['resume_after_event_id'], 9)
        record.publish.reset_mock()
        hub._has_uncertain_actions.return_value = True
        self.assertIsNone(await hub.resume('r'))
        record.publish.assert_not_awaited()
