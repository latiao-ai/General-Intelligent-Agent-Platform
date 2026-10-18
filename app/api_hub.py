"""任务中枢：会话、任务记录与事件广播（异步原生 + SQLite 持久化）。

分工：

- **SQLite（`app/store.py`）**：会话消息、任务状态、事件日志的**权威来源**。
  进程重启后会话与历史事件都还在。
- **内存**：只负责"实时投递"——订阅者队列与正在跑的 asyncio 任务。这两样天然
  不可能持久化：一个 asyncio.Task 是活着的进程状态，队列是连接级的。

因此晚连上来的、甚至重启之后再连上来的客户端，都能从库里补发完整执行过程，
而正在跑的客户端拿到的是内存直投的零延迟事件。用事件序号（seq）去重。

与 `app/session.py` 的区别：那个服务 Streamlit（脚本执行会阻塞 UI，所以用后台
线程 + 轮询），这里服务 FastAPI，直接跑在服务器事件循环上。
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field

from app.config import settings
from app.events import stage_end, stage_start
from app.main import run_task
from app.messages import cancel_note, reply_text
from app.store import Store

TERMINAL_STATUS = {'deployed', 'deploy_needs_check', 'local_ready', 'failed',
                   'cancelled', 'interrupted', 'answered'}

# 合成阶段：衡量"用户发出指令"到"真正开始搜索"之间的空档。
INSTRUCTION_STAGE = 'instruction'

# 哨兵：通知订阅者流已结束
CLOSE = object()


@dataclass
class TaskRecord:
    task_id: str
    session_id: str
    message: str
    store: Store
    status: str = 'running'
    artifact_id: str | None = None      # run_task 内部的任务 ID，即产物目录名
    error: str | None = None
    state: dict | None = None
    started_at: float = field(default_factory=time.time)
    latency: float | None = None        # 收到指令 → 开始搜索 的耗时
    open_stage: dict | None = None      # 当前未关闭的阶段 {'stage','at'}
    _subscribers: set = field(default_factory=set)
    _task: asyncio.Task | None = None

    @property
    def live(self) -> bool:
        """本进程里是否还有正在跑的任务。从库里恢复的记录为 False。"""
        return self._task is not None and not self._task.done()

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STATUS

    def publish(self, event: dict) -> None:
        """先落库再投递：库里是权威记录，内存只保证实时性。

        例外是 draft（半成品报告，每份是整份快照）与 answer_delta（增量文本，
        一次可能上千片）：它们体积大或条数多，落库会让 events 表膨胀，
        因此只做实时投递。序号传 None，SSE 端的去重逻辑会跳过它们。
        """
        if event.get('type') in ('draft', 'answer_delta'):
            for queue in list(self._subscribers):
                queue.put_nowait((None, event))
            return
        seq = self.store.append_event(self.task_id, event)
        for queue in list(self._subscribers):
            queue.put_nowait((seq, event))

    def subscribe(self):
        """返回 (队列, 历史快照)。

        注册订阅与读取快照之间可能插入新事件（两者之间无法做到原子），
        因此投递给订阅者的是 `(seq, event)`，由消费端按 seq 去重（见 app/api.py）。
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue, self.store.events(self.task_id)

    def unsubscribe(self, queue) -> None:
        self._subscribers.discard(queue)

    def close(self) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(CLOSE)
        self._subscribers.clear()

    def persist(self, **fields) -> None:
        """更新内存状态并写回库。"""
        for key, value in fields.items():
            setattr(self, key, value)
        self.store.update_task(self.task_id, **fields)

    def summary(self) -> dict:
        return {
            'task_id': self.task_id, 'session_id': self.session_id,
            'status': self.status, 'message': self.message, 'error': self.error,
            'artifact_id': self.artifact_id, 'state': self.state,
        }


class Hub:
    """会话与任务注册表。持久部分在 SQLite，实时部分是进程内的订阅队列。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store if store is not None else Store(settings.db_path)
        self.tasks: dict[str, TaskRecord] = {}

    def history(self, session_id: str) -> list:
        return self.store.messages(session_id)

    def start(self, message: str, session_id: str | None = None) -> TaskRecord:
        session_id = session_id or uuid.uuid4().hex[:12]
        history = self.history(session_id)
        self.store.append_message(session_id, 'user', message)
        record = TaskRecord(task_id=uuid.uuid4().hex[:12], session_id=session_id,
                            message=message, store=self.store)
        self.store.create_task(record.task_id, session_id, message)
        self.tasks[record.task_id] = record
        # 先占位一条"收到指令"的开始事件，让它排在阶段表第一行；
        # 真正进入检索阶段时再补齐耗时。
        event = stage_start(INSTRUCTION_STAGE)
        record.open_stage = {'stage': INSTRUCTION_STAGE, 'at': event['at']}
        record.publish(event)
        record._task = asyncio.create_task(self._run(record, history))
        return record

    async def _run(self, record: TaskRecord, history: list) -> None:
        def on_start(artifact_id: str) -> None:
            record.persist(artifact_id=artifact_id)
            record.publish({'type': 'task', 'task_id': record.task_id,
                            'artifact_id': artifact_id})

        def close_instruction(status: str) -> None:
            """补齐"收到指令"的耗时。只补一次；中断/失败也要补，否则那一行会一直停在"进行中"。"""
            if record.latency is not None:
                return
            record.latency = time.time() - record.started_at
            if record.open_stage and record.open_stage['stage'] == INSTRUCTION_STAGE:
                # 必须清掉，否则 close_open_stage 会把它再关一次
                record.open_stage = None
            record.publish(stage_end(INSTRUCTION_STAGE, record.latency, status))

        def track(event: dict) -> None:
            kind = event.get('type')
            if kind == 'stage_start':
                record.open_stage = {'stage': event.get('stage'),
                                     'at': event.get('at', time.time())}
            elif kind == 'stage_end' and record.open_stage \
                    and record.open_stage['stage'] == event.get('stage'):
                record.open_stage = None

        def close_open_stage(status: str) -> None:
            """兜底关闭仍未结束的阶段。

            任务被取消时，节点自己发的 stage_end 会丢：它写进 LangGraph 的流队列，
            而 run_task 的消费循环此刻已经被取消，没人来取。因此必须由中枢直接补发，
            否则前端那一行会永远停在"进行中"、耗时一路涨下去。
            """
            opened = record.open_stage
            if not opened:
                return
            record.open_stage = None
            record.publish(stage_end(opened['stage'],
                                     time.time() - opened['at'], status))

        def relay(event: dict) -> None:
            if (event.get('type') == 'stage_start'
                    and event.get('stage') == 'discover_sources'):
                close_instruction('解析完成')
            track(event)
            record.publish(event)

        try:
            state = await run_task(
                record.message, on_event=relay,
                on_start=on_start, history=history,
            )
        except asyncio.CancelledError:
            close_instruction('已中断')
            close_open_stage('已中断')
            detail = cancel_note(record.artifact_id)
            record.persist(status='cancelled')
            # 给会话补一条助手回复：否则下一轮上下文里会出现两条连续的用户消息，
            # 模型会看不出上一轮其实是被打断的。
            self.store.append_message(record.session_id, 'assistant', '（上一轮被用户中断）')
            record.publish({'type': 'status', 'status': 'cancelled', 'detail': detail})
            raise
        except Exception as exc:
            close_instruction('已失败')
            close_open_stage('已失败')
            record.error = f'{type(exc).__name__}: {exc}'
            record.persist(status='failed', error=record.error)
            record.publish({'type': 'status', 'status': 'failed',
                            'detail': record.error})
        else:
            close_instruction('解析完成')
            close_open_stage('已结束')
            record.state = state
            status = state.get('status', 'failed')
            reply = reply_text(state)
            self.store.append_message(record.session_id, 'assistant', reply)
            if record.latency is not None:
                # 同步写进持久化的 timings，state.json 与 CLI 汇总表同样能看到
                state.setdefault('timings', []).insert(0, {
                    'stage': INSTRUCTION_STAGE,
                    'seconds': round(record.latency, 2),
                    'status': '解析完成，开始搜索',
                })
            record.persist(status=status, state=state)
            record.publish({'type': 'status', 'status': status, 'detail': reply})
        finally:
            # 无论成功、失败还是被取消，都要唤醒并释放所有订阅者。
            record.close()

    def get(self, task_id: str) -> TaskRecord | None:
        """取任务。内存里没有再回库找——支持进程重启后继续查询与补发事件。"""
        record = self.tasks.get(task_id)
        if record is not None:
            return record
        data = self.store.task(task_id)
        if data is None:
            return None
        record = TaskRecord(task_id=data['task_id'], session_id=data['session_id'],
                            message=data['message'], store=self.store)
        record.status = data['status']
        record.artifact_id = data['artifact_id']
        record.error = data['error']
        record.state = data['state']
        self.tasks[task_id] = record
        return record

    def cancel(self, task_id: str) -> bool:
        """只能取消本进程里正在跑的任务；重启后恢复的历史任务无法取消。"""
        record = self.tasks.get(task_id)
        if record is None or not record.live:
            return False
        record._task.cancel()
        return True

    def close(self) -> None:
        self.store.close()
