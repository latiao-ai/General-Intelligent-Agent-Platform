"""结果读取的对象级隔离。

同一企业的另一个用户、另一家企业、以及只落了盘还没登记的临时文件，
都不能凭 result_id 读到内容。本人可以读。分块以登记表为准，不看磁盘清单。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

import app.api as api_module
from app import execution_control, ledger, policies
from app import tool_results
from app import tools as tool_module
from app.config import settings


def person(user_id: str, tenant_id: str) -> policies.Principal:
    return policies.Principal(tenant_id=tenant_id, user_id=user_id,
                              role='member', authenticated=True)


class MemoryResults:
    def __init__(self):
        self.manifests: dict = {}
        self.parts: dict = {}

    async def result_manifest(self, result_id: str):
        row = self.manifests.get(result_id)
        return dict(row) if row else None

    async def result_parts(self, result_id: str):
        return [dict(item) for item in self.parts.get(result_id, [])]


def remember(store: MemoryResults, staged: dict, *, tenant_id: str,
             owner_user_id: str) -> None:
    result_id = staged['result_id']
    store.manifests[result_id] = {
        'result_id': result_id,
        'tenant_id': tenant_id,
        'owner_user_id': owner_user_id,
        'raw_ref': staged.get('raw_ref') or '',
        'structured_ref': staged.get('raw_ref') or '',
    }
    store.parts[result_id] = [
        dict(part, result_id=result_id) for part in staged.get('parts') or []]


def capture(payload: dict, *, tenant_id: str, result_id: str,
            root: Path) -> dict:
    captured = tool_results.CapturedResult(
        structured=payload,
        parts=[tool_results.ResultPart(
            ordinal=0, kind='json', content_type='application/json',
            text=json.dumps(payload, ensure_ascii=False))])
    return tool_results.stage_result(
        root, captured, tenant_id=tenant_id, result_id=result_id)


class ResultIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryResults()
        self.saved_store = api_module.hub.store
        api_module.hub.store = self.store
        self.holder = {'principal': person('alice', 'acme')}
        api_module.app.dependency_overrides[policies.current_principal] = (
            lambda: self.holder['principal'])
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url='http://test')
        self.saved = {
            'artifact_dir': settings.artifact_dir,
            'policy_legacy_visible': settings.policy_legacy_visible,
        }
        object.__setattr__(settings, 'artifact_dir', self.root)
        object.__setattr__(settings, 'policy_legacy_visible', False)
        self.staged = capture(
            {'note': 'owner-only-secret'}, tenant_id='acme',
            result_id='res_owner', root=self.root)
        remember(self.store, self.staged, tenant_id='acme', owner_user_id='alice')

    async def asyncTearDown(self):
        await self.client.aclose()
        api_module.hub.store = self.saved_store
        api_module.app.dependency_overrides.pop(policies.current_principal, None)
        for key, value in self.saved.items():
            object.__setattr__(settings, key, value)
        self.tmp.cleanup()

    def _as(self, user_id: str, tenant_id: str) -> None:
        self.holder['principal'] = person(user_id, tenant_id)

    async def test_owner_can_read_result_and_part(self):
        result = await self.client.get('/api/results/res_owner')
        part = await self.client.get('/api/results/res_owner/parts/res_owner-p0')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['structured']['note'], 'owner-only-secret')
        self.assertEqual(part.status_code, 200, part.text)
        self.assertIn('owner-only-secret', part.json()['text'])

    async def test_same_tenant_and_other_tenant_get_the_same_404(self):
        missing = await self.client.get('/api/results/res_missing')
        missing_part = await self.client.get(
            '/api/results/res_missing/parts/res_missing-p0')

        self._as('bob', 'acme')
        same = await self.client.get('/api/results/res_owner')
        same_part = await self.client.get('/api/results/res_owner/parts/res_owner-p0')

        self._as('cara', 'other')
        other = await self.client.get('/api/results/res_owner')
        other_part = await self.client.get(
            '/api/results/res_owner/parts/res_owner-p0')

        for response in (missing, missing_part, same, same_part, other, other_part):
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('owner-only-secret', response.text)
        self.assertEqual(same.json(), missing.json())
        self.assertEqual(other.json(), missing.json())
        self.assertEqual(same_part.json(), missing_part.json())
        self.assertEqual(other_part.json(), missing_part.json())
        self.assertEqual(missing.json()['detail'], '结果不存在')

    async def test_unregistered_file_is_not_readable(self):
        capture({'note': 'temp-secret'}, tenant_id='acme',
                result_id='res_temp', root=self.root)
        response = await self.client.get('/api/results/res_temp')
        part = await self.client.get('/api/results/res_temp/parts/res_temp-p0')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(part.status_code, 404)
        self.assertNotIn('temp-secret', response.text)
        self.assertNotIn('temp-secret', part.text)
        self.assertEqual(response.json()['detail'], '结果不存在')

    async def test_part_only_on_disk_manifest_is_404(self):
        folder = tool_results.result_dir(self.root, 'acme', 'res_owner')
        decoy = folder / 'part-009.json'
        decoy.write_text('disk-only-secret', encoding='utf-8')
        manifest_path = folder / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['parts'].append({
            'part_id': 'res_owner-p9',
            'storage_key': tool_results.relative_result_file(
                'acme', 'res_owner', 'part-009.json'),
        })
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

        response = await self.client.get('/api/results/res_owner/parts/res_owner-p9')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['detail'], '分块不存在')
        self.assertNotIn('disk-only-secret', response.text)
        kept = await self.client.get('/api/results/res_owner/parts/res_owner-p0')
        self.assertEqual(kept.status_code, 200, kept.text)

    async def test_storage_key_cannot_escape_into_another_result(self):
        other = capture({'note': 'other-secret'}, tenant_id='other',
                        result_id='res_other', root=self.root)
        self.store.parts['res_owner'][0]['storage_key'] = other['parts'][0]['storage_key']
        response = await self.client.get('/api/results/res_owner/parts/res_owner-p0')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['detail'], '分块不存在')
        self.assertNotIn('other-secret', response.text)

    async def test_legacy_directory_remains_readable_by_its_owner(self):
        legacy = tool_results.results_root(self.root) / 'acme' / 'res_legacy'
        legacy.mkdir(parents=True)
        (legacy / 'structured.json').write_text(
            json.dumps({'note': 'legacy-body'}), encoding='utf-8')
        self.store.manifests['res_legacy'] = {
            'result_id': 'res_legacy',
            'tenant_id': 'acme',
            'owner_user_id': 'alice',
            'raw_ref': '_tool_results/acme/res_legacy/structured.json',
            'structured_ref': '',
        }
        own = await self.client.get('/api/results/res_legacy')
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()['structured']['note'], 'legacy-body')

        self._as('bob', 'acme')
        denied = await self.client.get('/api/results/res_legacy')
        self.assertEqual(denied.status_code, 404)
        self.assertNotIn('legacy-body', denied.text)

    def test_sanitized_tenant_names_no_longer_share_a_directory(self):
        slash = tool_results.result_dir(self.root, 'acme/corp', 'res_same')
        dash = tool_results.result_dir(self.root, 'acme-corp', 'res_same')
        prefix_a = tool_results.result_dir(self.root, 'a' * 40 + 'X', 'res_same')
        prefix_b = tool_results.result_dir(self.root, 'a' * 40 + 'Y', 'res_same')
        self.assertNotEqual(slash, dash)
        self.assertNotEqual(prefix_a, prefix_b)
        staged = capture({'note': 'hashed'}, tenant_id='acme/corp',
                         result_id='res_same', root=self.root)
        self.assertTrue((slash / 'structured.json').is_file())
        self.assertFalse((tool_results.results_root(self.root) / 'acme-corp').exists())
        self.assertIn(tool_results.tenant_segment('acme/corp'), staged['raw_ref'])


class ResultRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_result_registers_the_run_owner(self):
        class Store:
            def __init__(self):
                self.context = None
                self.staged = None

            async def run(self, run_id):
                return {'run_id': run_id, 'tenant_id': 'acme/corp',
                        'owner_user_id': 'user-1'}

            async def commit_tool_result(self, context, staged, envelope):
                self.context = context
                self.staged = staged
                return staged['result_id']

        store = Store()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            previous = settings.artifact_dir
            object.__setattr__(settings, 'artifact_dir', root)
            try:
                with ledger.use_ledger(SimpleNamespace(store=store)), \
                        execution_control.use(SimpleNamespace(run_id='run-1')):
                    result_id = await tool_module._stage_result(
                        {'note': 'registered-secret'}, tool_key='mcp:demo:search')
                self.assertTrue(result_id)
                self.assertEqual(store.context['tenant_id'], 'acme/corp')
                self.assertEqual(store.context['owner_user_id'], 'user-1')
                self.assertEqual(store.context['run_id'], 'run-1')
                folder_path = tool_results.result_dir(root, 'acme/corp', result_id)
                self.assertTrue((folder_path / 'structured.json').is_file())
                self.assertFalse(
                    (tool_results.results_root(root) / 'acme-corp').exists())
            finally:
                object.__setattr__(settings, 'artifact_dir', previous)

    async def test_stage_result_without_registration_returns_no_id(self):
        with ledger.use_ledger(None), execution_control.use(None):
            self.assertEqual(await tool_module._stage_result({'note': 'x'}), '')

        class Store:
            async def run(self, run_id):
                return {'run_id': run_id, 'tenant_id': 'acme',
                        'owner_user_id': 'user-1'}

            async def commit_tool_result(self, context, staged, envelope):
                raise RuntimeError('db down')

        with tempfile.TemporaryDirectory() as folder, \
                ledger.use_ledger(SimpleNamespace(store=Store())), \
                execution_control.use(SimpleNamespace(run_id='run-1')):
            previous = settings.artifact_dir
            object.__setattr__(settings, 'artifact_dir', Path(folder))
            try:
                result_id = await tool_module._stage_result({'note': 'x'})
            finally:
                object.__setattr__(settings, 'artifact_dir', previous)
        self.assertEqual(result_id, '')


if __name__ == '__main__':
    unittest.main()
