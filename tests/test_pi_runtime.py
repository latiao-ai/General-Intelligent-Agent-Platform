"""PiRuntime 假进程测试（第三阶段）。

用 `tests/fixtures/fake_pi_bridge.js` 这个纯协议回显的假进程（不含 Pi SDK、
不碰真实模型），验证 Python 侧的桥接生命周期、协议处理、工具往返、取消、
崩溃与清理。真实 Pi 联调见 `scripts/pi_smoke.py`，与假进程测试分开报告。
"""
import asyncio
import unittest

from pathlib import Path

from webpage_agent.agent.pi_bridge import PiBridge, PiBridgeError
from webpage_agent.agent.pi_runtime import PiRuntime

_FAKE_SCRIPT = Path(__file__).resolve().parent / "fixtures" / "fake_pi_bridge.js"


def _bridge(**kwargs) -> PiBridge:
    return PiBridge(node_script=_FAKE_SCRIPT, **kwargs)


class CurrentTimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_admission_refreshes_host_time(self):
        from unittest.mock import AsyncMock, patch
        from webpage_agent.agent import context, pi_protocol

        class Bridge:
            execution_id = 'x'
            send = AsyncMock()

        bridge = Bridge()
        runtime = PiRuntime(tools=[], system_prompt='自定义规则')
        runtime._begin_model = AsyncMock(return_value='reservation')
        with patch.object(context, 'current_time_prompt', side_effect=['第一天', '第二天']):
            for request_id in ('one', 'two'):
                msg = pi_protocol.make_message('x', 'model_request', request_id=request_id)
                await runtime._handle_model_request(bridge, msg)
        sent = [call.args[0] for call in bridge.send.await_args_list]
        self.assertEqual([item['current_time_prompt'] for item in sent], ['第一天', '第二天'])
        self.assertTrue(all(item['allow'] for item in sent))
        self.assertEqual(runtime._system_prompt, '自定义规则')

    def test_prepare_uses_shanghai_time_and_keeps_system(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        from webpage_agent.agent import context
        from webpage_agent.core import services

        instant = datetime(2030, 1, 1, 16, 30, tzinfo=timezone.utc)
        with patch.object(context, 'datetime') as clock:
            clock.now.side_effect = lambda tz: instant.astimezone(tz)
            with patch.object(services, 'emit'):
                conversation = services._prepare('自定义系统规则', [('user', '今天几号')])
            shanghai = conversation[0][1]
            utc = context.current_time_prompt('UTC')
        self.assertIn('自定义系统规则', shanghai)
        self.assertIn('2030-01-02T00:30:00+08:00', shanghai)
        self.assertIn('Asia/Shanghai', shanghai)
        self.assertIn('2030-01-01T16:30:00+00:00', utc)
        self.assertEqual(conversation[1], ('user', '今天几号'))


class PiProtocolTests(unittest.TestCase):
    """协议编解码与校验的纯单元测试。"""

    def test_encode_decode_roundtrip_unicode(self):
        from webpage_agent.agent import pi_protocol as proto
        msg = proto.make_message("x", "event", kind="text_delta", delta="你好😀")
        decoded = proto.decode(proto.encode(msg))
        self.assertEqual(decoded["delta"], "你好😀")

    def test_decode_bad_json_raises(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.decode(b"not json")

    def test_validate_envelope_version_mismatch(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_envelope({"protocol_version": 999, "execution_id": "x", "type": "event"}, "x")

    def test_validate_envelope_id_mismatch(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_envelope({"protocol_version": 1, "execution_id": "y", "type": "event"}, "x")


class PiBridgeTests(unittest.IsolatedAsyncioTestCase):
    """桥接层：启动、收发、取消、崩溃、清理。"""

    async def test_start_sends_ready(self):
        bridge = _bridge()
        try:
            await bridge.start()
            self.assertIsNotNone(bridge._proc)
        finally:
            await bridge.close()

    async def test_echo_roundtrip(self):
        bridge = _bridge()
        try:
            await bridge.start()
            from webpage_agent.agent import pi_protocol as proto
            await bridge.send(proto.make_message(bridge.execution_id, "start", user_input="hi"))
            msg = await bridge.next_message()
            self.assertEqual(msg.get("type"), "event")
            msg2 = await bridge.next_message()
            self.assertEqual(msg2.get("type"), "event")
            msg3 = await bridge.next_message()
            self.assertEqual(msg3.get("type"), "finished")
            self.assertEqual(msg3.get("outcome"), "answered")
        finally:
            await bridge.close()

    async def test_startup_timeout_when_no_ready(self):
        bridge = _bridge(startup_timeout=0.5)
        bridge._extra_env = {"FAKE_READY": "no"}
        with self.assertRaises(PiBridgeError):
            await bridge.start()

    async def test_crash_returns_recognizable_error(self):
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "crash"}
        try:
            await bridge.start()
            from webpage_agent.agent import pi_protocol as proto
            await bridge.send(proto.make_message(bridge.execution_id, "start", user_input="hi"))
            # 子进程崩溃后，next_message 应抛可识别错误，而非无限挂起
            with self.assertRaises(PiBridgeError):
                await bridge.next_message(timeout=3.0)
        finally:
            await bridge.close()

    async def test_noisy_stderr_does_not_block(self):
        bridge = _bridge(startup_timeout=5.0)
        bridge._extra_env = {"FAKE_MODE": "noisy"}
        try:
            await bridge.start()
            # 大量 stderr 输出不应阻塞 ready 或 stdout 读取
            self.assertTrue(bridge.stderr_tail())
        finally:
            await bridge.close()

    async def test_close_leaves_no_orphan(self):
        bridge = _bridge()
        await bridge.start()
        proc = bridge._proc
        await bridge.close()
        # 回收后子进程应已退出
        self.assertIsNotNone(proc)
        self.assertIsNotNone(proc.returncode)


class PiRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """PiRuntime 经假进程驱动，遵守 Runtime 契约。"""

    async def test_answered_maps_to_state(self):
        runtime = PiRuntime(bridge=_bridge())
        state = {"task_id": "x", "user_input": "你好"}
        result = await runtime.run(state)
        self.assertIs(result, state)  # 原地修改，返回同一引用
        self.assertEqual(state["pi_outcome"], "answered")
        self.assertEqual(state["answer"], "你好😀")
        # 运行时不越权写业务终态 status（终态归一属宿主 final_status）
        self.assertNotIn("status", state)

    async def test_tool_roundtrip_executes_python_tool(self):
        # 假进程发 tool_call(web_search)，Python 执行注入的工具，回执
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "tool"}
        calls = []

        async def executor(name, args):
            calls.append((name, args))
            return True, {"results": [{"title": "t", "url": "https://x"}]}

        runtime = PiRuntime(bridge=bridge, tool_executor=executor)
        state = {"task_id": "x", "user_input": "搜一下"}
        result = await runtime.run(state)
        self.assertEqual(result["pi_outcome"], "answered")
        self.assertEqual(result["answer"], "搜索结果")
        # 工具确实被 Python 执行，参数符合 schema
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "web_search")
        self.assertEqual(calls[0][1], {"query": "今天天气"})

    async def test_tool_failure_still_reports(self):
        # 假进程发 tool_call(nope)，Python 回工具错误，Pi 继续并答复
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "tool_fail"}
        runtime = PiRuntime(bridge=bridge)
        state = {"task_id": "x", "user_input": "测失败"}
        result = await runtime.run(state)
        self.assertEqual(result["pi_outcome"], "answered")

    async def test_multi_tool_serial_dispatch(self):
        # 同一轮多个工具调用：Python 逐个串行回执，参数分别正确
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "multi_tool"}
        calls = []

        async def executor(name, args):
            calls.append((name, args))
            return True, {"results": [{"title": name, "url": "https://x"}]}

        runtime = PiRuntime(bridge=bridge, tool_executor=executor)
        state = {"task_id": "x", "user_input": "搜两个"}
        result = await runtime.run(state)
        self.assertEqual(result["pi_outcome"], "answered")
        # 两个工具调用都被 Python 串行执行，参数正确
        self.assertEqual(len(calls), 2)
        self.assertEqual([c[0] for c in calls], ["web_search", "web_search"])
        self.assertEqual([c[1]["query"] for c in calls], ["a", "b"])

    async def test_events_forwarded_via_on_event(self):
        received = []

        async def on_event(event):
            received.append(event)

        runtime = PiRuntime(bridge=_bridge())
        state = {"task_id": "x", "user_input": "你好"}
        await runtime.run(state, on_event=on_event)
        # 文本增量应以 answer_delta 事件透传，公共契约用 text 字段（非 delta）
        deltas = [e["text"] for e in received if e.get("type") == "answer_delta"]
        self.assertIn("你", deltas)
        self.assertIn("好😀", deltas)

    async def test_cancel_propagates(self):
        # 假进程收到 cancel 回 cancelled；但 Python 侧取消语义由宿主驱动，
        # 这里验证 finished(cancelled) 正确映射回 state
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "hang"}
        runtime = PiRuntime(bridge=bridge)
        state = {"task_id": "x", "user_input": "hang"}
        # hang 模式不响应，触发超时路径由桥接层处理；这里只验证异常可识别
        with self.assertRaises((PiBridgeError, asyncio.TimeoutError)):
            await asyncio.wait_for(runtime.run(state), timeout=2.0)

    async def test_unknown_tool_call_id_rejected(self):
        # 用真实 main.ts（协议层逻辑，不需要模型）：直接发一个不匹配的
        # tool_result，Node 侧应回 error 而非静默忽略（重复/未知 call_id 要明确处理）。
        real_script = Path(__file__).resolve().parents[1] / "pi-runtime" / "dist" / "main.js"
        if not real_script.exists():
            self.skipTest("pi-runtime/dist/main.js 未编译")
        bridge = PiBridge(node_script=real_script)
        try:
            await bridge.start()
            from webpage_agent.agent import pi_protocol as proto
            await bridge.send(proto.make_message(bridge.execution_id, "tool_result",
                                                 call_id="nonexistent", ok=True, result=None))
            msg = await bridge.next_message(timeout=3.0)
            self.assertEqual(msg.get("type"), "error")
        finally:
            await bridge.close()


class PiProtocolFieldTests(unittest.TestCase):
    """第四阶段：协议校验函数的单元测试。"""

    def test_tool_call_rejects_non_object_args(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_tool_call({
                "protocol_version": 1, "execution_id": "x", "type": "tool_call",
                "call_id": "c1", "tool_name": "add", "arguments": "not-a-dict",
            }, "x")

    def test_tool_call_rejects_missing_call_id(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_tool_call({
                "protocol_version": 1, "execution_id": "x", "type": "tool_call",
                "tool_name": "add", "arguments": {},
            }, "x")

    def test_tool_call_rejects_bad_arg_type(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_tool_call({
                "protocol_version": 1, "execution_id": "x", "type": "tool_call",
                "call_id": "c1", "tool_name": "add",
                "arguments": {"a": lambda: 1},
            }, "x")

    def test_finished_rejects_bad_outcome(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_finished({
                "protocol_version": 1, "execution_id": "x", "type": "finished",
                "outcome": "weird",
            }, "x")

    def test_validate_inbound_rejects_unknown_type(self):
        from webpage_agent.agent import pi_protocol as proto
        with self.assertRaises(proto.ProtocolError):
            proto.validate_inbound({
                "protocol_version": 1, "execution_id": "x", "type": "bogus",
            }, "x")

    def test_validate_inbound_ok_for_known_types(self):
        from webpage_agent.agent import pi_protocol as proto
        proto.validate_inbound({
            "protocol_version": 1, "execution_id": "x", "type": "finished",
            "outcome": "answered", "answer": "hi",
        }, "x")
        proto.validate_inbound({
            "protocol_version": 1, "execution_id": "x", "type": "event",
            "kind": "text_delta", "delta": "a",
        }, "x")


class PiBridgeTerminationTests(unittest.IsolatedAsyncioTestCase):
    """第四阶段：子进程退出/EOF 后不永久等待。"""

    async def test_exit_after_result_consumes_valid_result_first(self):
        # 正常结果（finished）之后立即退出：应先消费 finished，不误判失败
        bridge = _bridge()
        try:
            await bridge.start()
            from webpage_agent.agent import pi_protocol as proto
            await bridge.send(proto.make_message(bridge.execution_id, "start", user_input="hi"))
            msg = await bridge.next_message()  # event
            self.assertEqual(msg.get("type"), "event")
            await bridge.next_message()  # event
            msg3 = await bridge.next_message()  # finished
            self.assertEqual(msg3.get("type"), "finished")
            self.assertEqual(msg3.get("outcome"), "answered")
        finally:
            await bridge.close()

    async def test_crash_wakes_waiter_without_explicit_timeout(self):
        # 子进程崩溃后，无人工超时的 next_message 必须返回错误而非挂死
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "crash"}
        try:
            await bridge.start()
            from webpage_agent.agent import pi_protocol as proto
            await bridge.send(proto.make_message(bridge.execution_id, "start", user_input="hi"))
            # 不带 timeout，依赖终结机制唤醒
            with self.assertRaises(PiBridgeError):
                await asyncio.wait_for(bridge.next_message(), timeout=5.0)
        finally:
            await bridge.close()

    async def test_eof_after_ready_wakes_waiter(self):
        # 假进程在 start 后崩溃退出（EOF），next_message 无 timeout 也应唤醒
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "crash"}
        try:
            await bridge.start()
            from webpage_agent.agent import pi_protocol as proto
            await bridge.send(proto.make_message(bridge.execution_id, "start", user_input="hi"))
            with self.assertRaises(PiBridgeError):
                await asyncio.wait_for(bridge.next_message(), timeout=5.0)
        finally:
            await bridge.close()

    async def test_badjson_terminates_with_error(self):
        # 非法 JSON 应终结桥接并唤醒等待方，而非静默丢弃挂起。
        # 假进程 badjson 模式：先发 ready，随后输出非法 JSON 并退出。
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "badjson"}
        try:
            try:
                await bridge.start()
            except PiBridgeError:
                # ready 之前就遇到 badjson/退出也算通过（终结机制已生效）
                return
            # start 成功拿到 ready，随后非法 JSON 触发终结：next_message 应报错
            with self.assertRaises(PiBridgeError):
                await asyncio.wait_for(bridge.next_message(), timeout=5.0)
        finally:
            await bridge.close()

    async def test_bad_envelope_terminates_on_read_path(self):
        # 非法信封（协议版本错误）：真实收发路径 `_read_stdout` 里 decode 之后
        # 的 validate_inbound 应拦截并终结，而非把脏消息放进队列。
        # 假进程 badenvelope 模式：ready 之后回一条 protocol_version=999 的消息。
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "badenvelope"}
        try:
            await bridge.start()
            from webpage_agent.agent import pi_protocol as proto
            await bridge.send(proto.make_message(bridge.execution_id, "start", user_input="hi"))
            with self.assertRaises(PiBridgeError):
                await asyncio.wait_for(bridge.next_message(), timeout=5.0)
        finally:
            await bridge.close()

    async def test_repeated_read_after_termination_raises(self):
        # 终结后**重复读取**：连续多次 next_message 都应报错，不挂起、不吞。
        # 哨兵只放一次，next_message 的前置检查保证第二次也不会重新阻塞。
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "crash"}
        try:
            await bridge.start()
            from webpage_agent.agent import pi_protocol as proto
            await bridge.send(proto.make_message(bridge.execution_id, "start", user_input="hi"))
            for _ in range(3):
                with self.assertRaises(PiBridgeError):
                    await asyncio.wait_for(bridge.next_message(), timeout=5.0)
        finally:
            await bridge.close()

    async def test_send_after_termination_rejected(self):
        # 终结后 send 应拒绝新请求
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "crash"}
        try:
            await bridge.start()
            from webpage_agent.agent import pi_protocol as proto
            await bridge.send(proto.make_message(bridge.execution_id, "start", user_input="hi"))
            with self.assertRaises(PiBridgeError):
                await asyncio.wait_for(bridge.next_message(), timeout=5.0)
            # 已终结：再次 send 应被拒绝
            with self.assertRaises(PiBridgeError):
                await bridge.send(proto.make_message(bridge.execution_id, "cancel"))
        finally:
            await bridge.close()

    async def test_close_idempotent(self):
        bridge = _bridge()
        await bridge.start()
        await bridge.close()
        await bridge.close()  # 幂等，不抛错
        self.assertTrue(bridge._closed)

    async def test_stdin_eof_causes_node_exit(self):
        # 真实 main.ts：Python 关闭 stdin（EOF）后 Node 应清理退出，不残留子进程
        real_script = Path(__file__).resolve().parents[1] / "pi-runtime" / "dist" / "main.js"
        if not real_script.exists():
            self.skipTest("pi-runtime/dist/main.js 未编译")
        bridge = PiBridge(node_script=real_script)
        await bridge.start()
        proc = bridge._proc
        # 直接关闭（触发 stdin EOF + shutdown）
        await bridge.close()
        # Node 应在 EOF 后退出
        self.assertIsNotNone(proc.returncode)


class PiRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    """第四阶段：错误停止原因、工具目录、错误正文、取消。"""

    async def test_failed_outcome_maps_to_failed(self):
        # 模型错误停止 → Python 标 failed，不冒充 answered
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "error_stop"}
        runtime = PiRuntime(bridge=bridge)
        state = {"task_id": "x", "user_input": "hi"}
        result = await runtime.run(state)
        self.assertEqual(result["pi_outcome"], "failed")
        self.assertIn("errors", result)

    async def test_tool_error_body_preserved(self):
        # 工具参数类型错误：回传时保留错误正文（query 不是字符串）
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "tool_err"}
        runtime = PiRuntime(bridge=bridge)
        state = {"task_id": "x", "user_input": "bad search"}
        result = await runtime.run(state)
        # query 类型非法 → 按完整 JSON Schema 校验拒绝，错误正文回传，Pi 答复
        self.assertEqual(result["pi_outcome"], "answered")

    async def test_missing_required_query_rejected(self):
        # 缺少必填 query：按完整 JSON Schema 校验在搜索前拒绝
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "missing_query"}
        runtime = PiRuntime(bridge=bridge)
        state = {"task_id": "x", "user_input": "search without query"}
        result = await runtime.run(state)
        self.assertEqual(result["pi_outcome"], "answered")

    async def test_tool_catalog_defaults_to_web_search(self):
        # 默认工具目录只开放 web_search（稳定键 local:web:search），无 echo/add
        runtime = PiRuntime()
        names = [t["name"] for t in runtime._tools]
        self.assertEqual(names[0], "web_search")
        self.assertIn("request_input", names)
        # web_search 参数是完整 JSON Schema（type/properties/required）
        ws = runtime._tools[0]
        self.assertEqual(ws["parameters"]["type"], "object")
        self.assertIn("query", ws["parameters"]["properties"])
        self.assertEqual(ws["parameters"]["required"], ["query"])

    async def test_empty_tool_catalog_means_no_tools(self):
        # tools=[] 表示本轮无工具，不回退 echo/add
        runtime = PiRuntime(tools=[])
        self.assertEqual(runtime._tools, [])

    async def test_disallowed_tool_rejected(self):
        # 宿主工具目录不含该工具：Python 侧拒绝执行并回工具错误，不静默放行
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "tool_fail"}  # 发 tool_call(nope)
        runtime = PiRuntime(bridge=bridge, tools=[
            {"name": "echo", "description": "回显",
             "parameters": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]}},
        ])
        state = {"task_id": "x", "user_input": "nope"}
        result = await runtime.run(state)
        # 工具 nope 不在允许目录 → 回工具错误，Pi 仍答复（failed 工具不阻断整轮）
        self.assertEqual(result["pi_outcome"], "answered")

    async def test_custom_tool_catalog_reaches_node(self):
        # 宿主传入的工具目录（含完整 JSON Schema）应进入 start 消息。
        # 用假进程验证：不依赖真实模型/真实 main.ts。
        captured = {}

        class _ProbeBridge:
            execution_id = "probe"

            async def start(self):
                pass

            async def send(self, msg):
                captured["start"] = msg

            async def next_message(self, timeout=None):
                # 回一个 finished 结束循环
                return {"type": "finished", "outcome": "answered", "answer": "ok"}

            def stderr_tail(self):
                return ""

            async def close(self):
                pass

            async def cancel_and_reap(self):
                pass

        tools = [{"name": "add", "description": "加法",
                  "parameters": {"type": "object",
                                 "properties": {"a": {"type": "integer"},
                                                "b": {"type": "integer"}},
                                 "required": ["a", "b"]}}]
        runtime = PiRuntime(bridge=_ProbeBridge(), tools=tools)
        state = {"task_id": "x", "user_input": "hi"}
        await runtime.run(state)
        sent_tools = captured["start"]["tools"]
        self.assertEqual(sent_tools, tools)
        # schema 确实带上了完整 JSON Schema（type/properties/required）
        self.assertEqual(sent_tools[0]["parameters"]["type"], "object")
        self.assertEqual(sent_tools[0]["parameters"]["required"], ["a", "b"])

    async def test_same_state_reference_preserved(self):
        # 同一 state 引用：正常与异常路径都不重新绑定 state
        bridge = _bridge()
        runtime = PiRuntime(bridge=bridge)
        state = {"task_id": "x", "user_input": "你好"}
        result = await runtime.run(state)
        self.assertIs(result, state)

    async def test_cancel_and_reap_no_orphan(self):
        # 有界顺序取消：cancel 后子进程被回收，无残留
        bridge = _bridge()
        try:
            await bridge.start()
            proc = bridge._proc
            await bridge.cancel_and_reap()
            # 回收后子进程应已退出
            self.assertIsNotNone(proc.returncode)
        finally:
            await bridge.close()

    async def test_cancelled_error_propagates_after_cleanup(self):
        # 宿主 CancelledError 原样传播，清理异常不覆盖原异常
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "hang"}  # 不响应，迫使取消路径
        runtime = PiRuntime(bridge=bridge)
        state = {"task_id": "x", "user_input": "hang"}
        task = asyncio.ensure_future(runtime.run(state))
        await asyncio.sleep(0.3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class EventSinkIsolationTests(unittest.IsolatedAsyncioTestCase):
    """第四阶段：作用域绑定的事件 sink 隔离与解绑。"""

    async def test_bind_sink_isolates_between_scopes(self):
        from webpage_agent.observe.events import bind_sink, emit_async, unbind_sink
        received_a = []
        received_b = []

        async def sink_a(event):
            received_a.append(event)

        async def sink_b(event):
            received_b.append(event)

        ta = bind_sink(sink_a)
        await emit_async({"type": "answer_delta", "text": "a1"})
        unbind_sink(ta)

        tb = bind_sink(sink_b)
        await emit_async({"type": "answer_delta", "text": "b1"})
        unbind_sink(tb)

        self.assertEqual([e["text"] for e in received_a], ["a1"])
        self.assertEqual([e["text"] for e in received_b], ["b1"])

    async def test_no_sink_falls_back_to_writer(self):
        # 没有绑定 sink 时静默跳过，不抛错。
        from webpage_agent.observe.events import emit_async
        await emit_async({"type": "answer_delta", "text": "x"})  # 不抛错


class PiRecoveryReplyTests(unittest.IsolatedAsyncioTestCase):
    """恢复决策落到用户回执上的措辞（回归：只读结果未定不得说成写操作）。

    `pi_recovery.decide()` 的 `reason` 明确标注「排障用」，里面带内部 action_id
    与英文状态；而 `core/messages.py` 把 `state['failure']` 直接拼进给用户看的
    句子。两者混用会同时造成两个问题：

      - 不写 `failure` → 回退成默认句"有写操作的结果没能确认"，可这条动作可能
        只是 `local:web:search`（effect=read），等于凭空告诉用户产生了副作用；
      - 直接写 `reason` → 内部标识漏进用户文案。

    这里锁住「内部留痕走 errors、用户文案单独写」这条边界。
    """

    def _state(self, kind: str, reason: str = '') -> dict:
        from webpage_agent.agent import pi_snapshot
        snapshot = pi_snapshot.new_snapshot(user_input='查天气')
        return {
            'task_id': 't', 'run_id': 'r', 'user_input': '查天气', 'history': [],
            'pi_resume': {
                'snapshot': snapshot,
                'recovery': {'kind': kind, 'reason': reason,
                             'tool_results': [], 'pending_calls': [],
                             'deliver_results': [], 'final_answer': None},
            },
        }

    async def test_reconcile_message_does_not_claim_write_side_effect(self):
        from webpage_agent.core.messages import reply_text

        reason = '动作 act_abc123 状态 in_flight，结果未定，需核实'
        state = await PiRuntime(bridge=_bridge()).run(self._state('resume_reconcile', reason))

        self.assertEqual(state['status'], 'needs_reconcile')
        # 用户文案：说清"这一步生没生效不确定、且没有重试"，不出现内部标识
        self.assertIn('没能确认', state['failure'])
        self.assertNotIn('act_abc123', state['failure'])
        self.assertNotIn('in_flight', state['failure'])
        # 排障信息不丢：原始 reason 仍在 errors 里
        self.assertEqual(state['errors'][-1]['detail'], reason)

        reply = reply_text(state)
        # 只读场景不得提"写操作"
        self.assertNotIn('写操作', reply)
        self.assertIn('没能确认', reply)

    async def test_block_kind_uses_same_user_facing_failure(self):
        state = await PiRuntime(bridge=_bridge()).run(
            self._state('resume_block', '结果未知，保守阻断'))
        self.assertEqual(state['status'], 'needs_reconcile')
        self.assertTrue(state['failure'])


if __name__ == "__main__":
    unittest.main()
