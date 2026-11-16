"""AgentBridge：把一条 IM 消息变成一次会话 turn，再把答复取回来。

本项目已经有完整的「会话 turn」执行链（`session_turns` + Worker 领取 + Pi 执行 +
结算），所以这里**不另造 Agent 调用**，只做三件事：

1. 为每个 IM 会话映射一条 Agent 会话（`im_sessions`），`/new` 时换新的；
2. 用 `message_id` 作为 `request_id` 建 turn——平台重投同一条消息时命中
   `(session_id, request_id)` 唯一键，不会跑第二遍；
3. 轮询 turn 终态，拿到答复。超时、失败都给用户可读的文案，不暴露堆栈。

轮询而不是 await 回调：回调要跨进程（Worker 才能执行 turn），而 turn 的终态
本来就在库里。轮询的代价是几百毫秒延迟，换来的是与执行进程彻底解耦。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from typing import Awaitable, Callable

from webpage_agent.core.config import settings
from webpage_agent.messaging.models import Inbound
from webpage_agent.messaging.normalize import user_label

logger = logging.getLogger(__name__)

TERMINAL = ('succeeded', 'failed', 'cancelled')
# 这些状态说明 turn 已经被执行者领走，继续等是合理的。
PENDING = ('accepted', 'running', 'retry')


def session_id_for(*, platform: str, tenant_id: str, owner_user_id: str,
                   chat_id: str, generation: int = 1,
                   instance_id: str = '') -> str:
    """一条 IM 会话对应的 Agent 会话 id。

    `instance_id` 进了种子：两个飞书应用（或两个企微应用）可能给出同一个
    `chat_id`，不带实例的话它们会算出同一个 id，第二条 `im_create_session`
    撞上 ON CONFLICT DO NOTHING——两个机器人共用一段对话，人格来回翻。
    """
    seed = (f'{platform}|{tenant_id}|{owner_user_id}|{instance_id}|'
            f'{chat_id}|{generation}')
    return 'im-' + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]


class ImBridge:
    def __init__(self, store, *, timeout: float | None = None,
                 poll_seconds: float = 0.8) -> None:
        self.store = store
        self.timeout = float(timeout or settings.messaging_reply_timeout_seconds)
        self.poll_seconds = max(0.2, float(poll_seconds))

    async def run(self, event: Inbound, *, owner_user_id: str,
                  channel_bot_id: str,
                  instance_id: str = '',
                  session_id: str | None = None,
                  on_delta: Callable[[str], Awaitable[None]] | None = None,
                  on_start: Callable[[], Awaitable[None]] | None = None) -> dict:
        """执行一轮，返回 `{text, session_id, turn_id, status}`。

        `channel_bot_id` 是**渠道默认**身份；`instance_id` 是这条消息从哪个接入
        实例进来的——会话映射按 (渠道, 实例, 会话) 存，两个应用给同一个 chat_id
        时不会互相顶掉。
        """
        session = await self._session(event, owner_user_id=owner_user_id,
                                      channel_bot_id=channel_bot_id,
                                      instance_id=instance_id,
                                      session_id=session_id)
        text = self._compose(event)
        if on_start is not None:
            try:
                await on_start()
            except Exception:                       # noqa: BLE001
                logger.debug('IM typing 回调失败', exc_info=True)
        turn_id = uuid.uuid4().hex
        request_id = f'im:{event.message_id}'
        request_hash = hashlib.sha256(
            f'{event.platform}|{event.chat_id}|{event.user_id}|{text}'.encode('utf-8')
        ).hexdigest()
        try:
            created = await self.store.create_turn(
                turn_id, session['session_id'], request_id=request_id,
                request_hash=request_hash, message=text, kind='chat',
                tenant_id=event.tenant, owner_user_id=owner_user_id)
        except Exception:                          # noqa: BLE001
            logger.exception('IM 建 turn 失败 session=%s', session['session_id'])
            return {'text': '抱歉，消息没能进入执行队列，请稍后重试。',
                    'session_id': session['session_id'], 'turn_id': '',
                    'status': 'error'}
        turn_id = str(created.get('turn_id') or turn_id)
        answer = await self._wait(turn_id, on_delta=on_delta)
        return {'text': answer, 'session_id': session['session_id'],
                'turn_id': turn_id, 'status': 'ok'}

    async def _session(self, event: Inbound, *, owner_user_id: str,
                       channel_bot_id: str, instance_id: str,
                       session_id: str | None) -> dict:
        """决定这条消息该落到哪条 Agent 会话，并保证**人格与会话一致**。

        身份解析只有一条规则：会话覆盖（`/bind` 指定）优先，否则跟随渠道默认。
        真正容易错的是下一条：映射指向的会话可能是上一个人格留下的。历史原因
        这里曾是 `ON CONFLICT DO NOTHING` 直通，于是"界面上把渠道机器人从 A 换成
        B，群里回话的还是 A"，只有 `/new` 才换——界面显示的与实际回话的不是同一
        个人。这里改成：身份不一致就换一代新会话（旧会话连同历史原样留在本机，
        只是不再接着聊）。
        """
        row = await self.store.im_session(
            tenant_id=event.tenant, owner_user_id=owner_user_id,
            platform=event.platform, chat_id=event.chat_id,
            instance_id=instance_id)
        # 有效身份：这个会话单独指定的 > 渠道默认的。
        bot_id = str((row or {}).get('bot_id') or '') or str(channel_bot_id or '')
        mapped = str((row or {}).get('session_id') or '')
        if mapped:
            current = await self.store.im_session_bot_id(mapped)
            if current is not None and current != bot_id:
                return await self._roll(
                    event, owner_user_id=owner_user_id, row=row, bot_id=bot_id,
                    instance_id=instance_id,
                    reason=f'{current or "通用助手"} → {bot_id or "通用助手"}')
            # 会话行可能被清理过；补建是幂等的（ON CONFLICT DO NOTHING），
            # 避免下一次 create_turn 把会话建成一条普通可见会话。
            await self.store.im_create_session(
                mapped, tenant_id=event.tenant, owner_user_id=owner_user_id,
                bot_id=bot_id, title=self._title(event))
            await self.store.im_session_touch(
                tenant_id=event.tenant, owner_user_id=owner_user_id,
                platform=event.platform, chat_id=event.chat_id,
                instance_id=instance_id)
            return row
        sid = session_id or session_id_for(
            platform=event.platform, tenant_id=event.tenant,
            owner_user_id=owner_user_id, chat_id=event.chat_id,
            instance_id=instance_id)
        await self.store.im_create_session(
            sid, tenant_id=event.tenant, owner_user_id=owner_user_id,
            bot_id=bot_id, title=self._title(event))
        return await self.store.im_session_bind(
            tenant_id=event.tenant, owner_user_id=owner_user_id,
            platform=event.platform, chat_id=event.chat_id,
            chat_type=event.chat_type, session_id=sid, generation=1,
            instance_id=instance_id)

    async def _roll(self, event: Inbound, *, owner_user_id: str, row: dict,
                    bot_id: str, instance_id: str, reason: str) -> dict:
        """换一代会话：新 `session_id`、`generation+1`，映射指过去。

        `generation` 是这一代在 `session_id_for` 里的种子，所以每次换身份都会
        得到一条全新会话，旧的那条不会被覆盖。身份覆盖（`row['bot_id']`）保持
        不动——换的是会话，不是"这个群归谁"。
        """
        generation = int(row.get('generation') or 1) + 1
        sid = session_id_for(
            platform=event.platform, tenant_id=event.tenant,
            owner_user_id=owner_user_id, chat_id=event.chat_id,
            generation=generation, instance_id=instance_id)
        await self.store.im_create_session(
            sid, tenant_id=event.tenant, owner_user_id=owner_user_id,
            bot_id=bot_id, title=self._title(event))
        logger.info('IM 回话身份变化，为 chat=%s 开第 %s 代会话（%s）',
                    event.chat_id[-8:], generation, reason)
        return await self.store.im_session_bind(
            tenant_id=event.tenant, owner_user_id=owner_user_id,
            platform=event.platform, chat_id=event.chat_id,
            chat_type=event.chat_type or str(row.get('chat_type') or 'dm'),
            session_id=sid, generation=generation,
            instance_id=instance_id)

    @staticmethod
    def _title(event: Inbound) -> str:
        who = user_label(event.user_id, event.user_name, platform=event.platform)
        return f'{event.platform} · {who}'[:60]

    def _compose(self, event: Inbound) -> str:
        """给 Agent 的消息正文：带上渠道与发言人，避免它把"谁说的"搞混。"""
        who = user_label(event.user_id, event.user_name, platform=event.platform)
        place = '群聊' if event.chat_type in ('group', 'channel') else '私聊'
        header = f'【{event.platform} · {place} · {who}】'
        body = (event.text or '').strip()
        if not body:
            body = '（对方发来了一条我暂时无法读取的内容）'
        return f'{header}\n{body}'

    async def _wait(self, turn_id: str,
                    on_delta: Callable[[str], Awaitable[None]] | None = None) -> str:
        deadline = time.monotonic() + self.timeout
        delay = self.poll_seconds
        last_draft = ''
        while time.monotonic() < deadline:
            turn = await self.store.turn(turn_id)
            if turn is None:
                return '这条对话的记录已被清理，请重新发一次。'
            status = str(turn.get('status') or '')
            if status == 'succeeded':
                answer = str(turn.get('answer') or '').strip()
                return answer or '（这次没有生成文字答复）'
            if status in TERMINAL:
                code = str(turn.get('error') or 'failed').splitlines()[0][:40]
                logger.info('IM turn %s 终态 %s：%s', turn_id, status, code)
                return f'抱歉，这次没能完成（{code or status}）。稍后可以再试一次。'
            if on_delta is not None:
                draft = str(turn.get('draft_answer') or '')
                if draft and draft != last_draft:
                    last_draft = draft
                    try:
                        await on_delta(draft)
                    except Exception:               # noqa: BLE001
                        logger.debug('IM 草稿回调失败', exc_info=True)
            await asyncio.sleep(delay)
            delay = min(2.0, delay * 1.2)
        return ('这件事还在处理，已经超过了我能等待的时间。'
                '结果仍然会保留在任务工作台里，稍后可以回去看。')
