"""serve_dev 的端口占用处理：--force 清场重启、--auto-port 自动换端口。

这些用例都跑在本地 socket 之外（真实监听只在 test_port_in_use_sees_listener 里做），
避免测试真的拉起后端/前端进程。
"""
import socket
import unittest
from unittest.mock import patch

from scripts import serve_dev


class _InterruptingProc:
    """假子进程：第一次轮询就当收到 Ctrl+C，让 main 打印完地址后退出。

    中断标记放在类上：main 会挨个轮询多个子进程，只中断第一次。
    """

    interrupted = False

    def __init__(self, argv):
        self.argv = list(argv)
        self.terminated = False
        self.killed = False

    def poll(self):
        if not _InterruptingProc.interrupted:
            _InterruptingProc.interrupted = True
            raise KeyboardInterrupt
        return 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class PortProbeTests(unittest.TestCase):
    def test_port_in_use_sees_listener(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            self.assertTrue(serve_dev.port_in_use(port))
        self.assertFalse(serve_dev.port_in_use(port))

    def test_first_free_port_skips_busy_and_taken(self):
        with patch.object(serve_dev, 'port_in_use', side_effect=lambda p: p in {8002, 8003}):
            self.assertEqual(serve_dev.first_free_port(8002), 8004)
            self.assertEqual(serve_dev.first_free_port(8002, taken={8004}), 8005)
            self.assertIsNone(serve_dev.first_free_port(8002, span=2))


class ResolvePortsTests(unittest.TestCase):
    def test_auto_port_moves_both_ports(self):
        with patch.object(serve_dev, 'port_in_use', side_effect=lambda p: p in {8001, 8002}):
            resolved = serve_dev.resolve_ports([(8002, '后端'), (8001, '前端代理')], True)
        self.assertEqual(resolved, [(8003, '后端'), (8004, '前端代理')])

    def test_auto_port_keeps_free_ports(self):
        with patch.object(serve_dev, 'port_in_use', return_value=False):
            resolved = serve_dev.resolve_ports([(8002, '后端'), (8001, '前端代理')], True)
        self.assertEqual(resolved, [(8002, '后端'), (8001, '前端代理')])

    def test_without_auto_port_keeps_busy_port(self):
        """没有 --auto-port 时不偷偷换端口：留给后面的占用检查报错。"""
        with patch.object(serve_dev, 'port_in_use', return_value=True):
            resolved = serve_dev.resolve_ports([(8002, '后端')], False)
        self.assertEqual(resolved, [(8002, '后端')])

    def test_auto_port_gives_up_after_span(self):
        with patch.object(serve_dev, 'port_in_use', return_value=True):
            self.assertIsNone(serve_dev.resolve_ports([(8001, '前端代理')], True))


class FreePortTests(unittest.TestCase):
    def test_refuses_foreign_process(self):
        with patch.object(serve_dev, 'listener_pids', return_value=[4242]), \
             patch.object(serve_dev, 'process_command', return_value='/usr/bin/redis-server'), \
             patch.object(serve_dev, 'is_project_process', return_value=False), \
             patch.object(serve_dev, 'stop_process') as stop:
            self.assertFalse(serve_dev.free_port(8001, '前端代理'))
            stop.assert_not_called()

    def test_refuses_when_owner_unknown(self):
        with patch.object(serve_dev, 'listener_pids', return_value=[]), \
             patch.object(serve_dev, 'stop_process') as stop:
            self.assertFalse(serve_dev.free_port(8001, '前端代理', force_any=True))
            stop.assert_not_called()

    def test_stops_project_process(self):
        with patch.object(serve_dev, 'listener_pids', return_value=[4242]), \
             patch.object(serve_dev, 'process_command',
                          return_value='/opt/python -m scripts.serve_frontend'), \
             patch.object(serve_dev, 'is_project_process', return_value=True), \
             patch.object(serve_dev, 'stop_process') as stop, \
             patch.object(serve_dev, 'wait_port_free', return_value=True):
            self.assertTrue(serve_dev.free_port(8001, '前端代理'))
            stop.assert_called_once_with(4242)

    def test_force_any_stops_foreign_process(self):
        with patch.object(serve_dev, 'listener_pids', return_value=[4242]), \
             patch.object(serve_dev, 'process_command', return_value='/usr/bin/redis-server'), \
             patch.object(serve_dev, 'is_project_process', return_value=False), \
             patch.object(serve_dev, 'stop_process') as stop, \
             patch.object(serve_dev, 'wait_port_free', return_value=True):
            self.assertTrue(serve_dev.free_port(8001, '前端代理', force_any=True))
            stop.assert_called_once_with(4242)

    def test_reports_when_port_stays_busy(self):
        with patch.object(serve_dev, 'listener_pids', return_value=[4242]), \
             patch.object(serve_dev, 'process_command', return_value='/opt/python -m scripts.serve_api'), \
             patch.object(serve_dev, 'is_project_process', return_value=True), \
             patch.object(serve_dev, 'stop_process'), \
             patch.object(serve_dev, 'wait_port_free', return_value=False):
            self.assertFalse(serve_dev.free_port(8002, '后端'))


class MainPortHandlingTests(unittest.TestCase):
    def _run(self, argv, spawn):
        _InterruptingProc.interrupted = False
        with patch.object(serve_dev.sys, 'argv', ['serve_dev'] + argv), \
             patch.object(serve_dev.time, 'sleep'), \
             patch.object(serve_dev, 'spawn', side_effect=spawn), \
             patch.object(serve_dev, 'lan_address', return_value='192.168.1.20'), \
             patch.object(serve_dev, 'stop_leftovers'):
            return serve_dev.main()

    def test_force_restarts_when_own_process_holds_port(self):
        state = {8001: True, 8002: False}
        procs = []

        def fake_spawn(label, argv):
            procs.append((label, argv))
            return _InterruptingProc(argv)

        def fake_stop(pid, grace=5.0):
            state[8001] = False
            return True

        with patch.object(serve_dev, 'port_in_use', side_effect=lambda p: state.get(p, False)), \
             patch.object(serve_dev, 'listener_pids', return_value=[4242]), \
             patch.object(serve_dev, 'process_command',
                          return_value='/opt/python -m scripts.serve_frontend'), \
             patch.object(serve_dev, 'is_project_process', return_value=True), \
             patch.object(serve_dev, 'stop_process', side_effect=fake_stop) as stop:
            self.assertEqual(self._run(['--with-frontend', '--force'], fake_spawn), 0)
            stop.assert_called_once_with(4242)
        self.assertEqual([label for label, _ in procs], ['后端', 'Worker', '前端代理'])
        self.assertIn('8001', procs[2][1])

    def test_force_refuses_foreign_holder_and_skips_spawn(self):
        with patch.object(serve_dev, 'port_in_use', side_effect=lambda p: p == 8001), \
             patch.object(serve_dev, 'listener_pids', return_value=[4242]), \
             patch.object(serve_dev, 'process_command', return_value='/usr/bin/redis-server'), \
             patch.object(serve_dev, 'is_project_process', return_value=False), \
             patch.object(serve_dev, 'stop_process') as stop:
            self.assertEqual(self._run(['--with-frontend', '--force'], lambda *a: _InterruptingProc(a[1])), 2)
            stop.assert_not_called()

    def test_busy_port_without_flags_still_exits(self):
        with patch.object(serve_dev, 'port_in_use', side_effect=lambda p: p == 8001):
            self.assertEqual(self._run(['--with-frontend'], lambda *a: _InterruptingProc(a[1])), 2)

    def test_auto_port_moves_frontend_and_reports_real_address(self):
        procs = []

        def fake_spawn(label, argv):
            procs.append((label, argv))
            return _InterruptingProc(argv)

        with patch.object(serve_dev, 'port_in_use', side_effect=lambda p: p == 8001):
            self.assertEqual(self._run(['--with-frontend', '--auto-port', '--no-worker'], fake_spawn), 0)
        self.assertEqual([label for label, _ in procs], ['后端', '前端代理'])
        self.assertEqual(procs[1][1][procs[1][1].index('--port') + 1], '8003')

    def test_force_and_auto_port_conflict(self):
        with patch.object(serve_dev.sys, 'argv', ['serve_dev', '--force', '--auto-port']):
            with self.assertRaises(SystemExit):
                serve_dev.main()

    def test_local_skips_lan_flag(self):
        procs = []

        def fake_spawn(label, argv):
            procs.append((label, argv))
            return _InterruptingProc(argv)

        with patch.object(serve_dev, 'port_in_use', return_value=False):
            self.assertEqual(self._run(['--local', '--no-worker'], fake_spawn), 0)
        self.assertNotIn('--lan', procs[0][1])


if __name__ == '__main__':
    unittest.main()
