"""动作账本：让"这次调用到底发生过没有"有据可查。

文档 §8 的核心诉求是**崩溃后能判断**：

| 崩溃时刻 | 恢复行为 |
|---|---|
| prepared 尚未发送 | 可以重发 |
| 已发送、回执未落库 | 状态未知，必须核实，不能盲目重发 |
| 已成功但检查点没推进 | 直接复用结果，不重复调用 |

两条纪律（本轮改造的重点）：

1. **准入与结算各自是一个事务**。以前准入要提交两次（建 action、置 in_flight），
   中间崩溃就留下一条 `prepared` 的"不知道发没发"的记录；现在
   `admit_action` 一次事务完成，`settle_action` 一次事务完成。
2. **执行确定性显式携带**（`app/errors.py`），不再按异常类名猜 unknown。

账本通过 ContextVar 传递：它是活对象（持有 Store 与执行上下文），不能塞进
graph state（那个会被 json 序列化落盘）。没有账本时（单元测试、CLI 直调）
所有记录动作自动变成 no-op。
"""
import hashlib
import json
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from app import errors as errors_module

logger = logging.getLogger(__name__)

_current: ContextVar['ActionLedger | None'] = ContextVar('action_ledger',
                                                        default=None)


def current_ledger() -> 'ActionLedger | None':
    return _current.get()


def bind(ledger: 'ActionLedger | None'):
    """把账本挂到当前上下文，返回用于解绑的 token。"""
    return _current.set(ledger)


def unbind(token) -> None:
    _current.reset(token)


@contextmanager
def use_ledger(ledger: 'ActionLedger | None'):
    token = _current.set(ledger)
    try:
        yield ledger
    finally:
        _current.reset(token)


def args_hash(args: dict) -> str:
    """参数指纹：幂等键要跟着最终参数走，参数变了就是另一个动作。"""
    body = json.dumps(args or {}, ensure_ascii=False, sort_keys=True,
                      default=str)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


class ActionLedger:
    """把一次执行里每个步骤的调用记录进 actions / action_attempts。"""

    def __init__(self, store, run_id: str, plan_version: int,
                 worker_id: str = 'local', context=None) -> None:
        self.store = store
        self.run_id = run_id
        self.plan_version = max(1, int(plan_version or 1))
        self.worker_id = worker_id
        # 执行权凭证：有它才走"受租约保护"的准入/结算
        self.context = context

    # ---- 幂等键 ----
    def key_of(self, step_id: str, tool_key: str, args: dict) -> str:
        """稳定身份：**含 tool_key**（同一个 step 换了工具就是另一个动作）。"""
        return f'{self.run_id}:{self.plan_version}:{step_id}:{tool_key}:{args_hash(args)}'

    async def admit(self, *, step_id: str, tool_key: str, effect: str,
                    args: dict, authorized_by: str = '',
                    expected_intent_version: int | None = None) -> dict:
        """取得调用准入。返回：

        - `{'status': 'admitted', 'action_id', 'attempt_id'}` 可以调用；
        - `{'status': 'succeeded', 'replay': 回执}` 之前已成功，直接复用；
        - `{'status': 'in_flight'|'unknown', 'blocked': 状态}` 结果未定，必须核实。

        `expected_intent_version` 必须来自**已提交的计划**（不是模型自报的版本）：
        仓储层会拿它和 `runs.intent_version` 比较，不相等抛 `IntentChanged`。
        这样"用户刚补了一句要求"与"旧计划要发下一个动作"由数据库裁决先后。
        """
        admitted = await self.store.admit_action(
            self.context, run_id=self.run_id, plan_version=self.plan_version,
            step_id=step_id, tool_key=tool_key,
            args_hash=args_hash(args), idempotency_key=self.key_of(step_id, tool_key, args),
            effect=effect, action_id='act_' + uuid.uuid4().hex[:16],
            attempt_id='att_' + uuid.uuid4().hex[:12],
            expected_intent_version=expected_intent_version)
        status = admitted.get('status')
        if status == 'admitted':
            return {'status': 'admitted',
                    'action_id': admitted['action_id'],
                    'attempt_id': admitted['attempt_id']}
        if status == 'succeeded':
            return {'status': 'succeeded', 'replay': admitted.get('replay')}
        if status in ('in_flight', 'unknown'):
            return {'status': status, 'blocked': status}
        return {'status': status or 'prepared', 'replay': None}

    async def settle(self, *, step_id: str, action_id: str,
                     attempt_id: str | None, status: str,
                     receipt: dict | None = None, error_type: str | None = None,
                     step_status: str | None = None, state: dict | None = None,
                     next_step_id: str = '') -> dict:
        return await self.store.settle_action(
            self.context, run_id=self.run_id, plan_version=self.plan_version,
            step_id=step_id, action_id=action_id, attempt_id=attempt_id,
            status=status, receipt=receipt, error_type=error_type,
            step_status=step_status, state=state, next_step_id=next_step_id)

    async def fail_from_exception(self, *, step_id: str, action_id: str,
                                  attempt_id: str | None, exc: BaseException,
                                  effect: str | None = None,
                                  state: dict | None = None,
                                  next_step_id: str = '') -> str:
        """按**显式确定性**结算失败，返回写入账本的状态。

        - not_executed → failed（确认没执行，可以显式重试）
        - failed_confirmed → failed（远端明确拒绝/报错）
        - unknown + 有副作用 → unknown（可能已执行：禁止自动重发，交给人核实）
        - unknown + 只读 → failed（重发一次没有可重复的后果，不必拦人）
        """
        outcome = errors_module.as_outcome(exc)
        blocking = errors_module.is_blocking(outcome.certainty, effect)
        status = 'unknown' if blocking else 'failed'
        await self.settle(step_id=step_id, action_id=action_id,
                          attempt_id=attempt_id, status=status,
                          receipt={'error_code': outcome.error_code,
                                   'phase': outcome.phase,
                                   'certainty': outcome.certainty,
                                   'effect': effect or ''},
                          error_type=outcome.error_code,
                          step_status=status, state=state,
                          next_step_id=next_step_id)
        return status
