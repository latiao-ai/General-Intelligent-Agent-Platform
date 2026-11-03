"""answer_directly 必须看得见本轮本地工具取得的正文（B.5 回归）。

复现的问题
----------
真实运行：`帮我读一下 https://www.sqlite.org/whentouse.html ... 整理成中文网页`
规划成 `fetch_pages → render_sources_html → answer_directly`，三步全部成功
（正文 14908 字符、HTML 17081 字节都已落盘），但最后一步对用户说：

    “我无法读取网页内容：本轮没有联网检索能力……”

原因
----
`base.tool_results_text()` 只遍历 `state['mcp_calls']`，而 `fetch_pages` /
`render_sources_html` 是本地能力包（tool_key 以 `local:` 开头），**从不进
mcp_calls**。所以 `tool_results_text()` 返回空串，`_answer_directly` 落进
`ANSWER_SYSTEM`（“本轮没有做任何联网检索”）分支。

`answer_with_sources` / `render_sources_html` 都有 `usable_documents()` 这一道
兜底，所以没坏；`answer_directly` 是唯一没有兜底的作答路径。

修复的边界
----------
`answer_directly` 的语义是“**不主动检索**、用模型自己的知识回答”——这条不变：
本轮真没有正文时，仍然走 `ANSWER_SYSTEM`（见 `..._still_uses_own_knowledge`）。
要修的是另一件事：**本轮明明有正文，就不能宣称没有**。
"""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from webpage_agent.core import sources
from webpage_agent.capabilities import answers as answers_cap
from webpage_agent.capabilities import export as export_cap
from webpage_agent.capabilities import publishing, web


def patch_all_emits(sink):
    """能力包各自持有 emit 引用，测试里需要一起打桩。"""
    stack = ExitStack()
    for module in (answers_cap, export_cap, publishing, web):
        stack.enter_context(patch.object(module, 'emit', sink))
    return stack


def _capture_chat(captured, reply='结论。'):
    async def fake_chat(system, messages, model=None, on_delta=None):
        captured['system'] = system
        captured['messages'] = messages
        return reply
    return fake_chat


class AnswerDirectlySeesLocalResultsTests(unittest.IsolatedAsyncioTestCase):
    """本地工具的结果也必须进得了作答步骤。"""

    async def test_answer_directly_sees_documents_from_local_tools(self):
        """fetch_pages 取得正文后，answer_directly 不能再说“没有联网”。"""
        captured = {}
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sources, 'settings',
                              SimpleNamespace(artifact_dir=Path(folder))):
                doc = sources.metadata(
                    't1', 's1', url='https://www.sqlite.org/whentouse.html',
                    title='何时使用 SQLite',
                    text='SQLite 不参与 client/server 竞争，它是嵌入式数据库引擎。')
                state = {'user_input': '整理成中文网页', 'history': [],
                         'documents': [doc], 'mcp_calls': []}
                with patch.object(answers_cap, 'chat_reply',
                                  _capture_chat(captured)), \
                        patch_all_emits(lambda event: None):
                    result = await answers_cap._answer_directly(
                        {'question': '整理成中文网页'}, state)

        self.assertEqual(result['status'], 'answered')
        # 核心断言：不能再声称“本轮没有做任何联网检索”
        self.assertNotIn('没有做任何联网检索', captured['system'])
        # 正文必须真的进了模型输入
        blob = json.dumps(captured['messages'], ensure_ascii=False)
        self.assertIn('client/server', blob)
        self.assertIn('何时使用 SQLite', blob)

    async def test_answer_directly_still_uses_own_knowledge_without_documents(self):
        """没有正文时仍走“用自身知识作答”——原语义不能被顺手改掉。"""
        captured = {}
        state = {'user_input': '什么是 ACID', 'history': [],
                 'documents': [], 'mcp_calls': []}
        with patch.object(answers_cap, 'chat_reply', _capture_chat(captured)), \
                patch_all_emits(lambda event: None):
            result = await answers_cap._answer_directly(
                {'question': '什么是 ACID'}, state)

        self.assertEqual(result['status'], 'answered')
        self.assertIn('没有做任何联网检索', captured['system'])

    async def test_answer_directly_prefers_mcp_tool_results(self):
        """有 MCP 调用回执时，仍优先用工具数据分支（防回归）。"""
        captured = {}
        state = {'user_input': '服务器上有几个站点', 'history': [],
                 'mcp_calls': [{'tool': 'mcp__beeftv__list_sites',
                                'args': {'keyword': 'x'},
                                'payload': {'sites': [{'id': 1}]}}]}
        with patch.object(answers_cap, 'chat_reply', _capture_chat(captured)), \
                patch_all_emits(lambda event: None):
            result = await answers_cap._answer_directly(
                {'question': '服务器上有几个站点'}, state)

        self.assertEqual(result['status'], 'answered')
        self.assertIn('工具返回的真实数据', captured['system'])
        blob = json.dumps(captured['messages'], ensure_ascii=False)
        self.assertIn('list_sites', blob)


if __name__ == '__main__':
    unittest.main()
