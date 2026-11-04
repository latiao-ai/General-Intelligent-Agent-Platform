"""测试前置检查：连不上库必须非零退出，不能静默"少跑一批用例却显示通过"。

这条检查存在的理由就是防止假阳性，所以这里断言的是退出码，不是"有没有打印"。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_test_db

ROOT = Path(__file__).resolve().parents[1]


class CheckTestDbTests(unittest.TestCase):
    def test_refuses_without_a_dsn(self):
        self.assertEqual(check_test_db.main(['check_test_db.py']), 1)
        self.assertEqual(check_test_db.main(['check_test_db.py', '   ']), 1)

    def test_unreachable_server_is_reported(self):
        # 端口上没人监听：必须失败，而不是返回 0 让调用方以为库就绪
        code = check_test_db.main(
            ['check_test_db.py', 'postgresql://postgres@127.0.0.1:59999/agent_test'])
        self.assertEqual(code, 1)

    def test_missing_driver_is_not_a_pass(self):
        # 驱动没装时脚本跑不了，这同样是"不能验证"，不能算通过
        with patch.dict(sys.modules, {'psycopg': None}):
            code = check_test_db.main(
                ['check_test_db.py', 'postgresql://postgres@127.0.0.1:55432/agent_test'])
        self.assertEqual(code, 1)

    def test_run_tests_script_calls_the_preflight(self):
        # 脚本文本里必须真的调用了它，否则检查形同虚设
        script = (ROOT / 'scripts' / 'run_tests.sh').read_text(encoding='utf-8')
        self.assertIn('check_test_db.py', script)
        # 连不上时要 exit 2，不能只是打印一行就继续跑
        self.assertIn('exit 2', script)
