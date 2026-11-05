"""Pi 桥接协议：消息结构校验与协议错误。

与 Node 侧 `pi-runtime/src/protocol.ts` 保持一致。每行一个 UTF-8 JSON，stdout
只传协议、stderr 写日志。所有消息统一携带 `protocol_version`、`execution_id`、
`type`；工具往返另带唯一 `call_id`。

业务 `run_id`、租约代次等由 Python 持有与校验，**不进**桥接协议。
"""
from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    """桥接协议层面的错误（版本不匹配、结构非法、消息格式错等）。"""


def encode(msg: dict[str, Any]) -> bytes:
    """把一条消息编码成一行 UTF-8 JSON（末尾带换行）。"""
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    """把一行字节解码成消息 dict。解析失败抛 ProtocolError。"""
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"无法解析 JSON 行：{exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"消息必须是 JSON 对象，收到 {type(obj).__name__}")
    return obj


def validate_envelope(msg: dict[str, Any], execution_id: str) -> None:
    """校验通用信封：protocol_version、execution_id、type。"""
    if msg.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(
            f"协议版本不匹配：期望 {PROTOCOL_VERSION}，收到 {msg.get('protocol_version')}"
        )
    if msg.get("execution_id") != execution_id:
        raise ProtocolError(
            f"execution_id 不匹配：期望 {execution_id}，收到 {msg.get('execution_id')}"
        )
    if not isinstance(msg.get("type"), str) or not msg["type"]:
        raise ProtocolError(f"消息缺少合法 type：{msg.get('type')!r}")


def make_message(execution_id: str, type_: str, **fields: Any) -> dict[str, Any]:
    """构造一条带通用信封的消息。"""
    return {"protocol_version": PROTOCOL_VERSION, "execution_id": execution_id,
            "type": type_, **fields}
