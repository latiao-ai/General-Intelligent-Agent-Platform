"""用 PostgreSQL NOTIFY 唤醒本进程的 SSE。

通知里只带 task_id。丢了通知也不丢事件：SSE 仍按序号从数据库补读。
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

_waiters: dict[str, list[asyncio.Event]] = {}
_listener: asyncio.Task | None = None


def subscribe(task_id: str) -> asyncio.Event:
    event = asyncio.Event()
    _waiters.setdefault(task_id, []).append(event)
    return event


def unsubscribe(task_id: str, event: asyncio.Event) -> None:
    waiting = _waiters.get(task_id) or []
    _waiters[task_id] = [item for item in waiting if item is not event]
    if not _waiters[task_id]:
        _waiters.pop(task_id, None)


def wake(task_id: str) -> None:
    for event in list(_waiters.get(task_id) or []):
        event.set()


async def _listen(dsn: str) -> None:
    import psycopg
    while True:
        try:
            connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
            try:
                await connection.execute('LISTEN agent_task_events')
                async for notice in connection.notifies():
                    payload = str(getattr(notice, 'payload', '') or '')
                    if payload:
                        wake(payload)
            finally:
                await connection.close()
        except asyncio.CancelledError:
            raise
        except Exception:                              # noqa: BLE001
            logger.warning('任务事件监听中断，两秒后重连', exc_info=True)
            await asyncio.sleep(2)


def start(dsn: str) -> None:
    """进程内只开一条监听连接。没有数据库地址时不启动。"""
    global _listener
    if not dsn or (_listener is not None and not _listener.done()):
        return
    _listener = asyncio.create_task(_listen(dsn), name='task-events')


async def stop() -> None:
    global _listener
    task = _listener
    _listener = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
