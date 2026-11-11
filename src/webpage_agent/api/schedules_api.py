"""定时任务接口（`/api/schedules/*`）。

## 边界

- **租户/用户只从 `principal` 取**：客户端传什么都不信（与 `/api/tasks` 一致）。
  一条定时任务会在未来某天以"它的创建者"的身份执行，所以归属必须落在服务端。
- 接口只负责"定义"与"立即运行"；真正的定时触发在 Worker 的调度 tick 里，
  所以关掉浏览器不影响它跑。
- 改规则/启停/改有效期都会**重算 `next_run_at`**：否则用户把"每天"改成"每周"后，
  卡片上仍显示旧的触发时间，看起来像没生效。

## 为什么创建时就把 `next_run_at` 算好

调度器每次 tick 只做 `WHERE next_run_at <= now` 的索引扫描，不在 tick 里解析规则。
规则解析只发生在"创建/修改/推进下一次"这三个点，坏了也只坏一处。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.core import rrules
from webpage_agent.core import services as services_module
from webpage_agent.core.config import settings
from webpage_agent.governance import policies
from webpage_agent.integrations import webhooks
from webpage_agent.scheduler import delivery as delivery_module
from webpage_agent.scheduler import tick as tick_module

router = APIRouter(prefix='/api/schedules', tags=['schedules'])

MAX_PROMPT = 10000
MAX_NAME = 60


def _hub():
    """用与业务接口同一个 Hub（含并发额度与就地执行能力）。

    延迟导入避免与 `routes.py`（它要 include 本 router）成环。
    """
    from webpage_agent.api import routes
    return routes.hub


class DeliveryConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channel: str = ''
    webhook_url: str = ''
    events: list[str] | None = None


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=MAX_NAME)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT)
    type: str = 'recurring'
    rrule: str = ''
    scheduled_at: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    enabled: bool = True
    connector_ids: list[str] | None = None
    delivery: DeliveryConfig | None = None


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME)
    prompt: str | None = Field(default=None, min_length=1, max_length=MAX_PROMPT)
    type: str | None = None
    rrule: str | None = None
    scheduled_at: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    enabled: bool | None = None
    connector_ids: list[str] | None = None
    delivery: DeliveryConfig | None = None


# ---------- 校验与转换 ----------

def _parse_iso(value: str | None, *, field: str) -> float | None:
    text = (value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
    except ValueError:
        raise HTTPException(status_code=400, detail=f'{field} 不是合法的 ISO 时间') from None


def _validate_prompt(prompt: str) -> str:
    text = (prompt or '').strip()
    if not text:
        raise HTTPException(status_code=400, detail='提示词不能为空')
    if len(text) > MAX_PROMPT:
        raise HTTPException(status_code=400, detail=f'提示词最多 {MAX_PROMPT} 字')
    if services_module.is_stop_command(text):
        # `Hub.start` 会把"停/取消"当控制指令处理，根本不建任务——保存下来
        # 只会得到一个永远不执行、也不报错的任务。必须在这里挡住。
        raise HTTPException(status_code=400,
                            detail='提示词不能是"停止/取消"这类控制指令')
    return text


def _next_for(*, kind: str, rrule_text: str, scheduled_at: str | None,
              valid_from: str | None, valid_until: str | None,
              after: float | None = None) -> tuple[float | None, str]:
    """算下一次触发：返回 `(epoch | None, 描述)`。规则非法时抛 400。"""
    now = time.time() if after is None else float(after)
    floor = _parse_iso(valid_from, field='valid_from')
    ceil = _parse_iso(valid_until, field='valid_until')
    if kind == 'once':
        target = _parse_iso(scheduled_at, field='scheduled_at')
        if target is None:
            raise HTTPException(status_code=400, detail='一次性任务必须给 scheduled_at')
        return (rrules.once_run_at(target, now=now, valid_from=floor, valid_until=ceil),
                '一次性任务')
    if kind != 'recurring':
        raise HTTPException(status_code=400, detail='type 只支持 recurring / once')
    try:
        rule = rrules.parse(rrule_text)
    except rrules.RuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return (rrules.next_run_at(rule, now, valid_from=floor, valid_until=ceil),
            rrules.describe(rule))


def _delivery_json(config: DeliveryConfig | None) -> dict:
    if config is None or not (config.channel or '').strip():
        return {}
    channel = config.channel.strip()
    if channel not in webhooks.CHANNELS:
        raise HTTPException(status_code=400,
                            detail='channel 只支持 ' + '/'.join(webhooks.CHANNELS))
    events = [item for item in (config.events or delivery_module.DEFAULT_EVENTS)
              if item in delivery_module.DEFAULT_EVENTS]
    url = (config.webhook_url or '').strip()
    if url:
        try:
            url = webhooks.validate_url(url)
        except webhooks.DeliveryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return {'channel': channel, 'webhook_url': url,
            'events': events or list(delivery_module.DEFAULT_EVENTS)}


def _view(row: dict) -> dict:
    """对外视图。推送地址**脱敏**：它通常带 key，回显出去等于把密钥写进页面和日志。"""
    delivery = dict(row.get('delivery') or {})
    if delivery.get('webhook_url'):
        delivery['webhook_url'] = _mask(delivery['webhook_url'])
    rule_text = ''
    try:
        if row.get('schedule_type') == 'recurring':
            rule_text = rrules.describe(rrules.parse(str(row.get('rrule') or '')))
    except rrules.RuleError:
        rule_text = '规则无效'
    if row.get('schedule_type') == 'once':
        rule_text = '一次性任务'
    return {
        'schedule_id': row.get('schedule_id'),
        'name': row.get('name'),
        'prompt': row.get('prompt'),
        'enabled': bool(row.get('enabled')),
        'type': row.get('schedule_type'),
        'rrule': row.get('rrule'),
        'schedule_desc': rule_text,
        'scheduled_at': row.get('scheduled_at'),
        'valid_from': row.get('valid_from'),
        'valid_until': row.get('valid_until'),
        'next_run_at': row.get('next_run_at'),
        'last_run_at': row.get('last_run_at'),
        'last_status': row.get('last_status') or '',
        'last_error': row.get('last_error_message') or '',
        'consecutive_failures': int(row.get('consecutive_failures') or 0),
        'connector_ids': row.get('connector_ids') or [],
        'delivery': delivery,
        'created_at': row.get('created_at'),
        'updated_at': row.get('updated_at'),
    }


def _mask(url: str) -> str:
    text = str(url or '')
    if '?' in text:
        head, _, tail = text.partition('?')
        return head + '?…' + (tail[-4:] if len(tail) > 4 else '')
    if len(text) <= 24:
        return text
    return text[:20] + '…' + text[-4:]


async def _owned(schedule_id: str, principal) -> dict:
    row = await _hub().store.schedule(
        schedule_id, tenant_id=principal.tenant_id, owner_user_id=principal.user_id)
    if row is None:
        # 不区分"不存在"与"不是你的"
        raise HTTPException(status_code=404, detail='定时任务不存在')
    return row


# ---------- 接口 ----------

@router.get('')
async def list_schedules(status: str = 'all',
                         principal=Depends(policies.current_principal)):
    if status not in ('all', 'active', 'paused'):
        raise HTTPException(status_code=400, detail='status 只支持 all/active/paused')
    rows = await _hub().store.list_schedules(
        tenant_id=principal.tenant_id, owner_user_id=principal.user_id, status=status)
    return {'tasks': [_view(row) for row in rows],
            'enabled': settings.scheduled_tasks_enabled}


@router.post('', status_code=201)
async def create_schedule(payload: ScheduleBody,
                          principal=Depends(policies.current_principal)):
    prompt = _validate_prompt(payload.prompt)
    name = payload.name.strip()
    next_run_at, _ = _next_for(kind=payload.type, rrule_text=payload.rrule,
                               scheduled_at=payload.scheduled_at,
                               valid_from=payload.valid_from,
                               valid_until=payload.valid_until)
    if payload.enabled and next_run_at is None:
        raise HTTPException(status_code=400,
                            detail='这条规则不会（再）触发，请检查时间或有效期')
    row = await _hub().store.create_schedule(
        schedule_id=uuid.uuid4().hex[:12],
        tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
        owner_role=str(getattr(principal, 'role', '') or 'member'),
        platform_id=str(getattr(principal, 'platform_id', '') or ''),
        platform_subject=str(getattr(principal, 'subject', '') or ''),
        name=name, prompt=prompt, enabled=bool(payload.enabled),
        schedule_type=payload.type, rrule=payload.rrule.strip(),
        scheduled_at=payload.scheduled_at, valid_from=payload.valid_from,
        valid_until=payload.valid_until,
        next_run_at=next_run_at if payload.enabled else None,
        connector_ids=payload.connector_ids, delivery=_delivery_json(payload.delivery))
    return _view(row)


@router.get('/{schedule_id}')
async def get_schedule(schedule_id: str,
                       principal=Depends(policies.current_principal)):
    return _view(await _owned(schedule_id, principal))


@router.patch('/{schedule_id}')
async def patch_schedule(schedule_id: str, payload: SchedulePatch,
                         principal=Depends(policies.current_principal)):
    current = await _owned(schedule_id, principal)
    fields: dict = {}
    if payload.name is not None:
        fields['name'] = payload.name.strip()
    if payload.prompt is not None:
        fields['prompt'] = _validate_prompt(payload.prompt)
    if payload.type is not None:
        fields['schedule_type'] = payload.type
    if payload.rrule is not None:
        fields['rrule'] = payload.rrule.strip()
    if payload.scheduled_at is not None:
        fields['scheduled_at'] = payload.scheduled_at
    if payload.valid_from is not None:
        fields['valid_from'] = payload.valid_from
    if payload.valid_until is not None:
        fields['valid_until'] = payload.valid_until
    if payload.enabled is not None:
        fields['enabled'] = bool(payload.enabled)
    # connector/delivery 在仓储里是 *_json 列，这里直接按列名编码。
    if payload.connector_ids is not None:
        fields['connector_ids_json'] = json.dumps(payload.connector_ids,
                                                  ensure_ascii=False)
    if payload.delivery is not None:
        fields['delivery_json'] = json.dumps(_delivery_json(payload.delivery),
                                             ensure_ascii=False)
    updated = await _hub().store.update_schedule(
        schedule_id, tenant_id=principal.tenant_id,
        owner_user_id=principal.user_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail='定时任务不存在')
    merged = {**current, **updated}
    # 规则/有效期/启停变了就重算下一次；只改名不重算（否则每改一次标题都会
    # 把"每天 09:00"推到明天，用户会觉得设置被吃掉了）。
    schedule_changed = any(key in fields for key in (
        'schedule_type', 'rrule', 'scheduled_at', 'valid_from', 'valid_until',
        'enabled'))
    if schedule_changed:
        recomputed, _ = _next_for(
            kind=str(merged.get('schedule_type') or 'recurring'),
            rrule_text=str(merged.get('rrule') or ''),
            scheduled_at=merged.get('scheduled_at'),
            valid_from=merged.get('valid_from'),
            valid_until=merged.get('valid_until'))
        await _hub().store.reschedule(
            schedule_id, tenant_id=principal.tenant_id,
            owner_user_id=principal.user_id,
            next_run_at=recomputed if merged.get('enabled') else None)
        merged = await _owned(schedule_id, principal)
    return _view(merged)


@router.delete('/{schedule_id}')
async def delete_schedule(schedule_id: str,
                          principal=Depends(policies.current_principal)):
    await _owned(schedule_id, principal)
    ok = await _hub().store.delete_schedule(
        schedule_id, tenant_id=principal.tenant_id,
        owner_user_id=principal.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail='定时任务不存在')
    return {'deleted': True}


@router.post('/{schedule_id}/trigger', status_code=202)
async def trigger_schedule(schedule_id: str,
                           principal=Depends(policies.current_principal)):
    """立即运行一次。**不推进** `next_run_at`：手动试跑不该顶掉今晚的定时。"""
    await _owned(schedule_id, principal)
    occurrence = await tick_module.run_now(
        _hub(), schedule_id, f'manual:{principal.user_id}',
        tenant_id=principal.tenant_id, owner_user_id=principal.user_id)
    if occurrence is None:
        raise HTTPException(status_code=409, detail='这条任务正在执行，稍后再试')
    return {'occurrence_id': occurrence.get('occurrence_id'),
            'run_id': occurrence.get('run_id'),
            'task_id': occurrence.get('task_id'),
            'session_id': occurrence.get('session_id')}


@router.get('/{schedule_id}/runs')
async def schedule_runs(schedule_id: str, limit: int = 20,
                        principal=Depends(policies.current_principal)):
    await _owned(schedule_id, principal)
    rows = await _hub().store.list_occurrences(
        schedule_id, limit=max(1, min(200, limit)))
    return {'runs': [{
        'occurrence_id': row.get('occurrence_id'),
        'due_at': row.get('due_at'),
        'status': row.get('status'),
        'task_id': row.get('task_id'),
        'run_id': row.get('run_id'),
        'session_id': row.get('session_id'),
        'error_code': row.get('error_code'),
        'error_message': row.get('error_message'),
        'summary': row.get('summary'),
        'delivery_status': row.get('delivery_status'),
        'delivery_attempts': int(row.get('delivery_attempts') or 0),
        'started_at': row.get('started_at'),
        'finished_at': row.get('finished_at'),
    } for row in rows]}


@router.post('/{schedule_id}/delivery/test')
async def test_delivery(schedule_id: str,
                        principal=Depends(policies.current_principal)):
    """配置自检：立刻发一条测试消息，不产生运行记录。"""
    row = await _owned(schedule_id, principal)
    config = row.get('delivery') or {}
    channel = str(config.get('channel') or '')
    if channel not in webhooks.CHANNELS:
        raise HTTPException(status_code=400, detail='这条任务没有配置推送渠道')
    url = str(config.get('webhook_url') or settings.scheduled_default_webhook or '')
    if not url:
        raise HTTPException(status_code=400, detail='没有可用的推送地址')
    body = webhooks.build_payload(
        channel, name=str(row.get('name') or '定时任务'),
        status_text='🔔 这是一条测试消息',
        summary='收到它就说明推送配置可用。', occurred_at='')
    try:
        await webhooks.post_webhook(url, body, channel=channel)
    except webhooks.DeliveryError as exc:
        raise HTTPException(status_code=502, detail=f'{exc.code}: {exc}') from None
    return {'sent': True, 'channel': channel}
