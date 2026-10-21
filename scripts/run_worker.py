"""独立 Worker：从数据库领取运行并执行（文档 §9.2）。

API 只负责建任务、写 queued 运行、返回；执行由 Worker 领取。第一版不引入
消息队列——数据库表就是队列，靠租约（lease_owner + lease_expires_at +
lease_epoch）保证"同一运行只有一个有效执行者"。

用法：

    python -m scripts.run_worker                 # 一直跑
    python -m scripts.run_worker --once          # 领一个就退出（测试/排障）
    python -m scripts.run_worker --jobs-only     # 只处理后台任务（摘要等）

本机开发用 SQLite，**只允许单 Worker**（SQLite 没有 SKIP LOCKED，多进程
领取靠租约串行化，并发能力有限）。生产换 PostgreSQL 后同一份代码可以多开。
"""
import argparse
import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api_hub import Hub                      # noqa: E402
from app.config import settings                  # noqa: E402
from app.logging_setup import setup_logging      # noqa: E402

logger = logging.getLogger('worker')

# 后台作业最多试几次。超过就留在 failed 里等人看，而不是无限重排。
MAX_JOB_ATTEMPTS = 5


async def run_once(hub: Hub, worker_id: str, lease_seconds: float | None = None,
                   run_id: str | None = None) -> bool:
    """领取并执行一个运行。返回是否真的领到了东西。"""
    lease = lease_seconds or settings.run_lease_seconds
    run = await hub.store.claim_run(worker_id, lease, run_id=run_id)
    if run is None:
        return False
    run_id = run['run_id']
    logger.info('领取 run=%s epoch=%s', run_id, run['lease_epoch'])
    print(f'[worker] 领取 run={run_id} epoch={run["lease_epoch"]}')

    task = await hub.store.task(run['task_id'])
    if task is None:
        await hub.store.update_run(run_id, status='failed', error='任务不存在')
        return True

    plan = await hub.store.plan(run_id) or {}
    steps = plan.get('steps') or []
    record = hub.tasks.get(run['task_id'])
    if record is None or not record.live:
        # 没有内存记录时，按"续跑"处理：沿用已存计划重跑，
        # 已成功的动作由账本复用回执，不会重复调用远端。
        record = hub.get(run['task_id'])
        if record is None:
            await hub.store.update_run(run_id, status='failed', error='无任务记录')
            return True
        record.run_id = run_id
        record.status = 'running'
        # 租约归属必须记在记录上：_run 里的看守协程靠它续租、靠它判断
        # "我才是执行者"。漏了这一步，续租和跨进程取消都会失效。
        record.lease_owner = run['lease_owner']
        record._task = asyncio.create_task(hub._run(
            record,
            hub.history(record.session_id),
            hub.prior_context(record.session_id),
            resume_plan={'steps': steps} or None,
            memory=await hub.store.memory(record.session_id),
        ))
    try:
        # 租约由 _run 里的看守协程持续续期，这里只防"卡死永不返回"。
        await asyncio.wait_for(record._task, timeout=settings.task_timeout * 2)
    except asyncio.TimeoutError:
        await hub.store.update_run(run_id, status='reconciling',
                             error='执行超过预算窗口')
        logger.warning('run %s 执行超过预算窗口', run_id)
    except asyncio.CancelledError:
        # 跨进程取消是通过 cancel_requested_at 传话的：_guard 看到标记后会取消
        # 这个任务，CancelledError 随之冒到这里。**不能**让它把整个 Worker 循环
        # 带走——只取消了一个 run，不是要停进程。
        current = await hub.store.run(run_id) or {}
        if current.get('cancel_requested_at'):
            logger.info('run %s 已按请求取消', run_id)
            return True
        raise
    except Exception as exc:                      # noqa: BLE001
        await hub.store.update_run(run_id, status='failed',
                             error=type(exc).__name__)
        logger.exception('run %s 执行异常', run_id)
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
                from app import memory as memory_module
                await memory_module.compact(
                    hub.store, job['payload'].get('session_id', ''))
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


async def main_loop(hub: Hub, worker_id: str, once: bool,
                    jobs_only: bool, interval: float) -> None:
    logger.info('启动 worker_id=%s db=%s', worker_id, settings.db_path)
    print(f'[worker] 启动 worker_id={worker_id} db={settings.db_path}')
    ready = getattr(hub.store, 'ready', None)
    if callable(ready):
        await ready()          # 连不上库就在启动阶段失败
    # 只对账**自己名下或租约已过期**的运行：多副本启动不能替别人判死
    stats = await hub.reconcile(owner=worker_id)
    logger.info('启动对账 %s', stats)
    print(f'[worker] 启动对账: {stats}')
    while True:
        worked = await drain_jobs(hub, worker_id)
        if not jobs_only:
            if await run_once(hub, worker_id):
                worked += 1
        if once:
            return
        if not worked:
            await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description='中台执行 Worker')
    parser.add_argument('--once', action='store_true', help='领一次就退出')
    parser.add_argument('--jobs-only', action='store_true', help='只跑后台任务')
    parser.add_argument('--interval', type=float, default=1.0)
    args = parser.parse_args()

    setup_logging('worker')
    worker_id = f'{os.uname().nodename}:{uuid.uuid4().hex[:6]}'
    hub = Hub(owner_id=worker_id)
    try:
        asyncio.run(main_loop(hub, worker_id, args.once, args.jobs_only,
                              args.interval))
    except KeyboardInterrupt:
        print('\n[worker] 退出')
    finally:
        # 收尾在事件循环之外：这里同步地把连接池关掉
        asyncio.run(hub.store.close())


if __name__ == '__main__':
    main()
