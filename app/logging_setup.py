"""统一日志入口。

为什么要单独一个模块：全仓此前**一条 logging 都没有**，排障只能翻 events 表，
拿不到"哪个 run / step 慢、失败率多少"。日志不是功能，但没有它，前面那些
租约、账本、对账状态只能靠人肉猜。

约定：
- 一条日志带 `run=xxx` 这样的键值，方便 grep；
- 默认 INFO，`LOG_LEVEL=DEBUG` 可开详细；
- 已经有人配过 handler（uvicorn --reload 会重复导入模块）就不再叠加，
  否则同一条日志会打印两遍。
"""
import logging
import os
import sys

FORMAT = '%(asctime)s %(levelname)-7s %(name)s %(message)s'


def setup_logging(component: str, level: str | None = None) -> None:
    """安装一次根 logger；重复调用是幂等的。"""
    name = (level or os.getenv('LOG_LEVEL', 'INFO')).upper()
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
    root.setLevel(getattr(logging, name, logging.INFO))
    logging.getLogger(component).debug('日志就绪 component=%s level=%s',
                                       component, name)
