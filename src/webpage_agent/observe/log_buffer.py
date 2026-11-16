"""进程内日志环形缓冲 + HTTP 指标，给管理控制台的「日志」页做实时源。

## 为什么不直接 tail 文件

控制台要的是「刚刚发生的那一条」，而不是「磁盘上已有的那些行」。tail 文件有两个
绕不过去的麻烦：

1. **轮转**：RotatingFileHandler 一 rename，按偏移量读的 tailer 会读到一半就断，
   要自己 inode 比对、重开文件，代码量比内存缓冲还大；
2. **结构化字段丢失**：文件里 trace_id 是拼在文本里的一列，读回来还得再解析一遍，
   而内存里本来就是结构化对象。

所以分工是：**内存留最近 N 条给实时视图，文件留给历史、告警和进程死后取证**。

## 多进程的现实

API 与 Worker 是两个进程，各自一份缓冲。控制台只连得上 API 那个进程，因此：

- `source=api`：读本进程的缓冲（含 http_span，能实时刷）；
- `source=worker`：读 `logs/worker.log` 的尾部（见 `console_api.console_logs`）。

别指望一份缓冲能装下两个进程 —— 真要那样得上 Redis 或 Loki，那是另一个量级的
部署负担，对一个自托管中台来说不划算。

## 线程安全

写日志的线程不止事件循环一个（`asyncio.to_thread`、同步 SDK 回调都在别的线程里
写）。deque 的 append 本身是原子的，但「加锁 + 计数」必须在一起，否则统计值会漂。
订阅者队列用 `loop.call_soon_threadsafe` 投递 —— `asyncio.Queue.put_nowait` 从
非事件循环线程调用是不安全的，跨线程直接 put 偶发丢消息就是这么来的。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import Counter, deque

# 进程启动时刻。uptime 用单调时钟算，不受系统改时间影响。
_STARTED = time.monotonic()
_STARTED_WALL = time.time()

LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
_ORDER = {name: index * 10 for index, name in enumerate(LEVELS)}
_ALIASES = {'WARN': 'WARNING', 'FATAL': 'CRITICAL', 'EXCEPTION': 'ERROR'}


def _env_int(name: str, default: int) -> int:
    try:
        return max(50, int(os.getenv(name, '') or default))
    except ValueError:
        return default


def normalise_level(name: str) -> str:
    """把日志里的各种写法收成五档。认不出来就当 INFO。"""
    text = (name or '').strip().upper()
    text = _ALIASES.get(text, text)
    return text if text in _ORDER else 'INFO'


class LogBuffer:
    """有界缓冲。满了丢最旧的 —— 排障看的是最近，不是最早。"""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._entries: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._dropped = 0
        self._counts: Counter = Counter()
        self._subscribers: set = set()

    # ---------- 写入 ----------

    def append(self, *, level: str, name: str, message: str,
               trace: str = '', run: str = '') -> None:
        level = normalise_level(level)
        with self._lock:
            self._seq += 1
            entry = {
                'seq': self._seq,
                'ts': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())
                      + f'.{int(time.time() * 1000) % 1000:03d}',
                'epoch': time.time(),
                'level': level,
                'logger': name,
                'message': message,
                'trace': trace,
                'run': run,
            }
            if len(self._entries) == self.capacity:
                # deque 自己会丢，但丢了多少要记下来：控制台显示「已丢弃 N 条」
                # 比默默少几条更值得信。
                self._dropped += 1
            self._entries.append(entry)
            self._counts[level] += 1
            targets = tuple(self._subscribers)

        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(self._deliver, queue, entry)
            except RuntimeError:
                # 事件循环已经关了（进程退出中），订阅者下次自己会掉线。
                pass

    @staticmethod
    def _deliver(queue: 'asyncio.Queue', entry: dict) -> None:
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            # 慢消费者不能把生产者拖住，也不能把内存吃光：丢这一条，
            # 页面上表现为「卡了一下」，比拖垮整个 API 强。
            pass

    # ---------- 读取 ----------

    def entries(self, *, level: str = '', query: str = '', limit: int = 200,
                after: int = 0) -> list:
        """按最低级别 + 关键字过滤，最新的在前（after>0 时按 seq 递增返回）。"""
        floor = _ORDER.get(normalise_level(level), 0) if level else 0
        needle = (query or '').strip().lower()
        with self._lock:
            snapshot = tuple(self._entries)
        rows = [item for item in snapshot
                if _ORDER.get(item['level'], 0) >= floor
                and item['seq'] > after
                and (not needle or needle in item['message'].lower()
                     or needle in item['logger'].lower()
                     or needle in item['trace'].lower())]
        if after:
            return rows[:limit]
        return rows[-limit:][::-1]

    def stats(self) -> dict:
        with self._lock:
            size = len(self._entries)
            seq = self._seq
            dropped = self._dropped
            counts = dict(self._counts)
            recent = tuple(self._entries)
        latest = recent[-1]['seq'] if recent else 0
        return {'capacity': self.capacity, 'size': size, 'seq': seq,
                'latest_seq': latest, 'dropped': dropped, 'counts': counts}

    # ---------- 订阅 ----------

    def subscribe(self, maxsize: int = 500):
        """返回一个上下文管理器，产出 asyncio.Queue。用法见 console_api 的 SSE。"""
        return _Subscription(self, maxsize)


class _Subscription:
    def __init__(self, buffer: LogBuffer, maxsize: int) -> None:
        self._buffer = buffer
        self._maxsize = maxsize
        self._loop = None
        self.queue: asyncio.Queue | None = None

    def __enter__(self) -> 'asyncio.Queue':
        self._loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=self._maxsize)
        with self._buffer._lock:
            self._buffer._subscribers.add((self._loop, self.queue))
        return self.queue

    def __exit__(self, *exc_info) -> None:
        with self._buffer._lock:
            self._buffer._subscribers.discard((self._loop, self.queue))
        return None


BUFFER = LogBuffer(_env_int('LOG_BUFFER_SIZE', 2000))


class HttpMetrics:
    """最近一段时间的请求量、错误率、慢请求。只留窗口内的，不做长期存储。

    为什么不用现成的 Prometheus client：这个中台的自托管形态里没有 scrape 端，
    指标的唯一消费者就是控制台那一屏。多一个依赖、多一条 /metrics 暴露面，
    换来的只是一份没人抓的时序数据。
    """

    WINDOW_SECONDS = 300

    def __init__(self, window: int = WINDOW_SECONDS, capacity: int = 5000) -> None:
        self.window = window
        self._events: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._total = 0
        self._by_class = Counter()

    def record_http(self, *, status: int, duration: float, route: str = '') -> None:
        now = time.monotonic()
        with self._lock:
            self._total += 1
            self._by_class[f'{int(status) // 100}xx'] += 1
            self._events.append((now, int(status), float(duration), route))
        self._trim(now)

    def _trim(self, now: float | None = None) -> None:
        cutoff = (now or time.monotonic()) - self.window
        with self._lock:
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

    def snapshot(self) -> dict:
        self._trim()
        with self._lock:
            events = tuple(self._events)
            total = self._total
            by_class = dict(self._by_class)
        window = self.window
        durations = sorted(item[2] for item in events)
        count = len(durations)
        slow = Counter()
        for _, _, duration, route in events:
            if duration >= 1.0:
                slow[route or 'unmatched'] += 1
        statuses = Counter(item[1] for item in events)
        errors = sum(n for code, n in statuses.items() if code >= 500)
        return {
            'window_seconds': window,
            'total_since_start': total,
            'total_by_class': by_class,
            'window_requests': count,
            'window_qps': round(count / window, 3) if window else 0,
            'window_errors': errors,
            'window_error_rate': round(errors / count, 4) if count else 0.0,
            'window_p50_ms': round(durations[int(count * 0.5)] * 1000, 1) if count else 0.0,
            'window_p95_ms': round(durations[min(count - 1, int(count * 0.95))] * 1000, 1) if count else 0.0,
            'window_slow': [{'route': route, 'count': n}
                            for route, n in slow.most_common(5)],
        }


METRICS = HttpMetrics()


def record_http(*, status: int, duration: float, route: str = '') -> None:
    METRICS.record_http(status=status, duration=duration, route=route)


def process_snapshot() -> dict:
    """进程本身的几个数：uptime、pid、轮转后的日志目录。"""
    return {'pid': os.getpid(),
            'uptime_seconds': int(time.monotonic() - _STARTED),
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%S',
                                        time.localtime(_STARTED_WALL))}
