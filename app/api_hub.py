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
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.events import action, stage_end, stage_start
from app.main import run_task
from app.messages import cancel_note, reply_text
from app import execution_control
from app import ledger as ledger_module
from app import memory as memory_module
from app import outcome as outcome_module
from app import policies
from app import run_inputs as run_inputs_module
from app import profile as profile_module
from app.capabilities import publishing as publishing_capability
from app.services import is_stop_command
from app.persistence import open_store
from app.pg_store import PostgresStore, now

logger = logging.getLogger(__name__)

# 状态机词汇只有一套，在 app/outcome.py 里定义：run/task 层只存通用状态，
# 界面看的 `answered` / `local_ready` / `deployed` 是**派生**出来的展示标签。
# 以前这里有 TERMINAL_STATUS + RUN_STATUS_OF 两张手工表，收尾时
# `RUN_STATUS_OF.get(status, status)` 兜底会把手写业务词静默写进 runs.status。
TERMINAL_STATUS = outcome_module.TERMINAL_RUN_STATUSES

# 合成阶段：衡量"用户发出指令"到"真正开始搜索"之间的空档。
INSTRUCTION_STAGE = 'instruction'

# 哨兵：通知订阅者流已结束
CLOSE = object()


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
    latency: float | None = None        # 收到指令 → 开始搜索 的耗时
    open_stage: dict | None = None      # 当前未关闭的阶段 {'stage','at'}
    # 谁在执行这个 run：持有租约的那一方。就地执行（API 进程）也必须写上，
    # 否则 Worker 会把"正在跑的 run"当孤儿再领一遍。
    lease_owner: str | None = None
    # 本次领取的租约代次：所有推进都带它去库里做条件更新（fenced）。
    # 内存里只用于展示与排障，判断依据永远是数据库。
    lease_epoch: int = 0
    tenant_id: str = 'default'
    owner_user_id: str = 'default'
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

        例外是 draft（半成品报告，每份是整份快照）与 answer_delta（增量文本，
        一次可能上千片）：它们体积大或条数多，落库会让 events 表膨胀，
        因此只做实时投递。序号传 None，SSE 端的去重逻辑会跳过它们。
        """
        if event.get('type') in ('draft', 'answer_delta'):
            for queue in list(self._subscribers):
                queue.put_nowait((None, event))
            return
        seq = await self.store.append_event(self.task_id, event)
        for queue in list(self._subscribers):
            queue.put_nowait((seq, event))

    async def subscribe(self):
        """返回 (队列, 历史快照)。

        注册订阅与读取快照之间可能插入新事件（两者之间无法做到原子），
        因此投递给订阅者的是 `(seq, event)`，由消费端按 seq 去重（见 app/api.py）。
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
        """更新内存状态并写回库。"""
        for key, value in fields.items():
            setattr(self, key, value)
        await self.store.update_task(self.task_id, **fields)

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


def _resume_index(steps: list[dict], actions: list[dict]) -> int:
    """按已落库的动作推断"下一步从哪开始"。

    只看 `succeeded` 的动作：`in_flight`/`unknown` 不能算完成（要核实），
    `prepared` 可以重新准入。返回第一个还需要执行的步骤下标。
    """
    done = {str(item.get('step_id') or '') for item in actions
            if str(item.get('status')) == 'succeeded'}
    for index, step in enumerate(steps):
        if str(step.get('step_id') or '') not in done:
            return index
    return len(steps)


def _artifact_view(state: dict) -> dict | None:
    """把 state 里的产物压成前端够用的形状（不存在就 None）。"""
    artifact = (state or {}).get('artifact') or {}
    path = artifact.get('path') or artifact.get('html_path')
    if not path:
        return None
    return {'kind': artifact.get('kind') or 'report',
            'filename': Path(path).name,
            'size_bytes': artifact.get('size_bytes'),
            'exists': Path(path).exists()}


class Hub:
    """会话与任务注册表。持久部分在 SQLite，实时部分是进程内的订阅队列。"""

    # 内存里最多留多少个任务记录（查过的历史任务不必永久驻留）
    MAX_CACHED_TASKS = 200

    def __init__(self, store: PostgresStore | None = None, *,
                 owner_id: str | None = None,
                 lease_seconds: float | None = None) -> None:
        # 默认按配置挑实现：DATABASE_URL 有值就是 PostgreSQL
        self.store = store if store is not None else open_store()
        self.tasks: dict[str, TaskRecord] = {}
        # 执行者身份写进 runs.lease_owner。带上进程号与随机后缀：
        # 同一台机器上起多个副本时也不能互相认成"自己"。
        self.owner_id = owner_id or f'api:{time.time_ns():x}:{uuid.uuid4().hex[:6]}'
        self.lease_seconds = lease_seconds or settings.run_lease_seconds

    async def _assert_session_owner(self, session_id: str, tenant_id: str,
                                    user_id: str) -> None:
        """会话归属校验：不存在或不属于当前主体 → 一律 404（不泄漏存在性）。"""
        row = await self.store.session(session_id)
        if row is None:
            return                      # 全新会话 ID：允许（稍后创建时写入主人）
        owner = row.get('owner_user_id') or 'default'
        tenant = row.get('tenant_id') or 'default'
        if tenant != tenant_id or owner != user_id:
            raise PermissionError(f'会话 {session_id} 不属于当前主体')

    async def history(self, session_id: str) -> list:
        return await self.store.messages(session_id)

    async def list_sessions(self, limit: int = 50, principal=None) -> list[dict]:
        if principal is None:
            return await self.store.list_sessions(limit)
        return await self.store.list_sessions(
            limit, tenant_id=principal.tenant_id, owner_user_id=principal.user_id)

    async def session_timeline(self, session_id: str) -> dict:
        """一个会话的完整回放：聊天气泡 + 每轮的执行过程事件。

        前端切换会话时用它重建"执行步骤"，否则那些步骤会随页面状态一起丢掉。
        草稿（draft）与逐字增量（answer_delta）本来就不落库，这里自然没有。
        """
        turns = []
        live_task_id = ''
        for task in await self.store.session_tasks(session_id):
            state = task.get('state') or {}
            run_status = outcome_module.normalize_status(task['status'])
            record = self.tasks.get(task['task_id'])
            live = bool(record is not None and record.live) or run_status in \
                outcome_module.ACTIVE_RUN_STATUSES
            if live and not live_task_id:
                live_task_id = task['task_id']
            meta = await self.store.task_meta(task['task_id']) or {}
            run_id = str(meta.get('current_run_id') or '')
            turns.append({
                'task_id': task['task_id'],
                # 切回历史会话时，前端要靠 run_id 才能给**正在跑的那一轮**补要求
                'run_id': run_id,
                'message': task['message'],
                # 库里存的是通用状态，回放时按 outcome 派生展示标签
                'status': outcome_module.label(run_status, state),
                'run_status': run_status,
                'created_at': task['created_at'],
                # 产物要能"切回来接着看"：视频/页面路径在这里给前端，
                # 否则一切会话就再也找不到入口（只有最后一条 finish 时才渲染过）
                'artifact': _artifact_view(state),
                'live': live,
                'events': [event for _, event in await self.store.events(task['task_id'])],
            })
        live_run_id = ''
        if live_task_id:
            meta = await self.store.task_meta(live_task_id) or {}
            live_run_id = str(meta.get('current_run_id') or '')
        return {'session_id': session_id,
                'messages': await self.store.messages(session_id),
                # 这个会话里还在跑的任务：前端切回来时要重新接上事件流
                'live_task_id': live_task_id,
                'live_run_id': live_run_id,
                'turns': turns}

    async def prior_context(self, session_id: str) -> dict:
        """上一轮可以接过来用的东西：HTML 产物 + 已取得的正文。

        多轮里用户常说"把上面的内容整理成 HTML""部署到服务器"，这一轮自己不检索、
        也不重新渲染，全靠上一轮留下的材料。只接正文来源（不含纯知识作答），
        因为报告要引用来源，没有 URL 的来源没法在报告里成立。

        取的是**最近一轮**有产物的任务（DESC 的第一条），并把来源任务 ID 一并
        给出：以前这里只返回一个 artifact，"刚才那份"到底指哪一份全靠猜，
        界面上也没法解释。多份候选时调用方可以据此提示用户。
        """
        candidates = []
        for row in await self.store.tasks_of(session_id, limit=20):
            data = await self.store.task(row['task_id'])
            state = (data or {}).get('state') or {}
            if not isinstance(state, dict):
                continue
            artifact = state.get('artifact') or None
            documents = [d for d in (state.get('documents') or [])
                         if d.get('status') == 'success']
            if artifact or documents:
                candidates.append({'task_id': row['task_id'],
                                   'artifact': artifact, 'documents': documents,
                                   'created_at': row.get('created_at')})
        if not candidates:
            return {}
        picked = candidates[0]
        return {'artifact': picked['artifact'], 'documents': picked['documents'],
                'from_task': picked['task_id'],
                'candidates': len(candidates)}

    async def stop_session(self, session_id: str, message: str,
                     principal=None) -> TaskRecord:
        """把"暂停/停止"当成控制指令：掐掉在跑的任务，不启动新流程。

        界面上已经没有停止按钮，用户就靠说一句停下。若不拦下来，这句话会被当成
        普通任务送进规划器——想停反而跑起最重的一条链路。
        """
        tenant = getattr(principal, 'tenant_id', 'default')
        owner = getattr(principal, 'user_id', 'default')
        running = [task for task in self.tasks.values()
                   if task.session_id == session_id and task.live]
        for task in running:
            task._task.cancel()
        await self.store.append_message(session_id, 'user', message,
                                  tenant_id=tenant, owner_user_id=owner)
        record = TaskRecord(task_id=uuid.uuid4().hex[:12], session_id=session_id,
                            message=message, store=self.store,
                            tenant_id=tenant, owner_user_id=owner)
        await self.store.create_task(record.task_id, session_id, message)
        self.tasks[record.task_id] = record
        detail = ('已停止当前任务，没有重新开始检索。' if running
                  else '当前没有正在执行的任务。')
        await self.store.append_message(session_id, 'assistant', detail,
                                  tenant_id=tenant, owner_user_id=owner)
        await record.persist(status='cancelled', state={'status': 'cancelled'})
        await record.publish({'type': 'status', 'status': 'cancelled', 'detail': detail})
        record.close()
        return record

    async def start(self, message: str, session_id: str | None = None,
              principal=None) -> TaskRecord:
        """建任务 + 建 run + **领取** + 就地执行。

        关键在"领取"这一步：API 进程要跑就地执行，也必须先按同一条 claim 路径
        占住租约。以前这里是 `create_run(status='running')` 却从不写租约，于是
        Worker 的领取条件（queued/running 且租约为空或过期）完全命中——同时起
        API 和 Worker，同一个任务就会跑两遍，部署类写操作真的会建出第二个站点。
        """
        session_id = session_id or uuid.uuid4().hex[:12]
        tenant = getattr(principal, 'tenant_id', 'default')
        owner = getattr(principal, 'user_id', 'default')
        # 传入已有 session_id 时**先验归属**再读历史：否则只要猜到 12 位 hex 就能
        # 把消息追加进别人的会话，并把对方的历史当上下文读出来。
        if session_id is not None:
            await self._assert_session_owner(session_id, tenant, owner)
        if is_stop_command(message):
            return await self.stop_session(session_id, message, principal)
        history = await self.history(session_id)
        prior = await self.prior_context(session_id)
        await self.store.append_message(session_id, 'user', message,
                                  tenant_id=tenant, owner_user_id=owner)
        record = TaskRecord(task_id=uuid.uuid4().hex[:12], session_id=session_id,
                            message=message, store=self.store,
                            tenant_id=tenant, owner_user_id=owner)
        await self.store.create_task(record.task_id, session_id, message)
        self.tasks[record.task_id] = record
        # 先占位一条"收到指令"的开始事件，让它排在阶段表第一行；
        # 真正进入检索阶段时再补齐耗时。
        event = stage_start(INSTRUCTION_STAGE)
        record.open_stage = {'stage': INSTRUCTION_STAGE, 'at': event['at']}
        await record.publish(event)
        # 建 run：task 是持续目标，run 是这一次执行尝试（文档 §3.1）。
        # 账本挂在 run 上，重跑同样的 run_id 才能复用"已成功不重发"。
        record.run_id = uuid.uuid4().hex[:12]
        await self.store.ensure_task_meta(record.task_id, session_id=session_id,
                                    goal=message, tenant_id=tenant,
                                    owner_user_id=owner)
        await self.store.create_run(record.run_id, record.task_id, status='queued',
                              tenant_id=tenant)
        claimed = await self.store.claim_run(self.owner_id, self.lease_seconds,
                                       run_id=record.run_id)
        if claimed is None:
            # 理论上不可能（刚刚才建出来的行）。真发生了就留在队列里，
            # 让 Worker 接手，绝不在没有租约的情况下开始执行。
            logger.warning('run %s 领取失败，已留在队列等待 Worker', record.run_id)
            record.status = 'queued'
            return record
        record.lease_owner = claimed.get('lease_owner')
        record.lease_epoch = int(claimed.get('lease_epoch') or 0)
        # 就地执行也必须走**同一个**执行入口：上下文、账本、恢复判定都不另写一份
        loop = asyncio.get_running_loop()
        record._task = loop.create_task(self.execute_claimed_run(claimed))
        return record

    async def execute_claimed_run(self, claimed: dict) -> TaskRecord | None:
        """**唯一的执行入口**：拿到已领取的 run 之后，从建记录到执行全在这里。

        以前 start / resume / Worker 三处各自拼装上下文、身份和计划，细节一旦
        分叉就出错（Worker 那份连 await 都漏了）。现在只有一个入口：

        1. 按领取结果构造/校验记录（owner + **epoch** 都记下来）；
        2. 读取历史、上轮产物、会话记忆；
        3. 有已提交计划就沿用它（不重新规划），没有才允许规划；
        4. 建立带执行上下文的账本，交给图执行。
        """
        run = claimed
        run_id = run['run_id']
        task_id = run['task_id']
        epoch = int(run.get('lease_epoch') or 0)
        owner = str(run.get('lease_owner') or '')

        task = await self.store.task(task_id)
        if task is None:
            await self.store.update_run(run_id, status='failed', error='任务不存在')
            return None

        record = self.tasks.get(task_id)
        if record is not None and record.live and record.run_id != run_id:
            # 本进程里有另一条 run 正在跑同一个任务：不能拿它充当本 run 的执行者
            logger.warning('任务 %s 已有运行中的 run（%s），本次领取 %s 交给新执行者',
                           task_id, record.run_id, run_id)
            record = None
        if record is None or record.run_id != run_id:
            record = await self.get(task_id)
            if record is None:
                await self.store.update_run(run_id, status='failed',
                                            error='无任务记录')
                return None

        record.run_id = run_id
        record.status = 'running'
        record.lease_owner = owner
        record.lease_epoch = epoch
        record._lease_lost = False

        context = execution_control.ExecutionContext(
            run_id=run_id, lease_owner=owner, lease_epoch=epoch)

        # 计划：有就恢复，没有才规划。**空计划不等于"没有计划"**——
        # `{'steps': []} or None` 会得到一个非空字典，正是旧代码的坑。
        plan = await self.store.plan(run_id) or {}
        steps = plan.get('steps') or []
        actions = await self.store.actions_of(run_id)
        if steps:
            plan_version = int(plan.get('version') or 1)
            context = context.with_plan_version(plan_version)
            resume_plan = {'steps': steps, 'summary': plan.get('goal') or '续跑',
                           'plan_version': plan_version,
                           'step_index': _resume_index(steps, actions)}
            if actions:
                # 落库 + 投递都要 await：这里本来就在协程里，"同步投递"只会
                # 得到一个没被 await 的协程（事件静默丢失）。
                await record.publish(action(
                    '沿用已提交的计划',
                    f'{len(steps)} 步 · 第 {resume_plan["step_index"] + 1} 步继续',
                    '↩️'))
        elif actions:
            # 有动作却没有计划：这不该发生。**不能拿新规划冒充恢复**——那会绕开
            # 原有动作身份，可能重复副作用。
            logger.error('run %s 有 %s 个动作但没有计划，转待核实',
                         run_id, len(actions))
            await self.store.update_run(run_id, status='needs_reconcile',
                                        error='有动作但缺失计划，需人工核实')
            record.status = 'needs_reconcile'
            await record.persist(status='needs_reconcile', state=record.state)
            return record
        else:
            resume_plan = None

        history = await self.history(record.session_id)
        prior = await self.prior_context(record.session_id)
        memory = await self.store.memory(record.session_id)

        # 让 live/done 这些判断仍然有效：当前协程就是这次执行
        record._task = asyncio.current_task()

        # 所有步骤都已结算、但 run 没来得及收尾（进程在"最后一次结算"与
        # "写终态"之间消失）：这里只剩收尾，**不能**再跑一遍图——图拿不到
        # 上一次的 step_outputs，只会得出"什么都没有产出"（文档 §5.4）。
        if resume_plan and resume_plan['step_index'] >= len(resume_plan['steps']):
            snapshot = await self._recovery_state(run_id)
            if not snapshot.get('step_outputs'):
                # 没有快照（旧数据，或崩溃在"结算已提交但快照没写全"）：
                # 用动作回执重建——已成功的动作本来就把结果留在 receipt 里。
                snapshot = self._state_from_actions(resume_plan['steps'], actions)
            if snapshot.get('step_outputs'):
                return await self._finish_recovered(record, snapshot, context)
        try:
            await self._run(record, history, prior, resume_plan=resume_plan,
                            memory=memory, context=context)
        except execution_control.LeaseLost as exc:
            logger.warning('run %s 执行权丢失：%s', run_id, exc)
        return record

    @staticmethod
    def _state_from_actions(steps: list[dict], actions: list[dict]) -> dict:
        """用动作回执重建恢复所需的 state（没有快照时的兜底）。

        只认 `succeeded` 的动作：在途/未知的动作不能当结果用。回执里的标量提到
        顶层，与 `run_step` 写入 step_outputs 的规则保持一致——否则派生终态会
        看不到"这一步产出了什么"。
        """
        outputs: dict = {}
        state: dict = {'plan': {'steps': steps}}
        for act in actions:
            if str(act.get('status')) != 'succeeded':
                continue
            step_id = str(act.get('step_id') or '')
            receipt = act.get('receipt') or {}
            if not isinstance(receipt, dict):
                receipt = {}
            outputs[step_id] = {'__status__': 'succeeded', **receipt}
            for key, value in receipt.items():
                if isinstance(value, (str, int, float, bool, dict, list)):
                    state.setdefault(key, value)
        if outputs:
            state['step_outputs'] = outputs
            state['step_index'] = len(steps)
        return state

    async def _finish_recovered(self, record: TaskRecord, state: dict,
                                context) -> TaskRecord:
        """只用已提交的快照收尾（所有步骤都已结算的情形）。

        **不重新执行、不重新规划**：结果已经从库里读回来了，再跑一遍就是重复
        副作用。这里要做的只是把终态、回复与订阅者补齐，然后交还租约。
        """
        state = dict(state or {})
        status = outcome_module.check_run_status(outcome_module.final_status(state))
        state['status'] = status
        state['outcome'] = outcome_module.build(state)
        record.state = state
        record.status = status
        reply = reply_text(state)
        try:
            await record.persist(status=status, state=state)
            await self.store.append_message(record.session_id, 'assistant', reply)
            await self._finish_run(record, state, status)
            await record.publish({'type': 'status', **outcome_module.view(status, state),
                                  'detail': reply})
            logger.info('run %s 按快照收尾（步骤已全部结算）status=%s',
                        record.run_id, status)
        finally:
            if record.lease_owner and record.run_id:
                try:
                    await self.store.release_lease(
                        record.run_id, record.lease_owner,
                        epoch=getattr(context, 'lease_epoch', None) or None)
                except Exception:                  # noqa: BLE001
                    logger.warning('释放租约失败 run=%s', record.run_id,
                                   exc_info=True)
            record.close()
        return record

    async def _guard(self, record: TaskRecord) -> None:
        """执行期间的看守协程：续租 + 跨进程取消。

        两件事都必须在**执行者这一侧**做：

        - **续租**：一个任务可能跑上百秒，租约只有几十秒。不续租，租约一过期
          另一个 Worker 就会认定执行者死了并接手同一份计划——重复副作用。
        - **取消**：取消请求可能落在另一个进程（API 收到，Worker 在跑），
          asyncio.Task 没法跨进程 cancel，只能靠库里的标记传话。
        """
        interval = max(1.0, min(2.0, self.lease_seconds / 4))
        renew_every = max(interval, self.lease_seconds / 3)
        last_renew = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            try:
                if time.monotonic() - last_renew >= renew_every:
                    # 必须带 epoch：旧执行者不能给"已被接手"的 run 续租，
                    # 那等于自己又发了一张执行许可（文档 §4.3）。
                    renewed = await self.store.renew_lease(
                        record.run_id, record.lease_owner, self.lease_seconds,
                        epoch=record.lease_epoch or None)
                    last_renew = time.monotonic()
                    if not renewed:
                        logger.warning('run %s 续租失败，停止推进', record.run_id)
                        record._lease_lost = True
                        if record._task is not None:
                            record._task.cancel()
                        return
                run = await self.store.run(record.run_id)
                if run and run.get('cancel_requested_at'):
                    logger.info('run %s 收到取消请求，停止执行', record.run_id)
                    if record._task is not None:
                        record._task.cancel()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:                      # noqa: BLE001
                # 看守协程自己不能把任务带崩：一次续租失败下一轮还会再来
                logger.warning('续租/取消轮询失败 run=%s', record.run_id,
                               exc_info=True)

    async def _run(self, record: TaskRecord, history: list,
                   prior: dict | None = None,
                   resume_plan: dict | None = None,
                   memory: dict | None = None,
                   context: 'execution_control.ExecutionContext | None' = None) -> None:
        """执行一次运行。`context` 是本次领取的执行权凭证（owner + epoch）。"""
        # 没有上下文时（兼容旧调用点）用记录上的字段兜一个
        if context is None and record.lease_owner:
            context = execution_control.ExecutionContext(
                run_id=record.run_id, lease_owner=record.lease_owner,
                lease_epoch=record.lease_epoch)
        guard = (asyncio.create_task(self._guard(record))
                 if record.lease_owner else None)
        # 把画像读写限定到这次任务的主体上：多用户时别人不该被写进我的画像
        profile_token = profile_module.use(record.owner_user_id)
        async def on_start(artifact_id: str) -> None:
            await record.persist(artifact_id=artifact_id)
            await record.publish({'type': 'task', 'task_id': record.task_id,
                            'artifact_id': artifact_id})

        async def close_instruction(status: str) -> None:
            """补齐"收到指令"的耗时。只补一次；中断/失败也要补，否则那一行会一直停在"进行中"。"""
            if record.latency is not None:
                return
            record.latency = time.time() - record.started_at
            if record.open_stage and record.open_stage['stage'] == INSTRUCTION_STAGE:
                # 必须清掉，否则 close_open_stage 会把它再关一次
                record.open_stage = None
            await record.publish(stage_end(INSTRUCTION_STAGE, record.latency, status))

        def track(event: dict) -> None:
            kind = event.get('type')
            if kind == 'stage_start':
                record.open_stage = {'stage': event.get('stage'),
                                     'at': event.get('at', time.time())}
            elif kind == 'stage_end' and record.open_stage \
                    and record.open_stage['stage'] == event.get('stage'):
                record.open_stage = None

        async def close_open_stage(status: str) -> None:
            """兜底关闭仍未结束的阶段。

            任务被取消时，节点自己发的 stage_end 会丢：它写进 LangGraph 的流队列，
            而 run_task 的消费循环此刻已经被取消，没人来取。因此必须由中枢直接补发，
            否则前端那一行会永远停在"进行中"、耗时一路涨下去。
            """
            opened = record.open_stage
            if not opened:
                return
            record.open_stage = None
            await record.publish(stage_end(opened['stage'],
                                     time.time() - opened['at'], status))

        # 这些阶段一出现，说明"规划已完成、开始真正干活"，可结算指令耗时。
        FIRST_WORK_STAGES = {'web_search', 'fetch_pages', 'render_sources_html',
                             publishing_capability.TOOL_NAME}

        async def relay(event: dict) -> None:
            if (event.get('type') == 'stage_start'
                    and event.get('stage') in FIRST_WORK_STAGES):
                await close_instruction('规划完成')
            track(event)
            await record.publish(event)

        # 先压缩更早的对话：成功则只把"未摘要"的部分当历史，避免摘要与原文重复。
        # 续跑时直接沿用库里已有的记忆，没必要再压一遍（那是一次模型调用）。
        if memory is None:
            memory = {}
            try:
                memory = await memory_module.compact(self.store, record.session_id)
                if memory.get('compacted'):
                    await record.publish(action(
                        '已压缩上下文',
                        f'合并 {memory["folded"]} 条较早对话为摘要', '📦'))
                    history = await self.store.messages_after(
                        record.session_id, memory.get('upto') or 0)
                    history = [{'role': m['role'], 'content': m['content']}
                               for m in history]
            except Exception:
                memory = {}

        plan_version = int((resume_plan or {}).get('plan_version') or 1)
        book = ledger_module.ActionLedger(self.store, record.run_id, plan_version,
                                          context=context)
        context_token = execution_control.bind(context)

        async def execute_round(plan_hint: dict | None) -> dict:
            """跑一轮图。要求版本变化时会被调用多次（**不是**重试同一个计划）。"""
            return await run_task(
                record.message, on_event=relay,
                on_start=on_start, history=history,
                prior=prior, memory=memory,
                ledger=book, run_id=record.run_id,
                resume_plan=plan_hint,
            )

        try:
            state = None
            # 为什么这里有个循环：收尾前用户可能又补了要求，`_finish_run` 的
            # 版本 CAS 会抛 IntentChanged。此时**不能**把 run 标成结束（否则那条
            # 补充就永远没人处理），要回到消费循环重新修订（文档 §5 第十一步）。
            max_rounds = max(1, int(settings.run_max_revisions or 1)) + 1
            for round_index in range(max_rounds):
                if round_index:
                    # 新一轮要读数据库里最新的计划与补充，而不是拿着旧的提示
                    resume_plan = await self._resume_plan(record.run_id)
                state = await execute_round(resume_plan)
                status_now = outcome_module.check_run_status(
                    state.get('status') or 'failed')
                try:
                    await self._settle_round(
                        record, state, status_now, context=context,
                        close_instruction=close_instruction,
                        close_open_stage=close_open_stage)
                    break
                except execution_control.IntentChanged as exc:
                    logger.info('run %s 收尾时发现新要求（%s → %s），回到消费循环',
                                record.run_id, exc.expected, exc.actual)
                    await record.publish(action(
                        '收到新的要求', '按最新要求调整后继续', '📝'))
                    continue
        except execution_control.LeaseLost as exc:
            # 失租：**不写任何业务状态**。这一次执行已经没有资格推进 run，
            # 由当前持租约的执行者或对账流程收尾。
            record._lease_lost = True
            logger.warning('run %s 失去执行权：%s', record.run_id, exc)
            await close_open_stage('执行权已转移')
            await record.publish({'type': 'action', 'icon': '⛔',
                                  'label': '执行权已转移',
                                  'detail': '另一个执行者接手了本次运行，本进程停止推进'})
            return
        except execution_control.ControlStopped as exc:
            # 在安全边界停下。**停下来的原因决定状态**：用户取消写 cancelled，
            # 暂停请求写 paused——把取消记成暂停会让它看起来"可以继续"。
            stopped = getattr(exc, 'status', '') or 'paused'
            label = '已取消' if stopped == 'cancelled' else '已暂停'
            await close_instruction(label)
            await close_open_stage(label)
            # 暂停/取消的状态必须以**已提交的快照**为底：内存里的 record.state
            # 只在成功收尾时被赋值，拿它写快照会得到一份几乎空的 state，
            # 续跑时就等于什么都想不起来（文档 §5.3）。
            state = await self._recovery_state(record.run_id)
            state['status'] = stopped
            if stopped == 'paused':
                state['failure'] = ''
            next_step_id = self._next_step_id(state)
            if stopped == 'cancelled':
                record.status = 'cancelled'
                await record.persist(status='cancelled', state=state)
                await self.store.update_run(record.run_id, status='cancelled',
                                            cancel_requested_at=now())
                await record.publish({'type': 'status', 'status': 'cancelled',
                                      'run_status': 'cancelled',
                                      'detail': '已按请求停止'})
                return
            paused = await self.store.pause_at_boundary(
                context, run_id=record.run_id, plan_version=plan_version,
                state=state, next_step_id=next_step_id)
            record.status = 'paused' if paused else record.status
            if paused:
                await record.persist(status='paused', state=state)
                await record.publish({'type': 'status', 'status': 'paused',
                                      'run_status': 'paused',
                                      'detail': '已暂停，可随时继续'})
            return
        except asyncio.CancelledError:
            await close_instruction('已中断')
            await close_open_stage('已中断')
            if record._lease_lost:
                # 失租导致的取消：**不能写任何业务状态**。这条 run 已经不归本进程，
                # 写 cancelled 会盖掉新执行者刚推进的状态。
                logger.warning('run %s 因失租中断，不做业务收尾', record.run_id)
                raise
            detail = cancel_note(record.artifact_id)
            # 取消**不能**把"可能已经发生的写操作"抹掉：有在途/未知动作时保留
            # 待核实，不能声称远端也停了。
            unknown = await self._has_uncertain_actions(record.run_id)
            record.status = 'needs_reconcile' if unknown else 'cancelled'
            await record.persist(status=record.status)
            await self.store.update_run(record.run_id, status=record.status,
                                  cancel_requested_at=now())
            # 给会话补一条助手回复：否则下一轮上下文里会出现两条连续的用户消息，
            # 模型会看不出上一轮其实是被打断的。
            await self.store.append_message(record.session_id, 'assistant', '（上一轮被用户中断）')
            await record.publish({'type': 'status', 'status': 'cancelled', 'detail': detail})
            raise
        except Exception as exc:
            await close_instruction('已失败')
            await close_open_stage('已失败')
            # 堆栈进服务端日志：状态里只留类型与一句安全文案
            logger.exception('run %s 执行异常', record.run_id)
            record.error = f'{type(exc).__name__}: {exc}'
            record.status = 'failed'
            await self.store.finish_run_owned(context, run_id=record.run_id,
                                             status='failed', error=record.error)
            await record.persist(status='failed', error=record.error)
            await record.publish({'type': 'status', 'status': 'failed',
                            'run_status': 'failed', 'detail': record.error})
        else:
            pass
        finally:
            execution_control.unbind(context_token)
            profile_module.reset(profile_token)
            if guard is not None:
                guard.cancel()
            # 交还租约：跑完还占着租约，等于让这条 run 在过期前无法被续跑。
            # release_lease 带 owner 条件，已被别人接手时不会误清。
            if record.lease_owner and record.run_id:
                try:
                    await self.store.release_lease(record.run_id, record.lease_owner,
                                                   epoch=record.lease_epoch or None)
                except Exception:                  # noqa: BLE001
                    logger.warning('释放租约失败 run=%s', record.run_id,
                                   exc_info=True)
            logger.info('run 结束 run=%s task=%s status=%s',
                        record.run_id, record.task_id, record.status)
            # 无论成功、失败还是被取消，都要唤醒并释放所有订阅者。
            record.close()

    # ---------- run 生命周期（文档 §7、§8.4） ----------

    async def _resume_plan(self, run_id: str) -> dict | None:
        """读库里当前的计划与起点（续跑/修订后重新执行用）。"""
        plan = await self.store.plan(run_id) or {}
        steps = plan.get('steps') or []
        if not steps:
            return None
        actions = await self.store.actions_of(run_id)
        info = await self.store.run_intent(run_id)
        return {'steps': steps, 'summary': plan.get('goal') or '续跑',
                'plan_version': int(plan.get('version') or 1),
                'intent_version': int(plan.get('intent_version')
                                      or info.get('intent_version') or 0),
                'processed_intent_version': int(
                    info.get('processed_intent_version') or 0),
                'step_index': _resume_index(steps, actions)}

    async def _settle_round(self, record: 'TaskRecord', state: dict, status: str, *,
                            context, close_instruction, close_open_stage) -> None:
        """一轮执行结束的收尾：先过门禁，再落记录与事件。

        `_finish_run` 里带版本 CAS：若用户在收尾前补了要求，会抛 `IntentChanged`，
        这里**不写任何东西**（事务在第一句校验就失败了），交给调用方回到消费循环。
        """
        await close_instruction('解析完成')
        await close_open_stage('已结束')
        record.state = state
        reply = reply_text(state)
        if record.latency is not None:
            # 同步写进持久化的 timings，state.json 与 CLI 汇总表同样能看到
            state.setdefault('timings', []).insert(0, {
                'stage': INSTRUCTION_STAGE,
                'seconds': round(record.latency, 2),
                'status': '解析完成，开始搜索',
            })
        # 只有"声称做完了"的状态才必须保证没有未处理的补充；停下等人
        # （waiting_input / failed / paused）允许留着，那正是它们在等的东西。
        await self._finish_run(
            record, state, status, context=context,
            expected_intent_version=state.get('intent_version'),
            require_pending_clear=status in ('succeeded', 'partially_succeeded',
                                             'needs_reconcile'))
        await self.store.append_message(record.session_id, 'assistant', reply)
        # 先落记录（内存 + tasks 行），再发事件：两者都写、职责不同。
        await record.persist(status=status, state=state)
        # 状态事件里 `status` 仍是展示标签（前端认这套），另带 run_status
        await record.publish({'type': 'status', **outcome_module.view(status, state),
                              'detail': reply,
                              'intent_version': int(state.get('intent_version') or 0),
                              'plan_version': int(state.get('plan_version') or 0)})

    async def _recovery_state(self, run_id: str) -> dict:
        """恢复用的最新 state：坏数据/没快照时退回空 dict，绝不因此失败。"""
        try:
            snapshot = await self.store.run_snapshot(run_id)
        except Exception:                          # noqa: BLE001
            logger.warning('读取快照失败 run=%s', run_id, exc_info=True)
            return {}
        return dict((snapshot or {}).get('state') or {})

    @staticmethod
    def _next_step_id(state: dict, next_step_id: str = '') -> str:
        """下一步的 step_id：优先用调用点给的，其次按游标从计划里取。"""
        if next_step_id:
            return next_step_id
        steps = (state.get('plan') or {}).get('steps') or []
        index = int(state.get('step_index') or 0)
        if 0 <= index < len(steps):
            return str(steps[index].get('step_id') or '')
        return ''

    async def _has_uncertain_actions(self, run_id: str) -> bool:
        """有没有 in_flight / unknown 的动作：取消与超时要据此区分"停下了"和"结果未定"。"""
        try:
            actions = await self.store.actions_of(run_id)
        except Exception:                          # noqa: BLE001
            return True
        return any(str(a.get('status')) in {'in_flight', 'unknown'} for a in actions)

    async def _finish_run(self, record: TaskRecord, state: dict, status: str, *,
                          context=None,
                          expected_intent_version: int | None = None,
                          require_pending_clear: bool = True) -> None:
        """把 run 收尾，并把计划与步骤落库，供续跑与界面查询。

        `status` 必须是**通用**状态：run 层不认识 `local_ready` 这类展示词。
        以前这里用 `RUN_STATUS_OF.get(status, status)` 折算，兜底会让新业务词
        静默写进状态机；现在越界会被 `check_run_status` 记 error 并退化。

        `expected_intent_version` 是这次收尾**依据的要求版本**。用户在收尾前又补充
        了要求时，仓储层会抛 `IntentChanged`——这时一个字都不能写，否则那条补充
        就永远没人处理了（文档 §5 第十一步）。
        """
        run_status = outcome_module.check_run_status(status)
        plan = state.get('plan') or {}
        steps = plan.get('steps') or []
        # waiting_input 的 run 不落计划：那份计划没过校验，存下来再续跑
        # 等于把坏计划喂回执行器。要恢复就重新规划。
        if steps and run_status != 'waiting_input':
            # 计划在**执行之前**就提交了（见 execute_claimed_run / main.run_task 的
            # commit_plan）。这里只做兜底：真的没提交过才补写，避免覆盖更早的版本。
            existing = await self.store.plan(record.run_id)
            if not existing:
                await self.store.save_plan(record.run_id, 1, goal=record.message,
                                           steps=steps,
                                           reason=plan.get('summary') or '')
                await self.store.save_steps(record.run_id, 1, steps)
            outputs = state.get('step_outputs') or {}
            for step in steps:
                step_id = step.get('step_id')
                if not step_id:
                    continue
                # 别用 status 这个名字：它会把整个 run 的状态覆盖成某一步的状态
                step_status = (outputs.get(step_id) or {}).get('__status__')
                if step_status:
                    await self.store.update_step(record.run_id, 1, step_id,
                                           status=step_status)
        artifact = state.get('artifact') or {}
        artifact_path = artifact.get('path') or artifact.get('html_path')
        if artifact_path:
            # 产物登记：把文件落成可授权的对象引用，而不是只有一条路径字符串
            await self.store.add_artifact(
                artifact.get('artifact_id') or f'art_{record.run_id}',
                run_id=record.run_id,
                kind=artifact.get('kind') or 'report',
                storage_key=artifact_path,
                mime=artifact.get('mime') or 'text/html',
                size=int(artifact.get('size_bytes') or 0),
                sha256=artifact.get('sha256') or '')
        plan_version = int(state.get('plan_version')
                           or (state.get('plan') or {}).get('version')
                           or (context.plan_version if context else 0)
                           or (1 if steps else 0))
        fields = {'status': run_status,
                  'plan_version': plan_version if steps else 0}
        if (state.get('compensations')
                and (plan.get('kind') or '') != 'compensation'):
            # 撤销是独立动作，必须有独立时间戳：只看 run 状态分不清
            # "失败"和"失败但已收回副作用"。
            # 补偿运行自己不再打这个标记——它本来就是撤销，标记属于被撤销的那条。
            fields['compensated_at'] = now()
        if run_status == 'waiting_input':
            fields['error'] = (state.get('failure')
                               or '计划未通过校验，等待用户补充信息')
        elif state.get('failure'):
            # 失败原因必须落在 run 行上：Worker/运维只看库，不能只留在一份
            # 内存 state 里——那等于"失败了但没人知道为什么"。
            fields['error'] = state['failure']
        if context is not None and context.lease_owner:
            # 有执行权就走带门禁的收尾：版本 + 待处理补充一起校验，一次事务。
            try:
                await self.store.finish_run_owned(
                    context, run_id=record.run_id, status=run_status,
                    error=fields.get('error'), state=state,
                    expected_intent_version=expected_intent_version,
                    plan_version=fields['plan_version'],
                    compensated_at=fields.get('compensated_at'),
                    require_no_pending_inputs=require_pending_clear)
            except execution_control.LeaseLost:
                logger.warning('run %s 收尾时已失租，不写终态', record.run_id)
                return
            record.status = run_status
        else:
            await self.store.update_run(record.run_id, **fields)
        # outbox：业务事件与派生作业在同一处写入，消费者按 dedupe_key 幂等
        await self.store.enqueue_job(
            uuid.uuid4().hex[:12], 'session_summary',
            f"{record.session_id}:{record.run_id}",
            {'session_id': record.session_id})

    # ---------- 运行中补充输入（文档 harness-live-input §3） ----------

    async def append_run_input(self, run_id: str, *, request_id: str,
                               message: str, principal=None,
                               task_id: str = '') -> dict:
        """接收一条"给当前任务的补充要求"。

        **不进入 `Hub.start()`**：不建 task、不建 run、不重新领取租约。补充要求
        只是把 `runs.intent_version` 推高一版，让旧计划的下一个动作在准入时被拦下
        （文档 §3 第二步）。

        授权与归属在这里做（`ensure_owner`），事务在仓储层。对于同一个
        `request_id` 的重试，仓储层会先返回原回执——即使这个 run 已经结束，
        也不能把"我收到了"变成"我没收到"。
        """
        run = await self.store.run(run_id)
        if run is None:
            raise run_inputs_module.InputRejected('run_missing', '运行不存在')
        meta = await self.store.task_meta(run['task_id']) or {}
        # 没有 principal（单机/测试）时用匿名主体：它对本机数据是放行的
        policies.ensure_owner({'tenant_id': run.get('tenant_id') or 'default',
                               'owner_user_id': meta.get('owner_user_id')
                               or 'default'},
                              principal or policies.Principal(), '运行')
        if task_id and task_id != run['task_id']:
            # 客户端拿的是别的任务：不能把输入转发到这条 run 上
            raise run_inputs_module.InputRejected('run_not_current',
                                                  '该运行不属于这个任务')
        result = await self.store.append_run_input(
            run_id=run_id, request_id=request_id, message=message,
            task_id=run['task_id'],
            max_pending=settings.run_input_max_pending,
            max_chars=settings.run_input_max_chars)
        if not result.get('replayed'):
            logger.info('run %s 接受补充 #%s（要求版本 %s）', run_id,
                        result.get('input_seq'), result.get('intent_version'))
            # 提交之后才通知进程内执行者：通知只是加速，安全靠数据库
            await self._wake_executor(run_id)
        return result

    async def run_inputs(self, run_id: str, *, after_seq: int = 0,
                         limit: int = 50) -> dict:
        """回读补充输入与处理状态（按 input_seq 分页）。"""
        items = await self.store.run_inputs(run_id, after_seq=after_seq,
                                           limit=limit)
        info = await self.store.run_intent(run_id)
        return {'run_id': run_id, 'inputs': items,
                'intent_version': int(info.get('intent_version') or 0),
                'processed_intent_version': int(
                    info.get('processed_intent_version') or 0),
                'pending_input_count': int(info.get('pending_input_count') or 0)}

    async def _wake_executor(self, run_id: str) -> None:
        """尽力唤醒本进程内正在跑这条 run 的执行者。

        真正生效的阻断来自 `admit_action` 的事务比较；这里只是让"正在等着的
        执行者"早点知道，少跑一步无用功。跨进程通知未实现（写入数据库即达成
        一致的可见性，另一个执行者会在自己的边界读到）。
        """
        for record in list(self.tasks.values()):
            if record.run_id != run_id or not record.live:
                continue
            try:
                await record.publish(action('补充要求已保存', '下一个边界按新要求调整', '📝'))
            except Exception:                       # noqa: BLE001
                logger.warning('唤醒执行者失败 run=%s', run_id, exc_info=True)

    async def pause(self, run_id: str) -> dict:
        """请求暂停。返回 `{ok, status, pause_requested}`。

        两种情形必须分开（文档 §7.1）：

        - **queued**：还没有执行者在跑，同一事务里直接置 `paused`，Worker 就领不到了；
        - **running**：只登记 `pause_requested`，由执行者在下一步准入前读到并安全退出。
          重复暂停是幂等的；已经结束的 run 返回当前状态而不是假装受理。
        """
        run = await self.store.run(run_id)
        if run is None:
            return {'ok': False, 'status': '', 'pause_requested': False}
        status = str(run.get('status') or '')
        if status == 'queued':
            await self.store.update_run(run_id, status='paused', pause_requested=1)
            logger.info('run %s 在排队阶段直接暂停', run_id)
            return {'ok': True, 'status': 'paused', 'pause_requested': True}
        if status == 'running' or status == 'reconciling':
            await self.store.update_run(run_id, pause_requested=1)
            return {'ok': True, 'status': status, 'pause_requested': True}
        if status == 'paused':
            return {'ok': True, 'status': 'paused', 'pause_requested': True}
        return {'ok': False, 'status': status,
                'pause_requested': bool(run.get('pause_requested'))}

    async def resume(self, run_id: str) -> TaskRecord | None:
        """续跑：沿用已存计划重跑，账本会把已成功的步骤直接复用。"""
        run = await self.store.run(run_id)
        if run is None:
            return None
        status = str(run.get('status') or '')
        # 只允许"停下来"的状态恢复。**不复活** succeeded/cancelled：
        # 前者再跑一遍是重复副作用，后者是用户明确要求结束。
        if status not in {'paused', 'interrupted', 'queued'}:
            logger.info('run %s 状态为 %s，不恢复', run_id, status)
            return None
        if await self._has_uncertain_actions(run_id):
            # 有在途/未知动作：必须先核实，不能靠 resume 把它清掉
            logger.warning('run %s 存在结果未定的动作，拒绝恢复', run_id)
            return None
        # 必须回库：进程重启后 self.tasks 是空的，而"重启后续跑"正是它最需要
        # 工作的场景。以前这里只查内存，于是停掉进程就再也续不上（永远 409）。
        record = await self.get(run['task_id'])
        if record is None:
            return None
        if record.live:
            return record
        plan = await self.store.plan(run_id) or {}
        steps = plan.get('steps') or []
        if not steps:
            return None
        # 续跑也要先占租约：否则同一份计划可能被 Worker 与本次 resume 同时执行。
        # 但已被别人持有租约时不能动它的状态——先把 running 改成 queued 就等于
        # 替正在执行的进程改账。
        if (run['status'] in {'running', 'reconciling'}
                and run.get('lease_expires_at')
                and float(run['lease_expires_at']) > time.time()):
            return None
        await self.store.update_run(run_id, status='queued', pause_requested=0)
        claimed = await self.store.claim_run(self.owner_id, self.lease_seconds,
                                       run_id=run_id)
        if claimed is None:
            return None
        record.run_id = run_id
        record.status = 'running'
        record.lease_owner = claimed.get('lease_owner')
        # **代次必须跟着新领取走**：漏掉这一行就会拿着旧代次去准入，
        # 第一次动作就被仓储层判失租（文档 §4.2）。
        record.lease_epoch = int(claimed.get('lease_epoch') or 0)
        record._lease_lost = False
        # 续跑与 Worker 走**同一条执行入口**：计划复用、按步骤恢复、快照收尾、
        # 失租处理只有一份实现。这里只负责领取并返回记录（API 要立刻回 run_id）。
        record._task = asyncio.create_task(self.execute_claimed_run(claimed))
        return record

    # ---------- 补偿（文档 §8.4） ----------

    async def compensation_steps(self, run_id: str) -> list[dict]:
        """把"已成功且声明了 compensate 的步骤"翻成一组撤销步骤（逆序）。

        状态只认**库里**的 steps：进程重启后内存里的 state 已经没了，而
        "哪些步骤真的成功过"必须以落库结果为准，不能凭计划推断。
        """
        plan = await self.store.plan(run_id) or {}
        done = {row['step_id']: row.get('status')
                for row in await self.store.steps_of(run_id)}
        undo = []
        for step in reversed(plan.get('steps') or []):
            comp = step.get('compensate') or {}
            if not comp.get('tool'):
                continue
            if done.get(step.get('step_id')) != 'succeeded':
                continue
            undo.append({'tool': comp['tool'],
                         'step_id': f'undo:{step["step_id"]}',
                         'args': comp.get('args') or {},
                         'depends_on': [], 'why': f'补偿步骤 {step["step_id"]}',
                         'tool_key': comp.get('tool_key') or '',
                         'effect': comp.get('effect') or 'unknown',
                         'compensates': step['step_id']})
        return undo

    async def compensate(self, run_id: str) -> dict | None:
        """手工补偿：**新建一次运行**去撤销已完成的动作，不改变原来那条记录。

        为什么不做成"把 run 标成已撤销"：撤销本身是有副作用的调用，必须有
        自己的账本动作、事件流和结果。也不和取消混在一起——取消只是停下来，
        已经发出去的写操作不会因为协程退出而消失。
        """
        run = await self.store.run(run_id)
        if run is None:
            return None
        if run['status'] in {'queued', 'running', 'reconciling'}:
            raise RuntimeError('该运行还没结束，不能补偿')
        if run.get('compensated_at'):
            raise RuntimeError('该运行已经补偿过')
        undo = await self.compensation_steps(run_id)
        if not undo:
            return None
        record = await self.get(run['task_id'])
        if record is None:
            return None
        if record.live:
            raise RuntimeError('该任务正在执行，不能同时补偿')

        before = dict(record.state or {})
        new_run_id = uuid.uuid4().hex[:12]
        await self.store.create_run(new_run_id, record.task_id, status='queued',
                              tenant_id=run.get('tenant_id') or 'default')
        claimed = await self.store.claim_run(self.owner_id, self.lease_seconds,
                                       run_id=new_run_id)
        if claimed is None:
            raise RuntimeError('无法领取补偿运行，请稍后重试')
        record.run_id = new_run_id
        record.lease_owner = claimed.get('lease_owner')
        record._task = asyncio.create_task(self._run(
            record, [], {},
            resume_plan={'steps': undo, 'summary': '补偿已完成步骤',
                         'kind': 'compensation'}))
        try:
            await record._task
        except asyncio.CancelledError:
            return {'run_id': new_run_id, 'status': 'cancelled',
                    'steps': []}
        # 补偿运行本身不产出产物，别把原任务的状态覆盖成空
        await self.store.update_run(run_id, compensated_at=now())
        state = dict(record.state or {})
        if before.get('artifact') and not state.get('artifact'):
            state['artifact'] = before['artifact']
        await record.persist(state=state)
        return {'run_id': new_run_id,
                **outcome_module.view(record.status, state),
                'steps': state.get('compensations') or []}

    async def reconcile(self, owner: str | None = None) -> dict:
        """启动时对账遗留运行，返回统计（文档 §8.3）。

        owner 不传时只对账**租约已过期或从未写入**的运行——多副本启动时
        不能替别人正在跑的 run 判死。
        """
        return await self.store.reconcile_orphan_runs(owner=owner)

    async def get(self, task_id: str) -> TaskRecord | None:
        """取任务。内存里没有再回库找——支持进程重启后继续查询与补发事件。"""
        record = self.tasks.get(task_id)
        if record is not None:
            return record
        data = await self.store.task(task_id)
        if data is None:
            return None
        meta = await self.store.task_meta(task_id) or {}
        record = TaskRecord(task_id=data['task_id'], session_id=data['session_id'],
                            message=data['message'], store=self.store,
                            tenant_id=meta.get('tenant_id') or 'default',
                            owner_user_id=meta.get('owner_user_id') or 'default')
        record.status = outcome_module.check_run_status(data['status'])
        record.artifact_id = data['artifact_id']
        record.error = data['error']
        record.state = data['state']
        self.tasks[task_id] = record
        self._evict_cache()
        return record

    def _evict_cache(self) -> None:
        """限制内存里驻留的任务数。

        每查一次历史任务就往 self.tasks 塞一条且永不释放，长跑进程会一直涨。
        只淘汰已结束、且本进程没有在跑的记录。
        """
        if len(self.tasks) <= self.MAX_CACHED_TASKS:
            return
        for key, item in list(self.tasks.items()):
            if len(self.tasks) <= self.MAX_CACHED_TASKS:
                break
            if item.done and not item.live:
                self.tasks.pop(key, None)

    async def cancel(self, task_id: str) -> bool:
        """请求取消一个任务当前的那次运行。"""
        meta = await self.store.task_meta(task_id) or {}
        run_id = meta.get('current_run_id')
        if not run_id:
            return False
        return await self.cancel_run(run_id)

    async def cancel_run(self, run_id: str) -> bool:
        """请求取消指定运行。True 表示请求已生效或已送达执行者。

        跨进程不能直接 cancel 一个 asyncio.Task，只能往库里写标记，由持有租约
        的执行者在自己那一侧轮询到再停（见 `_guard`）。以前这里只看内存、却
        无条件把 run 标成 cancelled —— 进程外的任务其实还在跑，界面说停了。
        """
        run = await self.store.run(run_id)
        if run is None or run['status'] not in {'queued', 'running', 'reconciling'}:
            return False
        if run.get('cancel_requested_at'):
            return True
        record = self.tasks.get(run['task_id'])
        if record is not None and record.live and record.run_id == run_id:
            record._task.cancel()
            return True
        await self.store.update_run(run_id, cancel_requested_at=now())
        logger.info('run %s 已写入取消请求（执行者在其他进程）', run_id)
        return True

    async def deploy_artifact(self, record: TaskRecord, prior_artifact: dict,
                              principal=None) -> dict:
        """把"部署这份产物"当成**一次正常执行**来跑：建 run → 一步计划 →
        账本、事件、策略、取消语义全部复用执行器。

        以前这个按钮直接调 `deploy_task()`：没有账本 action（崩溃后不知道发没
        发出去）、没有事件序号（界面看不到过程）、不检查策略，暂停/取消也管不到
        它。文档 §13 明确不允许保留"绕过执行器的独立写入口"。
        """
        tenant = getattr(principal, 'tenant_id', record.tenant_id or 'default')
        run_id = uuid.uuid4().hex[:12]
        await self.store.create_run(run_id, record.task_id, status='queued',
                              tenant_id=tenant)
        claimed = await self.store.claim_run(self.owner_id, self.lease_seconds,
                                       run_id=run_id)
        if claimed is None:
            raise RuntimeError('无法领取部署运行，请稍后重试')
        step = {'tool': publishing_capability.TOOL_NAME, 'step_id': 's1', 'args': {},
                'depends_on': [], 'why': '用户点击部署'}
        keep_artifact_id = record.artifact_id
        record.run_id = run_id
        record.lease_owner = claimed.get('lease_owner')
        record._task = asyncio.create_task(self._run(
            record, [], {'artifact': prior_artifact},
            resume_plan={'steps': [step], 'summary': '部署上一轮产物'}))
        try:
            await record._task
        except asyncio.CancelledError:
            # 部署运行被取消：状态已经由 _run 写好，别把取消当异常抛给接口
            logger.info('部署运行 %s 被取消', run_id)
            return {}
        # 这次 run 没有重新渲染，产物还是上一轮那份：不能让它被空 state 覆盖，
        # 否则部署完再点"下载产物"就 404 了。
        state = dict(record.state or {})
        if not state.get('artifact') and prior_artifact:
            state['artifact'] = prior_artifact
        record.artifact_id = keep_artifact_id or record.artifact_id
        await record.persist(state=state, artifact_id=record.artifact_id)
        return state.get('deployment') or {}

    async def close(self) -> None:
        """停掉本进程里还在跑的执行，再关闭仓储。

        顺序很重要：先取消本地执行（**进程关停不是用户取消**，状态留给对账），
        再关连接池。反过来的话，执行协程会在池关闭后继续要连接，栈里就只剩
        "error connecting"，进程退出还会卡住。
        """
        running = [record for record in self.tasks.values() if record.live]
        for record in running:
            logger.info('shutdown: 取消本地执行 run=%s', record.run_id)
            if record._task is not None:
                record._task.cancel()
        if running:
            await asyncio.gather(*(record._task for record in running
                                   if record._task is not None),
                                 return_exceptions=True)
        for record in self.tasks.values():
            record.close()
        await self.store.close()
