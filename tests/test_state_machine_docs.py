"""运行状态文档跟得上代码里的词汇。

改了 `RUN_STATUSES`、派生集合的名字，或 `ObservationDecision.DECISIONS`，
却没改 `docs/state-machine.md` 时，这里变红。只检查这些词出现过，
不检查出现顺序，也不检查标题怎么写。
"""
import unittest
from pathlib import Path

from webpage_agent.core import outcome as outcome_module
from webpage_agent.core.schemas import ObservationDecision

DOC = Path(__file__).resolve().parents[1] / 'docs' / 'state-machine.md'


def run_status_constant_names() -> list[str]:
    """`outcome` 里每一份运行状态集合的常量名。新加一份集合也会被算进来。"""
    return sorted(
        name for name, value in vars(outcome_module).items()
        if name.endswith('RUN_STATUSES') and isinstance(value, frozenset))


class StateMachineDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = DOC.read_text(encoding='utf-8')

    def test_doc_exists(self):
        self.assertTrue(DOC.is_file(), f'缺少状态机说明：{DOC}')
        self.assertTrue(self.doc.strip(), '状态机说明是空文件')

    def test_every_run_status_constant_name_appears(self):
        missing = [name for name in run_status_constant_names() if name not in self.doc]
        self.assertEqual(missing, [], f'文档缺少运行状态常量名：{missing}')

    def test_every_run_status_literal_appears(self):
        missing = sorted(status for status in outcome_module.RUN_STATUSES
                         if status not in self.doc)
        self.assertEqual(missing, [], f'文档缺少运行状态字面值：{missing}')

    def test_every_decision_appears(self):
        missing = [kind for kind in ObservationDecision.DECISIONS if kind not in self.doc]
        self.assertEqual(missing, [], f'文档缺少决定：{missing}')
