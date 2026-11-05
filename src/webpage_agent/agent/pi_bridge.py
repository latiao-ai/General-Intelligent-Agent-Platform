"""Pi 桥接：子进程生命周期、管道、待响应请求、超时与清理。

进程模型（第三阶段）：一次运行独占一个 Node 子进程和一个 Pi session，执行结束
后清理。不做进程池、不跨请求复用，避免混用上下文、回调和凭据。

并发设计（读管道不能被工具执行阻塞）：

    - 一个后台任务**持续**读 stdout，逐行解码后放进 asyncio.Queue；
    - 一个后台任务**持续**排空 stderr，只保留有界诊断尾部（避免管道写满卡死）；
    - 一个后台任务监控子进程退出；
    - 宿主（PiRuntime）从队列取消息驱动流程；工具执行在宿主侧完成，读任务不受影响。

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

        self.execution_id = uuid.uuid4().hex
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._exit_code: Optional[int] = None
        self._closed = False

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

        if msg.get("type") != "ready":
            await self._force_close()
            raise PiBridgeError(
                f"启动消息异常：期望 ready，收到 {msg.get('type')!r}",
                stderr_tail=self.stderr_tail(),
            )
        self._closed = False

    async def _read_stdout(self) -> None:
        """持续读 stdout，逐行解码进队列。"""
        assert self._proc and self._proc.stdout
        while True:
            try:
                line = await self._proc.stdout.readline()
            except (ValueError, asyncio.CancelledError):
                break
            if not line:
                break  # EOF
            if len(line) > self._max_message_bytes:
                logger.warning("丢弃超长消息（%d 字节）", len(line))
                continue
            try:
                msg = proto.decode(line)
            except ProtocolError as exc:
                logger.warning("丢弃非法消息：%s", exc)
                continue
            await self._queue.put(msg)

    async def _drain_stderr(self) -> None:
        """持续排空 stderr，只保留有界尾部。"""
        assert self._proc and self._proc.stderr
        while True:
            try:
                line = await self._proc.stderr.readline()
            except (ValueError, asyncio.CancelledError):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            self._stderr_tail.append(text)

    async def _monitor_exit(self) -> None:
        """监控子进程退出。"""
        assert self._proc
        self._exit_code = await self._proc.wait()

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    # ---- 收发 ----

    async def send(self, msg: dict[str, Any]) -> None:
        """写一条消息到子进程 stdin。进程已退出则报错。"""
        if self._proc is None or self._proc.stdin is None:
            raise PiBridgeError("子进程未启动或 stdin 不可用")
        if self._proc.returncode is not None:
            raise PiBridgeError(
                f"子进程已退出（code={self._proc.returncode}）",
                stderr_tail=self.stderr_tail(),
            )
        data = proto.encode(msg)
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def next_message(self, timeout: float | None = None) -> dict[str, Any]:
        """取一条消息。超时抛 TimeoutError；子进程退出且队列空时抛 PiBridgeError。"""
        try:
            if timeout is None:
                return await self._queue.get()
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            # 超时：检查是否已退出
            if self._exit_code is not None and self._queue.empty():
                raise PiBridgeError(
                    f"子进程已退出（code={self._exit_code}），无更多消息",
                    stderr_tail=self.stderr_tail(),
                )
            raise

    # ---- 取消与清理 ----

    async def cancel(self) -> None:
        """请求取消执行：发 cancel，等待 Node 中止并退出。"""
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            await self.send(proto.make_message(self.execution_id, "cancel"))
        except PiBridgeError:
            pass

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
        for task in (self._reader_task, self._stderr_task, self._monitor_task):
            if task and not task.done():
                task.cancel()

    async def close(self) -> None:
        """正常清理：发 shutdown，等待退出，回收后台任务。"""
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
