"""工具错误的类型化合同：**执行确定性不许靠异常类名去猜**。

以前的写法是 `if type(exc).__name__ in {'TimeoutError', 'ConnectError', ...}` 判
"结果未定"。它有两个致命问题：

1. 工具把底层异常包成 `ToolError` 之后，类型名就丢了，判断永远不命中；
2. 反过来，任何恰好叫这个名字的异常都会被当成"未定"，包括本地校验错误。

所以确定性必须是**显式携带**的字段，由最了解现场的那一层标注：

| 字段 | 取值 | 谁标注 |
|---|---|---|
| `phase` | validate / connect / dispatch / receive / decode | 工具或适配器 |
| `certainty` | not_executed / failed_confirmed / unknown | 工具或适配器 |
| `error_code` | 稳定字符串（E_*、LeaseLost…） | 工具或适配器 |

默认值刻意选 **unknown**：底层说不清"请求到底发出去没有"时，宁可让人类核实，
也不要自动重发一次可能有副作用的调用。
"""
from dataclasses import dataclass, field

PHASES = ('validate', 'connect', 'dispatch', 'receive', 'decode')
CERTAINTIES = ('not_executed', 'failed_confirmed', 'unknown')

# 默认：说不清就是未定
DEFAULT_CERTAINTY = 'unknown'

# 会产生副作用的效果类型。"结果未定"只在**有副作用**时才是拦路虎：只读调用重发
# 一次没有可重复的后果，而有副作用的调用重发可能再造一个站点。
SIDE_EFFECT_EFFECTS = ('write', 'destructive', 'external', 'unknown')


def has_side_effects(effect: str | None) -> bool:
    """重发一次是否可能留下第二份副作用。

    空串 / read / none 明确安全；其余（含未标注的 unknown）一律按有副作用处理——
    宁可多拦一次，也不要在说不清的地方悄悄重发。
    """
    return (effect or 'unknown').lower() in SIDE_EFFECT_EFFECTS


def is_blocking(certainty: str, effect: str | None) -> bool:
    """结果未定 + 有副作用 = 必须人工核实，不能自动重试。"""
    return certainty == 'unknown' and has_side_effects(effect)


@dataclass(frozen=True)
class ExecutionOutcome:
    """一次调用的执行确定性。账本与恢复流程只认这个。"""
    certainty: str = DEFAULT_CERTAINTY
    phase: str = ''
    error_code: str = ''
    receipt_ref: str = ''
    safe_message: str = ''

    def __post_init__(self) -> None:
        if self.certainty not in CERTAINTIES:
            raise ValueError(f'未知的执行确定性：{self.certainty}')
        if self.phase and self.phase not in PHASES:
            raise ValueError(f'未知的执行阶段：{self.phase}')

    @property
    def retryable(self) -> bool:
        """只有"确认没执行"才可以安全重试；failed_confirmed 与 unknown 都不行。"""
        return self.certainty == 'not_executed'


def as_outcome(exc: BaseException) -> ExecutionOutcome:
    """从异常里取确定性；没标注过就按 unknown 处理（保守）。"""
    certainty = getattr(exc, 'certainty', '') or DEFAULT_CERTAINTY
    if certainty not in CERTAINTIES:
        certainty = DEFAULT_CERTAINTY
    return ExecutionOutcome(
        certainty=certainty,
        phase=getattr(exc, 'phase', '') or '',
        error_code=getattr(exc, 'error_code', '') or type(exc).__name__,
        receipt_ref=getattr(exc, 'receipt_ref', '') or '',
        safe_message=getattr(exc, 'safe_message', '') or type(exc).__name__)
