"""统一日志入口。

为什么要单独一个模块：全仓此前**一条 logging 都没有**，排障只能翻 events 表，
拿不到"哪个 run / step 慢、失败率多少"。日志不是功能，但没有它，前面那些
租约、账本、对账状态只能靠人肉猜。

约定：
- 一条日志带 `run=xxx` 这样的键值，方便 grep；
- 默认 INFO，`LOG_LEVEL=DEBUG` 可开详细，运行期可从控制台的日志页临时调；
- 已经有人配过 handler（uvicorn --reload 会重复导入模块）就不再叠加，
  否则同一条日志会打印两遍。

## 三个出口，各管一段

| 出口 | 谁看 | 为什么需要 |
|---|---|---|
| stdout | 终端、容器日志 | 进程被 kill 之后唯一还留着的东西 |
| `artifacts/logs/<component>.log` | 排障、取证 | 终端滚过去就没了；按 10MB × 5 份轮转 |
| 进程内环形缓冲 | 控制台「日志」页 | 实时看最新一条，不用 tail、不用登机器 |

三处用**同一个 Formatter**，所以同一条日志在三处长得一样（生产是 JSON）。
之前 dev 格式里没有 trace_id：中间件明明算了 trace、也写进了响应头，日志里却看不见，
拿 `traceparent` 去对日志等于对不上 —— 这是这次补的第一件事。
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from webpage_agent.observe import log_buffer

# `trace=` 放在消息前面而不是末尾：日志是左对齐扫的，尾部一列在长消息上会被
# 挤到屏幕外，扫不到。
FORMAT = '%(asctime)s %(levelname)-7s %(name)s trace=%(trace)s %(message)s'

# 轮转参数可由环境变量覆盖：出事时第一反应往往是"再多留一点"，
# 那不该需要改代码重新发版。
_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5

# 这些 logger 的日志不进食环形缓冲，否则控制台会自己喂自己：
# 每来一条就记一次"读到了"，又产生一条。
_SELF_LOGGERS = ('webpage_agent.observe.logging_setup',
                 'webpage_agent.observe.log_buffer')


class TextFormatter(logging.Formatter):
    """开发用的人类可读格式，但把 trace 的短号带上。

    只取前 8 位：完整 trace 是 32 位十六进制，一屏放三个就看不见消息了。
    排障时用前 8 位 grep，要完整值去响应头 `X-Request-ID` 或 JSON 里拿。
    """

    def format(self, record: logging.LogRecord) -> str:
        from webpage_agent.observe import telemetry
        record.trace = (telemetry.trace_id.get() or '-')[:8]
        return super().format(record)


class BufferHandler(logging.Handler):
    """把日志推进进程内环形缓冲，控制台的实时流从这里读。"""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in _SELF_LOGGERS:
            return
        try:
            from webpage_agent.observe import telemetry
            message = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                # 缓冲里只附异常类型：完整栈在文件和 stdout 里，控制台每行
                # 塞 40 行栈会把整页刷没。
                message = f'{message} | {record.exc_info[0].__name__}'
            log_buffer.BUFFER.append(
                level=record.levelname, name=record.name, message=message,
                trace=telemetry.trace_id.get(), run=telemetry.run_id.get())
        except Exception:                                   # noqa: BLE001
            # 日志通道自己出错绝不能把业务带崩。这里也不调 handleError：
            # stderr 可能正是坏掉的那一环。
            pass


def log_dir() -> Path:
    """日志目录。`LOG_DIR` 显式配置优先，否则落在 artifacts/logs。"""
    raw = os.getenv('LOG_DIR', '').strip()
    if raw:
        return Path(raw).expanduser()
    from webpage_agent.core.config import settings
    return Path(settings.artifact_dir) / 'logs'


def file_logging_enabled() -> bool:
    """要不要落文件。

    默认落，但 **APP_ENV=test 与 pytest 下不落**：CI 里每跑一个用例就在仓库里
    建一个 logs 目录毫无意义，还会让"跑完测试 git status 变脏"变成一个需要
    解释的现象。显式关掉用 `LOG_TO_FILE=0`。
    """
    flag = os.getenv('LOG_TO_FILE', '').strip().lower()
    if flag in ('0', 'false', 'no', 'off'):
        return False
    if flag in ('1', 'true', 'yes', 'on'):
        return True
    if os.getenv('PYTEST_CURRENT_TEST'):
        return False
    return os.getenv('APP_ENV', 'development').strip().lower() != 'test'


def _make_formatter() -> logging.Formatter:
    from webpage_agent.observe.telemetry import JsonFormatter
    if os.getenv('APP_ENV', '').strip().lower() == 'production':
        return JsonFormatter()
    return TextFormatter(FORMAT)


def _root_level(name: str | None = None) -> int:
    raw = (name or os.getenv('LOG_LEVEL', 'INFO')).upper()
    return getattr(logging, raw, logging.INFO)


def setup_logging(component: str, level: str | None = None) -> None:
    """安装一次根 logger；重复调用是幂等的。"""
    name = (level or os.getenv('LOG_LEVEL', 'INFO')).upper()
    root = logging.getLogger()
    formatter = _make_formatter()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    # 按标记属性判断而不是按 handler 类型：uvicorn --reload 会重新导入这个模块，
    # 原来那套 `if not root.handlers` 只在"一个 handler 都没有"时拦住，
    # 一旦有 stdout 就不会再加文件 handler —— 顺序一变行为就变。
    if not any(getattr(item, '_webpage_buffer', False) for item in root.handlers):
        buffer_handler = BufferHandler()
        buffer_handler._webpage_buffer = True
        buffer_handler.setLevel(logging.DEBUG)
        root.addHandler(buffer_handler)
    if file_logging_enabled() and not any(
            getattr(item, '_webpage_file', False) for item in root.handlers):
        try:
            target = log_dir()
            target.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                target / f'{component}.log', maxBytes=_MAX_BYTES,
                backupCount=_BACKUPS, encoding='utf-8')
            file_handler.setFormatter(formatter)
            file_handler._webpage_file = True
            root.addHandler(file_handler)
        except OSError as exc:
            # 目录只读 / 磁盘满：能跑就得继续跑，只是没有历史文件而已。
            root.warning('文件日志不可用，仅写 stdout：%s', exc)
    root.setLevel(_root_level(name))
    # httpx 每个请求打一行 INFO，量大且没有排障价值（真正的请求证据是 http_span）。
    # 压到 WARNING，只保留它出问题时的声音。
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger(component).debug('日志就绪 component=%s level=%s dir=%s',
                                       component, name,
                                       log_dir() if file_logging_enabled() else '-')


def set_level(name: str) -> str:
    """运行期改级别，返回实际生效的值。控制台的日志页用它。

    只改根 logger：各子 logger 都没有单独设过级别，根一改就全都跟着改。
    重启后回到 `LOG_LEVEL` 的值 —— 这是有意的，"临时调成 DEBUG 忘了调回来"
    不该变成持久的线上配置。
    """
    text = (name or '').strip().upper()
    if text not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        raise ValueError(f'未知日志级别：{name}')
    logging.getLogger().setLevel(getattr(logging, text))
    return current_level()


def current_level() -> str:
    return logging.getLevelName(logging.getLogger().level)
