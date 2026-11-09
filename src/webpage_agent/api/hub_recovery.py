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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from webpage_agent.agent.pi_recovery import RecoveryDecision

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


def _operation_payload_from_run(run_result) -> dict:
    """把 get_operation 工具的 run 返回规整成 verify_operation 期望的 payload。

    工具 run 返回通常是 `{status, operation, ...}`（MCP runner 把标量提到顶层）。
    这里兜底几种形状，提取 `status` / `operation` / `code` / `message`。
    """
    if not isinstance(run_result, dict):
        return {}
    return {
        'status': run_result.get('status', 'ok'),
        'operation': run_result.get('operation'),
        'code': run_result.get('code'),
        'message': run_result.get('message'),
    }


async def _return(value):
    """把一个值包成 awaitable（供 verify_operation 的 get_operation_call 用）。"""
    return value


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

    async def _resume_for_engine(self, run_id: str) -> dict | None:
        """按这条 run 已存的引擎装配续跑输入。

        Pi 没有图计划。补充要求触发的下一轮如果仍走 `_resume_plan`，
        会丢掉 `pi_resume`，PiRuntime 会当成首次执行再跑一遍工具。
        """
        from webpage_agent.agent import runtime_identity
        ident = await self.store.runtime_identity(run_id)
        if ident['kind'] == runtime_identity.KIND_PI:
            run = await self.store.run(run_id) or {}
            return await self._pi_resume_plan(run_id, run)
        return await self._resume_plan(run_id)

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
        # 第十阶段收口第 6 点：恢复 unknown 写时，先按原 operation ID 调 get_operation
        # 核实「远端是否已写」，再决定（applied → 复用 / not_found → 可重试 /
        # permission_denied、query_failed → 保持待核实）。不能盲目重发、也不能把
        # 权限错误当成「未执行」。
        if decision.kind == pi_recovery.RESUME_RECONCILE:
            try:
                verify = await self._verify_unknown_writes(run_id, actions)
            except Exception:                      # noqa: BLE001
                logger.warning('核实 unknown 写失败 run=%s', run_id, exc_info=True)
                verify = None
            if verify is not None and verify.get('applied'):
                # 远端已写：结果可复用，转 RESUME_DELIVER（回读对象 + 交付结果），
                # 不生成新写入冒充恢复。
                decision = pi_recovery.RecoveryDecision(
                    kind=pi_recovery.RESUME_DELIVER,
                    reason=f'远端已写（{",".join(verify["applied"])}），复用结果回读对象')
            elif verify is not None and verify.get('not_found') \
                    and not verify.get('applied') and not verify.get('unknown'):
                # 远端明确未写：可重试，但必须显式记录策略（这里保守转待核实，
                # 由人工确认是否重发，不自动重发避免重复副作用）。
                decision = pi_recovery.RecoveryDecision(
                    kind=pi_recovery.RESUME_RECONCILE,
                    reason='远端确认未写，可显式重试（需记录策略）')
        # 第十阶段第 5 点：等待审批的恢复。快照有 awaiting_approval 事实时，查
        # 审批表决定：已批准 → 恢复原候选（RESUME_ADMIT，重新检查权限/版本/revision/
        # 审批有效性）；仍 pending/已拒绝/过期 → 继续等待或待核实，不盲目重发。
        if snapshot and snapshot.get('awaiting_approval') \
                and decision.kind == pi_recovery.RESUME_WAIT:
            awaiting = list(snapshot.get('awaiting_approval') or [])
            decision = await self._decide_approval_recovery(
                run_id, awaiting, snapshot)
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
            # 第八阶段收口第 3 点：已有最终答复（resume_finish）时，**不被**旧的
            # awaiting 覆盖为「继续等待/交付」。最终答复说明外部结果早已消费、后续
            # 已推理完成，只需补收尾，不能退回外部等待。
            if ext_decision is not None and decision.kind != pi_recovery.RESUME_FINISH:
                decision = ext_decision
                # 第八阶段收口第 3 点：外部任务已完成 → 读回已登记的结果，装配成
                # 可直接注入 Node 的 deliver_results（原 toolCallId + 结果内容）。
                # 结果读回失败（结果没真正可消费）→ 转待核实，不能装成已交付。
                if ext_decision.kind == pi_recovery.RESUME_DELIVER:
                    try:
                        deliver = await self._assemble_deliver_results(
                            ext_decision.tool_results)
                        if deliver is None:
                            decision = pi_recovery.RecoveryDecision(
                                kind=pi_recovery.RESUME_RECONCILE,
                                reason='外部任务已完成但结果无法读回，需核实')
                        else:
                            ext_decision.deliver_results = deliver
                    except Exception:              # noqa: BLE001
                        logger.warning('装配外部结果失败 run=%s', run_id, exc_info=True)
                        decision = pi_recovery.RecoveryDecision(
                            kind=pi_recovery.RESUME_RECONCILE,
                            reason='外部结果装配失败，需核实')
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
        # 第十阶段第 6 点：恢复时把**新增要求内容真正注入 Pi**，不只是更新版本号。
        # 快照里的原始输入 + 尚未纳入计划提交的补充输入，合并成一句完整要求交给
        # Pi 决策；否则「版本号升了但模型没看到新要求」，旧候选仍会按旧要求继续。
        # 第十阶段收口第 5 点：除合并文本外，另把 pending 补充的**原文 + 序号**作为
        # 增量输入（pending_inputs）传下去，让 Pi 恢复时把它们当**明确的新消息**进入
        # 上下文——不是只在合并文本里隐式带上，否则模型分不清「新要求」和「旧上下文」。
        from webpage_agent.agent import run_inputs as run_inputs_module
        base_input = str((snapshot or {}).get('user_input') or '')
        pending_inputs = []
        try:
            if hasattr(self.store, 'pending_run_inputs'):
                pending_inputs = await self.store.pending_run_inputs(run_id)
        except Exception:                          # noqa: BLE001
            logger.warning('读待处理补充失败 run=%s', run_id, exc_info=True)
        resume['user_input'] = run_inputs_module.merged_requirement(
            base_input, pending_inputs)
        # 增量输入（原文 + 序号），供 PiRuntime 传进 Node resume.pending_inputs。
        resume['pending_inputs'] = [
            {'input_seq': int(item.get('input_seq') or 0),
             'message': str(item.get('message') or '')}
            for item in pending_inputs
            if isinstance(item, dict) and str(item.get('message') or '').strip()
        ]
        # 缺参停在 waiting_input：有新补充就把停靠的工具结果交付回去再继续；
        # 没有补充则保持等待，不重新推理。
        decision, snapshot = pi_recovery.apply_pending_input(
            decision, snapshot, resume['pending_inputs'])
        resume['pi_resume']['snapshot'] = snapshot
        resume['pi_resume']['recovery'] = decision.to_dict()
        return resume

    async def _decide_approval_recovery(self, run_id: str, awaiting: list,
                                        snapshot: dict) -> 'RecoveryDecision':
        """等待审批的恢复决策（第十阶段第 5 点收口）。

        查每个待审批调用的 approval 状态：
        - 已批准（approved）→ 恢复原候选（RESUME_ADMIT，原 call_id + **最终参数**
          + 参数指纹 + 对象 revision + 要求版本），由执行点重新检查权限、要求版本、
          对象 revision 与审批有效性；
        - 仍 pending → 继续等待（RESUME_WAIT），不重新推理；
        - 已拒绝/撤销/过期 → 转待核实（RESUME_RECONCILE），不盲目重发。
        """
        from webpage_agent.agent import pi_recovery
        approved_calls: list[dict] = []
        any_pending = False
        any_bad = False
        for item in awaiting:
            if not isinstance(item, dict):
                continue
            approval_id = str(item.get('approval_id') or '')
            if not approval_id:
                any_bad = True
                continue
            try:
                row = await self.store.approval(approval_id)
            except Exception:                      # noqa: BLE001
                row = None
            status = str((row or {}).get('status') or '')
            if status == 'approved':
                approved_calls.append({
                    'call_id': str(item.get('call_id') or ''),
                    'tool_name': str(item.get('tool_name') or ''),
                    # 恢复原调用的**最终参数**（含幂等键）与参数指纹——不是模型
                    # 重发的参数。参数已变化时，执行点按 args_hash 拒绝复用。
                    'arguments': dict(item.get('arguments') or {}),
                    'args_hash': str(item.get('args_hash') or ''),
                    'expected_revision': item.get('expected_revision'),
                    'intent_version': int(item.get('intent_version') or 0),
                })
            elif status == 'pending':
                any_pending = True
            else:
                # rejected / revoked / expired / 未知
                any_bad = True
        if approved_calls:
            # 已批准：恢复原候选（原 call_id/最终参数），执行点重新检查授权有效性
            # 与要求版本/revision（变化则拒绝）。
            return pi_recovery.RecoveryDecision(
                kind=pi_recovery.RESUME_ADMIT, pending_calls=approved_calls,
                reason='审批已批准，恢复原候选重新检查权限与版本')
        if any_bad:
            return pi_recovery.RecoveryDecision(
                kind=pi_recovery.RESUME_RECONCILE,
                reason='审批被拒绝/撤销/过期，需核实')
        if any_pending:
            return pi_recovery.RecoveryDecision(
                kind=pi_recovery.RESUME_WAIT, pending_calls=[],
                reason='审批仍待处理，继续等待')
        return pi_recovery.RecoveryDecision(
            kind=pi_recovery.RESUME_RECONCILE,
            reason='等待审批事实缺失有效关联，需核实')

    async def _verify_unknown_writes(self, run_id: str,
                                     actions: list[dict]) -> dict | None:
        """恢复 unknown 写时，按原 operation ID 调 get_operation 核实（第十阶段第 6 点）。

        从 actions 里找 `unknown` 且 effect=write 的动作，提取其最终参数里的幂等键
        （operation_id），用同一可信主体调用 `get_operation` 核实「远端是否已写」。
        严格区分四种结局（`platform_verify.verify_operation`）：

        - `applied`：远端已写（可复用结果，回读对象，不重发）；
        - `not_found`：远端明确未写（可显式重试，但需记录策略）；
        - `permission_denied`：权限错误——**不能当成未执行**，结果未知；
        - `query_failed`：查询失败，结果未知。

        返回 `{'applied': [...], 'not_found': [...], 'unknown': [...]}`，分别列出
        operation_id；找不到任何 unknown 写动作 / 无法提取幂等键时返回 None。
        """
        from webpage_agent.core import platform_verify
        from webpage_agent.tools import core as tools_core

        unknown_writes = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            if str(action.get('status')) != 'unknown':
                continue
            effect = str(action.get('effect') or '')
            receipt = action.get('receipt') or {}
            if not isinstance(receipt, dict):
                receipt = {}
            final_args = receipt.get('final_args') or {}
            operation_id = str(
                (final_args or {}).get('operation_id')
                or (final_args or {}).get('idempotency_key') or '')
            if effect != 'write' or not operation_id:
                continue
            unknown_writes.append({
                'action_id': str(action.get('action_id') or ''),
                'operation_id': operation_id,
                'final_args': final_args,
            })
        if not unknown_writes:
            return None

        # 找 get_operation 工具（同 server 的只读工具），用它核实。
        get_op_tool = None
        try:
            for tool in await tools_core.mcp_tools():
                if getattr(tool, 'name', '') == 'get_operation' \
                        and str(getattr(tool, 'effect', '') or 'unknown') == 'read':
                    get_op_tool = tool
                    break
        except Exception:                          # noqa: BLE001
            logger.warning('发现 get_operation 工具失败', exc_info=True)

        result = {'applied': [], 'not_found': [], 'unknown': []}
        for item in unknown_writes:
            operation_id = item['operation_id']
            if get_op_tool is None:
                # 没有核实工具：结果未知，转待核实。
                result['unknown'].append(operation_id)
                continue
            try:
                # 用同一可信主体（当前执行上下文）调用 get_operation。
                run_result = await get_op_tool.run(
                    {'operation_id': operation_id}, {'task_id': ''})
            except Exception as exc:                # noqa: BLE001
                logger.warning('核实操作失败 op=%s：%s', operation_id, exc)
                result['unknown'].append(operation_id)
                continue
            # 把 run 返回规整成 verify_operation 期望的 payload 形状。
            payload = _operation_payload_from_run(run_result)
            verification = await platform_verify.verify_operation(
                operation_id, server=None,
                get_operation_call=lambda oid: _return(payload))
            state = verification.get('state')
            if state == 'applied':
                result['applied'].append(operation_id)
            elif state == 'not_found':
                result['not_found'].append(operation_id)
            else:
                result['unknown'].append(operation_id)
        return result

    async def _assemble_deliver_results(self, refs: list[dict]) -> list[dict] | None:
        """把外部任务的完成结果读回，装配成可注入 Node 的 deliver_results。

        第八阶段收口第 3 点：交付的是**已登记、可消费**的结果内容（经标准结果
        存储登记），不是 status 包或文件路径。读回失败返回 None（调用方转待核实）。

        返回每项：`{call_id, tool_name, result, is_error}`，Node 据此 pushToolResult。
        """
        from webpage_agent.tools import tool_results as tr
        from webpage_agent.core.config import settings as cfg
        assembled: list[dict] = []
        for ref in (refs or []):
            if not isinstance(ref, dict):
                continue
            result_id = str(ref.get('last_result_id') or '')
            call_id = str(ref.get('call_id') or '')
            if not result_id or not call_id:
                # 缺 result 引用或原 toolCallId：无法交付，转待核实。
                return None
            manifest = None
            try:
                manifest = await self.store.result_manifest(result_id)
            except Exception:                      # noqa: BLE001
                manifest = None
            if manifest is None:
                # 结果未登记（不是可消费结果）→ 待核实。
                return None
            try:
                content = tr.read_structured(str(cfg.artifact_dir), manifest)
            except Exception:                      # noqa: BLE001
                content = None
            if content is None:
                # 结构化结果读不回（损坏/缺失）→ 待核实。
                return None
            assembled.append({
                'call_id': call_id,
                'tool_name': str(ref.get('tool_name') or ''),
                'result': content,
                'is_error': False,
            })
        return assembled

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
        # 第八阶段收口第 10 点：waiting_external 是「持久等待」，**不是终态**，
        # 不能写最终答复——否则「任务结束，状态：waiting_external」这条兜底文案
        # 会占掉 final:<run_id> 去重键，导致后续真正终态（succeeded）的最终答复
        # 被去重、用户看到的答复停留在「等待中」。
        final_key = f'final:{record.run_id}'
        final_message = None
        if status != 'waiting_external':
            final_message = reply
        committed = await self._finish_run(
            record, state, status, context=context,
            expected_intent_version=state.get('intent_version'),
            require_pending_clear=status in ('succeeded', 'partially_succeeded',
                                             'needs_reconcile'),
            final_message=final_message, final_message_key=final_key)
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
        from webpage_agent.agent import runtime_identity
        ident = await self.store.runtime_identity(run_id)
        if ident['kind'] != runtime_identity.KIND_PI:
            # 只属于图：有动作却没有计划必须核实。Pi 没有步骤计划，
            # 工具动作落在 actions / 快照里，不能用这条规则把续跑判死。
            plan = await self.store.plan(run_id) or {}
            steps = plan.get('steps') or []
            if not steps and await self.store.actions_of(run_id):
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
        # 部署仍是「一步图计划」，不是 Pi 工具循环。显式固定 LangGraph，
        # 避免默认引擎改成 pi 之后这条 run 丢掉步骤、改去自由工具循环。
        await self.store.create_run(run_id, record.task_id, status='queued',
                              tenant_id=tenant,
                              owner_user_id=str(getattr(principal, 'user_id', '')
                                                or record.owner_user_id or 'default'),
                              runtime_kind='langgraph')
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
