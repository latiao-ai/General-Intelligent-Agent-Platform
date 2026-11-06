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

import hashlib
import uuid

from webpage_agent.core import outcome as outcome_module
from webpage_agent.store.pg_store import TurnRequestConflict


def request_hash(message: str, target: str) -> str:
    """请求指纹：同 request_id 同指纹重放，不同指纹冲突。"""
    body = '\n'.join([str(message or ''), str(target or '')])
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


# 控制动词：明确要操作某个任务。只有命中这些词 + 有明确目标，才路由到 control。
_CONTROL_VERBS = ('建', '创建', '新建', '列出', '查进度', '补充', '暂停', '恢复',
                  '继续', '取消', '停止', '停下', '修改', '改')

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

    if target_task_id:
        matched = [t for t in active if str(t.get('task_id')) == target_task_id]
        if matched:
            return {'kind': 'control', 'target_task_id': target_task_id,
                    'target_run_id': str(matched[0].get('run_id') or '')}
        # 显式目标但不在活动任务里：归 chat，由执行层判定（可能已结束/越权）
        return {'kind': 'chat', 'target_task_id': target_task_id,
                'target_run_id': ''}

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

        turn_id = uuid.uuid4().hex[:12]
        fingerprint = request_hash(message, target_task_id)
        try:
            result = await self.store.create_turn(
                turn_id, session_id, request_id=request_id,
                request_hash=fingerprint, message=message,
                target_task_id=route.get('target_task_id') or '',
                target_run_id=route.get('target_run_id') or '',
                kind=route.get('kind') or 'chat',
                tenant_id=tenant, owner_user_id=owner)
        except TurnRequestConflict:
            raise
        return {'turn_id': result['turn_id'],
                'message_id': result.get('message_id'),
                'status': result['status'],
                'kind': result['kind'],
                'target_task_id': result.get('target_task_id') or '',
                'target_run_id': result.get('target_run_id') or '',
                'replayed': result.get('replayed', False)}

    async def run_chat_turn(self, session_id: str, *, owner: str = '',
                            ttl: float = 120.0) -> dict | None:
        """领取并执行一个聊天/查询 turn（第十一阶段第 4 点）。

        按 session 内 turn_seq 顺序领取（有界，不会并发修改同一 Pi session）。
        答复生成经 `chat_responder`（可注入，默认确定性）；结算 turn 后写
        assistant 消息。`control` / `ambiguous` turn 不在此处理（走宿主控制工具）。
        """
        claimed = await self.store.claim_turn(owner or self.owner_id, ttl,
                                              session_id=session_id)
        if claimed is None:
            return None
        turn_id = str(claimed['turn_id'])
        kind = str(claimed.get('kind') or 'chat')
        message = str(claimed.get('message') or '')

        if kind not in ('chat', 'query'):
            # 控制/歧义 turn 不在此执行：交还（标记需要宿主处理）。
            await self.store.settle_turn(
                turn_id, status='accepted', lease_owner=claimed.get('lease_owner'),
                lease_epoch=int(claimed.get('lease_epoch') or 0))
            return None

        answer = await self._chat_answer(kind, message)
        await self.store.settle_turn(
            turn_id, status='succeeded', answer=answer,
            lease_owner=claimed.get('lease_owner'),
            lease_epoch=int(claimed.get('lease_epoch') or 0))
        await self.store.finish_turn_message(
            session_id, answer, dedupe_key=f'turn-answer:{turn_id}')
        return {'turn_id': turn_id, 'kind': kind, 'answer': answer,
                'status': 'succeeded'}

    async def _chat_answer(self, kind: str, message: str) -> str:
        """生成聊天/查询答复（可注入 `chat_responder` 覆盖）。"""
        if self.chat_responder is not None:
            return await self.chat_responder(message, kind)
        if kind == 'query':
            return f'（查询）关于「{message}」，我暂时没有可用的只读工具来回答，请补充更多信息。'
        return '你好！有什么可以帮你的吗？'
