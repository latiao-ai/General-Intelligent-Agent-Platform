"""运行中途补充输入 mixin。

补充要求不新建任务，只抬高 intent_version；停在等人的那条 run 再排队续跑。
方法体与拆分前逐字相同。日志仍走 api_hub 的 logger，调用方的日志配置不用改。
"""
import asyncio
import logging

from webpage_agent.agent import run_inputs as run_inputs_module
from webpage_agent.core.config import settings
from webpage_agent.governance import policies
from webpage_agent.observe.events import action

logger = logging.getLogger('webpage_agent.api.api_hub')


class HubInputsMixin:
    """Hub 的运行中途输入。"""

    # ---------- 运行中补充输入 ----------

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
            await self._continue_waiting_input(run)
        return result

    async def _continue_waiting_input(self, run: dict) -> None:
        """等人补充的运行收到回答后，接着跑同一条 run，不另开任务。

        暂停不在这里恢复：暂停期间的补充只落库，要等显式 resume。
        执行者还在跑时也不领第二遍；等它退出后，若补充仍没被纳入，再排队续跑。
        """
        if not await self._accepts_input_continuation(run):
            return
        run_id = str(run.get('run_id') or '')
        if not run_id or run_id in self._continuing_inputs:
            return
        for record in list(self.tasks.values()):
            if record.run_id == run_id and record.live:
                self._continuing_inputs.add(run_id)
                asyncio.create_task(self._continue_after_executor(run_id))
                return
        await self._requeue_waiting_input(run)

    async def _continue_after_executor(self, run_id: str) -> None:
        """执行者还在收尾时先等它结束，再决定要不要把同一条 run 重新排队。"""
        try:
            record = next((item for item in self.tasks.values()
                           if item.run_id == run_id), None)
            task = getattr(record, '_task', None) if record else None
            if task is not None:
                try:
                    await task
                except Exception:                  # noqa: BLE001
                    return
            run = await self.store.run(run_id)
            if not run or not await self._accepts_input_continuation(run):
                return
            if not await self.store.pending_run_inputs(run_id):
                return
            await self._requeue_waiting_input(run)
        finally:
            self._continuing_inputs.discard(run_id)

    async def _accepts_input_continuation(self, run: dict) -> bool:
        if run.get('status') == 'waiting_input':
            return True
        if run.get('status') != 'waiting_approval':
            return False
        # External platform confirmations may require observation, while local
        # action approvals must still use their manifest-bound approval route.
        approvals = await self.store.approvals_for(tenant_id=run.get('tenant_id') or 'default',
                                                  status='pending')
        return not any(row.get('run_id') == run.get('run_id') for row in approvals)

    async def _requeue_waiting_input(self, run: dict) -> None:
        """把停在等人的运行放回队列，由当前进程或 Worker 接着修订。"""
        run_id = str(run.get('run_id') or '')
        record = await self.get(run['task_id'])
        if record is None or record.live:
            return
        # 管理动作（补充要求后放回队列）：调用者不是这条 run 的执行者，
        # 唤醒的正当性来自"用户补充了要求"。改成 _owned 会让这条永远唤不醒。
        await self.store.update_run(run_id, status='queued')
        await self.store.update_task(run['task_id'], status='queued')
        record.run_id = run_id
        record.status = 'queued'
        await self._claim_and_arm(
            record, run_id,
            lambda claimed: self.execute_claimed_run(claimed), wait=False)

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
