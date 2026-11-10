"""多用户隔离的缺口：身份源、私有目录、文件工具、产物和模型选择。"""
import os
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from webpage_agent.api import auth
from webpage_agent.capabilities import files as file_tools
from webpage_agent.core import model_choice, sandbox
from webpage_agent.core.config import settings
from webpage_agent.governance import policies
from webpage_agent.tools import tool_results


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.alice = self.root / 'alice'
        self.bob = self.root / 'bob'
        self.alice.mkdir()
        self.bob.mkdir()
        self.outside = self.root / 'outside.txt'
        self.outside.write_text('outside-secret', encoding='utf-8')
        (self.alice / 'note.txt').write_text('alice-secret', encoding='utf-8')
        link = self.alice / 'escape'
        link.symlink_to(self.outside)
        nested = self.alice / 'sub'
        nested.mkdir()
        (nested / 'leap').symlink_to(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _state(self, folder: Path) -> dict:
        return {'workspace_dir': str(folder)}

    async def test_read_write_list_stay_inside(self):
        written = await file_tools.write_file(
            {'path': 'nested/new.txt', 'text': 'hello'}, self._state(self.alice))
        self.assertEqual(written['written'], ['nested/new.txt'])
        listed = await file_tools.list_dir({'path': '.'}, self._state(self.alice))
        names = {item['name'] for item in listed['entries']}
        self.assertIn('note.txt', names)
        got = await file_tools.read_file({'path': 'note.txt'}, self._state(self.alice))
        self.assertEqual(got['text'], 'alice-secret')
        with self.assertRaises(file_tools.ToolError):
            await file_tools.read_file({'path': 'note.txt'}, self._state(self.bob))

    async def test_traversal_absolute_and_symlink_are_rejected(self):
        attacks = [
            '../outside.txt',
            'sub/leap/outside.txt',
            'escape',
            str(self.outside),
            '/etc/passwd',
        ]
        for path in attacks:
            with self.subTest(path=path):
                with self.assertRaises(file_tools.ToolError):
                    await file_tools.read_file({'path': path}, self._state(self.alice))
                with self.assertRaises(file_tools.ToolError):
                    await file_tools.write_file(
                        {'path': path, 'text': 'nope'}, self._state(self.alice))
        self.assertEqual(self.outside.read_text(encoding='utf-8'), 'outside-secret')

    async def test_two_users_cannot_read_each_others_files(self):
        await file_tools.write_file(
            {'path': 'secret.txt', 'text': 'only-alice'}, self._state(self.alice))
        await file_tools.write_file(
            {'path': 'secret.txt', 'text': 'only-bob'}, self._state(self.bob))
        alice = await file_tools.read_file({'path': 'secret.txt'}, self._state(self.alice))
        bob = await file_tools.read_file({'path': 'secret.txt'}, self._state(self.bob))
        self.assertEqual(alice['text'], 'only-alice')
        self.assertEqual(bob['text'], 'only-bob')
        with self.assertRaises(file_tools.ToolError):
            await file_tools.read_file(
                {'path': str(self.alice / 'secret.txt')}, self._state(self.bob))

    def test_tool_results_are_separated_by_user(self):
        captured = tool_results.CapturedResult(
            structured={'secret': 'alice'},
            parts=[tool_results.ResultPart(
                ordinal=0, kind='json', content_type='application/json',
                text='{"secret":"alice"}')])
        alice = tool_results.stage_result(
            self.root, captured, tenant_id='acme', owner_user_id='alice',
            result_id='res_same')
        captured.parts[0].text = '{"secret":"bob"}'
        captured.structured = {'secret': 'bob'}
        tool_results.stage_result(
            self.root, captured, tenant_id='acme', owner_user_id='bob',
            result_id='res_same')
        alice_dir = tool_results.result_dir(self.root, 'acme', 'res_same', 'alice')
        bob_dir = tool_results.result_dir(self.root, 'acme', 'res_same', 'bob')
        self.assertNotEqual(alice_dir, bob_dir)
        seen = tool_results.read_structured(self.root, {
            'tenant_id': 'acme', 'owner_user_id': 'alice', 'result_id': 'res_same',
            'raw_ref': alice['raw_ref']})
        self.assertEqual(seen.get('secret'), 'alice')
        other = tool_results.read_structured(self.root, {
            'tenant_id': 'acme', 'owner_user_id': 'bob', 'result_id': 'res_same',
            'raw_ref': alice['raw_ref']})
        self.assertEqual(other.get('secret'), 'bob')
        import shutil
        shutil.rmtree(bob_dir)
        with self.assertRaises(tool_results.ResultError):
            tool_results.read_structured(self.root, {
                'tenant_id': 'acme', 'owner_user_id': 'bob', 'result_id': 'res_same',
                'raw_ref': alice['raw_ref']})


class ModelChoiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.profile = root / 'USER.md'
        self._patch = patch.object(
            model_choice, 'settings',
            replace(settings, profile_path=self.profile, llm_model='process-default'))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_users_do_not_share_a_model_choice(self):
        model_choice.save('alice', 'model-a')
        model_choice.save('bob', 'model-b')
        self.assertEqual(model_choice.load_id('alice'), 'model-a')
        self.assertEqual(model_choice.load_id('bob'), 'model-b')
        self.assertEqual(model_choice.settings.llm_model, 'process-default')
        token = model_choice.bind('alice', 'process-default', {'model-a', 'model-b'})
        try:
            self.assertEqual(model_choice.active('process-default'), 'model-a')
        finally:
            model_choice.reset(token)
        self.assertEqual(model_choice.active('process-default'), 'process-default')
        self.assertFalse((self.profile.parent / 'model.json').exists())


class PiCwdTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_pins_cwd_to_the_workspace(self):
        from webpage_agent.agent.pi_bridge import PiBridge, PiBridgeError
        from webpage_agent.agent.pi_runtime import PiRuntime

        class Stop(PiBridge):
            async def start(self):
                raise PiBridgeError('stop')

        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / 'space'
            folder.mkdir()
            outside = Path(raw) / 'else'
            outside.mkdir()
            link = folder / 'out'
            # 工作目录本身若是符号链接，不能拿来当子进程 cwd。
            linked = Path(raw) / 'linked'
            linked.symlink_to(outside, target_is_directory=True)
            bridge = Stop(node_script=Path('unused'))
            runtime = PiRuntime(bridge=bridge, tools=[])
            with self.assertRaises(PiBridgeError):
                await runtime.run({'workspace_dir': str(folder), 'user_input': 'hi',
                                   'history': []})
            self.assertEqual(bridge.cwd, str(folder.resolve()))
            bridge.cwd = None
            with self.assertRaises(PiBridgeError):
                await runtime.run({'workspace_dir': str(linked), 'user_input': 'hi',
                                   'history': []})
            self.assertIsNone(bridge.cwd)
            self.assertFalse(link.exists())


@unittest.skipUnless(os.getenv('PG_TEST_DSN'), '需要 PG_TEST_DSN')
class PlatformLoginIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.test_auth_exchange import new_store, reset_db
        import webpage_agent.api.routes as routes
        import webpage_agent.api.models_api as models_api

        self.routes = routes
        self.store = new_store()
        await reset_db(self.store)
        async with self.store._lease() as conn:
            await conn.execute('DELETE FROM user_workspaces')
        self.root = Path(tempfile.mkdtemp())
        tuned = replace(
            settings, auth_mode='ticket', auth_cookie_secure=False,
            policy_legacy_visible=False, api_rate_limit_enabled=False,
            workspace_root=self.root / 'workspaces',
            artifact_dir=self.root / 'artifacts',
            profile_path=self.root / 'profile' / 'USER.md')
        self.patches = []
        for module in (routes, auth, policies, sandbox, model_choice, models_api):
            self.patches.append(patch.object(module, 'settings', tuned))
        self.patches.append(patch.dict(os.environ, {
            'BEEFTV_API_BASE': 'http://127.0.0.1:9/api',
            'AVAILABLE_MODELS': 'model-a,model-b',
            'PLATFORM_LOGIN_TENANT': 'acme',
            'PLATFORM_LOGIN_IDP': 'beeftv',
        }))
        saved = routes.hub
        routes.hub = type(saved)(self.store)
        self.saved_hub = saved
        self.saved_state = getattr(routes.app.state, 'store', None)
        routes.app.state.store = self.store
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=routes.app), base_url='http://test')

    async def asyncTearDown(self):
        close = getattr(self, 'client', None)
        if close is not None:
            await close.aclose()
        for item in reversed(getattr(self, 'patches', [])):
            item.stop()
        if getattr(self, 'saved_hub', None) is not None:
            self.routes.hub = self.saved_hub
        if hasattr(self, 'saved_state'):
            self.routes.app.state.store = self.saved_state
        if getattr(self, 'store', None) is not None:
            await self.store.close()
        import shutil
        if getattr(self, 'root', None) is not None:
            shutil.rmtree(self.root, ignore_errors=True)

    async def test_platform_login_binds_once_and_hides_password(self):
        # 修正 FakeClient：post 从测试实例的 users 读。
        accounts = {}

        class FakeResponse:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body

            def json(self):
                return self._body

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json=None):
                Client.seen = dict(json or {})
                user = accounts.get((json or {}).get('username'))
                if not user:
                    return FakeResponse(401, {})
                return FakeResponse(200, {'data': {'user': user}})

        accounts['ada'] = {'id': '42', 'username': 'ada', 'status': 'active', 'role': 'member'}
        accounts['bea'] = {'id': '42', 'username': 'bea', 'status': 'active', 'role': 'member'}
        with patch.object(self.routes.httpx, 'AsyncClient', Client):
            first = await self.client.post(
                '/api/auth/platform-login',
                json={'username': 'ada', 'password': 's3cret-not-stored'})
            self.assertEqual(first.status_code, 200, first.text)
            again = await self.client.post(
                '/api/auth/platform-login',
                json={'username': 'ada', 'password': 's3cret-not-stored'})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(first.json()['user_id'], again.json()['user_id'])
        self.assertTrue(first.json()['first_login'])
        self.assertFalse(again.json()['first_login'])
        self.assertEqual(Client.seen['password'], 's3cret-not-stored')
        async with self.store._lease() as conn:
            blob = await self.store._all(conn, '''
                SELECT u.display_name, u.email, i.subject, i.email AS iemail, s.subject AS ssub
                FROM app_users u
                JOIN identities i ON i.user_id=u.user_id
                JOIN auth_sessions s ON s.user_id=u.user_id
            ''')
        self.assertNotIn('s3cret-not-stored', str([dict(row) for row in blob]))

        other = await self.store.login_platform_identity(
            platform_id='libtv', provider='libtv', subject='42',
            identity_provider_id='libtv', tenant_id='elsewhere',
            session_id='as_' + uuid.uuid4().hex, session_digest='d' + uuid.uuid4().hex,
            session_expires_at=time.time() + 60)
        self.assertNotEqual(other['user_id'], first.json()['user_id'])
        same = await self.store.login_platform_identity(
            platform_id='beeftv', provider='beeftv', subject='42',
            identity_provider_id='beeftv', tenant_id='acme',
            session_id='as_' + uuid.uuid4().hex, session_digest='d' + uuid.uuid4().hex,
            session_expires_at=time.time() + 60)
        self.assertEqual(same['user_id'], first.json()['user_id'])
        self.assertFalse(same['created'])

        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE identities SET role='operator' WHERE user_id=%s",
                (first.json()['user_id'],))
        listed = await self.client.get('/api/admin/users')
        self.assertEqual(listed.status_code, 200, listed.text)
        ids = [item['user_id'] for item in listed.json()['users']]
        self.assertIn(first.json()['user_id'], ids)
        self.assertNotIn(other['user_id'], ids)

        async with self.store._lease() as conn:
            await conn.execute(
                "UPDATE app_users SET status='disabled' WHERE user_id=%s",
                (first.json()['user_id'],))
        with patch.object(self.routes.httpx, 'AsyncClient', Client):
            blocked = await self.client.post(
                '/api/auth/platform-login',
                json={'username': 'ada', 'password': 's3cret-not-stored'})
        self.assertEqual(blocked.status_code, 403, blocked.text)

    async def test_workspace_namespace_and_artifact_do_not_leak(self):
        accounts = {
            'ada': {'id': '42', 'username': 'ada', 'status': 'active', 'role': 'member'},
            'bea': {'id': '99', 'username': 'bea', 'status': 'active', 'role': 'member'},
        }

        class FakeResponse:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body

            def json(self):
                return self._body

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json=None):
                user = accounts.get((json or {}).get('username'))
                if not user:
                    return FakeResponse(401, {})
                return FakeResponse(200, {'data': {'user': user}})

        ada = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.routes.app),
                                base_url='http://test')
        bea = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.routes.app),
                                base_url='http://test')
        try:
            with patch.object(self.routes.httpx, 'AsyncClient', Client):
                self.assertEqual((await ada.post(
                    '/api/auth/platform-login',
                    json={'username': 'ada', 'password': 'pw-a'})).status_code, 200)
                self.assertEqual((await bea.post(
                    '/api/auth/platform-login',
                    json={'username': 'bea', 'password': 'pw-b'})).status_code, 200)
            mine = await ada.get('/api/workspace')
            theirs = await bea.get('/api/workspace')
            self.assertEqual(mine.status_code, 200, mine.text)
            self.assertNotEqual(mine.json()['path'], theirs.json()['path'])
            self.assertTrue(str(mine.json()['path']).startswith(str(self.root)))
            denied = await ada.post('/api/workspace', json={'path': '/tmp'})
            self.assertEqual(denied.status_code, 403)
            created = await ada.post('/api/workspace/namespace', json={'name': '项目甲'})
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()['name'], '项目甲')
            self.assertTrue(created.json()['path'].startswith(mine.json()['private_root']))
            self.assertEqual((Path(created.json()['path']) / '.space-name').read_text(
                encoding='utf-8').strip(), '项目甲')
            stolen = await bea.post('/api/workspace/select', json={'name': '项目甲'})
            self.assertEqual(stolen.status_code, 404)
            self.assertNotEqual((await bea.get('/api/workspace')).json()['path'],
                                created.json()['path'])

            ada_id = (await ada.get('/api/me')).json()['user_id']
            bea_id = (await bea.get('/api/me')).json()['user_id']
            task_id, run_id = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
            artifact_id = uuid.uuid4().hex
            await self.store.create_goal(
                task_id, run_id, 'sess' + ada_id[-6:], 'hello',
                tenant_id='acme', owner_user_id=ada_id)
            folder = sandbox.task_dir(artifact_id, 'acme', ada_id)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / 'index.html'
            target.write_text('ada-artifact', encoding='utf-8')
            await self.store.update_task(
                task_id, status='succeeded', artifact_id=artifact_id,
                state={'artifact': {'html_path': str(target), 'kind': 'sources'}})
            own = await ada.get(f'/api/tasks/{task_id}/artifact')
            self.assertEqual(own.status_code, 200, own.text)
            self.assertIn('ada-artifact', own.text)
            other = await bea.get(f'/api/tasks/{task_id}/artifact')
            self.assertEqual(other.status_code, 404)
            bob_file = sandbox.task_dir(artifact_id, 'acme', bea_id) / 'index.html'
            bob_file.parent.mkdir(parents=True, exist_ok=True)
            bob_file.write_text('bea-artifact', encoding='utf-8')
            await self.store.update_task(
                task_id, state={'artifact': {'html_path': str(bob_file)}})
            leaked = await ada.get(f'/api/tasks/{task_id}/artifact')
            self.assertEqual(leaked.status_code, 404)

            picked = await ada.post('/api/models/select', json={'id': 'model-a'})
            self.assertEqual(picked.status_code, 200, picked.text)
            other_model = await bea.post('/api/models/select', json={'id': 'model-b'})
            self.assertEqual(other_model.status_code, 200, other_model.text)
            self.assertEqual((await ada.get('/api/models')).json()['current'], 'model-a')
            self.assertEqual((await bea.get('/api/models')).json()['current'], 'model-b')
            self.assertNotEqual(settings.llm_model, 'model-a')
        finally:
            await ada.aclose()
            await bea.aclose()


if __name__ == '__main__':
    unittest.main()
