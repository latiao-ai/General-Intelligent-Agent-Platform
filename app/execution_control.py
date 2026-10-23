"""执行控制：把"我是不是还有权推进这一次运行"变成可校验的东西。

为什么单独一个模块：**租约（lease）在数据库里，执行者却活在一个协程里**。两者之间
必须有一份明确的、可传递的执行权凭证，否则会出现这些真实事故：

- A 领取 epoch 1，卡了一下，租约过期；B 领取 epoch 2 开始执行；A 醒来继续写
  状态、派发下一步、把 run 标成 failed —— 覆盖了 B，等于两个执行者同时操作；
- 续租失败被当成"日志一行"，执行者继续跑，副作用照发；
- 计划只在收尾时才落库，执行中崩溃就既没有计划也没有步骤可恢复。

`ExecutionContext` 就是那份凭证：`(run_id, plan_version, lease_owner, lease_epoch)`。
所有推进都必须带它，仓储层用 `WHERE lease_owner=%s AND lease_epoch=%s` 做条件更新——
**旧代次更新影响零行就抛 `LeaseLost`**，绝不允许"写失败当成功"。

它通过 ContextVar 传递，而不是塞进 `AgentState`：后者会被 json 序列化落盘，
把执行权凭证写进状态文件等于把"我可以写"这张通行证一起存下来。
"""
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_current: ContextVar['ExecutionContext | None'] = ContextVar(
    'execution_context', default=None)


class LeaseLost(RuntimeError):
    """执行权已丢失（被别的执行者接手，或自己的租约已过期）。

    语义上**不是失败**：这一次执行不再有权写任何业务状态。调用方应当安静退出，
    由当前持租约的执行者或对账流程处理，不能把 run 标成 failed 假装是自己结束的。
    """

    def __init__(self, run_id: str, reason: str = '') -> None:
        super().__init__(f'run {run_id} 的执行权已丢失{("：" + reason) if reason else ""}')
        self.run_id = run_id
        self.reason = reason


class ControlStopped(RuntimeError):
    """控制请求要求停止派发（暂停/取消）。`status` 是期望的 run 状态。"""

    def __init__(self, reason: str, status: str = 'paused') -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class ExecutionContext:
    """一次运行的执行权凭证。**数据库判断，内存只做传递**。"""
    run_id: str
    plan_version: int = 1
    lease_owner: str = ''
    lease_epoch: int = 0

    @classmethod
    def from_claim(cls, claimed: dict, plan_version: int = 1) -> 'ExecutionContext':
        return cls(run_id=claimed['run_id'],
                   plan_version=int(plan_version or 1),
                   lease_owner=str(claimed.get('lease_owner') or ''),
                   lease_epoch=int(claimed.get('lease_epoch') or 0))

    def with_plan_version(self, version: int) -> 'ExecutionContext':
        return ExecutionContext(self.run_id, max(1, int(version or 1)),
                                self.lease_owner, self.lease_epoch)


def current() -> 'ExecutionContext | None':
    return _current.get()


def bind(context: 'ExecutionContext | None'):
    return _current.set(context)


def unbind(token) -> None:
    _current.reset(token)


@contextmanager
def use(context: 'ExecutionContext | None'):
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def require() -> 'ExecutionContext | None':
    """取当前执行上下文；没有就返回 None（CLI/单元测试直调时是合法的）。

    返回 None 时仓储层走"不加租约条件"的老路径——**只允许**在没有租约的场景
    （单机就地执行、测试）使用。生产路径（Worker/API 执行）一定有上下文。
    """
    return _current.get()


def lease_deadline(lease_seconds: float) -> float:
    return time.time() + max(1.0, float(lease_seconds))
