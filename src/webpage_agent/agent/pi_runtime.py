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

第四阶段收口：

    - 工具目录**由宿主传入**（名称、描述、参数 schema），Node 据此注册；
      本实现执行工具时**再次校验允许目录与参数**，不静默放行未知工具；
    - 工具失败回传时**保留错误正文**，不只在成功时读 result 字段；
    - **不写业务终态 `status`**：`answered` 只是「本轮有有效答复」的技术事实，
      记入 `pi_outcome`；业务成功与否由宿主的 `core.outcome.final_status()`
      判定（业务失败也可能带着答复文本）。运行时不越权做终态归一。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from webpage_agent.agent import pi_protocol as proto
from webpage_agent.agent.pi_bridge import PiBridge, PiBridgeError
from webpage_agent.agent.pi_protocol import ProtocolError
from webpage_agent.observe.events import (
    answer_delta,
    bind_sink,
    emit_async,
    stage_start,
    unbind_sink,
)

logger = logging.getLogger(__name__)

# 工具执行器：接收 (tool_name, arguments) 返回 (ok, result)。测试可注入替身。
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, Any]]]

# 默认测试工具目录：名称、描述、参数 schema（字段名 → 类型字符串）。
# 与 Node 侧 tools.ts 的 DEFAULT_TOOL_DECLS 对应。
_DEFAULT_TOOLS = [
    {"name": "echo", "description": "回显传入的字符串，用于验证工具调用往返。",
     "parameters": {"text": "string"}},
    {"name": "add", "description": "计算两个整数之和。",
     "parameters": {"a": "integer", "b": "integer"}},
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
        tools: list[dict[str, Any]] | None = None,
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
          - `tool_call`：Python 校验并执行假工具 → 发 tool_result；
          - `finished`：answered/cancelled/failed，写回 state；
          - `error`：抛 PiBridgeError。
        结束或异常后关闭桥接，回收子进程。
        """
        bridge = self._bridge or PiBridge()
        own_bridge = self._bridge is None
        answer_parts: list[str] = []

        # 绑定宿主 sink：让 Pi 事件经统一的公共事件出口（emit_async）有序转交，
        # 与 LangGraph 节点内部事件走同一套 answer_delta / stage_start / stage_end 契约。
        token = bind_sink(on_event) if on_event else None

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
                    await self._handle_event(msg, answer_parts)
                elif mtype == "tool_call":
                    await self._handle_tool_call(bridge, msg)
                elif mtype == "finished":
                    self._apply_finished(state, msg, bridge.execution_id)
                    break
                elif mtype == "error":
                    raise PiBridgeError(
                        f"Pi 桥接错误：{msg.get('error', '未知错误')}",
                        stderr_tail=bridge.stderr_tail(),
                    )
                else:
                    # 未知消息类型：协议错误，终结执行
                    raise PiBridgeError(
                        f"未知消息类型：{mtype!r}", stderr_tail=bridge.stderr_tail()
                    )
        except asyncio.CancelledError:
            # 取消：有界顺序清理（请求取消→宽限→terminate→kill→wait），
            # 但**原样传播** CancelledError，清理异常不能覆盖原异常。
            if own_bridge:
                try:
                    await bridge.cancel_and_reap()
                except Exception as exc:  # noqa: BLE001 清理异常记录即可，不覆盖取消
                    logger.warning("取消清理失败：%s", exc)
            raise
        finally:
            if token is not None:
                unbind_sink(token)
            if own_bridge:
                await bridge.close()

        return state

    async def _handle_event(self, msg: dict, answer_parts: list[str]) -> None:
        """转发事件：text_delta 走 answer_delta（text 字段），阶段走 stage 契约。"""
        kind = msg.get("kind")
        if kind == "text_delta":
            delta = str(msg.get("delta", ""))
            answer_parts.append(delta)
            # 公共事件契约：answer_delta 使用 text 字段
            await emit_async(answer_delta(delta))
        elif kind == "progress" or kind == "stage":
            stage = str(msg.get("label", ""))
            await emit_async(stage_start(stage))

    async def _handle_tool_call(self, bridge: PiBridge, msg: dict) -> None:
        """校验并执行工具，回执（保留错误正文）。"""
        try:
            proto.validate_tool_call(msg, bridge.execution_id)
        except ProtocolError as exc:
            raise PiBridgeError(
                f"非法 tool_call：{exc}", stderr_tail=bridge.stderr_tail()
            ) from exc

        call_id = msg["call_id"]
        tool_name = msg["tool_name"]
        args = msg["arguments"]

        # 再次校验允许目录：工具必须在宿主声明的目录里，参数类型符合 schema
        allowed = self._allowed_tool(tool_name)
        if allowed is None:
            await self._send_tool_error(bridge, call_id, f"工具未在允许目录：{tool_name}")
            return
        if not self._args_match_schema(args, allowed["parameters"]):
            await self._send_tool_error(bridge, call_id, f"参数不符合 schema：{args!r}")
            return

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

    async def _send_tool_error(self, bridge: PiBridge, call_id: str, error: str) -> None:
        """回执一个工具错误（保留错误正文）。"""
        reply = proto.make_message(
            bridge.execution_id, "tool_result", call_id=call_id, ok=False,
            result=None, error=error,
        )
        await bridge.send(reply)

    def _allowed_tool(self, name: str) -> dict[str, Any] | None:
        for tool in self._tools:
            if tool.get("name") == name:
                return tool
        return None

    @staticmethod
    def _args_match_schema(args: dict[str, Any], schema: dict[str, str]) -> bool:
        """按参数 schema 校验参数类型（string/number/boolean/integer）。"""
        for key, kind in schema.items():
            if key not in args:
                continue  # 缺省字段允许（默认假工具给缺省值）
            value = args[key]
            if kind == "string" and not isinstance(value, str):
                return False
            if kind in ("number", "integer") and not isinstance(value, (int, float)):
                return False
            if kind == "boolean" and not isinstance(value, bool):
                return False
        return True

    def _apply_finished(self, state: dict, msg: dict, execution_id: str) -> None:
        """把本轮终结结果映射回 state（只写宿主认可的结果字段）。

        关键：**不写业务终态 `status`**。`status` 是任务/运行层的通用状态机
        词汇（`core.outcome.RUN_STATUSES`），终态归一是宿主的职责——宿主用
        `core.outcome.final_status()` 按「产出了什么 / 副作用是否核实」判定，
        而不是「有答复就成功」（业务失败也可能带着一段答复文本）。

        PiRuntime 只把 Pi 的**技术结果**写回 state：

          - `answer`：本轮产出的答复文本（可空）；
          - `pi_outcome`：Pi 侧终结原因 `answered` / `failed` / `cancelled`，
            供独立 smoke 入口与宿主按需读取；
          - `errors`：失败时追加可识别错误（不覆盖已有 errors）。
        """
        try:
            proto.validate_finished(msg, execution_id)
        except ProtocolError:
            # 非法 finished：按失败处理
            state["pi_outcome"] = "failed"
            state.setdefault("errors", []).append(
                {"stage": "pi", "type": "PiError", "detail": "非法 finished 消息"}
            )
            return

        outcome = msg.get("outcome")
        if outcome == "answered":
            answer = msg.get("answer") or ""
            if not answer:
                # 无有效答复：明确失败，不保留旧 answer 冒充本轮答复
                state["pi_outcome"] = "failed"
                state.setdefault("errors", []).append(
                    {"stage": "pi", "type": "PiError", "detail": "本轮无有效答复"}
                )
                return
            state["answer"] = answer
            state["pi_outcome"] = "answered"
        elif outcome == "cancelled":
            state["pi_outcome"] = "cancelled"
        elif outcome == "failed":
            state["pi_outcome"] = "failed"
            err = str(msg.get("error", "Pi 执行失败"))
            state.setdefault("errors", []).append(
                {"stage": "pi", "type": "PiError", "detail": err}
            )
        else:
            # validate_finished 已保证 outcome 只可能是上述三者，此分支防御性兜底
            state["pi_outcome"] = "failed"
