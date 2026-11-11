"""定时任务的结果投递：入队（事务/幂等）与出队发送（重试/去重）。

为什么复用 `background_jobs` 而不是再建一张 outbox 表：它已经具备这套东西的全部要素
——`(kind, dedupe_key)` 唯一索引（同一次运行同一渠道只发一条）、`FOR UPDATE SKIP LOCKED`
的租约、带 `lease_epoch` 的 fencing、以及"失败退回队列并指数退避"的 `finish_job`。
再建一张表只会多一份没被验证过的重试逻辑。

投递**与执行解耦**：执行记录先落库，发送失败只影响 `delivery_status`，
不会把一次成功的运行标成失败。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from webpage_agent.core.config import settings
from webpage_agent.integrations import webhooks

logger = logging.getLogger(__name__)

KIND = 'schedule_delivery'
# 默认通知哪些结果。失败必发；成功也发（不然用户不知道它跑了），
# 需要人工确认的那类单独一档，方便以后按需关掉。
DEFAULT_EVENTS = ('success', 'failure', 'needs_action')
STATUS_TEXT = {
    'succeeded': '✅ 已完成',
    'failed': '❌ 执行失败',
    'paused': '⏳ 需要你确认后继续',
}


def _local_time(stamp: float | None) -> str:
    if not stamp:
        return ''
    return datetime.fromtimestamp(float(stamp)).strftime('%Y-%m-%d %H:%M')


async def enqueue(store, schedule: dict, occurrence: dict, *, notify: str,
                  status: str, code: str = '', message: str = '',
                  summary: str = '') -> bool:
    """把一条通知放进 outbox。返回是否真的入队（未配置渠道/未命中事件时不入）。"""
    config = (schedule or {}).get('delivery') or {}
    channel = str(config.get('channel') or '').strip()
    if channel not in webhooks.CHANNELS:
        return False
    events = tuple(config.get('events') or DEFAULT_EVENTS)
    if notify not in events:
        return False
    url = str(config.get('webhook_url') or settings.scheduled_default_webhook or '')
    if not url:
        # 没配置地址也仍然保留运行记录，只是没有出站消息。
        return False
    occurrence_id = str(occurrence.get('occurrence_id') or '')
    inserted = await store.enqueue_job(
        uuid.uuid4().hex[:12], KIND, f'{occurrence_id}:{channel}',
        {'occurrence_id': occurrence_id,
         'schedule_id': str(schedule.get('schedule_id') or ''),
         'channel': channel, 'notify': notify, 'status': status,
         'code': code, 'message': message, 'summary': summary,
         'occurred_at': _local_time(occurrence.get('due_at'))})
    if inserted:
        await store.update_occurrence(occurrence_id, delivery_status='queued')
    return bool(inserted)


async def deliver_job(hub, job: dict) -> None:
    """Worker 消费一条投递作业。

    可重试的失败**故意抛出去**：`finish_job(retry=True)` 会按 attempts 退避重排；
    不可重试的（4xx、地址非法）就地记失败并正常返回，免得白退避五次。
    """
    store = hub.store
    payload = job.get('payload') or {}
    occurrence_id = str(payload.get('occurrence_id') or '')
    occurrence = await store.occurrence(occurrence_id)
    if occurrence is None:
        # 任务被删了：这条通知没有意义，视作完成。
        return
    if str(occurrence.get('delivery_status') or '') == 'sent':
        return
    schedule = await store.schedule(str(payload.get('schedule_id') or ''))
    if schedule is None:
        return
    config = schedule.get('delivery') or {}
    channel = str(payload.get('channel') or config.get('channel') or 'webhook')
    url = str(config.get('webhook_url') or settings.scheduled_default_webhook or '')
    attempts = int(occurrence.get('delivery_attempts') or 0) + 1
    status = str(payload.get('status') or occurrence.get('status') or '')
    code = str(payload.get('code') or occurrence.get('error_code') or '')
    summary = str(payload.get('summary') or occurrence.get('summary') or '')
    head = STATUS_TEXT.get(status, 'ℹ️ 执行状态更新')
    if code and status == 'succeeded' and code != 'succeeded':
        head = '⚠️ 已完成，但需要核实'
    text = f'{head}：{schedule.get("name") or "定时任务"}'
    body = webhooks.build_payload(
        channel, name=str(schedule.get('name') or '定时任务'),
        status_text=head, summary=summary,
        occurred_at=str(payload.get('occurred_at') or ''),
        run_id=str(occurrence.get('run_id') or ''))
    try:
        await webhooks.post_webhook(url, body, channel=channel)
    except webhooks.DeliveryError as exc:
        await store.update_occurrence(
            occurrence_id, delivery_status='failed', delivery_attempts=attempts,
            error_code=str(occurrence.get('error_code') or '') or exc.code,
            error_message=f'{exc.code}: {exc}')
        logger.warning('定时任务投递失败（第 %s 次，%s）：%s', attempts, exc.code, text)
        if exc.retryable:
            raise
        return
    await store.update_occurrence(
        occurrence_id, delivery_status='sent', delivery_attempts=attempts)
    logger.info('定时任务已投递 occurrence=%s channel=%s', occurrence_id, channel)
