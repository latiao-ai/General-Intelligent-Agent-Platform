"""恢复、暂停续跑、对账和部署 mixin。

从已落库的计划、动作和快照找回进度。暂停/继续与部署上一份产物也在这里：
它们和恢复共用同一套「结果未定就不续跑」的判断。
`_resume_index` 留在模块层，api_hub 里的 `execute_claimed_run` 仍按原名调用。
`INSTRUCTION_STAGE` 的定义留在 api_hub，本文件末尾再导入，避免两个模块顶上成环。
方法体与拆分前逐字相同。日志仍走 api_hub 的 logger。
"""
import asyncio
import logging
import time
import uuid

from webpage_agent.api.task_record import TaskRecord
from webpage_agent.capabilities import registry as capability_registry
from webpage_agent.core import outcome as outcome_module
from webpage_agent.core.messages import reply_text

logger = logging.getLogger('webpage_agent.api.api_hub')

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


class HubRecoveryMixin:
    """Hub 的恢复、暂停续跑、对账与部署。"""

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

    @staticmethod
    def _needs_observation() -> bool:
        """所有步骤都结算完之后，还要不要过观察/核对？

        **计划步骤跑完不等于任务完成**（文档 observe-decide §2 第 5 条）：
        片段可能声明了 observe，也可能还有没核对的完成条件。所以只要观察能力
        开着，就一律让图跑完 `observe_result → verify_completion`，不按快照
        直接收尾；只有观察关闭时才走"看快照收尾"的老捷径。
        """
        from webpage_agent.core.config import settings as cfg
        return bool(getattr(cfg, 'observe_enabled', True))

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
            # 传 context：这条 run 是本进程领的，收尾要走带租约的门禁。
            # **先过门禁**：失租时不得追加最终消息；最终答复与终态同事务提交，
            # 用稳定去重键保证恢复补收尾不重复（第七阶段）。
            final_key = f'final:{record.run_id}'
            committed = await self._finish_run(
                record, state, status, context=context,
                final_message=reply, final_message_key=final_key)
            if not committed:
                logger.warning('run %s 按快照收尾失租，跳过最终消息', record.run_id)
                return record
            await record.persist(status=status, state=state)
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

    # ---------- run 生命周期（文档 §7、§8.4） ----------

    async def _resume_plan(self, run_id: str) -> dict | None:
        """读库里当前的计划与起点（续跑/修订后重新执行用）。"""
        plan = await self.store.plan(run_id) or {}
        steps = plan.get('steps') or []
        info = await self.store.run_intent(run_id)
        snapshot = await self._recovery_state(run_id)
        # 已经问过的问题跟着快照走：下一轮修订不该把同一组问题再问一遍
        asked = [str(q) for q in (snapshot.get('asked_questions') or [])
                 if str(q).strip()]
        clarified = str(snapshot.get('clarification') or '')
        if not steps:
            # 没有已提交计划（上一轮停在等补充）：只能带版本号和问过的问题。
            # 版本号必须带上——收尾时的 CAS 拿它比对，缺了就会被当成
            # "补充还没处理"从而把同一轮再跑一遍。
            return {'asked_questions': asked, 'clarification': clarified,
                    'intent_version': int(info.get('intent_version') or 0),
                    'processed_intent_version': int(
                        info.get('processed_intent_version') or 0)}
        actions = await self.store.actions_of(run_id)
        return {'steps': steps, 'summary': plan.get('goal') or '续跑',
                'plan_version': int(plan.get('version') or 1),
                'intent_version': int(plan.get('intent_version')
                                      or info.get('intent_version') or 0),
                'processed_intent_version': int(
                    info.get('processed_intent_version') or 0),
                'asked_questions': asked, 'clarification': clarified,
                'step_index': _resume_index(steps, actions)}

    async def _pi_resume_plan(self, run_id: str, run: dict) -> dict:
        """Pi 恢复装配：读版本化 Pi 快照，构造续跑所需的 resume 上下文。

        与 LangGraph 的 `_resume_plan` 不同：Pi 没有「图计划 + 步骤游标」，
        它的恢复事实来自 `run_snapshots` 里的版本化 Pi 快照（阶段、检查点序号、
        要求版本、宿主结果状态、完整模型消息、逻辑调用序号、待处理调用、
        已提交工具结果引用、最终答复候选）。

        快照读取合同（第七阶段收口）——严格区分四种情况：

        - **首次执行**：无快照且无动作 → `pi_snapshot=None`，正常从头开始；
        - **已有动作却缺快照**：有动作但读不到快照 → 转待核实，**不得退回空
          状态重新执行**（否则会重复副作用）；
        - **快照损坏**（state_json 解析失败 / state 为空）→ 抛异常停止；
        - **版本不兼容**（state_schema 与当前不一致）→ 抛异常停止。

        后三种都不退回空状态重新执行。
        """
        from webpage_agent.agent import runtime_identity
        from webpage_agent.agent import pi_snapshot
        from webpage_agent.agent import pi_recovery

        actions = await self.store.actions_of(run_id)
        raw = None
        try:
            raw = await self.store.run_snapshot(run_id)
        except RuntimeError as exc:
            # 快照损坏：明确停止，不退回空状态
            raise pi_snapshot.SnapshotError(str(exc)) from exc

        snapshot = None
        if raw is not None:
            # 有快照行：严格解析（版本不兼容在这里抛）
            snapshot = pi_snapshot.parse_row(raw)
        elif actions:
            # 已有动作却缺快照：转待核实，不退回空状态重新执行
            decision = pi_recovery.RecoveryDecision(
                kind=pi_recovery.RESUME_RECONCILE,
                reason='有动作但缺失快照，无法确定进度，需核实')
            return {
                'runtime_kind': runtime_identity.KIND_PI,
                'intent_version': int(run.get('intent_version') or 0),
                'processed_intent_version': int(
                    run.get('processed_intent_version') or 0),
                'plan_version': int(run.get('plan_version') or 1),
                'pi_resume': {'snapshot': None,
                              'recovery': decision.to_dict()},
                'user_input': str(run.get('goal') or ''),
            }

        decision = pi_recovery.decide(
            snapshot, actions, str(run.get('status') or ''))
        # 第八阶段第 5 点：恢复时**先判断外部任务**。快照有 awaiting_calls（已受理
        # 的外部调用）时，按外部任务状态决定等待/交付/待核实，**不套用**普通
        # pending 重派发规则——否则会把已受理的提交重新执行（重复副作用）。
        external_tasks = []
        if snapshot and snapshot.get('awaiting_calls'):
            try:
                external_tasks = await self.store.external_tasks_of_run(run_id)
            except Exception:                      # noqa: BLE001
                logger.warning('读外部任务失败 run=%s', run_id, exc_info=True)
            ext_decision = pi_recovery.decide_external(snapshot, external_tasks)
            if ext_decision is not None:
                decision = ext_decision
        # 第七阶段收口：恢复输入用**单一字段** `pi_resume` 承载（快照 + 恢复决策），
        # 不在顶层与 resume_plan 里同时维护两份。run_task 把它原样放进
        # state['pi_resume']，PiRuntime.run() 只从这里读。
        resume = {
            'runtime_kind': runtime_identity.KIND_PI,
            'intent_version': int(run.get('intent_version') or 0),
            'processed_intent_version': int(
                run.get('processed_intent_version') or 0),
            'plan_version': int(run.get('plan_version') or 1),
            'pi_resume': {
                'snapshot': snapshot,
                'recovery': decision.to_dict(),
            },
        }
        # 保留用户原始输入：恢复后**不重复追加**原始输入，只把快照里未完成
        # 的处理位置交回。原始输入从快照取（首次执行则交给 run_task 的 user_input）。
        resume['user_input'] = str((snapshot or {}).get('user_input') or '')
        return resume

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
        # 最终答复与终态同事务提交，用稳定去重键（final:<run_id>）保证恢复
        # 补收尾不重复追加（第七阶段）。
        final_key = f'final:{record.run_id}'
        committed = await self._finish_run(
            record, state, status, context=context,
            expected_intent_version=state.get('intent_version'),
            require_pending_clear=status in ('succeeded', 'partially_succeeded',
                                             'needs_reconcile'),
            final_message=reply, final_message_key=final_key)
        if not committed:
            # 第七阶段：失租（finish_run_owned 落空）后**禁止追加最终消息**。
            # 终态与最终答复由新执行者按恢复决策补收尾，这里不再写 assistant
            # 消息或发状态事件，避免恢复后重复答复。
            logger.warning('run %s 收尾失租，跳过最终消息与状态事件', record.run_id)
            return
        # 先落记录（内存 + tasks 行），再发事件：两者都写、职责不同。
        await record.persist(status=status, state=state)
        # 状态事件里 `status` 仍是展示标签（前端认这套），另带 run_status
        await record.publish({'type': 'status', **outcome_module.view(status, state),
                              'detail': reply,
                              'intent_version': int(state.get('intent_version') or 0),
                              'plan_version': int(state.get('plan_version') or 0),
                              'asked_questions': [str(q) for q in (state.get('asked_questions') or [])
                                                 if str(q).strip()],
                              'clarification': str(state.get('clarification') or '')})

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
        # 管理动作（运维/用户暂停）：queued 的 run 没有执行者，不存在租约可校验；
        # 同一事务里直接置 paused，Worker 就领不到它了。
        if status == 'queued':
            await self.store.update_run(run_id, status='paused', pause_requested=1)
            logger.info('run %s 在排队阶段直接暂停', run_id)
            return {'ok': True, 'status': 'paused', 'pause_requested': True}
        # 管理动作：running 的执行者在别的进程，无法直接 cancel asyncio.Task，
        # 只能写控制标记，由执行者在下一个安全边界自己读到并停下。
        # **不能**在这里顺手改 status——那等于替正在跑的进程改账。
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
        if not steps and await self.store.actions_of(run_id):
            # 有动作却没有计划必须核实；首次规划尚未成功且没有动作时可以重试规划。
            return None
        # 续跑也要先占租约：否则同一份计划可能被 Worker 与本次 resume 同时执行。
        # 但已被别人持有租约时不能动它的状态——先把 running 改成 queued 就等于
        # 替正在执行的进程改账。
        if (run['status'] in {'running', 'reconciling'}
                and run.get('lease_expires_at')
                and float(run['lease_expires_at']) > time.time()):
            return None
        # 管理动作（resume）：上面已确认没有有效租约（否则直接返回 None），
        # 所以此刻没有执行者可被 fencing，不存在覆盖别人进度的风险。
        await self.store.update_run(run_id, status='queued', pause_requested=0)
        record.run_id = run_id
        previous_events = await self.store.events(record.task_id)
        await record.publish({'type': 'action', 'label': '继续执行当前任务', 'icon': '▶',
                              'resume_after_event_id': max((seq for seq, _ in previous_events), default=0)})
        # 续跑与 Worker 走**同一条执行入口**：计划复用、按步骤恢复、快照收尾、
        # 失租处理只有一份实现。满员时留在队列，不另起一条没有上限的协程。
        outcome = await self._claim_and_arm(
            record, run_id,
            lambda claimed: self.execute_claimed_run(claimed), wait=False)
        if outcome == 'unclaimed':
            return None
        return record

    async def reconcile(self, owner: str | None = None) -> dict:
        """启动时对账遗留运行，返回统计（文档 §8.3）。

        owner 不传时只对账**租约已过期或从未写入**的运行——多副本启动时
        不能替别人正在跑的 run 判死。
        """
        return await self.store.reconcile_orphan_runs(owner=owner)

    async def deploy_artifact(self, record: TaskRecord, prior_artifact: dict,
                              principal=None) -> dict:
        """把"部署这份产物"当成**一次正常执行**来跑：建 run → 一步计划 →
        账本、事件、策略、取消语义全部复用执行器。

        以前这个按钮直接调 `deploy_task()`：没有账本 action（崩溃后不知道发没
        发出去）、没有事件序号（界面看不到过程）、不检查策略，暂停/取消也管不到
        它。文档 §13 明确不允许保留"绕过执行器的独立写入口"。
        """
        tenant = getattr(principal, 'tenant_id', record.tenant_id or 'default')
        tool_name = capability_registry.direct_tool('deploy')
        if not tool_name:
            raise RuntimeError('没有声明部署动作的能力包')
        run_id = uuid.uuid4().hex[:12]
        await self.store.create_run(run_id, record.task_id, status='queued',
                              tenant_id=tenant,
                              owner_user_id=str(getattr(principal, 'user_id', '')
                                                or record.owner_user_id or 'default'))
        step = {'tool': tool_name, 'step_id': 's1', 'args': {},
                'depends_on': [], 'why': '用户点击部署'}
        keep_artifact_id = record.artifact_id
        record.run_id = run_id
        outcome = await self._claim_and_arm(
            record, run_id,
            lambda claimed: self._run(
                record, [], {'artifact': prior_artifact},
                resume_plan={'steps': [step], 'summary': '部署上一轮产物'}),
            wait=True)
        if outcome != 'started':
            raise RuntimeError('无法领取部署运行，请稍后重试')
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

from webpage_agent.api.api_hub import INSTRUCTION_STAGE
