"""补充类短句的执行身份回归：chat 也必须拿到 ActionLedger。

复现场景（2026-11-10）：

1. 「帮我查询一下网上关于动漫男主王林的相关事迹」→ 命中 `_QUERY_HINTS`，
   `kind=query`，绑了账本，`web_search` 成功并给出来源；
2. 紧接的补充句「主要是在朱雀星上的相关事情」→ 一个查询词都不含，
   `route_message` 判成 `kind=chat`，但工具目录里照样有 `web_search`。
   原先只有 query 分支建内部执行记录并绑账本，于是模型依据会话历史继续
   调用 `web_search` 时，`governed_call` 返回 `E_NO_LEDGER`
   「没有执行账本，拒绝真实工具调用」。

修复：`_run_pi_chat` 对 chat/query 一视同仁地发放执行身份（两者共用同一套
只读工具目录，且 `create_turn_run` 本来就是为「chat/query turn」设计的）。

这里不依赖真实 PostgreSQL：mock store 走完整 `_run_pi_chat`，直接观察 Pi
执行期间 `current_ledger()` 是否有值、内部执行记录是否被建立与收尾。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.api.hub_messages import HubSessionMessageMixin, route_message


FIRST_MESSAGE = '帮我查询一下网上关于动漫男主王林的相关事迹'
FOLLOWUP_MESSAGE = '主要是在朱雀星上的相关事情'


def _claimed(kind='chat', turn_id='turn-followup-1'):
    return {
        'turn_id': turn_id,
        'message': FOLLOWUP_MESSAGE,
        'session_id': 'sess-1',
        'kind': kind,
        'tenant_id': 'tenant-a',
        'owner_user_id': 'user-u',
        'owner_role': 'member',
        'lease_owner': 'worker-a',
        'lease_epoch': 1,
    }


def _make_hub(observed, *, create_error=None):
    """构造一个只关心执行身份的 hub：Pi runtime 与文件系统全部替身化。"""
    hub = HubSessionMessageMixin()
    hub.store = SimpleNamespace(
        chat_history=AsyncMock(return_value=[]),
        session=AsyncMock(return_value={}),
        create_turn_run=AsyncMock(return_value={
            'run_id': 'turn-followup-1', 'status': 'running'}),
        get_workspace=AsyncMock(return_value='/tmp/ws'),
        settle_turn_run=AsyncMock(return_value=True),
    )
    if create_error is not None:
        hub.store.create_turn_run.side_effect = create_error

    class Runtime:
        async def run(inner, state):
            from webpage_agent.store import ledger as ledger_module
            book = ledger_module.current_ledger()
            observed.append({
                'has_ledger': book is not None,
                'run_id': getattr(book, 'run_id', ''),
                'history': state.get('history'),
                'workspace_dir': state.get('workspace_dir'),
            })
            return {'answer': 'ok'}

    hub._build_pi_runtime = lambda **kwargs: Runtime()
    return hub


class FollowupRoutingTests(unittest.TestCase):
    """固化触发条件：补充句不含查询词，走 chat 而不是 query。

    若将来 `route_message` 升级为「继承上一 turn 意图」，请同步更新本用例。
    """

    def test_first_message_is_query_followup_is_chat(self):
        self.assertEqual(route_message(FIRST_MESSAGE)['kind'], 'query')
        self.assertEqual(
            route_message(FOLLOWUP_MESSAGE)['kind'], 'chat',
            '补充类短句不含 _QUERY_HINTS，会被判成 chat')


class ChatTurnExecutionIdentityTests(unittest.IsolatedAsyncioTestCase):

    async def _run_with_patches(self, hub, claimed):
        with patch('webpage_agent.agent.pi_runtime.default_tool_catalog',
                   return_value=[]), \
                patch('webpage_agent.core.sandbox.provision',
                      return_value='/tmp/ws'), \
                patch('webpage_agent.core.sandbox.stored_path_ok',
                      return_value=True):
            return await hub._run_pi_chat(claimed)

    async def test_chat_and_query_turns_both_bind_ledger(self):
        from webpage_agent.store import ledger as ledger_module

        for kind in ('chat', 'query'):
            with self.subTest(kind=kind):
                observed = []
                hub = _make_hub(observed)
                claimed = _claimed(kind=kind)

                self.assertEqual(await self._run_with_patches(hub, claimed), 'ok')
                self.assertEqual(len(observed), 1)
                self.assertTrue(observed[0]['has_ledger'],
                                f'{kind} turn 必须有账本，否则 web_search 报 E_NO_LEDGER')
                self.assertEqual(observed[0]['run_id'], 'turn-followup-1')
                self.assertEqual(hub.store.create_turn_run.await_count, 1)
                self.assertEqual(hub.store.settle_turn_run.await_count, 1)
                self.assertIsNone(ledger_module.current_ledger(),
                                  '执行结束后必须解绑，账本不能泄漏到下一个 turn')

    async def test_followup_keeps_session_history(self):
        """补充句虽然被判成 chat，会话历史仍带进 Pi（语义层没断）。"""
        observed = []
        hub = _make_hub(observed)
        hub.store.chat_history = AsyncMock(return_value=[
            {'role': 'user', 'content': FIRST_MESSAGE},
            {'role': 'assistant', 'content': '王林，出自《仙逆》…'}])
        await self._run_with_patches(hub, _claimed())
        self.assertEqual(len(observed[0]['history']), 2)
        self.assertTrue(observed[0]['has_ledger'])

    async def test_create_turn_run_failure_does_not_break_chat(self):
        """建行异常不硬阻断闲聊：账本缺失时由工具如实说明能力边界。"""
        from webpage_agent.store import ledger as ledger_module

        observed = []
        hub = _make_hub(observed, create_error=RuntimeError('db down'))
        self.assertEqual(await self._run_with_patches(hub, _claimed()), 'ok')
        self.assertFalse(observed[0]['has_ledger'])
        self.assertEqual(hub.store.settle_turn_run.await_count, 0,
                         '没有建成内部记录就不该去收尾')
        self.assertIsNone(ledger_module.current_ledger())


if __name__ == '__main__':
    unittest.main()
