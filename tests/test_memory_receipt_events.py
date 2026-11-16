"""抽取回执：记住了要说，没记住更要说。

`my 画像` 这一路最难查的不是"没写进去"，而是**没写进去还看不出来**：
模型的回答里写着"已记录"，`extract_user_memory` 那边失败或整轮被跳过，
界面上一个字都没有，用户只能自己打开画像面板才发现对不上账。

这里用假 store 走完整的 `_run_pi_chat`，锁住三件事：

1. 画像真变了 → `memory_learned`（已有行为，防回归）；
2. 抽取失败/超时 → `memory_extract_failed`，带一句人话；
3. 抽到空结果（这轮没什么值得记的）→ **不写事件**，否则每句寒暄都弹警告。
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.api.hub_messages import HubSessionMessageMixin
from webpage_agent.core.config import settings
from webpage_agent.core.memory import ExtractResult


def _claimed():
    return {
        'turn_id': 'turn-receipt-1',
        'message': '我来自浙江台州',
        'session_id': 'sess-receipt',
        'kind': 'chat',
        'tenant_id': 'tenant-a',
        'owner_user_id': 'user-u',
        'owner_role': 'member',
        'lease_owner': 'worker-a',
        'lease_epoch': 1,
    }


class _Runtime:
    async def run(self, state, *, on_event=None):
        return {**state, 'answer': '已记录：唐浩南，来自浙江台州。'}


class ChatMemoryReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.saved_profile_path = settings.profile_path
        self.saved_skills_dir = settings.skills_dir
        self.saved_mode = settings.memory_extract_mode
        object.__setattr__(settings, 'profile_path', self.root / 'USER.md')
        object.__setattr__(settings, 'skills_dir', self.root / 'skills')
        # 每轮都要跑抽取，才谈得上"失败要播报"
        object.__setattr__(settings, 'memory_extract_mode', 'always')

    async def asyncTearDown(self):
        object.__setattr__(settings, 'profile_path', self.saved_profile_path)
        object.__setattr__(settings, 'skills_dir', self.saved_skills_dir)
        object.__setattr__(settings, 'memory_extract_mode', self.saved_mode)
        self._tmp.cleanup()

    async def run_turn(self, outcome):
        workspace = self.root / 'ws'
        workspace.mkdir(parents=True, exist_ok=True)
        events: list[dict] = []
        hub = HubSessionMessageMixin()
        hub.store = SimpleNamespace(
            chat_history=AsyncMock(return_value=[]),
            session=AsyncMock(return_value={'session_id': 'sess-receipt',
                                            'connector_ids_json': '[]'}),
            session_addons=AsyncMock(return_value={}),
            create_turn_run=AsyncMock(return_value={'run_id': 'turn-receipt-1',
                                                    'status': 'running'}),
            settle_turn_run=AsyncMock(return_value=True),
            get_workspace=AsyncMock(return_value=str(workspace)),
            recall=AsyncMock(return_value=[]),
            memory=AsyncMock(return_value={'summary': '', 'facts': {}, 'upto': 0}),
            messages_after=AsyncMock(return_value=[]),
            append_turn_event=AsyncMock(
                side_effect=lambda _turn_id, event: events.append(event)),
        )
        hub._build_pi_runtime = lambda **kwargs: _Runtime()
        with patch('webpage_agent.api.hub_messages.memory_module.extract_user_memory',
                   AsyncMock(return_value=outcome)), \
                patch('webpage_agent.agent.pi_runtime.default_tool_catalog',
                      return_value=[]), \
                patch('webpage_agent.core.sandbox.provision',
                      return_value=workspace), \
                patch('webpage_agent.core.sandbox.stored_path_ok',
                      return_value=True):
            await hub._run_pi_chat(_claimed())
        return [event for event in events
                if str(event.get('type') or '').startswith('memory_')]

    async def test_learned_facts_are_announced(self):
        events = await self.run_turn(ExtractResult(
            saved=1, learned={'籍贯': '浙江台州'}))
        self.assertEqual([e['type'] for e in events], ['memory_learned'])
        self.assertEqual(events[0]['detail'], {'籍贯': '浙江台州'})

    async def test_failure_is_announced_with_reason(self):
        """失败不再是 debug 日志里的一声闷响：用户得看得见"这轮没记住"。"""
        events = await self.run_turn(ExtractResult(error='记忆抽取超时'))
        self.assertEqual([e['type'] for e in events], ['memory_extract_failed'])
        self.assertEqual(events[0]['detail'], {'reason': '记忆抽取超时'})

    async def test_empty_extraction_stays_quiet(self):
        """这轮本来就没有值得记的，不能报成失败——否则寒暄一句弹一次警告。"""
        events = await self.run_turn(ExtractResult(saved=0, learned={}))
        self.assertEqual(events, [])

    async def test_learned_wins_over_error(self):
        """改口回写成功的同时带了残余错误：说"记住了"比说"没记住"更接近事实。"""
        events = await self.run_turn(ExtractResult(
            saved=1, learned={'称呼': '南哥'}, error='记忆抽取超时'))
        self.assertEqual([e['type'] for e in events], ['memory_learned'])


if __name__ == '__main__':                                  # pragma: no cover
    unittest.main()
