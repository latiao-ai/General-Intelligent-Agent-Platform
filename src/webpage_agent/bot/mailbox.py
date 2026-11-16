"""机器人之间的投递：邮箱 + 收据状态机。

一次私信不是一个函数调用，而是**一行收据 + 一行目标会话的 turn**：

```
A 的回合调 message_bot(target=researcher, text=...)
  ├─ 解析目标（handle → 本主体的 active 机器人）
  ├─ delivery_id = sha256(turn_id | from | to | body)   ← 重试复用，天然幂等
  ├─ 落 bot_deliveries(status=queued)                    ← 崩溃后仍然知道"发没发出去"
  └─ 立刻返回 {status:'queued', delivery_id}             ← 不拿回复
        │
        └─ Worker 的 pump（每拍一次）
             ├─ queued → 建目标规范会话的一轮 turn（request_id=botdm:<id>）
             │            → 收据 claimed
             └─ claimed → 回合终态? → settled/failed
                          → 成功且深度未超限：给发送方回投一次（kind=reply, hop+1）
                          → 超限：只落一条消息，不再起回合（防互相刷屏）
```

为什么不用进程内队列：`docs/state-machine.md` 的同一条纪律——进程会重启，
队列里的东西会丢；数据库里的收据不会。为什么"只重试一次"：目标没活人、
租户不匹配这类失败重试也不会好，只有 `queued → claimed` 这一步值得重试
（而它本身是幂等的）。

状态：

| 状态 | 含义 | 会不会自动再跑 |
| --- | --- | --- |
| `queued` | 收据已落，目标回合还没建 | 会（每拍扫一次） |
| `claimed` | 目标回合已建，等它跑完 | 不会（等回合终态） |
| `settled` | 目标给出了答复 | 不会 |
| `failed` | 目标回合失败 | 不会 |
| `refused` | 根本没投出去（目标不存在/超深度/归属不符） | 不会 |
| `ambiguous` | 回合状态不明（进程崩溃等） | **永不自动重放** |
"""
from __future__ import annotations

import hashlib
import logging

from webpage_agent.bot import prompts

logger = logging.getLogger(__name__)

# 收据状态。和 Hermes 的七态一一对应，少一个 `cancelled`（房间关闭时才用）。
STATUS_QUEUED = 'queued'
STATUS_CLAIMED = 'claimed'
STATUS_SETTLED = 'settled'
STATUS_FAILED = 'failed'
STATUS_REFUSED = 'refused'
STATUS_AMBIGUOUS = 'ambiguous'
TERMINAL_STATUSES = frozenset({STATUS_SETTLED, STATUS_FAILED, STATUS_REFUSED})

# 默认允许的最大转述深度：A→B（0）→A（1）→B（2）之后不再自动回。
DEFAULT_MAX_HOPS = 2


def delivery_id_for(*, turn_id: str, from_bot: str, to_bot: str, body: str) -> str:
    """幂等键：由发送方按"哪个回合、发给谁、说了什么"算出来。

    用 `turn_id` 而不是随机 nonce：同一轮里模型重试同一个工具调用（或进程在
    "落收据"和"返回结果"之间崩掉后整轮被重跑）会算出同一个 id，于是只会有一行
    收据、一条目标回合。**不要**把时间戳放进来——那正好会让幂等失效。
    """
    raw = '\n'.join([str(turn_id or ''), str(from_bot or ''), str(to_bot or ''),
                     str(body or '')])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def reply_delivery_id(parent_id: str) -> str:
    """回执的稳定 id：一条收据最多派生一条回执。"""
    return hashlib.sha256(f'reply:{parent_id}'.encode('utf-8')).hexdigest()[:32]


class BotMailbox:
    """投递服务：`deliver()` 给模型用，`pump()` 给 Worker 用。"""

    def __init__(self, store, *, max_hops: int = DEFAULT_MAX_HOPS):
        self.store = store
        self.max_hops = max(0, int(max_hops))

    # ---------- 发送侧 ----------

    async def deliver(self, *, from_bot: dict, to_handle: str, body: str,
                      turn_id: str, hop: int = 0) -> dict:
        """把一条私信落成收据。**不等待目标回合**（异步邮箱语义）。

        `from_bot` / `to_handle` 都只在本主体名下解析，跨租户一律拒绝：
        机器人不是后门，"知道别人的 handle"不等于有权限给他发消息。
        """
        text = str(body or '').strip()
        if not text:
            return {'status': 'refused', 'reason': 'empty_message',
                    'detail': '消息内容不能为空'}
        tenant = str(from_bot.get('tenant_id') or 'default')
        owner = str(from_bot.get('owner_user_id') or 'default')
        from_id = str(from_bot.get('bot_id') or '')
        handle = str(to_handle or '').strip().lstrip('@').lower()
        target = await self.store.bot_row_by_handle(handle, tenant_id=tenant,
                                                   owner_user_id=owner)
        if target is None:
            return {'status': 'refused', 'reason': 'unknown_target',
                    'detail': f'没有找到 @{handle}。可在机器人面板的花名册里核对 handle。',
                    'available': [item.get('handle') for item in
                                  await self.store.list_bot_rows(
                                      tenant_id=tenant, owner_user_id=owner)]}
        if str(target.get('bot_id') or '') == from_id:
            return {'status': 'refused', 'reason': 'self_target',
                    'detail': '不要给自己发消息；直接把这个结论写进答复。'}

        to_id = str(target.get('bot_id') or '')
        delivery_id = delivery_id_for(turn_id=turn_id, from_bot=from_id,
                                      to_bot=to_id, body=text)
        row, created = await self.store.create_delivery(
            delivery_id=delivery_id, tenant_id=tenant, owner_user_id=owner,
            from_bot=from_id, to_bot=to_id, body=text, kind='dm',
            hop=int(hop), max_hops=self.max_hops)
        if not created:
            # 同一个回合里重复发同一条：返回同一张收据，不再排队。
            return {'status': str(row.get('status') or STATUS_QUEUED),
                    'delivery_id': delivery_id, 'to': f'@{handle}',
                    'replayed': True,
                    'detail': '同一条消息已经受理，不要重复发送。'}
        logger.info('机器人投递入队 %s: %s → %s', delivery_id, from_id, to_id)
        return {'status': STATUS_QUEUED, 'delivery_id': delivery_id,
                'to': f'@{handle}', 'target': target.get('name') or to_id,
                'replayed': False,
                'detail': '已受理。对方是异步处理的：答复会作为一条新消息出现在本会话里，'
                          '不要在同一轮反复催。'}

    # ---------- Worker 侧 ----------

    async def pump(self, *, limit: int = 5) -> dict:
        """一拍：先收敛已建回合的收据，再把排队的收据变成目标回合。"""
        stats = {'claimed': 0, 'settled': 0, 'failed': 0, 'refused': 0,
                 'replied': 0}
        await self._settle_open(stats)
        pending = await self.store.claim_pending_deliveries(limit=max(1, int(limit)))
        for row in pending:
            try:
                await self._dispatch(row, stats)
            except Exception:                      # noqa: BLE001
                # 单条投递失败不能带走整拍；留 queued 下一拍再试（收据还在）。
                logger.exception('投递 %s 派发失败', row.get('delivery_id'))
        return stats

    async def _dispatch(self, row: dict, stats: dict) -> None:
        delivery_id = str(row.get('delivery_id') or '')
        target = await self.store.bot_row(
            str(row.get('to_bot') or ''), tenant_id=str(row.get('tenant_id') or ''),
            owner_user_id=str(row.get('owner_user_id') or ''))
        if target is None or str(target.get('status') or '') != 'active':
            await self.store.mark_delivery(delivery_id, status=STATUS_REFUSED,
                                           reason='target_unavailable')
            stats['refused'] += 1
            return
        hop = int(row.get('hop') or 0)
        max_hops = int(row.get('max_hops') or self.max_hops)
        sender = await self.store.bot_row(str(row.get('from_bot') or '')) or {}
        session = await self.store.ensure_bot_session(
            bot_id=str(target.get('bot_id') or ''),
            tenant_id=str(target.get('tenant_id') or 'default'),
            owner_user_id=str(target.get('owner_user_id') or 'default'),
            session_kind='bot_chat', title=prompts.BOT_CHAT_TITLE)
        session_id = str(session.get('session_id') or '')
        if not session_id:
            await self.store.mark_delivery(delivery_id, status=STATUS_REFUSED,
                                           reason='no_target_session')
            stats['refused'] += 1
            return

        body = str(row.get('body') or '')
        if hop > max_hops:
            # 超深度：只留一条可见消息，不再起回合——否则两个机器人会互相刷。
            await self.store.append_message(
                session_id, 'user',
                prompts.relay_limit_note(sender, body, hop=hop, max_hops=max_hops),
                tenant_id=str(target.get('tenant_id') or 'default'),
                owner_user_id=str(target.get('owner_user_id') or 'default'),
                dedupe_key=f'delivery:{delivery_id}')
            await self.store.mark_delivery(delivery_id, status=STATUS_REFUSED,
                                           reason='hop_limit', session_id=session_id)
            stats['refused'] += 1
            return
        text = (prompts.reply_turn_text(sender, body, hop=hop, max_hops=max_hops)
                if str(row.get('kind') or '') == 'reply'
                else prompts.dm_turn_text(sender, body, hop=hop, max_hops=max_hops))
        turn = await self._create_turn(
            session_id=session_id, tenant_id=str(target.get('tenant_id') or 'default'),
            owner_user_id=str(target.get('owner_user_id') or 'default'),
            request_id=f'botdm:{delivery_id}', message=text)
        await self.store.mark_delivery(
            delivery_id, status=STATUS_CLAIMED, turn_id=str(turn.get('turn_id') or ''),
            session_id=session_id, attempts=int(row.get('attempts') or 0) + 1)
        stats['claimed'] += 1

    async def _create_turn(self, *, session_id: str, tenant_id: str,
                           owner_user_id: str, request_id: str,
                           message: str) -> dict:
        """把一条投递变成目标会话的一轮聊天 turn（幂等）。"""
        from webpage_agent.api.hub_messages import request_hash

        # turn_id 由 request_id 派生：同一张收据永远指向同一行 turn。真正防双跑的
        # 不是这个 id，而是 `create_turn` 在会话行锁内先查 `(session_id, request_id)`
        # 再插入——重复派发会拿回 `replayed=True` 的原行，不会建第二次。
        turn_id = 'dm' + hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:10]
        return await self.store.create_turn(
            turn_id, session_id, request_id=request_id,
            request_hash=request_hash(message, ''), message=message,
            kind='chat', tenant_id=tenant_id, owner_user_id=owner_user_id,
            owner_role='member')

    async def _settle_open(self, stats: dict) -> None:
        """收敛 claimed/ambiguous：回合终态 → 收据终态 → 必要时回投答复。"""
        rows = await self.store.open_deliveries(limit=20)
        for row in rows:
            turn_id = str(row.get('turn_id') or '')
            if not turn_id:
                continue
            turn = await self.store.turn(turn_id)
            if turn is None:
                # 回合行被清理掉了：这张收据永远等不到终态。记失败而不是 ambiguous，
                # 否则每一拍都会再查一次、永远查不到（"未知"要留给人判断，不是死循环）。
                await self.store.mark_delivery(
                    str(row.get('delivery_id') or ''), status=STATUS_FAILED,
                    reason='turn_missing')
                continue
            status = str(turn.get('status') or '')
            if status not in ('succeeded', 'failed', 'cancelled'):
                continue
            ok = status == 'succeeded'
            answer = str(turn.get('answer') or '')
            error = str(turn.get('error') or status)
            settled_rows = await self.store.settle_delivery_by_turn(
                turn_id, ok=ok, answer=answer, error=error)
            stats['settled' if ok else 'failed'] += len(settled_rows)
            if not ok:
                continue
            for settled in settled_rows:
                try:
                    await self._reply(settled, answer, stats)
                except Exception:                  # noqa: BLE001
                    logger.exception('回投答复失败 %s', settled.get('delivery_id'))

    async def _reply(self, parent: dict, answer: str, stats: dict) -> None:
        """把目标机器人的答复回投给发送方（一条 reply 收据 + 可能的一轮 turn）。"""
        from_id = str(parent.get('to_bot') or '')
        to_id = str(parent.get('from_bot') or '')
        if not from_id or not to_id:
            return
        hop = int(parent.get('hop') or 0) + 1
        max_hops = int(parent.get('max_hops') or self.max_hops)
        sender = await self.store.bot_row(
            from_id, tenant_id=str(parent.get('tenant_id') or ''),
            owner_user_id=str(parent.get('owner_user_id') or ''))
        if sender is None:
            return
        reply_id = reply_delivery_id(str(parent.get('delivery_id') or ''))
        row, created = await self.store.create_delivery(
            delivery_id=reply_id, tenant_id=str(parent.get('tenant_id') or 'default'),
            owner_user_id=str(parent.get('owner_user_id') or 'default'),
            from_bot=from_id, to_bot=to_id,
            body=str(answer or '')[:8000], kind='reply', hop=hop,
            max_hops=max_hops)
        if not created:
            return
        logger.info('机器人答复回投 %s: %s → %s hop=%s', reply_id, from_id, to_id, hop)
        stats['replied'] += 1
        # 回执本身也是**一张排队收据**，由下一拍 `_dispatch` 统一处理：
        # 深度未超限 → 给发送方起一轮 turn；超限 → 只落一条可见消息。
        # 两条路径共用一份派发逻辑，避免"回投"和"首发"各写一套、行为发散。

    # ---------- 唤醒（供 UI 判断"这个机器人现在能不能回"） ----------

    async def reachable(self, bot_id: str, *, tenant_id: str,
                        owner_user_id: str) -> dict:
        """目标机器人此刻是否可投递：只有 active + 有规范会话才算。

        本项目没有"每机器人一个 gateway"，执行者是共享 Worker，所以"可投递"
        的判据是身份行与会话行都在，而不是"某个进程活着"。Worker 全部离线时，
        消息会留在 `queued`（收据里看得见），不会假装成功。
        """
        bot = await self.store.bot_row(bot_id, tenant_id=tenant_id,
                                       owner_user_id=owner_user_id)
        if bot is None:
            return {'reachable': False, 'reason': 'unknown_bot'}
        if str(bot.get('status') or '') != 'active':
            return {'reachable': False, 'reason': f'bot_{bot.get("status")}'}
        session = await self.store.bot_session(bot_id)
        return {'reachable': bool(session),
                'reason': '' if session else 'no_session',
                'session_id': (session or {}).get('session_id') or ''}


