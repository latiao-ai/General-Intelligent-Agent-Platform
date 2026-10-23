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
import logging
import os
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import execution_control                # noqa: E402
from app.api_hub import Hub                      # noqa: E402
from app.config import settings                  # noqa: E402
from app.logging_setup import setup_logging      # noqa: E402

logger = logging.getLogger('worker')

# 后台作业最多试几次。超过就留在 failed 里等人看，而不是无限重排。
MAX_JOB_ATTEMPTS = 5


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
        await hub.store.update_run(run['run_id'], status='reconciling',
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
            await hub.store.update_run(run['run_id'], status='failed',
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
    logger.info('启动 worker_id=%s db=%s', worker_id, settings.database_url)
    print(f'[worker] 启动 worker_id={worker_id} db={settings.database_url}')
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


async def run_worker(worker_id: str, once: bool, jobs_only: bool,
                     interval: float) -> None:
    """Worker 的完整生命周期：建 Hub → 主循环 → **在同一个循环里**关连接池。

    连接池必须跟它创建时的事件循环一起收尾。以前是 `asyncio.run(main_loop(...))`
    之后再 `asyncio.run(store.close())`：那是第二个事件循环，池里的后台任务属于
    已经关闭的第一个循环，close() 会抛一串 `CancelledError` 噪音（关闭本身其实
    也没做成），退出码还可能是非零。
    """
    hub = Hub(owner_id=worker_id)
    try:
        await main_loop(hub, worker_id, once, jobs_only, interval)
    finally:
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
