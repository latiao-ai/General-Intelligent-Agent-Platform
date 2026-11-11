"""端到端：会话选择 → 真实 Worker 执行 → 技能被物化进工作目录。

前面几个用例分别验证了"接口能存"和"解析能算"，但真实链路里还有两段：
接纳时能不能一起落库、Worker 领取这一轮时读到的到底是哪份选择。这里用
受控模型（`PI_MOCK_SCRIPT`）跑一次真正的 turn：

- 不联网、不花钱；
- 走的是和 HTTP 入口同一条 `accept_session_message` + `claim_and_execute_turn`；
- 断言落在**磁盘**（`.agent-skills/<session>/<skill>/SKILL.md`）与 turn 答复上，
  而不是只看返回值。
"""
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webpage_agent.addons import skills
from webpage_agent.core.config import settings
from tests.exec_env import PG_DSN, new_store, reset_db

SKILL_MD = """---
name: 事实核查
description: 核对说法是否属实，列出查到的来源
version: 1.0.0
---

# 事实核查
"""


def _mock_answer(text: str) -> str:
    return json.dumps([{'type': 'answer', 'text': text}])


@unittest.skipUnless(PG_DSN, '需要 PG_TEST_DSN 才跑真实 PostgreSQL 用例')
class AddonsTurnEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_db()
        self.store = new_store()
        await self.store.ready()
        suffix = uuid.uuid4().hex[:8]
        self.tenant = f't_{suffix}'
        self.user = f'u_{suffix}'
        self.session = f's_{suffix}'
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.saved = (settings.skills_dir, settings.skills_workspace_dirname)
        object.__setattr__(settings, 'skills_dir', self.root / 'skills')
        object.__setattr__(settings, 'skills_workspace_dirname', '.agent-skills')
        self._saved_workspace_root = settings.workspace_root
        object.__setattr__(settings, 'workspace_root', self.root / 'workspaces')
        skills.install_archive('SKILL.md', SKILL_MD.encode('utf-8'), skill_id='fact-check')

    async def asyncTearDown(self):
        os.environ.pop('PI_MOCK_SCRIPT', None)
        object.__setattr__(settings, 'skills_dir', self.saved[0])
        object.__setattr__(settings, 'skills_workspace_dirname', self.saved[1])
        object.__setattr__(settings, 'workspace_root', self._saved_workspace_root)
        self._tmp.cleanup()
        await self.store.close()

    async def test_turn_materializes_selected_skills(self):
        from webpage_agent.api.api_hub import Hub
        from webpage_agent.api import hub_messages
        from dataclasses import replace

        hub = Hub(self.store)
        principal = SimpleNamespace(tenant_id=self.tenant, user_id=self.user,
                                    role='member', platform_id='', subject='')
        os.environ['PI_MOCK_SCRIPT'] = _mock_answer('已核实：这条说法属实。')
        try:
            with patch.object(hub_messages, 'worker_alive', AsyncMock(return_value=True)), \
                    patch.object(hub_messages, 'settings',
                                 replace(settings, require_live_worker=False)):
                accepted = await hub.accept_session_message(
                    self.session, request_id='req-e2e-1',
                    message='帮我核实这条消息',
                    principal=principal,
                    addon_selection={'expert_id': 'exp-research',
                                     'skill_ids': ['fact-check']})
            self.assertTrue(await hub.claim_and_execute_turn('worker-e2e', 120.0))
            turn = await self.store.turn(accepted['turn_id'])
            self.assertEqual(str(turn.get('status')), 'succeeded')
            self.assertIn('属实', str(turn.get('answer') or ''))
        finally:
            await hub.close()

        # 聊天 turn 没设置过工作目录时用私有根：和 `_run_pi_chat` 里的取法一致。
        from webpage_agent.core import sandbox
        workspace = await self.store.get_workspace(
            tenant_id=self.tenant, owner_user_id=self.user)
        base = Path(workspace) if workspace else sandbox.provision(self.tenant, self.user)
        target = (base / '.agent-skills' / self.session / 'fact-check' / 'SKILL.md')
        self.assertTrue(target.is_file(), f'技能没有物化到 {target}')
        stored = await self.store.session_addons(self.session)
        self.assertEqual(stored.get('expert_id'), 'exp-research')
        self.assertEqual(stored.get('skill_ids'), ['fact-check'])


if __name__ == '__main__':                                  # pragma: no cover
    unittest.main()
