"""渠道适配器的抽象基类。

一个渠道 = 一个 `Transport` 子类 + 一份注册表清单（`registry.py`）。

**硬性约束**（违反任何一条都会在生产里出事）：

1. `send` 对同一 `Outbound` 重试不产生副作用——能带幂等键就带；
2. `capabilities()` 不撒谎：不支持卡片就别声明，Router 会按能力降级；
3. `start()` 失败要抛 `TransportError`，由网关决定 `retrying` 还是 `fatal`；
4. 禁止在 Transport 里直接调用 Agent——入站只能交给 `on_inbound`。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from webpage_agent.messaging.models import (Capabilities, Inbound, Outbound,
                                            SendResult)

logger = logging.getLogger(__name__)

InboundCallback = Callable[[Inbound], Awaitable[None]]
StatusCallback = Callable[[str, str, str], None]


class TransportError(RuntimeError):
    """渠道启动/运行失败。`retryable` 决定网关是退避重连还是直接 fatal。"""

    def __init__(self, message: str, *, code: str = 'transport_error',
                 retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ChannelConfig:
    """一个渠道实例的全部配置。密钥只从环境变量/登录态来，不落配置 JSON。"""

    channel_id: str
    tenant_id: str
    owner_user_id: str
    bot_id: str = ''
    # 一个渠道的「第几份配置」。'' = 存量那一份；其余是后续加的机器人各自的实例。
    instance_id: str = ''
    options: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    dm_policy: str = 'pair'
    group_policy: str = 'mention'
    allowed_users: tuple[str, ...] = ()
    public_base_url: str = ''

    @property
    def key(self) -> str:
        return (f'{self.tenant_id}:{self.owner_user_id}:{self.channel_id}'
                f':{self.instance_id}')

    def option(self, name: str, default: str = '') -> str:
        return str(self.options.get(name) or default).strip()

    def secret(self, name: str, default: str = '') -> str:
        return str(self.secrets.get(name) or default).strip()

    def require(self, key: str) -> str:
        value = self.secret(key)
        if not value:
            raise TransportError(f'缺少必填凭据 {key}', code='missing_credential',
                                 retryable=False)
        return value


class Transport(ABC):
    """渠道协议适配器。"""

    name = ''
    # 是否自己拉取入站消息（长轮询/长连接）。回调渠道为 False：入站由 API 落库。
    pulls_inbound = True

    def __init__(self, config: ChannelConfig, *,
                 on_inbound: InboundCallback,
                 on_status: StatusCallback | None = None,
                 http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.on_inbound = on_inbound
        self._on_status = on_status
        self._http_transport = http_transport
        self._http: httpx.AsyncClient | None = None

    # ---------- 生命周期 ----------

    @abstractmethod
    async def start(self) -> None:
        """建连 / 启动轮询。失败抛 `TransportError`。"""

    @abstractmethod
    async def stop(self) -> None:
        """取消后台任务、关闭连接。重复调用必须安全。"""

    @abstractmethod
    async def send(self, chat_id: str, msg: Outbound) -> SendResult:
        ...

    async def typing(self, chat_id: str) -> None:
        """可选：正在输入。默认什么都不做。"""

    def alive(self) -> bool:
        """后台连接是否还在。回调渠道恒为 True；长轮询渠道由子类覆写。"""
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities()

    # ---------- 给子类用的小工具 ----------

    def client(self, **kwargs) -> httpx.AsyncClient:
        """一个带超时与测试注入点的 client。调用方负责 aclose。"""
        if self._http_transport is not None:
            kwargs.setdefault('transport', self._http_transport)
        kwargs.setdefault('timeout', 15.0)
        kwargs.setdefault('follow_redirects', False)
        return httpx.AsyncClient(**kwargs)

    @property
    def http(self) -> httpx.AsyncClient:
        """共享 client：`start()` 里创建，`stop()` 里关闭。"""
        if self._http is None:
            self._http = self.client(timeout=30.0)
        return self._http

    async def close_http(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def report(self, state: str, detail: str = '', code: str = '') -> None:
        if self._on_status is not None:
            try:
                self._on_status(state, detail, code)
            except Exception:                      # noqa: BLE001
                logger.debug('渠道状态回调失败', exc_info=True)

    def require_option(self, name: str, label: str = '') -> str:
        value = self.config.option(name)
        if not value:
            raise TransportError(f'缺少配置 {label or name}', code='missing_config',
                                 retryable=False)
        return value


class CallbackTransport(Transport):
    """回调渠道（飞书/企微）：没有自己的入站连接，只负责发送与验签后的解析。

    `start()` 只做配置自检并把状态置为 `connected`——真正的"收"发生在
    `POST /api/messaging/webhook/<channel>`：验签 → 归一化 → 落 `im_inbox`。
    """

    pulls_inbound = False

    async def start(self) -> None:
        self.report('connected', '回调已就绪，等待平台推送')

    async def stop(self) -> None:
        await self.close_http()
