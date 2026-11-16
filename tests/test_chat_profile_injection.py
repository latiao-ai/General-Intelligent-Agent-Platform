"""聊天 turn 必须认识"这个人是谁"。

`我的画像` 以前只在任务规划链路注入（nodes / answers），闲聊一路完全没拼过：
用户在画像面板写完偏好，回到对话框一问，模型还是不知道。

这里用假 store 走完整的 `_run_pi_chat`，直接看交给 Pi 的系统提示，锁住两件事：

1. 画像按 `owner_user_id` 读取（不是 default 那份）；
2. 与画像同键的长期记忆不再重复出现，避免"同一条偏好两个值"。
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.api.hub_messages import HubSessionMessageMixin
from webpage_agent.core.config import settings

PROFILE_TEXT = """# User Profile
> Last updated: 2026-11-12T00:00:00+00:00
> Version: 3

称呼：南哥
偏好：先给结论
"""


def _claimed():
    return {
        'turn_id': 'turn-profile-1',
        'message': '你还记得怎么称呼我吗',
        'session_id': 'sess-profile',
        'kind': 'chat',
        'tenant_id': 'tenant-a',
        'owner_user_id': 'user-u',
        'owner_role': 'member',
        'lease_owner': 'worker-a',
        'lease_epoch': 1,
    }


class _Runtime:
    async def run(self, state, *, on_event=None):
        return {**state, 'answer': 'ok'}


class ChatProfileInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.saved_profile_path = settings.profile_path
        self.saved_skills_dir = settings.skills_dir
        object.__setattr__(settings, 'profile_path', self.root / 'USER.md')
        object.__setattr__(settings, 'skills_dir', self.root / 'skills')
        # 画像按主体落盘：user-u 有自己的文件
        users_dir = self.root / 'users'
        users_dir.mkdir(parents=True, exist_ok=True)
        (users_dir / 'user-u.md').write_text(PROFILE_TEXT, encoding='utf-8')

    async def asyncTearDown(self):
        object.__setattr__(settings, 'profile_path', self.saved_profile_path)
        object.__setattr__(settings, 'skills_dir', self.saved_skills_dir)
        self._tmp.cleanup()

    async def run_turn(self, user_rows):
        workspace = self.root / 'ws'
        workspace.mkdir(parents=True, exist_ok=True)
        captured: dict = {}
        hub = HubSessionMessageMixin()
        hub.store = SimpleNamespace(
            chat_history=AsyncMock(return_value=[]),
            session=AsyncMock(return_value={'session_id': 'sess-profile',
                                            'connector_ids_json': '[]'}),
            session_addons=AsyncMock(return_value={}),
            create_turn_run=AsyncMock(return_value={'run_id': 'turn-profile-1',
                                                    'status': 'running'}),
            settle_turn_run=AsyncMock(return_value=True),
            get_workspace=AsyncMock(return_value=str(workspace)),
            recall=AsyncMock(side_effect=lambda scope, scope_id:
                             user_rows if scope == 'user' else []),
            memory=AsyncMock(return_value={'summary': '', 'facts': {}, 'upto': 0}),
            messages_after=AsyncMock(return_value=[]),
        )
        hub._build_pi_runtime = lambda **kwargs: captured.update(kwargs) or _Runtime()
        with patch('webpage_agent.agent.pi_runtime.default_tool_catalog',
                   return_value=[]), \
                patch('webpage_agent.core.sandbox.provision',
                      return_value=workspace), \
                patch('webpage_agent.core.sandbox.stored_path_ok',
                      return_value=True):
            answer = await hub._run_pi_chat(_claimed())
        return answer, captured.get('system_prompt') or ''

    async def test_profile_reaches_chat_system_prompt(self):
        answer, prompt = await self.run_turn([])
        self.assertEqual(answer, 'ok')
        self.assertIn('[用户画像]', prompt)
        self.assertIn('称呼：南哥', prompt)
        self.assertIn('不要逐字复述', prompt)

    async def test_profile_key_not_duplicated_in_memory(self):
        _answer, prompt = await self.run_turn([
            {'predicate': '称呼', 'value': '浩南'},        # 与画像同键，应被画像覆盖
            {'predicate': '回复语言', 'value': '中文'},
        ])
        self.assertIn('称呼：南哥', prompt)
        self.assertNotIn('称呼：浩南', prompt)
        self.assertIn('回复语言：中文', prompt)


if __name__ == '__main__':                                  # pragma: no cover
    unittest.main()
