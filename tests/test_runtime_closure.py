"""治理闭环里不依赖数据库的合同：批准匹配、异步状态、路径、启动检查。"""
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from app import approvals, artifact_store, async_contracts, external_tasks
from app import runtime_check, tool_authorization
from app.config import governance_enabled, settings


@contextmanager
def assigned(target, **fields):
    previous = {key: getattr(target, key) for key in fields}
    for key, value in fields.items():
        object.__setattr__(target, key, value)
    try:
        yield
    finally:
        for key, value in previous.items():
            object.__setattr__(target, key, value)


VIDEO = async_contracts.AsyncContract(
    contract_id='video-job-v1', version=1,
    submit_tool_key='mcp:video:create_video',
    remote_id_pointer='/job/id',
    status_tool_key='mcp:video:get_job',
    status_id_argument='job_id',
    status_pointer='/job/status',
    status_map={'queued': 'accepted', 'processing': 'running',
                'done': 'succeeded', 'error': 'failed'},
    output_pointer='/job/outputs', recoverable=True)


class AsyncContractTests(unittest.TestCase):
    def test_unknown_status_is_not_success(self):
        self.assertEqual(async_contracts.map_status('mystery', VIDEO), 'unknown')
        self.assertEqual(async_contracts.map_status('done', VIDEO), 'succeeded')

    def test_remote_id_comes_from_the_pointer(self):
        self.assertEqual(
            async_contracts.remote_id_of({'job': {'id': 'job-9'}}, VIDEO), 'job-9')
        self.assertEqual(async_contracts.remote_id_of({'job': {}}, VIDEO), '')

    def test_acceptance_marks_an_open_job(self):
        with assigned(settings, async_tool_runtime_enabled=True):
            original = async_contracts.load
            async_contracts.load = lambda: [VIDEO]
            try:
                marked = async_contracts.acceptance(
                    'mcp:video:create_video',
                    {'job': {'id': 'job-9', 'status': 'queued'}})
            finally:
                async_contracts.load = original
        self.assertTrue(marked['async_accepted'])
        self.assertEqual(marked['remote_task_id'], 'job-9')

    def test_finished_submit_is_not_treated_as_waiting(self):
        with assigned(settings, async_tool_runtime_enabled=True):
            original = async_contracts.load
            async_contracts.load = lambda: [VIDEO]
            try:
                marked = async_contracts.acceptance(
                    'mcp:video:create_video',
                    {'job': {'id': 'job-9', 'status': 'done'}})
            finally:
                async_contracts.load = original
        self.assertEqual(marked, {})


class OutcomeTests(unittest.TestCase):
    def test_unknown_outcome_does_not_become_success(self):
        state = external_tasks.apply_outcome(
            {'tool_results': [{'action_id': 'a1', 'phase': 'accepted',
                               'execution_status': 'accepted'}]},
            action_id='a1', remote_id='job-9', mapped='needs_reconcile',
            payload={'job': {'status': '???'}})
        row = state['tool_results'][0]
        self.assertEqual(row['execution_status'], 'unknown')
        self.assertNotEqual(row['phase'], 'completed')

    def test_success_patches_only_the_matching_action(self):
        state = external_tasks.apply_outcome(
            {'tool_results': [
                {'action_id': 'a1', 'phase': 'accepted'},
                {'action_id': 'a2', 'phase': 'accepted'}]},
            action_id='a1', remote_id='', mapped='succeeded', payload={})
        self.assertEqual(state['tool_results'][0]['phase'], 'completed')
        self.assertEqual(state['tool_results'][1]['phase'], 'accepted')


class ApprovalMatchTests(unittest.TestCase):
    def test_hash_covers_max_actions(self):
        left = approvals.ApprovalManifest(items=[{'key': 'call'}], max_actions=1)
        right = approvals.ApprovalManifest(items=[{'key': 'call'}], max_actions=2)
        self.assertNotEqual(left.hash(), right.hash())

    def test_changed_args_do_not_match(self):
        args_hash = 'abc'
        row = {
            'approval_id': 'apr_1', 'status': 'approved', 'expires_at': 0,
            'manifest_json': '{"items":[{"key":"call","tool_key":"mcp:comic:update_scene",'
                             '"args_hash":"abc"}]}',
        }
        matched = tool_authorization.usable_approval(
            row, tool_key='mcp:comic:update_scene', args_hash=args_hash,
            now_ts=time.time())
        missed = tool_authorization.usable_approval(
            row, tool_key='mcp:comic:update_scene', args_hash='other',
            now_ts=time.time())
        self.assertEqual(matched['item_key'], 'call')
        self.assertIsNone(missed)


class PathAndStartupTests(unittest.TestCase):
    def test_artifact_path_cannot_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertTrue(artifact_store.contained(folder, 'a/b.txt').is_absolute())
            with self.assertRaises(artifact_store.PathDenied):
                artifact_store.contained(folder, '../outside')

    def test_gc_keeps_registered_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            kept = root / '_tool_results' / 'default' / 'res_keep'
            orphan = root / '_tool_results' / 'default' / 'res_old'
            kept.mkdir(parents=True)
            orphan.mkdir(parents=True)
            (kept / 'manifest.json').write_text('{}', encoding='utf-8')
            old = time.time() - 10_000
            import os
            os.utime(orphan, (old, old))
            removed = artifact_store.gc_unregistered(root, grace_seconds=3600, now_ts=time.time())
            self.assertEqual(removed, 1)
            self.assertTrue(kept.is_dir())
            self.assertFalse(orphan.exists())

    def test_ticket_mode_requires_the_three_switches(self):
        with assigned(settings, auth_mode='ticket', auth_dev_compat=False,
                      tool_policy_enforced=False, result_envelope_v2=False,
                      async_tool_runtime_enabled=False):
            found = runtime_check.issues()
        self.assertEqual(len(found), 3)

    def test_ticket_mode_enables_governance_without_a_dev_escape(self):
        self.assertTrue(governance_enabled('ticket', False, False))
        self.assertTrue(governance_enabled('ticket', False, True))
        self.assertFalse(governance_enabled('ticket', True, False))
        self.assertFalse(governance_enabled('off', False, False))
        self.assertTrue(governance_enabled('off', False, True))

    def test_ticket_mode_starts_when_the_three_switches_are_on(self):
        with tempfile.TemporaryDirectory() as folder, assigned(
                settings, auth_mode='ticket', auth_dev_compat=False,
                tool_policy_enforced=True, result_envelope_v2=True,
                async_tool_runtime_enabled=True, artifact_dir=Path(folder)):
            self.assertEqual(runtime_check.issues(), [])

    def test_off_mode_does_not_require_the_switches(self):
        with assigned(settings, auth_mode='off'):
            self.assertEqual(runtime_check.issues(), [])

    def test_browser_origin_is_rejected_when_unset_in_ticket_mode(self):
        with assigned(settings, auth_mode='ticket', auth_dev_compat=False,
                      auth_allowed_origins=()):
            self.assertTrue(runtime_check.origin_rejected(
                'POST', 'https://evil.example'))
            self.assertFalse(runtime_check.origin_rejected('POST', ''))
            self.assertFalse(runtime_check.origin_rejected(
                'GET', 'https://evil.example'))


class _MemoryStore:
    def __init__(self):
        self.task = {
            'external_task_id': 'et1', 'remote_task_id': 'job-9',
            'tenant_id': 'default', 'owner_user_id': 'default',
            'contract_id': 'video-job-v1', 'contract_version': 1,
            'run_id': 'run1', 'action_id': 'a1', 'state': 'accepted',
            'version': 1, 'deadline_at': time.time() + 1000, 'poll_epoch': 0,
        }
        self.run_row = {'status': 'waiting_input', 'run_id': 'run1'}
        self.snapshot = {
            'plan_version': 1, 'next_step_id': '',
            'state': {'tool_results': [{
                'action_id': 'a1', 'phase': 'accepted',
                'remote_task_id': 'job-9', 'execution_status': 'accepted'}]},
        }

    async def due_external_tasks(self, now_ts=None, limit=10):
        return [dict(self.task)]

    async def claim_external_task_poll(self, external_task_id, owner, lease_seconds=60):
        self.task['poll_epoch'] = int(self.task['poll_epoch']) + 1
        return dict(self.task)

    async def finish_external_poll(self, external_task_id, epoch, **fields):
        if int(epoch) != int(self.task['poll_epoch']):
            return None
        self.task.update(fields)
        return dict(self.task)

    async def run(self, run_id):
        return dict(self.run_row)

    async def run_snapshot(self, run_id):
        return {'plan_version': self.snapshot['plan_version'],
                'next_step_id': self.snapshot['next_step_id'],
                'state': self.snapshot['state']}

    async def save_snapshot(self, run_id, *, plan_version, state, next_step_id=''):
        self.snapshot['state'] = state

    async def external_tasks_of_run(self, run_id):
        return [dict(self.task)]

    async def update_run(self, run_id, **fields):
        self.run_row.update(fields)


class PollTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_status_is_retried_and_not_woken_as_success(self):
        store = _MemoryStore()

        async def ask(task, contract):
            return {'job': {'status': 'mystery'}}

        with tempfile.TemporaryDirectory() as folder, \
                assigned(settings, async_tool_runtime_enabled=True,
                         artifact_dir=Path(folder)):
            original = async_contracts.load
            async_contracts.load = lambda: [VIDEO]
            try:
                count = await external_tasks.poll_due(
                    store, worker_id='w', querier=ask)
            finally:
                async_contracts.load = original
        self.assertEqual(count, 1)
        self.assertEqual(store.task['state'], 'running')
        self.assertEqual(store.task['error_code'], 'unknown_status')
        self.assertEqual(store.run_row['status'], 'waiting_input')

    async def test_success_wakes_the_waiting_run_without_a_new_submit(self):
        store = _MemoryStore()
        submits = {'n': 0}

        async def ask(task, contract):
            submits['n'] += 1
            self.assertEqual(contract.status_tool_key, 'mcp:video:get_job')
            self.assertEqual(task['remote_task_id'], 'job-9')
            return {'job': {'status': 'done', 'outputs': ['clip']}}

        with tempfile.TemporaryDirectory() as folder, \
                assigned(settings, async_tool_runtime_enabled=True,
                         artifact_dir=Path(folder)):
            original = async_contracts.load
            async_contracts.load = lambda: [VIDEO]
            try:
                await external_tasks.poll_due(store, worker_id='w', querier=ask)
            finally:
                async_contracts.load = original
        self.assertEqual(submits['n'], 1)
        self.assertEqual(store.task['state'], 'completed')
        self.assertEqual(store.run_row['status'], 'queued')
        self.assertEqual(store.snapshot['state']['tool_results'][0]['phase'],
                         'completed')


class ContractFileTests(unittest.TestCase):
    def test_missing_file_loads_nothing(self):
        with assigned(settings,
                      async_contracts_file='/tmp/does-not-exist-async-contracts.json'):
            self.assertEqual(async_contracts.load(), [])
            self.assertEqual(async_contracts.summary(),
                             {'loaded': 0, 'recoverable': 0})

    def test_contract_without_status_query_is_not_recoverable(self):
        import json
        body = {'contracts': [{
            'contract_id': 'half',
            'submit_tool_key': 'mcp:video:create_video',
            'remote_id_pointer': '/job/id',
            'recoverable': True,
        }]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'async_tools.json'
            path.write_text(json.dumps(body), encoding='utf-8')
            with assigned(settings, async_contracts_file=str(path),
                          async_tool_runtime_enabled=True):
                found = async_contracts.load()
                self.assertEqual(len(found), 1)
                self.assertFalse(found[0].recoverable)
                self.assertEqual(async_contracts.summary()['recoverable'], 0)
                self.assertEqual(async_contracts.acceptance(
                    'mcp:video:create_video',
                    {'job': {'id': 'job-9', 'status': 'queued'}}), {})

    def test_example_file_is_recoverable_only_when_selected(self):
        example = Path(__file__).resolve().parents[1] / 'contracts' / 'async_tools.example.json'
        with assigned(settings, async_contracts_file=str(example)):
            rows = async_contracts.load()
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].recoverable)
            self.assertEqual(async_contracts.summary(),
                             {'loaded': 1, 'recoverable': 1})


class ConcurrencyCapTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_run_stays_queued_until_a_slot_frees(self):
        import asyncio
        from app.api_hub import Hub, TaskRecord

        class Store:
            def __init__(self):
                self.runs = {}

            async def claim_run(self, worker_id, lease_seconds=60,
                                run_id=None, now_ts=None):
                row = self.runs.get(run_id)
                if row is None or row.get('lease_owner'):
                    return None
                if row.get('status') not in ('queued', 'running', 'reconciling'):
                    return None
                row['status'] = 'running'
                row['lease_owner'] = worker_id
                row['lease_epoch'] = int(row.get('lease_epoch') or 0) + 1
                return dict(row)

            async def update_run(self, run_id, **fields):
                self.runs[run_id].update(fields)

        store = Store()
        hub = Hub(store, owner_id='api-test', lease_seconds=30)
        release = asyncio.Event()
        started = []

        async def execute(claimed):
            started.append(claimed['run_id'])
            await release.wait()

        hub.execute_claimed_run = execute

        def make(task_id, run_id):
            item = TaskRecord(task_id=task_id, session_id='s', message='m',
                              store=store)
            item.run_id = run_id
            store.runs[run_id] = {
                'run_id': run_id, 'task_id': task_id, 'status': 'queued',
                'lease_epoch': 0, 'lease_owner': None,
            }
            hub.tasks[task_id] = item
            return item

        first = make('t1', 'r1')
        second = make('t2', 'r2')
        with assigned(settings, max_concurrent_runs=1):
            self.assertEqual(await hub._claim_and_arm(
                first, 'r1', lambda claimed: hub.execute_claimed_run(claimed),
                wait=False), 'started')
            for _ in range(50):
                if 'r1' in started:
                    break
                await asyncio.sleep(0.01)
            self.assertIn('r1', started)
            self.assertEqual(await hub._claim_and_arm(
                second, 'r2', lambda claimed: hub.execute_claimed_run(claimed),
                wait=False), 'queued')
            self.assertIsNone(store.runs['r2'].get('lease_owner'))
            self.assertEqual(store.runs['r2']['status'], 'queued')
            release.set()
            for _ in range(50):
                if 'r2' in started:
                    break
                await asyncio.sleep(0.01)
            self.assertIn('r2', started)
            self.assertEqual(store.runs['r2']['status'], 'running')


if __name__ == '__main__':
    unittest.main()
