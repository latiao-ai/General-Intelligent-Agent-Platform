"""运行时边界测试（Pi 改造第一阶段）。

验证 `agent.runtime.Runtime` 契约与 `agent.langgraph_runtime.LangGraphRuntime` 的
行为：驱动图执行、合并状态增量、转发 custom 事件、记录节点耗时，以及异常原样穿透。
同时验证 `run_task()` 经新运行时接口后行为不回归（首次执行 + 恢复执行）。
"""
import tempfile
import unittest

from unittest.mock import patch

from tests.core_support import reset_db


class LangGraphRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """LangGraphRuntime 的驱动与事件透传边界。"""

    def runtime(self):
        from webpage_agent.agent.langgraph_runtime import LangGraphRuntime
        return LangGraphRuntime()

    def test_satisfies_runtime_protocol(self):
        """LangGraphRuntime 应当实现 Runtime 契约。"""
        from webpage_agent.agent.runtime import Runtime
        from webpage_agent.agent.langgraph_runtime import LangGraphRuntime
        self.assertIsInstance(LangGraphRuntime(), Runtime)

    async def test_merges_updates_and_forwards_custom_events(self):
        """updates 增量要合并进 state，custom 事件要透传给回调。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool

        received = []

        async def fake_answer(args, state):
            return {'answer': '你好', 'status': 'answered'}

        table = dict(tool_module.BUILTIN_BY_NAME)
        table['answer_directly'] = Tool('answer_directly', '直接回答', 'd', {},
                                        fake_answer)

        async def fake_catalog(context=None):
            return list(table.values())

        with tempfile.TemporaryDirectory() as folder:
            state = {
                'task_id': 'x', 'user_input': '你好', 'documents': [],
                'errors': [], 'usage': [], 'step_index': 0, 'mcp_calls': [],
                'timings': [], 'artifact_dir': folder,
            }
            with patch.dict(tool_module.BUILTIN_BY_NAME, table), \
                    patch.object(tool_module, 'catalog', fake_catalog):
                async def on_event(event):
                    received.append(event)

                result = await self.runtime().run(state, on_event=on_event)

        from webpage_agent.core import outcome
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(outcome.label(result['status'], result), 'answered')
        # 阶段起止事件（节点内部发出）应通过 custom 流转发出来
        self.assertTrue(any(e.get('type') == 'stage_start' for e in received))
        self.assertTrue(any(e.get('type') == 'stage_end' for e in received))
        # 节点耗时应当被记录
        self.assertTrue(result['timings'])

    async def test_progress_callback_is_called_with_node_and_elapsed(self):
        """节点级进度回调要收到 (node, status, elapsed)。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool

        async def fake_answer(args, state):
            return {'answer': 'hi', 'status': 'answered'}

        table = dict(tool_module.BUILTIN_BY_NAME)
        table['answer_directly'] = Tool('answer_directly', '直接回答', 'd', {},
                                        fake_answer)

        async def fake_catalog(context=None):
            return list(table.values())

        progress_calls = []

        with tempfile.TemporaryDirectory() as folder:
            state = {
                'task_id': 'x', 'user_input': 'hi', 'documents': [],
                'errors': [], 'usage': [], 'step_index': 0, 'mcp_calls': [],
                'timings': [], 'artifact_dir': folder,
            }
            with patch.dict(tool_module.BUILTIN_BY_NAME, table), \
                    patch.object(tool_module, 'catalog', fake_catalog):
                await self.runtime().run(
                    state, progress=lambda node, status, elapsed:
                    progress_calls.append((node, status, elapsed)))
        self.assertTrue(progress_calls)
        self.assertTrue(all(isinstance(c[2], float) for c in progress_calls))

    async def test_control_exceptions_pass_through(self):
        """控制流异常必须原样穿透，不能被运行时吞掉或包装。"""
        from webpage_agent.governance import execution_control

        class _StubGraph:
            def astream(self, *args, **kwargs):
                async def _gen():
                    raise execution_control.ControlStopped('停')
                    yield
                return _gen()

        with tempfile.TemporaryDirectory() as folder:
            state = {'task_id': 'x', 'artifact_dir': folder, 'timings': []}
            with patch('webpage_agent.agent.langgraph_runtime.build_graph',
                       return_value=_StubGraph()):
                with self.assertRaises(execution_control.ControlStopped):
                    await self.runtime().run(state)

    async def test_sync_and_async_event_callbacks_both_supported(self):
        """事件回调协议：同步回调按普通函数调用，异步回调则 await，两者都收到事件。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool

        async def fake_answer(args, state):
            return {'answer': 'hi', 'status': 'answered'}

        table = dict(tool_module.BUILTIN_BY_NAME)
        table['answer_directly'] = Tool('answer_directly', '直接回答', 'd', {},
                                        fake_answer)

        async def fake_catalog(context=None):
            return list(table.values())

        # 同步回调：返回 None（非 awaitable），运行时按普通函数调用
        sync_received = []

        def sync_cb(event):
            sync_received.append(event)

        # 异步回调：返回协程，运行时 await
        async_received = []

        async def async_cb(event):
            async_received.append(event)

        with tempfile.TemporaryDirectory() as folder:
            state = {
                'task_id': 'x', 'user_input': 'hi', 'documents': [],
                'errors': [], 'usage': [], 'step_index': 0, 'mcp_calls': [],
                'timings': [], 'artifact_dir': folder,
            }
            with patch.dict(tool_module.BUILTIN_BY_NAME, table), \
                    patch.object(tool_module, 'catalog', fake_catalog):
                await self.runtime().run(state, on_event=sync_cb)
                await self.runtime().run(state, on_event=async_cb)

        self.assertTrue(sync_received)
        self.assertTrue(async_received)
        self.assertTrue(any(e.get('type') == 'stage_start' for e in sync_received))
        self.assertTrue(any(e.get('type') == 'stage_start' for e in async_received))

    async def test_event_callback_exception_is_not_swallowed(self):
        """回调内部抛出的异常必须原样向上传播，不能被静默吞掉。"""
        from webpage_agent.tools import core as tool_module
        from webpage_agent.tools.core import Tool

        async def fake_answer(args, state):
            return {'answer': 'hi', 'status': 'answered'}

        table = dict(tool_module.BUILTIN_BY_NAME)
        table['answer_directly'] = Tool('answer_directly', '直接回答', 'd', {},
                                        fake_answer)

        async def fake_catalog(context=None):
            return list(table.values())

        class _CallbackBoom(Exception):
            pass

        async def boom(event):
            raise _CallbackBoom('回调炸了')

        with tempfile.TemporaryDirectory() as folder:
            state = {
                'task_id': 'x', 'user_input': 'hi', 'documents': [],
                'errors': [], 'usage': [], 'step_index': 0, 'mcp_calls': [],
                'timings': [], 'artifact_dir': folder,
            }
            with patch.dict(tool_module.BUILTIN_BY_NAME, table), \
                    patch.object(tool_module, 'catalog', fake_catalog):
                with self.assertRaises(_CallbackBoom):
                    await self.runtime().run(state, on_event=boom)

    async def test_recursion_budget_maps_to_generic_exception(self):
        """LangGraph 的 GraphRecursionError 应在适配层折成通用 RecursionBudgetExceeded。"""
        from langgraph.errors import GraphRecursionError
        from webpage_agent.core.errors import RecursionBudgetExceeded

        class _StubGraph:
            def astream(self, *args, **kwargs):
                async def _gen():
                    raise GraphRecursionError('recursion limit')
                    yield
                return _gen()

        with tempfile.TemporaryDirectory() as folder:
            state = {'task_id': 'x', 'artifact_dir': folder, 'timings': []}
            with patch('webpage_agent.agent.langgraph_runtime.build_graph',
                       return_value=_StubGraph()):
                with self.assertRaises(RecursionBudgetExceeded):
                    await self.runtime().run(state)

    async def test_state_keeps_partial_updates_on_exception(self):
        """异常发生时，运行时已合并进 state 的部分增量必须保留（同一 dict 引用）。"""
        from webpage_agent.governance import execution_control

        class _StubGraph:
            def astream(self, *args, **kwargs):
                async def _gen():
                    # 先产出一个节点增量（真实 updates 模式下 chunk 是 {node: dict}），
                    # 再抛控制流异常
                    yield 'updates', {'plan_node': {
                        'plan': {'steps': [{'step_id': 's1'}]},
                        'status': 'planned'}}
                    raise execution_control.ControlStopped('停')
                return _gen()

        with tempfile.TemporaryDirectory() as folder:
            state = {'task_id': 'x', 'artifact_dir': folder, 'timings': [],
                     'status': 'created'}
            with patch('webpage_agent.agent.langgraph_runtime.build_graph',
                       return_value=_StubGraph()):
                with self.assertRaises(execution_control.ControlStopped):
                    await self.runtime().run(state)
            # 同一份 state 引用里仍保留异常前的部分增量
            self.assertEqual(state['status'], 'planned')
            self.assertIn('plan', state)


class RunTaskRuntimeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """run_task 经新运行时接口后行为不回归。"""

    async def asyncSetUp(self):
        await reset_db()

    async def test_run_task_uses_runtime_and_preserves_contract(self):
        """run_task 通过 LangGraphRuntime 驱动，返回协议与终态不变。"""
        from tests.exec_env import install_main, install_plan, install_tools
        from webpage_agent.api.main import run_task

        called = {}

        async def fake_answer(args, state):
            called['answer'] = True
            return {'answer': '你好！', 'status': 'answered'}

        async def fake_search(args, state):
            return {'documents': []}

        from webpage_agent.tools.core import Tool
        table = {
            'answer_directly': Tool('answer_directly', '直接回答', 'd', {},
                                    fake_answer,
                                    tool_key='local:answers:direct', effect='read'),
            'web_search': Tool('web_search', '检索来源', 'd', {}, fake_search,
                               tool_key='local:web:search', effect='read'),
        }

        with tempfile.TemporaryDirectory() as folder:
            with install_main(folder), install_tools(table), \
                    install_plan([{'tool': 'answer_directly', 'step_id': 's1',
                                   'args': {}}])[0]:
                state = await run_task('你好')
        self.assertEqual(state['status'], 'succeeded')
        self.assertIn('timings', state)
        self.assertIn('elapsed_seconds', state)
        self.assertIn('outcome', state)
        self.assertTrue(called.get('answer'))

    async def test_run_task_recovery_passes_resume_plan_through(self):
        """恢复执行：resume_plan 经初始状态进入图，沿用计划、不重新规划。

        真实链路里 intake 看到 resume_plan 里有 steps 会直接恢复计划并路由到
        run_step，**不会**再进 plan 节点——这正是"续跑不重新规划"的语义。
        """
        from tests.exec_env import install_main, install_plan, install_tools
        from webpage_agent.api.main import run_task
        from webpage_agent.tools.core import Tool

        async def fake_answer(args, state):
            return {'answer': '好的', 'status': 'answered'}

        table = {
            'answer_directly': Tool('answer_directly', '直接回答', 'd', {},
                                    fake_answer,
                                    tool_key='local:answers:direct', effect='read'),
        }
        patch_plan, plan_calls = install_plan(
            [{'tool': 'answer_directly', 'step_id': 's1', 'args': {}}])

        with tempfile.TemporaryDirectory() as folder:
            with install_main(folder), install_tools(table), patch_plan:
                state = await run_task(
                    '继续', resume_plan={
                        'steps': [{'tool': 'answer_directly', 'step_id': 's1',
                                   'args': {}}],
                        'summary': '续跑', 'plan_version': 1, 'step_index': 0})
        # 有已提交计划就沿用：plan 节点不应被再次调用（不重新规划）
        self.assertEqual(plan_calls, [])
        self.assertEqual(state['status'], 'succeeded')


class RuntimeFactoryAndContractTests(unittest.IsolatedAsyncioTestCase):
    """运行时工厂与宿主的可替换接入边界。"""

    async def asyncSetUp(self):
        await reset_db()

    async def test_get_runtime_returns_langgraph_by_default(self):
        """生产默认运行时是 LangGraphRuntime。"""
        from webpage_agent.agent.runtime import get_runtime
        from webpage_agent.agent.langgraph_runtime import LangGraphRuntime
        self.assertIsInstance(get_runtime(), LangGraphRuntime)

    async def test_run_task_uses_factory_not_hardcoded_impl(self):
        """run_task 通过工厂取运行时，测试可注入替代实现。

        注入一个不驱动真实图、只标记被调用的假运行时，验证宿主收尾（终态、
        outcome、落盘）仍按同一份 state 完成，且外部调用签名不变。
        """
        from tests.exec_env import install_main
        from webpage_agent.api import main as main_module
        from webpage_agent.api.main import run_task

        called = {}

        class _FakeRuntime:
            async def run(self, state, *, on_event=None, progress=None):
                called['run'] = True
                # 模拟运行时推进状态
                state['status'] = 'succeeded'
                state['answer'] = '假答案'
                return state

        with tempfile.TemporaryDirectory() as folder:
            with install_main(folder):
                # main.py 里是 `from ...runtime import get_runtime`，持有绑定引用，
                # 要 patch main 模块里的名字，而不是 runtime 模块属性。
                with patch.object(main_module, 'get_runtime',
                                  return_value=_FakeRuntime()):
                    state = await run_task('你好')
                # 宿主收尾仍完成：终态归一 + outcome + 落盘
                from pathlib import Path
                self.assertTrue((Path(state['artifact_dir']) / 'state.json').exists())

        self.assertTrue(called.get('run'))
        self.assertEqual(state['status'], 'succeeded')
        self.assertIn('outcome', state)
        self.assertIn('elapsed_seconds', state)

    async def test_run_task_recursion_budget_preserves_interrupted_semantics(self):
        """递归预算耗尽经工厂返回的运行时抛通用异常时，宿主按 interrupted 收尾。"""
        from tests.exec_env import install_main
        from webpage_agent.api import main as main_module
        from webpage_agent.api.main import run_task
        from webpage_agent.core.errors import RecursionBudgetExceeded

        class _BoomRuntime:
            async def run(self, state, *, on_event=None, progress=None):
                state['status'] = 'planned'
                raise RecursionBudgetExceeded('预算用尽', limit=40)

        with tempfile.TemporaryDirectory() as folder:
            with install_main(folder):
                with patch.object(main_module, 'get_runtime',
                                  return_value=_BoomRuntime()):
                    state = await run_task('你好')

        # 递归预算耗尽不是失败，是可恢复的中断
        self.assertEqual(state['status'], 'interrupted')
        self.assertIn('failure', state)
        self.assertIn('40', state['failure'])


if __name__ == '__main__':
    unittest.main()
