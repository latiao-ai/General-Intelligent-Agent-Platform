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
import os
import sys
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api_hub import Hub                      # noqa: E402
from app.config import settings                  # noqa: E402
from app.ledger import ActionLedger              # noqa: E402


async def run_once(hub: Hub, worker_id: str, lease_seconds: float = 120.0) -> bool:
    """领取并执行一个运行。返回是否真的领到了东西。"""
    run = hub.store.claim_run(worker_id, lease_seconds)
    if run is None:
        return False
    run_id = run['run_id']
    print(f'[worker] 领取 run={run_id} epoch={run["lease_epoch"]}')

    task = hub.store.task(run['task_id'])
    if task is None:
        hub.store.update_run(run_id, status='failed', error='任务不存在')
        return True

    plan = hub.store.plan(run_id) or {}
    steps = plan.get('steps') or []
    record = hub.tasks.get(run['task_id'])
    if record is None or not record.live:
        # 没有内存记录时，按"续跑"处理：沿用已存计划重跑，
        # 已成功的动作由账本复用回执，不会重复调用远端。
        record = hub.get(run['task_id'])
        if record is None:
            hub.store.update_run(run_id, status='failed', error='无任务记录')
            return True
        record.run_id = run_id
        record.status = 'running'
        record._task = asyncio.create_task(
            hub._run(record, [], {}, resume_plan={'steps': steps} or None))
    try:
        await asyncio.wait_for(record._task, timeout=lease_seconds * 2)
    except asyncio.TimeoutError:
        hub.store.update_run(run_id, status='reconciling',
                             error='执行超过租约窗口')
    except Exception as exc:                      # noqa: BLE001
        hub.store.update_run(run_id, status='failed',
                             error=type(exc).__name__)
    return True


def drain_jobs(hub: Hub, worker_id: str, limit: int = 5) -> int:
    """消费后台任务（outbox）。

    第一版只处理 session_summary：把"该压缩会话记忆"这件事从请求链路挪到
    后台，避免阻塞每一次任务（文档 §12）。
    """
    done = 0
    for _ in range(limit):
        job = hub.store.claim_job(worker_id)
        if job is None:
            break
        try:
            if job['kind'] == 'session_summary':
                from app import memory as memory_module
                asyncio.run(memory_module.compact(hub.store,
                                                  job['payload'].get('session_id', '')))
            hub.store.finish_job(job['job_id'])
            done += 1
        except Exception as exc:                  # noqa: BLE001
            hub.store.finish_job(job['job_id'], error=type(exc).__name__)
    return done


async def main_loop(hub: Hub, worker_id: str, once: bool,
                    jobs_only: bool, interval: float) -> None:
    print(f'[worker] 启动 worker_id={worker_id} db={settings.db_path}')
    print(f'[worker] 启动对账: {hub.reconcile()}')
    while True:
        worked = drain_jobs(hub, worker_id)
        if not jobs_only:
            if await run_once(hub, worker_id):
                worked += 1
        if once and not worked:
            return
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

    worker_id = f'{os.uname().nodename}:{uuid.uuid4().hex[:6]}'
    hub = Hub()
    try:
        asyncio.run(main_loop(hub, worker_id, args.once, args.jobs_only,
                              args.interval))
    except KeyboardInterrupt:
        print('\n[worker] 退出')
    finally:
        hub.store.close()


if __name__ == '__main__':
    main()
