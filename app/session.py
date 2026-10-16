"""多轮会话：任务在后台线程执行，事件供前端轮询，并支持中途停止。

Streamlit 的脚本执行期间不响应任何交互，所以"实时展示思考过程 + 随时停止"
必须把任务放到独立线程执行，前端只负责轮询事件队列。

停止的语义是"取消"，不是"撤销"：已经产生的副作用（例如部署成功的站点）
不会回滚，因此取消时会检查并提示。
"""
import asyncio
import threading
import uuid
from dataclasses import dataclass, field

from app.main import run_task
from app.messages import cancel_note, reply_text

TERMINAL_STATUS = {'deployed', 'deploy_needs_check', 'local_ready', 'failed'}


@dataclass
class Session:
    """一次多轮对话的会话状态。"""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    messages: list = field(default_factory=list)   # [{'role', 'content'}]
    status: str = 'idle'                           # idle / running / 终态
    task_id: str | None = None
    result: dict | None = None
    error: str | None = None
    _events: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _task: asyncio.Task | None = None

    # ---------- 对外接口 ----------

    @property
    def running(self) -> bool:
        return self.status == 'running'

    def start(self, user_input: str) -> None:
        if self.running:
            raise RuntimeError('当前任务还在执行，请先停止')
        self.messages.append({'role': 'user', 'content': user_input})
        self.status = 'running'
        self.task_id = None
        self.result = None
        self.error = None
        with self._lock:
            self._events = [{'type': 'status', 'status': 'running', 'detail': '已开始'}]
        self._thread = threading.Thread(
            target=self._run, args=(user_input,),
            name=f'session-{self.session_id}', daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        """请求停止。取消在下一个 await 点生效，最坏要等当前 HTTP 调用收尾。"""
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)

    def drain(self) -> list:
        """取走尚未消费的事件；前端每次刷新调用一次。"""
        with self._lock:
            out, self._events = self._events, []
        return out

    # ---------- 内部实现 ----------

    def _push(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)

    def _set_task_id(self, task_id: str) -> None:
        self.task_id = task_id

    def _run(self, user_input: str) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            self._task = loop.create_task(self._main(user_input))
            try:
                loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                pass          # 用户主动停止，状态已在 _main 中记录
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._loop = None
                self._task = None

    async def _main(self, user_input: str) -> None:
        history = list(self.messages[:-1])
        try:
            state = await run_task(
                user_input,
                on_event=self._push,
                on_start=self._set_task_id,
                history=history,
            )
        except asyncio.CancelledError:
            self.status = 'cancelled'
            self._push({'type': 'status', 'status': 'cancelled',
                        'detail': cancel_note(self.task_id)})
            raise
        except Exception as exc:
            self.status = 'failed'
            self.error = f'{type(exc).__name__}: {exc}'
            self._push({'type': 'status', 'status': 'failed', 'detail': self.error})
            return
        self.result = state
        self.status = state.get('status', 'failed')
        self.messages.append({'role': 'assistant', 'content': reply_text(state)})
        self._push({'type': 'status', 'status': self.status,
                    'detail': reply_text(state)})
