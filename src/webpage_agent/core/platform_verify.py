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

    返回 `{readback, readback_match, checks}`：
    - `readback`：回读到的对象（owner/revision/fields/object_id）；
    - `readback_match`：**所有必查项**都匹配才为 True（对象一致 + revision +
      字段）。任何一项缺失/不符都判不匹配，绝不跳过验证；
    - `checks`：逐项核验明细（object_id / revision / 每个字段），供证据持久化
      与排障，不能只有顶层一个 readback_match。
    """
    object_id = str(write_result.get('object_id') or '')
    expected_revision = write_result.get('new_revision')
    expected_fields = write_result.get('modified_fields') or {}
    checks: dict[str, Any] = {}

    # 写入目标缺失（没有 object_id）→ 无从核验，直接判不匹配，不跳过。
    if not object_id:
        return {'readback': None, 'readback_match': False,
                'checks': {'object_id': False}}

    try:
        payload = await get_object_call(object_id)
    except Exception as exc:                        # noqa: BLE001
        logger.warning('回读对象失败 id=%s', object_id, exc_info=True)
        return {'readback': None, 'readback_match': False,
                'checks': {'object_id': False, 'error': type(exc).__name__}}

    if not isinstance(payload, dict) or payload.get('status') != 'ok':
        return {'readback': payload, 'readback_match': False,
                'checks': {'object_id': False,
                           'error': (payload or {}).get('code') if isinstance(
                               payload, dict) else 'readback_not_ok'}}

    # 对象一致性核对：回读到的对象必须与写入目标一致，不能读到别的对象。
    read_object_id = str(payload.get('object_id') or '')
    checks['object_id'] = (read_object_id == object_id)
    if not checks['object_id']:
        return {'readback': payload, 'readback_match': False, 'checks': checks}

    # revision 核对：合同要求写入后的 revision 必须等于写入结果声明的
    # new_revision。缺 new_revision（写回执没报）也不能跳过——没有目标 revision
    # 就无法确认这次写真的落地，判不匹配。
    if expected_revision is None:
        checks['revision'] = False
        return {'readback': payload, 'readback_match': False, 'checks': checks}
    try:
        actual_revision = int(payload.get('revision') or 0)
        expected_revision_int = int(expected_revision)
    except (TypeError, ValueError):
        checks['revision'] = False
        return {'readback': payload, 'readback_match': False, 'checks': checks}
    checks['revision'] = (actual_revision == expected_revision_int)
    if not checks['revision']:
        return {'readback': payload, 'readback_match': False, 'checks': checks}

    # 字段核对：每个修改字段的值必须等于回读到的值；字段缺失（回读里没有该
    # 键）同样判不匹配，不能当成「跳过」。
    fields = payload.get('fields') or {}
    checks['fields'] = {}
    fields_ok = True
    for key, value in (expected_fields or {}).items():
        key_ok = (key in fields) and (fields.get(key) == value)
        checks['fields'][key] = key_ok
        if not key_ok:
            fields_ok = False

    return {'readback': payload, 'readback_match': fields_ok,
            'checks': checks}


async def verify_operation(operation_id: str, *, server,
                           get_operation_call) -> dict:
    """按原操作 ID 核实「远端是否已写」（第十阶段第 8 点）。

    用于「远端已写、本地未记账」窗口：写入响应丢失/超时/进程退出后，恢复先
    用 get_operation 按原操作 ID 核实，再结合对象回读判断，**不生成新写入冒充
    恢复**。

    返回 `{applied, exists, operation, error, state}`，严格区分四种结局：

    - `applied=True, exists=True`：远端明确有该操作（已写，可复用结果，不重发）；
    - `applied=False, exists=False`：远端明确不存在该操作（未写，可显式重试）；
    - `error` 非空且 `state='permission_denied'`：权限错误——**不能当成未执行**，
      结果未知（可能已写，只是没有权限查），转待核实；
    - `error` 非空且 `state='query_failed'`：查询失败（网络/超时），结果未知，
      转待核实。
    """
    if not operation_id:
        return {'applied': False, 'exists': False, 'operation': None,
                'error': '缺少操作 ID，无法核实', 'state': 'query_failed'}
    try:
        payload = await get_operation_call(operation_id)
    except Exception as exc:                        # noqa: BLE001
        logger.warning('核实操作失败 id=%s', operation_id, exc_info=True)
        return {'applied': False, 'exists': None, 'operation': None,
                'error': type(exc).__name__, 'state': 'query_failed'}
    if isinstance(payload, dict) and payload.get('status') == 'ok' \
            and payload.get('operation'):
        return {'applied': True, 'exists': True,
                'operation': payload['operation'], 'error': '', 'state': 'applied'}
    if isinstance(payload, dict):
        code = str(payload.get('code') or '')
        message = str(payload.get('message') or '')
        # 权限错误：不是「操作不存在」，而是「没有权限确认」——结果未知，不能
        # 当未执行去重试（重试可能重复副作用）。
        if code in ('E_FORBIDDEN', 'E_UNAUTHORIZED', 'E_PERMISSION_DENIED'):
            return {'applied': False, 'exists': None, 'operation': None,
                    'error': f'{code}: {message}',
                    'state': 'permission_denied'}
        # 明确不存在：服务端明确说「操作不存在」→ 未写。
        if code in ('E_NOT_FOUND', 'E_NO_SUCH_OPERATION'):
            return {'applied': False, 'exists': False, 'operation': None,
                    'error': '', 'state': 'not_found'}
        # 其它业务错误：查询失败，结果未知。
        return {'applied': False, 'exists': None, 'operation': None,
                'error': f'{code}: {message}', 'state': 'query_failed'}
    # 服务端未返回可识别结构：结果未知。
    return {'applied': False, 'exists': None, 'operation': None,
            'error': '无法识别 get_operation 返回', 'state': 'query_failed'}
