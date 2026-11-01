"""标准 MCP Tasks 原语（experimental）的接入层。

MCP 1.30 起协议自带任务语义，**不需要异步合同**：

- 服务端在工具元数据里声明 `Tool.execution.taskSupport`：
  `forbidden`（缺省）/ `optional` / `required`；
- 客户端把 `task={ttl}` 放进 callTool 请求，服务端回 `CreateTaskResult{task}`；
- 之后用 `tasks/get`、`tasks/cancel` 查询与取消，`tasks/result` 取结果；
- `ClientSession` 初始化时已自动声明 tasks 能力，无需额外协商。

合同（`app/async_contracts.py`）继续服务那些**没有**实现该原语、只用自定义
`job_id` + 查询工具方言语的服务端（openmontage 就是这一类）。两条路产出的
"已受理"增量同形，所以下游的账本、快照、对账、恢复都不用改。

边界：这里的判断只依据工具自己声明的元数据，不猜。声明是 `forbidden` 的工具
永远走普通调用——包括现在所有已接入的服务端。
"""
from webpage_agent.core.config import settings

# 平台侧保留的合同身份：走到这里说明用的是**协议任务**，而不是对方自定义方言。
# `external_tasks` 靠它分辨该用 `tasks/get` 还是合同里的状态查询工具。
PROTOCOL_CONTRACT_ID = 'mcp-task'
PROTOCOL_CONTRACT_VERSION = 1

SUPPORT_FORBIDDEN = 'forbidden'
SUPPORT_OPTIONAL = 'optional'
SUPPORT_REQUIRED = 'required'
SUPPORT_MODES = (SUPPORT_FORBIDDEN, SUPPORT_OPTIONAL, SUPPORT_REQUIRED)

# TaskStatus -> 中台状态。
# `input_required` 表示服务端在等客户端补输入；我们没有对应的交互通道，
# 既不判成功也不判失败，留在"已受理"，由 deadline 决定何时收口。
STATUS_MAP = {
    'working': 'running',
    'input_required': 'accepted',
    'completed': 'succeeded',
    'failed': 'failed',
    'cancelled': 'cancelled',
}

_LEGAL = ('accepted', 'running', 'succeeded', 'failed', 'cancelled')


def support_of(record) -> str:
    """从工具发现结果读 taskSupport。缺省是 forbidden（协议默认值）。"""
    if not isinstance(record, dict):
        return SUPPORT_FORBIDDEN
    mode = str(record.get('task_support') or '').strip().lower()
    if mode in SUPPORT_MODES:
        return mode
    execution = record.get('execution')
    if isinstance(execution, dict):
        mode = str(execution.get('taskSupport') or '').strip().lower()
        if mode in SUPPORT_MODES:
            return mode
    return SUPPORT_FORBIDDEN


def wants_task(support: str) -> bool:
    """这次调用该不该走协议任务通道。

    `required` 必须走——服务端声明了它不接受普通调用。
    `optional` 只在显式配置时走：默认保持原有同步行为，避免"接了新服务端
    就悄悄换了调用方式"。总开关关掉时一律不走。
    """
    if not settings.mcp_tasks_enabled:
        return False
    mode = str(support or '').strip().lower()
    if mode == SUPPORT_REQUIRED:
        return True
    return mode == SUPPORT_OPTIONAL and settings.mcp_tasks_optional_async


def map_status(raw) -> str:
    """把 TaskStatus 折成中台状态；认不出的值一律 unknown，不猜成功。"""
    if raw is None:
        return 'unknown'
    mapped = STATUS_MAP.get(str(raw).strip().lower(), '')
    return mapped if mapped in _LEGAL else 'unknown'


def is_terminal(payload) -> bool:
    return map_status((payload or {}).get('status')
                      if isinstance(payload, dict) else None) in (
        'succeeded', 'failed', 'cancelled')


def acceptance(payload) -> dict:
    """把 `CreateTaskResult` 归一成平台"已受理"增量。

    形状与 `async_contracts.acceptance()` 完全一致，唯独 `contract_id`
    固定为本模块的保留身份——下游据此知道该用 `tasks/get` 查询。
    """
    if not settings.async_tool_runtime_enabled or not isinstance(payload, dict):
        return {}
    remote = str(payload.get('taskId') or payload.get('task_id') or '').strip()
    if not remote:
        return {}
    if is_terminal(payload):
        # 提交即终态：不登记成"等待"，否则会永远轮询一个已经结束的任务。
        return {}
    return {
        'async_accepted': True,
        'remote_task_id': remote,
        'contract_id': PROTOCOL_CONTRACT_ID,
        'contract_version': PROTOCOL_CONTRACT_VERSION,
        'execution_status': 'accepted',
        'phase': 'accepted',
    }


def server_of_tool_key(tool_key: str) -> str:
    """`mcp:<server>:<tool>` -> `<server>`；不是这个形状返回空串。"""
    parts = str(tool_key or '').split(':')
    return parts[1] if len(parts) >= 3 and parts[0] == 'mcp' else ''
