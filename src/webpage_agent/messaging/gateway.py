"""消息网关：渠道生命周期、入站队列消费、配对、出站重试。

运行位置：**Worker 进程**。API 进程只写配置、读运行态、接收回调并落
`im_inbox`；真正执行 Agent 与会话的是 Worker。这样"关掉浏览器也照常回复"，
也避免 API 被长时间回合拖住。

单实例：Worker 可能多副本，但同一条 iLink 长轮询只能有一个消费者。网关启动时
抢一把 PostgreSQL advisory lock（`MESSAGING_LEADER_LOCK_ID`），抢不到就什么都不
做——控制面会显示 `gateway_stopped`，而不是两个进程互相抢消息。

状态机（全部经 `im_runtime`，API 只读）：

    start() → connected
    start 失败 → retrying（可重试，退避再起）/ fatal（配置错、凭据失效）
    运行中断线 → retrying → 成功 connected / 连续失败 fatal
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field

import httpx

from webpage_agent.core.config import settings
from webpage_agent.messaging import router as router_module
from webpage_agent.messaging.base import (ChannelConfig, Transport,
                                          TransportError)
from webpage_agent.messaging.bridge import ImBridge
from webpage_agent.messaging.models import (Inbound, Outbound, inbound_from_payload,
                                            inbound_to_payload)
from webpage_agent.messaging.normalize import (chunk_text, command_arg,
                                               command_of, user_label)
from webpage_agent.messaging.registry import (ChannelManifest, diagnose,
                                              get_manifest, secrets_for)
from webpage_agent.messaging.security import chat_id_hash

logger = logging.getLogger(__name__)

LEADER_LOCK_ID = 719284632
MAX_RESTART_BACKOFF = 120.0


@dataclass
class ActiveChannel:
    row: dict
    manifest: ChannelManifest
    config: ChannelConfig
    transport: Transport
    fingerprint: str
    state: str = 'connecting'
    detail: str = ''
    error_code: str = ''
    last_started: float = field(default_factory=time.time)


def build_channel_config(row: dict, manifest: ChannelManifest,
                         login: dict | None = None,
                         instance_id: str | None = None) -> ChannelConfig:
    """把一行 `im_channels` + 环境变量 + 登录态拼成 Transport 配置。

    `instance_id` 默认取行上的值；显式传入用于「这一行还没建出来」的场景
    （控制面预览未落库的实例）。按行取值时，未落库的实例会读成基础键的密钥，
    指纹也就跟着算错，界面会永远停在「待生效」。
    """
    login = login or {}
    instance = (str(row.get('instance_id') or '')
                if instance_id is None else str(instance_id))
    # 实例的密钥在环境变量里带后缀；非默认实例不回落到基础键。
    secrets = secrets_for(manifest, login, instance)
    options = dict(row.get('config') or {})
    if manifest.id == 'weixin':
        # 登录态里的 base_url 是 iLink 在扫码确认时下发的，优先级最高。
        if login.get('base_url'):
            options.setdefault('base_url', login['base_url'])
    return ChannelConfig(
        channel_id=str(row.get('channel_id') or ''),
        tenant_id=str(row.get('tenant_id') or 'default'),
        owner_user_id=str(row.get('owner_user_id') or 'default'),
        bot_id=str(row.get('bot_id') or ''),
        instance_id=instance,
        options=options,
        secrets=secrets,
        dm_policy=str(row.get('dm_policy') or manifest.default_dm_policy),
        group_policy=str(row.get('group_policy')
                         or manifest.default_group_policy),
        allowed_users=tuple(str(item) for item in (row.get('allowed_users') or [])),
        public_base_url=settings.messaging_public_base_url,
    )


class MessagingGateway:
    def __init__(self, hub, *, worker_id: str,
                 http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.hub = hub
        self.store = hub.store
        self.worker_id = worker_id
        self._http_transport = http_transport
        self._active: dict[str, ActiveChannel] = {}
        self._failures: dict[str, int] = {}
        self._next_restart: dict[str, float] = {}
        self._inflight: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._leader = None
        self._started = False
        self._bridge = ImBridge(self.store)
        self._ticks = 0

    # ---------- 生命周期 ----------

    async def start(self) -> bool:
        if self._started:
            return True
        if not settings.messaging_enabled:
            logger.info('消息平台未启用（MESSAGING_ENABLED=false）')
            return False
        if self._http_transport is None:
            self._leader = await self.store.im_take_leader_lock(LEADER_LOCK_ID)
            if self._leader is None:
                logger.info('另一个 Worker 持有消息网关锁，本进程不启动渠道')
                return False
        self._started = True
        await self._beat()
        await self.sync()
        logger.info('消息网关已启动：%s 个渠道', len(self._active))
        return True

    async def stop(self) -> None:
        self._started = False
        for task in list(self._inflight):
            task.cancel()
        for task in list(self._inflight):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._inflight.clear()
        for key in list(self._active):
            await self._stop_channel(key, clear_runtime=False)
        if self._leader is not None:
            await self.store.im_release_leader_lock(self._leader, LEADER_LOCK_ID)
            self._leader = None

    async def tick(self) -> dict:
        """一拍：心跳 → 同步配置 → 领取入站 → 泵出站。"""
        if not self._started:
            return {}
        self._ticks += 1
        await self._beat()
        try:
            await self.sync()
        except Exception:                          # noqa: BLE001
            logger.exception('同步消息渠道失败（不影响已运行的渠道）')

        claimed = 0
        try:
            claimed = await self._claim_inbox()
        except Exception:                          # noqa: BLE001
            logger.exception('领取 IM 入站消息失败')

        sent = 0
        for active in list(self._active.values()):
            try:
                sent += await self._drain_outbox(active)
            except Exception:                      # noqa: BLE001
                logger.exception('IM 出站队列处理失败 channel=%s',
                                 active.config.channel_id)

        if self._ticks % 200 == 0:
            try:
                await self.store.im_event_cleanup(keep_seconds=7 * 86400)
            except Exception:                      # noqa: BLE001
                logger.debug('清理 IM 审计事件失败', exc_info=True)
        return {'inbound': claimed, 'outbound': sent, 'channels': len(self._active)}

    # ---------- 配置同步 ----------

    async def sync(self) -> None:
        rows = await self.store.im_enabled_channels()
        desired: dict[str, dict] = {}
        for row in rows:
            # 键里必须带实例：两个机器人各挂一个飞书应用时，它们同租户、同 owner、
            # 同 channel_id，只有 instance_id 不同。少这一维就会互相顶掉。
            key = (f'{row["tenant_id"]}:{row["owner_user_id"]}:'
                   f'{row["channel_id"]}:{row.get("instance_id") or ""}')
            desired[key] = row

        for key in list(self._active):
            if key not in desired:
                await self._stop_channel(key, clear_runtime=True)
        for key in list(self._failures):
            if key not in desired:
                self._failures.pop(key, None)
                self._next_restart.pop(key, None)

        for key, row in desired.items():
            manifest = get_manifest(str(row.get('channel_id') or ''))
            if manifest is None:
                continue
            login = {}
            if manifest.id == 'weixin':
                login = await self.store.im_login_get(
                    manifest.id, tenant_id=str(row['tenant_id']),
                    owner_user_id=str(row['owner_user_id']))
            configured, _missing, _callback = diagnose(
                manifest, login_state=login,
                public_base_url=settings.messaging_public_base_url,
                instance_id=str(row.get('instance_id') or ''))
            if not configured:
                if key in self._active:
                    await self._stop_channel(key, clear_runtime=False)
                continue
            config = build_channel_config(row, manifest, login)
            fingerprint = _fingerprint(config)
            active = self._active.get(key)
            if active and active.fingerprint == fingerprint:
                if active.transport.alive():
                    # 连续运行满 60 秒才认为"这次真的好了"，把失败计数清零。
                    # 否则"起得来但一连上就 fatal"的渠道会每拍重启一次、永不熔断。
                    if time.monotonic() - active.last_started > 60:
                        self._failures[key] = 0
                    continue
                self._failures[key] = self._failures.get(key, 0) + 1
                await self._stop_channel(key, clear_runtime=False)
                if self._failures[key] >= 5:
                    await self._write_runtime(
                        config, 'fatal', '渠道连续断开，已停止自动重连',
                        'restart_storm', fingerprint)
                    self._next_restart[key] = time.monotonic() + 86400
                    logger.error('渠道 %s 连续断开，停止自动重连', key)
                else:
                    delay = _backoff(self._failures[key])
                    self._next_restart[key] = time.monotonic() + delay
                    logger.warning('渠道 %s 已断开，%.0fs 后重连', key, delay)
                continue
            if active:
                await self._stop_channel(key, clear_runtime=False)
            if time.monotonic() < self._next_restart.get(key, 0):
                continue
            await self._start_channel(key, row, manifest, config, fingerprint)

    async def _start_channel(self, key: str, row: dict,
                             manifest: ChannelManifest, config: ChannelConfig,
                             fingerprint: str) -> None:
        transport_cls = manifest.transport() if manifest.transport else None
        if transport_cls is None:
            return
        def on_status(state: str, detail: str, code: str) -> None:
            # Transport 的状态回调是同步的（可能从线程里调）；写库必须交给事件循环。
            # 带上本次配置指纹：否则启动早期写入的 connected 会把空指纹落库，
            # 控制面就会永远显示"待生效"。
            try:
                asyncio.create_task(
                    self._on_status(config, state, detail, code, fingerprint))
            except RuntimeError:
                pass

        transport = transport_cls(
            config,
            on_inbound=lambda event: self._on_inbound(config, event),
            on_status=on_status,
            http_transport=self._http_transport,
        )
        try:
            await asyncio.wait_for(transport.start(), timeout=30)
        except TransportError as exc:
            await transport.stop()
            self._failures[key] = self._failures.get(key, 0) + 1
            state = 'retrying' if exc.retryable else 'fatal'
            await self._write_runtime(config, state, str(exc), exc.code,
                                      fingerprint)
            if exc.retryable:
                delay = _backoff(self._failures[key])
                self._next_restart[key] = time.monotonic() + delay
                logger.warning('渠道 %s 启动失败（%.0fs 后重试）：%s',
                               key, delay, exc)
            else:
                self._next_restart[key] = time.monotonic() + 86400
                logger.warning('渠道 %s 启动失败（不重试）：%s', key, exc)
            return
        except Exception as exc:                    # noqa: BLE001
            await transport.stop()
            self._failures[key] = self._failures.get(key, 0) + 1
            await self._write_runtime(config, 'startup_failed',
                                      f'{type(exc).__name__}: {exc}'[:300],
                                      'startup_failed', fingerprint)
            self._next_restart[key] = time.monotonic() + _backoff(
                self._failures[key])
            logger.exception('渠道 %s 启动异常', key)
            return
        self._next_restart.pop(key, None)
        self._active[key] = ActiveChannel(
            row=row, manifest=manifest, config=config, transport=transport,
            fingerprint=fingerprint)
        await self._write_runtime(config, 'connected',
                                  '连接已建立', '', fingerprint)
        logger.info('渠道已启动：%s（%s）', key, manifest.name)

    async def _stop_channel(self, key: str, *, clear_runtime: bool) -> None:
        active = self._active.pop(key, None)
        if active is None:
            return
        try:
            await asyncio.wait_for(active.transport.stop(), timeout=10)
        except Exception:                          # noqa: BLE001
            logger.debug('关闭渠道 %s 失败', key, exc_info=True)
        if clear_runtime:
            await self.store.im_clear_runtime(
                active.config.channel_id, tenant_id=active.config.tenant_id,
                owner_user_id=active.config.owner_user_id,
                instance_id=active.config.instance_id)

    # ---------- 入站 ----------

    async def _on_inbound(self, config: ChannelConfig, event: Inbound) -> None:
        try:
            await self.store.im_inbox_add(
                platform=event.platform, tenant_id=config.tenant_id,
                owner_user_id=config.owner_user_id, instance_id=config.instance_id,
                message_id=event.message_id, payload=inbound_to_payload(event))
        except Exception:                          # noqa: BLE001
            logger.exception('IM 入站落库失败 message=%s', event.message_id)

    async def _claim_inbox(self) -> int:
        quota = settings.messaging_concurrency - len(self._inflight)
        if quota <= 0:
            return 0
        rows = await self.store.im_inbox_claim(
            min(quota, settings.messaging_inbox_batch))
        for row in rows:
            task = asyncio.create_task(self._process_inbox(row),
                                       name=f'im:inbox:{row["inbox_id"]}')
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        return len(rows)

    async def _process_inbox(self, row: dict) -> None:
        inbox_id = str(row.get('inbox_id') or '')
        try:
            event = inbound_from_payload(row.get('payload') or {})
            # 入站行记了它属于哪个实例：两个飞书应用都叫 feishu，只有实例分得开。
            instance = str(row.get('instance_id') or '')
            key = (f'{row["tenant_id"]}:{row["owner_user_id"]}:'
                   f'{event.platform}:{instance}')
            active = self._active.get(key)
            if active is None:
                channel = await self.store.im_channel(
                    event.platform, tenant_id=str(row['tenant_id']),
                    owner_user_id=str(row['owner_user_id']), instance_id=instance)
                if channel is not None and channel.get('enabled'):
                    # 渠道启用了但还没就绪（启动中/凭据暂不可用）：先排队，
                    # 不要丢用户的发言。24 小时后仍没人处理才算失败。
                    if await self.store.im_inbox_requeue(inbox_id):
                        return
                await self.store.im_inbox_finish(inbox_id, ok=True)
                return
            seen = await self.store.im_mark_seen(
                event.platform, event.tenant, event.message_id,
                ttl_seconds=settings.messaging_dedupe_ttl_seconds)
            if not seen:
                await self.store.im_event_add(
                    tenant_id=event.tenant, owner_user_id=active.config.owner_user_id,
                    channel_id=event.platform,
                    instance_id=active.config.instance_id,
                    direction='in', message_id=event.message_id,
                    chat_id_hash=chat_id_hash(event.chat_id),
                    status='duplicate', error_code='dedupe')
                await self.store.im_inbox_finish(inbox_id, ok=True)
                return
            await self._handle_event(active, event)
            await self.store.im_inbox_finish(inbox_id, ok=True)
        except Exception as exc:                    # noqa: BLE001
            attempts = int(row.get('attempts') or 1)
            retry = attempts < settings.messaging_inbox_max_attempts
            delay = _backoff(attempts)
            logger.exception('处理 IM 入站失败 inbox=%s（第 %s 次）',
                             inbox_id, attempts)
            await self.store.im_inbox_finish(
                inbox_id, ok=False, retry=retry,
                error_code=type(exc).__name__, delay_seconds=delay)
            if not retry:
                await self._notify_failure(row, exc)

    async def _handle_event(self, active: ActiveChannel, event: Inbound) -> None:
        paired = await self.store.im_pairing_approved_users(
            tenant_id=event.tenant, owner_user_id=active.config.owner_user_id,
            platform=event.platform)
        decision = router_module.authorize(
            event, dm_policy=active.config.dm_policy,
            group_policy=active.config.group_policy,
            allowed_users=active.config.allowed_users,
            paired_users=paired)
        if decision.action == router_module.IGNORE:
            await self._audit(active, event, 'ignored', decision.reason)
            return
        if decision.action == router_module.PAIR:
            await self._issue_pairing(active, event)
            return
        command = command_of(event.text)
        action = router_module.command_action(command)
        if action == 'help':
            await self._reply(active, event, router_module.HELP_TEXT,
                              sent_key=f'cmd-help:{event.message_id}')
            return
        if action == 'new':
            await self._new_session(active, event)
            return
        if action == 'who':
            await self._who(active, event)
            return
        if action == 'bind':
            await self._bind(active, event, command_arg(event.text))
            return
        if action == 'unbind':
            await self._unbind(active, event)
            return
        if action == 'stop':
            await self._reply(
                active, event,
                '收到。当前这条回复无法中断，会继续完成；下一条消息可以重新提问。',
                sent_key=f'cmd-stop:{event.message_id}')
            return

        key = router_module.session_key(event)
        lock = self._lock_for(key)
        async with lock:
            result = await self._bridge.run(
                event, owner_user_id=active.config.owner_user_id,
                channel_bot_id=active.config.bot_id,
                instance_id=active.config.instance_id,
                on_start=lambda: self._typing(active, event))
        await self._reply(active, event, result.get('text') or '（没有生成回复）',
                          sent_key=f'reply:{event.platform}:{event.message_id}')
        await self._audit(active, event, 'answered')

    async def _new_session(self, active: ActiveChannel, event: Inbound) -> None:
        from webpage_agent.messaging.bridge import session_id_for
        instance = active.config.instance_id
        row = await self.store.im_session(
            tenant_id=event.tenant, owner_user_id=active.config.owner_user_id,
            platform=event.platform, chat_id=event.chat_id,
            instance_id=instance)
        # `/new` 换的是对话，不是"这个群归谁"：身份覆盖照旧，新会话用同一个身份。
        bot_id = str((row or {}).get('bot_id') or '') or active.config.bot_id
        generation = int((row or {}).get('generation') or 1) + 1
        sid = session_id_for(platform=event.platform, tenant_id=event.tenant,
                             owner_user_id=active.config.owner_user_id,
                             chat_id=event.chat_id, generation=generation,
                             instance_id=instance)
        await self.store.im_create_session(
            sid, tenant_id=event.tenant,
            owner_user_id=active.config.owner_user_id,
            bot_id=bot_id,
            title=f'{event.platform} · {event.chat_id[:12]}')
        await self.store.im_session_bind(
            tenant_id=event.tenant, owner_user_id=active.config.owner_user_id,
            platform=event.platform, chat_id=event.chat_id,
            chat_type=event.chat_type, session_id=sid, generation=generation,
            instance_id=instance)
        await self._reply(active, event, '已开始新会话。之前的对话记录仍然保留在本机。',
                          sent_key=f'cmd-new:{event.message_id}')

    # ---------- 会话级身份 ----------

    async def _session_row(self, active: ActiveChannel, event: Inbound) -> dict | None:
        return await self.store.im_session(
            tenant_id=event.tenant, owner_user_id=active.config.owner_user_id,
            platform=event.platform, chat_id=event.chat_id,
            instance_id=active.config.instance_id)

    async def _bot_label(self, bot_id: str, owner_user_id: str,
                         tenant_id: str) -> str:
        if not bot_id:
            return '通用助手（未绑定机器人）'
        bot = await self.store.bot_row(bot_id, tenant_id=tenant_id,
                                       owner_user_id=owner_user_id)
        if not bot:
            return '一个已被删除的机器人'
        name = str(bot.get('name') or bot_id)
        handle = str(bot.get('handle') or '').strip()
        return f'{name}（@{handle}）' if handle else name

    async def _who(self, active: ActiveChannel, event: Inbound) -> None:
        row = await self._session_row(active, event)
        override = str((row or {}).get('bot_id') or '')
        bot_id = override or active.config.bot_id
        label = await self._bot_label(bot_id, active.config.owner_user_id,
                                      event.tenant)
        source = '这个会话单独指定' if override else '跟随渠道默认'
        await self._reply(
            active, event,
            f'现在由「{label}」回答（{source}）。\n'
            '发 /bind 看可选的机器人，/unbind 恢复跟随渠道。',
            sent_key=f'cmd-who:{event.message_id}')

    async def _bind(self, active: ActiveChannel, event: Inbound, arg: str) -> None:
        tenant, owner = event.tenant, active.config.owner_user_id
        row = await self._session_row(active, event)
        override = str((row or {}).get('bot_id') or '')
        current = override or active.config.bot_id
        if not arg:
            await self._reply(active, event,
                              await self._bind_menu(tenant, owner, current),
                              sent_key=f'cmd-bind:{event.message_id}')
            return
        bot = await self._find_bot(tenant, owner, arg)
        if bot is None:
            await self._reply(
                active, event,
                f'没找到机器人「{arg}」。\n\n' + await self._bind_menu(tenant, owner, current),
                sent_key=f'cmd-bind-miss:{event.message_id}')
            return
        bot_id = str(bot.get('bot_id') or '')
        if bot_id == current:
            await self._reply(
                active, event,
                f'这个会话已经是「{await self._bot_label(bot_id, owner, tenant)}」在回答。',
                sent_key=f'cmd-bind-same:{event.message_id}')
            return
        if row is None:
            # 还没聊过：直接建一条映射并带上覆盖，避免下次消息落到一条
            # 指向不存在 session 的空映射上。
            from webpage_agent.messaging.bridge import session_id_for
            instance = active.config.instance_id
            sid = session_id_for(platform=event.platform, tenant_id=tenant,
                                 owner_user_id=owner, chat_id=event.chat_id,
                                 generation=1, instance_id=instance)
            await self.store.im_create_session(
                sid, tenant_id=tenant, owner_user_id=owner, bot_id=bot_id,
                title=f'{event.platform} · {user_label(event.user_id, event.user_name, platform=event.platform)}'[:60])
            await self.store.im_session_bind(
                tenant_id=tenant, owner_user_id=owner, platform=event.platform,
                chat_id=event.chat_id, chat_type=event.chat_type,
                session_id=sid, generation=1, bot_id=bot_id,
                instance_id=instance)
        else:
            await self.store.im_session_set_bot(
                tenant_id=tenant, owner_user_id=owner, platform=event.platform,
                chat_id=event.chat_id, bot_id=bot_id,
                instance_id=active.config.instance_id)
        await self._reply(
            active, event,
            f'好，这个会话从这里开始由「{await self._bot_label(bot_id, owner, tenant)}」回答。\n'
            '下一条消息会开一段新对话——旧对话的历史仍然保留在本机，只是不再接着聊。',
            sent_key=f'cmd-bind-ok:{event.message_id}')
        await self._audit(active, event, 'bound', bot_id)

    async def _unbind(self, active: ActiveChannel, event: Inbound) -> None:
        tenant, owner = event.tenant, active.config.owner_user_id
        row = await self._session_row(active, event)
        if not str((row or {}).get('bot_id') or ''):
            await self._reply(
                active, event,
                f'这个会话本来就在跟随渠道默认（当前是'
                f'「{await self._bot_label(active.config.bot_id, owner, tenant)}」）。',
                sent_key=f'cmd-unbind-noop:{event.message_id}')
            return
        await self.store.im_session_set_bot(
            tenant_id=tenant, owner_user_id=owner, platform=event.platform,
            chat_id=event.chat_id, bot_id='',
            instance_id=active.config.instance_id)
        await self._reply(
            active, event,
            f'已恢复跟随渠道默认（「{await self._bot_label(active.config.bot_id, owner, tenant)}」）。',
            sent_key=f'cmd-unbind:{event.message_id}')
        await self._audit(active, event, 'unbound')

    async def _find_bot(self, tenant: str, owner: str, token: str) -> dict | None:
        """按 id / handle / 名字找机器人。

        名字必须能查：`/bind` 的菜单列的是名字，菜单怎么说就得怎么填。查不到
        再回落到花名册里逐个比（一个人只有几十个机器人，不值得再建一张索引）。
        """
        text = str(token or '').strip().lstrip('@')
        if not text:
            return None
        bot = await self.store.bot_row(text, tenant_id=tenant, owner_user_id=owner)
        if bot:
            return bot
        bot = await self.store.bot_row_by_handle(
            text, tenant_id=tenant, owner_user_id=owner)
        if bot:
            return bot
        wanted = text.casefold()
        for item in await self.store.list_bot_rows(
                tenant_id=tenant, owner_user_id=owner):
            name = str(item.get('name') or '').strip()
            if name and name.casefold() == wanted:
                return item
        return None

    async def _bind_menu(self, tenant: str, owner: str, current: str) -> str:
        bots = await self.store.list_bot_rows(
            tenant_id=tenant, owner_user_id=owner)
        label = await self._bot_label(current, owner, tenant)
        head = f'这个会话现在由「{label}」回答。'
        if not bots:
            return head + '\n还没有创建任何机器人，先到「机器人」里建一个。'
        lines = []
        for bot in bots[:12]:
            name = str(bot.get('name') or bot.get('bot_id') or '')
            handle = str(bot.get('handle') or '').strip()
            mark = '（当前）' if str(bot.get('bot_id') or '') == current else ''
            lines.append(f'· {name}{f" @{handle}" if handle else ""}{mark}')
        more = '' if len(bots) <= 12 else f'\n…还有 {len(bots) - 12} 个'
        return (f'{head}\n\n可选的机器人：\n' + '\n'.join(lines) + more
                + '\n\n`/bind 名字` 指定，`/unbind` 恢复跟随渠道。')

    async def _issue_pairing(self, active: ActiveChannel,
                             event: Inbound) -> None:
        code = f'{secrets.randbelow(10 ** 8):08d}'
        code_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()
        ok, reason = await self.store.im_pairing_issue(
            tenant_id=event.tenant,
            owner_user_id=active.config.owner_user_id,
            platform=event.platform, user_id=event.user_id,
            chat_id=event.chat_id, code_hash=code_hash,
            instance_id=active.config.instance_id)
        if not ok:
            logger.info('配对请求被限流：%s user=%s', reason, event.user_id[:8])
            return
        text = (f'这是一条配对请求。请让管理员在「消息平台 → 配对请求」里批准。\n'
                f'配对码：{code}\n（1 小时内有效，请勿转发给他人）')
        await self._reply(active, event, text,
                          sent_key=f'pair:{event.platform}:{event.user_id}:{code}')
        await self._audit(active, event, 'pair_requested')

    # ---------- 出站 ----------

    async def _reply(self, active: ActiveChannel, event: Inbound, text: str,
                     *, sent_key: str) -> None:
        await self._send(active, event.chat_id, text, sent_key=sent_key)

    async def _send(self, active: ActiveChannel, chat_id: str, text: str,
                    *, sent_key: str) -> None:
        content = str(text or '').strip()
        if not content:
            return
        caps = active.transport.capabilities()
        pieces = chunk_text(content, caps.max_text_chars)
        added = False
        for index, piece in enumerate(pieces, start=1):
            if await self.store.im_outbox_add(
                    tenant_id=active.config.tenant_id,
                    owner_user_id=active.config.owner_user_id,
                    channel_id=active.config.channel_id,
                    instance_id=active.config.instance_id,
                    platform_chat_id=chat_id,
                    sent_key=f'{sent_key}:{index}', text=piece):
                added = True
        if added:
            await self._drain_outbox(active)

    async def _drain_outbox(self, active: ActiveChannel) -> int:
        if not await self._channel_due(active):
            return 0
        rows = await self.store.im_outbox_claim(
            active.config.channel_id, tenant_id=active.config.tenant_id,
            owner_user_id=active.config.owner_user_id, limit=5,
            instance_id=active.config.instance_id)
        sent = 0
        for row in rows:
            try:
                result = await active.transport.send(
                    str(row['platform_chat_id']), Outbound(text=row['text']))
            except Exception:                       # noqa: BLE001
                logger.exception('IM 发送异常 channel=%s', active.config.channel_id)
                result = None
            attempts = int(row.get('attempts') or 1)
            if result is not None and result.ok:
                sent += 1
                await self.store.im_outbox_finish(
                    row['outbox_id'], ok=True)
                await self.store.im_event_add(
                    tenant_id=active.config.tenant_id,
                    owner_user_id=active.config.owner_user_id,
                    channel_id=active.config.channel_id,
                    instance_id=active.config.instance_id, direction='out',
                    chat_id_hash=chat_id_hash(str(row['platform_chat_id'])),
                    message_id=str(result.message_id or ''), status='sent')
                continue
            error = (result.error_code if result is not None
                     else 'exception') or 'send_failed'
            # 发送函数抛异常通常是网络问题：按可重试处理，别把可恢复的抖动
            # 直接记成终态失败。
            retryable = True if result is None else bool(result.retryable)
            if retryable and attempts < settings.messaging_outbox_max_attempts:
                await self.store.im_outbox_finish(
                    row['outbox_id'], ok=False, retry=True, error=error,
                    delay_seconds=_backoff(attempts))
            else:
                await self.store.im_outbox_finish(
                    row['outbox_id'], ok=False, error=error)
                await self.store.im_event_add(
                    tenant_id=active.config.tenant_id,
                    owner_user_id=active.config.owner_user_id,
                    channel_id=active.config.channel_id,
                    instance_id=active.config.instance_id, direction='out',
                    chat_id_hash=chat_id_hash(str(row['platform_chat_id'])),
                    status='failed', error_code=error)
                logger.warning('IM 发送失败 channel=%s code=%s',
                               active.config.channel_id, error)
        return sent

    async def _typing(self, active: ActiveChannel, event: Inbound) -> None:
        if not active.transport.capabilities().typing:
            return
        try:
            await asyncio.wait_for(active.transport.typing(event.chat_id),
                                   timeout=5)
        except Exception:                           # noqa: BLE001
            logger.debug('typing 失败', exc_info=True)

    # ---------- 状态与审计 ----------

    async def _on_status(self, config: ChannelConfig, state: str,
                         detail: str, code: str, fingerprint: str) -> None:
        active = self._active.get(config.key)
        if active is not None:
            active.state, active.detail, active.error_code = state, detail, code
        await self._write_runtime(config, state, detail, code, fingerprint)

    async def _write_runtime(self, config: ChannelConfig, state: str,
                             detail: str, code: str, fingerprint: str) -> None:
        try:
            await self.store.im_set_runtime(
                config.channel_id, tenant_id=config.tenant_id,
                owner_user_id=config.owner_user_id, state=state,
                detail=str(detail or '')[:300], error_code=str(code or ''),
                fingerprint=fingerprint, worker_id=self.worker_id,
                instance_id=config.instance_id)
        except Exception:                           # noqa: BLE001
            logger.debug('写 IM 运行态失败', exc_info=True)

    async def _beat(self) -> None:
        try:
            await self.store.beat(
                'messaging', self.worker_id, host=os.uname().nodename,
                pid=os.getpid(),
                meta=json.dumps({'channels': len(self._active)},
                                ensure_ascii=False))
        except Exception:                           # noqa: BLE001
            logger.debug('消息网关心跳失败', exc_info=True)

    async def _audit(self, active: ActiveChannel, event: Inbound,
                     status: str, error_code: str = '') -> None:
        try:
            await self.store.im_event_add(
                tenant_id=active.config.tenant_id,
                owner_user_id=active.config.owner_user_id,
                channel_id=active.config.channel_id,
                instance_id=active.config.instance_id, direction='in',
                chat_id_hash=chat_id_hash(event.chat_id),
                message_id=event.message_id, status=status,
                error_code=error_code)
        except Exception:                           # noqa: BLE001
            logger.debug('写 IM 审计事件失败', exc_info=True)

    async def _notify_failure(self, row: dict, exc: Exception) -> None:
        """入站彻底失败时告诉用户一声；不再重试，避免死循环。"""
        # 键必须带实例：多实例下同一 (tenant, owner, platform) 有多条连接，
        # 少了这一维就永远查不到（等于这条失败通知从来没发出去过）。
        key = (f'{row["tenant_id"]}:{row["owner_user_id"]}:'
               f'{row["platform"]}:{row.get("instance_id") or ""}')
        active = self._active.get(key)
        if active is None:
            return
        event = inbound_from_payload(row.get('payload') or {})
        try:
            await self._reply(
                active, event,
                f'抱歉，这条消息处理失败了（{type(exc).__name__}）。请稍后重发。',
                sent_key=f'failed:{event.message_id}')
        except Exception:                           # noqa: BLE001
            pass

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            if len(self._locks) > 512:
                for name, item in list(self._locks.items()):
                    if not item.locked():
                        self._locks.pop(name, None)
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _channel_due(self, active: ActiveChannel) -> bool:
        try:
            return bool(await self.store.im_outbox_pending(
                tenant_id=active.config.tenant_id,
                owner_user_id=active.config.owner_user_id,
                channel_id=active.config.channel_id,
                instance_id=active.config.instance_id))
        except Exception:                           # noqa: BLE001
            return True


def _fingerprint(config: ChannelConfig) -> str:
    from webpage_agent.messaging.state import config_fingerprint
    return config_fingerprint(
        enabled=True, bot_id=config.bot_id, dm_policy=config.dm_policy,
        group_policy=config.group_policy, allowed_users=config.allowed_users,
        config=config.options, secrets=config.secrets,
        public_base_url=config.public_base_url)


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动，上限 120s。抖动防止多副本同时重连。"""
    base = min(MAX_RESTART_BACKOFF, 2.0 ** max(1, min(7, int(attempt))))
    return base + (int(uuid.uuid4().hex[:4], 16) / 65535.0) * 2.0


