"""动作账本：让"这次调用到底发生过没有"有据可查。

文档 §8 的核心诉求是**崩溃后能判断**：

| 崩溃时刻 | 恢复行为 |
|---|---|
| prepared 尚未发送 | 可以重发 |
| 已发送、回执未落库 | 状态未知，必须核实，不能盲目重发 |
| 已成功但检查点没推进 | 直接复用结果，不重复调用 |

所以每个步骤在真正调用工具**之前**先落一条 action（prepared），调用前落一条
attempt（in_flight），回来后更新 action 与 attempt。重试只新增 attempt，
action 身份不变——幂等键挂在 action 上。

这里用 ContextVar 把账本传给图节点：它是活对象（持有 Store），不能塞进
graph state（那个会被 json 序列化落盘）。没有账本时（单元测试、CLI 直调）
所有记录动作自动变成 no-op。
"""
import hashlib
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

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
                 worker_id: str = 'local') -> None:
        self.store = store
        self.run_id = run_id
        self.plan_version = max(1, int(plan_version or 1))
        self.worker_id = worker_id

    # ---- 幂等键 ----
    def key_of(self, step_id: str, tool_key: str, args: dict) -> str:
        return f'{self.run_id}:{self.plan_version}:{step_id}:{args_hash(args)}'

    async def begin(self, *, step_id: str, tool_key: str, effect: str,
                    args: dict, authorized_by: str = '') -> dict:
        """准备一次动作。

        返回 {'action_id', 'replay'}：replay 非空表示这个动作之前已经成功过，
        直接用它的回执，不要再调用远端——这正是"已成功不重发"的落地。
        """
        key = self.key_of(step_id, tool_key, args)
        existing = await self.store.find_action(key)
        if existing is not None:
            if existing['status'] == 'succeeded':
                return {'action_id': existing['action_id'],
                        'replay': existing['receipt']}
            # 已发送但结果未定、或已判定未知：一律阻断，要求核实后再继续。
            # 只有 prepared（根本没发出去）才可以安全重发（文档 §8.3 窗口一）。
            if existing['status'] in {'in_flight', 'unknown'}:
                return {'action_id': existing['action_id'], 'replay': None,
                        'blocked': existing['status']}
            return {'action_id': existing['action_id'], 'replay': None}

        action_id = 'act_' + uuid.uuid4().hex[:16]
        await self.store.create_action(
            action_id, run_id=self.run_id, plan_version=self.plan_version,
            step_id=step_id, tool_key=tool_key, args_hash=args_hash(args),
            idempotency_key=key, effect=effect, authorized_by=authorized_by)
        return {'action_id': action_id, 'replay': None}

    async def in_flight(self, action_id: str) -> str:
        """真正发出请求之前调用：先把状态标成 in_flight 再发。

        顺序很重要——如果先发后标，崩溃就落在"已发送但没记录"的窗口里，
        恢复时无法判断到底发没发。
        """
        attempt_id = 'att_' + uuid.uuid4().hex[:12]
        await self.store.update_action(action_id, status='in_flight')
        await self.store.add_attempt(attempt_id, action_id)
        return attempt_id

    async def succeed(self, action_id: str, attempt_id: str | None,
                      receipt: dict | None) -> None:
        await self.store.update_action(action_id, status='succeeded',
                                       receipt_json=receipt or {})
        if attempt_id:
            await self.store.finish_attempt(attempt_id, result_ref='inline')

    async def fail(self, action_id: str, attempt_id: str | None,
                   error_type: str) -> None:
        await self.store.update_action(action_id, status='failed')
        if attempt_id:
            await self.store.finish_attempt(attempt_id, error_type=error_type)

    async def mark_unknown(self, action_id: str, attempt_id: str | None,
                           error_type: str) -> None:
        """结果不确定（超时、连接中断）：绝不能当成失败去重试。

        文档 §8.3：远端没有幂等与核实能力时，这一步必须保持 unknown，
        要求外部核实或人工处理，系统要如实报告不确定。
        """
        await self.store.update_action(action_id, status='unknown')
        if attempt_id:
            await self.store.finish_attempt(attempt_id, error_type=error_type)
