"""异步工具的版本化合同。

只认登记过的字段和 JSON Pointer。未出现在状态表里的值保持未知，
不能当成成功。没有持久任务号的合同不能承诺重启后还能查到。
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings


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
    candidate = Path(__file__).resolve().parents[1] / 'contracts' / 'async_tools.json'
    return candidate if candidate.is_file() else None


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
        found.append(AsyncContract(
            contract_id=str(row['contract_id']),
            version=int(row.get('version') or 1),
            submit_tool_key=str(row.get('submit_tool_key') or ''),
            remote_id_pointer=str(row.get('remote_id_pointer') or ''),
            status_tool_key=str(row.get('status_tool_key') or ''),
            status_id_argument=str(row.get('status_id_argument') or ''),
            status_pointer=str(row.get('status_pointer') or ''),
            status_map=dict(row.get('status_map') or {}),
            output_pointer=str(row.get('output_pointer') or ''),
            cancel_tool_key=str(row.get('cancel_tool_key') or ''),
            poll_interval_seconds=float(row.get('poll_interval_seconds') or 5),
            deadline_seconds=float(row.get('deadline_seconds') or 1800),
            recoverable=bool(row.get('recoverable', True))))
    return found


def match_submit(tool_key: str) -> AsyncContract | None:
    for contract in load():
        if contract.submit_tool_key and contract.submit_tool_key == tool_key:
            return contract
    return None


def match_id(contract_id: str, version: int | None = None) -> AsyncContract | None:
    for contract in load():
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
    """提交回执如果只是受理，标成 accepted，供观察去等，而不是当成做完了。"""
    if not settings.async_tool_runtime_enabled or not isinstance(payload, dict):
        return {}
    contract = match_submit(tool_key)
    if contract is None or not contract.recoverable:
        return {}
    remote = remote_id_of(payload, contract)
    if not remote:
        return {}
    mapped = map_status(pointer(payload, contract.status_pointer), contract)
    if mapped in ('succeeded', 'failed', 'cancelled'):
        return {}
    return {'async_accepted': True, 'remote_task_id': remote,
            'contract_id': contract.contract_id,
            'contract_version': contract.version,
            'execution_status': 'accepted', 'phase': 'accepted'}
