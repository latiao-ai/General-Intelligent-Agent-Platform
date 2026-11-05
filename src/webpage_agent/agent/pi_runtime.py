"""PiRuntime：通过 Node 桥接驱动 Pi 推理，实现 `agent.runtime.Runtime` 契约。

第三阶段目标：打通「Python → Node 中的 Pi → Python 假工具 → Pi 答复」这一条
最小链路。生产入口 `get_runtime()` 仍然默认 `LangGraphRuntime`，本实现只通过
测试注入和独立 smoke 入口验证，不开放全局切换、不新增数据库字段。

契约遵守（与 Runtime 接口一致）：

    - **原地修改并返回同一个 state**：Pi 的最终答复写回 `state['answer']`，展示用
      的文本增量通过事件回调交出去，宿主据此做终态收口与落盘；
    - **异常、取消、失租原样传播**：PiBridgeError 折成可识别异常向上抛；
      `CancelledError` / `LeaseLost` / `ControlStopped` 不在这里吞掉；
    - **不允许 Node 返回任意 state 补丁**：Node 只回 `event` / `tool_call` /
      `finished`，本实现只把「宿主认可的结果」映射回 state（answer、status 等），
      绝不覆盖身份、租约、路径或任务状态字段。

工具执行交回 Python：Node 发 `tool_call`，本实现用可注入的 `tool_executor`
执行（默认内置 echo/add 两个测试工具），再把结果作为 `tool_result` 回给 Node。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from webpage_agent.agent import pi_protocol as proto
from webpage_agent.agent.pi_bridge import PiBridge, PiBridgeError
from webpage_agent.agent.runtime import invoke_callback

logger = logging.getLogger(__name__)

# 工具执行器：接收 (tool_name, arguments) 返回 (ok, result)。测试可注入替身。
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, Any]]]

# 第三阶段内置的测试工具目录（与 Node 侧 tools.ts 的 TEST_TOOLS 对应）。
_DEFAULT_TOOLS = [
    {"name": "echo", "description": "回显传入的字符串，用于验证工具调用往返。"},
    {"name": "add", "description": "计算两个整数之和。"},
]


async def _default_tool_executor(name: str, args: dict[str, Any]) -> tuple[bool, Any]:
    """内置假工具：echo 回显、add 整数加法。"""
    if name == "echo":
        return True, str(args.get("text", ""))
    if name == "add":
        try:
            a = int(args.get("a", 0))
            b = int(args.get("b", 0))
            return True, a + b
        except (TypeError, ValueError) as exc:
            return False, f"参数非法：{exc}"
    return False, f"未知工具：{name}"


class PiRuntime:
    """通过 Node 桥接驱动 Pi，实现 Runtime 契约。

    一次 `run()` 独占一个子进程与一个 Pi session，结束后清理。可注入 `bridge`
    （测试用假进程）与 `tool_executor`（测试用替身工具）。
    """

    def __init__(
        self,
        *,
        bridge: PiBridge | None = None,
        tool_executor: ToolExecutor | None = None,
        tools: list[dict[str, str]] | None = None,
        model: dict[str, str] | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        model_timeout_ms: int | None = None,
    ) -> None:
        self._bridge = bridge
        self._tool_executor = tool_executor or _default_tool_executor
        self._tools = tools if tools is not None else _DEFAULT_TOOLS
        self._model = model or {
            "id": "deepseek-v4-1-flash",
            "base_url": "https://www.hi-x.cn/v1",
            "provider": "deepseek",
            "api": "openai-completions",
        }
        self._api_key = api_key
        self._system_prompt = system_prompt
        self._model_timeout_ms = model_timeout_ms

    async def run(
        self,
        state: dict,
        *,
        on_event=None,
        progress=None,
    ) -> dict:
        """驱动一次 Pi 执行，原地修改并返回 state。

        流程：启动桥接 → 发 start → 循环读消息：
          - `event`：转发为 on_event（text_delta 拼进本地缓冲，展示用）；
          - `tool_call`：Python 执行假工具 → 发 tool_result；
          - `finished`：answered/cancelled/failed，写回 state；
          - `error`：抛 PiBridgeError。
        结束或异常后关闭桥接，回收子进程。
        """
        bridge = self._bridge or PiBridge()
        own_bridge = self._bridge is None
        answer_parts: list[str] = []

        try:
            await bridge.start()

            # 构造 start 消息：输入、上下文、工具目录、模型与运行限制
            history = []
            for item in state.get("history") or []:
                if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                    history.append({"role": item["role"], "content": str(item.get("content", ""))})

            start = proto.make_message(
                bridge.execution_id,
                "start",
                user_input=str(state.get("user_input", "")),
                system_prompt=self._system_prompt,
                history=history,
                tools=self._tools,
                model=self._model,
                api_key=self._api_key,
                model_timeout_ms=self._model_timeout_ms,
            )
            await bridge.send(start)

            while True:
                msg = await bridge.next_message()
                mtype = msg.get("type")

                if mtype == "event":
                    await self._handle_event(msg, on_event, answer_parts)
                elif mtype == "tool_call":
                    await self._handle_tool_call(bridge, msg)
                elif mtype == "finished":
                    self._apply_finished(state, msg)
                    break
                elif mtype == "error":
                    raise PiBridgeError(
                        f"Pi 桥接错误：{msg.get('error', '未知错误')}",
                        stderr_tail=bridge.stderr_tail(),
                    )
                else:
                    logger.warning("忽略未知消息类型：%r", mtype)
        finally:
            if own_bridge:
                await bridge.close()

        return state

    async def _handle_event(self, msg: dict, on_event, answer_parts: list[str]) -> None:
        """转发事件；text_delta 拼进缓冲并透传展示。"""
        kind = msg.get("kind")
        if kind == "text_delta":
            delta = str(msg.get("delta", ""))
            answer_parts.append(delta)
            if on_event:
                await invoke_callback(on_event, {"type": "answer_delta", "delta": delta})
        elif kind == "progress" or kind == "stage":
            if on_event:
                await invoke_callback(on_event, {
                    "type": "stage",
                    "label": msg.get("label", ""),
                    "status": msg.get("status", ""),
                })

    async def _handle_tool_call(self, bridge: PiBridge, msg: dict) -> None:
        """执行假工具并回执。"""
        call_id = str(msg.get("call_id", ""))
        tool_name = str(msg.get("tool_name", ""))
        args = msg.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}

        ok, result = await self._tool_executor(tool_name, args)
        reply = proto.make_message(
            bridge.execution_id,
            "tool_result",
            call_id=call_id,
            ok=ok,
            result=result if ok else None,
            error=None if ok else str(result),
        )
        await bridge.send(reply)

    def _apply_finished(self, state: dict, msg: dict) -> None:
        """把本轮终结结果映射回 state（只写宿主认可的结果字段）。"""
        outcome = msg.get("outcome")
        if outcome == "answered":
            # 完整答复是最终依据；本地缓冲只作兜底
            answer = msg.get("answer") or ""
            if answer:
                state["answer"] = answer
            state["status"] = "succeeded"
        elif outcome == "cancelled":
            state["status"] = "cancelled"
        elif outcome == "failed":
            state["status"] = "failed"
            err = str(msg.get("error", "Pi 执行失败"))
            state.setdefault("errors", []).append({"stage": "pi", "type": "PiError", "detail": err})
        else:
            logger.warning("未知 finished outcome：%r", outcome)
            state["status"] = "failed"
