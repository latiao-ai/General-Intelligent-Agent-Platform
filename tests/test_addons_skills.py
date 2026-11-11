"""技能库（磁盘技能包）：安装/上传/导入/启停/物化。

这条链路上最容易出事的三处，各钉一条用例：

1. **上传包的结构**：没有 SKILL.md、frontmatter 没 description、包里有 `../`
   这类穿越路径——都要在落盘前拒绝，否则"上传一个技能"等于"往服务器任意写"。
2. **启停是软开关**：禁用后仍能在列表里看到（状态为 disabled），但物化时不再
   出现；重新启用立刻恢复。
3. **会话物化**：只拷本轮选中的技能，模型看到的是相对工作目录的路径；取消选择
   后上一轮的目录必须被清掉（否则"取消了却还在用"）。
"""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from webpage_agent.addons import skills
from webpage_agent.core.config import settings

SKILL_MD = """---
name: 会议纪要
description: 从录音转写中提炼决议、待办与负责人
version: 1.2.0
---

# 会议纪要

把会议内容整理成：决议、待办、负责人、截止时间。
"""


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class SkillStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.saved = settings.skills_dir
        object.__setattr__(settings, 'skills_dir', self.root / 'skills')
        object.__setattr__(settings, 'skills_workspace_dirname', '.agent-skills')

    def tearDown(self):
        object.__setattr__(settings, 'skills_dir', self.saved)
        self._tmp.cleanup()

    # ---------- 安装 / 上传 ----------

    def test_upload_zip_with_references_lands_on_disk(self):
        payload = make_zip({
            'meeting-notes/SKILL.md': SKILL_MD,
            'meeting-notes/references/format.md': '# 格式\n决议 | 待办',
        })
        row = skills.install_archive('meeting-notes.zip', payload)
        self.assertEqual(row['id'], 'meeting-notes')
        self.assertEqual(row['version'], '1.2.0')
        self.assertIn('references', row['files'])
        self.assertTrue((self.root / 'skills' / 'meeting-notes' / 'SKILL.md').is_file())
        # 正文里的脚本/参考要原样保留，不能只存一份 SKILL.md
        self.assertTrue((self.root / 'skills' / 'meeting-notes' / 'references'
                         / 'format.md').is_file())

    def test_upload_rejects_missing_or_empty_description(self):
        payload = make_zip({'SKILL.md': '---\nname: x\n---\n正文'})
        with self.assertRaises(skills.SkillError):
            skills.install_archive('x.zip', payload)
        payload = make_zip({'SKILL.md': '# 只有正文，没有 frontmatter'})
        with self.assertRaises(skills.SkillError):
            skills.install_archive('y.zip', payload)

    def test_upload_rejects_path_traversal(self):
        payload = make_zip({'SKILL.md': SKILL_MD, '../../evil.sh': 'rm -rf /'})
        with self.assertRaises(skills.SkillError):
            skills.install_archive('bad.zip', payload)

    def test_upload_rejects_multiple_skill_roots(self):
        payload = make_zip({'a/SKILL.md': SKILL_MD, 'b/SKILL.md': SKILL_MD})
        with self.assertRaises(skills.SkillError):
            skills.install_archive('two.zip', payload)

    def test_single_markdown_upload(self):
        row = skills.install_archive('SKILL.md', SKILL_MD.encode('utf-8'),
                                     skill_id='notes')
        self.assertEqual(row['id'], 'notes')
        self.assertEqual(row['name'], '会议纪要')

    def test_parse_frontmatter_ignores_complex_yaml(self):
        meta, body = skills.parse_frontmatter(
            '---\nname: a\ndescription: "带引号"\ntags:\n  - x\n---\n正文')
        self.assertEqual(meta['name'], 'a')
        self.assertEqual(meta['description'], '带引号')
        self.assertEqual(body.strip(), '正文')

    def test_import_directory_adds_description(self):
        source = self.root / 'import-me'
        source.mkdir(parents=True)
        (source / 'SKILL.md').write_text('# 导入技能\n做点事', encoding='utf-8')
        row = skills.import_from_path(str(source))
        self.assertEqual(row['id'], 'import-me')
        self.assertTrue(row['description'])
        self.assertIn('description:', (source.parent / 'skills' / 'import-me'
                                       / 'SKILL.md').read_text(encoding='utf-8'))

    # ---------- 启停 / 删除 ----------

    def test_disable_keeps_directory_but_hides_from_materialize(self):
        skills.install_archive('SKILL.md', SKILL_MD.encode('utf-8'), skill_id='notes')
        skills.set_enabled('notes', False)
        row = skills.get_skill('notes')
        self.assertFalse(row['enabled'])
        workspace = self.root / 'ws'
        workspace.mkdir()
        result = skills.materialize(['notes'], workspace_dir=str(workspace),
                                    session_id='s_1')
        self.assertEqual(result['skills'], [])
        self.assertEqual(result['missing'], ['notes'])
        # 软开关：目录还在
        self.assertTrue((self.root / 'skills' / 'notes' / 'SKILL.md').is_file())
        skills.set_enabled('notes', True)
        self.assertTrue(skills.get_skill('notes')['enabled'])

    def test_remove_deletes_directory(self):
        skills.install_archive('SKILL.md', SKILL_MD.encode('utf-8'), skill_id='notes')
        self.assertTrue(skills.remove_skill('notes'))
        self.assertIsNone(skills.get_skill('notes'))
        self.assertFalse(skills.remove_skill('notes'))

    def test_directory_without_skill_md_is_not_a_skill(self):
        (self.root / 'skills' / 'not-a-skill').mkdir(parents=True)
        self.assertEqual(skills.list_skills(), [])

    # ---------- 会话物化 ----------

    def test_materialize_copies_selected_skills_and_renders_index(self):
        skills.install_archive('SKILL.md', SKILL_MD.encode('utf-8'), skill_id='notes')
        workspace = self.root / 'ws'
        workspace.mkdir()
        result = skills.materialize(['notes', 'ghost'], workspace_dir=str(workspace),
                                    session_id='s_42')
        self.assertEqual([row['id'] for row in result['skills']], ['notes'])
        self.assertEqual(result['missing'], ['ghost'])
        target = workspace / '.agent-skills' / 's_42' / 'notes' / 'SKILL.md'
        self.assertTrue(target.is_file())
        prompt = skills.render_prompt(result)
        self.assertIn('会议纪要', prompt)
        self.assertIn('.agent-skills/s_42/notes/SKILL.md', prompt)

    def test_materialize_rebuilds_directory_each_turn(self):
        skills.install_archive('SKILL.md', SKILL_MD.encode('utf-8'), skill_id='notes')
        workspace = self.root / 'ws'
        workspace.mkdir()
        skills.materialize(['notes'], workspace_dir=str(workspace), session_id='s_42')
        stale = workspace / '.agent-skills' / 's_42' / 'notes' / 'stale.txt'
        stale.write_text('x', encoding='utf-8')
        skills.materialize(['notes'], workspace_dir=str(workspace), session_id='s_42')
        self.assertFalse(stale.exists())

    def test_state_file_is_not_treated_as_skill(self):
        skills.install_archive('SKILL.md', SKILL_MD.encode('utf-8'), skill_id='notes')
        state = json.loads((self.root / 'skills' / '.state.json').read_text(encoding='utf-8'))
        self.assertTrue(state['notes']['enabled'])


if __name__ == '__main__':                                  # pragma: no cover
    unittest.main()
