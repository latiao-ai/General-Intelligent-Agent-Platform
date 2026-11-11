"""聊天 turn 真的把"会话选择"交给模型：专家人设 + 技能索引 + 技能文件。

`addons` 的三块里最容易假的是**接线**：接口能存、解析能算，但聊天那一路没接上，
表现就是"我选了专家/技能，它还是老样子"。这里用一个只实现被调用方法的假 store
走完整的 `_run_pi_chat`，直接观察交给 Pi 的系统提示与工作目录。

不碰真实模型、不碰真实 PostgreSQL：执行身份那段由 `test_chat_turn_execution_identity`
覆盖，这里只关心 addons 有没有真正进入本轮执行。
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.addons import skills
from webpage_agent.api.hub_messages import HubSessionMessageMixin
from webpage_agent.core.config import settings

SKILL_MD = """---
name: 事实核查
description: 核对说法是否属实，列出查到的来源
version: 1.0.0
---

# 事实核查
"""


def _claimed():
    return {
        'turn_id': 'turn-addon-1',
        'message': '帮我核实这条消息',
        'session_id': 'sess-addon',
        'kind': 'chat',
        'tenant_id': 'tenant-a',
        'owner_user_id': 'user-u',
        'owner_role': 'member',
        'lease_owner': 'worker-a',
        'lease_epoch': 1,
    }


def _make_hub(addons: dict, captured: dict, workspace: str):
    hub = HubSessionMessageMixin()
    hub.store = SimpleNamespace(
        chat_history=AsyncMock(return_value=[]),
        session=AsyncMock(return_value={'session_id': 'sess-addon',
                                        'connector_ids_json': '[]'}),
        session_addons=AsyncMock(return_value=addons),
        create_turn_run=AsyncMock(return_value={'run_id': 'turn-addon-1',
                                                'status': 'running'}),
        settle_turn_run=AsyncMock(return_value=True),
        get_workspace=AsyncMock(return_value=workspace),
        recall=AsyncMock(return_value=[]),
    )
    hub._build_pi_runtime = lambda **kwargs: captured.update(kwargs) or _Runtime()
    return hub


class _Runtime:
    async def run(self, state, *, on_event=None):
        return {**state, 'answer': 'ok'}


class AddonsChatInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.saved = settings.skills_dir
        object.__setattr__(settings, 'skills_dir', self.root / 'skills')
        object.__setattr__(settings, 'skills_workspace_dirname', '.agent-skills')
        skills.install_archive('SKILL.md', SKILL_MD.encode('utf-8'), skill_id='fact-check')

    async def asyncTearDown(self):
        object.__setattr__(settings, 'skills_dir', self.saved)
        self._tmp.cleanup()

    async def run_turn(self, addons):
        workspace = str(self.root / 'ws')
        Path(workspace).mkdir(parents=True, exist_ok=True)
        captured: dict = {}
        hub = _make_hub(addons, captured, workspace)
        with patch('webpage_agent.agent.pi_runtime.default_tool_catalog',
                   return_value=[]), \
                patch('webpage_agent.core.sandbox.provision',
                      return_value=Path(workspace)), \
                patch('webpage_agent.core.sandbox.stored_path_ok',
                      return_value=True):
            answer = await hub._run_pi_chat(_claimed())
        return answer, captured, workspace

    async def test_expert_and_skill_reach_system_prompt(self):
        answer, captured, workspace = await self.run_turn(
            {'expert_id': 'exp-report', 'skill_ids': ['fact-check']})
        self.assertEqual(answer, 'ok')
        prompt = captured.get('system_prompt') or ''
        self.assertIn('报告写作专家', prompt)                 # 专家人设
        self.assertIn('事实核查', prompt)                     # 技能索引
        self.assertIn('.agent-skills/sess-addon/fact-check/SKILL.md', prompt)
        # 技能包真的落进工作目录，模型才能 read_file
        self.assertTrue((Path(workspace) / '.agent-skills' / 'sess-addon'
                         / 'fact-check' / 'SKILL.md').is_file())
        self.assertEqual(captured.get('connector_ids'), [])

    async def test_no_selection_says_no_skill(self):
        _answer, captured, workspace = await self.run_turn({})
        prompt = captured.get('system_prompt') or ''
        self.assertIn('没有勾选技能', prompt)
        self.assertNotIn('报告写作专家', prompt)
        self.assertFalse((Path(workspace) / '.agent-skills').exists())

    async def test_explicit_connectors_reach_runtime(self):
        _answer, captured, _workspace = await self.run_turn(
            {'connector_ids': ['beeftv']})
        self.assertEqual(captured.get('connector_ids'), ['beeftv'])


if __name__ == '__main__':                                  # pragma: no cover
    unittest.main()
