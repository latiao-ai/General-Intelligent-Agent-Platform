"""解析层：会话选择 → 本轮专家人设 / 技能物化 / 连接器集合 / 模型。

这里钉的是**优先级语义**，因为它是"选了却不生效"类问题的唯一来源：

- 会话显式选择 > 专家预设；
- "没选"（缺键）≠ "选了空"（显式 `[]`）：前者沿用旧行为 / 专家预设，
  后者是明确不要；
- 专家预设技能没安装时，提示里要说"不可用"，而不是假装在用。

**不 TRUNCATE 整库**：每个用例用自己的租户/用户/会话 id。以前这类测试在
`reset_db()` 里整库 TRUNCATE，和前一个用例的写事务撞上就会偶发 PG 死锁
（跨文件跑必现）。按主体隔离既快又没有共享状态。
"""
import tempfile
import unittest
import uuid
from pathlib import Path

from webpage_agent.addons import resolver, skills
from webpage_agent.core.config import settings
from tests.exec_env import PG_DSN, new_store

SKILL_MD = """---
name: 事实核查
description: 核对说法是否属实，列出查到的来源
version: 1.0.0
---

# 事实核查

1. 拆出可验证的事实断言。
2. 至少找两个独立来源。
"""


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = new_store()
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
        skills.install_archive('SKILL.md', SKILL_MD.encode('utf-8'), skill_id='fact-check')

    async def asyncTearDown(self):
        object.__setattr__(settings, 'skills_dir', self.saved_dir)
        self._tmp.cleanup()
        await self.store.close()

    async def resolve(self, **kwargs):
        return await resolver.resolve(
            self.store, session_id=self.session, tenant_id=self.tenant,
            owner_user_id=self.user, workspace_dir=str(self.root / 'ws'), **kwargs)

    async def test_no_selection_means_legacy_connectors(self):
        resolved = await self.resolve()
        self.assertIsNone(resolved['expert'])
        self.assertIsNone(resolved['connector_ids'])       # 没选 → 沿用旧行为
        self.assertFalse(resolved['connectors_explicit'])
        self.assertEqual(resolved['skills'], [])
        self.assertIn('没有勾选技能', resolved['prompt_block'])

    async def test_expert_presets_apply_as_defaults(self):
        await self.store.set_session_addons(self.session, {'expert_id': 'exp-research'})
        resolved = await self.resolve()
        self.assertEqual(resolved['expert']['id'], 'exp-research')
        self.assertIn('检索与研究专家', resolved['prompt_block'])
        # 专家预设里只有 fact-check 装了，deep-research / web-extract 没装
        self.assertIn('fact-check', resolved['skill_ids'])
        self.assertIn('deep-research', resolved['missing_skill_ids'])
        self.assertIn('.agent-skills', resolved['prompt_block'])
        # 专家没有预设连接器 → 不显式，沿用旧行为
        self.assertIsNone(resolved['connector_ids'])

    async def test_explicit_skills_override_expert_presets(self):
        await self.store.set_session_addons(
            self.session, {'expert_id': 'exp-research', 'skill_ids': []})
        resolved = await self.resolve()
        self.assertTrue(resolved['skills_explicit'])
        # 显式空数组 = 明确不要技能，不能退回专家预设
        self.assertEqual(resolved['skill_ids'], [])
        self.assertEqual(resolved['missing_skill_ids'], [])

    async def test_expert_connector_presets_are_used_as_defaults(self):
        """专家预设连接器（如画布专家 → beeftv）生效，且算"已决定"。"""
        await self.store.set_session_addons(self.session, {'expert_id': 'exp-canvas'})
        resolved = await self.resolve()
        self.assertEqual(resolved['connector_ids'], ['beeftv'])
        self.assertTrue(resolved['connectors_decided'])
        self.assertFalse(resolved['connectors_explicit'])

    async def test_explicit_empty_connectors_is_not_legacy(self):
        await self.store.set_session_addons(self.session, {'connector_ids': []})
        resolved = await self.resolve()
        self.assertTrue(resolved['connectors_explicit'])
        self.assertEqual(resolved['connector_ids'], [])

    async def test_missing_skill_is_reported_in_prompt(self):
        await self.store.set_session_addons(self.session, {'skill_ids': ['ghost']})
        resolved = await self.resolve()
        self.assertEqual(resolved['missing_skill_ids'], ['ghost'])
        self.assertIn('不可用', resolved['prompt_block'])

    async def test_materialized_dir_is_rebuilt_when_selection_changes(self):
        await self.store.set_session_addons(self.session, {'skill_ids': ['fact-check']})
        await self.resolve()
        target = (self.root / 'ws' / '.agent-skills' / self.session / 'fact-check'
                  / 'SKILL.md')
        self.assertTrue(target.is_file())
        await self.store.set_session_addons(self.session, {'skill_ids': []})
        await self.resolve()
        self.assertFalse(target.exists())

    async def test_dry_run_does_not_touch_disk(self):
        await self.store.set_session_addons(self.session, {'skill_ids': ['fact-check']})
        await self.resolve(materialize=False)
        self.assertFalse((self.root / 'ws' / '.agent-skills').exists())
        # 但"生效清单"仍然要按磁盘现状算出来
        resolved = await self.resolve(materialize=False)
        self.assertEqual(resolved['skill_ids'], ['fact-check'])

    async def test_custom_expert_only_visible_to_owner(self):
        from webpage_agent.addons import experts
        created = await experts.create_expert(
            self.store, {'name': '私有专家', 'system_prompt': '你是私有专家。'},
            tenant_id=self.tenant, owner_user_id=self.user)
        mine = await experts.get_expert(self.store, created['id'], tenant_id=self.tenant,
                                        owner_user_id=self.user)
        self.assertIsNotNone(mine)
        other = await experts.get_expert(self.store, created['id'], tenant_id=self.tenant,
                                         owner_user_id='someone-else')
        self.assertIsNone(other)

    def test_merge_selection_patch_semantics(self):
        current = {'expert_id': 'exp-data', 'skill_ids': ['a'], 'connector_ids': ['beeftv']}
        merged = resolver.merge_selection(current, {'skill_ids': ['b']})
        self.assertEqual(merged['skill_ids'], ['b'])
        self.assertEqual(merged['expert_id'], 'exp-data')
        self.assertEqual(merged['connector_ids'], ['beeftv'])
        cleared = resolver.merge_selection(current, {'expert_id': None})
        self.assertEqual(cleared['expert_id'], '')
        self.assertEqual(cleared['skill_ids'], ['a'])

    def test_merge_selection_null_means_back_to_expert_presets(self):
        """`skill_ids: null` 是"恢复专家预设"，`[]` 才是"明确不要技能"。"""
        current = {'expert_id': 'exp-data', 'skill_ids': ['a']}
        restored = resolver.merge_selection(current, {'skill_ids': None})
        self.assertNotIn('skill_ids', restored)
        self.assertEqual(restored['expert_id'], 'exp-data')
        emptied = resolver.merge_selection(current, {'skill_ids': []})
        self.assertEqual(emptied['skill_ids'], [])
        # 连接器同理：null = 回到"沿用旧行为"
        with_connector = resolver.merge_selection(
            {'connector_ids': ['beeftv']}, {'connector_ids': None})
        self.assertNotIn('connector_ids', with_connector)

    def test_normalize_selection_drops_junk(self):
        normalized = resolver.normalize_selection(
            {'skill_ids': ['a', 'a', '', None, 3], 'connector_ids': 'nope'})
        self.assertEqual(normalized['skill_ids'], ['a', '3'])
        self.assertEqual(normalized['connector_ids'], [])
        self.assertEqual(normalized['expert_id'], '')


if __name__ == '__main__':                                  # pragma: no cover
    unittest.main()
