"""连上连接器 → 它的工具在执行口要能调；写操作执行前必须确认。

产品约定（2026-11-11 定）：用户把某个 MCP server 连进会话，就是授权智能体使用
它的工具——读、写都算。问"我有几个画布"要能找到 `canvas.search`；要建节点、出图，
也要能找到 `canvas.nodes.create` / `canvas.generation.submit`，然后**停下来问一句**。
安全边界不在"能不能调"，而在"调之前确认 + 调之后核验"。

真踩过（同一台机器，beeftv）：会话选了 beeftv，中台把 216 个工具全声明给模型，
系统提示还写着「必须先调用本轮已注入的连接器工具」；结果模型问「我有几个画布」时
挨个调 business_get_canvas_projects / canvas_search / business_get_projects，每一个
都返回「工具未开放或不在白名单」——因为执行口的 `resolve_tool` 只认
`PI_READ_TOOL_KEYS`，而里面只有 `mcp:beeftv:canvas.get` 一条。

这里钉住三件事：

1. 注入目录用「选中连接器」当尺子，执行口也必须用同一把尺子（读和写）；
2. `connector_scope` 这个 ContextVar 在装配目录后就被 reset，执行工具时是空的，
   所以选中集合必须**显式**传到 `HostToolCaller` / `PiRuntime`，不能现场读；
3. 确认门禁不能演成假动作：不可执行就别弹确认框，确认之后必须真的执行。
"""
import os
import unittest
from unittest.mock import patch

from webpage_agent.agent import pi_host_tools
from webpage_agent.agent.pi_host_tools import HostToolCaller
from webpage_agent.capabilities.registry import Tool


async def _noop(args, state):
    return {}


def make(name, key, effect='read'):
    return Tool(name, name, name, {'type': 'object'}, _noop,
                tool_key=key, effect=effect, server_id='beeftv')


CANVAS_SEARCH = make('mcp__beeftv__canvas_search', 'mcp:beeftv:canvas.search')
CANVAS_DELETE = make('mcp__beeftv__canvas_node_delete',
                     'mcp:beeftv:canvas.node.delete', effect='write')
CANVAS_UPDATE = make('mcp__beeftv__canvas_node_update',
                     'mcp:beeftv:canvas.node.update', effect='write')
LTV_PROJECTS = make('mcp__libtv__list_projects', 'mcp:libtv:list_projects')
LTV_DELETE = make('mcp__libtv__delete_node', 'mcp:libtv:delete_node',
                  effect='destructive')


class _EnvMixin:
    """把三个白名单环境变量清干净，测完还原——它们会渗进别的用例。"""

    KEYS = ('PI_READ_TOOL_KEYS', 'PI_WRITE_TOOL_KEYS', 'PI_WRITE_TOOLS_ENABLED')

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        for key in self.KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ToolAllowedTests(_EnvMixin, unittest.TestCase):
    def test_read_tool_of_selected_connector_is_allowed(self):
        self.assertTrue(pi_host_tools.tool_allowed(CANVAS_SEARCH,
                                                   connector_ids=['beeftv']))

    def test_same_tool_is_denied_when_nothing_is_selected(self):
        """没选连接器时，不能因为「目录里有」就放行——那是另一把尺子。"""
        self.assertFalse(pi_host_tools.tool_allowed(CANVAS_SEARCH))
        self.assertFalse(pi_host_tools.tool_allowed(CANVAS_SEARCH,
                                                    connector_ids=[]))

    def test_selection_does_not_leak_to_other_servers(self):
        self.assertFalse(pi_host_tools.tool_allowed(LTV_PROJECTS,
                                                    connector_ids=['beeftv']))

    def test_selected_connector_opens_writes_too(self):
        """连上 server 就是授权使用它的工具：读、写都放行。

        安全边界不在"能不能调"，而在"调之前必须确认"——那是 `pi_runtime`
        的确认门禁，不是这里。产品要求的形状：用户想建节点/出图，智能体要能
        找到并调用对应工具，然后停下来问一句。
        """
        self.assertTrue(pi_host_tools.tool_allowed(CANVAS_DELETE,
                                                   connector_ids=['beeftv']))
        self.assertTrue(pi_host_tools.tool_allowed(LTV_DELETE,
                                                   connector_ids=['libtv']))

    def test_write_needs_both_the_gate_and_the_key(self):
        os.environ['PI_WRITE_TOOL_KEYS'] = '["mcp:beeftv:canvas.node.delete"]'
        os.environ['PI_WRITE_TOOLS_ENABLED'] = 'false'
        self.assertFalse(pi_host_tools.tool_allowed(CANVAS_DELETE))
        os.environ['PI_WRITE_TOOLS_ENABLED'] = 'true'
        self.assertTrue(pi_host_tools.tool_allowed(CANVAS_DELETE))

    def test_read_whitelist_still_works_without_selection(self):
        os.environ['PI_READ_TOOL_KEYS'] = '["mcp:beeftv:canvas.search"]'
        self.assertTrue(pi_host_tools.tool_allowed(CANVAS_SEARCH))


class ResolveToolTests(_EnvMixin, unittest.IsolatedAsyncioTestCase):
    def caller(self, connector_ids=None):
        return HostToolCaller(tools_catalog=[CANVAS_SEARCH, CANVAS_DELETE],
                              connector_ids=connector_ids)

    def test_selector_makes_the_injected_read_tool_resolvable(self):
        self.assertIs(self.caller(['beeftv']).resolve_tool(
            'mcp__beeftv__canvas_search'), CANVAS_SEARCH)

    def test_without_selection_it_still_reads_as_not_available(self):
        self.assertIsNone(self.caller().resolve_tool('mcp__beeftv__canvas_search'))

    def test_write_tool_is_resolvable_when_the_connector_is_selected(self):
        self.assertIs(self.caller(['beeftv']).resolve_tool(
            'mcp__beeftv__canvas_node_delete'), CANVAS_DELETE)

    def test_write_tool_stays_unresolvable_without_a_selection(self):
        self.assertIsNone(self.caller().resolve_tool(
            'mcp__beeftv__canvas_node_delete'))


class _Bridge:
    """`_on_tool_call` 只用到 execution_id / send / stderr_tail。"""

    execution_id = 'exec'

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    def stderr_tail(self):
        return ''


class ConfirmationGateTests(_EnvMixin, unittest.IsolatedAsyncioTestCase):
    """写操作一律先确认；确认之后必须真的执行（不能只是"点了个寂寞"）。"""

    TOOL_NAME = 'mcp__beeftv__canvas_node_delete'
    UPDATE_NAME = 'mcp__beeftv__canvas_node_update'

    def runtime(self, tool_name=None, tool_obj=None, connector_ids=None):
        from webpage_agent.agent.pi_runtime import PiRuntime
        tool_name = tool_name or self.TOOL_NAME
        return PiRuntime(
            tools=[{'name': tool_name, 'description': '写', 'parameters':
                    {'type': 'object', 'properties': {}}}],
            tool_objects=[tool_obj or CANVAS_DELETE],
            connector_ids=connector_ids)

    async def _call(self, runtime, tool_name=None):
        from webpage_agent.agent import pi_runtime as module
        events = []

        async def fake_emit(event):
            events.append(event)

        bridge = _Bridge()
        with patch.object(module, 'emit_async', fake_emit):
            message = {'protocol_version': 1, 'execution_id': bridge.execution_id,
                       'type': 'tool_call', 'call_id': 'c1',
                       'tool_name': tool_name or self.TOOL_NAME, 'arguments': {}}
            await runtime._handle_tool_call(bridge, message, {})
        return events, bridge.sent

    async def test_not_connected_tool_says_not_enabled_instead_of_asking(self):
        """没连这个 server 时，别弹一个"确认了也执行不了"的确认框。"""
        runtime = self.runtime()
        runtime._interactive_chat = True
        events, sent = await self._call(runtime)
        self.assertEqual([item for item in events
                          if item.get('type') == 'tool_confirmation_required'], [])
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]['ok'])
        self.assertIn('TOOL_NOT_ENABLED', sent[0]['error'])

    async def test_write_tool_asks_before_executing(self):
        from unittest.mock import AsyncMock
        runtime = self.runtime(connector_ids=['beeftv'])
        runtime._interactive_chat = True
        runtime._host_caller = AsyncMock()
        events, sent = await self._call(runtime)
        self.assertEqual([item for item in events
                          if item.get('type') == 'tool_confirmation_required'],
                         [{'type': 'tool_confirmation_required',
                           'name': self.TOOL_NAME, 'args': {},
                           'reason': '高风险/付费操作，需要用户明确确认'}])
        self.assertIn('HIGH_RISK_CONFIRMATION_REQUIRED', sent[0]['error'])
        runtime._host_caller.call.assert_not_awaited()

    async def test_plain_update_also_asks(self):
        """按名字不算"高危"的写工具（canvas.node.update）同样要先问。"""
        runtime = self.runtime(tool_name=self.UPDATE_NAME, tool_obj=CANVAS_UPDATE,
                               connector_ids=['beeftv'])
        runtime._interactive_chat = True
        events, _sent = await self._call(runtime, tool_name=self.UPDATE_NAME)
        self.assertEqual(len([item for item in events
                              if item.get('type') == 'tool_confirmation_required']), 1)

    def test_confirmation_must_be_a_short_directive(self):
        """长句里的"确认"不是授权——'确认一下我的画布有几个？' 不能当确认。"""
        from webpage_agent.agent.pi_runtime import _is_confirmation
        self.assertTrue(_is_confirmation('确认'))
        self.assertTrue(_is_confirmation('好的，执行吧'))
        self.assertFalse(_is_confirmation('确认一下我的画布有几个？'))
        self.assertFalse(_is_confirmation('帮我创建一个图片节点'))

    async def test_confirmed_write_really_executes(self):
        from unittest.mock import AsyncMock
        from webpage_agent.agent.pi_host_tools import ToolReceipt
        from webpage_agent.agent.pi_runtime import _is_confirmation

        # run() 用用户这句话设置本轮授权：说"确认"才放行。
        self.assertTrue(_is_confirmation('确认，执行吧'))
        runtime = self.runtime(connector_ids=['beeftv'])
        runtime._interactive_chat = True
        runtime._high_risk_approved = True
        caller = AsyncMock()
        caller.call = AsyncMock(return_value=ToolReceipt(ok=True, content={'ok': True}))
        runtime._host_caller = caller
        events, sent = await self._call(runtime)
        self.assertEqual([item for item in events
                          if item.get('type') == 'tool_confirmation_required'], [])
        caller.call.assert_awaited_once()
        self.assertEqual(caller.call.await_args.kwargs['tool_name'], self.TOOL_NAME)
        self.assertTrue(sent[0]['ok'])


if __name__ == '__main__':
    unittest.main()
