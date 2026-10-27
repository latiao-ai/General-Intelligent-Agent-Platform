"""中台接入边界：真实数据库并发、崩溃窗口、计划合同与主体隔离。"""
import asyncio
import json
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import jwt

from app import (auth_connections, execution_control, idempotency, ledger,
                 nodes, observation, policies, scene_context, tools)
from app.api_hub import Hub
from app.capabilities.registry import Tool
from app.integrations.mcp_servers import Server
from app.pg_store import SessionGoalConflict
from app.schemas import ObservationDecision, RevisedPlan
from tests.exec_env import new_store, reset_db
from tests.test_ticket_auth import ISS, PRIVATE_A, connection


class GoalAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        await self.store.ready()
        self.addAsyncCleanup(self.store.close)

    async def create(self, suffix, *, store=None, session='s1', tenant='ta',
                     user='ua', spec=None):
        return await (store or self.store).create_goal(
            't' + suffix, 'r' + suffix, session, '修改对象', tenant_id=tenant,
            owner_user_id=user, request_spec=spec)

    def spec(self, body='修改对象', key='k1'):
        return {'scope': idempotency.scope_key('ta', 'ua', 'tasks.create', key),
                'client_key': key, 'fingerprint': idempotency.fingerprint(body)}

    async def test_two_processes_cannot_create_two_goals_in_one_session(self):
        other = new_store()
        self.addAsyncCleanup(other.close)
        await other.ready()
        results = await asyncio.gather(self.create('1'), self.create('2', store=other),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(r, SessionGoalConflict) for r in results), 1)
        self.assertEqual(len(await self.store.session_tasks('s1')), 1)
        self.assertEqual(len(await self.store.messages('s1')), 1)

    async def test_all_unfinished_states_block_new_goals(self):
        for index, status in enumerate(('queued', 'running', 'reconciling', 'paused',
                                       'waiting_input', 'waiting_approval',
                                       'needs_reconcile', 'interrupted')):
            session = 'state-' + status
            await self.create(str(index), session=session)
            await self.store.update_run('r' + str(index), status=status)
            with self.subTest(status=status), self.assertRaises(SessionGoalConflict):
                await self.create('blocked-' + str(index), session=session)

    async def test_finished_goal_allows_next_turn(self):
        for index, status in enumerate(('succeeded', 'partially_succeeded', 'failed', 'cancelled')):
            session = 'finished-' + status
            await self.create(str(index), session=session)
            await self.store.update_run('r' + str(index), status=status)
            await self.create('next-' + str(index), session=session)
            self.assertEqual(len(await self.store.session_tasks(session)), 2)

    async def test_concurrent_first_use_cannot_steal_session_ownership(self):
        results = await asyncio.gather(self.create('a'),
                                       self.create('b', tenant='tb', user='ub'),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(r, PermissionError) for r in results), 1)
        self.assertEqual(len(await self.store.messages('s1')), 1)

    async def test_concurrent_idempotency_replays_one_committed_goal(self):
        other = new_store()
        self.addAsyncCleanup(other.close)
        await other.ready()
        a, b = await asyncio.gather(self.create('a', session='sa', spec=self.spec()),
                                    self.create('b', session='sb', store=other, spec=self.spec()))
        self.assertEqual(a['task_id'], b['task_id'])
        self.assertEqual(a['run_id'], b['run_id'])
        self.assertEqual({a['replayed'], b['replayed']}, {False, True})

    async def test_committed_goal_replays_after_process_restart_without_response(self):
        first = await self.create('a', spec=self.spec())
        row = await self.store.request_key(self.spec()['scope'])
        self.assertFalse(row.get('response'))
        self.assertEqual(row['task_id'], first['task_id'])
        other = new_store()
        self.addAsyncCleanup(other.close)
        # No startup recovery is needed to recover the request-to-task association.
        second = await self.create('b', store=other, session='ignored', spec=self.spec())
        self.assertTrue(second['replayed'])
        self.assertEqual(second['task_id'], first['task_id'])
        self.assertEqual(len(await self.store.messages('s1')), 1)

    async def test_creation_failure_rolls_back_key_message_task_and_session(self):
        original = self.store._run
        async def fail_run(conn, sql, args=None):
            if sql.startswith('INSERT INTO runs '):
                raise RuntimeError('simulate crash before commit')
            return await original(conn, sql, args)
        with patch.object(self.store, '_run', fail_run):
            with self.assertRaises(RuntimeError):
                await self.create('bad', spec=self.spec())
        self.assertIsNone(await self.store.task('tbad'))
        self.assertIsNone(await self.store.request_key(self.spec()['scope']))
        self.assertIsNone(await self.store.session('s1'))
        self.assertEqual(await self.store.messages('s1'), [])
        await self.create('good', spec=self.spec())

    async def test_same_key_different_payload_conflicts(self):
        await self.create('a', spec=self.spec())
        with self.assertRaises(idempotency.IdempotencyConflict):
            await self.create('b', spec=self.spec('different'))

    async def test_api_returns_current_goal_and_survives_failure_before_dispatch(self):
        from app import api
        hub = Hub(self.store)
        principal = policies.Principal(tenant_id='ta', user_id='ua')
        api.app.dependency_overrides[policies.current_principal] = lambda: principal
        self.addCleanup(api.app.dependency_overrides.pop, policies.current_principal)
        with patch.object(api, 'hub', hub):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(
                    app=api.app, raise_app_exceptions=False), base_url='http://test') as client:
                with patch.object(hub, '_claim_and_arm', AsyncMock(side_effect=RuntimeError('crash'))):
                    failed = await client.post('/api/tasks', json={'message': '任务', 'session_id': 'api'},
                                               headers={'Idempotency-Key': 'api-key'})
                self.assertEqual(failed.status_code, 500)
                replay = await client.post('/api/tasks', json={'message': '任务', 'session_id': 'api'},
                                           headers={'Idempotency-Key': 'api-key'})
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertTrue(replay.json()['idempotent_replay'])
                conflict = await client.post('/api/tasks', json={'message': '另一个任务', 'session_id': 'api'})
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(conflict.json()['detail']['run_id'], replay.json()['run_id'])
                self.assertEqual(len(await self.store.messages('api')), 1)

    async def test_valid_idempotent_replay_does_not_require_a_fresh_scene_ticket(self):
        from app import api
        hub = Hub(self.store)
        principal = policies.Principal(tenant_id='tenant-a', user_id='local-a',
            authenticated=True, subject='alice', platform_id='pa',
            connection_id=connection().connection_id)
        api.app.dependency_overrides[policies.current_principal] = lambda: principal
        self.addCleanup(api.app.dependency_overrides.pop, policies.current_principal)
        signing = SceneBindingTests()
        body = {'message': '修改对象', 'context': signing.context,
                'context_assertion': signing.sign()}
        with patch.object(api, 'hub', hub), \
                patch.object(auth_connections, 'connections', return_value=[connection()]), \
                patch.object(hub, '_claim_and_arm', AsyncMock(return_value='queued')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                         base_url='http://test') as client:
                first = await client.post('/api/tasks', json=body,
                                          headers={'Idempotency-Key': 'scene-key'})
                self.assertEqual(first.status_code, 200, first.text)
                stamp = int(time.time())
                body['context_assertion'] = signing.sign(iat=stamp - 120, exp=stamp - 90)
                replay = await client.post('/api/tasks', json=body,
                                           headers={'Idempotency-Key': 'scene-key'})
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertTrue(replay.json()['idempotent_replay'])
                self.assertEqual(first.json()['task_id'], replay.json()['task_id'])


class RevisionContractTests(unittest.IsolatedAsyncioTestCase):
    def previous(self):
        return {'version': 1, 'intent_version': 1, 'result_locus': 'platform_change',
                'steps': [{'step_id': 's1', 'tool': 'update'}],
                'completion_criteria': [{'kind': 'tool_succeeded', 'target': 'update',
                                         'detail': '', 'requires_model': False}],
                'after_segment': 'observe', 'observation_goal': '确认平台状态'}

    def catalog(self):
        return [Tool('update', '修改', '测试', {'type': 'object'}, None,
                     tool_key='mcp:comic:update', server_id='comic', effect='write'),
                Tool('answer_directly', '作答', '测试', {'type': 'object'}, None,
                     tool_key='local:answers:direct', effect='read')]

    async def revise(self, proposed):
        old = self.previous()
        store = SimpleNamespace(
            run_intent=AsyncMock(return_value={'intent_version': 2, 'processed_intent_version': 1}),
            pending_run_inputs=AsyncMock(return_value=[{'input_seq': 1, 'message': '改红色', 'intent_version': 2}]),
            actions_of=AsyncMock(return_value=[]), plan=AsyncMock(return_value=old),
            run_snapshot=AsyncMock(return_value={'state': {'plan': old}}))
        with patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'r1'))), \
                patch.object(nodes, '_tool_context', AsyncMock(return_value=None)), \
                patch.object(tools, 'catalog', AsyncMock(return_value=self.catalog())), \
                patch.object(nodes, 'structured_call', AsyncMock(return_value=(proposed, {}))):
            return await nodes.revise_plan({'user_input': '修改平台对象', 'plan': old, 'intent_version': 2})

    async def test_user_revision_cannot_downgrade_platform_change_to_chat(self):
        result = await self.revise(RevisedPlan(
            steps=[{'tool': 'answer_directly', 'step_id': 'p2_s1'}], result_locus='chat'))
        self.assertEqual(result['status'], 'waiting_input')
        self.assertIn('result_locus_changed', result['failure'])
        self.assertNotIn('plan', result)

    async def test_revision_preserves_criteria_and_observation_policy(self):
        result = await self.revise(RevisedPlan(steps=[{'tool': 'update', 'step_id': 'p2_s1'}]))
        plan = result['plan']
        self.assertEqual(plan['completion_criteria'], self.previous()['completion_criteria'])
        self.assertEqual(plan['after_segment'], 'observe')
        self.assertEqual(plan['observation_goal'], '确认平台状态')
        checked = observation.completion_state(plan['completion_criteria'], state={}, results=[])
        self.assertFalse(checked['ok'], '没有真实修改回执不能通过验收')

    async def test_explicit_new_criteria_are_saved_without_erasing_old_criteria(self):
        proposed = RevisedPlan(steps=[{'tool': 'update', 'step_id': 'p2_s1'}],
            after_segment='observe', observation_goal='检查新状态',
            completion_criteria=[{'kind': 'answer_present'}])
        plan = (await self.revise(proposed))['plan']
        self.assertEqual(len(plan['completion_criteria']), 2)
        self.assertEqual(plan['observation_goal'], '检查新状态')

    async def test_observation_revision_uses_same_contract(self):
        old = self.previous()
        for locus, tool, expected in [('chat', 'answer_directly', 'waiting_input'),
                                       ('platform_change', 'update', None)]:
            decision = ObservationDecision(kind='revise', result_locus=locus,
                steps=[{'tool': tool, 'step_id': 'p2_s1'}], after_segment='observe',
                observation_goal='继续核对', completion_criteria=[{'kind': 'tool_succeeded', 'target': 'update'}])
            store = SimpleNamespace(pending_run_inputs=AsyncMock(return_value=[]),
                run_intent=AsyncMock(return_value={'intent_version': 1}),
                observation_state=AsyncMock(return_value={'observation_seq': 1}),
                reserve_decision=AsyncMock(return_value={'decision_id': 'd1', 'created': True}),
                consume_budget=AsyncMock(),
                finish_decision=AsyncMock())
            with patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'r1'))), \
                    patch.object(execution_control, 'require', return_value=None), \
                    patch.object(nodes, '_tool_context', AsyncMock(return_value=None)), \
                    patch.object(tools, 'catalog', AsyncMock(return_value=self.catalog())), \
                    patch.object(nodes, 'structured_call', AsyncMock(return_value=(decision, {}))):
                result = await nodes.decide_next({'plan': old, 'intent_version': 1})
            if expected:
                self.assertEqual(result.get('status'), expected)
            else:
                self.assertTrue(result['plan']['completion_criteria'])
                self.assertEqual(result['plan']['after_segment'], 'observe')

    def test_invalid_observation_policy_is_rejected(self):
        _, issues = nodes.revision_metadata({'after_segment': 'observe', 'observation_goal': ''}, {})
        self.assertEqual(issues[0]['code'], 'bad_segment_policy')

    def test_replaced_step_criterion_follows_equivalent_new_step(self):
        previous = self.previous()
        previous['completion_criteria'] = [{'kind': 'tool_succeeded', 'target': 's1'}]
        updated, issues = nodes.revision_metadata({
            'steps': [{'tool': 'update', 'step_id': 'p2_s1'}], 'superseded': ['s1']}, previous)
        self.assertFalse(issues)
        self.assertEqual(updated['completion_criteria'][0]['target'], 'p2_s1')
        self.assertEqual(previous['completion_criteria'][0]['target'], 's1')

    async def test_finish_proposal_cannot_erase_unmet_original_criterion(self):
        result = await nodes.verify_completion({'plan': self.previous(),
            'criteria_proposed': [{'kind': 'answer_present'}], 'answer': '已处理'})
        self.assertFalse(result['criteria_state']['ok'])
        self.assertEqual(result['criteria_state']['unmet'][0]['target'], 'update')

    async def test_intake_restores_full_plan_for_execution_and_user_revision(self):
        old = self.previous()
        store = SimpleNamespace(
            run_intent=AsyncMock(return_value={'intent_version': 1, 'plan_version': 1}),
            pending_run_inputs=AsyncMock(return_value=[]), plan=AsyncMock(return_value=old),
            run_snapshot=AsyncMock(return_value={'state': {'plan': old}}),
            actions_of=AsyncMock(return_value=[]),
            observation_state=AsyncMock(return_value={}))
        with patch.object(nodes, '_store_and_run', AsyncMock(return_value=(store, 'r1'))), \
                patch.object(nodes, '_load_scene', AsyncMock(return_value={})):
            resumed = await nodes.intake({'user_input': '修改对象'})
            self.assertEqual(resumed['plan']['completion_criteria'], old['completion_criteria'])
            self.assertEqual(resumed['plan']['after_segment'], 'observe')
            store.pending_run_inputs.return_value = [{'input_seq': 1}]
            revised = await nodes.intake({'user_input': '修改对象'})
            self.assertEqual(revised['entry_route'], 'revise_plan')
            self.assertEqual(revised['plan']['completion_criteria'], old['completion_criteria'])


class SceneBindingTests(unittest.TestCase):
    context = {'project_id': 'p1'}

    def sign(self, *, algorithm='RS256', drop=(), **overrides):
        stamp = int(time.time())
        claims = {'iss': ISS, 'aud': scene_context.SCENE_AUDIENCE,
                  'sub': 'alice', 'tenant_id': 'tenant-a', 'platform_id': 'pa',
                  'ctx': scene_context.canonical_hash(self.context), 'iat': stamp, 'exp': stamp + 30}
        claims.update(overrides)
        for key in drop:
            claims.pop(key, None)
        return jwt.encode(claims, PRIVATE_A, algorithm=algorithm,
                          headers={'typ': scene_context.SCENE_TYPE, 'kid': 'k1'})

    def verify(self, token, **overrides):
        expected = {'tenant_id': 'tenant-a', 'subject': 'alice',
                    'platform_id': 'pa', 'connection_id': connection().connection_id}
        expected.update(overrides)
        with patch.object(auth_connections, 'connections', return_value=[connection()]):
            return scene_context.verify(token, self.context, **expected)

    def test_valid_scene_is_bound_to_login_subject(self):
        self.assertTrue(self.verify(self.sign())['verified'])

    def test_wrong_user_platform_or_connection_is_rejected(self):
        for values in ({'subject': 'bob'}, {'platform_id': 'pb'}, {'connection_id': 'other'}):
            with self.subTest(values=values), self.assertRaises(scene_context.SceneError):
                self.verify(self.sign(), **values)

    def test_algorithm_is_not_selected_by_token(self):
        with self.assertRaises(scene_context.SceneError):
            self.verify(self.sign(algorithm='RS512'))

    def test_required_time_and_identity_fields(self):
        for field in ('iat', 'exp', 'sub', 'platform_id', 'ctx'):
            with self.subTest(field=field), self.assertRaises(scene_context.SceneError):
                self.verify(self.sign(drop=(field,)))

    def test_overlong_and_invalid_lifetime_are_rejected(self):
        stamp = int(time.time())
        for values in ({'exp': stamp + 600}, {'exp': stamp}, {'iat': True}):
            with self.subTest(values=values), self.assertRaises(scene_context.SceneError):
                self.verify(self.sign(**values))

    def test_api_checks_scene_against_external_subject_not_local_user_id(self):
        from app import api
        principal = policies.Principal(tenant_id='tenant-a', user_id='u_internal',
            authenticated=True, subject='alice', platform_id='pa',
            connection_id=connection().connection_id)
        with patch.object(api, 'settings', SimpleNamespace(auth_mode='ticket')), \
                patch.object(auth_connections, 'connections', return_value=[connection()]):
            snapshot = api._scene_for(api.TaskRequest(message='修改对象', context=self.context,
                                                       context_assertion=self.sign()), principal)
        self.assertTrue(snapshot['verified'])


class DiscoveryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_binding_scope_contains_subject_and_revision_without_secrets(self):
        from app import mcp_connections, credentials
        context = execution_control.ExecutionContext('run-a', lease_epoch=1)
        store = SimpleNamespace(run=AsyncMock(return_value={
            'tenant_id': 'tenant-a', 'owner_user_id': 'alice'}))
        book = ledger.ActionLedger(store, 'run-a', 1, context=context)
        binding = credentials.CredentialBinding(
            binding_id='key-a', tenant_id='tenant-a', subject_kind='user',
            subject_id='alice', server_binding_id='server-a',
            credential_ref='env:PRIVATE_KEY', scope={'tools': ['read']}, revision=2)
        with ledger.use_ledger(book), execution_control.use(context), \
                patch.object(mcp_connections, 'binding_for_server', AsyncMock(return_value=binding)):
            result = await mcp_connections.discovery_scope([Server('comic')])
            self.assertEqual(result[:2], ('tenant-a', 'alice'))
            self.assertEqual(result[2][0][2], 2)
            self.assertNotIn('PRIVATE_KEY', repr(result))
            store.run.return_value = None
            with self.assertRaises(credentials.CredentialMissing):
                await mcp_connections.discovery_scope([Server('comic')])

    async def test_run_scopes_and_rotation_do_not_share_directory_or_inner_names(self):
        server = Server('comic', url='https://example.invalid/mcp', capability_tool='list_capabilities')
        store = object()
        first = ledger.ActionLedger(store, 'ra', 1,
            context=execution_control.ExecutionContext('ra', lease_epoch=1))
        second = ledger.ActionLedger(store, 'rb', 1,
            context=execution_control.ExecutionContext('rb', lease_epoch=1))
        active = {'name': 'alice', 'revision': 1, 'token': 'alice-token'}
        async def scope(servers):
            return 'tenant', active['name'], active['revision']
        async def token(server):
            return active['token']
        async def discover(server):
            return [{'name': active['name'], 'description': active['name'],
                     'inputSchema': {'type': 'object'}, 'effect_type': 'read'}], ''
        async def capabilities(server):
            return {active['name']: {'status': 'AVAILABLE'}}
        tools._SCOPED_MCP_CACHES.clear()
        self.addCleanup(tools._SCOPED_MCP_CACHES.clear)
        with patch.object(tools, 'settings', replace(tools.settings, tool_policy_enforced=True)), \
                patch.object(tools.mcp_servers, 'servers', return_value=[server]), \
                patch('app.mcp_connections.discovery_scope', scope), \
                patch.object(tools, '_subject_token', token), \
                patch.object(tools, '_discover', AsyncMock(side_effect=discover)) as calls, \
                patch.object(tools, '_discover_capabilities', capabilities):
            with ledger.use_ledger(first):
                a = await tools.mcp_tools()
                await tools.mcp_tools()
                self.assertEqual(calls.await_count, 1)
            active.update(name='bob', token='bob-token')
            with ledger.use_ledger(second):
                b = await tools.mcp_tools()
                self.assertEqual(set(tools.known_inner_names('comic')), {'bob'})
            with ledger.use_ledger(first):
                self.assertEqual(set(tools.known_inner_names('comic')), {'alice'})
                active.update(name='alice', token='alice-token', revision=2)
                await tools.mcp_tools()
                self.assertEqual(calls.await_count, 3, '凭据版本变化即使密钥相同也必须重新发现')
                self.assertNotEqual(a[0].name, b[0].name)
                with patch.object(tools, '_subject_token', AsyncMock(side_effect=RuntimeError('revoked'))):
                    with self.assertRaises(RuntimeError):
                        await tools.mcp_tools()
                self.assertEqual(tools.known_inner_names('comic'), {})
            self.assertNotIn('alice-token', repr(tools._SCOPED_MCP_CACHES))
