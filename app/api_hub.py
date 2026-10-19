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
from app.events import action, stage_end, stage_start
from app.main import run_task
from app.messages import cancel_note, reply_text
from app import ledger as ledger_module
from app import memory as memory_module
from app.services import is_stop_command
from app.store import Store, now

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
    run_id: str = ''                    # 本次执行尝试的 ID（账本、续跑都挂在它上面）
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

    def list_sessions(self, limit: int = 50) -> list[dict]:
        return self.store.list_sessions(limit)

    def session_timeline(self, session_id: str) -> dict:
        """一个会话的完整回放：聊天气泡 + 每轮的执行过程事件。

        前端切换会话时用它重建"执行步骤"，否则那些步骤会随页面状态一起丢掉。
        草稿（draft）与逐字增量（answer_delta）本来就不落库，这里自然没有。
        """
        turns = [{
            'task_id': task['task_id'],
            'message': task['message'],
            'status': task['status'],
            'created_at': task['created_at'],
            'events': [event for _, event in self.store.events(task['task_id'])],
        } for task in self.store.session_tasks(session_id)]
        return {'session_id': session_id,
                'messages': self.store.messages(session_id),
                'turns': turns}

    def prior_context(self, session_id: str) -> dict:
        """上一轮可以接过来用的东西：HTML 产物 + 已取得的正文。

        多轮里用户常说"把上面的内容整理成 HTML""部署到服务器"，这一轮自己不检索、
        也不重新渲染，全靠上一轮留下的材料。只接正文来源（不含纯知识作答），
        因为报告要引用来源，没有 URL 的来源没法在报告里成立。
        """
        for row in self.store.tasks_of(session_id, limit=20):
            data = self.store.task(row['task_id'])
            state = (data or {}).get('state') or {}
            if not isinstance(state, dict):
                continue
            artifact = state.get('artifact') or None
            documents = [d for d in (state.get('documents') or [])
                         if d.get('status') == 'success']
            if artifact or documents:
                return {'artifact': artifact, 'documents': documents}
        return {}

    def stop_session(self, session_id: str, message: str) -> TaskRecord:
        """把"暂停/停止"当成控制指令：掐掉在跑的任务，不启动新流程。

        界面上已经没有停止按钮，用户就靠说一句停下。若不拦下来，这句话会被当成
        普通任务送进规划器——想停反而跑起最重的一条链路。
        """
        running = [task for task in self.tasks.values()
                   if task.session_id == session_id and task.live]
        for task in running:
            task._task.cancel()
        self.store.append_message(session_id, 'user', message)
        record = TaskRecord(task_id=uuid.uuid4().hex[:12], session_id=session_id,
                            message=message, store=self.store)
        self.store.create_task(record.task_id, session_id, message)
        self.tasks[record.task_id] = record
        detail = ('已停止当前任务，没有重新开始检索。' if running
                  else '当前没有正在执行的任务。')
        self.store.append_message(session_id, 'assistant', detail)
        record.persist(status='cancelled', state={'status': 'cancelled'})
        record.publish({'type': 'status', 'status': 'cancelled', 'detail': detail})
        record.close()
        return record

    def start(self, message: str, session_id: str | None = None) -> TaskRecord:
        session_id = session_id or uuid.uuid4().hex[:12]
        if is_stop_command(message):
            return self.stop_session(session_id, message)
        history = self.history(session_id)
        prior = self.prior_context(session_id)
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
        # 建 run：task 是持续目标，run 是这一次执行尝试（文档 §3.1）。
        # 账本挂在 run 上，重跑同样的 run_id 才能复用"已成功不重发"。
        record.run_id = uuid.uuid4().hex[:12]
        self.store.ensure_task_meta(record.task_id, session_id=session_id,
                                    goal=message)
        self.store.create_run(record.run_id, record.task_id, status='running')
        record._task = asyncio.create_task(self._run(record, history, prior))
        return record

    async def _run(self, record: TaskRecord, history: list,
                   prior: dict | None = None,
                   resume_plan: dict | None = None) -> None:
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

        # 这些阶段一出现，说明"规划已完成、开始真正干活"，可结算指令耗时。
        FIRST_WORK_STAGES = {'web_search', 'fetch_pages', 'answer_directly',
                             'compose_report', 'render_html', 'deploy_site'}

        def relay(event: dict) -> None:
            if (event.get('type') == 'stage_start'
                    and event.get('stage') in FIRST_WORK_STAGES):
                close_instruction('规划完成')
            track(event)
            record.publish(event)

        # 先压缩更早的对话：成功则只把"未摘要"的部分当历史，避免摘要与原文重复。
        memory = {}
        try:
            memory = await memory_module.compact(self.store, record.session_id)
            if memory.get('compacted'):
                record.publish(action(
                    '已压缩上下文',
                    f'合并 {memory["folded"]} 条较早对话为摘要', '📦'))
                history = self.store.messages_after(
                    record.session_id, memory.get('upto') or 0)
                history = [{'role': m['role'], 'content': m['content']}
                           for m in history]
        except Exception:
            memory = {}

        book = ledger_module.ActionLedger(self.store, record.run_id, 1)
        try:
            state = await run_task(
                record.message, on_event=relay,
                on_start=on_start, history=history,
                prior=prior, memory=memory,
                ledger=book, run_id=record.run_id,
                resume_plan=resume_plan,
            )
        except asyncio.CancelledError:
            close_instruction('已中断')
            close_open_stage('已中断')
            detail = cancel_note(record.artifact_id)
            record.persist(status='cancelled')
            self.store.update_run(record.run_id, status='cancelled',
                                  cancel_requested_at=now())
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
            self.store.update_run(record.run_id, status='failed',
                                  error=record.error)
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
            self._finish_run(record, state, status)
            record.publish({'type': 'status', 'status': status, 'detail': reply})
        finally:
            # 无论成功、失败还是被取消，都要唤醒并释放所有订阅者。
            record.close()

    # ---------- run 生命周期（文档 §7、§8.4） ----------

    def _finish_run(self, record: TaskRecord, state: dict, status: str) -> None:
        """把 run 收尾，并把计划与步骤落库，供续跑与界面查询。"""
        run_status = {'answered': 'succeeded', 'local_ready': 'succeeded',
                      'deployed': 'succeeded', 'failed': 'failed',
                      'cancelled': 'cancelled'}.get(status, status)
        plan = state.get('plan') or {}
        steps = plan.get('steps') or []
        if steps:
            self.store.save_plan(record.run_id, 1, goal=record.message,
                                 steps=steps, reason=plan.get('summary') or '')
            self.store.save_steps(record.run_id, 1, steps)
            outputs = state.get('step_outputs') or {}
            for step in steps:
                step_id = step.get('step_id')
                if not step_id:
                    continue
                status = (outputs.get(step_id) or {}).get('__status__')
                if status:
                    self.store.update_step(record.run_id, 1, step_id,
                                           status=status)
        artifact = state.get('artifact') or {}
        if artifact.get('html_path'):
            # 产物登记：把文件落成可授权的对象引用，而不是只有一条路径字符串
            self.store.add_artifact(
                artifact.get('artifact_id') or f'art_{record.run_id}',
                run_id=record.run_id,
                kind=artifact.get('kind') or 'report',
                storage_key=artifact['html_path'],
                mime='text/html',
                size=int(artifact.get('size_bytes') or 0),
                sha256=artifact.get('sha256') or '')
        self.store.update_run(record.run_id, status=run_status,
                              plan_version=1 if steps else 0)
        # outbox：业务事件与派生作业在同一处写入，消费者按 dedupe_key 幂等
        self.store.enqueue_job(
            uuid.uuid4().hex[:12], 'session_summary',
            f"{record.session_id}:{record.run_id}",
            {'session_id': record.session_id})

    def pause(self, run_id: str) -> bool:
        """请求暂停：只停止派发新动作，在途动作尽量收结果（文档 §8.4）。"""
        run = self.store.run(run_id)
        if run is None or run['status'] not in {'running', 'queued'}:
            return False
        self.store.update_run(run_id, pause_requested=1)
        return True

    def resume(self, run_id: str) -> TaskRecord | None:
        """续跑：沿用已存计划重跑，账本会把已成功的步骤直接复用。"""
        run = self.store.run(run_id)
        if run is None:
            return None
        record = self.tasks.get(run['task_id'])
        if record is None:
            return None
        if record.live:
            return record
        plan = self.store.plan(run_id) or {}
        steps = plan.get('steps') or []
        if not steps:
            return None
        self.store.update_run(run_id, status='running', pause_requested=0)
        record.run_id = run_id
        record.status = 'running'
        record._task = asyncio.create_task(
            self._run(record, [], {}, resume_plan={'steps': steps}))
        return record

    def reconcile(self) -> dict:
        """启动时对账遗留运行，返回统计（文档 §8.3）。"""
        return self.store.reconcile_orphan_runs()

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
