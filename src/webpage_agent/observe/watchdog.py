"""执行器失联的兜底：把卡住的 turn 结算成「有话说」的失败。

**为什么需要这个模块（真实事故）**

聊天/查询的模型调用不是 API 进程做的：API 只把消息写成 `accepted` 并立刻
返 202，真正领取并执行的是 Worker。Worker 一旦被回收（终端关掉、容器退出、
OOM、重启），库里那批 `accepted` 就再也没人领——前端一直转圈，客户看到的
就是"发出去没回复"，而 API 表面上一切正常（HTTP 200、消息也确实进了库）。

三道防线里这是最后一道：**就算上面的进程守护和接单门禁都没兜住，也要在
有限时间内给用户一句明确的话**。所以这里的结算必须同时写 `answer`：
只把状态改成 failed 而不写答复，前端仍然什么都没有——那只是把"没回复"
换个地方发生。

判定口径（宁少杀不误杀）：

1. `accepted`/`retry` 且超过容忍期没人领：
   - 没有活 Worker 时用 `turn_accept_grace_seconds`（默认 120 秒）；
   - 有活 Worker 时用 `turn_queue_wait_max_seconds`（默认 600 秒）——
     同会话串行、并发额度满导致排队是正常现象，不能当卡死；
   - 该会话里还有 `running` 的 turn 时跳过：那是排队，不是没人管。
2. `running` 且租约过期超过宽限期：执行者已经不在了（续租会失败），
   结果未知，如实说明且**不自动重试**，避免重复副作用。

多副本并发：用 advisory lock 让同一时刻只有一个副本扫，扫不到锁就直接退出。
逐行 `FOR UPDATE SKIP LOCKED` 保证不会两副本同时结算同一条 turn。
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from webpage_agent.core.config import settings

logger = logging.getLogger('watchdog')

# 与 maintenance 用不同的锁号，两者不互相阻塞。
LOCK_ID = 719284633

# 结算时写给用户的话。必须是**人话 + 可操作**，不是错误码。
# 两种成因必须分开说：说错了比不说更糟——用户会照着错误的原因去处理。
STUCK_ANSWER = (
    '这条消息没有执行成功：系统里没有可用的执行器（Worker 未在运行），'
    '消息一直没人领取。请稍后重试；如果反复出现，请把这句话转给运维。')
QUEUED_ANSWER = (
    '这条消息排队太久，已经中止：执行器在线，但在容忍时间内一直没轮到它。'
    '请重新发送一次；如果队列持续积压，请联系运维加 Worker 副本或并发额度。')
LOST_ANSWER = (
    '这条消息执行中断了：执行过程中执行器失联（Worker 异常退出或重启），'
    '结果未知。为避免重复操作，系统没有自动重试，请确认当前进度后再重发。')


def _iso_before(seconds: float, now_ts: float | None = None) -> str:
    """当前时间往前推 seconds 的 ISO 字符串。

    `session_turns.created_at` 存的是 `datetime.now(timezone.utc).isoformat()`
    的文本，同格式下按文本比较与按时间比较等价，所以直接比字符串。
    """
    base = time.time() if now_ts is None else now_ts
    return (datetime.fromtimestamp(base, timezone.utc)
            - timedelta(seconds=float(seconds))).isoformat()


async def reap_once(store, *, now_ts: float | None = None) -> dict:
    """扫一轮并结算卡住的 turn。返回各类计数，便于日志与测试断言。"""
    stats = {'stuck': 0, 'lost': 0, 'workers_alive': 0}
    if not settings.turn_watchdog_enabled:
        return stats
    current = time.time() if now_ts is None else float(now_ts)
    stamp = datetime.fromtimestamp(current, timezone.utc).isoformat()
    accepted_deadline = _iso_before(
        settings.turn_accept_grace_seconds, current)
    queue_deadline = _iso_before(
        settings.turn_queue_wait_max_seconds, current)
    lost_deadline = current - float(settings.turn_lost_grace_seconds)

    async with store._lease() as conn:
        locked = await store._one(
            conn, 'SELECT pg_try_advisory_xact_lock(%s) AS locked', (LOCK_ID,))
        if not locked or not locked.get('locked'):
            return stats

        row = await store._one(
            conn, 'SELECT COUNT(*) AS n FROM service_heartbeats '
                  'WHERE service=%s AND beat_at >= %s',
            ('worker', current - settings.worker_alive_window_seconds))
        workers_alive = int((row or {}).get('n') or 0)
        stats['workers_alive'] = workers_alive

        # --- 1. 一直没人领的 ---
        deadline = queue_deadline if workers_alive else accepted_deadline
        if workers_alive:
            code, answer = 'E_TURN_STUCK', QUEUED_ANSWER
        else:
            code, answer = 'E_WORKER_OFFLINE', STUCK_ANSWER
        candidates = await store._all(conn,
            "SELECT turn_id, session_id, status, kind FROM session_turns "
            "WHERE status IN ('accepted','retry') AND created_at < %s "
            'ORDER BY created_at LIMIT 50 FOR UPDATE SKIP LOCKED',
            (deadline,))
        for turn in candidates:
            busy = await store._one(conn,
                "SELECT 1 FROM session_turns WHERE session_id=%s "
                "AND status='running' AND lease_expires_at > %s LIMIT 1",
                (turn['session_id'], current))
            if busy is not None:
                # 同会话还有在跑的 turn：这是排队，不是没人管
                continue
            await _settle(conn, store, turn, stamp, code=code, answer=answer)
            stats['stuck'] += 1

        # --- 2. 领走了但执行者失联的 ---
        lost = await store._all(conn,
            "SELECT turn_id, session_id, status, kind FROM session_turns "
            "WHERE status='running' AND lease_expires_at > 0 "
            'AND lease_expires_at < %s '
            'ORDER BY lease_expires_at LIMIT 50 FOR UPDATE SKIP LOCKED',
            (lost_deadline,))
        for turn in lost:
            await _settle(conn, store, turn, stamp,
                          code='E_WORKER_LOST', answer=LOST_ANSWER)
            stats['lost'] += 1
        await conn.commit()

    if stats['stuck'] or stats['lost']:
        logger.warning('看门狗结算卡住的 turn %s（活 Worker %s 个）',
                       stats, stats['workers_alive'])
    return stats


async def _settle(conn, store, turn: dict, stamp: str, *,
                  code: str, answer: str) -> None:
    """终态 + 答复 + 消息三件套在同一个事务里落。

    走的是 `settle_turn` 的同一套去重键（`turn-answer:{turn_id}`），所以
    即便和正常的结算撞上，也只会有一条答复，不会重复。
    """
    await store._run(conn,
        "UPDATE session_turns SET status='failed', answer=%s, error=%s, "
        'lease_expires_at=0, updated_at=%s WHERE turn_id=%s',
        (answer, code, stamp, turn['turn_id']))
    await store._run(conn,
        'INSERT INTO messages (session_id, role, content, created_at, dedupe_key) '
        'VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
        (turn['session_id'], 'assistant', answer, stamp,
         f'turn-answer:{turn["turn_id"]}'))
    await store._run(conn,
        'UPDATE sessions SET updated_at=%s WHERE session_id=%s',
        (stamp, turn['session_id']))


async def loop(store) -> None:
    """周期任务入口。单轮失败不影响下一轮。"""
    while True:
        await asyncio.sleep(settings.turn_watchdog_interval_seconds)
        try:
            await reap_once(store)
        except asyncio.CancelledError:
            raise
        except Exception:                          # noqa: BLE001
            logger.exception('watchdog_failed')
