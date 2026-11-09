"""补偿 mixin：把已成功步骤翻成撤销计划，另起一条 run 执行。

方法体与拆分前逐字相同。
"""
import asyncio
import uuid

from webpage_agent.core import outcome as outcome_module
from webpage_agent.store.pg_store import now


class HubCompensationMixin:
    """Hub 的补偿。"""

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
        # 多步 compensate 是图语义：把已成功步骤翻成撤销计划再跑。
        # Pi 工具循环不做这件事。补偿 run 显式固定 LangGraph，不跟默认引擎走 pi，
        # 否则会建一条不执行撤销步骤的空转。原 run 的引擎不变。
        await self.store.create_run(new_run_id, record.task_id, status='queued',
                              tenant_id=run.get('tenant_id') or 'default',
                              owner_user_id=str(run.get('owner_user_id') or 'default'),
                              runtime_kind='langgraph')
        record.run_id = new_run_id
        outcome = await self._claim_and_arm(
            record, new_run_id,
            lambda claimed: self._run(
                record, [], {},
                resume_plan={'steps': undo, 'summary': '补偿已完成步骤',
                             'kind': 'compensation'}),
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
