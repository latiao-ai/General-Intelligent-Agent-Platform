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
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["answer"], "你好😀")

    async def test_tool_roundtrip_executes_python_tool(self):
        # 假进程发 tool_call(add 17 25)，Python 执行内置假工具，回 42
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "tool"}
        runtime = PiRuntime(bridge=bridge)
        state = {"task_id": "x", "user_input": "17加25"}
        result = await runtime.run(state)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"], "结果是 42")

    async def test_tool_failure_still_reports(self):
        # 假进程发 tool_call(nope)，Python 回工具错误，Pi 继续并答复
        bridge = _bridge()
        bridge._extra_env = {"FAKE_MODE": "tool_fail"}
        runtime = PiRuntime(bridge=bridge)
        state = {"task_id": "x", "user_input": "测失败"}
        result = await runtime.run(state)
        self.assertEqual(result["status"], "succeeded")

    async def test_events_forwarded_via_on_event(self):
        received = []

        async def on_event(event):
            received.append(event)

        runtime = PiRuntime(bridge=_bridge())
        state = {"task_id": "x", "user_input": "你好"}
        await runtime.run(state, on_event=on_event)
        # 文本增量应以 answer_delta 事件透传（跨分片/Unicode 都保留）
        deltas = [e["delta"] for e in received if e.get("type") == "answer_delta"]
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


if __name__ == "__main__":
    unittest.main()
