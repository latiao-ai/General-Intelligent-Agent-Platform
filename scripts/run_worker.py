"""独立 Worker：从数据库领取运行并执行（文档 §9.2）。

API 只负责建任务、写 queued 运行、返回；执行由 Worker 领取。第一版不引入
消息队列——数据库表就是队列，靠租约（lease_owner + lease_expires_at +
lease_epoch）保证"同一运行只有一个有效执行者"。

用法：

    python -m scripts.run_worker                 # 一直跑
    python -m scripts.run_worker --once          # 领一个就退出（测试/排障）
    python -m scripts.run_worker --jobs-only     # 只处理后台任务（摘要等）

仓储只有 PostgreSQL：`claim_run` 用 `FOR UPDATE SKIP LOCKED` 原子领取，
`lease_owner + lease_epoch` 做 fencing，所以可以放心多开 Worker。
"""
import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT / 'src'))

from webpage_agent.governance import execution_control
from webpage_agent.api.api_hub import Hub
from webpage_agent.core.config import settings
from webpage_agent.observe.logging_setup import setup_logging

logger = logging.getLogger('worker')

# 后台作业最多试几次。超过就留在 failed 里等人看，而不是无限重排。
MAX_JOB_ATTEMPTS = 5


async def _mark_owned(hub: Hub, run: dict, worker_id: str, **fields) -> None:
    """超时或异常收尾必须带上领取时的 owner 和代次。"""
    owner = str(run.get('lease_owner') or worker_id)
    epoch = int(run.get('lease_epoch') or 0)
    marker = getattr(hub.store, 'update_run_owned', None)
    if callable(marker):
        await marker(run['run_id'], lease_owner=owner, lease_epoch=epoch, **fields)
        return
    await hub.store.update_run(run['run_id'], **fields)


async def run_once(hub: Hub, worker_id: str, lease_seconds: float | None = None,
                   run_id: str | None = None) -> bool:
    """领取并执行一个运行。返回是否真的领到了东西。

    以前这里有三个会让"独立 Worker"完全跑不起来的问题（文档阶段 A）：

    1. `hub.get(...)` / `history(...)` / `prior_context(...)` 漏了 await，传下去的
       是协程对象；
    2. 组装 Record 的那几行在 try 之外，一次初始化异常就把整个 Worker 循环带走；
    3. `resume_plan={'steps': steps} or None` —— 空列表也是真值，于是"没有计划"
       被当成"有计划的续跑"。

    现在：领取 → 交给 `hub.execute_claimed_run()` 这一个入口，异常只影响本条 run。
    """
    lease = lease_seconds or settings.run_lease_seconds
    if hub.at_capacity():
        logger.info('本进程执行中的运行已达 %s，暂不领取',
                    settings.max_concurrent_runs)
        return False
    run = await hub.store.claim_run(worker_id, lease, run_id=run_id)
    if run is None:
        return False
    logger.info('领取 run=%s epoch=%s', run['run_id'], run['lease_epoch'])
    print(f'[worker] 领取 run={run["run_id"]} epoch={run["lease_epoch"]}')

    try:
        # execute_claimed_run 内部已 await 整次执行；这里只加"卡死永不返回"的护栏。
        # 租约由 _run 里的看守协程持续续期。
        await asyncio.wait_for(hub.execute_claimed_run(run),
                                       timeout=settings.task_timeout * 2)
        return True
    except asyncio.TimeoutError:
        await _mark_owned(hub, run, worker_id, status='reconciling',
                          error='执行超过预算窗口')
        logger.warning('run %s 执行超过预算窗口', run['run_id'])
        return True
    except asyncio.CancelledError:
        # 跨进程取消靠 cancel_requested_at 传话；只取消一个 run，不该带走 Worker
        current = await hub.store.run(run['run_id']) or {}
        if current.get('cancel_requested_at'):
            logger.info('run %s 已按请求取消', run['run_id'])
            return True
        raise
    except execution_control.LeaseLost as exc:
        # 被别人接手了：本条 run 的收尾由新执行者负责，Worker 继续领下一条
        logger.warning('run %s 执行权已转移：%s', run['run_id'], exc)
        return True
    except Exception as exc:                       # noqa: BLE001
        logger.exception('run %s 执行异常', run['run_id'])
        try:
            await _mark_owned(hub, run, worker_id, status='failed',
                              error=type(exc).__name__)
        except Exception:                          # noqa: BLE001
            logger.warning('run %s 失败状态写入也失败', run['run_id'],
                           exc_info=True)
        return True


async def drain_jobs(hub: Hub, worker_id: str, limit: int = 5) -> int:
    """消费后台任务（outbox）。

    第一版只处理 session_summary：把"该压缩会话记忆"这件事从请求链路挪到
    后台，避免阻塞每一次任务（文档 §12）。

    **这个函数必须是 async 的**。以前它是同步函数、却从正在运行的事件循环里
    被调用，内部又用 `asyncio.run(...)` —— 必然抛 `RuntimeError: asyncio.run()
    cannot be called from a running event loop`，被 `except` 吞掉后标成"完成"。
    结果是：摘要作业从来没成功过一次，而且失败即终态，不会重试。
    """
    done = 0
    for _ in range(limit):
        job = await hub.store.claim_job(worker_id)
        if job is None:
            break
        try:
            if job['kind'] == 'session_summary':
                from webpage_agent.core import memory as memory_module
                from webpage_agent.governance import core as governance
                session_id = job['payload'].get('session_id', '')
                session = await hub.store.session(session_id) or {}
                token = governance.bind(hub.store, session.get('tenant_id', 'default'))
                try:
                    await memory_module.compact(hub.store, session_id)
                finally:
                    governance.unbind(token)
            elif job['kind'] == 'schedule_delivery':
                from webpage_agent.scheduler import delivery as scheduler_delivery
                await scheduler_delivery.deliver_job(hub, job)
            else:
                raise RuntimeError(f'未知后台作业 {job["kind"]}')
            await hub.store.finish_job(job['job_id'])
            done += 1
        except Exception as exc:                  # noqa: BLE001
            attempts = int(job.get('attempts') or 0)
            retry = attempts < MAX_JOB_ATTEMPTS
            await hub.store.finish_job(job['job_id'], error=type(exc).__name__,
                                 retry=retry)
            logger.warning('后台作业 %s 失败（第 %s 次，%s）',
                           job['job_id'], attempts,
                           '稍后重试' if retry else '不再重试', exc_info=True)
    return done


async def heartbeat_loop(hub: Hub, worker_id: str) -> None:
    """定期写心跳：让 API 和监控看得见"执行器还活着"。

    为什么必须有：API 只会把消息写成 `accepted` 然后返回 202，真正干活的
    是本进程。没有心跳时，本进程挂掉对 API 完全不可见——客户看到的是
    "发出去没回复"，运维看到的是"服务正常"。心跳把这种"假活"变成可检测
    的事实：超过 `worker_alive_window_seconds` 没有心跳即视为离线，
    接纳层据此拒收，监控据此告警。

    心跳写失败不退出主循环：数据库短暂抖动时，执行比心跳重要。
    """
    host = os.uname().nodename
    pid = os.getpid()
    meta = json.dumps({'jobs_only': False,
                       'interval': settings.worker_heartbeat_seconds},
                      ensure_ascii=False)
    while True:
        try:
            await hub.store.beat('worker', worker_id, host=host, pid=pid,
                                 meta=meta)
        except asyncio.CancelledError:
            raise
        except Exception:                          # noqa: BLE001
            logger.warning('心跳写入失败（不影响执行）', exc_info=True)
        await asyncio.sleep(settings.worker_heartbeat_seconds)


async def drain_chat_turns(hub: Hub, worker_id: str, limit: int) -> list[asyncio.Task]:
    """领取聊天 turn 并**非阻塞地**派出执行任务（第十一阶段 A 组第 1 点）。

    聊天 turn 与业务 run **分池**：业务长任务、外部轮询吃的是 run 的并发额度，
    聊天走 `session_chat_concurrency` 自己的额度，不会互相阻塞。

    这里只领取 + `create_task` 派发，**不 await 执行完成**——否则一条聊天 turn
    的真实模型调用会拖住整个主循环，让业务 run 和外部轮询全部饿死。返回派发的
    任务列表，由调用方（`main_loop`）加入 in-flight 集合统一收口。
    """
    from webpage_agent.core.config import settings as _settings
    if not (_settings.session_chat_enabled or _settings.session_query_enabled):
        return []
    limit = max(1, int(limit or 1))
    tasks: list[asyncio.Task] = []
    for _ in range(limit):
        turn = await hub.store.claim_turn(
            worker_id, _settings.session_turn_lease_seconds)
        if turn is None:
            break
        tasks.append(asyncio.create_task(
            hub.execute_claimed_turn(turn), name=f'turn:{turn["turn_id"]}'))
    return tasks


async def _run_one_spawned(hub: Hub, run: dict, worker_id: str) -> None:
    """执行一条已领取的业务 run（在独立 task 里，异常只影响本条）。"""
    try:
        await asyncio.wait_for(hub.execute_claimed_run(run),
                               timeout=settings.task_timeout * 2)
    except asyncio.TimeoutError:
        await _mark_owned(hub, run, worker_id, status='reconciling',
                          error='执行超过预算窗口')
        logger.warning('run %s 执行超过预算窗口', run['run_id'])
    except asyncio.CancelledError:
        current = await hub.store.run(run['run_id']) or {}
        if not current.get('cancel_requested_at'):
            raise
        logger.info('run %s 已按请求取消', run['run_id'])
    except execution_control.LeaseLost as exc:
        logger.warning('run %s 执行权已转移：%s', run['run_id'], exc)
    except Exception as exc:                       # noqa: BLE001
        logger.exception('run %s 执行异常', run['run_id'])
        try:
            await _mark_owned(hub, run, worker_id, status='failed',
                              error=type(exc).__name__)
        except Exception:                          # noqa: BLE001
            logger.warning('run %s 失败状态写入也失败', run['run_id'],
                           exc_info=True)


async def drain_runs_spawned(hub: Hub, worker_id: str) -> list[asyncio.Task]:
    """领取业务 run 并**非阻塞地**派出（第十一阶段 A 组第 1 点）。

    业务 run 的并发上限由 `hub.at_capacity()`（进程内 in-flight）约束；这里领取
    后 spawn 独立 task 就返回，不 await 执行完成。返回派发的任务列表。
    """
    if hub.at_capacity():
        logger.info('本进程执行中的运行已达 %s，暂不领取',
                    settings.max_concurrent_runs)
        return []
    run = await hub.store.claim_run(worker_id, settings.run_lease_seconds)
    if run is None:
        return []
    logger.info('领取 run=%s epoch=%s', run['run_id'], run['lease_epoch'])
    print(f'[worker] 领取 run={run["run_id"]} epoch={run["lease_epoch"]}')
    return [asyncio.create_task(_run_one_spawned(hub, run, worker_id),
                                name=f'run:{run["run_id"]}')]


async def main_loop(hub: Hub, worker_id: str, once: bool,
                    jobs_only: bool, interval: float) -> None:
    from webpage_agent.observe import runtime_check
    problems = runtime_check.issues()
    if problems:
        raise RuntimeError('; '.join(problems))
    logger.info('启动 worker_id=%s', worker_id)
    print(f'[worker] 启动 worker_id={worker_id}')
    ready = getattr(hub.store, 'ready', None)
    if callable(ready):
        await ready()          # 连不上库就在启动阶段失败
    # 只对账**自己名下或租约已过期**的运行：多副本启动不能替别人判死
    stats = await hub.reconcile(owner=worker_id)
    logger.info('启动对账 %s', stats)
    print(f'[worker] 启动对账: {stats}')

    # 聊天 turn 与业务 run 的 in-flight 任务集合：主循环只领取 + 派发，
    # 不 await 执行完成，保证三者互不阻塞（第十一阶段 A 组第 1 点）。
    # 聊天 turn 单独计数（chat_inflight），按**实际在途数量**限流：只有当前在途
    # 聊天数 < session_chat_concurrency 才继续领，否则本轮跳过（定点修复 1）。
    inflight: set[asyncio.Task] = set()
    chat_inflight: set[asyncio.Task] = set()
    # 定时任务的下一次 tick（单调时钟）。**不要**用 `await asyncio.sleep(30)`：
    # 这个主循环同时负责领取业务 run，睡 30s 会一起拖慢。
    next_schedule_tick = time.monotonic()

    while True:
        # 1. 后台作业（session_summary 等，快速领取执行）
        worked = await drain_jobs(hub, worker_id)

        # 2. 外部轮询（独立，不阻塞聊天/业务领取）
        if settings.async_tool_runtime_enabled:
            from webpage_agent.core import external_tasks
            try:
                worked += await external_tasks.poll_due(
                    hub.store, worker_id=worker_id)
            except Exception:                      # noqa: BLE001
                logger.exception('外部任务轮询失败')

        # 2b. 定时任务：到点才跑（认领 → 派发 → 收敛），单次失败不影响主循环。
        if settings.scheduled_tasks_enabled and time.monotonic() >= next_schedule_tick:
            next_schedule_tick = time.monotonic() + settings.scheduler_tick_seconds
            try:
                from webpage_agent.scheduler import tick as scheduler_tick
                stats = await scheduler_tick.tick(hub, worker_id)
                if any(stats.values()):
                    logger.info('定时任务 tick：%s', stats)
                    worked += 1
            except Exception:                      # noqa: BLE001
                logger.exception('定时任务 tick 失败')

        # 3. 聊天 turn 与业务 run：各自领取 + 派发，不等待完成
        if not jobs_only:
            # 聊天 turn 实际在途限流：额度 = 总额度 - 当前在途聊天数（定点修复 1）。
            chat_quota = max(0, int(settings.session_chat_concurrency)
                             - len(chat_inflight))
            if chat_quota > 0:
                try:
                    chat_tasks = await drain_chat_turns(
                        hub, worker_id, chat_quota)
                    chat_inflight.update(chat_tasks)
                    inflight.update(chat_tasks)
                except Exception:                  # noqa: BLE001
                    logger.exception('聊天 turn 消费失败')
            try:
                inflight.update(await drain_runs_spawned(hub, worker_id))
            except Exception:                      # noqa: BLE001
                logger.exception('业务 run 领取失败')

        # 4. 收口已完成的任务（回收 done task，避免泄漏）。
        #    定点修复 3：已取消的任务 `t.exception()` 会抛 CancelledError，必须先
        #    用 `t.cancelled()` 判断，否则一个被取消的 turn/run 会带走整个主循环。
        if inflight:
            done, inflight = await asyncio.wait(inflight, timeout=0)
            for t in done:
                chat_inflight.discard(t)
                if t.cancelled():
                    logger.info('后台执行任务已取消回收: %s', t.get_name())
                    continue
                exc = t.exception()
                if exc is not None:
                    logger.warning('后台执行任务异常收口: %s', exc, exc_info=exc)

        if once:
            # once 模式：等已派发的任务跑完再退出，否则 task 被提前回收。
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)
            return
        if not worked:
            await asyncio.sleep(interval)


async def run_worker(worker_id: str, once: bool, jobs_only: bool,
                     interval: float) -> None:
    """Worker 的完整生命周期：建 Hub → 主循环 → **在同一个循环里**关连接池。

    连接池必须跟它创建时的事件循环一起收尾。以前是 `asyncio.run(main_loop(...))`
    之后再 `asyncio.run(store.close())`：那是第二个事件循环，池里的后台任务属于
    已经关闭的第一个循环，close() 会抛一串 `CancelledError` 噪音（关闭本身其实
    也没做成），退出码还可能是非零。
    """
    hub = Hub(owner_id=worker_id)
    background: list[asyncio.Task] = []
    try:
        ready = getattr(hub.store, 'ready', None)
        if callable(ready):
            await ready()          # 连不上库就在启动阶段失败
        # 心跳 + 看门狗：
        # - 心跳让 API 与监控看得见"执行器还在"（--once 是一次性排障模式，
        #   不写心跳，避免短暂冒充"有人值守"）；
        # - 看门狗在只部署了 Worker、没有 API 的拓扑下也能兜住卡住的 turn，
        #   多副本并发由 advisory lock 拦住。
        from webpage_agent.core.config import settings as _settings
        from webpage_agent.observe import watchdog
        if not once:
            background.append(asyncio.create_task(
                heartbeat_loop(hub, worker_id), name='heartbeat'))
        if _settings.turn_watchdog_enabled:
            background.append(asyncio.create_task(
                watchdog.loop(hub.store), name='watchdog'))
        await main_loop(hub, worker_id, once, jobs_only, interval)
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        from webpage_agent.integrations.mcp_adapter import close_stdio_sessions
        await close_stdio_sessions()
        await hub.store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='中台执行 Worker')
    parser.add_argument('--once', action='store_true', help='领一次就退出')
    parser.add_argument('--jobs-only', action='store_true', help='只跑后台任务')
    parser.add_argument('--interval', type=float, default=1.0)
    args = parser.parse_args()

    setup_logging('worker')
    worker_id = f'{os.uname().nodename}:{uuid.uuid4().hex[:6]}'
    try:
        asyncio.run(run_worker(worker_id, args.once, args.jobs_only,
                               args.interval))
    except KeyboardInterrupt:
        print('\n[worker] 退出')


if __name__ == '__main__':
    main()
