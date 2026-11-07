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


def validate_model_request(msg: dict[str, Any], execution_id: str) -> None:
    """校验入站 model_request：唯一 request_id。"""
    validate_envelope(msg, execution_id)
    if msg["type"] != "model_request":
        raise ProtocolError(f"期望 model_request，收到 {msg['type']!r}")
    _require_str(msg, "request_id")


def validate_model_usage(msg: dict[str, Any], execution_id: str) -> None:
    """校验入站 model_usage：唯一 request_id、outcome 合法、usage 字段为非负整数。"""
    validate_envelope(msg, execution_id)
    if msg["type"] != "model_usage":
        raise ProtocolError(f"期望 model_usage，收到 {msg['type']!r}")
    _require_str(msg, "request_id")
    outcome = msg.get("outcome")
    if outcome not in ("succeeded", "failed", "cancelled"):
        raise ProtocolError(f"非法 model_usage outcome：{outcome!r}")
    usage = msg.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ProtocolError(f"model_usage 的 usage 必须是对象，收到 {type(usage).__name__}")
    for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "total_tokens"):
        value = (usage or {}).get(key)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ProtocolError(f"model_usage 的 {key} 必须是非负整数，收到 {value!r}")


def validate_checkpoint(msg: dict[str, Any], execution_id: str) -> None:
    """校验入站 checkpoint：版本化上下文导出。

    Node 在「完整模型轮次结束 / 工具调用候选确定」等边界发检查点，宿主持久化
    并确认后才允许后续派发。上下文含完整模型消息（assistant/toolCall/toolResult
    配对）、检查点序号、待处理工具调用。
    """
    validate_envelope(msg, execution_id)
    if msg["type"] != "checkpoint":
        raise ProtocolError(f"期望 checkpoint，收到 {msg['type']!r}")
    seq = msg.get("checkpoint_seq")
    if not isinstance(seq, int) or seq < 0:
        raise ProtocolError(f"checkpoint 的 checkpoint_seq 必须是非负整数，收到 {seq!r}")
    messages = msg.get("messages")
    if messages is not None and not isinstance(messages, list):
        raise ProtocolError(f"checkpoint 的 messages 必须是列表，收到 {type(messages).__name__}")
    pending = msg.get("pending_calls")
    if pending is not None and not isinstance(pending, list):
        raise ProtocolError(f"checkpoint 的 pending_calls 必须是列表，收到 {type(pending).__name__}")


def validate_checkpoint_ack(msg: dict[str, Any], execution_id: str) -> None:
    """校验出站 checkpoint_ack（Python → Node）：确认持久化。"""
    validate_envelope(msg, execution_id)
    if msg["type"] != "checkpoint_ack":
        raise ProtocolError(f"期望 checkpoint_ack，收到 {msg['type']!r}")
    if not isinstance(msg.get("checkpoint_seq"), int):
        raise ProtocolError(
            f"checkpoint_ack 的 checkpoint_seq 必须是整数，收到 {msg.get('checkpoint_seq')!r}")
    if not isinstance(msg.get("ok"), bool):
        raise ProtocolError(f"checkpoint_ack 的 ok 必须是布尔，收到 {msg.get('ok')!r}")


def validate_finished(msg: dict[str, Any], execution_id: str) -> None:
    """校验入站 finished：outcome 必须是 answered/cancelled/failed/waiting。"""
    validate_envelope(msg, execution_id)
    if msg["type"] != "finished":
        raise ProtocolError(f"期望 finished，收到 {msg['type']!r}")
    outcome = msg.get("outcome")
    if outcome not in ("answered", "cancelled", "failed", "waiting"):
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
    "model_request": validate_model_request,
    "model_usage": validate_model_usage,
    "checkpoint": validate_checkpoint,
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


# ---- 工具目录（完整 JSON Schema）----

def validate_tool_decl(decl: dict[str, Any]) -> None:
    """校验一条宿主工具声明：name、description、parameters 必须是完整 JSON Schema。

    第五阶段起工具目录传递能力包 `Tool.args` 的完整 JSON Schema（含 type、
    properties、required），不再用「字段名→类型字符串」的简化格式——简化格式
    无法表达 required / 嵌套对象 / 数组元素约束。
    """
    if not isinstance(decl, dict):
        raise ProtocolError(f"工具声明必须是对象，收到 {type(decl).__name__}")
    _require_str(decl, "name")
    if not isinstance(decl.get("description"), str):
        raise ProtocolError(f"工具 {decl.get('name')!r} 的 description 必须是字符串")
    schema = decl.get("parameters")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ProtocolError(
            f"工具 {decl.get('name')!r} 的 parameters 必须是 JSON Schema object"
        )
    # properties 若存在必须是对象；required 若存在必须是字符串数组
    props = schema.get("properties")
    if props is not None and not isinstance(props, dict):
        raise ProtocolError(f"工具 {decl.get('name')!r} 的 properties 必须是对象")
    required = schema.get("required")
    if required is not None and (not isinstance(required, list)
                                 or not all(isinstance(x, str) for x in required)):
        raise ProtocolError(f"工具 {decl.get('name')!r} 的 required 必须是字符串数组")


def validate_tool_catalog(tools: Any) -> None:
    """校验整个工具目录：必须是列表，逐条过 validate_tool_decl。

    `[]` 是合法的（表示本轮无工具），不抛错。
    """
    if not isinstance(tools, list):
        raise ProtocolError(f"工具目录必须是列表，收到 {type(tools).__name__}")
    for decl in tools:
        validate_tool_decl(decl)


def validate_args_against_schema(args: dict[str, Any], schema: dict[str, Any]) -> None:
    """按完整 JSON Schema 校验工具参数（子集：object/properties/required/标量/数组）。

    覆盖真实 web_search 所需的约束：缺少必填字段（query）、字段类型错误、
    嵌套对象与数组元素类型。不支持的 schema 关键字明确抛错，不静默放行。
    """
    if not isinstance(schema, dict):
        raise ProtocolError("参数 schema 必须是对象")
    if schema.get("type") != "object":
        raise ProtocolError(f"工具参数顶层必须是 object，收到 {schema.get('type')!r}")
    if not isinstance(args, dict):
        raise ProtocolError(f"工具参数必须是对象，收到 {type(args).__name__}")

    required = schema.get("required") or []
    if not isinstance(required, list):
        raise ProtocolError("required 必须是数组")
    for key in required:
        if key not in args:
            raise ProtocolError(f"缺少必填参数：{key}")

    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise ProtocolError("properties 必须是对象")
    for key, value in args.items():
        sub = properties.get(key)
        if sub is None:
            # 未声明的字段：拒绝（web_search 等工具不接受未知字段）
            raise ProtocolError(f"未声明的参数：{key}")
        _validate_value(key, value, sub)


def _validate_value(key: str, value: Any, sub: dict[str, Any]) -> None:
    """递归校验单个参数值是否符合其 schema 节点。"""
    if not isinstance(sub, dict):
        raise ProtocolError(f"参数 {key!r} 的 schema 必须是对象")
    kind = sub.get("type")
    if kind == "string":
        if not isinstance(value, str):
            raise ProtocolError(f"参数 {key!r} 必须是字符串，收到 {type(value).__name__}")
    elif kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ProtocolError(f"参数 {key!r} 必须是数字，收到 {type(value).__name__}")
    elif kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProtocolError(f"参数 {key!r} 必须是整数，收到 {type(value).__name__}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ProtocolError(f"参数 {key!r} 必须是布尔，收到 {type(value).__name__}")
    elif kind == "array":
        if not isinstance(value, list):
            raise ProtocolError(f"参数 {key!r} 必须是数组，收到 {type(value).__name__}")
        items = sub.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _validate_value(f"{key}[{i}]", item, items)
    elif kind == "object":
        if not isinstance(value, dict):
            raise ProtocolError(f"参数 {key!r} 必须是对象，收到 {type(value).__name__}")
        sub_props = sub.get("properties") or {}
        sub_required = sub.get("required") or []
        # 开放对象：声明了 additionalProperties=True，或没有声明 properties
        # （未约束字段集）时，允许任意字段——不能把「空 properties」误判成
        # 「禁止所有字段」。只有显式声明了 properties 且 additionalProperties
        # 非 true 时才逐字段收紧。
        allow_extra = bool(sub.get("additionalProperties", False)) or not sub_props
        for rk in sub_required:
            if rk not in value:
                raise ProtocolError(f"参数 {key!r} 缺少必填字段 {rk}")
        for sk, sv in value.items():
            sub_node = sub_props.get(sk)
            if sub_node is None:
                if allow_extra:
                    continue
                raise ProtocolError(f"参数 {key!r} 含未声明字段 {sk}")
            _validate_value(f"{key}.{sk}", sv, sub_node)
    elif kind is None:
        # 无 type 约束：只保证可序列化
        if not isinstance(value, _ALLOWED_ARG_TYPES):
            raise ProtocolError(f"参数 {key!r} 类型不受支持：{type(value).__name__}")
    else:
        raise ProtocolError(f"参数 {key!r} 的 schema 类型不受支持：{kind!r}")
