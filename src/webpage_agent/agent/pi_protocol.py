"""Pi 桥接协议：消息结构校验与协议错误。

与 Node 侧 `pi-runtime/src/protocol.ts` 保持一致。每行一个 UTF-8 JSON，stdout
只传协议、stderr 写日志。所有消息统一携带 `protocol_version`、`execution_id`、
`type`；工具往返另带唯一 `call_id`。

业务 `run_id`、租约代次等由 Python 持有与校验，**不进**桥接协议。

这里的校验函数**必须接入真实收发路径**（`pi_bridge.py`），不能只被单元测试
调用：所有入站消息都要检查版本、执行 ID、类型与字段；工具调用检查唯一
`call_id`、工具名与参数类型。非对象参数明确拒绝，不静默替换成 `{}`。
"""
from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1

# 工具参数的合法类型集合：参数必须是 JSON 对象（dict），且允许的字段类型
# 限定在基础标量/数组/对象，杜绝函数、符号等不可序列化类型混入。
_ALLOWED_ARG_TYPES = (str, int, float, bool, type(None), list, dict)


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


def _require_str(msg: dict[str, Any], key: str) -> str:
    """取出一个必须为非空字符串的字段，否则拒绝。"""
    value = msg.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"字段 {key!r} 必须是非空字符串，收到 {value!r}")
    return value


def validate_tool_call(msg: dict[str, Any], execution_id: str) -> None:
    """校验入站 tool_call：唯一 call_id、工具名、参数必须是对象。

    非对象参数（例如参数整体是字符串或数字）明确拒绝，**不**静默替换成 `{}`。
    """
    validate_envelope(msg, execution_id)
    if msg["type"] != "tool_call":
        raise ProtocolError(f"期望 tool_call，收到 {msg['type']!r}")
    _require_str(msg, "call_id")
    _require_str(msg, "tool_name")
    if "arguments" not in msg:
        raise ProtocolError("tool_call 缺少 arguments 字段")
    args = msg["arguments"]
    if not isinstance(args, dict):
        raise ProtocolError(
            f"tool_call 参数必须是 JSON 对象，收到 {type(args).__name__}"
        )
    for key, value in args.items():
        if not isinstance(value, _ALLOWED_ARG_TYPES):
            raise ProtocolError(
                f"工具参数 {key!r} 类型不受支持：{type(value).__name__}"
            )


def validate_finished(msg: dict[str, Any], execution_id: str) -> None:
    """校验入站 finished：outcome 必须是 answered/cancelled/failed。"""
    validate_envelope(msg, execution_id)
    if msg["type"] != "finished":
        raise ProtocolError(f"期望 finished，收到 {msg['type']!r}")
    outcome = msg.get("outcome")
    if outcome not in ("answered", "cancelled", "failed"):
        raise ProtocolError(f"非法 finished outcome：{outcome!r}")


def validate_tool_result(msg: dict[str, Any], execution_id: str) -> None:
    """校验入站 tool_result：唯一 call_id、ok 布尔、错误正文为字符串。"""
    validate_envelope(msg, execution_id)
    if msg["type"] != "tool_result":
        raise ProtocolError(f"期望 tool_result，收到 {msg['type']!r}")
    _require_str(msg, "call_id")
    if not isinstance(msg.get("ok"), bool):
        raise ProtocolError(f"tool_result 的 ok 必须是布尔，收到 {msg.get('ok')!r}")
    err = msg.get("error")
    if err is not None and not isinstance(err, str):
        raise ProtocolError(f"tool_result 的 error 必须是字符串，收到 {type(err).__name__}")


# 入站消息（Node → Python）按 type 分发的校验器。Python 侧只接收这几类。
_INBOUND_VALIDATORS = {
    "ready": validate_envelope,
    "event": validate_envelope,
    "tool_call": validate_tool_call,
    "finished": validate_finished,
    "error": validate_envelope,
}


def validate_inbound(msg: dict[str, Any], execution_id: str) -> None:
    """按消息类型分发校验所有入站消息。未知类型抛 ProtocolError。"""
    validate_envelope(msg, execution_id)
    validator = _INBOUND_VALIDATORS.get(msg["type"])
    if validator is None:
        raise ProtocolError(f"未知的入站消息类型：{msg['type']!r}")
    validator(msg, execution_id)


def make_message(execution_id: str, type_: str, **fields: Any) -> dict[str, Any]:
    """构造一条带通用信封的消息。"""
    return {"protocol_version": PROTOCOL_VERSION, "execution_id": execution_id,
            "type": type_, **fields}
