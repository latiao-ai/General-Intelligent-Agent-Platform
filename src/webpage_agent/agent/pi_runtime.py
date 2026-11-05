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

第五阶段（本阶段第一组修改）：

    - 工具目录改为**完整 JSON Schema**（能力包 `Tool.args`，含 type/properties/
      required/嵌套对象/数组），Python 执行前按同一 schema 校验，缺少 query、
      类型错误都在搜索前拒绝；`tools=[]` 表示本轮无工具，**不回退** echo/add；
    - 模型请求握手：Node 每次 stream 前后发 model_request / model_usage，
      Python 准入/结算后回 model_admit / model_usage_ack；
    - 真实工具走 `HostToolCaller` → `execution_gateway.governed_call` 的完整治理链
      （授权 → 准入 → 撤权复查 → 真实调用 → 结算）：**成功结算前不向 Pi 发成功
      回执**（只有 `governed_call` 返回 `succeeded` 即结算完成后才回 ok=True），
      结算失败异常向上传播、停止后续模型执行；
    - 模型 usage 结算失败不阻断 ack（模型已跑完，usage 是事后记账），但「全零
      或缺失 usage」在 `core.services.end_model_request` 里走保守估算、不填零。

第六阶段（共享网关）修正：

    - `HostToolCaller.call()` 传入**真实 state**（含 task_id / artifact_dir /
      documents），移除正式路径里的 `state={}`；可信主体、要求版本、执行上下文
      与账本由宿主持有，不取自 Node 或模型输出；
    - 工具结果**成功结算后**把认可增量（documents / usage / errors 白名单）合并
      回宿主 state，不把 status="searched" 当业务终态，也不让任意结果覆盖身份、
      租约等宿主字段；
    - 桥接 `call_id` 只用于往返关联，稳定执行身份由 `_stable_step_id`
      （tool_key + args 指纹）分配，映射到账本幂等键，重复调用复用同一动作。
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
    drain_events,
    emit_async,
    stage_start,
    unbind_sink,
)

logger = logging.getLogger(__name__)

# 工具执行器：接收 (tool_name, arguments) 返回 (ok, result)。测试可注入替身。
# 第五阶段起，真实执行的工具通过 execution_gateway 走完整治理；这个 executor
# 只作为独立 smoke 与单元测试的注入点，不再内置 echo/add 假工具。
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, Any]]]


def _tool_decl(tool) -> dict[str, Any]:
    """把一个能力包 Tool 转成桥接协议里的工具声明（完整 JSON Schema）。"""
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.args,
    }


def default_tool_catalog() -> list[dict[str, Any]]:
    """Pi 默认可用的真实工具目录：只开放 web_search（稳定键 local:web:search）。

    不开放其它工具：本阶段 Pi 只支持单次独立执行里的公开网页检索，不做 MCP
    写操作。`tools=[]`（显式传入空）表示本轮无工具，**不回退**到任何假工具。
    """
    from webpage_agent.capabilities import registry
    for tool in registry.load():
        if tool.tool_key == "local:web:search":
            return [_tool_decl(tool)]
    return []


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
        host_caller=None,
        tools: list[dict[str, Any]] | None = None,
        model: dict[str, str] | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        model_timeout_ms: int | None = None,
    ) -> None:
        self._bridge = bridge
        # tool_executor 是测试注入的假工具替身；host_caller 是真实受治理调用对象
        # （默认 None → 惰性创建 HostToolCaller，只开放 web_search）。
        self._tool_executor = tool_executor
        self._host_caller = host_caller
        # tools=None 表示用默认目录（web_search）；tools=[] 表示本轮无工具。
        # 不再回退到 echo/add。
        self._tools = tools if tools is not None else default_tool_catalog()
        self._model = model or {
            "id": "deepseek-v4-1-flash",
            "base_url": "https://www.hi-x.cn/v1",
            "provider": "deepseek",
            "api": "openai-completions",
        }
        self._api_key = api_key
        self._system_prompt = system_prompt
        self._model_timeout_ms = model_timeout_ms
        # 模型请求握手：request_id → reservation，准入时存、结算时取。
        self._model_reservations: dict[str, Any] = {}
        # 第七阶段：版本化 Pi 快照（检查点/恢复用），run() 里初始化。
        self._snapshot: dict[str, Any] = {}

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

        # 第七阶段收口：恢复输入用**单一字段** `pi_resume`（快照 + 恢复决策）承载，
        # 由 Hub 的 _pi_resume_plan 装配、run_task 展平进 state。这里只从这一处读，
        # 不在顶层与 resume_plan 里同时维护两份。
        from webpage_agent.agent import pi_snapshot
        pi_resume = state.get('pi_resume') if isinstance(state, dict) else None
        resume_snapshot = (pi_resume or {}).get('snapshot') if pi_resume else None
        self._snapshot = dict(resume_snapshot) if resume_snapshot else \
            pi_snapshot.new_snapshot(user_input=str(state.get('user_input') or ''))

        # 恢复决策。resume_finish / resume_reconcile / resume_block 属于
        # 「不重新推理」的结局，在进入桥接前直接写回 state 收尾。
        recovery = (pi_resume or {}).get('recovery') if pi_resume else None
        if recovery is not None and getattr(recovery, 'kind', '') in (
                'resume_finish', 'resume_reconcile', 'resume_block'):
            kind = recovery.kind
            if kind == 'resume_finish':
                state['answer'] = recovery.final_answer or ''
                state['pi_outcome'] = 'answered'
                state['status'] = 'succeeded'
            else:
                state['pi_outcome'] = 'failed'
                state['status'] = 'needs_reconcile'
                state.setdefault('errors', []).append({
                    'stage': 'pi', 'type': 'RecoveryBlocked',
                    'detail': recovery.reason or '结果未定，需核实后再继续'})
            return state

        # 绑定宿主 sink：让 Pi 事件经统一的公共事件出口（emit_async）有序转交，
        # 与 LangGraph 节点内部事件走同一套 answer_delta / stage_start / stage_end 契约。
        token = bind_sink(on_event) if on_event else None

        try:
            await bridge.start()

            # 构造 start 消息：输入、上下文、工具目录、模型与运行限制。
            # 限制历史与输入长度，避免把无限长的上下文发给模型。
            from webpage_agent.core.config import settings
            max_input = int(getattr(settings, 'run_input_max_chars', 4000) or 4000)
            user_input = str(state.get("user_input", "") or "")[:max_input]
            history = []
            for item in state.get("history") or []:
                if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                    content = str(item.get("content", "") or "")[:max_input]
                    history.append({"role": item["role"], "content": content})

            # 工具目录发送前校验：完整 JSON Schema，非法则明确报错
            proto.validate_tool_catalog(self._tools)

            # 恢复上下文：有已提交的完整消息就带上，Node 据此重建、不重复追加
            # 原始用户输入；无则走首次执行（pushUser + history）。
            resume_messages = None
            if resume_snapshot and isinstance(resume_snapshot.get('messages'), list) \
                    and resume_snapshot['messages']:
                resume_messages = resume_snapshot['messages']

            start = proto.make_message(
                bridge.execution_id,
                "start",
                user_input=user_input,
                system_prompt=self._system_prompt,
                history=history,
                tools=self._tools,
                model=self._model,
                api_key=self._api_key,
                model_timeout_ms=self._model_timeout_ms,
                resume={'messages': resume_messages} if resume_messages else None,
            )
            await bridge.send(start)

            while True:
                msg = await bridge.next_message()
                mtype = msg.get("type")

                if mtype == "event":
                    await self._handle_event(msg, answer_parts)
                elif mtype == "tool_call":
                    await self._handle_tool_call(bridge, msg, state)
                elif mtype == "model_request":
                    await self._handle_model_request(bridge, msg)
                elif mtype == "model_usage":
                    await self._handle_model_usage(bridge, msg)
                elif mtype == "checkpoint":
                    await self._handle_checkpoint(bridge, msg)
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
            # 终结前先排空所有未等待的异步事件（真实搜索里的同步 emit 会入队），
            # 确保工具事件在终结前按序处理完，再解绑 sink、回收子进程。
            if token is not None:
                try:
                    await drain_events()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("排空事件失败：%s", exc)
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

    async def _handle_tool_call(self, bridge: PiBridge, msg: dict, state: dict) -> None:
        """校验并执行工具，回执（保留错误正文）。

        真实受治理调用成功后，把工具返回的**认可增量**（documents / usage /
        errors）合并回 `state`，保留来源与结果引用；不把 status="searched"
        当作业务终态，也不允许任意结果覆盖身份、租约等宿主字段。
        """
        try:
            proto.validate_tool_call(msg, bridge.execution_id)
        except ProtocolError as exc:
            raise PiBridgeError(
                f"非法 tool_call：{exc}", stderr_tail=bridge.stderr_tail()
            ) from exc

        call_id = msg["call_id"]
        tool_name = msg["tool_name"]
        args = msg["arguments"]

        # 再次校验允许目录：工具必须在宿主声明的目录里，参数符合完整 JSON Schema
        allowed = self._allowed_tool(tool_name)
        if allowed is None:
            await self._send_tool_error(bridge, call_id, f"工具未在允许目录：{tool_name}")
            return
        try:
            proto.validate_args_against_schema(args, allowed["parameters"])
        except ProtocolError as exc:
            await self._send_tool_error(bridge, call_id, f"参数不符合 schema：{exc}")
            return

        if self._tool_executor is not None:
            # 测试注入的假工具替身
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
            return

        # 真实受治理调用：走 HostToolCaller（授权 → 准入 → 撤权复查 → 真实调用 → 结算）
        if self._host_caller is None:
            from webpage_agent.agent.pi_host_tools import (
                HostToolCaller, PiCallRegistry)
            # 用当前快照重建逻辑调用身份注册表：恢复时复用原调用 ID，
            # 新调用（哪怕参数相同）分配新 ID。快照已由 pi_resume 装配进
            # self._snapshot，这里直接用它，不再从 state 里重复取。
            self._host_caller = HostToolCaller(PiCallRegistry(self._snapshot))

        # 第七阶段：把本次动作的恢复事实（已提交工具结果引用）与 settle_action
        # 同一事务写进快照。build_snapshot 在网关结算前被调用，返回序列化的
        # 版本化 Pi 快照。tool_result 由网关归一（含 step_id/action_id/attempt_id/
        # data_ref），这里只取结果引用，不把权限/租约混进快照。
        from webpage_agent.agent import pi_snapshot

        def build_snapshot(tool_updates: dict, tool_result=None):
            self._snapshot['logical_call_seq'] = self._host_caller.registry.seq
            committed = {
                'step_id': str((tool_result or {}).get('step_id') or ''),
                'action_id': str((tool_result or {}).get('action_id') or ''),
                'attempt_id': str((tool_result or {}).get('attempt_id') or ''),
                'data_ref': str((tool_result or {}).get('data_ref') or ''),
            }
            existing = list(self._snapshot.get('committed_tool_results') or [])
            existing.append(committed)
            self._snapshot['committed_tool_results'] = existing
            return pi_snapshot.serialize(self._snapshot)

        receipt = await self._host_caller.call(
            call_id=call_id, tool_name=tool_name, args=args, state=state,
            build_snapshot=build_snapshot)
        if receipt.ok:
            # 成功结算后合并认可增量回宿主 state（documents/usage/errors）
            if receipt.updates:
                for key, value in receipt.updates.items():
                    state[key] = value
            reply = proto.make_message(
                bridge.execution_id,
                "tool_result",
                call_id=call_id,
                ok=True,
                result=receipt.content,
            )
        else:
            reply = proto.make_message(
                bridge.execution_id,
                "tool_result",
                call_id=call_id,
                ok=False,
                result=None,
                error=receipt.error,
            )
        await bridge.send(reply)

    async def _handle_model_request(self, bridge: PiBridge, msg: dict) -> None:
        """模型请求准入握手：Node 请求真正调用模型前，宿主检查预算并放行/拒绝。

        复用 core/services.begin_model_request（预算 hook + governance.reserve）。
        拒绝时回 allow=False，Node 不得 stream。
        """
        try:
            proto.validate_model_request(msg, bridge.execution_id)
        except ProtocolError as exc:
            raise PiBridgeError(
                f"非法 model_request：{exc}", stderr_tail=bridge.stderr_tail()
            ) from exc
        request_id = msg["request_id"]
        try:
            reservation = await self._begin_model(request_id)
        except Exception as exc:  # noqa: BLE001 预算耗尽/无 scope 都按拒绝处理
            logger.warning("模型请求准入失败：%s", exc)
            await bridge.send(proto.make_message(
                bridge.execution_id, "model_admit", request_id=request_id, allow=False,
                reason=getattr(exc, 'safe_message', None) or type(exc).__name__))
            return
        self._model_reservations[request_id] = reservation
        await bridge.send(proto.make_message(
            bridge.execution_id, "model_admit", request_id=request_id, allow=True))

    async def _handle_model_usage(self, bridge: PiBridge, msg: dict) -> None:
        """模型用量结算握手：Node 上报 usage，宿主结算后回 ack。

        复用 core/services.end_model_request（governance.finish）。缺 usage 时
        保守估算，不填零。结算完成后回 ack，Node 才允许下一轮模型请求。
        """
        try:
            proto.validate_model_usage(msg, bridge.execution_id)
        except ProtocolError as exc:
            raise PiBridgeError(
                f"非法 model_usage：{exc}", stderr_tail=bridge.stderr_tail()
            ) from exc
        request_id = msg["request_id"]
        reservation = self._model_reservations.pop(request_id, None)
        usage = msg.get("usage")
        outcome = msg.get("outcome")
        settled = True
        try:
            await self._end_model(reservation, usage=usage, succeeded=(outcome == "succeeded"))
        except Exception as exc:  # noqa: BLE001
            # 第七阶段：结算失败**不得**静默确认。回 ok=False，Node 停止后续推理；
            # 未决费用沿用现有存储记录（provider_charges 的 reserved/未完成行）
            # 供对账，不填零冒充已结算。
            logger.warning("模型用量结算失败：%s", exc)
            settled = False
        ack = proto.make_message(
            bridge.execution_id, "model_usage_ack", request_id=request_id, ok=settled)
        await bridge.send(ack)

    async def _handle_checkpoint(self, bridge: PiBridge, msg: dict) -> None:
        """检查点握手：Node 在完整轮次结束 / 工具候选确定时导出版本化上下文，
        宿主持久化后回 checkpoint_ack，Node 才允许后续派发。

        第七阶段：快照里的 messages / pending_calls / checkpoint_seq 由这里更新
        并落库（`pi_snapshot` 版式）。持久化失败**不回 ok=True**——否则 Node 会
        认为进度已安全落库、继续派发，而实际上恢复会漏结果。
        """
        try:
            proto.validate_checkpoint(msg, bridge.execution_id)
        except ProtocolError as exc:
            raise PiBridgeError(
                f"非法 checkpoint：{exc}", stderr_tail=bridge.stderr_tail()
            ) from exc
        seq = int(msg["checkpoint_seq"])
        self._snapshot['checkpoint_seq'] = seq
        self._snapshot['messages'] = list(msg.get("messages") or [])
        self._snapshot['pending_calls'] = list(msg.get("pending_calls") or [])
        self._snapshot['phase'] = (
            'awaiting_tools' if self._snapshot.get('pending_calls') else 'running')
        ok = await self._persist_snapshot()
        await bridge.send(proto.make_message(
            bridge.execution_id, "checkpoint_ack", checkpoint_seq=seq, ok=ok))

    async def _persist_snapshot(self) -> bool:
        """把当前版本化 Pi 快照落库（经账本的 store）。无账本/无 store 返回 False。"""
        from webpage_agent.agent import pi_snapshot
        from webpage_agent.agent import runtime_identity
        from webpage_agent.store import ledger as ledger_module
        book = ledger_module.current_ledger()
        if book is None or getattr(book, 'store', None) is None:
            return False
        try:
            await book.store.save_snapshot(
                book.run_id, plan_version=int(book.plan_version or 1),
                state=pi_snapshot.serialize(self._snapshot),
                snapshot_schema=runtime_identity.PI_STATE_SCHEMA,
                intent_version=int(self._snapshot.get('intent_version') or 0),
                processed_intent_version=int(
                    self._snapshot.get('processed_intent_version') or 0),
                observation_seq=int(self._snapshot.get('observation_seq') or 0))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("持久化 Pi 快照失败：%s", exc)
            return False

    async def _begin_model(self, request_id: str):
        """一次真实模型请求的准入：复用 core/services.begin_model_request。"""
        from webpage_agent.core import services as services_module
        return await services_module.begin_model_request(
            model=self._model.get("id"), purpose="pi_turn")

    async def _end_model(self, reservation, *, usage=None, succeeded: bool) -> None:
        """一次真实模型请求的结算：复用 core/services.end_model_request。"""
        from webpage_agent.core import services as services_module
        await services_module.end_model_request(
            reservation, usage=usage, succeeded=succeeded)

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
