"""运行级预算的扣减与退回。

只有 `consume_budget` 和 `refund_budget`。限流和 provider 用量查询已经在
`GovernanceStore` 上，不是 `PostgresStore` 的方法。方法体没有改。
"""
import json

from webpage_agent.core.errors import BudgetExceeded
from webpage_agent.store.pg_store import now

class PgBudget:
    """PgBudget 方法组。见模块说明。"""

    async def consume_budget(self, context, *, run_id: str, key: str,
                             amount: int = 1, limit: int | None = None) -> int:
        """原子累加运行级用量，超过上限抛 `BudgetExceeded`。

        为什么要落库：`state.usage` 只是展示统计，进程重启就没了；而"这次运行
        到底用掉了多少预算"必须跨重启、跨执行者累计（文档 §12）。
        """
        async with self._lease() as conn:
            row = await self._owned(conn, context, run_id=run_id)
            if row is None:
                raise BudgetExceeded('运行不存在')
            # `_owned` 返回的是**原始行**（列名是 *_json），不是解码后的字典：
            # 这里按列名解析，否则每次累加都会把上一种用量覆盖掉（看起来像"只记录了最后一次"）。
            try:
                consumed = json.loads(row.get('consumed_json') or '{}')
            except ValueError:
                consumed = {}
            try:
                budget = json.loads(row.get('budget_json') or '{}')
            except ValueError:
                budget = {}
            used = int(consumed.get(key) or 0)
            cap = limit
            if cap is None:
                cap = budget.get(key)
            if cap is not None and used + int(amount) > int(cap):
                raise BudgetExceeded(
                    f'运行预算用尽（{key}: {used}/{cap}）')
            consumed[key] = used + int(amount)
            await self._run(
                conn, 'UPDATE runs SET consumed_json=%s, updated_at=%s WHERE run_id=%s',
                (json.dumps(consumed, ensure_ascii=False), now(), run_id))
            return consumed[key]

    async def refund_budget(self, context, *, run_id: str, key: str,
                            amount: int = 1) -> int:
        """退回还没发出去的那一笔运行级用量。

        与 `consume_budget` 同一套门禁：`_owned` 校验 owner、epoch 和租约未过期，
        并用 `FOR UPDATE` 锁住这一行。读出 `consumed_json`、减去 amount、写回，
        都在这一个事务里。旧执行者（租约已经被人接走）抛 LeaseLost，写不进去。

        不能改成无门禁的 `update_run`。那会让已经失去执行权的人改掉接管者的账。
        减完不会低于 0。其它用量键原样保留。
        """
        async with self._lease() as conn:
            row = await self._owned(conn, context, run_id=run_id)
            if row is None:
                raise BudgetExceeded('运行不存在')
            try:
                consumed = json.loads(row.get('consumed_json') or '{}')
            except ValueError:
                consumed = {}
            used = int(consumed.get(key) or 0)
            remaining = max(0, used - int(amount))
            consumed[key] = remaining
            await self._run(
                conn, 'UPDATE runs SET consumed_json=%s, updated_at=%s WHERE run_id=%s',
                (json.dumps(consumed, ensure_ascii=False), now(), run_id))
            return remaining
