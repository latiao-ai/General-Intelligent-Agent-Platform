"""异步工具的版本化合同。

只认登记过的字段和 JSON Pointer。未出现在状态表里的值保持未知，
不能当成成功。没有持久任务号的合同不能承诺重启后还能查到。
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from webpage_agent.core.config import settings


@dataclass(frozen=True)
class AsyncContract:
    contract_id: str
    version: int = 1
    submit_tool_key: str = ''
    remote_id_pointer: str = ''
    status_tool_key: str = ''
    status_id_argument: str = ''
    status_pointer: str = ''
    status_map: dict = field(default_factory=dict)
    output_pointer: str = ''
    cancel_tool_key: str = ''
    poll_interval_seconds: float = 5.0
    deadline_seconds: float = 1800.0
    recoverable: bool = True
    # 提交的幂等键提取指针（第八阶段第 2 点）：提交是有副作用的操作，必须
    # 要么有可查询的 remote id，要么有幂等键/提交结果核实方式；两者都没有时，
    # 提交结果未知必须转待核实，不能盲目重提。
    idempotency_key_pointer: str = ''
    # 用幂等键查任务的工具键（第八阶段收口第 8 点）：首发合同若只返回提交幂等键
    # 而没有 remote id，必须声明「能用幂等键查任务」的映射能力，否则无法持续
    # 查询任务进度，不允许进入正常等待（转待核实）。
    idempotency_lookup_tool_key: str = ''


def pointer(document, path: str):
    """取 JSON Pointer。路径不存在时返回 None，不猜一个默认成功值。"""
    if not isinstance(path, str) or path in ('', '/'):
        return document
    current = document
    for part in path.lstrip('/').split('/'):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _contracts_path() -> Path | None:
    raw = str(getattr(settings, 'async_contracts_file', '') or '').strip()
    if raw:
        return Path(raw)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / 'contracts' / 'async_tools.json'
        if candidate.is_file():
            return candidate
    return None


def load() -> list[AsyncContract]:
    path = _contracts_path()
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    rows = data.get('contracts') if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    found = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get('contract_id') or '').strip():
            continue
        remote_id_pointer = str(row.get('remote_id_pointer') or '')
        status_tool_key = str(row.get('status_tool_key') or '')
        status_id_argument = str(row.get('status_id_argument') or '')
        # 没有持久任务号，或没有按这个号查询的工具，重启后接不上。
        # 文件里写成 recoverable=true 也不能把这种合同算成可恢复。
        recoverable = bool(row.get('recoverable', True)) and bool(
            remote_id_pointer and status_tool_key and status_id_argument)
        found.append(AsyncContract(
            contract_id=str(row['contract_id']),
            version=int(row.get('version') or 1),
            submit_tool_key=str(row.get('submit_tool_key') or ''),
            remote_id_pointer=remote_id_pointer,
            status_tool_key=status_tool_key,
            status_id_argument=status_id_argument,
            status_pointer=str(row.get('status_pointer') or ''),
            status_map=dict(row.get('status_map') or {}),
            output_pointer=str(row.get('output_pointer') or ''),
            cancel_tool_key=str(row.get('cancel_tool_key') or ''),
            poll_interval_seconds=float(row.get('poll_interval_seconds') or 5),
            deadline_seconds=float(row.get('deadline_seconds') or 1800),
            recoverable=recoverable,
            idempotency_key_pointer=str(row.get('idempotency_key_pointer') or ''),
            idempotency_lookup_tool_key=str(
                row.get('idempotency_lookup_tool_key') or '')))
    return found


def summary() -> dict:
    """当前进程实际加载到的合同。示例文件不在这条路径上，所以不会被算进去。"""
    rows = load()
    return {'loaded': len(rows),
            'recoverable': sum(1 for row in rows if row.recoverable)}


def _enabled_selection() -> dict[str, int]:
    """读显式选中的合同（`contract_id -> version`）。

    第八阶段第 2 点：配置必须**显式选择**合同及版本，不能自动启用仓库里的
    示例合同。`ASYNC_CONTRACTS_ENABLED` 形如 `contract_id:version,...`；
    未配置时**不启用任何文件合同**（协议任务 `mcp-task` 走它自己的保留身份，
    不在这里）。
    """
    raw = str(getattr(settings, 'async_contracts_enabled', '') or '').strip()
    selection: dict[str, int] = {}
    if not raw:
        return selection
    for part in raw.split(','):
        item = part.strip()
        if not item:
            continue
        contract_id, _, version = item.partition(':')
        contract_id = contract_id.strip()
        if not contract_id:
            continue
        try:
            selection[contract_id] = int(version) if version.strip() else 0
        except ValueError:
            selection[contract_id] = 0
    return selection


def selected_contracts() -> list[AsyncContract]:
    """返回**生效**的文件合同。

    第八阶段第 2 点：不自动启用仓库里的示例合同——出厂 `contracts/async_tools.json`
    是空清单（示例在 `async_tools.example.json`，不在加载路径上），所以默认
    `load()` 已经返回空。当配置了 `ASYNC_CONTRACTS_ENABLED`（`contract_id:version`
    逗号分隔）时，只启用显式选中的合同并按版本过滤；未配置时启用已加载文件里
    的全部合同（该文件本身已由 `ASYNC_CONTRACTS_FILE` 显式指定）。
    """
    selection = _enabled_selection()
    all_contracts = load()
    if not selection:
        return all_contracts
    out = []
    for contract in all_contracts:
        wanted = selection.get(contract.contract_id)
        if wanted is None:
            continue
        if wanted and contract.version != wanted:
            continue
        out.append(contract)
    return out


def match_submit(tool_key: str) -> AsyncContract | None:
    for contract in selected_contracts():
        if contract.submit_tool_key and contract.submit_tool_key == tool_key:
            return contract
    return None


def protocol_contract() -> AsyncContract:
    """标准 MCP Tasks 原语的保留身份 `mcp-task`。

    这类任务的"查询"是协议请求 `tasks/get`，不是某个工具，所以这份合同
    没有 `status_tool_key`；`external_tasks` 靠 `contract_id` 分辨走哪条路。
    状态映射直接沿用协议自己的 TaskStatus 表。
    """
    from webpage_agent.integrations import mcp_tasks
    return AsyncContract(
        contract_id=mcp_tasks.PROTOCOL_CONTRACT_ID,
        version=mcp_tasks.PROTOCOL_CONTRACT_VERSION,
        status_pointer='/status',
        status_map=dict(mcp_tasks.STATUS_MAP),
        recoverable=True)


def match_id(contract_id: str, version: int | None = None) -> AsyncContract | None:
    from webpage_agent.integrations import mcp_tasks
    if str(contract_id or '') == mcp_tasks.PROTOCOL_CONTRACT_ID:
        try:
            same = version is None or int(version) == mcp_tasks.PROTOCOL_CONTRACT_VERSION
        except (TypeError, ValueError):
            same = False
        return protocol_contract() if same else None
    for contract in selected_contracts():
        if contract.contract_id != contract_id:
            continue
        if version is not None and contract.version != int(version):
            continue
        return contract
    return None


def map_status(raw, contract: AsyncContract) -> str:
    """把供应商状态折成 accepted/running/succeeded/failed/cancelled/unknown。"""
    if raw is None or contract is None:
        return 'unknown'
    table = {str(key).strip().lower(): str(value).strip().lower()
             for key, value in (contract.status_map or {}).items()}
    mapped = table.get(str(raw).strip().lower(), '')
    if mapped in ('accepted', 'running', 'succeeded', 'failed', 'cancelled'):
        return mapped
    return 'unknown'


def remote_id_of(payload: dict, contract: AsyncContract) -> str:
    raw = pointer(payload, contract.remote_id_pointer)
    if isinstance(raw, bool) or raw is None:
        return ''
    if isinstance(raw, (int, float)):
        return str(int(raw)) if isinstance(raw, float) and raw.is_integer() else str(raw)
    text = str(raw).strip()
    return text


def acceptance(tool_key: str, payload: dict | None) -> dict:
    """提交回执如果只是受理，标成 accepted，供观察去等，而不是当成做完了。

    第八阶段第 2 点：提交是有副作用的操作。受理回执必须能**按 remote id 持续
    查询**；拿不到 remote id 时，若有幂等键（`idempotency_key_pointer`）还能
    安全核实，否则提交结果未知，返回 `needs_reconcile` 增量——调用方据此转
    待核实，**不能盲目重提**（重提等于再产生一次副作用）。
    """
    if not settings.async_tool_runtime_enabled or not isinstance(payload, dict):
        return {}
    contract = match_submit(tool_key)
    if contract is None or not contract.recoverable:
        return {}
    remote = remote_id_of(payload, contract)
    mapped = map_status(pointer(payload, contract.status_pointer), contract)
    if mapped in ('succeeded', 'failed', 'cancelled'):
        # 提交即终态：不登记成等待。
        return {}
    if remote:
        return {'async_accepted': True, 'remote_task_id': remote,
                'contract_id': contract.contract_id,
                'contract_version': contract.version,
                'execution_status': 'accepted', 'phase': 'accepted'}
    # 没有可查询的 remote id：看有没有幂等键可核实。
    idem = pointer(payload, contract.idempotency_key_pointer) \
        if contract.idempotency_key_pointer else None
    if isinstance(idem, (str, int, float)) and str(idem).strip():
        # 第八阶段收口第 8 点：只有幂等键、没有 remote id 时，必须有「用幂等键
        # 查任务」的映射能力（`idempotency_lookup_tool_key`）才能进入正常等待；
        # 没有则无法持续查询任务进度，转待核实，不能假装能跟踪。
        if contract.idempotency_lookup_tool_key:
            return {'async_accepted': True, 'remote_task_id': '',
                    'idempotency_key': str(idem).strip(),
                    'contract_id': contract.contract_id,
                    'contract_version': contract.version,
                    'execution_status': 'accepted', 'phase': 'accepted'}
        return {'async_accepted': False, 'execution_status': 'unknown',
                'phase': 'needs_reconcile',
                'idempotency_key': str(idem).strip(),
                'contract_id': contract.contract_id,
                'contract_version': contract.version}
    # 既无 remote id 又无幂等键：提交结果未知，必须转待核实，不能盲目重提。
    return {'async_accepted': False, 'execution_status': 'unknown',
            'phase': 'needs_reconcile',
            'contract_id': contract.contract_id,
            'contract_version': contract.version}
