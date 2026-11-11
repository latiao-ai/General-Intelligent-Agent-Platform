"""定时任务：时间语义、触发判定、派发、投递。

对外只暴露三个入口：

- `core.rrules`：规则解析与下一次触发计算（纯函数）；
- `scheduler.tick.tick(hub, worker_id)`：Worker 主循环每 30s 调一次；
- `scheduler.tick.run_now(...)` / `scheduler.delivery.deliver_job(...)`：手动触发与投递。

调度器**不自己执行 agent**：一切执行都走 `Hub.start()`，与用户手点一条任务同一条路径。
"""
from webpage_agent.scheduler import delivery, tick  # noqa: F401
