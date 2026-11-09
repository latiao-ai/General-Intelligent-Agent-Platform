"""补偿 mixin：把已成功步骤翻成撤销计划，另起一条 Pi run 按脚本执行。

撤销步骤有两个来源，都是事先写好的，不让模型看着账本现编：

1. 图计划里声明了 `compensate`、并且步骤状态是 succeeded（存量 LangGraph run）；
2. 账本成功动作的回执里冻着 `compensate`（Pi run 没有计划时走这条）。
   回执由 `tools.compensation.attach_compensate` 在结算时写入。
"""
import asyncio
import uuid

from webpage_agent.agent import runtime_identity
from webpage_agent.agent.pi_script import scripted_resume
from webpage_agent.core import outcome as outcome_module
from webpage_agent.store.pg_store import now


class HubCompensationMixin:
    """Hub 的补偿。"""

    # ---------- 补偿（文档 §8.4） ----------

    async def compensation_steps(self, run_id: str) -> list[dict]:
        """把已成功、且事先声明了撤销的动作翻成一组撤销步骤（逆序）。

        图计划优先：存量 run 的 `compensate` 写在计划步骤上，步骤状态以库为准。
        计划给不出撤销步骤时（Pi 原 run 通常没有 plan.steps），改读账本成功
        动作回执里冻住的 `compensate`。两种都只认已经落库的成功，不凭推断。
        """
        from_plan = await self._compensation_steps_from_plan(run_id)
        if from_plan:
            return from_plan
        return await self._compensation_steps_from_actions(run_id)

    async def _compensation_steps_from_plan(self, run_id: str) -> list[dict]:
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
            undo.append(_undo_step(comp, origin=str(step.get('step_id') or '')))
        return undo

    async def _compensation_steps_from_actions(self, run_id: str) -> list[dict]:
        actions = await self.store.actions_of(run_id)
        undo = []
        seen: set[str] = set()
        for act in reversed(actions or []):
            if str(act.get('status') or '') != 'succeeded':
                continue
            receipt = act.get('receipt') if isinstance(act.get('receipt'), dict) else {}
            comp = receipt.get('compensate') if isinstance(receipt, dict) else None
            if not isinstance(comp, dict) or not comp.get('tool'):
                continue
            origin = str(act.get('step_id') or '')
            if not origin or origin in seen:
                continue
            seen.add(origin)
            undo.append(_undo_step(comp, origin=origin))
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
        # 补偿 run 跟默认引擎一样走 Pi，但步骤是脚本，不是模型自由挑工具。
        # 原 run 的 runtime_kind 不动。
        await self.store.create_run(new_run_id, record.task_id, status='queued',
                              tenant_id=run.get('tenant_id') or 'default',
                              owner_user_id=str(run.get('owner_user_id') or 'default'),
                              runtime_kind=runtime_identity.KIND_PI)
        created = await self.store.run(new_run_id) or {}
        record.run_id = new_run_id
        resume_plan = scripted_resume(
            undo, kind='compensation', summary='补偿已完成步骤',
            intent_version=int(created.get('intent_version') or 1))
        outcome = await self._claim_and_arm(
            record, new_run_id,
            lambda claimed: self._run(record, [], {}, resume_plan=resume_plan),
            wait=True)
        if outcome != 'started':
            raise RuntimeError('无法领取补偿运行，请稍后重试')
        try:
            await record._task
        except asyncio.CancelledError:
            return {'run_id': new_run_id, 'status': 'cancelled',
                    'steps': []}
        # 补偿运行本身不产出产物，别把原任务的状态覆盖成空
        # 管理动作：补偿是**另起一条 run** 跑的，这里给被补偿的原 run 盖时间戳。
        # 原 run 早已结束、没有活跃执行者，因此是合法的越权写。
        await self.store.update_run(run_id, compensated_at=now())
        state = dict(record.state or {})
        if before.get('artifact') and not state.get('artifact'):
            state['artifact'] = before['artifact']
        await record.persist(state=state)
        return {'run_id': new_run_id,
                **outcome_module.view(record.status, state),
                'steps': state.get('compensations') or []}


def _undo_step(comp: dict, *, origin: str) -> dict:
    return {'tool': comp['tool'],
            'step_id': f'undo:{origin}',
            'args': comp.get('args') or {},
            'depends_on': [], 'why': f'补偿步骤 {origin}',
            'tool_key': comp.get('tool_key') or '',
            'effect': comp.get('effect') or 'unknown',
            'compensates': origin}
