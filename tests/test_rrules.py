"""RRULE 子集的语义测试：这些断言就是"定时任务什么时候触发"的唯一说明书。

全部用**本地时区**的 `datetime(...)` 构造期望值，不写死 epoch —— CI 在 UTC 上跑、
开发机在 UTC+8 上跑，结果必须一致。
"""
import unittest
from datetime import datetime

from webpage_agent.core import rrules


def at(year: int, month: int, day: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute)


class ParseTests(unittest.TestCase):
    def test_parse_daily(self):
        rule = rrules.parse('FREQ=DAILY;BYHOUR=9;BYMINUTE=0')
        self.assertEqual(rule.freq, 'DAILY')
        self.assertEqual(rule.byhour, (9,))
        self.assertEqual(rule.byminute, (0,))

    def test_roundtrip_text(self):
        text = 'FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH;BYHOUR=9;BYMINUTE=30'
        self.assertEqual(rrules.parse(text).to_text(), text)

    def test_rejects_bad_input(self):
        for bad in ('', 'DAILY', 'FREQ=SECONDLY', 'FREQ=DAILY;INTERVAL=0',
                    'FREQ=DAILY;INTERVAL=x', 'FREQ=DAILY;BYHOUR=24',
                    'FREQ=DAILY;BYDAY=XX', 'FREQ=WEEKLY', 'FREQ=MONTHLY',
                    'FREQ=YEARLY', 'FREQ=DAILY;FOO=1', 'FREQ=DAILY;BYHOUR=1;BYHOUR=2'):
            with self.subTest(bad=bad), self.assertRaises(rrules.RuleError):
                rrules.parse(bad)


class NextRunAtTests(unittest.TestCase):
    def test_daily_same_time_next_day_when_already_past(self):
        rule = rrules.parse('FREQ=DAILY;BYHOUR=9;BYMINUTE=0')
        next_at = rrules.next_run_at(rule, at(2026, 11, 11, 10, 0).timestamp())
        self.assertEqual(next_at, at(2026, 11, 12, 9, 0).timestamp())

    def test_daily_later_today(self):
        rule = rrules.parse('FREQ=DAILY;BYHOUR=9;BYMINUTE=0')
        next_at = rrules.next_run_at(rule, at(2026, 11, 11, 8, 0).timestamp())
        self.assertEqual(next_at, at(2026, 11, 11, 9, 0).timestamp())

    def test_exactly_on_the_minute_goes_to_next_occurrence(self):
        rule = rrules.parse('FREQ=DAILY;BYHOUR=9;BYMINUTE=0')
        next_at = rrules.next_run_at(rule, at(2026, 11, 11, 9, 0).timestamp())
        self.assertEqual(next_at, at(2026, 11, 12, 9, 0).timestamp())

    def test_daily_inherits_anchor_clock(self):
        rule = rrules.parse('FREQ=DAILY')
        next_at = rrules.next_run_at(rule, at(2026, 11, 11, 15, 26).timestamp())
        self.assertEqual(next_at, at(2026, 11, 12, 15, 26).timestamp())

    def test_weekly_next_matching_weekday(self):
        rule = rrules.parse('FREQ=WEEKLY;BYDAY=MO,TH;BYHOUR=9;BYMINUTE=0')
        # 2026-11-11 是周三
        next_at = rrules.next_run_at(rule, at(2026, 11, 11, 10, 0).timestamp())
        self.assertEqual(next_at, at(2026, 11, 12, 9, 0).timestamp())

    def test_weekly_interval_two_skips_a_week(self):
        rule = rrules.parse('FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;BYHOUR=9;BYMINUTE=0')
        # 2026-11-09 是周一，09:00 已经触发过
        next_at = rrules.next_run_at(rule, at(2026, 11, 9, 9, 0).timestamp())
        self.assertEqual(next_at, at(2026, 11, 23, 9, 0).timestamp())

    def test_monthly_first_day(self):
        rule = rrules.parse('FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0')
        next_at = rrules.next_run_at(rule, at(2026, 1, 15, 12, 0).timestamp())
        self.assertEqual(next_at, at(2026, 2, 1, 9, 0).timestamp())

    def test_monthly_31_clamps_to_last_day(self):
        rule = rrules.parse('FREQ=MONTHLY;BYMONTHDAY=31;BYHOUR=10;BYMINUTE=0')
        next_at = rrules.next_run_at(rule, at(2027, 1, 31, 11, 0).timestamp())
        self.assertEqual(next_at, at(2027, 2, 28, 10, 0).timestamp())

    def test_yearly_fixed_date(self):
        rule = rrules.parse('FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0')
        next_at = rrules.next_run_at(rule, at(2026, 6, 1, 0, 0).timestamp())
        self.assertEqual(next_at, at(2027, 3, 1, 9, 0).timestamp())

    def test_yearly_feb_29_in_leap_year(self):
        rule = rrules.parse('FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;BYHOUR=9;BYMINUTE=0')
        next_at = rrules.next_run_at(rule, at(2028, 3, 1, 0, 0).timestamp())
        self.assertEqual(next_at, at(2029, 2, 28, 9, 0).timestamp())

    def test_hourly_ignores_byminute(self):
        rule = rrules.parse('FREQ=HOURLY;BYMINUTE=30')
        next_at = rrules.next_run_at(rule, at(2026, 11, 11, 15, 26).timestamp())
        self.assertEqual(next_at, at(2026, 11, 11, 16, 0).timestamp())

    def test_valid_until_stops_the_schedule(self):
        rule = rrules.parse('FREQ=DAILY;BYHOUR=9;BYMINUTE=0')
        next_at = rrules.next_run_at(
            rule, at(2026, 11, 11, 10, 0).timestamp(),
            valid_until=at(2026, 11, 11, 23, 0).timestamp())
        self.assertIsNone(next_at)

    def test_valid_from_defers_the_first_run(self):
        rule = rrules.parse('FREQ=DAILY;BYHOUR=9;BYMINUTE=0')
        next_at = rrules.next_run_at(
            rule, at(2026, 11, 11, 10, 0).timestamp(),
            valid_from=at(2026, 11, 20, 0, 0).timestamp())
        self.assertEqual(next_at, at(2026, 11, 20, 9, 0).timestamp())

    def test_once_semantics(self):
        now = at(2026, 11, 11, 10, 0).timestamp()
        self.assertEqual(rrules.once_run_at(at(2026, 11, 12, 9, 0).timestamp(), now=now),
                         at(2026, 11, 12, 9, 0).timestamp())
        self.assertIsNone(rrules.once_run_at(at(2026, 11, 11, 9, 0).timestamp(), now=now))
        self.assertIsNone(rrules.once_run_at(
            at(2026, 11, 12, 9, 0).timestamp(), now=now,
            valid_until=at(2026, 11, 11, 23, 0).timestamp()))


class DescribeTests(unittest.TestCase):
    def test_texts(self):
        cases = {
            'FREQ=DAILY;BYHOUR=9;BYMINUTE=0': '每天 09:00',
            'FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0': '每 2 天 09:00',
            'FREQ=WEEKLY;BYDAY=MO,TH;BYHOUR=9;BYMINUTE=0': '每周周一、周四 09:00',
            'FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;BYHOUR=9;BYMINUTE=0': '每 2 周周一 09:00',
            'FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0': '每月 1 日 09:00',
            'FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0': '每年 3 月 1 日 09:00',
            'FREQ=HOURLY': '每小时',
            'FREQ=HOURLY;INTERVAL=2': '每 2 小时',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(rrules.describe(rrules.parse(text)), expected)


if __name__ == '__main__':
    unittest.main()
