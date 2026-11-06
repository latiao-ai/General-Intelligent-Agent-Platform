"""写后独立回读核验（第十阶段第 7/9 点）。

平台写工具成功后，用**同一可信主体**独立读取同一对象，核对目标字段与
revision，把回读证据与核验结果落回 state。这是「写任务成功」的必要条件之一：
「有 answer」「审批通过」「提交 accepted」都不等于效果完成——只有独立回读
匹配 + 证据持久化，宿主才把写效果判为已核实。

本模块只做**纯逻辑 + 一次回读调用**，不碰账本、不写终态；终态由宿主据
`readback` / `readback_match` 证据决定。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def needs_readback(write_result: dict | None) -> bool:
    """这个写结果是否需要回读核验。

    有对象 ID（平台对象修改）才需要；纯部署类（deployment）走它自己的核验，
    不由本模块处理。
    """
    if not isinstance(write_result, dict):
        return False
    return bool(write_result.get('object_id'))


async def verify_platform_write(write_result: dict, *, server,
                                get_object_call) -> dict:
    """对一次平台写做独立回读，返回回读证据。

    参数：
    - `write_result`：写工具回执（含 object_id / new_revision / modified_fields）；
    - `server`：平台 MCP 服务端；
    - `get_object_call`：`async (object_id) -> payload` 的回读调用（宿主注入，
      走真实 MCP transport 的 get_object）。

    返回 `{readback, readback_match}`：
    - `readback`：回读到的对象（owner/revision/fields）；
    - `readback_match`：目标字段与 revision 是否匹配（bool）。
    回读失败返回 `readback_match=False`，不吞异常也不冒充匹配。
    """
    object_id = str(write_result.get('object_id') or '')
    expected_revision = write_result.get('new_revision')
    expected_fields = write_result.get('modified_fields') or {}
    if not object_id:
        return {'readback': None, 'readback_match': False}

    try:
        payload = await get_object_call(object_id)
    except Exception as exc:                        # noqa: BLE001
        logger.warning('回读对象失败 id=%s', object_id, exc_info=True)
        return {'readback': None, 'readback_match': False}

    if not isinstance(payload, dict) or payload.get('status') != 'ok':
        return {'readback': payload, 'readback_match': False}

    # revision 核对
    if expected_revision is not None:
        if int(payload.get('revision') or 0) != int(expected_revision):
            return {'readback': payload, 'readback_match': False}

    # 字段核对：每个修改字段的值必须等于回读到的值
    fields = payload.get('fields') or {}
    for key, value in (expected_fields or {}).items():
        if fields.get(key) != value:
            return {'readback': payload, 'readback_match': False}

    return {'readback': payload, 'readback_match': True}


async def verify_operation(operation_id: str, *, server,
                           get_operation_call) -> dict:
    """按原操作 ID 核实「远端是否已写」（第十阶段第 8 点）。

    用于「远端已写、本地未记账」窗口：写入响应丢失/超时/进程退出后，恢复先
    用 get_operation 按原操作 ID 核实，再结合对象回读判断，**不生成新写入冒充
    恢复**。

    返回 `{applied, operation, error}`：
    - `applied=True`：远端已按该操作 ID 写入（可复用结果，不重发）；
    - `applied=False`：远端没有该操作（未写入，可显式重试）；
    - 查询失败返回 `error` 非空（结果未知，转待核实）。
    """
    if not operation_id:
        return {'applied': False, 'operation': None,
                'error': '缺少操作 ID，无法核实'}
    try:
        payload = await get_operation_call(operation_id)
    except Exception as exc:                        # noqa: BLE001
        logger.warning('核实操作失败 id=%s', operation_id, exc_info=True)
        return {'applied': False, 'operation': None,
                'error': type(exc).__name__}
    if isinstance(payload, dict) and payload.get('status') == 'ok' \
            and payload.get('operation'):
        return {'applied': True, 'operation': payload['operation'], 'error': ''}
    # 服务端明确返回「操作不存在」→ 未写入
    return {'applied': False, 'operation': None, 'error': ''}
