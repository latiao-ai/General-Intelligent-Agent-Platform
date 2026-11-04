"""运行预算异常的归属。

定义在 `webpage_agent.core.errors`。`store.pg_store` 只按原名再导出，
旧路径拿到的必须是同一个类对象。`BudgetExceeded` 的三个类属性被
`as_outcome` 收成执行结果：丢了 `certainty`，可安全重试会变成需要人工核实。
"""
import ast
import unittest
from pathlib import Path

from webpage_agent.core.errors import BudgetExceeded, DecisionBudgetExceeded
from webpage_agent.observe.errors import as_outcome

ROOT = Path(__file__).resolve().parents[1]
_AGENT_SOURCES = (
    ROOT / 'src/webpage_agent/agent/nodes.py',
    ROOT / 'src/webpage_agent/agent/decide.py',
)
# 动作账本仍从 store 导入。这次只搬走预算异常，不把 ledger 一起换家。
_REMAINING_STORE_IMPORT = frozenset({('webpage_agent.store', 'ledger')})


def _store_imports(path: Path) -> set[tuple[str, str]]:
    """源码里每一处 store 层导入，含写在函数体内的。"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if module == 'webpage_agent.store' or module.startswith('webpage_agent.store.'):
                found.update((module, alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == 'webpage_agent.store' or name.startswith('webpage_agent.store.'):
                    found.add((name, ''))
    return found


def _imports_name(path: Path, module: str, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            if any(alias.name == name for alias in node.names):
                return True
    return False


class BudgetErrorLayerTests(unittest.TestCase):
    """类身份、执行确定性、以及 agent 不再为这两个异常依赖 store。"""

    def test_store_reexports_the_same_class_objects(self):
        from webpage_agent.core import errors
        from webpage_agent.store import pg_store

        self.assertIs(errors.BudgetExceeded, pg_store.BudgetExceeded)
        self.assertIs(errors.DecisionBudgetExceeded, pg_store.DecisionBudgetExceeded)
        try:
            raise errors.BudgetExceeded('运行预算用尽')
        except pg_store.BudgetExceeded as caught:
            self.assertIsInstance(caught, errors.BudgetExceeded)
        else:
            self.fail('store 再导出的 BudgetExceeded 接不住 core 抛出的同一个类')

    def test_budget_exceeded_keeps_retryable_outcome_attributes(self):
        self.assertEqual(BudgetExceeded.error_code, 'E_RUN_BUDGET')
        self.assertEqual(BudgetExceeded.certainty, 'not_executed')
        self.assertEqual(BudgetExceeded.phase, 'validate')
        self.assertEqual(BudgetExceeded.__bases__, (RuntimeError,))

        outcome = as_outcome(BudgetExceeded())
        self.assertEqual(outcome.error_code, 'E_RUN_BUDGET')
        self.assertEqual(outcome.certainty, 'not_executed')
        self.assertEqual(outcome.phase, 'validate')
        self.assertTrue(outcome.retryable)

        self.assertEqual(str(BudgetExceeded()), '运行预算用尽')
        self.assertEqual(BudgetExceeded().safe_message, '运行预算用尽')
        custom = BudgetExceeded('本次运行的 token 已用 3，上限 2')
        self.assertEqual(str(custom), '本次运行的 token 已用 3，上限 2')
        self.assertEqual(custom.safe_message, '本次运行的 token 已用 3，上限 2')

    def test_decision_budget_message_and_outcome_stay_unchanged(self):
        """自主决策用尽没有运行预算那三个属性，不能在搬家时抄过去。"""
        self.assertFalse(hasattr(DecisionBudgetExceeded, 'error_code'))
        self.assertFalse(hasattr(DecisionBudgetExceeded, 'certainty'))
        self.assertFalse(hasattr(DecisionBudgetExceeded, 'phase'))
        self.assertEqual(DecisionBudgetExceeded.__bases__, (RuntimeError,))
        self.assertEqual(str(DecisionBudgetExceeded()), '决策预算用尽')
        self.assertEqual(DecisionBudgetExceeded().safe_message, '决策预算用尽')

        outcome = as_outcome(DecisionBudgetExceeded())
        self.assertEqual(outcome.certainty, 'unknown')
        self.assertEqual(outcome.error_code, 'DecisionBudgetExceeded')
        self.assertEqual(outcome.phase, '')
        self.assertFalse(outcome.retryable)

    def test_agent_modules_do_not_import_budget_exceptions_from_store(self):
        """把 nodes 或 decide 的异常导入改回 store.pg_store，这里会失败。"""
        expected_exception = {
            'nodes.py': 'BudgetExceeded',
            'decide.py': 'DecisionBudgetExceeded',
        }
        for path in _AGENT_SOURCES:
            exception_name = expected_exception[path.name]
            store_imports = _store_imports(path)
            self.assertEqual(
                store_imports, _REMAINING_STORE_IMPORT,
                f'{path.name} 的 store 导入应只剩 ledger，实际是 {store_imports}')
            self.assertTrue(
                _imports_name(path, 'webpage_agent.core.errors', exception_name),
                f'{path.name} 没有从 webpage_agent.core.errors 导入 {exception_name}')
