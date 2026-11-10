"""PiRuntime：通过 Node 桥接驱动 Pi 推理，实现 `agent.runtime.Runtime` 契约。

第三阶段目标：打通「Python → Node 中的 Pi → Python 假工具 → Pi 答复」这一条
最小链路。生产执行引擎只有 Pi。存量 langgraph 行在进入这里之前
已经改挂，或被宿主停在待核实。

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

脚本化补偿 / 部署（仍是本类，不另开运行时）：

    - `resume_plan.scripted` 或快照 `host_result_state.script` 为真时，不启动
      Node，按步骤顺序走 `HostToolCaller.call_declared`。没有这个标记时，
      `resume_plan.steps` 仍然被忽略——那是只改引擎、不执行步骤的空转。

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
import json
import logging
import re
import time
from pathlib import Path
from dataclasses import replace as dataclass_replace
from typing import Any, Awaitable, Callable

from webpage_agent.agent import pi_protocol as proto
from webpage_agent.agent.pi_bridge import PiBridge, PiBridgeError
from webpage_agent.agent.pi_protocol import ProtocolError
from webpage_agent.core.errors import RecursionBudgetExceeded
from webpage_agent.observe.events import (
    answer_delta,
    reasoning_delta,
    bind_sink,
    drain_events,
    emit_async,
    stage_end,
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


# 模型端点对函数名的要求：1~128 个 ASCII 字母/数字/下划线/连字符。MCP 工具名
# 常带点（如 BeeFTV 的 canvas.get），原样下发会让**整次请求** 400，一个工具都
# 调不了。这里只改「对模型暴露的名字」，Tool 的 run 闭包仍绑定真实 MCP 工具。
_UNSAFE_TOOL_NAME = re.compile(r'[^A-Za-z0-9_-]+')


def _safe_tool_name(name: str) -> str:
    base = _UNSAFE_TOOL_NAME.sub('_', str(name or '')).strip('_')
    return (base or 'tool')[:64]


def sanitize_tool_names(tools: list) -> list:
    """把工具名收成供应商允许的字符集；改名后重名的保留第一个并告警。"""
    seen: set[str] = set()
    result: list = []
    for tool in tools:
        original = getattr(tool, 'name', '')
        safe = _safe_tool_name(original)
        if safe in seen:
            logger.warning('Pi 工具改名后重名，已丢弃：%s', original)
            continue
        seen.add(safe)
        if safe != original:
            logger.info('Pi 工具改名：%s -> %s（仅对模型暴露名）', original, safe)
            tool = dataclass_replace(tool, name=safe)
        result.append(tool)
    return result


def default_tool_catalog() -> list[dict[str, Any]]:
    """Pi 默认可用的真实工具目录：按**显式只读白名单**从可信目录取工具声明。

    第九阶段去硬编码：不再写死 `local:web:search`。白名单定义在
    `pi_host_tools.SYNC_READ_TOOL_KEYS`，这里从能力包 registry 里按白名单
    查找，且只取 `effect == 'read'` 的工具。白名单里声明但未注册 / 非只读的
    工具不进目录。

    `tools=[]`（显式传入空）表示本轮无工具，**不回退**到任何假工具。
    """
    from webpage_agent.agent import pi_host_tools
    from webpage_agent.capabilities import registry
    allowed = set(pi_host_tools.SYNC_READ_TOOL_KEYS)
    declared = []
    for tool in registry.load():
        if tool.tool_key not in allowed:
            continue
        if str(tool.effect or '') != 'read':
            continue
        declared.append(_tool_decl(tool))
    declared.sort(key=lambda item: (item['name'] != 'web_search', item['name']))
    declared.append(pi_host_tools.request_input_decl())
    from webpage_agent.agent.pi_tool_schema import prepare_declarations
    kept, _dropped = prepare_declarations(declared)
    return kept


async def pi_tool_catalog() -> tuple[list[dict[str, Any]], list]:
    """第十阶段：Pi 的完整工具目录 = 本地只读白名单 + MCP 平台工具。

    返回 `(decls, tool_objects)`：
    - `decls`：发给 Node 的 JSON Schema 声明；
    - `tool_objects`：带执行器的 Tool 对象列表（HostToolCaller 按请求工具名
      唯一匹配用）。

    读/写白名单分别来自 `pi_host_tools.SYNC_READ_TOOL_KEYS` /
    `WRITE_TOOL_KEYS`；MCP 工具经 `tools/core.mcp_tools()` 发现（真实 transport）。
    默认不开放写工具（`WRITE_TOOL_KEYS` 空）；第十阶段测试显式配置写白名单后
    才启用受控平台的 update_object。
    """
    from webpage_agent.agent import pi_host_tools
    from webpage_agent.tools import core as tools_core
    from webpage_agent.capabilities import registry as cap_registry

    # 读白名单可经 PI_READ_TOOL_KEYS 扩展。写白名单默认空，且必须再开
    # PI_WRITE_TOOLS_ENABLED 才会并入 PI_WRITE_TOOL_KEYS（见 pi_host_tools）。
    read_keys = pi_host_tools._effective_read_keys()
    write_keys = pi_host_tools._effective_write_keys()
    tool_objects: list = []

    # 本地只读工具
    for tool in cap_registry.load():
        if tool.tool_key in read_keys and str(tool.effect or '') == 'read':
            tool_objects.append(tool)

    # MCP 工具（读/写白名单按 tool_key = mcp:<server>:<name> 匹配）
    try:
        mcp = await tools_core.mcp_tools()
    except Exception:                                # noqa: BLE001
        mcp = []
    from webpage_agent.agent import connector_scope
    allowed_connectors = connector_scope.current()
    if allowed_connectors is not None:
        allowed = set(allowed_connectors)
        def _mcp_server_id(tool) -> str:
            parts = str(getattr(tool, 'tool_key', '') or '').split(':', 2)
            return parts[1] if len(parts) >= 3 else ''
        mcp = [tool for tool in mcp if _mcp_server_id(tool) in allowed]
    for tool in mcp:
        key = getattr(tool, 'tool_key', '') or ''
        effect = str(getattr(tool, 'effect', '') or 'unknown')
        if key in read_keys and effect == 'read':
            tool_objects.append(tool)
        elif key in write_keys:
            tool_objects.append(tool)

    # 名字先收成供应商允许的字符集，声明与执行目录用同一份名字。
    tool_objects = sanitize_tool_names(tool_objects)
    decls = [_tool_decl(t) for t in tool_objects]
    decls.append(pi_host_tools.request_input_decl())
    from webpage_agent.agent.pi_tool_schema import prepare_declarations
    kept, dropped = prepare_declarations(decls)
    if dropped:
        logger.warning('Pi 工具目录丢弃 %s 个参数 schema 不合格的工具', len(dropped))
        dropped_names = {item.get('name') for item in dropped}
        tool_objects = [tool for tool in tool_objects
                        if getattr(tool, 'name', '') not in dropped_names]
    return kept, tool_objects


def _tool_event_args(args) -> dict:
    """工具参数的事件摘要：保留 query 等可读字段，屏蔽凭据。"""
    if not isinstance(args, dict):
        return {}
    secret = ('token', 'secret', 'password', 'authorization', 'cookie', 'api_key')
    out = {}
    for key, value in list(args.items())[:12]:
        name = str(key)
        if any(part in name.lower() for part in secret):
            out[name] = '***'
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[name] = str(value)[:300] if isinstance(value, str) else value
        else:
            try:
                out[name] = json.dumps(value, ensure_ascii=False)[:300]
            except (TypeError, ValueError):
                out[name] = str(value)[:300]
    return out


async def _emit_tool_call(tool_name: str, args) -> None:
    try:
        await emit_async({'type': 'tool_call', 'name': str(tool_name),
                          'args': _tool_event_args(args)})
    except Exception:                              # noqa: BLE001
        logger.debug('tool_call 事件发送失败', exc_info=True)


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
        mock_script: list[dict[str, Any]] | None = None,
        tool_runner: ToolExecutor | None = None,
        tool_objects: list | None = None,
    ) -> None:
        self._bridge = bridge
        # tool_executor 是测试注入的假工具替身；host_caller 是真实受治理调用对象
        # （默认 None → 惰性创建 HostToolCaller）。
        self._tool_executor = tool_executor
        self._host_caller = host_caller
        # tools=None 表示用默认目录（web_search）；tools=[] 表示本轮无工具。
        # 不再回退到 echo/add。
        self._tools = tools if tools is not None else default_tool_catalog()
        # 是否由调用方**显式**给出工具目录。显式目录优先于 run() 里的惰性发现：
        # 发现逻辑只看环境变量（PI_READ/WRITE_TOOL_KEYS），不加这个标记的话，
        # 只要环境里配了白名单，任何显式传入的目录都会在 run() 里被悄悄覆盖成
        # 真实工具目录——调用方的意图被环境变量劫持，且失败时表现为"工具不对"。
        self._tools_explicit = tools is not None
        # 第十阶段：Tool 对象目录（含执行器），供 HostToolCaller 按请求工具名
        # 唯一匹配（读/写白名单）。None 时 HostToolCaller 回退到本地能力 registry。
        self._tool_objects = tool_objects
        self._model = model or {
            "id": "deepseek-v4-1-flash",
            "base_url": "https://www.hi-x.cn/v1",
            "provider": "deepseek",
            "api": "openai-completions",
        }
        self._api_key = api_key
        self._system_prompt = system_prompt
        self._model_timeout_ms = model_timeout_ms
        # 受控模型脚本（测试）：非空时经真实 Node 走确定性模型产出，不依赖外部凭据。
        self._mock_script = mock_script
        # 受控工具执行器（测试）：覆盖真实 web_search 的 tool.run，但仍走完整
        # 受治理结算链路（产生 action/attempt），用于无网络下验证恢复不变量。
        self._tool_runner = tool_runner
        # 同一工具参数校验连续失败的次数。达到上限就停在 waiting_input，
        # 不再把错误回给模型空转。
        self._schema_rejects: dict[str, int] = {}
        # 模型请求握手：request_id → reservation，准入时存、结算时取。
        self._model_reservations: dict[str, Any] = {}
        # 第七阶段：版本化 Pi 快照（检查点/恢复用），run() 里初始化。
        self._snapshot: dict[str, Any] = {}
        # 产品循环：步数、重复指纹、确认边界用的已调用工具。每次 run() 清零。
        self._tool_calls = 0
        self._repeat_counts: dict[str, int] = {}
        self._call_trace: list[dict] = []
        self._progress = None
        self._live_state: dict | None = None
        self._progress_started = 0.0
        self._progress_marked = 0.0
        self._open_stage = ''
        self._stage_opened = 0.0

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
        workspace = str((state or {}).get('workspace_dir') or '').strip()
        if workspace and isinstance(bridge, PiBridge):
            folder = Path(workspace)
            if folder.is_dir() and not folder.is_symlink():
                bridge.cwd = str(folder.resolve())
        answer_parts: list[str] = []
        self._tool_calls = 0
        self._repeat_counts = {}
        self._call_trace = []
        self._progress = progress
        self._live_state = state
        self._progress_started = time.perf_counter()
        self._progress_marked = self._progress_started
        self._open_stage = ''

        # 第七阶段收口：恢复输入用**单一字段** `pi_resume`（快照 + 恢复决策）承载，
        # 由 Hub 的 _pi_resume_plan 装配、run_task 展平进 state。这里只从这一处读，
        # 不在顶层与 resume_plan 里同时维护两份。
        from webpage_agent.agent import pi_snapshot
        pi_resume = state.get('pi_resume') if isinstance(state, dict) else None
        resume_snapshot = (pi_resume or {}).get('snapshot') if pi_resume else None
        self._snapshot = dict(resume_snapshot) if resume_snapshot else \
            pi_snapshot.new_snapshot(user_input=str(state.get('user_input') or ''))

        # 脚本化补偿 / 部署：步骤是宿主定好的，不能交给模型，也不能把「已结算但
        # 没有模型消息」理解成要把结果送回 Node。这条路径在恢复决策之前返回。
        from webpage_agent.agent import pi_script
        script = pi_script.script_of(state, self._snapshot)
        if script is not None:
            token = bind_sink(on_event) if on_event else None
            try:
                await pi_script.execute(self, state, script)
            finally:
                if token is not None:
                    try:
                        await drain_events()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("排空事件失败：%s", exc)
                    unbind_sink(token)
            state['_pi_snapshot'] = pi_snapshot.serialize(self._snapshot)
            return state

        # 恢复决策。resume_finish / resume_reconcile / resume_block / resume_wait
        # 属于「不重新推理」的结局，在进入桥接前直接写回 state 收尾。
        # recovery 是 dict（pi_resume 里 to_dict 后），用 .get 读。
        recovery = (pi_resume or {}).get('recovery') if pi_resume else None
        if isinstance(recovery, dict) and recovery.get('kind') in (
                'resume_finish', 'resume_reconcile', 'resume_block',
                'resume_wait', 'resume_wait_input'):
            kind = recovery['kind']
            if kind == 'resume_finish':
                state['answer'] = recovery.get('final_answer') or ''
                state['pi_outcome'] = 'answered'
                state['status'] = 'succeeded'
            elif kind == 'resume_wait':
                # 第十阶段收口：区分等待原因。快照有 awaiting_approval（审批仍
                # pending）→ waiting_approval；否则（已受理外部任务未完成）→
                # waiting_external。两者都是「持久等待」，不重新推理、不重派发。
                state['pi_outcome'] = 'waiting'
                if self._snapshot.get('awaiting_approval'):
                    state['status'] = 'waiting_approval'
                else:
                    state['status'] = 'waiting_external'
            elif kind == 'resume_wait_input':
                fact = recovery.get('input_request') if isinstance(
                    recovery.get('input_request'), dict) else {}
                question = str(fact.get('question') or recovery.get('reason')
                               or '需要补充信息后才能继续')
                self._mark_waiting_input(state, question)
            else:
                # resume_reconcile / resume_block：结果未定，既不重试也不当成功。
                # 两个字段分工不同，别混用：
                # - errors[].detail 留恢复决策的原始说明（含内部 action_id 与英文
                #   状态），排障要看这个；
                # - state['failure'] 是给用户的回执正文，由 core/messages.py 直接
                #   拼进句子，不能塞内部标识，也不能缺——缺了会退化成默认那句
                #   "有写操作的结果没能确认"，而这里可能只是一次只读检索，等于
                #   凭空告诉用户产生了副作用、要去核对线上数据。
                detail = recovery.get('reason') or '结果未定，需核实后再继续'
                state['pi_outcome'] = 'failed'
                state['status'] = 'needs_reconcile'
                state['failure'] = ('有一步操作执行到一半，没能确认是否已经生效'
                                    '（未重试，避免重复执行）')
                state.setdefault('errors', []).append({
                    'stage': 'pi', 'type': 'RecoveryBlocked', 'detail': detail})
            state['_pi_snapshot'] = pi_snapshot.serialize(self._snapshot)
            return state

        # 第十阶段收口：审批已批准（RESUME_ADMIT 恢复原候选）时，清空 awaiting_approval
        # 事实——这些候选即将重新派发、执行点重查约束，旧等待事实不得残留，否则
        # 下次恢复又会误判「仍有待审批调用」重复恢复。
        if isinstance(recovery, dict) and recovery.get('kind') == 'resume_admit' \
                and self._snapshot.get('awaiting_approval'):
            self._snapshot['awaiting_approval'] = []
            self._snapshot['phase'] = 'running'

        # 绑定宿主 sink：让 Pi 事件经统一的公共事件出口（emit_async）有序转交，
        # 与 LangGraph 节点内部事件走同一套 answer_delta / stage_start / stage_end 契约。
        token = bind_sink(on_event) if on_event else None

        # 第十阶段收口：惰性发现工具目录。当宿主未显式传入工具目录（tools / 
        # tool_objects 都是 None），且环境变量显式扩展了读/写白名单（PI_READ_TOOL_KEYS
        # / PI_WRITE_TOOL_KEYS）时，异步发现完整工具目录（本地只读 + MCP 平台工具），
        # 让正式链路能真实走 MCP transport 的 update_object / get_object / get_operation。
        # 未设置环境变量时保持默认（只读 web_search），不改变生产行为。
        import os as _os
        if not self._tools_explicit and self._tool_objects is None and (
                _os.environ.get('PI_WRITE_TOOL_KEYS')
                or _os.environ.get('PI_READ_TOOL_KEYS')):
            decls, tool_objects = await pi_tool_catalog()
            self._tools = decls
            self._tool_objects = tool_objects

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

            # 白名单装配的最后一道：坏 schema 在这里丢掉，不让它进 start。
            # validate_tool_catalog 仍然要求留下的每一条根都是 type:object。
            from webpage_agent.agent.pi_tool_schema import prepare_declarations
            self._tools, _dropped = prepare_declarations(self._tools)
            proto.validate_tool_catalog(self._tools)

            # 恢复上下文：有已提交的完整消息就带上，Node 据此重建、不重复追加
            # 原始用户输入；无则走首次执行（pushUser + history）。
            resume_messages = None
            resume_seq = 0
            resume_mode = None
            resume_pending = None
            resume_deliver = None
            if resume_snapshot and isinstance(resume_snapshot.get('messages'), list) \
                    and resume_snapshot['messages']:
                resume_messages = resume_snapshot['messages']
                resume_seq = int(resume_snapshot.get('checkpoint_seq') or 0)
                # 第七阶段收口：按恢复决策给 Node 模式（admit/deliver）与待重派发
                # 的原调用（含原 call_id）。resume_finish/reconcile/block/wait 已在
                # 上面提前返回，不会走到这里。
                kind = recovery.get('kind', '') if isinstance(recovery, dict) else ''
                # 第八阶段收口第 4 点：待重派发的原调用以**恢复决策**里的
                # pending_calls 为准（decide / decide_external 都按快照装配，
                # decide_external 的 RESUME_DELIVER 会带上剩余未派发的同步候选）。
                pending = (recovery.get('pending_calls') or []) \
                    if isinstance(recovery, dict) else []
                if kind in ('resume_admit', 'resume_deliver') and pending:
                    resume_mode = 'admit' if kind == 'resume_admit' else 'deliver'
                    resume_pending = [
                        {'call_id': str(p.get('call_id') or ''),
                         'tool_name': str(p.get('tool_name') or ''),
                         'arguments': dict(p.get('arguments') or {})}
                        for p in pending if isinstance(p, dict)
                    ]
                # 第八阶段收口第 3 点：外部任务已完成、结果已读回 → 交给 Node
                # 直接注入 toolResult（不重新派发），再继续推理。deliver_results
                # 由 Hub 装配（含原 toolCallId + 结果内容）。
                if kind == 'resume_deliver':
                    deliver = (recovery.get('deliver_results') or []) \
                        if isinstance(recovery, dict) else []
                    if deliver:
                        resume_deliver = [
                            {'call_id': str(d.get('call_id') or ''),
                             'tool_name': str(d.get('tool_name') or ''),
                             'result': d.get('result'),
                             'is_error': bool(d.get('is_error'))}
                            for d in deliver if isinstance(d, dict)
                        ]

            resume_field = None
            # 第十阶段收口第 5 点：新增的补充要求作为**明确的新消息**进入恢复后
            # context（不只是合并进 user_input 一句话）。pending_inputs 由 Hub 装配
            # （含原文 + 序号），随 resume 传给 Node，Node importContext 后逐条 push。
            pending_inputs = list(state.get('pending_inputs') or [])
            pending_inputs = [
                {'input_seq': int(p.get('input_seq') or 0),
                 'message': str(p.get('message') or '')}
                for p in pending_inputs
                if isinstance(p, dict) and str(p.get('message') or '').strip()
            ]
            if resume_messages:
                resume_field = {'messages': resume_messages,
                                'checkpoint_seq': resume_seq}
                if resume_mode:
                    resume_field['mode'] = resume_mode
                    resume_field['pending_calls'] = resume_pending or []
                if resume_deliver:
                    resume_field['deliver_results'] = resume_deliver
                if pending_inputs:
                    resume_field['pending_inputs'] = pending_inputs

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
                resume=resume_field,
                mock=self._mock_script,
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
                    await self._close_stage('done')
                    self._note_progress('pi', str(state.get('status') or ''))
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
            # 注入的桥接也要在循环还活着时关掉。测试常把真实 PiBridge 交进来
            # 却不自己 close，管道会拖到循环关闭后才析构。
            try:
                await bridge.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭桥接失败：%s", exc)

        # 第八阶段第 1 点：把**真正的版本化 Pi 快照**序列化后交给宿主，供收尾时
        # 落库。宿主的 `_finish_run` 据此提交版本化 Pi 快照（经 `pi_snapshot.
        # serialize` 的结构），而不是给普通宿主 state 打 Pi schema 标签。
        from webpage_agent.agent import pi_snapshot as _snap
        state['_pi_snapshot'] = _snap.serialize(self._snapshot)
        return state

    def _note_progress(self, stage: str, status: str) -> None:
        """节点级进度：写入 timings，并调用宿主的同步 progress 回调。"""
        now = time.perf_counter()
        state = self._live_state
        if isinstance(state, dict):
            state.setdefault('timings', []).append({
                'stage': stage,
                'seconds': round(now - self._progress_marked, 2),
                'status': state.get('status') or status,
            })
        self._progress_marked = now
        if self._progress is not None:
            elapsed = round(now - self._progress_started, 2)
            shown = ''
            if isinstance(state, dict):
                shown = str(state.get('status') or status)
            self._progress(stage, shown, elapsed)

    async def _close_stage(self, status: str) -> None:
        if not self._open_stage:
            return
        elapsed = time.perf_counter() - self._stage_opened
        stage = self._open_stage
        self._open_stage = ''
        await emit_async(stage_end(stage, elapsed, status or 'done'))

    async def _open_named_stage(self, stage: str) -> None:
        if self._open_stage and self._open_stage != stage:
            await self._close_stage('done')
        if self._open_stage == stage:
            return
        self._open_stage = stage
        self._stage_opened = time.perf_counter()
        await emit_async(stage_start(stage))
        self._note_progress(stage, '')

    async def _handle_event(self, msg: dict, answer_parts: list[str]) -> None:
        """转发事件：text_delta 走 answer_delta（text 字段），阶段走 stage 契约。"""
        kind = msg.get("kind")
        if kind == "text_delta":
            delta = str(msg.get("delta", ""))
            answer_parts.append(delta)
            # 公共事件契约：answer_delta 使用 text 字段
            await emit_async(answer_delta(delta))
        elif kind == "thinking_delta":
            await emit_async(reasoning_delta(str(msg.get("delta", ""))))
        elif kind == "progress" or kind == "stage":
            await self._open_named_stage(str(msg.get("label", "") or "pi"))

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
            strikes = self._schema_rejects.get(tool_name, 0) + 1
            self._schema_rejects[tool_name] = strikes
            # 同一工具参数反复不合格：停下来问人，不再把错误喂回模型空转。
            if strikes >= 2:
                await self._park_for_input(
                    bridge, state, call_id, tool_name,
                    f'调用 {tool_name} 的参数仍不完整：{exc}。请补充后继续。')
                return
            await self._send_tool_error(bridge, call_id, f"参数不符合 schema：{exc}")
            return

        from webpage_agent.agent import pi_host_tools
        from webpage_agent.agent import pi_product
        from webpage_agent.core.config import settings
        if tool_name == pi_host_tools.REQUEST_INPUT_NAME:
            question = str((args or {}).get('question') or '').strip()
            if not question:
                await self._send_tool_error(bridge, call_id, 'question 不能为空')
                return
            await self._park_for_input(
                bridge, state, call_id, tool_name, question)
            return

        limit = max(1, int(getattr(settings, 'run_max_tool_calls', 40) or 40))
        if self._tool_calls >= limit:
            raise RecursionBudgetExceeded('这一轮已经用满步数预算', limit=limit)
        fingerprint = pi_product.repeat_key(tool_name, args if isinstance(args, dict) else {})
        stalled = pi_product.repeat_block(
            self._repeat_counts, fingerprint,
            int(getattr(settings, 'run_max_no_progress', 2) or 2))
        if stalled:
            raise RecursionBudgetExceeded(stalled)
        blocked = pi_product.confirmation_block(
            self._call_trace, self._tool_for_guard(tool_name), self._guard_catalog())
        if blocked:
            await self._park_for_input(bridge, state, call_id, tool_name, blocked)
            return
        self._tool_calls += 1
        await _emit_tool_call(tool_name, args)

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
            if ok:
                pi_product.note_repeat(self._repeat_counts, fingerprint)
                self._call_trace.append({'tool': tool_name, 'step_id': call_id, 'args': {}})
            return

        # 真实受治理调用：走 HostToolCaller（授权 → 准入 → 撤权复查 → 真实调用 → 结算）
        if self._host_caller is None:
            from webpage_agent.agent.pi_host_tools import (
                HostToolCaller, PiCallRegistry)
            # 用当前快照重建逻辑调用身份注册表：恢复时复用原调用 ID，
            # 新调用（哪怕参数相同）分配新 ID。快照已由 pi_resume 装配进
            # self._snapshot，这里直接用它，不再从 state 里重复取。
            # 受控工具执行器（测试）：仍走完整受治理结算链路，只是覆盖 tool.run。
            runner = self._tool_runner
            executor = None
            if runner is not None:
                async def executor(tool, args, state):
                    ok, result = await runner(tool.name, args)
                    if not ok:
                        raise RuntimeError(str(result))
                    return result if isinstance(result, dict) else {}
            self._host_caller = HostToolCaller(
                PiCallRegistry(self._snapshot), executor=executor,
                tools_catalog=self._tool_objects)

        # 第七阶段：把本次动作的恢复事实（已提交工具结果引用）与 settle_action
        # 同一事务写进快照。build_snapshot 在网关结算前被调用，返回序列化的
        # 版本化 Pi 快照。tool_result 由网关归一（含 step_id/action_id/attempt_id/
        # data_ref），这里只取结果引用，不把权限/租约混进快照。
        from webpage_agent.agent import pi_snapshot

        def build_snapshot(tool_updates: dict, tool_result=None, external=None):
            self._snapshot['logical_call_seq'] = self._host_caller.registry.seq
            # 第八阶段收口第 4 点：accepted 时**先把当前调用标记为 accepted**，
            # 再导出 pending_calls——否则这个已受理的调用会以 pending 状态残留
            # 在快照里，恢复时被当作待重派发候选再次执行（重复副作用）。
            if external is not None and \
                    (tool_result or {}).get('execution_status') == 'accepted':
                self._host_caller.registry.mark(call_id, 'accepted')
            # 第七阶段收口：把 registry 已分配的逻辑调用身份**回填**进快照的
            # pending_calls。Node 发的检查点里 logical_call_id 是空占位（Node
            # 不知道宿主分配的逻辑 ID），恢复时必须从这里读回原 ID，否则同一次
            # 调用会被当成新调用再分配新 ID、重复执行工具。
            self._snapshot['pending_calls'] = (
                self._host_caller.registry.pending_calls())
            committed = {
                'step_id': str((tool_result or {}).get('step_id') or ''),
                'action_id': str((tool_result or {}).get('action_id') or ''),
                'attempt_id': str((tool_result or {}).get('attempt_id') or ''),
                'data_ref': str((tool_result or {}).get('data_ref') or ''),
            }
            existing = list(self._snapshot.get('committed_tool_results') or [])
            existing.append(committed)
            self._snapshot['committed_tool_results'] = existing
            # 第八阶段收口第 2 点：accepted 时把受理事实（awaiting_calls）在
            # 结算事务里随快照一起写入，不再依赖结算后的第二次快照写补齐。
            # external 由网关在 settle_accepted 前装配并传入，含 external_task_id
            # / action_id / remote_task_id / contract 等本地匹配主键。
            if external is not None and (tool_result or {}).get('execution_status') == 'accepted':
                self._snapshot['phase'] = 'awaiting_external'
                binding = self._host_caller.registry._bindings.get(call_id) or {}
                awaiting = list(self._snapshot.get('awaiting_calls') or [])
                awaiting.append({
                    'call_id': call_id,
                    'logical_call_id': str(binding.get('logical_call_id') or ''),
                    'tool_name': tool_name,
                    'tool_key': str(binding.get('tool_key') or ''),
                    'arguments': dict(args or {}),
                    'external_task_id': str(external.get('external_task_id') or ''),
                    'action_id': str(external.get('action_id')
                                     or (tool_result or {}).get('action_id') or ''),
                    'remote_task_id': str(external.get('remote_task_id') or ''),
                    'contract_id': str(external.get('contract_id') or ''),
                    'contract_version': int(external.get('contract_version') or 1),
                    'status': 'awaiting',
                })
                self._snapshot['awaiting_calls'] = awaiting
            return pi_snapshot.serialize(self._snapshot)

        receipt = await self._host_caller.call(
            call_id=call_id, tool_name=tool_name, args=args, state=state,
            build_snapshot=build_snapshot)
        if receipt.needs_approval:
            # 第十阶段第 5 点：需要批准。动作尚未发出（授权层 ApprovalRequired），
            # 不是失败。把等待审批事实写进快照，结束本轮、释放进程；批准后按快照
            # 恢复原候选重新检查权限/版本/revision。
            # 第十阶段收口：快照保存**完整调用身份 + 最终参数 + 参数指纹 + 对象
            # revision + 要求版本**——批准后恢复必须复用同一份最终参数（含幂等键），
            # 不能靠模型重发的参数冒充原调用，也不能在参数变化后继续执行。
            self._host_caller.registry.mark(call_id, 'accepted')
            self._snapshot['phase'] = 'awaiting_approval'
            binding = self._host_caller.registry._bindings.get(call_id) or {}
            final_args = dict(receipt.final_args or args or {})
            from webpage_agent.store import ledger as _ledger
            args_hash = receipt.args_hash or _ledger.args_hash(final_args)
            awaiting = list(self._snapshot.get('awaiting_approval') or [])
            awaiting.append({
                'call_id': call_id,
                'logical_call_id': str(binding.get('logical_call_id') or ''),
                'tool_name': tool_name,
                'tool_key': str(binding.get('tool_key') or ''),
                'arguments': dict(final_args),
                'args_hash': args_hash,
                'approval_id': str(receipt.approval_id or ''),
                # 对象 revision / 要求版本：批准后重查约束的依据（要求变化或
                # revision 前进都使旧批准失效）。
                'object_id': str(final_args.get('object_id') or ''),
                'expected_revision': final_args.get('expected_revision'),
                'intent_version': int(state.get('intent_version') or 0),
                'status': 'awaiting_approval',
            })
            self._snapshot['awaiting_approval'] = awaiting
            # 等待审批事实落库（与 accepted 类似，持久化后才回 Node 结束本轮）。
            from webpage_agent.agent import pi_snapshot as _snap
            state['_pi_snapshot'] = _snap.serialize(self._snapshot)
            reply = proto.make_message(
                bridge.execution_id, "tool_result", call_id=call_id,
                ok=False, result=None, error='',
                needs_approval=True, approval_id=receipt.approval_id)
            await bridge.send(reply)
            return
        if receipt.accepted:
            # 第八阶段第 5 点：远端已受理、结果未定。宿主确认等待事实落库后，
            # Node 结束本轮执行、释放进程；等待期间不再请求模型。
            # 第八阶段收口第 2 点：受理事实（awaiting_calls，含 external_task_id /
            # action_id / logical_call_id）已在 settle_accepted 的同一事务里随
            # 快照写入（见 build_snapshot），这里**不再二次持久化**——否则崩溃在
            # 「accepted 已提交、awaiting 未落库」之间会漏跟踪。只把 registry
            # 标记为 accepted（区别于 pending 重派发），并回 accepted 回执给 Node。
            self._host_caller.registry.mark(call_id, 'accepted')
            self._snapshot['phase'] = 'awaiting_external'
            reply = proto.make_message(
                bridge.execution_id, "tool_result", call_id=call_id,
                ok=False, result=None, error='',
                accepted=True, remote_task_id=receipt.remote_task_id)
            await bridge.send(reply)
            return
        if receipt.ok:
            pi_product.note_repeat(self._repeat_counts, fingerprint)
            self._call_trace.append({'tool': tool_name, 'step_id': call_id, 'args': {}})
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
        from webpage_agent.agent.context import current_time_prompt
        from webpage_agent.core.config import settings
        time_prompt = current_time_prompt(settings.prompt_timezone)
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
            bridge.execution_id, "model_admit", request_id=request_id, allow=True,
            current_time_prompt=time_prompt))

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

        第七阶段收口：持久化前校验检查点序号（拒绝回退、拒绝同序号不同内容）
        与租约/执行代次（拒绝过期执行者）。持久化失败**不回 ok=True**——否则
        Node 会认为进度已安全落库、继续派发，而实际上恢复会漏结果。
        """
        try:
            proto.validate_checkpoint(msg, bridge.execution_id)
        except ProtocolError as exc:
            raise PiBridgeError(
                f"非法 checkpoint：{exc}", stderr_tail=bridge.stderr_tail()
            ) from exc
        seq = int(msg["checkpoint_seq"])
        new_messages = list(msg.get("messages") or [])
        new_pending = list(msg.get("pending_calls") or [])

        # 序号单调性：回退（新 < 旧）拒绝，恢复时继续原序号、不从零重来。
        prev_seq = int(self._snapshot.get('checkpoint_seq') or 0)
        if prev_seq and seq < prev_seq:
            await bridge.send(proto.make_message(
                bridge.execution_id, "checkpoint_ack", checkpoint_seq=seq,
                ok=False))
            logger.warning('检查点序号回退：%s < %s，拒绝', seq, prev_seq)
            return
        # 同序号不同内容：拒绝（同一序号必须是同一份上下文）。
        if prev_seq and seq == prev_seq:
            prev_messages = self._snapshot.get('messages')
            prev_pending = self._snapshot.get('pending_calls')
            if (prev_messages != new_messages or prev_pending != new_pending):
                await bridge.send(proto.make_message(
                    bridge.execution_id, "checkpoint_ack", checkpoint_seq=seq,
                    ok=False))
                logger.warning('检查点序号 %s 内容变化，拒绝', seq)
                return

        self._snapshot['checkpoint_seq'] = seq
        self._snapshot['messages'] = new_messages
        self._snapshot['pending_calls'] = new_pending
        # 最终答复候选：Node 在 answered 前把 end.answer 随检查点一起交回，
        # 宿主持久化后才允许发 finished。恢复时 resume_finish 据此补收尾，
        # 不重新推理（第七阶段收口）。
        if msg.get('final_answer') is not None:
            self._snapshot['final_answer_candidate'] = str(msg.get('final_answer'))
        self._snapshot['phase'] = (
            'awaiting_tools' if self._snapshot.get('pending_calls') else 'running')
        ok = await self._persist_snapshot()
        await bridge.send(proto.make_message(
            bridge.execution_id, "checkpoint_ack", checkpoint_seq=seq, ok=ok))

    async def _persist_snapshot(self) -> bool:
        """把当前版本化 Pi 快照落库（租约校验 + 序号检查 + 写入同一事务）。

        - **无账本**（独立 smoke / CLI 直调）：没有持久化目标，检查点确认视为
          空操作、放行（返回 True）——这不等于「提交失败」，而是根本没有治理
          账本可写，与 `governed_call` 的兼容路径一致。
        - **有账本但 store 缺失 / 持久化失败 / 失租**：返回 False，停止。

        第八阶段第 1 点：租约校验与快照写入**必须同一事务**。之前是
        「先 `verify_lease` 再独立 `save_snapshot`」两步，两步之间租约可能被
        接管，过期执行者仍能覆盖新执行者的快照。现在走 `save_snapshot_owned`
        （锁 run 行 → 校验 owner/epoch/expires → UPSERT 快照，同一事务），
        失租返回 False、一行都不写。
        """
        from webpage_agent.agent import pi_snapshot
        from webpage_agent.agent import runtime_identity
        from webpage_agent.store import ledger as ledger_module
        book = ledger_module.current_ledger()
        if book is None:
            # 独立 smoke/CLI：无治理账本，检查点无持久化目标，放行。
            return True
        if getattr(book, 'store', None) is None:
            logger.error('有账本但 store 缺失，无法持久化 Pi 快照')
            return False
        context = getattr(book, 'context', None)
        try:
            # 序号检查 + 租约校验 + 写入：序号检查在内存侧（本进程内单调），
            # 租约校验与写入在库里同一事务（跨进程 fencing）。有执行权则走
            # 原子门禁；无执行权（context 为空，独立路径已在上方无账本分支返回）
            # 走普通 save_snapshot（这里 context 为空意味着有账本但无执行权，
            # 属异常，保守拒绝）。
            if context is not None and getattr(context, 'lease_owner', None):
                committed = await book.store.save_snapshot_owned(
                    book.run_id, plan_version=int(book.plan_version or 1),
                    state=pi_snapshot.serialize(self._snapshot),
                    lease_owner=getattr(context, 'lease_owner', ''),
                    lease_epoch=getattr(context, 'lease_epoch', 0),
                    snapshot_schema=runtime_identity.PI_STATE_SCHEMA,
                    intent_version=int(self._snapshot.get('intent_version') or 0),
                    processed_intent_version=int(
                        self._snapshot.get('processed_intent_version') or 0),
                    observation_seq=int(self._snapshot.get('observation_seq') or 0))
                if not committed:
                    logger.warning('持久化 Pi 快照时已失租，拒绝写入')
                    return False
                return True
            logger.error('有账本但无执行上下文，无法原子持久化 Pi 快照')
            return False
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

    def _mark_waiting_input(self, state: dict, question: str) -> None:
        """缺参/需要补充：写宿主状态，清掉旧答复，避免被收成成功。"""
        text = str(question or '').strip() or '需要补充信息后才能继续'
        state['pi_outcome'] = 'waiting'
        state['status'] = 'waiting_input'
        state['failure'] = text
        # clarification 留空：reply_text 在两者相同时会藏起问题正文。
        # 问题放在 failure 里，收尾回执会原样带给用户。
        state['clarification'] = ''
        asked = [str(item) for item in (state.get('asked_questions') or []) if str(item).strip()]
        if text not in asked:
            asked.append(text)
        state['asked_questions'] = asked
        state.pop('answer', None)

    async def _park_for_input(self, bridge: PiBridge, state: dict, call_id: str,
                              tool_name: str, question: str) -> None:
        """把当前工具调用停成 waiting_input，并让 Node 结束本轮。"""
        from webpage_agent.agent import pi_snapshot
        text = str(question or '').strip() or '需要补充信息后才能继续'
        host = dict(self._snapshot.get('host_result_state') or {})
        host['awaiting_input'] = {
            'call_id': call_id,
            'tool_name': tool_name,
            'question': text,
        }
        self._snapshot['host_result_state'] = host
        self._snapshot['phase'] = 'awaiting_input'
        pending = [item for item in (self._snapshot.get('pending_calls') or [])
                   if not (isinstance(item, dict) and item.get('call_id') == call_id)]
        self._snapshot['pending_calls'] = pending
        self._mark_waiting_input(state, text)
        state['_pi_snapshot'] = pi_snapshot.serialize(self._snapshot)
        await self._persist_snapshot()
        reply = proto.make_message(
            bridge.execution_id, 'tool_result', call_id=call_id,
            ok=False, result=None, error='', needs_input=True)
        await bridge.send(reply)

    def _guard_catalog(self) -> list:
        if self._tool_objects:
            return list(self._tool_objects)
        caller = self._host_caller
        catalog = getattr(caller, '_tools_catalog', None) if caller is not None else None
        return list(catalog or [])

    def _tool_for_guard(self, name: str):
        for tool in self._guard_catalog():
            if getattr(tool, 'name', '') == name:
                return tool
        return None

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
            from webpage_agent.agent import pi_product
            gap = pi_product.completion_block(state)
            if gap:
                # 有答复不等于做完：产物缺失或部署回执未确认时不能当成功。
                state["pi_outcome"] = "failed"
                state.pop("answer", None)
                state["failure"] = gap
                state.setdefault("errors", []).append(
                    {"stage": "verify_completion", "type": "UnmetCriteria", "detail": gap})
        elif outcome == "cancelled":
            state["pi_outcome"] = "cancelled"
            # 第七阶段收口：取消**不得**因残留旧 answer 被归一为成功。清除旧答复，
            # 让 final_status 按「未产出有效结果」判定。
            state.pop("answer", None)
        elif outcome == "failed":
            state["pi_outcome"] = "failed"
            err = str(msg.get("error", "Pi 执行失败"))
            state.setdefault("errors", []).append(
                {"stage": "pi", "type": "PiError", "detail": err}
            )
            # 第七阶段收口：失败同样清除旧 answer，避免 final_status 误判成功。
            state.pop("answer", None)
        elif outcome == "waiting":
            # 第十阶段收口：区分等待原因（Node 在 finished 里带 wait_reason）。
            #  - approval：工具需要批准 → run 停在 waiting_approval，等用户批准后
            #    恢复原候选（不是失败，也不是成功）；
            #  - external（缺省）：远端已受理、结果未定 → run 停在 waiting_external，
            #    等 Worker 从原远端任务取得结果后唤醒。
            # 两种情况本轮都**不是**失败也不是成功，而是「持久等待」；不得因残留
            # 旧 answer 归一为成功。
            state["pi_outcome"] = "waiting"
            if msg.get("wait_reason") == "approval":
                state["status"] = "waiting_approval"
                state.pop("answer", None)
            elif msg.get("wait_reason") == "input":
                question = str(
                    ((self._snapshot.get('host_result_state') or {}).get('awaiting_input')
                     or {}).get('question') or '需要补充信息后才能继续')
                self._mark_waiting_input(state, question)
            else:
                state["status"] = "waiting_external"
                state.pop("answer", None)
        else:
            # validate_finished 已保证 outcome 只可能是上述四者，此分支防御性兜底
            state["pi_outcome"] = "failed"
