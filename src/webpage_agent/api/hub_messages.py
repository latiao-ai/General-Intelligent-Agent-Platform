"""会话消息入口：路由 + turn 持久化 + 会话执行（第十一阶段）。

把「用户发来的一句话」路由成三类，而不是一律建成业务任务：

- `chat`：闲聊/寒暄（"你好"），只产生聊天答复，不建任务、不更新任何
  `intent_version`；
- `query`：只读查询（"今天天气怎么样"），用明确开放的只读工具，缺参数追问；
- `control`：明确目标下的任务控制（建任务/补充/暂停/取消），经宿主控制工具，
  复用现有 Hub/Store 入口，逐项开放；
- `ambiguous`：有多个候选目标（"把它改蓝"），追问并保存待澄清，不自动投递。

路由是**纯逻辑**（可单测），不碰数据库；执行由宿主据 turn 的 kind 落地。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid

from webpage_agent.core import outcome as outcome_module
from webpage_agent.core.config import settings
from webpage_agent.store.pg_store import TurnRequestConflict

logger = logging.getLogger(__name__)


def request_hash(message: str, target: str) -> str:
    """请求指纹：同 request_id 同指纹重放，不同指纹冲突。"""
    body = '\n'.join([str(message or ''), str(target or '')])
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


class CapabilityDisabled(RuntimeError):
    """某项会话能力被开关关闭，且关闭时明确拒绝（不接纳后搁置）。

    A 组第 5 点：API 接纳、Worker 调度、执行入口三处都用同一判断；关闭时抛此
    异常，由 API 层映射为明确的 HTTP 错误，而不是先落 turn 再等 Worker 判失败。
    """

    def __init__(self, kind: str, reason: str = ''):
        self.kind = kind
        super().__init__(reason or f'{kind} 能力未开放')


def kind_enabled(kind: str) -> bool:
    """统一的能力开关判断（A 组第 5 点）：API 接纳、Worker 调度、执行入口共用。

    - `chat` → `settings.session_chat_enabled`
    - `query` → `settings.session_query_enabled`
    - `control` → `settings.session_task_control_enabled`
    - `ambiguous` → 总是 True（它是路由中间态，追问不是"能力"）
    - 其余（未知）→ 关闭（保守拒绝）。
    """
    if kind == 'chat':
        return settings.session_chat_enabled
    if kind == 'query':
        return settings.session_query_enabled
    if kind == 'control':
        return settings.session_task_control_enabled
    if kind == 'ambiguous':
        return True
    return False


# 控制动词：明确要操作某个任务。只有命中这些词 + 有明确目标，才路由到 control。
_CONTROL_VERBS = ('建', '创建', '新建', '列出', '查进度', '补充', '暂停', '恢复',
                  '继续', '取消', '停止', '停下', '修改', '改')

# 任务进度查询词：指向本地活动任务（第十一阶段第 5 点）。有活动任务时优先于
# 外部查询——"查一下进度"应读本地任务进度，而不是让模型去外部检索。
_PROGRESS_HINTS = ('进度', '状态', '到哪了', '怎么样了', '如何了', '查进度', '查状态')

# 查询意图词：明确要读外部事实（天气/时间/数量），用只读工具。缺参数追问。
_QUERY_HINTS = ('天气', '几度', '温度', '下雨', '多少', '几点', '现在时间', '查询',
                '搜索', '查一下', '今天日期', '几号')


def route_message(message: str, *, active_tasks: list[dict] | None = None,
                  target_task_id: str = '') -> dict:
    """把一句话路由到 turn 的 kind（纯逻辑，可单测）。

    `active_tasks`：该会话下未结束的任务列表（每项含 task_id / message / status）。
    `target_task_id`：请求里**显式**指定的目标（可选，客户端带上的才可信）。

    规则（第十一阶段第 6 点）：
    - 显式目标 → 若属于本会话的活动任务则 `control`（目标明确），否则仍归
      `chat`（目标不存在/越权由执行层拒绝）；
    - 命中控制动词 + 恰好一个活动任务 → `control`；
    - 命中控制动词 + 多个活动任务 → `ambiguous`（追问，不自动投递）；
    - 只读查询（缺控制动词）→ `query`；
    - 其余 → `chat`。
    """
    text = ' '.join(str(message or '').split())
    active = [t for t in (active_tasks or []) if isinstance(t, dict)]
    has_control = any(verb in text for verb in _CONTROL_VERBS)
    has_progress = any(hint in text for hint in _PROGRESS_HINTS)

    if target_task_id:
        matched = [t for t in active if str(t.get('task_id')) == target_task_id]
        if matched:
            return {'kind': 'control', 'target_task_id': target_task_id,
                    'target_run_id': str(matched[0].get('run_id') or '')}
        # 显式目标但不在活动任务里：归 chat，由执行层判定（可能已结束/越权）
        return {'kind': 'chat', 'target_task_id': target_task_id,
                'target_run_id': ''}

    # 任务进度查询：有活动任务 + 进度查询词 → 读本地任务进度（第十一阶段第 5 点）。
    # 多个活动任务时追问查哪个（不误读、不误改）。
    if has_progress and active:
        if len(active) == 1:
            return {'kind': 'control',
                    'target_task_id': str(active[0].get('task_id') or ''),
                    'target_run_id': str(active[0].get('run_id') or '')}
        return {'kind': 'ambiguous', 'target_task_id': '',
                'target_run_id': '',
                'candidates': [str(t.get('task_id') or '') for t in active]}

    if has_control:
        if len(active) == 1:
            return {'kind': 'control',
                    'target_task_id': str(active[0].get('task_id') or ''),
                    'target_run_id': str(active[0].get('run_id') or '')}
        if len(active) > 1:
            return {'kind': 'ambiguous', 'target_task_id': '',
                    'target_run_id': '',
                    'candidates': [str(t.get('task_id') or '') for t in active]}
        # 有控制动词但无活动任务：可能想新建任务（"建个页面"）→ 交给 control，
        # 执行层据能力开关决定建任务或说明能力未开放。
        return {'kind': 'control', 'target_task_id': '', 'target_run_id': ''}

    # 命中查询意图 → query（用只读工具）；否则 chat（纯闲聊答复）。
    if any(hint in text for hint in _QUERY_HINTS):
        return {'kind': 'query', 'target_task_id': '', 'target_run_id': ''}
    return {'kind': 'chat', 'target_task_id': '', 'target_run_id': ''}


class HubSessionMessageMixin:
    """会话消息入口：持久接纳 + 路由 + 聊天执行。"""

    # 可注入的聊天答复生成器（测试用）：`async (message, kind) -> str`。
    # 默认确定性答复；正式链路经受控模型替身注入，验证「依据真实工具结果作答」。
    chat_responder = None

    async def accept_session_message(
            self, session_id: str, *, request_id: str, message: str,
            target_task_id: str = '', principal=None) -> dict:
        """接纳一条会话消息，返回 202 形状（不等待模型完成）。

        同一 request_id 同载荷重放返回原 turn；不同载荷抛 `TurnRequestConflict`。
        用户消息、待执行 turn、接纳事件同事务提交（`store.create_turn`）。
        """
        tenant = getattr(principal, 'tenant_id', 'default')
        owner = getattr(principal, 'user_id', 'default')
        await self._assert_session_owner(session_id, tenant, owner)

        # 路由（先读活动任务，纯逻辑判定）
        active = await self.store.session_tasks(session_id)
        active = [t for t in active if outcome_module.normalize_status(
            t.get('status') or '') not in outcome_module.TERMINAL_RUN_STATUSES]
        route = route_message(message, active_tasks=active,
                              target_task_id=target_task_id)

        # 继续澄清（第十一阶段第 6 点）：存在等待澄清的歧义 turn 时，把「编号/
        # 候选名」回复映射回候选任务，让下一条回答能继续澄清，而不是新建一次歧义。
        source_turn_id = ''
        if target_task_id:
            route = {'kind': 'control', 'target_task_id': target_task_id,
                     'target_run_id': route.get('target_run_id') or '',
                     'candidates': [], 'clarification': ''}
        elif route.get('kind') == 'ambiguous' and route.get('candidates'):
            # 本身又是歧义：正常追问
            pass
        else:
            pending = await self._pending_clarification(session_id)
            if pending:
                resolved = self._resolve_clarification(message, pending, active)
                if resolved:
                    source_turn_id = pending['turn_id']
                    route = {'kind': 'control',
                             'target_task_id': resolved['task_id'],
                             'target_run_id': resolved.get('run_id') or '',
                             'candidates': [], 'clarification': ''}

        turn_id = uuid.uuid4().hex[:12]
        fingerprint = request_hash(message, target_task_id)
        # 歧义追问：带候选关联 + 追问文案，turn 进入明确等待状态（第十一阶段第 6 点）。
        candidates = route.get('candidates') or []
        clarification = ''
        if route.get('kind') == 'ambiguous':
            clarification = self._ambiguous_prompt(message, candidates, active)

        # 能力开关统一判断（A 组第 5 点）：接纳层对 chat/query 关闭时明确拒绝，
        # 不落 turn 搁置。`control` / `ambiguous` 总是接纳——control 的写能力
        # （建/暂停/取消/补充）开关是**分项**的，须在执行层（`_control_answer`）
        # 按具体意图逐一检查；「查进度」是只读、总是开放。
        route_kind = route.get('kind') or 'chat'
        if route_kind in ('chat', 'query') and not kind_enabled(route_kind):
            raise CapabilityDisabled(route_kind)

        try:
            result = await self.store.create_turn(
                turn_id, session_id, request_id=request_id,
                request_hash=fingerprint, message=message,
                target_task_id=route.get('target_task_id') or '',
                target_run_id=route.get('target_run_id') or '',
                kind=route_kind,
                tenant_id=tenant, owner_user_id=owner,
                candidates=candidates, clarification=clarification,
                source_turn_id=source_turn_id)
        except TurnRequestConflict:
            raise
        return {'turn_id': result['turn_id'],
                'message_id': result.get('message_id'),
                'status': result['status'],
                'kind': result['kind'],
                'target_task_id': result.get('target_task_id') or '',
                'target_run_id': result.get('target_run_id') or '',
                'candidates': candidates,
                'clarification': clarification,
                'replayed': result.get('replayed', False)}

    def _ambiguous_prompt(self, message: str, candidates: list[str],
                          active: list[dict]) -> str:
        """歧义追问文案：列出候选任务，请用户指定目标（第十一阶段第 6 点）。"""
        names = []
        for cid in candidates:
            task = next((t for t in active if str(t.get('task_id')) == cid), None)
            label = (task.get('message') or cid) if task else cid
            names.append(f'「{label}」({cid})')
        joined = '、'.join(names) if names else '、'.join(candidates)
        return (f'你的意思是操作哪个任务？当前有多个进行中的任务：{joined}。'
                f'请指明任务，或回复对应编号。')

    async def _pending_clarification(self, session_id: str) -> dict | None:
        """该会话最近一条等待澄清的歧义 turn（若有），供「下一条回答继续澄清」。"""
        for turn in await self.store.turns_of(session_id):
            if turn.get('status') == 'waiting_clarification' and turn.get('candidates_json'):
                return turn
        return None

    def _resolve_clarification(self, message: str, pending: dict,
                               active: list[dict]) -> dict | None:
        """把「编号 / 候选名」回复映射回候选任务。

        支持：
        - 纯编号（"1" / "2" / "第一个"）→ 按 candidates_json 顺序；
        - 候选任务名/任务 ID 命中 → 对应任务。
        匹配不到返回 None（继续按新消息路由）。
        """
        text = ' '.join(str(message or '').split())
        try:
            candidates = json.loads(pending.get('candidates_json') or '[]')
        except ValueError:
            candidates = []
        if not candidates:
            return None
        # 候选任务（取当前仍活动的）
        by_id = {str(t.get('task_id') or ''): t for t in active}
        # 编号映射（1 起）
        for i, cid in enumerate(candidates, start=1):
            if text in (str(i), f'第{i}', f'第{i}个', f'{i}号'):
                task = by_id.get(str(cid))
                if task:
                    return {'task_id': str(cid),
                            'run_id': str(task.get('run_id') or '')}
        # 任务 ID / 名称命中
        for cid in candidates:
            if text == str(cid) or text in str(cid) or str(cid) in text:
                task = by_id.get(str(cid))
                if task:
                    return {'task_id': str(cid),
                            'run_id': str(task.get('run_id') or '')}
        return None

    async def run_chat_turn(self, session_id: str, *, owner: str = '',
                            ttl: float = 120.0) -> dict | None:
        """领取并执行一个聊天 turn（单会话入口，兼容既有调用）。

        Worker 用 `claim_and_execute_turn`（跨会话、有界并发）；这里按
        session 领取一条并执行，供就地执行与测试使用。
        """
        claimed = await self.store.claim_turn(owner or self.owner_id, ttl,
                                              session_id=session_id)
        if claimed is None:
            return None
        return await self.execute_claimed_turn(claimed)

    async def claim_and_execute_turn(self, owner: str, ttl: float) -> bool:
        """跨会话领取并执行一条聊天 turn（Worker 主循环入口）。

        返回是否真的领到了东西。有界并发由调用方（Worker）的 `session_chat_concurrency`
        额度控制：聊天 turn 与业务 run 分池，长业务/外部轮询不会占满聊天名额。

        开关必须在这里被读取（第十一阶段第 1 点）：聊天/查询关闭时，Worker 不领
        这些 kind 的 turn，避免「接纳后永久搁置」。
        """
        claimed = await self.store.claim_turn(owner, ttl)
        if claimed is None:
            return False
        kind = str(claimed.get('kind') or 'chat')
        gen = int(claimed.get('execution_generation') or 0)
        epoch = int(claimed.get('lease_epoch') or 0)
        lease_owner = claimed.get('lease_owner')
        if kind == 'chat' and not settings.session_chat_enabled:
            # 聊天开关关闭：不能搁置，明确结算为失败并说明原因
            await self.store.settle_turn(
                claimed['turn_id'], status='failed',
                error='聊天能力未开放（SESSION_CHAT_ENABLED=false）',
                lease_owner=lease_owner, lease_epoch=epoch,
                execution_generation=gen,
                answer='抱歉，聊天能力当前未开放。')
            return True
        if kind == 'query' and not settings.session_query_enabled:
            await self.store.settle_turn(
                claimed['turn_id'], status='failed',
                error='查询能力未开放（SESSION_QUERY_ENABLED=false）',
                lease_owner=lease_owner, lease_epoch=epoch,
                execution_generation=gen,
                answer='抱歉，查询能力当前未开放。')
            return True
        await self.execute_claimed_turn(claimed)
        return True

    async def execute_claimed_turn(self, claimed: dict) -> dict | None:
        """执行一条已领取的 turn（第十一阶段第 4 点核心）。

        按 kind 分流：
        - `chat` / `query`：走真实 Pi（Node 桥接 + 模型预算 + 费用结算 + 事件出口）；
        - `control`：走宿主控制工具（建任务/补充/暂停/恢复/取消，逐项检查归属权限）；
        - `ambiguous`：追问（候选关联已存，下一条回答能继续澄清）。

        第十一阶段 A 组第 2 点：执行期间续租（看守协程）；结算检查 owner + epoch +
        代次 + running 状态 + 租约有效期，失租停止执行、不写答复。
        """
        turn_id = str(claimed['turn_id'])
        kind = str(claimed.get('kind') or 'chat')
        message = str(claimed.get('message') or '')
        session_id = str(claimed.get('session_id') or '')
        lease_owner = str(claimed.get('lease_owner') or '')
        lease_epoch = int(claimed.get('lease_epoch') or 0)
        execution_generation = int(claimed.get('execution_generation') or 0)

        # 续租看守：长执行（真实模型）期间定期续租，失租则取消执行。
        lease_ttl = float(settings.session_turn_lease_seconds)
        renew_task = asyncio.create_task(
            self._renew_turn_guard(turn_id, lease_owner, lease_epoch,
                                   lease_ttl),
            name=f'turn-renew:{turn_id}')

        try:
            if kind in ('chat', 'query'):
                answer = await self._run_pi_chat(claimed)
                if answer is None:
                    return None
                ok = await self.store.settle_turn(
                    turn_id, status='succeeded', answer=answer,
                    lease_owner=lease_owner, lease_epoch=lease_epoch,
                    execution_generation=execution_generation)
                if not ok:
                    # 失租：停止，不再发布成功结果（第 2/3 点）
                    logger.warning('turn %s 结算时已失租，丢弃答复', turn_id)
                    return None
                return {'turn_id': turn_id, 'kind': kind, 'answer': answer,
                        'status': 'succeeded'}
            if kind == 'control':
                return await self._run_control_turn(claimed)
            if kind == 'ambiguous':
                return await self._run_ambiguous_turn(claimed)
            # 未知 kind：保守结算失败，不搁置
            await self.store.settle_turn(
                turn_id, status='failed', error=f'未知 turn 类型 {kind}',
                lease_owner=lease_owner, lease_epoch=lease_epoch,
                execution_generation=execution_generation)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            logger.exception('turn %s 执行异常', turn_id)
            await self.store.settle_turn(
                turn_id, status='failed', error=type(exc).__name__,
                lease_owner=lease_owner, lease_epoch=lease_epoch,
                execution_generation=execution_generation)
            return None
        finally:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass

    async def _renew_turn_guard(self, turn_id: str, owner: str,
                                lease_epoch: int, ttl: float) -> None:
        """turn 长执行续租看守（第十一阶段 A 组第 2 点）。

        每 1/3 租约续一次；连续续租失败说明已失租，取消当前执行任务，避免
        「失租后仍继续执行并尝试写答复」。清理（cancel）不能覆盖原异常——这里
        只负责取消，不吞执行异常。
        """
        interval = max(1.0, ttl / 3.0)
        current = asyncio.current_task()
        while True:
            await asyncio.sleep(interval)
            ok = await self.store.renew_turn_lease(
                turn_id, owner, ttl, lease_epoch=lease_epoch)
            if not ok:
                logger.warning('turn %s 续租失败，已失租', turn_id)
                # 失租：取消执行协程（父任务）——失租后不应继续执行
                if current is not None:
                    current.cancel()
                return

    # ---------- 聊天 / 查询：真实 Pi 执行 ----------

    def _pi_model_config(self) -> tuple[dict, str | None]:
        """从配置构造 Pi 模型端点（复用 settings 的 LLM 模型/网关/密钥）。"""
        model = {
            'id': settings.llm_model or 'deepseek-v4-1-flash',
            'base_url': settings.llm_base_url or 'https://www.hi-x.cn/v1',
            'provider': 'deepseek',
            'api': 'openai-completions',
        }
        api_key = settings.llm_key or None
        return model, api_key

    def _build_pi_runtime(self, *, tools=None, tool_objects=None):
        """构造 PiRuntime：受控模型脚本（测试）经环境变量注入，正式链路走真实模型。"""
        from webpage_agent.agent.pi_runtime import PiRuntime
        model, api_key = self._pi_model_config()
        mock_script = None
        raw_mock = _os_getenv('PI_MOCK_SCRIPT')
        if raw_mock:
            try:
                parsed = json.loads(raw_mock)
                if isinstance(parsed, list):
                    mock_script = parsed
            except ValueError:
                mock_script = None
        return PiRuntime(model=model, api_key=api_key,
                         mock_script=mock_script,
                         tools=tools if tools is not None else [],
                         tool_objects=tool_objects)

    async def _chat_history(self, session_id: str, exclude_turn_id: str) -> list:
        """聊天上下文：该 turn 之前的 user/assistant 消息（排除当前 turn 的 user 消息）。"""
        return await self.store.chat_history(session_id, exclude_turn_id)

    async def _run_pi_chat(self, claimed: dict) -> str | None:
        """用真实 Pi 执行一次聊天/查询 turn，返回答复文本（或 None 表示失租/失败）。

        复用 Node 桥接、模型预算（governance.reserve/finish）、费用结算与事件出口。
        普通聊天不建业务 run，但用 turn_id 作为独立执行身份（billing scope 的 run_id）。
        聊天上下文、bridge、消息消费水位按本次执行隔离（每次新 PiRuntime）。
        """
        turn_id = str(claimed['turn_id'])
        message = str(claimed.get('message') or '')
        session_id = str(claimed.get('session_id') or '')
        kind = str(claimed.get('kind') or 'chat')
        tenant_id = str(claimed.get('tenant_id') or 'default')

        # 测试注入的确定性答复生成器：优先（不碰真实 Pi）。
        if self.chat_responder is not None:
            return await self.chat_responder(message, kind)

        # 会话历史（排除当前 turn 的 user 消息）
        history = await self._chat_history(session_id, turn_id)

        # 只读工具目录：chat 不带工具（纯闲聊），query 带只读工具白名单。
        tools: list = []
        tool_objects = None
        if kind == 'query' and settings.session_query_enabled:
            from webpage_agent.agent import pi_host_tools
            from webpage_agent.agent.pi_runtime import default_tool_catalog
            tools = default_tool_catalog()
            # 只读工具对象目录（无 run 时工具调用会因无可信主体被拒绝，
            # 回退说明能力限制——这是诚实的能力边界，不是硬编码「不可用」）。
            tool_objects = None

        runtime = self._build_pi_runtime(tools=tools, tool_objects=tool_objects)

        # 独立 turn 执行身份（A 组第 5 点）：
        # - billing scope 用 turn_id 作为 run_id 标识（费用结算独立于业务 run）；
        # - 绑定 ExecutionContext（run_id=turn_id），让只读工具经 `tool_context`
        #   的 turn 回退取得**当前用户**的可信主体（tenant_id / owner_user_id 已
        #   在接纳时落库到 session_turns），不依赖业务 run 碰巧提供身份。
        from webpage_agent.governance import core as governance
        from webpage_agent.governance import execution_control
        owner_user_id = str(claimed.get('owner_user_id') or 'default')
        exec_ctx = execution_control.ExecutionContext(
            run_id=turn_id,
            lease_owner=str(claimed.get('lease_owner') or ''),
            lease_epoch=int(claimed.get('lease_epoch') or 0))
        billing_token = governance.bind(self.store, tenant_id, turn_id)
        context_token = execution_control.bind(exec_ctx)
        try:
            state = {
                'user_input': message,
                'history': history,
                'task_id': turn_id,
                'run_id': turn_id,
                'owner_user_id': owner_user_id,
                'tenant_id': tenant_id,
                'documents': [], 'mcp_calls': [], 'errors': [], 'usage': [],
                'status': 'created',
            }
            state = await runtime.run(state)
            answer = str(state.get('answer') or '').strip()
            if not answer:
                # 无有效答复：如实说明，不落空答复冒充成功
                return '抱歉，我暂时无法回答这个问题。'
            return answer
        finally:
            execution_control.unbind(context_token)
            governance.unbind(billing_token)

    # ---------- 控制 / 歧义 ----------

    async def _run_control_turn(self, claimed: dict) -> dict | None:
        """执行控制 turn：建任务/补充/暂停/恢复/取消（第十一阶段第 6 点）。

        逐次检查归属、权限及状态；对控制调用去重（request_hash 已在接纳时去重）。
        尚未满足第十阶段前置条件的控制保持关闭，返回明确原因。
        """
        turn_id = str(claimed['turn_id'])
        message = str(claimed.get('message') or '')
        session_id = str(claimed.get('session_id') or '')
        target_task_id = str(claimed.get('target_task_id') or '')
        lease_owner = str(claimed.get('lease_owner') or '')
        lease_epoch = int(claimed.get('lease_epoch') or 0)
        execution_generation = int(claimed.get('execution_generation') or 0)

        answer = await self._control_answer(
            claimed, message, session_id, target_task_id)
        ok = await self.store.settle_turn(
            turn_id, status='succeeded', answer=answer,
            lease_owner=lease_owner, lease_epoch=lease_epoch,
            execution_generation=execution_generation)
        if not ok:
            return None
        return {'turn_id': turn_id, 'kind': 'control', 'answer': answer,
                'status': 'succeeded'}

    async def _control_answer(self, claimed: dict, message: str,
                              session_id: str, target_task_id: str) -> str:
        """按控制意图落地并返回答复文案（写能力未开放时如实说明原因）。"""
        text = ' '.join(str(message or '').split())

        # 查询进度（只读，总是开放）：查看指定任务/运行的状态与进度
        if any(k in text for k in ('查进度', '进度', '状态', '怎么样', '如何了', '到哪')):
            if not target_task_id:
                return '请告诉我要查看哪个任务的进度（例如「查一下 XX 的进度」）。'
            return await self._task_progress_answer(target_task_id)

        # 写能力：建任务/补充/暂停/恢复/取消，逐项检查开关与状态
        if any(k in text for k in ('建', '创建', '新建', '做一个', '帮我做', '生成')):
            if not settings.session_task_create_enabled:
                return ('抱歉，在会话里直接创建业务任务的能力尚未开放'
                        '（SESSION_TASK_CREATE_ENABLED=false）。请用正式任务入口提交。')
            return await self._create_task_answer(session_id, message, claimed)

        if any(k in text for k in ('暂停', '停下', '停止')):
            if not settings.session_task_control_enabled:
                return ('抱歉，在会话里暂停任务的能力尚未开放'
                        '（SESSION_TASK_CONTROL_ENABLED=false）。')
            if not target_task_id:
                return '请告诉我要暂停哪个任务。'
            return await self._pause_answer(target_task_id)

        if any(k in text for k in ('恢复', '继续')):
            if not settings.session_task_control_enabled:
                return ('抱歉，在会话里恢复任务的能力尚未开放'
                        '（SESSION_TASK_CONTROL_ENABLED=false）。')
            if not target_task_id:
                return '请告诉我要恢复哪个任务。'
            return await self._resume_answer(target_task_id)

        if any(k in text for k in ('取消',)):
            if not settings.session_task_control_enabled:
                return ('抱歉，在会话里取消任务的能力尚未开放'
                        '（SESSION_TASK_CONTROL_ENABLED=false）。')
            if not target_task_id:
                return '请告诉我要取消哪个任务。'
            return await self._cancel_answer(target_task_id)

        if any(k in text for k in ('补充', '再加', '改一下', '修改')):
            if not settings.session_task_control_enabled:
                return ('抱歉，在会话里补充任务要求的能力尚未开放'
                        '（SESSION_TASK_CONTROL_ENABLED=false）。')
            if not target_task_id:
                return '请告诉我要给哪个任务补充要求。'
            return await self._supplement_answer(target_task_id, message, claimed)

        # 无法识别的控制意图
        return '我不太确定你想怎么操作，请说得更具体一些（例如「暂停任务」「查一下进度」）。'

    async def _task_progress_answer(self, target_task_id: str) -> str:
        """读取任务进度（只读，总是开放）。"""
        record = await self.get(target_task_id)
        if record is None:
            return f'没有找到任务 {target_task_id}。'
        status = outcome_module.label(record.status, record.state or {})
        return (f'任务 {target_task_id} 当前状态：{status}。'
                f'消息：{record.message}')

    async def _create_task_answer(self, session_id: str, message: str,
                                  claimed: dict) -> str:
        """会话内建任务：复用 Hub.start，source_turn_id 避免重复保存用户消息。"""
        turn_id = str(claimed['turn_id'])
        tenant = str(claimed.get('tenant_id') or 'default')
        owner = str(claimed.get('owner_user_id') or 'default')
        from types import SimpleNamespace
        principal = SimpleNamespace(tenant_id=tenant, user_id=owner,
                                    role='member', platform_id='', subject='')
        # 建任务需要 session 归属与无活动目标冲突；Hub.start 会处理
        try:
            record = await self.start(message, session_id, principal)
        except Exception as exc:                       # noqa: BLE001
            return f'创建任务失败：{type(exc).__name__}'
        return (f'已创建任务 {record.task_id}（run {record.run_id}），'
                f'正在处理：{message}')

    async def _pause_answer(self, target_task_id: str) -> str:
        record = await self.get(target_task_id)
        if record is None:
            return f'没有找到任务 {target_task_id}。'
        result = await self.pause(record.run_id)
        if not result.get('ok'):
            return f'任务 {target_task_id} 当前状态为 {result.get("status") or "未知"}，不能暂停。'
        return f'已请求暂停任务 {target_task_id}。'

    async def _resume_answer(self, target_task_id: str) -> str:
        record = await self.get(target_task_id)
        if record is None:
            return f'没有找到任务 {target_task_id}。'
        resumed = await self.resume(record.run_id)
        if resumed is None:
            return f'任务 {target_task_id} 没有可续跑的计划。'
        return f'已请求恢复任务 {target_task_id}。'

    async def _cancel_answer(self, target_task_id: str) -> str:
        record = await self.get(target_task_id)
        if record is None:
            return f'没有找到任务 {target_task_id}。'
        if not await self.cancel(record.task_id):
            return f'任务 {target_task_id} 已结束，无法取消。'
        return f'已请求取消任务 {target_task_id}。'

    async def _supplement_answer(self, target_task_id: str, message: str,
                                 claimed: dict) -> str:
        record = await self.get(target_task_id)
        if record is None:
            return f'没有找到任务 {target_task_id}。'
        if not record.run_id:
            return f'任务 {target_task_id} 没有正在运行的 run。'
        # 补充要求走 run_inputs 入口（受控，写入 run 要求版本）
        from webpage_agent.agent import run_inputs as run_inputs_module
        try:
            result = await self.append_run_input(
                record.run_id, request_id=f'turn-{claimed["turn_id"]}',
                message=message, principal=None, task_id=target_task_id)
        except run_inputs_module.InputRejected as exc:
            return f'补充要求未接受：{exc.message}'
        return (f'已补充要求（版本 {result["intent_version"]}），'
                f'任务会在下一个边界按新要求调整。')

    async def _run_ambiguous_turn(self, claimed: dict) -> dict | None:
        """歧义 turn：追问，turn 进入明确等待状态（第十一阶段第 6 点）。

        候选关联已存，追问文案作为答复落库；下一条用户消息（带明确目标）能
        继续澄清。不能简单退回 accepted 让 Worker 反复领取。
        """
        turn_id = str(claimed['turn_id'])
        session_id = str(claimed.get('session_id') or '')
        clarification = str(claimed.get('clarification') or '')
        lease_owner = str(claimed.get('lease_owner') or '')
        lease_epoch = int(claimed.get('lease_epoch') or 0)
        execution_generation = int(claimed.get('execution_generation') or 0)
        answer = clarification or '我不确定你想操作哪个任务，请指明目标。'
        # ambiguous 结算为 waiting_clarification（明确等待，不再被领取执行）
        ok = await self.store.settle_turn(
            turn_id, status='waiting_clarification', answer=answer,
            lease_owner=lease_owner, lease_epoch=lease_epoch,
            execution_generation=execution_generation)
        if not ok:
            return None
        return {'turn_id': turn_id, 'kind': 'ambiguous', 'answer': answer,
                'status': 'waiting_clarification'}

    async def _chat_answer(self, kind: str, message: str) -> str:
        """兼容旧调用点的确定性答复（正式路径走 `_run_pi_chat` 的真实 Pi）。"""
        if self.chat_responder is not None:
            return await self.chat_responder(message, kind)
        if kind == 'query':
            return f'（查询）关于「{message}」，我暂时没有可用的只读工具来回答，请补充更多信息。'
        return '你好！有什么可以帮你的吗？'


def _os_getenv(name: str) -> str:
    import os
    return os.environ.get(name, '')
