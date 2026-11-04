"""运行预算用尽。

这两个异常原来写在 `store.pg_store`。agent 和 governance 都要抛、要接，
定义留在 store 的话，上层就得为了一个异常类去依赖存储实现。

`BudgetExceeded` 上的 `error_code` / `certainty` / `phase` 会被
`observe.errors.as_outcome` 收成执行结果。缺了 `certainty` 会按 unknown
处理：本可以安全重试的预算拦截，变成需要人工核实。这三个是类属性，
搬家时必须原样留在类上。
"""


class DecisionBudgetExceeded(RuntimeError):
    """自主决策预算用尽：保存已完成事实，等人显式继续（不是失败）。"""

    def __init__(self, message: str = '决策预算用尽') -> None:
        super().__init__(message)
        self.safe_message = message


class BudgetExceeded(RuntimeError):
    """运行级预算（provider 请求数 / 工具调用数 …）用尽。"""

    error_code = 'E_RUN_BUDGET'
    certainty = 'not_executed'
    phase = 'validate'

    def __init__(self, message: str = '运行预算用尽') -> None:
        super().__init__(message)
        self.safe_message = message
