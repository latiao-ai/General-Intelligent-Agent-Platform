"""PostgreSQL 持久化：与 SQLite 实现同一套仓储接口。

设计取舍：

- **连接池**（`psycopg_pool`）而不是"一个进程一条连接 + 一把锁"。每次调用
  借一条连接，用完立即归还；池大小与等待超时都可配（`DB_POOL_SIZE` /
  `DB_POOL_TIMEOUT`）。多线程读写因此能真正并行；
- **并发正确性不靠应用锁，靠数据库**：租约 + `lease_epoch` fencing、
  `FOR UPDATE SKIP LOCKED` 领取、事件序号靠主键冲突重算。应用层的锁一旦
  和事务边界纠缠不清，就会变成"看起来安全、实际把吞吐锁死"；
- **事务边界显式写在每个方法里**（`_lease()` 里提交/回滚），不依赖连接池的
  隐式提交；
- **事件日志以数据库为准，内存只负责实时投递**：这样进程重启后客户端仍能拿到
  完整的执行过程（见 webpage_agent/api/api_hub.py 的 subscribe/去重逻辑）；
- 启动时把库里遗留的 `running` 运行交给对账（`reconcile_orphan_runs`）——
  进程重启后不可能还有任务在跑，否则前端会一直等一个永远不会结束的任务。
"""

import json
from datetime import datetime, timezone

from webpage_agent.governance.core import GovernanceStore
from webpage_agent.store.pg_schema import MIGRATIONS as MIGRATIONS
from webpage_agent.store.pg_schema import SCHEMA as SCHEMA

class SessionGoalConflict(RuntimeError):
    """会话已有未结束目标；调用方应向该 run 提交补充。"""

    def __init__(self, task_id: str, run_id: str) -> None:
        super().__init__('会话已有未结束目标，请向当前运行提交补充要求')
        self.task_id, self.run_id = task_id, run_id


class TicketAlreadyUsed(RuntimeError):
    """同一张票据被第二次兑换（含并发）。调用方应回 409。"""


class TurnRequestConflict(RuntimeError):
    """同一个 session 的 request_id 已用于不同内容（请求指纹不同）。调用方回 409。"""

    def __init__(self, request_id: str) -> None:
        super().__init__('该 request_id 已用于不同内容的请求')
        self.request_id = request_id


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 定义在 core.errors。按原名再导出，旧路径拿到的仍是同一个类对象；
# 在这里重写一遍会让 except / isinstance 对不上。
from webpage_agent.core.errors import BudgetExceeded as BudgetExceeded
from webpage_agent.core.errors import DecisionBudgetExceeded as DecisionBudgetExceeded


def _decode_run(row: dict) -> dict:
    row['budget'] = json.loads(row.pop('budget_json') or '{}')
    row['consumed'] = json.loads(row.pop('consumed_json') or '{}')
    return row


def _decode_action(row: dict) -> dict:
    row['receipt'] = json.loads(row.pop('receipt_json') or 'null')
    return row


def _decode_request_key(row: dict) -> dict:
    row['response'] = json.loads(row.pop('response_json') or 'null')
    return row


def _decode_credential(row: dict) -> dict:
    """凭据绑定解码：scope_json → scope。**这里不出现明文密钥**。"""
    row['scope'] = json.loads(row.pop('scope_json') or '{}')
    return row


def _decode_policy(row: dict) -> dict:
    """政策解码：limits_json → limits，enabled → bool。"""
    row['limits'] = json.loads(row.pop('limits_json') or '{}')
    row['enabled'] = bool(row.get('enabled'))
    return row


def _decode_manifest(row: dict) -> dict:
    """结果 manifest 解码：summary_json / completeness_json → dict。"""
    row['summary'] = json.loads(row.pop('summary_json') or '{}')
    row['completeness'] = json.loads(row.pop('completeness_json') or '{}')
    return row


# 连接 mixin 在 GovernanceStore 之前，四个抽象方法由 PgConnection 实现。
# 其余 mixin 没有同名方法，顺序只决定 MRO 里的展示。
from webpage_agent.store.pg_conn import PgConnection
from webpage_agent.store.pg_auth import PgAuth
from webpage_agent.store.pg_tasks import PgTasks
from webpage_agent.store.pg_runs import PgRuns
from webpage_agent.store.pg_budget import PgBudget
from webpage_agent.store.pg_actions import PgActions
from webpage_agent.store.pg_jobs import PgJobs
from webpage_agent.store.pg_governance import PgGovernance


class PostgresStore(
    PgConnection,
    PgAuth,
    PgTasks,
    PgRuns,
    PgBudget,
    PgActions,
    PgJobs,
    PgGovernance,
    GovernanceStore,
):
    """PostgreSQL 仓储实现（满足 `webpage_agent.store.persistence.Repository`），**全异步**。

    连接用法：`psycopg_pool.AsyncConnectionPool` + 每次调用借一条。以前是
    "一个进程一条连接 + 一把 threading.Lock"，把所有 DB 访问串行化，而且同步
    调用会直接阻塞事件循环；现在借连接、执行、提交全部 await，等 IO 的时候
    事件循环可以继续跑别的协程（SSE 推送、其他任务）。

    并发正确性仍然**不靠应用锁，靠数据库**：租约 + `lease_epoch` fencing、
    `claim_run` 的 `FOR UPDATE SKIP LOCKED`、事件序号用 `pg_advisory_xact_lock`
    按任务串行化。所以多进程、多协程同时用一个库是安全的。

    构造本身不碰 IO（连接池以 `open=False` 创建），真正的初始化在第一次
    `await ready()` 里做：打开池 → 建表 → 对账。这样 `PostgresStore(dsn)` 可以
    在模块导入期构造，服务启动时再用 `await store.ready()` 把失败提前暴露。
    """

