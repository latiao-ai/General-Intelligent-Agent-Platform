"""任务目录的孤儿清理：只删「库里已没有这个 task_id」的目录。

`_tool_results` 有 manifest 表可以反查孤儿；任务目录没有登记表，保留策略
（retention_artifacts_days）也只遍历库里还存在的 task，所以库重建或任务被删
之后那些目录会永远留在磁盘上。这里锁住新加的 `gc_orphan_task_dirs` 边界。
"""
import os
import tempfile
import time
import unittest
from pathlib import Path

from webpage_agent.store import artifact_store

LIVE = 'a' * 32        # 还在库里
DEAD_OLD = 'b' * 32    # 库没了、且过了宽限期 → 该删
DEAD_NEW = 'c' * 32    # 库没了、但还在宽限期内 → 先不动


class OrphanTaskDirGcTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        for name in (LIVE, DEAD_OLD, DEAD_NEW):
            (self.root / name).mkdir()
            (self.root / name / 'state.json').write_text('{}')
            (self.root / name / 'results').mkdir()
        old = time.time() - 3 * 86400
        os.utime(self.root / DEAD_OLD, (old, old))
        # 非任务目录：工具结果缓存与任意名字的目录都必须原样保留
        (self.root / '_tool_results').mkdir()
        (self.root / 'not-a-task-dir').mkdir()

    def tearDown(self):
        self._folder.cleanup()

    def _names(self):
        return sorted(p.name for p in self.root.iterdir())

    def test_deletes_only_stale_orphans(self):
        removed = artifact_store.gc_orphan_task_dirs(
            self.root, grace_seconds=86400, live_task_ids={LIVE})
        self.assertEqual(removed, 1)
        self.assertEqual(self._names(), sorted([LIVE, DEAD_NEW, '_tool_results', 'not-a-task-dir']))

    def test_live_task_dir_is_kept_even_when_old(self):
        old = time.time() - 30 * 86400
        os.utime(self.root / LIVE, (old, old))
        removed = artifact_store.gc_orphan_task_dirs(
            self.root, grace_seconds=86400, live_task_ids={LIVE})
        self.assertEqual(removed, 1)          # 只有 DEAD_OLD
        self.assertIn(LIVE, self._names())

    def test_zero_grace_disables_cleanup(self):
        """宽限期 0 = 关闭，和 gc_unregistered 的语义保持一致。"""
        removed = artifact_store.gc_orphan_task_dirs(
            self.root, grace_seconds=0, live_task_ids={LIVE})
        self.assertEqual(removed, 0)
        self.assertIn(DEAD_OLD, self._names())

    def test_missing_live_set_disables_cleanup(self):
        """拿不到在用 task 集合时宁可不删，避免把正在跑的任务目录删掉。"""
        removed = artifact_store.gc_orphan_task_dirs(self.root, grace_seconds=86400)
        self.assertEqual(removed, 0)
        self.assertIn(DEAD_OLD, self._names())

    def test_never_follows_or_removes_symlinks(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        keep = Path(outside.name) / 'keep.txt'
        keep.write_text('outside')
        link = self.root / ('d' * 32)
        link.symlink_to(Path(outside.name))
        old = time.time() - 30 * 86400
        os.utime(link, (old, old), follow_symlinks=False)
        removed = artifact_store.gc_orphan_task_dirs(
            self.root, grace_seconds=86400, live_task_ids={LIVE})
        self.assertEqual(removed, 1)                      # 仍然只删 DEAD_OLD
        self.assertTrue(keep.is_file())                   # 外部目录没被动
        self.assertTrue(link.is_symlink())                # 链接本身也没被删


if __name__ == '__main__':
    unittest.main()
