"""控制台日志页的契约。

三块东西值得单独锁住，因为它们挂掉的方式都很安静：

1. **环形缓冲的过滤语义**——`level=WARNING` 必须是"WARNING 及以上"，
   写成"只有 WARNING"的话，页面上挑 ERROR 反而什么都看不到；
2. **文件尾部的文本解析**——存量日志没有 `trace=` 这一列，
   只认新格式的话，页面对历史文件会一片空白；
3. **准入**——日志里有 tenant_id / user_id，operator 看到别家企业的日志
   等于跨租户泄露。这条比前两条严重，单独一组用例。

接口层的其余部分（真连库、真起进程）留给手工验收：这一页的输入是内存里的
缓冲和磁盘上的文件，用 TestClient 造这两样东西比直接调函数更绕。
"""
import asyncio
import json
import unittest

from fastapi import HTTPException

from webpage_agent.api import console_api
from webpage_agent.governance.policies import Principal
from webpage_agent.observe.log_buffer import LogBuffer


def _admin():
    return Principal(authenticated=True, role='platform_admin',
                     user_id='u_admin', tenant_id='t-default')


def _body(response):
    return json.loads(response.body)


class LogBufferTests(unittest.TestCase):
    def test_newest_first_and_limit(self):
        buffer = LogBuffer(10)
        for index in range(5):
            buffer.append(level='INFO', name='t', message=f'msg-{index}')
        rows = buffer.entries(limit=3)
        self.assertEqual([row['message'] for row in rows],
                         ['msg-4', 'msg-3', 'msg-2'])

    def test_level_filter_is_a_floor_not_an_equality(self):
        buffer = LogBuffer(10)
        buffer.append(level='INFO', name='t', message='i')
        buffer.append(level='WARNING', name='t', message='w')
        buffer.append(level='ERROR', name='t', message='e')
        self.assertEqual(len(buffer.entries(level='WARNING')), 2)
        self.assertEqual(len(buffer.entries(level='ERROR')), 1)
        self.assertEqual(len(buffer.entries(level='DEBUG')), 3)

    def test_query_matches_message_logger_and_trace(self):
        buffer = LogBuffer(10)
        buffer.append(level='INFO', name='webpage_agent.hub', message='开始执行')
        buffer.append(level='INFO', name='x', message='普通一条', trace='86abcd12')
        self.assertEqual(len(buffer.entries(query='hub')), 1)
        self.assertEqual(len(buffer.entries(query='执行')), 1)
        self.assertEqual(len(buffer.entries(query='86abcd')), 1)
        self.assertEqual(len(buffer.entries(query='没有这条')), 0)

    def test_capacity_drops_oldest_and_counts_it(self):
        buffer = LogBuffer(2)
        for index in range(4):
            buffer.append(level='INFO', name='t', message=str(index))
        stats = buffer.stats()
        self.assertEqual(stats['size'], 2)
        self.assertEqual(stats['dropped'], 2)
        self.assertEqual([row['message'] for row in buffer.entries()], ['3', '2'])

    def test_subscribe_receives_new_entry(self):
        buffer = LogBuffer(10)

        async def run():
            with buffer.subscribe() as queue:
                buffer.append(level='ERROR', name='t', message='实时这条')
                return await asyncio.wait_for(queue.get(), timeout=1)

        entry = asyncio.run(run())
        self.assertEqual(entry['message'], '实时这条')
        # 退出上下文后订阅者要摘干净，否则每次开日志页都会漏一个队列。
        self.assertEqual(len(buffer._subscribers), 0)


class TextLogParseTests(unittest.TestCase):
    def test_dev_format_with_trace(self):
        lines = ['2026-11-13 10:02:57,435 INFO    webpage_agent.api.routes '
                 'trace=86abcd12 http_span method=GET route=/ status=200']
        rows = console_api._parse_lines(lines)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['level'], 'INFO')
        self.assertEqual(rows[0]['logger'], 'webpage_agent.api.routes')
        self.assertEqual(rows[0]['trace'], '86abcd12')
        self.assertIn('http_span', rows[0]['message'])

    def test_legacy_format_without_trace_still_parses(self):
        """存量文件（这次改动之前写的）没有 trace 列，不能因此整行丢掉。"""
        lines = ['2026-11-12 14:15:42,238 WARNING webpage_agent.tools.core '
                 'MCP 服务端 beeftv 工具发现失败']
        rows = console_api._parse_lines(lines)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['level'], 'WARNING')
        self.assertEqual(rows[0]['trace'], '')
        self.assertIn('beeftv', rows[0]['message'])

    def test_traceback_lines_merge_into_previous(self):
        lines = ['2026-11-13 09:00:26,695 ERROR   webpage_agent.api.main '
                 'trace=aaaabbbb 任务 123 执行异常',
                 'Traceback (most recent call last):',
                 '  File "x.py", line 1',
                 'ValueError: boom']
        rows = console_api._parse_lines(lines)
        self.assertEqual(len(rows), 1)
        self.assertIn('Traceback', rows[0]['message'])
        self.assertIn('ValueError: boom', rows[0]['message'])

    def test_json_lines_from_production(self):
        line = json.dumps({'time': '2026-11-13T10:02:57+00:00', 'level': 'ERROR',
                           'logger': 'webpage_agent.api.main',
                           'message': '炸了', 'trace_id': 'ffeeddcc'},
                          ensure_ascii=False)
        rows = console_api._parse_lines([line])
        self.assertEqual(rows[0]['level'], 'ERROR')
        self.assertEqual(rows[0]['trace'], 'ffeeddcc')
        self.assertEqual(rows[0]['message'], '炸了')


class HttpMetricsTests(unittest.TestCase):
    def test_error_rate_and_percentiles(self):
        """控制台卡片上的四个数就是这几个字段，口径错了页面会一直显示 0%。"""
        metrics = console_api.log_buffer_module.HttpMetrics()
        for _ in range(8):
            metrics.record_http(status=200, duration=0.02, route='/api/a')
        metrics.record_http(status=500, duration=2.5, route='/api/b')
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot['window_requests'], 9)
        self.assertEqual(snapshot['window_errors'], 1)
        self.assertAlmostEqual(snapshot['window_error_rate'], round(1 / 9, 4))
        self.assertEqual(snapshot['total_by_class'], {'2xx': 8, '5xx': 1})
        # P95 会把那条 2.5s 的算进尾部，所以一定 >= 2500ms
        self.assertGreaterEqual(snapshot['window_p95_ms'], 2500)
        self.assertEqual(snapshot['window_slow'][0]['route'], '/api/b')

    def test_window_drops_old_events(self):
        """窗口外的旧请求不该继续影响 QPS，否则重启后的第一屏永远是历史均值。"""
        metrics = console_api.log_buffer_module.HttpMetrics(window=0)
        metrics.record_http(status=200, duration=0.01, route='/api/a')
        self.assertEqual(metrics.snapshot()['window_requests'], 0)
        self.assertEqual(metrics.snapshot()['total_since_start'], 1)


class AccessTests(unittest.TestCase):
    def test_admin_can_read_logs(self):
        response = asyncio.run(console_api.console_logs(level='WARNING', q='',
                                                        limit=10, principal=_admin()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_body(response)['source'], 'api')

    def test_member_and_operator_are_refused(self):
        for role in ('member', 'operator'):
            principal = Principal(authenticated=True, role=role, tenant_id='t-x')
            with self.assertRaises(HTTPException) as error:
                asyncio.run(console_api.console_logs(level='', q='', limit=10,
                                                     principal=principal))
            self.assertEqual(error.exception.status_code, 403)

    def test_stream_and_status_are_admin_only(self):
        member = Principal(authenticated=True, role='member', tenant_id='t-x')
        with self.assertRaises(HTTPException):
            asyncio.run(console_api.console_logs_status(principal=member))

        async def run():
            return await console_api.console_logs_stream(
                request=None, level='', q='', principal=member)

        # 准入在建立流之前就判掉，所以这里拿不到 StreamingResponse 而是 403。
        with self.assertRaises(HTTPException):
            asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
