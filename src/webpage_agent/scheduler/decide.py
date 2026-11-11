"""触发判定：**纯函数**，不碰数据库、不碰时钟。

把"这一次该不该跑"从 IO 里拆出来，是因为它是这套东西里最容易写错的一段：
错过窗口、上一轮还在跑、进程已满、抖动，四个条件交织，只能靠单测钉住。
`tick.py` 负责取数据、调它、把结果写回去。
"""
from __future__ import annotations

from dataclasses import dataclass

FIRE = 'fire'
DEFER = 'defer'
SKIP = 'skip'


@dataclass(frozen=True)
class Decision:
    action: str
    # 推进到哪一次（skip 时用；不是 None 就写回 next_run_at）
    next_run_at: float | None = None
    # 延后重试的闸门（defer 时用）
    next_attempt_at: float = 0.0
    # 这次延后要不要计数（抖动不算"跑不上"，不该吃掉 defer 次数）
    count_deferral: bool = False
    code: str = ''
    message: str = ''


def decide(*, due: float, now: float, missed_window: float, busy: bool,
           at_capacity: bool, deferred_count: int, max_deferrals: int,
           defer_backoff: float = 60.0, dispatch_at: float | None = None,
           next_run_at: float | None = None) -> Decision:
    """`due` 是该 occurrence 的计划触发时刻，`now` 是当前时刻（epoch 秒）。

    顺序与 WorkBuddy 的 `decide()` 一致，但多了一层"上一轮还没结束"的显式判断：

    1. 上一轮还在跑（busy）或本进程已满（at_capacity）→ 延后，不记失败；
       延后次数到顶 → 记 `skipped/deferred_too_long` 并推进，绝不无限堆。
    2. 错过太久（`now - due > missed_window`）→ 记 `skipped/missed_window`，
       **不补跑**。补跑一堆过期任务是"重启后收到 47 条推送"的来源。
    3. 抖动还没到点 → 延后，但**不计数**（它不是"跑不上"）。
    4. 否则开跑。
    """
    if busy or at_capacity:
        if deferred_count + 1 >= max_deferrals:
            return Decision(SKIP, next_run_at=next_run_at,
                            code='deferred_too_long',
                            message=f'连续 {deferred_count + 1} 次没能开跑，跳过这一次')
        return Decision(DEFER, next_attempt_at=now + defer_backoff,
                        count_deferral=True, code='busy',
                        message='上一轮还没结束' if busy else '本进程并发已满')
    if now - due > missed_window:
        return Decision(SKIP, next_run_at=next_run_at, code='missed_window',
                        message='超过错过窗口，不再补跑')
    if dispatch_at is not None and now < dispatch_at:
        return Decision(DEFER, next_attempt_at=min(dispatch_at, now + defer_backoff),
                        count_deferral=False, code='jitter',
                        message='按抖动窗口延后')
    return Decision(FIRE)


def jitter_delay(schedule_id: str, due: float, window: float) -> float:
    """按 (任务, occurrence) 确定性的散开量，避免所有任务卡在同一秒触发。

    用 FNV-1a 而不是 `hash()`：字符串 `hash()` 每个进程都不同（PYTHONHASHSEED），
    多副本会算出不同延迟，抖动就变成了"每次都不一样"。
    """
    if window <= 0:
        return 0.0
    seed = f'{schedule_id}:{int(due)}'.encode('utf-8')
    value = 2166136261
    for byte in seed:
        value ^= byte
        value = (value * 16777619) & 0xFFFFFFFF
    return (value / 0xFFFFFFFF) * float(window)
