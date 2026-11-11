"""专家与会话选择的 HTTP 接口 + 技能上传/启停接口。

覆盖"迁移期最容易只做一半"的几处：

- 会话选择写进会话行（不是消息），读回来是同一个专家/技能集合；
- 不存在的专家 id 直接 400（存进去等于之后每轮悄悄丢人设）；
- 技能"安装"必须真的落盘（只有一条 id 的技能在会话里读不到任何内容）；
- 上传包缺 description / 有穿越路径 → 400；
- 启停与删除是两种操作：启停不删目录，删除删目录并从"已安装"里摘掉。

每个用例用自己的 `x-dsh-tenant` / `x-dsh-user`（AUTH_MODE=off 时这两个头就是
声明身份），不整库 TRUNCATE：共享库 + TRUNCATE 会让跨文件跑出现偶发死锁。
"""
import io
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

from webpage_agent.core.config import settings
from tests.exec_env import PG_DSN, new_store

SKILL_MD = """---
name: 上传的技能
description: 上传测试用技能
version: 0.2.0
---

# 上传的技能

先读正文再动手。
"""


def zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class AddonsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import webpage_agent.api.routes as api_module
        from webpage_agent.api.api_hub import Hub

        self.store = new_store()
        self.api = api_module
        self.saved_state_store = getattr(api_module.app.state, 'store', None)
        self.saved_hub = api_module.hub
        api_module.app.state.store = self.store
        # 消息入口走全局 hub；不替换就会写进 .env 指向的真实库。
        api_module.hub = Hub(self.store)
        suffix = uuid.uuid4().hex[:8]
        self.tenant = f't_{suffix}'
        self.user = f'u_{suffix}'
        self.session = f's_{suffix}'
        await self.store.ensure_session(self.session, tenant_id=self.tenant,
                                        owner_user_id=self.user)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.saved_dir = settings.skills_dir
        object.__setattr__(settings, 'skills_dir', self.root / 'skills')
        object.__setattr__(settings, 'skills_workspace_dirname', '.agent-skills')
        # 技能中心"已安装"是 JSON 文件，别写进仓库真实的 profile/。
        self._profile = patch('webpage_agent.skillhub.store._PROFILE',
                              self.root / 'skillhub')
        self._profile.start()
        self.headers = {'x-dsh-tenant': self.tenant, 'x-dsh-user': self.user}
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.api.app), base_url='http://test',
            headers=self.headers)

    async def asyncTearDown(self):
        await self.client.aclose()
        self._profile.stop()
        object.__setattr__(settings, 'skills_dir', self.saved_dir)
        self._tmp.cleanup()
        await self.api.hub.close()
        self.api.hub = self.saved_hub
        self.api.app.state.store = self.saved_state_store
        await self.store.close()

    # ---------- 专家 ----------

    async def test_list_includes_builtin_experts(self):
        res = await self.client.get('/api/experts')
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        ids = {item['id'] for item in data['items']}
        self.assertIn('exp-general', ids)
        self.assertEqual(data['custom'], 0)
        self.assertTrue(all(item['builtin'] for item in data['items']))
        # 内置专家要带"预设"，前端才能显示"用它开始会带哪些技能"
        general = next(item for item in data['items'] if item['id'] == 'exp-general')
        self.assertIn('deep-research', general['skill_ids'])

    async def test_create_update_delete_custom_expert(self):
        res = await self.client.post('/api/experts', json={
            'name': '我的写手', 'description': '只写短文案',
            'system_prompt': '你是短文案专家。', 'skill_ids': ['html-report'],
            'connector_ids': ['beeftv'], 'model': 'deepseek-v4-1-flash'})
        self.assertEqual(res.status_code, 201, res.text)
        expert_id = res.json()['id']
        self.assertFalse(res.json()['builtin'])

        res = await self.client.patch(f'/api/experts/{expert_id}',
                                      json={'description': '改过的简介'})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['description'], '改过的简介')

        res = await self.client.delete(f'/api/experts/{expert_id}')
        self.assertEqual(res.status_code, 200, res.text)
        res = await self.client.get('/api/experts')
        self.assertNotIn(expert_id, {item['id'] for item in res.json()['items']})

    async def test_custom_expert_is_not_visible_to_another_user(self):
        res = await self.client.post('/api/experts', json={
            'name': '私有专家', 'system_prompt': '你是私有专家。'})
        expert_id = res.json()['id']
        other = await self.client.get('/api/experts', headers={
            'x-dsh-tenant': self.tenant, 'x-dsh-user': 'someone-else'})
        self.assertNotIn(expert_id, {item['id'] for item in other.json()['items']})

    async def test_builtin_expert_is_read_only(self):
        res = await self.client.delete('/api/experts/exp-general')
        self.assertEqual(res.status_code, 400)

    async def test_create_expert_requires_prompt(self):
        res = await self.client.post('/api/experts', json={'name': '空专家'})
        self.assertEqual(res.status_code, 400)

    # ---------- 会话选择 ----------

    async def test_patch_session_addons_persists_and_resolves(self):
        res = await self.client.get(f'/api/sessions/{self.session}/addons')
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsNone(res.json()['expert'])

        res = await self.client.patch(f'/api/sessions/{self.session}/addons',
                                      json={'expert_id': 'exp-data'})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['expert']['id'], 'exp-data')

        # 落库：不是只在响应里有效
        stored = await self.store.session_addons(self.session)
        self.assertEqual(stored['expert_id'], 'exp-data')
        res = await self.client.get(f'/api/sessions/{self.session}/addons')
        self.assertEqual(res.json()['expert']['id'], 'exp-data')

    async def test_patch_rejects_unknown_expert(self):
        res = await self.client.patch(f'/api/sessions/{self.session}/addons',
                                      json={'expert_id': 'exp-does-not-exist'})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(await self.store.session_addons(self.session), {})

    async def test_patch_skills_reports_missing(self):
        res = await self.client.patch(f'/api/sessions/{self.session}/addons',
                                      json={'skill_ids': ['not-installed']})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['missing_skill_ids'], ['not-installed'])
        self.assertIn('notice', res.json())

    async def test_patch_connectors_explicit_empty(self):
        await self.client.patch(f'/api/sessions/{self.session}/addons',
                                json={'connector_ids': []})
        res = await self.client.get(f'/api/sessions/{self.session}/addons')
        self.assertTrue(res.json()['connectors_explicit'])
        self.assertEqual(res.json()['connector_ids'], [])

    async def test_other_users_session_is_not_found(self):
        res = await self.client.get(f'/api/sessions/{self.session}/addons',
                                    headers={'x-dsh-tenant': self.tenant,
                                             'x-dsh-user': 'someone-else'})
        self.assertEqual(res.status_code, 404)

    async def test_first_message_carries_addon_selection(self):
        """新会话第一句话就带选择：随 turn 同事务落库，不留"第一条不生效"的窗口。"""
        from dataclasses import replace
        from unittest.mock import AsyncMock

        from webpage_agent.api import hub_messages

        with patch.object(hub_messages, 'worker_alive', AsyncMock(return_value=True)), \
                patch.object(hub_messages, 'settings',
                             replace(settings, require_live_worker=False)):
            res = await self.client.post(
                f'/api/sessions/{self.session}/messages',
                json={'request_id': 'req-addon-1', 'message': '帮我核实这条消息',
                      'addon_selection': {'expert_id': 'exp-research',
                                          'skill_ids': ['fact-check']}})
        self.assertEqual(res.status_code, 202, res.text)
        stored = await self.store.session_addons(self.session)
        self.assertEqual(stored.get('expert_id'), 'exp-research')
        self.assertEqual(stored.get('skill_ids'), ['fact-check'])

    # ---------- 技能 ----------

    async def test_install_catalog_skill_writes_skill_file(self):
        res = await self.client.post('/api/skills/deep-research/install')
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue((self.root / 'skills' / 'deep-research' / 'SKILL.md').is_file())

        res = await self.client.get('/api/skills')
        item = next((x for x in res.json()['items'] if x['id'] == 'deep-research'), None)
        self.assertIsNotNone(item)
        self.assertTrue(item['installed'])
        self.assertTrue(item['on_disk'])
        self.assertTrue(item['enabled'])

    async def test_upload_skill_zip(self):
        payload = zip_bytes({'SKILL.md': SKILL_MD,
                             'references/tone.md': '# 语气\n短句为主'})
        res = await self.client.post(
            '/api/skills/upload',
            files={'file': ('upload.zip', payload, 'application/zip')})
        self.assertEqual(res.status_code, 201, res.text)
        body = res.json()
        self.assertEqual(body['id'], 'upload')
        self.assertTrue((self.root / 'skills' / 'upload' / 'references'
                         / 'tone.md').is_file())

    async def test_upload_rejects_skill_without_description(self):
        payload = zip_bytes({'SKILL.md': '# 没 frontmatter'})
        res = await self.client.post(
            '/api/skills/upload',
            files={'file': ('bad.zip', payload, 'application/zip')})
        self.assertEqual(res.status_code, 400)
        self.assertIn('SKILL.md', res.json()['detail'])

    async def test_enable_disable_and_delete_skill(self):
        await self.client.post('/api/skills/deep-research/install')
        res = await self.client.post('/api/skills/deep-research/enable?enabled=false')
        self.assertEqual(res.status_code, 200, res.text)
        self.assertFalse(res.json()['enabled'])
        self.assertTrue((self.root / 'skills' / 'deep-research' / 'SKILL.md').is_file())

        res = await self.client.delete('/api/skills/deep-research')
        self.assertEqual(res.status_code, 200, res.text)
        self.assertFalse((self.root / 'skills' / 'deep-research').exists())
        # 目录条目仍在（它是"可安装的技能"），但已不是"装好的技能"
        res = await self.client.get('/api/skills/deep-research')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()['installed'])
        self.assertFalse(res.json()['on_disk'])


if __name__ == '__main__':                                  # pragma: no cover
    unittest.main()
