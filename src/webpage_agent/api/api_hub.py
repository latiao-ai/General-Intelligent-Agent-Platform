"""任务中枢：会话、任务记录与事件广播（异步原生 + PostgreSQL 持久化）。

分工：

- **PostgreSQL（`webpage_agent/store/pg_store.py`）**：会话消息、任务状态、事件日志的**权威来源**。
  进程重启后会话与历史事件都还在。
- **内存**：只负责"实时投递"——订阅者队列与正在跑的 asyncio 任务。这两样天然
  不可能持久化：一个 asyncio.Task 是活着的进程状态，队列是连接级的。

因此晚连上来的、甚至重启之后再连上来的客户端，都能从库里补发完整执行过程，
而正在跑的客户端拿到的是内存直投的零延迟事件。用事件序号（seq）去重。

服务对象是 FastAPI：订阅与投递直接跑在服务器事件循环上，不需要额外的后台线程
与轮询（早期 Streamlit 版本那份实现已随 UI 迁移删除）。
"""
import asyncio
import logging
import time
import uuid
from dataclasses import replace

from webpage_agent.core.config import settings
from webpage_agent.observe.events import action, stage_end, stage_start
from webpage_agent.api.main import run_task
from webpage_agent.core.messages import cancel_note
from webpage_agent.governance import execution_control
from webpage_agent.store import ledger as ledger_module
from webpage_agent.core import memory as memory_module
from webpage_agent.core import outcome as outcome_module
from webpage_agent.core import services
from webpage_agent.core import profile as profile_module
from webpage_agent.core.services import is_stop_command
from webpage_agent.store.persistence import open_store
from webpage_agent.core.errors import BudgetExceeded
from webpage_agent.store.pg_store import PostgresStore, now

logger = logging.getLogger(__name__)

# 状态机词汇只有一套，在 webpage_agent/core/outcome.py 里定义：run/task 层只存通用状态，
# 界面看的 `answered` / `local_ready` / `deployed` 是**派生**出来的展示标签。
# 以前这里有 TERMINAL_STATUS + RUN_STATUS_OF 两张手工表，收尾时
# `RUN_STATUS_OF.get(status, status)` 兜底会把手写业务词静默写进 runs.status。
TERMINAL_STATUS = outcome_module.TERMINAL_RUN_STATUSES

# 合成阶段：衡量"用户发出指令"到"真正开始调用工具"之间的空档。
INSTRUCTION_STAGE = 'instruction'

# 哨兵：通知订阅者流已结束
CLOSE = object()

from webpage_agent.api.task_record import TaskRecord
from webpage_agent.api.hub_session import HubSessionMixin
from webpage_agent.api.hub_inputs import HubInputsMixin
from webpage_agent.api.hub_compensation import HubCompensationMixin
from webpage_agent.api.hub_recovery import HubRecoveryMixin, _resume_index
from webpage_agent.api.hub_messages import HubSessionMessageMixin


class Hub(HubSessionMixin, HubSessionMessageMixin, HubInputsMixin,
          HubCompensationMixin, HubRecoveryMixin):
    """会话与任务注册表。持久部分在 PostgreSQL，实时队列通过数据库通知跨进程补读。"""

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
        # 本进程同时执行的运行数。满了就留在 queued，不领取、不起协程。
        self._slots = asyncio.Condition()
        self._waiting_runs: list[str] = []
        # 等人补充的运行收到回答后，避免同一条 run 被排进两次续跑
        self._continuing_inputs: set[str] = set()
        self._closing = False

    def _limit(self) -> int:
        return max(1, int(settings.max_concurrent_runs))

    def _inflight(self, *, ignore_current: bool = False) -> int:
        """本进程里还没结束的执行。

        收尾时当前这条还没标记完成，补领下一条要把它排除，否则名额永远补不出来。
        对外判断是否已满时要算上它。
        """
        current = asyncio.current_task() if ignore_current else None
        count = 0
        for record in self.tasks.values():
            task = record._task
            if task is None or task.done() or task is current:
                continue
            count += 1
        return count

    def at_capacity(self) -> bool:
        return self._inflight() >= self._limit()

    def _arm(self, record: TaskRecord, claimed: dict, awaitable) -> None:
        record.lease_owner = claimed.get('lease_owner')
        record.lease_epoch = int(claimed.get('lease_epoch') or 0)
        record.run_id = str(claimed.get('run_id') or record.run_id)
        record.status = 'running'
        record._lease_lost = False
        self._waiting_runs = [
            item for item in self._waiting_runs if item != record.run_id]
        record._task = asyncio.get_running_loop().create_task(
            self._finish_and_pump(awaitable))

    async def _finish_and_pump(self, awaitable):
        try:
            return await awaitable
        finally:
            async with self._slots:
                try:
                    await self._pump_locked()
                finally:
                    self._slots.notify_all()

    async def _pump_locked(self) -> None:
        """调用方已持有名额锁。把先前留在队列里的运行补上，补到上限为止。"""
        if self._closing:
            return
        while self._waiting_runs and self._inflight(ignore_current=True) < self._limit():
            run_id = self._waiting_runs[0]
            try:
                claimed = await self.store.claim_run(
                    self.owner_id, self.lease_seconds, run_id=run_id)
            except Exception:                      # noqa: BLE001
                logger.warning('补领排队运行 %s 失败', run_id, exc_info=True)
                return
            self._waiting_runs.pop(0)
            if claimed is None:
                continue
            record = self.tasks.get(str(claimed.get('task_id') or ''))
            if record is None or record.live:
                # 刚领到的 run 要原样放回队列。带 owner + epoch 做门禁：如果这几行里
                # 已经被别的 Worker 接手，就不该再动它的租约与状态。
                requeue = getattr(self.store, 'requeue_run_owned', None)
                if callable(requeue):
                    await requeue(run_id,
                                  lease_owner=str(claimed.get('lease_owner') or ''),
                                  lease_epoch=int(claimed.get('lease_epoch') or 0))
                else:
                    await self.store.update_run(
                        run_id, status='queued', lease_owner=None,
                        lease_expires_at=None)
                continue
            self._arm(record, claimed, self.execute_claimed_run(claimed))

    async def _claim_and_arm(self, record: TaskRecord, run_id: str, factory,
                             *, wait: bool) -> str:
        """领取并在本进程执行。

        返回 `started`、`queued`（已达上限，留在队列）或 `unclaimed`（行已经被别人领走）。
        `wait=True` 时等到有名额再领，用于部署和补偿这种调用方要等结果的入口。
        """
        async with self._slots:
            while self._inflight() >= self._limit():
                if not wait:
                    if run_id not in self._waiting_runs:
                        self._waiting_runs.append(run_id)
                    record.status = 'queued'
                    logger.info('run %s 已达并发上限 %s，留在队列',
                                run_id, self._limit())
                    return 'queued'
                await self._slots.wait()
            claimed = await self.store.claim_run(
                self.owner_id, self.lease_seconds, run_id=run_id)
            if claimed is None:
                return 'unclaimed'
            self._arm(record, claimed, factory(claimed))
            return 'started'

    async def start(self, message: str, session_id: str | None = None,
              principal=None, scene: dict | None = None,
              request_spec: dict | None = None) -> TaskRecord:
        """建任务 + 建 run + **领取** + 就地执行。

        关键在"领取"这一步：API 进程要跑就地执行，也必须先按同一条 claim 路径
        占住租约。以前这里是 `create_run(status='running')` 却从不写租约，于是
        Worker 的领取条件（queued/running 且租约为空或过期）完全命中——同时起
        API 和 Worker，同一个任务就会跑两遍，部署类写操作真的会建出第二个站点。

        本进程同时执行的运行数有上限（`MAX_CONCURRENT_RUNS`）。到顶之后这条运行
        留在 queued，不领取、不起协程。有名额空出来时，再补领先前留下的运行。
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
        task_id, run_id = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
        admitted = await self.store.create_goal(
            task_id, run_id, session_id, message, tenant_id=tenant,
            owner_user_id=owner,
            owner_role=str(getattr(principal, 'role', '') or 'member'),
            platform_id=str(getattr(principal, 'platform_id', '') or ''),
            platform_subject=str(getattr(principal, 'subject', '') or ''),
            scene=scene, request_spec=request_spec)
        if admitted['replayed']:
            record = await self.get(admitted['task_id'])
            if record is None:
                raise RuntimeError('幂等记录指向不存在的任务')
            return replace(record, run_id=admitted['run_id'], idempotent_replay=True)
        record = TaskRecord(task_id=task_id, session_id=session_id,
                            message=message, store=self.store, run_id=run_id,
                            status='queued', tenant_id=tenant, owner_user_id=owner)
        self.tasks[record.task_id] = record
        event = stage_start(INSTRUCTION_STAGE)
        record.open_stage = {'stage': INSTRUCTION_STAGE, 'at': event['at']}
        await record.publish(event)
        outcome = await self._claim_and_arm(
            record, record.run_id,
            lambda claimed: self.execute_claimed_run(claimed), wait=False)
        if outcome == 'unclaimed':
            # 理论上不可能（刚刚才建出来的行）。真发生了就留在队列里，
            # 让 Worker 接手，绝不在没有租约的情况下开始执行。
            logger.warning('run %s 领取失败，已留在队列等待 Worker', record.run_id)
            record.status = 'queued'
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

        # 第七阶段：在读取/解释旧计划之前先确定运行时。未知引擎或版本不兼容
        # 在这里抛异常（不回退默认引擎），由上层收尾。
        from webpage_agent.agent import runtime_identity
        runtime_kind = await runtime_identity.resolve_kind(self.store, run_id)

        task = await self.store.task(task_id)
        if task is None:
            # 执行者收尾：带上领取时的 owner + epoch。若此刻已被别人接手，
            # 这一笔会落空（返回 False），而不是把接管者的状态盖掉。
            await self.store.update_run_owned(
                run_id, lease_owner=owner, lease_epoch=epoch,
                status='failed', error='任务不存在')
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
                # 同上：执行者收尾，必须过租约门禁
                await self.store.update_run_owned(
                    run_id, lease_owner=owner, lease_epoch=epoch,
                    status='failed', error='无任务记录')
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
        # 第七阶段：这段「图计划 + 步骤游标」的恢复解释**只属于 LangGraph**。
        # Pi 有动作却没有「图计划」是**正常状态**——它有自己的版本化快照装配，
        # 不套用「有动作但没有图计划就异常」这条 LangGraph 规则。
        if runtime_kind == runtime_identity.KIND_LANGGRAPH:
            plan = await self.store.plan(run_id) or {}
            steps = plan.get('steps') or []
            actions = await self.store.actions_of(run_id)
            snapshot_state = await self._recovery_state(run_id)
            waiting = {
                'asked_questions': [str(q) for q in (snapshot_state.get('asked_questions') or [])
                                    if str(q).strip()],
                'clarification': str(snapshot_state.get('clarification') or ''),
                'intent_version': int(run.get('intent_version') or 0),
                'processed_intent_version': int(run.get('processed_intent_version') or 0),
            }
            if steps:
                plan_version = int(plan.get('version') or 1)
                context = context.with_plan_version(plan_version)
                resume_plan = {'steps': steps, 'summary': plan.get('goal') or '续跑',
                               'plan_version': plan_version,
                               'step_index': _resume_index(steps, actions),
                               **waiting}
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
                # 执行者收尾：这是我们领到的 run，写结论必须带上自己的租约
                await self.store.update_run_owned(
                    run_id, lease_owner=owner, lease_epoch=epoch,
                    status='needs_reconcile', error='有动作但缺失计划，需人工核实')
                record.status = 'needs_reconcile'
                await record.persist(status='needs_reconcile', state=record.state)
                return record
            else:
                # 停在追问、计划还没提交：下一轮仍要带着问过的问题和版本，不能从零规划
                resume_plan = waiting
        else:
            # Pi：用自己的版本化快照装配恢复（第七阶段）。不读 plans/steps，
            # 不套 LangGraph 的「有动作但没图计划就异常」。
            resume_plan = await self._pi_resume_plan(run_id, run)

        history = await self.history(record.session_id)
        prior = await self.prior_context(record.session_id)
        memory = await self.store.memory(record.session_id)

        # 让 live/done 这些判断仍然有效：当前协程就是这次执行
        record._task = asyncio.current_task()

        # 所有步骤都已结算、但 run 没来得及收尾（进程在"最后一次结算"与
        # "写终态"之间消失）：这里只剩收尾，**不能**再跑一遍图——图拿不到
        # 上一次的 step_outputs，只会得出"什么都没有产出"（文档 §5.4）。
        # 这条「看快照收尾」的捷径也只属于 LangGraph 的步骤游标；Pi 的
        # 收尾决策走它自己的快照与恢复决策表。
        if runtime_kind == runtime_identity.KIND_LANGGRAPH \
                and resume_plan and resume_plan.get('steps') \
                and resume_plan['step_index'] >= len(resume_plan['steps']) \
                and not self._needs_observation():
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
        run_data = await self.store.run(record.run_id) or {}
        guard = (asyncio.create_task(self._guard(record))
                 if record.lease_owner else None)
        # 把画像读写限定到这次任务的主体上：多用户时别人不该被写进我的画像
        profile_token = profile_module.use(record.owner_user_id)
        from webpage_agent.governance import core as governance
        from webpage_agent.observe import telemetry
        billing_token = governance.bind(self.store, record.tenant_id, record.run_id)
        trace_token = telemetry.trace_id.set(run_data.get('trace_id') or uuid.uuid4().hex)
        span_token = telemetry.span_id.set(uuid.uuid4().hex[:16])
        logger.info('run_span_start run=%s', record.run_id)
        run_token = telemetry.run_id.set(record.run_id)
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

        # 编排节点（规划、观察、落库）还不算开始干活。第一个真正的工具阶段出现，
        # 才结算「收到指令」的耗时。工具名来自能力包或 MCP，这里不列举业务名。
        orchestration = {'instruction', 'plan', 'intake', 'persist_plan',
                         'run_step', 'observe_result', 'decide_next',
                         'wait_async', 'verify_completion', 'finalize'}

        async def relay(event: dict) -> None:
            stage = str(event.get('stage') or '')
            if (event.get('type') == 'stage_start'
                    and stage and stage not in orchestration):
                await close_instruction('规划完成')
            track(event)
            await record.publish(event)

        plan_version = int((resume_plan or {}).get('plan_version') or 1)
        book = ledger_module.ActionLedger(self.store, record.run_id, plan_version,
                                          context=context)
        context_token = execution_control.bind(context)

        async def bill_provider_request() -> None:
            """每次真实模型请求前先扣一次次数，紧接着按已记账 token 决定还能不能发。

            次数落在 `runs.consumed_json`：进程重启后要接着累计。token 不写进这列——
            结算发生在请求结束之后，手上没有执行租约（owner 与 epoch），写 runs
            就是一次没有门禁的越权写。token 只留在 provider_charges，这里用 SUM 读。

            流式 usage 在最后一个分片才到，所以这是事后记账、下一次请求前拦截，
            不是按最坏情况预留。次数闸仍先跑：它自己超限就抛 BudgetExceeded，
            这次请求不会发出，次数也不会加上。token 闸后跑，同样在请求发出前。
            被 token 闸拦住时，刚才加上的这一次还没变成真实请求，也没有
            provider_charges 行，必须退回。退回走 `refund_budget`（同一套
            owner/epoch 与行锁），不能直接改 consumed_json。
            重试也走这个钩子，不另开一条路径。
            """
            await self.store.consume_budget(
                context, run_id=record.run_id, key='provider_requests',
                limit=settings.run_max_provider_requests)
            try:
                await self.store.enforce_run_token_budget(record.run_id)
            except BudgetExceeded:
                # 只退 token 闸这一次。次数闸的 BudgetExceeded 在上面已经抛出，
                # 进不到这里，所以不会把「次数到顶」误退成「还能再发一次」。
                await self.store.refund_budget(
                    context, run_id=record.run_id, key='provider_requests')
                raise

        budget_token = services.bind_budget(bill_provider_request)

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

            state = None
            # 为什么这里有个循环：收尾前用户可能又补了要求，`_finish_run` 的
            # 版本 CAS 会抛 IntentChanged。此时**不能**把 run 标成结束（否则那条
            # 补充就永远没人处理），要回到消费循环重新修订（文档 §5 第十一步）。
            max_rounds = max(1, int(settings.run_max_revisions or 1)) + 1
            for round_index in range(max_rounds):
                if round_index:
                    # 新一轮要读数据库里最新的进度与补充，而不是拿着旧的提示。
                    # Pi 走自己的快照装配；这里若误用图的 `_resume_plan`，
                    # 有动作、没计划的 Pi run 会丢掉快照并从头再执行。
                    resume_plan = await self._resume_for_engine(record.run_id)
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
                # 执行者收尾：取消是本进程按控制请求停下的，仍要过租约门禁——
                # 否则一个已被接管的旧执行者会把接管者刚推进的状态改成 cancelled。
                await self.store.update_run_owned(
                    record.run_id, lease_owner=context.lease_owner,
                    lease_epoch=context.lease_epoch,
                    status='cancelled', cancel_requested_at=now())
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
            # 执行者收尾：上面已排除 `_lease_lost`，但租约仍可能在这几行之间过期，
            # 所以照样带 owner + epoch——门禁是数据库判的，不是内存里猜的。
            await self.store.update_run_owned(
                record.run_id, lease_owner=context.lease_owner,
                lease_epoch=context.lease_epoch,
                status=record.status, cancel_requested_at=now())
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
            hint = getattr(exc, 'safe_message', '') or ''
            record.error = f'{type(exc).__name__}: {hint or exc}'
            record.status = 'failed'
            await self.store.finish_run_owned(context, run_id=record.run_id,
                                             status='failed', error=record.error)
            await record.persist(status='failed', error=record.error)
            await record.publish({'type': 'status', 'status': 'failed',
                            'run_status': 'failed', 'detail': record.error})
        else:
            pass
        finally:
            logger.info('run_span_end run=%s status=%s', record.run_id, record.status)
            telemetry.span_id.reset(span_token)
            governance.unbind(billing_token)
            telemetry.trace_id.reset(trace_token)
            telemetry.run_id.reset(run_token)
            services.unbind_budget(budget_token)
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

    async def _finish_run(self, record: TaskRecord, state: dict, status: str, *,
                          context=None,
                          expected_intent_version: int | None = None,
                          require_pending_clear: bool = True,
                          final_message: str | None = None,
                          final_message_key: str = '') -> bool:
        """把 run 收尾，并把计划与步骤落库，供续跑与界面查询。

        返回 True 表示终态已提交；返回 False 表示**失租**（finish_run_owned 落空），
        调用方据此**禁止追加最终消息**（第七阶段）。

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
            # 第七阶段收口：Pi run 的终态快照必须标注版本化 Pi 快照 schema，
            # 否则 finish_run_owned 会把 Pi 快照覆盖成普通 state（schema=1）。
            # 第八阶段第 1 点：Pi 收尾提交**真正的版本化 Pi 快照**（来自
            # runtime 序列化到 state['_pi_snapshot'] 的结构），而不是给普通宿主
            # state 打 Pi schema 标签——普通 state 没有 messages/pending_calls/
            # checkpoint_seq 等恢复事实，恢复会拿它当 Pi 快照解析失败或错乱。
            from webpage_agent.agent import runtime_identity
            from webpage_agent.agent import pi_snapshot
            is_pi = bool(state.get('_pi_snapshot')) or bool(state.get('pi_resume'))
            snapshot_state = state
            if is_pi:
                raw_snapshot = state.get('_pi_snapshot')
                if isinstance(raw_snapshot, dict):
                    # 真正的版本化 Pi 快照：再规整一次（剥离任何混入的宿主字段），
                    # 保证落库的是最小完整的 Pi 快照，而不是普通 state。
                    try:
                        snapshot_state = pi_snapshot.serialize(raw_snapshot)
                    except Exception:      # noqa: BLE001
                        snapshot_state = raw_snapshot
            try:
                committed = await self.store.finish_run_owned(
                    context, run_id=record.run_id, status=run_status,
                    error=fields.get('error'), state=snapshot_state,
                    observation_seq=int(state.get('observation_seq') or 0),
                    next_route=str(state.get('next_route') or ''),
                    expected_intent_version=expected_intent_version,
                    plan_version=fields['plan_version'],
                    compensated_at=fields.get('compensated_at'),
                    require_no_pending_inputs=require_pending_clear,
                    final_message=final_message,
                    final_message_key=final_message_key,
                    session_id=record.session_id,
                    snapshot_schema=(runtime_identity.PI_STATE_SCHEMA
                                     if is_pi else None))
            except execution_control.LeaseLost:
                logger.warning('run %s 收尾时已失租，不写终态', record.run_id)
                return False
            if not committed:
                # 第七阶段：finish_run_owned 返回 False = 已失租（内部吞了
                # LeaseLost）。失租后**禁止继续追加最终消息**——这一笔落空，
                # 最终答复/消息由新执行者按恢复决策补收尾，这里直接返回。
                logger.warning('run %s 收尾时已失租（finish_run_owned=False），'
                               '禁止追加最终消息', record.run_id)
                record._lease_lost = True
                return False
            # 这里**只动 runs 行**，不动 record.status：`tasks` 行要等调用方的
            # persist() 才写。提前把内存置成终态会开一个窗口——record.done 已经
            # 为真、库里 tasks.status 还是 queued，回放接口按库读就显示"已完成
            # 但还在排队"。内存状态统一由 persist() 在落库之后设置。
        else:
            # 没有执行上下文（CLI 直调、单测）：这是**唯一**允许无租约写终态的地方。
            # 生产路径一定带 context（Worker / API 就地执行都会先 claim_run），
            # 所以这段不是"绕过 fencing 的后门"，而是"本来就没有租约可校验"。
            # 若将来出现新的无上下文生产调用点，那是设计错误，应当补 claim 而不是加到这里。
            await self.store.update_run(record.run_id, **fields)
            # 无上下文路径的最终答复也按稳定去重键幂等落库（第七阶段）。
            if final_message is not None:
                await self.store.append_message(
                    record.session_id, 'assistant', final_message,
                    dedupe_key=final_message_key)
        # outbox：业务事件与派生作业在同一处写入，消费者按 dedupe_key 幂等
        await self.store.enqueue_job(
            uuid.uuid4().hex[:12], 'session_summary',
            f"{record.session_id}:{record.run_id}",
            {'session_id': record.session_id})
        return True

    async def get(self, task_id: str) -> TaskRecord | None:
        """Refresh passive records from PostgreSQL; another worker may have changed them."""
        record = self.tasks.get(task_id)
        if record is not None and record.live:
            return record
        data = await self.store.task(task_id)
        if data is None:
            return None
        meta = await self.store.task_meta(task_id) or {}
        if record is None:
            record = TaskRecord(task_id=data['task_id'], session_id=data['session_id'],
                                message=data['message'], store=self.store,
                                tenant_id=meta.get('tenant_id') or 'default',
                                owner_user_id=meta.get('owner_user_id') or 'default')
        record.run_id = str(meta.get('current_run_id') or record.run_id)
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

        第八阶段收口第 7 点：也接受 `waiting_external`（持久等待中的 run）。等待
        中的 run 没有活跃执行者，取消要**持久化取消意图**，再对每个开放的外部
        任务发远端取消请求并查询确认（区分本地停止 / 远端取消请求 / 远端确认
        取消）。远端不支持取消时如实保留状态，不声称已取消。
        """
        run = await self.store.run(run_id)
        if run is None or run['status'] not in {'queued', 'running', 'reconciling',
                                                 'waiting_external'}:
            return False
        if run.get('cancel_requested_at'):
            return True
        if run['status'] == 'waiting_external':
            return await self._cancel_waiting_run(run_id, run)
        record = self.tasks.get(run['task_id'])
        if record is not None and record.live and record.run_id == run_id:
            record._task.cancel()
            return True
        # 管理动作（跨进程取消）：执行者在别的进程，这里只写请求标记，
        # 由持有租约的一方在自己那一侧读到再停。
        await self.store.update_run(run_id, cancel_requested_at=now())
        logger.info('run %s 已写入取消请求（执行者在其他进程）', run_id)
        return True

    async def _cancel_waiting_run(self, run_id: str, run: dict) -> bool:
        """取消一个持久等待中的 run：持久化取消意图 + 远端取消请求 + 查询确认。

        第八阶段收口第 7 点：区分三种情况——
        - **本地停止**：run 侧标 cancel_requested_at，不再被唤醒；
        - **远端取消请求**：对每个开放外部任务调合同取消能力（请求可能失败）；
        - **远端确认取消**：查询确认后 external task 标 cancelled，run 进
          cancelled/needs_reconcile。远端不支持取消时如实保留 waiting_external，
          **不声称任务已取消**。
        """
        from webpage_agent.core import external_tasks as ext
        # 先本地登记取消意图（run 侧）：不再被 complete 唤醒。
        await self.store.update_run(run_id, cancel_requested_at=now())
        tasks = await self.store.external_tasks_of_run(run_id)
        open_tasks = [t for t in tasks
                      if str(t.get('state') or '') in ('accepted', 'running',
                                                       'materializing')]
        confirmed_cancel = 0
        for task in open_tasks:
            result = await ext.request_external_cancel(
                self.store, external_task_id=str(task.get('external_task_id') or ''),
                worker_id=f'cancel-{run_id}')
            if result.get('confirmed'):
                confirmed_cancel += 1
        if confirmed_cancel:
            # 所有开放任务都已确认取消 → run 标记 cancelled（不复活、不误报）。
            await self.store.update_run(run_id, status='cancelled',
                                        waiting_external=0,
                                        cancel_requested_at=now())
            logger.info('run %s 等待中的外部任务已确认取消', run_id)
            return True
        # 远端未确认取消 / 不支持取消：如实保留状态（仍是 waiting_external），
        # 不声称已取消——由后续轮询或人工核实决定结局。
        logger.info('run %s 取消意图已登记，远端取消未确认（保留等待状态）', run_id)
        return True

    async def close(self) -> None:
        """停掉本进程里还在跑的执行，再关闭仓储。

        顺序很重要：先取消本地执行（**进程关停不是用户取消**，状态留给对账），
        再关连接池。反过来的话，执行协程会在池关闭后继续要连接，栈里就只剩
        "error connecting"，进程退出还会卡住。
        """
        self._closing = True
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
