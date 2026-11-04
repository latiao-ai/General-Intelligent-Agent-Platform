"""一次任务在本进程里的记录。

权威状态在 PostgreSQL。内存里只留订阅者队列，以及从库里刷回来的字段。
`CLOSE` 与 `TERMINAL_STATUS` 仍定义在 api_hub，本文件末尾再导入，避免成环。
类体与拆分前逐字相同。
"""
import asyncio
import time
from dataclasses import dataclass, field

from webpage_agent.core import outcome as outcome_module
from webpage_agent.store.pg_store import PostgresStore

@dataclass
class TaskRecord:
    task_id: str
    session_id: str
    message: str
    store: PostgresStore
    status: str = 'running'
    run_id: str = ''                    # 本次执行尝试的 ID（账本、续跑都挂在它上面）
    artifact_id: str | None = None      # run_task 内部的任务 ID，即产物目录名
    error: str | None = None
    state: dict | None = None
    started_at: float = field(default_factory=time.time)
    latency: float | None = None        # 收到指令 → 开始执行工具 的耗时
    open_stage: dict | None = None      # 当前未关闭的阶段 {'stage','at'}
    # 谁在执行这个 run：持有租约的那一方。就地执行（API 进程）也必须写上，
    # 否则 Worker 会把"正在跑的 run"当孤儿再领一遍。
    lease_owner: str | None = None
    # 本次领取的租约代次：所有推进都带它去库里做条件更新（fenced）。
    # 内存里只用于展示与排障，判断依据永远是数据库。
    lease_epoch: int = 0
    tenant_id: str = 'default'
    owner_user_id: str = 'default'
    idempotent_replay: bool = False
    _lease_lost: bool = False
    _subscribers: set = field(default_factory=set)
    _task: asyncio.Task | None = None

    @property
    def live(self) -> bool:
        """本进程里是否还有正在跑的任务。从库里恢复的记录为 False。"""
        return self._task is not None and not self._task.done()

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STATUS

    async def publish(self, event: dict) -> None:
        """先落库再投递：库里是权威记录，内存只保证实时性。

        draft 与 answer_delta 同样落库，保证 API 与 Worker 分离后仍能实时补读。
        事件按保留期清理；所有事件共享同一序号与跨进程通知。
        """
        seq = await self.store.append_event(self.task_id, event)
        for queue in list(self._subscribers):
            queue.put_nowait((seq, event))

    async def subscribe(self):
        """返回 (队列, 历史快照)。

        注册订阅与读取快照之间可能插入新事件（两者之间无法做到原子），
        因此投递给订阅者的是 `(seq, event)`，由消费端按 seq 去重（见 webpage_agent/api/routes.py）。
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue, await self.store.events(self.task_id)

    def unsubscribe(self, queue) -> None:
        self._subscribers.discard(queue)

    def close(self) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(CLOSE)
        self._subscribers.clear()

    async def persist(self, **fields) -> None:
        """写回库并更新内存状态：**先落库，再改内存**。

        顺序反过来会开一个窗口：`done`/`live` 这些判断读的是内存，库是另一份。
        内存先置成终态、库要等下一条语句才写，这中间 `record.done` 已经是 True，
        而 `store.task()` 还是旧的 `queued`/`running`。回放接口按库读，
        于是"任务已完成但回放显示还在排队"——多进程下别的副本看到的也是旧值。
        同文件的 `publish()` 早就按"先落库再投递"写，这里保持一致。
        """
        await self.store.update_task(self.task_id, **fields)
        for key, value in fields.items():
            setattr(self, key, value)

    def summary(self) -> dict:
        """接口视图。

        `status` 保持旧字段名与前端的词汇（展示标签），`run_status` 才是状态机
        里的通用值，`outcome` 是本轮产出的结构化记录。判流程用 run_status，
        给人看用 status——两者不要混用。
        """
        return {
            'task_id': self.task_id, 'session_id': self.session_id,
            **outcome_module.view(self.status, self.state or {}),
            'message': self.message, 'error': self.error,
            'artifact_id': self.artifact_id, 'state': self.state,
        }

from webpage_agent.api.api_hub import CLOSE, TERMINAL_STATUS
