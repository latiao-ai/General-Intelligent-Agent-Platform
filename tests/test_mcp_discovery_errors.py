"""MCP 发现失败必须说清真实原因；探测脚本要能覆盖 forward_subject 服务端。

背景：anyio 把真实的连接错误（`httpx.ConnectError`、HTTP 401）包进
`ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`。调用路径
早就拆开了这层包装，发现路径没有——于是 `/api/health` 上所有掉线的服务端长得
一模一样，看不出是网络不通还是鉴权被拒。
"""
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from webpage_agent.governance import execution_control
from webpage_agent.store import ledger
from webpage_agent.integrations import mcp_adapter
from webpage_agent.tools import core as tool_module
from scripts import list_mcp_tools


def _group(*leaves):
    """按 anyio 的样子包一层（可以再包一层，模拟任务组套任务组）。"""
    return ExceptionGroup('unhandled errors in a TaskGroup', list(leaves))


def _server(server_id='ops', transport='streamable_http', **extra):
    base = dict(id=server_id, transport=transport, url='http://127.0.0.1:9/mcp',
                token='', stdio_command='', stdio_args=(), default=False)
    base.update(extra)
    return SimpleNamespace(**base)


def _ledger_context(server_ids, *, tenant='customer', platform='vendorx',
                    subject='user-42', run_id='run-discovery'):
    """装一套「可信主体 + 每服务端都有凭据绑定」的执行上下文。

    强制模式（ticket）下发现先按租户/平台/凭据过滤服务，没有这一层就只剩
    "缺少可信执行主体"。dev 模式也用同一份按运行隔离的缓存，所以两种模式
    走的是同一条路径——这也是这条用例以前只在 dev 模式通过的原因。
    """
    from tests.test_mcp_connections import _Store, _cred, _server as _binding_row
    store = _Store(
        [_binding_row(f'sb_{sid}', tenant, sid) for sid in server_ids],
        [_cred(f'cb_{sid}', tenant, 'tenant', tenant, f'sb_{sid}', 'literal:')
         for sid in server_ids])
    for row in store.rows:
        row['scope'] = {'platforms': [platform]}
    store.run = AsyncMock(return_value={
        'tenant_id': tenant, 'owner_user_id': 'owner',
        'platform_id': platform, 'platform_subject': subject})
    context = execution_control.ExecutionContext(run_id, lease_epoch=1)
    book = ledger.ActionLedger(store, run_id, 1, context=context)
    return context, book


class DescribeErrorTests(unittest.TestCase):
    def test_exception_group_is_unwrapped_to_the_real_cause(self):
        exc = _group(_group(httpx.ConnectError('All connection attempts failed')))
        text = mcp_adapter.describe_error(exc)
        self.assertIn('ConnectError', text)
        self.assertIn('All connection attempts failed', text)
        self.assertNotIn('TaskGroup', text)

    def test_plain_exception_keeps_its_own_type(self):
        text = mcp_adapter.describe_error(RuntimeError('ops 掉线'))
        self.assertEqual(text, 'RuntimeError: ops 掉线')

    def test_addresses_are_removed_from_the_message(self):
        exc = _wrap_status_error()
        text = mcp_adapter.describe_error(exc)
        self.assertIn('401', text)
        self.assertNotIn('127.0.0.1', text)
        self.assertNotIn('http://', text)

    def test_empty_message_falls_back_to_the_type_name(self):
        self.assertEqual(mcp_adapter.describe_error(ConnectionResetError()),
                         'ConnectionResetError: ConnectionResetError')

    def test_long_messages_are_cut(self):
        text = mcp_adapter.describe_error(RuntimeError('x' * 500),
                                          limit=mcp_adapter.ERROR_LIMIT)
        self.assertEqual(len(text), mcp_adapter.ERROR_LIMIT)

    def test_wrapped_connect_error_still_means_not_executed(self):
        """拆包之后的类型必须还能用于判定"没执行"，否则会被当成"结果未知"。"""
        leaf = mcp_adapter.unwrap_error(
            _group(httpx.ConnectError('All connection attempts failed')))
        self.assertEqual(mcp_adapter.certainty_of_error(leaf), 'not_executed')


def _wrap_status_error():
    request = httpx.Request('POST', 'http://127.0.0.1:9/mcp')
    response = httpx.Response(401, request=request,
                              text='{"error":"invalid token"}')
    return _group(httpx.HTTPStatusError('Client error', request=request,
                                        response=response))


class DiscoveryErrorTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        # 按运行隔离的缓存是进程级的：用例之间留残余会让别的断言看到上一次的错误。
        tool_module._SCOPED_MCP_CACHES.clear()

    async def test_discovery_reports_the_real_cause_per_server(self):
        """一个掉线、一个正常：errors 里是能看懂的原因，好服务端照常出工具。

        在账本上下文里跑（两种模式同一条路径）：强制模式（ticket）要求发现带
        可信主体，dev 模式也用同一份按运行隔离的缓存——不装上下文的话，
        这条用例在 ticket 模式下会因为"缺少可信执行主体"直接失败。
        """
        async def fake_list(server, **kwargs):
            if server.id == 'ops':
                raise _group(httpx.ConnectError('All connection attempts failed'))
            return [{'name': 'list_sites', 'inputSchema': {'type': 'object'}}]

        servers = [_server('publishing', url='https://a.example/mcp'),
                   _server('ops', url='https://b.example/mcp')]
        context, book = _ledger_context(['publishing', 'ops'])
        with execution_control.use(context), ledger.use_ledger(book), \
                patch.object(tool_module.mcp_servers, 'servers', lambda: servers), \
                patch.object(tool_module.mcp_adapter, 'list_server_tools', fake_list):
            tool_module._MCP_CACHE.update(at=0.0, tools=[], errors={}, signature=())
            tool_module._SCOPED_MCP_CACHES.clear()
            discovered = await tool_module.mcp_tools(force=True)
            status = tool_module.mcp_status()

        self.assertIn('ops', status['errors'])
        self.assertIn('ConnectError', status['errors']['ops'])
        self.assertNotIn('TaskGroup', status['errors']['ops'])
        self.assertNotIn('ops', status['errors'].get('publishing', ''))
        names = [t.name for t in discovered if t.capability == 'mcp']
        self.assertEqual(names, ['mcp__publishing__list_sites'])


class SdkVersionTests(unittest.IsolatedAsyncioTestCase):
    def test_the_pinned_sdk_is_accepted(self):
        """本机装的 1.30.0 与 requirements.lock.txt 一致，不得误报。"""
        self.assertEqual(mcp_adapter.sdk_problem(), '')

    async def test_a_future_major_version_is_refused_with_an_actionable_message(self):
        with patch.object(mcp_adapter, 'sdk_problem',
                          lambda: '已安装 mcp 2.3.0，本客户端按 1.x 的接口编写'):
            with self.assertRaises(mcp_adapter.SDKIncompatible) as caught:
                async with mcp_adapter.connect_session(_server()):
                    pass
        self.assertIn('mcp 2.3.0', str(caught.exception))
        # 一次远端都没发出去：记账必须是"没执行"，不是"结果未知"
        self.assertEqual(mcp_adapter.certainty_of_error(caught.exception),
                         'not_executed')

    async def test_connect_refuses_before_touching_the_transport(self):
        """版本不对时不许先起子进程再报错。"""
        calls = []

        def spawn(params):
            calls.append(params)
            raise AssertionError('不该走到 stdio_client')

        with patch.object(mcp_adapter, 'sdk_problem', lambda: '版本不对'), \
                patch.object(mcp_adapter, 'stdio_client', spawn):
            with self.assertRaises(RuntimeError) as caught:
                async with mcp_adapter.connect_session(_server(transport='stdio')):
                    pass
        self.assertIn('版本不对', str(caught.exception))
        self.assertEqual(calls, [])


class ProbeScriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_subject_server_without_subject_says_what_to_do(self):
        async def fake_list(server):        # 不该被调用：没有主体就不建连
            raise AssertionError('没有主体时不应尝试连接')

        servers = [_server('beeftv', transport='stdio', stdio_command='beeftv',
                           forward_subject=True)]
        with patch.object(list_mcp_tools.mcp_servers, 'servers', lambda: servers), \
                patch.object(list_mcp_tools, 'list_server_tools', fake_list):
            out = await list_mcp_tools.discover_all()
        self.assertIn('--subject', out['beeftv']['error'])
        self.assertEqual(out['beeftv']['transport'], 'stdio')
        self.assertNotIn('tools', out['beeftv'])

    async def test_subject_puts_a_trusted_run_in_scope(self):
        seen = {}

        async def fake_list(server):
            from webpage_agent.governance import execution_control
            from webpage_agent.store import ledger
            book = ledger.current_ledger()
            context = execution_control.current()
            run = await book.store.run(context.run_id)
            seen['subject'] = run['platform_subject']
            seen['has_ledger'] = book is not None
            return [{'name': 'canvas.get', 'inputSchema': {'type': 'object'}}]

        servers = [_server('beeftv', transport='stdio', stdio_command='beeftv',
                           forward_subject=True)]
        with patch.object(list_mcp_tools.mcp_servers, 'servers', lambda: servers), \
                patch.object(list_mcp_tools, 'list_server_tools', fake_list):
            out = await list_mcp_tools.discover_all(subject='alice')

        self.assertTrue(seen['has_ledger'])
        self.assertEqual(seen['subject'], 'alice')
        self.assertEqual([t['name'] for t in out['beeftv']['tools']], ['canvas.get'])
        # 上下文只在本脚本内生效：退出后不留残留
        from webpage_agent.governance import execution_control
        from webpage_agent.store import ledger
        self.assertIsNone(ledger.current_ledger())
        self.assertIsNone(execution_control.current())

    def test_exit_code_is_non_zero_when_any_server_fails(self):
        async def fake_discover_all(subject=''):
            return {'a': {'transport': 'sse', 'url': 'http://x', 'tools': []},
                    'b': {'transport': 'sse', 'url': 'http://y', 'error': 'x'}}

        servers = [_server('a'), _server('b')]
        with patch.object(list_mcp_tools.mcp_servers, 'servers', lambda: servers), \
                patch.object(list_mcp_tools, 'discover_all', fake_discover_all), \
                redirect_stdout(StringIO()):
            self.assertEqual(list_mcp_tools.main([]), 1)

    def test_no_configured_server_is_a_failure_not_a_silent_zero(self):
        with patch.object(list_mcp_tools.mcp_servers, 'servers', lambda: []), \
                redirect_stdout(StringIO()):
            self.assertEqual(list_mcp_tools.main([]), 1)

    def test_output_stays_json(self):
        async def fake_discover_all(subject=''):
            return {'a': {'transport': 'sse', 'url': 'http://x', 'tools': []}}

        servers = [_server('a')]
        with patch.object(list_mcp_tools.mcp_servers, 'servers', lambda: servers), \
                patch.object(list_mcp_tools, 'discover_all', fake_discover_all), \
                patch('sys.stdout') as out:
            self.assertEqual(list_mcp_tools.main([]), 0)
        printed = ''.join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(json.loads(printed)['a']['transport'], 'sse')


if __name__ == '__main__':
    unittest.main()
