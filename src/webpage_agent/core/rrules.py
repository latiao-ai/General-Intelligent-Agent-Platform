"""RRULE 子集：解析、下一次触发计算、人类可读描述。

**纯函数、无 IO、无数据库**，所以可以直接单测，也能被接口层与调度器同时使用。

## 为什么是 RRULE 子集而不是 cron

用户嘴里的规则是"每两周一、周四 09:00""每月 1 号早上"这类话，cron 表达不了
`INTERVAL`，只能靠"每隔一个周的星期几"这种补丁；而本项目**没有**云端调度要兼容，
所以只保留一种表示（WorkBuddy 同时留 RRULE 与 6 段 cron 并互相转换，是它的历史包袱）。

## 语义（与 WorkBuddy 的 `computeNextRunAt` 对齐）

- `after` 是**上一次触发时刻**（epoch 秒），返回**严格大于它**的下一次；创建任务时传 `now`。
  用上一次触发而不是 `now` 做基准，是为了 `INTERVAL=2` 这类规则不会随执行延迟漂移。
- 结果必须 `>= valid_from`（给了的话）且 `<= valid_until`（给了的话），否则返回 `None`
  （= 这个任务不再触发了）。
- 按**进程本地时区**算（`datetime.fromtimestamp` / `.timestamp()`），落库是 epoch 秒。
- `MONTHLY;BYMONTHDAY=31` 遇到没有 31 号的月份**夹到当月最后一天**；
  `YEARLY;BYMONTH=2;BYMONTHDAY=29` 平年**夹到 2/28**。RFC 5545 是跳过，这里跟随 WorkBuddy。
- `FREQ=HOURLY` **忽略 `BYMINUTE`**（分钟固定为 0），与 WorkBuddy 一致。
- 缺省值：`BYHOUR` 取 `after` 的时，`BYMINUTE` 取 `after` 的分，`BYMONTHDAY` 取 `after` 的日，
  `BYMONTH` 取 `after` 的月，`BYDAY` 取 `after` 的星期。所以 `FREQ=DAILY` 就是"每天同一时刻"。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

FREQS = ('HOURLY', 'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')
WEEKDAYS = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')
_WEEKDAY_INDEX = {name: index for index, name in enumerate(WEEKDAYS)}
# 中文短名，给前端与通知文案用（周一起）。
WEEKDAY_LABELS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
# 一个月/年的候选扫描上限：真出现"永远不满足"的规则（例如 2 月 30 日）也要能停下。
_MAX_PERIODS = 1200
# INTERVAL 上限：挡住 `INTERVAL=999999` 这种把扫描拖死的输入。
_MAX_INTERVAL = 366


class RuleError(ValueError):
    """规则不合法。调用方应把它变成 400，而不是猜一个默认值继续跑。"""


@dataclass(frozen=True)
class Rule:
    freq: str
    interval: int = 1
    byday: tuple[int, ...] = ()
    bymonthday: tuple[int, ...] = ()
    bymonth: tuple[int, ...] = ()
    byhour: tuple[int, ...] = ()
    byminute: tuple[int, ...] = ()

    def to_text(self) -> str:
        parts = [f'FREQ={self.freq}']
        if self.interval != 1:
            parts.append(f'INTERVAL={self.interval}')
        if self.byday:
            parts.append('BYDAY=' + ','.join(WEEKDAYS[index] for index in self.byday))
        if self.bymonthday:
            parts.append('BYMONTHDAY=' + ','.join(str(day) for day in self.bymonthday))
        if self.bymonth:
            parts.append('BYMONTH=' + ','.join(str(month) for month in self.bymonth))
        if self.byhour:
            parts.append('BYHOUR=' + ','.join(str(hour) for hour in self.byhour))
        if self.byminute:
            parts.append('BYMINUTE=' + ','.join(str(minute) for minute in self.byminute))
        return ';'.join(parts)


def _ints(value: str, *, low: int, high: int, name: str) -> tuple[int, ...]:
    items: list[int] = []
    for chunk in value.split(','):
        text = chunk.strip()
        if not text:
            continue
        try:
            number = int(text)
        except ValueError:
            raise RuleError(f'{name} 必须是整数：{text!r}') from None
        if number < low or number > high:
            raise RuleError(f'{name} 超出 {low}-{high}：{number}')
        items.append(number)
    if not items:
        raise RuleError(f'{name} 不能为空')
    return tuple(sorted(set(items)))


def _days(value: str) -> tuple[int, ...]:
    items: list[int] = []
    for chunk in value.split(','):
        text = chunk.strip().upper()
        if not text:
            continue
        if text not in _WEEKDAY_INDEX:
            raise RuleError(f'BYDAY 只支持 MO/TU/WE/TH/FR/SA/SU：{text!r}')
        items.append(_WEEKDAY_INDEX[text])
    if not items:
        raise RuleError('BYDAY 不能为空')
    return tuple(sorted(set(items)))


def parse(text: str) -> Rule:
    """解析 `FREQ=...;INTERVAL=...` 形式的规则。未知键直接报错，不静默忽略。"""
    raw = (text or '').strip()
    if not raw:
        raise RuleError('规则不能为空')
    fields: dict[str, str] = {}
    for chunk in raw.split(';'):
        piece = chunk.strip()
        if not piece:
            continue
        if '=' not in piece:
            raise RuleError(f'规则片段缺少 "="：{piece!r}')
        key, _, value = piece.partition('=')
        key = key.strip().upper()
        if key in fields:
            raise RuleError(f'规则里 {key} 重复出现')
        fields[key] = value.strip()
    unknown = set(fields) - {'FREQ', 'INTERVAL', 'BYDAY', 'BYMONTHDAY', 'BYMONTH',
                             'BYHOUR', 'BYMINUTE'}
    if unknown:
        raise RuleError('不支持的规则字段：' + '、'.join(sorted(unknown)))
    freq = (fields.pop('FREQ', '') or '').upper()
    if freq not in FREQS:
        raise RuleError('FREQ 只支持 ' + '/'.join(FREQS))
    interval = 1
    if 'INTERVAL' in fields:
        try:
            interval = int(fields.pop('INTERVAL'))
        except ValueError:
            raise RuleError('INTERVAL 必须是整数') from None
        if interval < 1 or interval > _MAX_INTERVAL:
            raise RuleError(f'INTERVAL 必须在 1-{_MAX_INTERVAL} 之间')
    rule = Rule(
        freq=freq,
        interval=interval,
        byday=_days(fields['BYDAY']) if 'BYDAY' in fields else (),
        bymonthday=(_ints(fields['BYMONTHDAY'], low=1, high=31, name='BYMONTHDAY')
                    if 'BYMONTHDAY' in fields else ()),
        bymonth=(_ints(fields['BYMONTH'], low=1, high=12, name='BYMONTH')
                 if 'BYMONTH' in fields else ()),
        byhour=(_ints(fields['BYHOUR'], low=0, high=23, name='BYHOUR')
                if 'BYHOUR' in fields else ()),
        byminute=(_ints(fields['BYMINUTE'], low=0, high=59, name='BYMINUTE')
                  if 'BYMINUTE' in fields else ()),
    )
    if rule.freq == 'WEEKLY' and not rule.byday:
        raise RuleError('WEEKLY 必须给 BYDAY')
    if rule.freq == 'MONTHLY' and not rule.bymonthday:
        raise RuleError('MONTHLY 必须给 BYMONTHDAY')
    if rule.freq == 'YEARLY' and not rule.bymonth:
        raise RuleError('YEARLY 必须给 BYMONTH')
    return rule


# ---------- 候选生成 ----------

def _times(rule: Rule, anchor: datetime) -> list[tuple[int, int]]:
    hours = rule.byhour or (anchor.hour,)
    minutes = (0,) if rule.freq == 'HOURLY' else (rule.byminute or (anchor.minute,))
    return [(hour, minute) for hour in hours for minute in minutes]


def _clamped_day(year: int, month: int, day: int) -> int:
    """把 1-31 的日夹到当月真实天数（2 月 30 日 → 28/29）。"""
    return min(day, calendar.monthrange(year, month)[1])


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _period_candidates(rule: Rule, anchor: datetime, step: int) -> list[datetime]:
    """第 step 个周期的候选时刻（本地 naive，升序）。"""
    if rule.freq == 'HOURLY':
        day = anchor + timedelta(hours=rule.interval * step)
        if rule.byday and day.weekday() not in rule.byday:
            return []
        if rule.byhour and day.hour not in rule.byhour:
            return []
        return [day.replace(minute=0, second=0, microsecond=0)]
    if rule.freq == 'DAILY':
        day = anchor + timedelta(days=rule.interval * step)
        if rule.byday and day.weekday() not in rule.byday:
            return []
        return [day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                for hour, minute in _times(rule, anchor)]
    if rule.freq == 'WEEKLY':
        week_start = anchor - timedelta(days=anchor.weekday())
        start = week_start + timedelta(weeks=rule.interval * step)
        days = rule.byday or (anchor.weekday(),)
        times = _times(rule, anchor)
        return [start.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=offset, hours=hour, minutes=minute)
                for offset in days for hour, minute in times]
    if rule.freq == 'MONTHLY':
        year, month = _add_months(anchor.year, anchor.month, rule.interval * step)
        days = rule.bymonthday or (anchor.day,)
        times = _times(rule, anchor)
        return [datetime(year, month, _clamped_day(year, month, day),
                         hour, minute, 0, 0)
                for day in days for hour, minute in times]
    # YEARLY
    year = anchor.year + rule.interval * step
    months = rule.bymonth or (anchor.month,)
    days = rule.bymonthday or (anchor.day,)
    times = _times(rule, anchor)
    return [datetime(year, month, _clamped_day(year, month, day), hour, minute, 0, 0)
            for month in months for day in days for hour, minute in times]


def _candidates(rule: Rule, anchor: datetime) -> Iterator[datetime]:
    for step in range(_MAX_PERIODS):
        produced = _period_candidates(rule, anchor, step)
        if produced:
            yield from sorted(produced)


def next_run_at(rule: Rule, after: float, *, valid_from: float | None = None,
                valid_until: float | None = None) -> float | None:
    """下一次触发时刻（epoch 秒）；`None` = 不再触发。

    `after` 传上一次触发时刻（创建时传 `now`）。严格大于它，且落在有效期窗口内。
    """
    anchor = datetime.fromtimestamp(float(after))
    upper = float(valid_until) if valid_until is not None else None
    floor = float(valid_from) if valid_from is not None else None
    for candidate in _candidates(rule, anchor):
        stamp = candidate.timestamp()
        # 候选按周期单调前进：越过有效期上界就不必再找了。
        if upper is not None and stamp > upper:
            return None
        if stamp <= float(after):
            continue
        if floor is not None and stamp < floor:
            continue
        return stamp
    return None


# ---------- 一次性任务 ----------

def once_run_at(scheduled_at: float, *, now: float,
                valid_from: float | None = None,
                valid_until: float | None = None) -> float | None:
    """一次性任务的时间点。已经过去（或超出有效期）就返回 `None`。"""
    target = float(scheduled_at)
    if valid_from is not None and target < float(valid_from):
        return None
    if valid_until is not None and target > float(valid_until):
        return None
    return target if target > float(now) else None


# ---------- 人类可读 ----------

def _clock(rule: Rule) -> str:
    hours = rule.byhour or ()
    minutes = rule.byminute or ()
    if not hours:
        return ''
    if minutes:
        return '、'.join(f'{hour:02d}:{minute:02d}' for hour in hours for minute in minutes)
    return '、'.join(f'{hour:02d}:00' for hour in hours)


def describe(rule: Rule) -> str:
    """给界面用的一句话：`每天 09:00`、`每 2 周周一 09:00`、`每月 1 日 09:00`。"""
    every = '' if rule.interval == 1 else f'每 {rule.interval} '
    clock = _clock(rule)
    tail = f' {clock}' if clock and rule.freq != 'HOURLY' else ''
    if rule.freq == 'HOURLY':
        interval = '' if rule.interval == 1 else f'每 {rule.interval} 小时'
        head = '每小时' if rule.interval == 1 else interval
        if rule.byhour:
            head += '（' + '、'.join(f'{hour:02d}:00' for hour in rule.byhour) + '）'
        return head
    if rule.freq == 'DAILY':
        return f'{every}天{tail}' if every else f'每天{tail}'
    if rule.freq == 'WEEKLY':
        days = '、'.join(WEEKDAY_LABELS[index] for index in rule.byday)
        if rule.interval == 1:
            return f'每周{days}{tail}'
        return f'每 {rule.interval} 周{days}{tail}'
    if rule.freq == 'MONTHLY':
        days = '、'.join(f'{day} 日' for day in rule.bymonthday)
        if rule.interval == 1:
            return f'每月 {days}{tail}'
        return f'每 {rule.interval} 个月 {days}{tail}'
    months = '、'.join(f'{month} 月' for month in rule.bymonth)
    if not rule.bymonthday:
        body = months
    else:
        body = '、'.join(f'{month} 月 {day} 日'
                        for month in rule.bymonth for day in rule.bymonthday)
    if rule.interval == 1:
        return f'每年 {body}{tail}'
    return f'每 {rule.interval} 年 {body}{tail}'
