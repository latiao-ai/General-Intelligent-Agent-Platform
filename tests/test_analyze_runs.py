"""`scripts/analyze_runs.py` 的纯函数测试（不连库）。

重点是钉住两个已经踩过的坑：
1. SQL 里的裸 `%` 会被 psycopg 当占位符 → 整个查询直接报错；
2. 修订率不能拿 `committed_plan_version` 算（实测库里恒为 0），要用 `base_plan_version`。
"""
import unittest

from scripts import analyze_runs


class SqlShapeTests(unittest.TestCase):
    def test_server_sql_has_no_bare_percent(self):
        # psycopg 只允许 %s/%b/%t；裸 % 会被当成占位符解析失败
        self.assertNotIn('%', analyze_runs.SERVER_SQL)

    def test_server_sql_splits_known_prefixes(self):
        self.assertIn('split_part(tool_key', analyze_runs.SERVER_SQL)
        self.assertIn("'mcp'", analyze_runs.SERVER_SQL)
        self.assertIn("'local'", analyze_runs.SERVER_SQL)


class WindowTests(unittest.TestCase):
    def test_no_window_means_no_filter(self):
        where, params = analyze_runs._window(None)
        self.assertEqual(where, '')
        self.assertEqual(params, [])

    def test_window_filters_by_created_at(self):
        where, params = analyze_runs._window(7)
        self.assertIn('created_at >= %s', where)
        self.assertEqual(len(params), 1)
        self.assertIsInstance(params[0], str)


def _payload(**over):
    base = {
        'window_days': None,
        'external_tasks': {
            'total': 100, 'by_state': {'completed': 80, 'needs_reconcile': 12,
                                       'running': 8},
            'unresolved': 12, 'unresolved_rate': 0.12, 'still_open': 8,
            'by_server': [
                {'server': 'mcp:openmontage', 'total': 60, 'unresolved': 10,
                 'unresolved_rate': 0.1667},
                {'server': 'mcp:beeftv', 'total': 40, 'unresolved': 2,
                 'unresolved_rate': 0.05},
            ],
        },
        'planning': {
            'runs_with_decisions': 5, 'decisions': 6,
            'decisions_per_run': 1.2, 'provider_requests_per_run': 1.2,
            'revised_runs': 1, 'revision_rate': 0.2,
            'by_status': [{'status': 'decided', 'count': 4}],
            'by_kind': [{'kind': 'wait_approval', 'count': 3,
                         'avg_provider_requests': 1.0}],
            'stop_reasons': [{'reason': 'no_progress', 'count': 2}],
            'duplicate_decisions': 0,
            'decision_errors': [],
        },
    }
    base.update(over)
    return base


class RenderTests(unittest.TestCase):
    def test_reports_unresolved_rate_and_revision_rate(self):
        text = analyze_runs.render(_payload())
        self.assertIn('未定率 12.0%', text)
        self.assertIn('修订率 20.0%', text)
        self.assertIn('mcp:openmontage', text)

    def test_flags_duplicate_decisions_when_present(self):
        data = _payload()
        data['planning']['duplicate_decisions'] = 3
        self.assertIn('重复决策 3 组', analyze_runs.render(data))

    def test_empty_database_degrades_gracefully(self):
        data = _payload()
        data['external_tasks'] = {'total': 0, 'by_state': {}, 'unresolved': 0,
                                  'unresolved_rate': None, 'still_open': 0,
                                  'by_server': []}
        data['planning'] = {'runs_with_decisions': 0, 'decisions': 0,
                            'decisions_per_run': None,
                            'provider_requests_per_run': None,
                            'revised_runs': 0, 'revision_rate': None,
                            'by_status': [], 'by_kind': [], 'stop_reasons': [],
                            'duplicate_decisions': 0, 'decision_errors': []}
        text = analyze_runs.render(data)
        self.assertIn('无记录', text)
        self.assertNotIn('Traceback', text)


if __name__ == '__main__':
    unittest.main()
