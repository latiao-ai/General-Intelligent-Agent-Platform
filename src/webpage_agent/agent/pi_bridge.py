"""Pi 桥接：子进程生命周期、管道、待响应请求、超时与清理。

进程模型（第三阶段）：一次运行独占一个 Node 子进程和一个 Pi session，执行结束
后清理。不做进程池、不跨请求复用，避免混用上下文、回调和凭据。

并发设计（读管道不能被工具执行阻塞）：

    - 一个后台任务**持续**读 stdout，逐行解码后放进 asyncio.Queue；
    - 一个后台任务**持续**排空 stderr，只保留有界诊断尾部（避免管道写满卡死）；
    - 一个后台任务监控子进程退出；
    - 宿主（PiRuntime）从队列取消息驱动流程；工具执行在宿主侧完成，读任务不受影响。

统一终结机制（第四阶段，防止子进程退出后永久等待）：

    stdout EOF、读取异常、非法协议、子进程退出，任一发生都会「终结」本次桥接：

        - 用一个内部 `_SENTINEL` 哨兵写入队列，唤醒所有正在 `next_message()` 上
          等待的调用方；它们在哨兵处要么先消费完残留的有效消息，要么抛
          `PiBridgeError`，绝不永久挂起；
        - 已收到的有效结果（如 finished）**先消费**，不会被紧随其后的退出误判
          成失败——哨兵排在有效消息之后，天然保证顺序；
        - 终结后 `send()` 拒绝新请求，`close()` 可重复调用（幂等）。

取消与崩溃（明确失败，不自动重放）：

    - 取消时发 cancel，宽限期内等待退出，超时 terminate，再超时 kill，并等待回收；
    - 子进程意外退出时，拒绝全部未完成等待并返回可识别错误，**不**自动重启重发；
    - 宿主 CancelledError / LeaseLost / ControlStopped 原样向上传播。
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

from webpage_agent.agent import pi_protocol as proto
from webpage_agent.agent.pi_protocol import ProtocolError

logger = logging.getLogger(__name__)

# 子进程路径：pi-runtime/dist/main.js，相对仓库根。
_NODE_SCRIPT = Path(__file__).resolve().parents[3] / "pi-runtime" / "dist" / "main.js"

# 诊断尾部最多保留多少行（stderr 只留有界尾巴）。
_STDERR_TAIL_LINES = 200

# 队列终结哨兵：EOF / 读异常 / 非法协议 / 子进程退出时放入，唤醒等待方。
_SENTINEL = object()


async def _close_subprocess_transport(proc: asyncio.subprocess.Process) -> None:
    """关掉 asyncio 子进程传输，并让 close 回调在当前循环里跑完。"""
    transport = getattr(proc, '_transport', None)
    if transport is None or transport.is_closing():
        return
    try:
        transport.close()
    except Exception:  # noqa: BLE001 — 清理失败不能盖住原来的结束原因
        logger.debug('关闭子进程传输失败', exc_info=True)
        return
    await asyncio.sleep(0)


class PiBridgeError(Exception):
    """桥接层错误：启动失败、子进程崩溃、超时等，可被宿主识别。"""

    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


class PiBridge:
    """管理一个 Pi Node 子进程的启动、收发消息与清理。"""

    def __init__(
        self,
        *,
        node_script: Path | None = None,
        startup_timeout: float = 30.0,
        graceful_timeout: float = 5.0,
        kill_timeout: float = 3.0,
        max_message_bytes: int = 16 * 1024 * 1024,
        env: dict[str, str] | None = None,
    ) -> None:
        self._node_script = node_script or _NODE_SCRIPT
        self._startup_timeout = startup_timeout
        self._graceful_timeout = graceful_timeout
        self._kill_timeout = kill_timeout
        self._max_message_bytes = max_message_bytes
        self._extra_env = env or {}
        self.cwd: str | None = None

        self.execution_id = uuid.uuid4().hex
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._exit_code: Optional[int] = None
        self._closed = False
        self._sentinel_enqueued = False
        self._termination_reason: Optional[str] = None

    # ---- 生命周期 ----

    async def start(self) -> None:
        """启动子进程并等待 ready。失败抛 PiBridgeError。"""
        if self._proc is not None:
            return

        env = dict(os.environ)
        env["PI_EXECUTION_ID"] = self.execution_id
        env.update(self._extra_env)

        try:
            self._proc = await asyncio.create_subprocess_exec(
                "node", str(self._node_script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.cwd or None,
            )
        except (FileNotFoundError, OSError) as exc:
            raise PiBridgeError(f"无法启动 Node 子进程：{exc}") from exc

        assert self._proc.stdout is not None
        assert self._proc.stderr is not None

        # 三个后台任务：读 stdout、排空 stderr、监控退出
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._monitor_task = asyncio.create_task(self._monitor_exit())

        # 等待 ready
        try:
            msg = await asyncio.wait_for(
                self._queue.get(), timeout=self._startup_timeout
            )
        except asyncio.TimeoutError:
            await self._force_close()
            raise PiBridgeError(
                f"启动超时（{self._startup_timeout}s 内未收到 ready）",
                stderr_tail=self.stderr_tail(),
            ) from None

        if msg is _SENTINEL:
            await self._force_close()
            raise PiBridgeError(
                "启动期间子进程已退出或协议错误",
                stderr_tail=self.stderr_tail(),
            )
        if msg.get("type") != "ready":
            await self._force_close()
            raise PiBridgeError(
                f"启动消息异常：期望 ready，收到 {msg.get('type')!r}",
                stderr_tail=self.stderr_tail(),
            )
        self._closed = False

    async def _read_stdout(self) -> None:
        """持续读 stdout，逐行解码进队列。EOF/读异常/非法协议触发终结。"""
        assert self._proc and self._proc.stdout
        try:
            while True:
                try:
                    line = await self._proc.stdout.readline()
                except ValueError:
                    break  # 管道关闭
                if not line:
                    break  # EOF
                if len(line) > self._max_message_bytes:
                    # 超限消息：明确报错并终结，不能静默丢弃导致等待方挂起
                    self._terminate(
                        f"消息超过大小上限（{self._max_message_bytes} 字节）"
                    )
                    return
                try:
                    msg = proto.decode(line)
                    # 接入真实收发路径的协议校验：每条入站消息都校验版本、
                    # execution_id、type 及按类型分发的字段，非法即终结，绝不
                    # 把未校验的消息放进队列（否则等待方可能拿到脏消息）。
                    proto.validate_inbound(msg, self.execution_id)
                except ProtocolError as exc:
                    # 非法协议：终结本次执行，唤醒所有等待方
                    self._terminate(f"非法协议消息：{exc}")
                    return
                await self._queue.put(msg)
        except asyncio.CancelledError:
            return
        finally:
            # EOF / 异常退出读循环 → 唤醒等待方
            self._terminate("stdout 已关闭（子进程退出）")

    def _terminate(self, reason: str) -> None:
        """终结桥接：记录原因，并（首次时）放哨兵唤醒所有等待方。"""
        if self._termination_reason is None:
            self._termination_reason = reason
        if not self._sentinel_enqueued:
            self._sentinel_enqueued = True
            self._queue.put_nowait(_SENTINEL)

    async def _drain_stderr(self) -> None:
        """持续排空 stderr，只保留有界尾部。"""
        assert self._proc and self._proc.stderr
        try:
            while True:
                try:
                    line = await self._proc.stderr.readline()
                except ValueError:
                    break
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                self._stderr_tail.append(text)
        except asyncio.CancelledError:
            return

    async def _monitor_exit(self) -> None:
        """监控子进程退出，退出时唤醒等待方。"""
        assert self._proc
        try:
            self._exit_code = await self._proc.wait()
        except asyncio.CancelledError:
            return
        self._terminate(
            f"子进程已退出（code={self._exit_code}）"
        )

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    # ---- 收发 ----

    async def send(self, msg: dict[str, Any]) -> None:
        """写一条消息到子进程 stdin。已终结则报错。"""
        if self._proc is None or self._proc.stdin is None:
            raise PiBridgeError("子进程未启动或 stdin 不可用")
        if self._proc.returncode is not None:
            raise PiBridgeError(
                f"子进程已退出（code={self._proc.returncode}）",
                stderr_tail=self.stderr_tail(),
            )
        if self._termination_reason is not None:
            raise PiBridgeError(
                f"桥接已终结（{self._termination_reason}），拒绝新请求",
                stderr_tail=self.stderr_tail(),
            )
        data = proto.encode(msg)
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        """取一条消息。

        正常返回一条有效消息。若子进程退出 / EOF / 协议错误已发生且队列已无
        有效消息，抛 `PiBridgeError`——绝不在无消息时永久挂起，也**不因哨兵被
        消费过一次就重新阻塞**（终结后重复读取每次都报错，而不是第二次卡住）。
        """
        # 已终结且队列里没有有效消息：直接报终结原因，不阻塞。
        # 哨兵只放一次，这里的前置检查保证「终结后重复读取」每次都明确报错。
        if self._termination_reason is not None and self._queue.empty():
            raise PiBridgeError(
                self._termination_reason or "子进程已退出，无更多消息",
                stderr_tail=self.stderr_tail(),
            )

        if timeout is None:
            item = await self._queue.get()
        else:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                # 超时：若已终结则报错，否则原样抛 TimeoutError
                if self._termination_reason is not None and self._queue.empty():
                    raise PiBridgeError(
                        self._termination_reason, stderr_tail=self.stderr_tail()
                    )
                raise

        if item is _SENTINEL:
            # 已终结：先看队列里是否还有残留的有效消息（正常结果后退出），
            # 有则先消费，避免误判失败；否则报终结原因。
            if not self._queue.empty():
                return await self._queue.get()
            raise PiBridgeError(
                self._termination_reason or "子进程已退出，无更多消息",
                stderr_tail=self.stderr_tail(),
            )
        return item

    # ---- 取消与清理 ----

    async def cancel(self) -> None:
        """请求取消执行：发 cancel。终结与回收由 close() 完成。"""
        if self._proc is None or self._proc.returncode is not None:
            return
        if self._termination_reason is not None:
            return
        try:
            await self.send(proto.make_message(self.execution_id, "cancel"))
        except PiBridgeError:
            pass

    async def cancel_and_reap(self) -> None:
        """有界顺序取消：请求取消 → 宽限等待退出 → terminate → 必要时 kill → wait 回收。

        用于宿主 `CancelledError` / `LeaseLost` / `ControlStopped` 时清理：先给 Node
        机会优雅中止（发 cancel 并等待其退出），宽限期内不退出则 escalate 到
        terminate，再不行 kill。最终保证子进程被回收，无残留。
        """
        if self._proc is None:
            return
        # 1) 请求取消
        await self.cancel()
        # 2) 宽限等待：给 Node 机会回 finished(cancelled) 并退出
        if self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=self._graceful_timeout)
            except asyncio.TimeoutError:
                # 3) 宽限期后 escalate：terminate → kill → wait
                await self._force_close()

    async def _force_close(self) -> None:
        """强制关闭：terminate → 宽限 → kill → 等待回收。"""
        if self._proc is None:
            return
        proc = self._proc
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._graceful_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=self._kill_timeout)
                except asyncio.TimeoutError:
                    logger.warning("子进程 kill 后仍未退出")
        pending = [task for task in
                   (self._reader_task, self._stderr_task, self._monitor_task)
                   if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._terminate(f"子进程已退出（code={proc.returncode}）")
        # 管道传输必须在事件循环还活着时关掉。否则对象拖到循环关闭后才析构，
        # BaseSubprocessTransport.__del__ 会在已关闭的循环上 call_soon，
        # 打出 Event loop is closed（-W error 下会把整套测试打红）。
        await _close_subprocess_transport(proc)

    async def close(self) -> None:
        """正常清理：发 shutdown，等待退出，回收后台任务。幂等，可重复调用。"""
        if self._closed:
            return
        self._closed = True
        if self._proc is not None and self._proc.returncode is None:
            try:
                await self.send(proto.make_message(self.execution_id, "shutdown"))
            except PiBridgeError:
                pass
        await self._force_close()
        self._proc = None
