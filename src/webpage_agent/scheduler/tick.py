"""定时任务的一次 tick：认领 → 判定 → 派发 → 收敛 → 投递入队。

挂在 Worker 的 `main_loop` 里（`scripts/run_worker.py`），与外部任务轮询同层。
真正的执行**不在这里**：派发走 `Hub.start()`，和用户手点一条任务是同一条路径，
所以租约、并发上限、会话隔离、计费绑定都自动一致。

三条不变量：

1. **同一 occurrence 只跑一次**：靠 `scheduled_task_runs(schedule_id, due_at)` 唯一索引，
   不靠进程内状态（多副本时进程内状态一律不可信）。
2. **推进 `next_run_at` 必须带租约门禁**：被接管的旧副本写不进去，
   否则会把新副本刚算好的下一次踩回去。
3. **派发成功不等于执行完成**：`hub.start()` 在本进程满额时会带着 `queued` 返回，
   由别的 Worker 领走。所以 occurrence 先记 `queued`，终态由 `collect()` 按 `runs` 行收敛。
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from types import SimpleNamespace

from webpage_agent.core import rrules
from webpage_agent.core.config import settings
from webpage_agent.core.outcome import (
    STOPPED_RUN_STATUSES, TERMINAL_RUN_STATUSES)
from webpage_agent.scheduler import decide as decide_module
from webpage_agent.scheduler import delivery as delivery_module

logger = logging.getLogger(__name__)

# 运行终态 → occurrence 终态。`partially_succeeded` / `needs_reconcile` 归到
# attention：它们不是失败，但"需要人看一眼"，通知文案必须区分开。
SUCCESS_RUNS = frozenset({'succeeded'})
ATTENTION_RUNS = frozenset({'partially_succeeded', 'needs_reconcile'})
# 派发了但 run 行迟迟没建出来（进程在两步之间崩了）的宽限。
ORPHAN_GRACE_SECONDS = 600
SUMMARY_LIMIT = 2000


def occurrence_id_for(schedule_id: str, due: float) -> str:
    """同一时刻的 occurrence 有稳定 id：手动触发与定时触发都按 (任务, 时刻) 归一。"""
    return f'{schedule_id}:{int(float(due) * 1000)}'


def _iso_to_ts(value: object) -> float | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def next_after(row: dict, after: float) -> tuple[float | None, str, str]:
    """按任务定义算下一次触发。返回 `(epoch | None, error_code, error_message)`。

    规则解析失败一律返回 `None`：一条写坏了的规则不该让 tick 每 30 秒抛一次，
    它应该"不再触发"，并在任务列表里带着错误码可见。
    """
    kind = str(row.get('schedule_type') or 'recurring')
    valid_from = _iso_to_ts(row.get('valid_from'))
    valid_until = _iso_to_ts(row.get('valid_until'))
    if kind == 'once':
        scheduled_at = _iso_to_ts(row.get('scheduled_at'))
        if scheduled_at is None:
            return None, 'invalid_once', '一次性任务缺少触发时间'
        nxt = rrules.once_run_at(scheduled_at, now=after,
                                 valid_from=valid_from, valid_until=valid_until)
        return nxt, '', ''
    try:
        rule = rrules.parse(str(row.get('rrule') or ''))
    except rrules.RuleError as exc:
        return None, 'invalid_rrule', str(exc)
    nxt = rrules.next_run_at(rule, after, valid_from=valid_from,
                             valid_until=valid_until)
    return nxt, '', ''


def _principal(row: dict):
    """无人值守执行用的主体：**全部来自任务行**，不猜、不兜底。

    `Hub.start` 只用 getattr 读这几个属性，所以不需要真正的认证对象。
    这里刻意不写 `or 'default'`：用默认身份跑别人的任务，等于把租户隔离作废。
    """
    return SimpleNamespace(
        tenant_id=str(row.get('tenant_id') or 'default'),
        user_id=str(row.get('owner_user_id') or 'default'),
        role=str(row.get('owner_role') or 'member'),
        platform_id=str(row.get('platform_id') or ''),
        subject=str(row.get('platform_subject') or ''),
    )


async def tick(hub, worker_id: str, *, now_ts: float | None = None) -> dict:
    """跑一轮。返回统计（供 Worker 日志与测试断言）。"""
    stats = {'claimed': 0, 'fired': 0, 'skipped': 0, 'deferred': 0,
             'collected': 0, 'delivered': 0}
    if not settings.scheduled_tasks_enabled:
        return stats
    store = hub.store
    now = time.time() if now_ts is None else float(now_ts)
    rows = await store.claim_due_schedules(
        worker_id, now_ts=now, limit=settings.scheduled_batch_limit,
        lease_seconds=settings.scheduled_claim_lease_seconds)
    stats['claimed'] = len(rows)
    for row in rows:
        try:
            action = await _handle(hub, row, now)
        except Exception:                      # noqa: BLE001
            # 单条任务出错不能带走整轮 tick（更不能带走 Worker 主循环）。
            logger.exception('定时任务 %s 处理失败', row.get('schedule_id'))
            action = 'error'
        stats[action] = stats.get(action, 0) + 1
    try:
        collected, delivered = await collect(hub, now)
        stats['collected'] += collected
        stats['delivered'] += delivered
    except Exception:                          # noqa: BLE001
        logger.exception('定时任务收敛失败')
    return stats


# ---------- 单条任务 ----------

async def _handle(hub, row: dict, now: float) -> str:
    store = hub.store
    schedule_id = row['schedule_id']
    due = float(row['next_run_at'])
    lease_owner = str(row.get('lease_owner') or '')
    lease_epoch = int(row.get('lease_epoch') or 0)

    # 1) 这个 occurrence 已经处理过（上一轮推进成功、或重复认领）→ 只补推进。
    existing = await store.occurrence_for_due(schedule_id, due)
    if existing is not None and existing.get('status') in (
            'succeeded', 'failed', 'skipped', 'paused'):
        nxt, code, message = next_after(row, now)
        await store.finish_schedule_tick(
            schedule_id, lease_owner=lease_owner, lease_epoch=lease_epoch,
            next_run_at=nxt, last_status=str(existing.get('status') or ''),
            last_error_code=code, last_error_message=message)
        return 'skipped'

    # 2) 判定
    busy = await store.open_occurrence(schedule_id) is not None
    dispatch_at = due + decide_module.jitter_delay(
        schedule_id, due, settings.scheduled_jitter_seconds)
    decision = decide_module.decide(
        due=due, now=now,
        missed_window=settings.scheduled_missed_window_seconds,
        busy=busy, at_capacity=hub.at_capacity(),
        deferred_count=int(row.get('deferred_count') or 0),
        max_deferrals=settings.scheduled_max_deferrals,
        defer_backoff=settings.scheduled_defer_backoff_seconds,
        dispatch_at=dispatch_at,
        next_run_at=next_after(row, now)[0])
    if decision.action == decide_module.SKIP:
        await store.record_occurrence(
            occurrence_id=occurrence_id_for(schedule_id, due), schedule_id=schedule_id,
            tenant_id=row['tenant_id'], owner_user_id=row['owner_user_id'],
            due_at=due, status='skipped', error_code=decision.code,
            error_message=decision.message, finished_at=now)
        await store.finish_schedule_tick(
            schedule_id, lease_owner=lease_owner, lease_epoch=lease_epoch,
            next_run_at=decision.next_run_at, last_run_at=now,
            last_status='skipped', last_error_code=decision.code,
            last_error_message=decision.message, deferred_count=0)
        return 'skipped'
    if decision.action == decide_module.DEFER:
        await store.record_occurrence(
            occurrence_id=occurrence_id_for(schedule_id, due), schedule_id=schedule_id,
            tenant_id=row['tenant_id'], owner_user_id=row['owner_user_id'],
            due_at=due, status='deferred', error_code=decision.code,
            error_message=decision.message)
        await store.finish_schedule_tick(
            schedule_id, lease_owner=lease_owner, lease_epoch=lease_epoch,
            next_attempt_at=decision.next_attempt_at,
            deferred_count=(int(row.get('deferred_count') or 0)
                            + (1 if decision.count_deferral else 0)),
            last_status='deferred', last_error_code=decision.code,
            last_error_message=decision.message)
        return 'deferred'

    # 3) 开跑
    return await _fire(hub, row, due, advance=True, now=now)


async def _fire(hub, row: dict, due: float, *, advance: bool, now: float) -> str:
    """建任务并派发。`advance=False` 用于手动触发（不动 next_run_at）。"""
    store = hub.store
    schedule_id = row['schedule_id']
    lease_owner = str(row.get('lease_owner') or '')
    lease_epoch = int(row.get('lease_epoch') or 0)
    occurrence_id = occurrence_id_for(schedule_id, due)
    session_id = uuid.uuid4().hex[:12]

    # 先落 occurrence 再派发：中间崩掉时，收敛阶段能凭这一行判"派发了但没跑起来"，
    # 而不是留下一个永远查不到的运行。
    await store.record_occurrence(
        occurrence_id=occurrence_id, schedule_id=schedule_id,
        tenant_id=row['tenant_id'], owner_user_id=row['owner_user_id'],
        due_at=due, status='queued', session_id=session_id,
        started_at=now)
    # 下一次一律从 **now** 算，不从 due 算：机器停过、上一轮延后过之后，
    # 从 due 推出来的"下一次"可能仍在过去，会立刻再触发一次。
    next_at, code, message = next_after(row, now) if advance else (
        float(row.get('next_run_at') or due), '', '')
    try:
        record = await hub.start(str(row.get('prompt') or ''), session_id=session_id,
                                 principal=_principal(row))
    except Exception as exc:                   # noqa: BLE001
        logger.exception('定时任务 %s 派发失败', schedule_id)
        await store.update_occurrence(
            occurrence_id, status='failed', error_code='dispatch_failed',
            error_message=f'{type(exc).__name__}: {exc}', finished_at=now)
        if advance:
            await store.finish_schedule_tick(
                schedule_id, lease_owner=lease_owner, lease_epoch=lease_epoch,
                next_run_at=next_at, last_run_at=now, last_status='failed',
                last_error_code='dispatch_failed',
                last_error_message=f'{type(exc).__name__}: {exc}',
                consecutive_failures=int(row.get('consecutive_failures') or 0) + 1,
                deferred_count=0)
        return 'error'

    try:
        await store.set_session_connectors(record.session_id,
                                           row.get('connector_ids') or None)
    except Exception:                          # noqa: BLE001
        logger.warning('会话 %s 写入连接器选择失败（不影响执行）', record.session_id,
                       exc_info=True)
    await store.update_occurrence(
        occurrence_id, status='running', task_id=record.task_id,
        run_id=record.run_id, error_code='', error_message='')
    if advance:
        await store.finish_schedule_tick(
            schedule_id, lease_owner=lease_owner, lease_epoch=lease_epoch,
            next_run_at=next_at, last_run_at=now, last_status='running',
            last_error_code='', last_error_message='',
            deferred_count=0)
    else:
        # 手动触发只释放租约，不动 next_run_at / last_run_at。
        await store.finish_schedule_tick(
            schedule_id, lease_owner=lease_owner, lease_epoch=lease_epoch,
            last_status='manual')
    logger.info('定时任务 %s 已派发 occurrence=%s run=%s',
                schedule_id, occurrence_id, record.run_id)
    return 'fired'


async def run_now(hub, schedule_id: str, worker_id: str, *,
                  tenant_id: str, owner_user_id: str) -> dict | None:
    """手动"立即运行"。与定时触发同一条执行路径，只是不推进 next_run_at。"""
    store = hub.store
    row = await store.claim_schedule_manually(
        schedule_id, worker_id=worker_id,
        lease_seconds=settings.scheduled_claim_lease_seconds)
    if row is None or (row.get('tenant_id'), row.get('owner_user_id')) != (
            tenant_id, owner_user_id):
        if row is not None:
            await store.finish_schedule_tick(
                schedule_id, lease_owner=str(row.get('lease_owner') or ''),
                lease_epoch=int(row.get('lease_epoch') or 0))
        return None
    now = time.time()
    await _fire(hub, row, now, advance=False, now=now)
    return await store.occurrence_for_due(schedule_id, now)


# ---------- 收敛与投递 ----------

async def collect(hub, now: float) -> tuple[int, int]:
    """把"跑完的 occurrence"收成终态，并把该通知的投递入队。

    这里只读 `runs` 行，不订阅执行器事件：Worker 和 API 是两个进程，
    跨进程可靠的通知只能靠"库里的事实"，不能靠内存回调。
    """
    store = hub.store
    collected = delivered = 0
    for occurrence in await store.open_occurrences(limit=100):
        occurrence_id = occurrence['occurrence_id']
        run_id = str(occurrence.get('run_id') or '')
        if not run_id:
            created = _iso_to_ts(occurrence.get('created_at')) or now
            if now - created > ORPHAN_GRACE_SECONDS:
                await store.update_occurrence(
                    occurrence_id, status='failed', error_code='not_started',
                    error_message='派发后没有生成运行记录', finished_at=now)
                collected += 1
            continue
        run = await store.run(run_id)
        if run is None:
            await store.update_occurrence(
                occurrence_id, status='failed', error_code='run_missing',
                error_message='运行记录不存在', finished_at=now)
            collected += 1
            continue
        status = str(run.get('status') or '')
        if status in STOPPED_RUN_STATUSES:
            # 等用户确认/输入：这不是失败，但也不能一直占着"未结束"。
            await _finish(hub, occurrence, status='paused',
                          code=status, message='等待人工确认后继续',
                          now=now, notify='needs_action')
            collected += 1
            continue
        if status not in TERMINAL_RUN_STATUSES:
            continue
        occurrence_status, code = _terminal_of(status)
        if await _finish(hub, occurrence, status=occurrence_status, code=code,
                         message=str(run.get('error') or ''), now=now,
                         notify=('success' if occurrence_status == 'succeeded'
                                 else ('needs_action' if code in ('partially_succeeded',
                                                                  'needs_reconcile')
                                       else 'failure'))):
            delivered += 1
        collected += 1
    return collected, delivered


def _terminal_of(run_status: str) -> tuple[str, str]:
    if run_status in SUCCESS_RUNS:
        return 'succeeded', ''
    if run_status in ATTENTION_RUNS:
        return 'succeeded', run_status
    return 'failed', run_status or 'failed'


async def _finish(hub, occurrence: dict, *, status: str, code: str,
                  message: str, now: float, notify: str) -> bool:
    """写终态 + 生成摘要 + 写回任务行 + 入队通知。返回是否入队了通知。"""
    store = hub.store
    occurrence_id = occurrence['occurrence_id']
    summary = await _summary(store, occurrence, message)
    await store.update_occurrence(
        occurrence_id, status=status, error_code=code, error_message=message,
        summary=summary, finished_at=now)
    schedule_id = str(occurrence.get('schedule_id') or '')
    # 列表上"上次 … · 执行中"必须能变成终态，否则卡片会永远停在执行中。
    await store.note_schedule_result(
        schedule_id, status=status, error_code=code, error_message=message,
        succeeded=(status == 'succeeded'))
    schedule = await store.schedule(schedule_id)
    if schedule is None:
        return False
    return await delivery_module.enqueue(
        store, schedule, occurrence, notify=notify, status=status, code=code,
        message=message, summary=summary)


async def _summary(store, occurrence: dict, fallback: str = '') -> str:
    """通知正文：优先任务终态的答复，其次会话最后一条 assistant 消息，最后是错误。

    **不做"截全文"**：`reply_text` 已经是给人看的一段话；只有拿不到它时才退到
    消息表，并且只在那段话真的被截断时才标注"截断"。
    """
    from webpage_agent.core.messages import reply_text

    task_id = str(occurrence.get('task_id') or '')
    if task_id:
        task = await store.task(task_id)
        state = (task or {}).get('state') or {}
        if state:
            try:
                text = reply_text(state)
            except Exception:                  # noqa: BLE001
                text = ''
            if text:
                return _clip(text)
            answer = str(state.get('answer') or '').strip()
            if answer:
                return _clip(answer)
    session_id = str(occurrence.get('session_id') or '')
    if not session_id:
        return ''
    messages = await store.messages(session_id) or []
    for row in reversed(messages):
        if str(row.get('role')) == 'assistant' and str(row.get('content') or '').strip():
            text = str(row['content']).strip()
            if len(text) > SUMMARY_LIMIT:
                return text[:SUMMARY_LIMIT] + '…（最终答复截断）'
            return text
    return _clip(fallback)


def _clip(text: str) -> str:
    clean = (text or '').strip()
    return clean if len(clean) <= SUMMARY_LIMIT else clean[:SUMMARY_LIMIT] + '…'
