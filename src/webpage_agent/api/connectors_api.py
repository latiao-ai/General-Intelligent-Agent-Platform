"""连接器 API：把 MCP Server 的启停做成前端可操作的「开关」。

- `GET /api/connectors`：已配置的 MCP Server + 目录里未配置的连接器
- `POST /api/connectors/{id}/connect`：真实建立 MCP 会话并列出工具
- `POST /api/connectors/{id}/disconnect`：关闭开关（保留配置）
- `GET /api/connectors/{id}/tools`：最近一次连接发现的工具

连接状态按主体保存在 `profile/skillhub/connectors/`。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from webpage_agent.governance import policies
from webpage_agent.integrations import mcp_adapter, mcp_servers
from webpage_agent.skillhub import catalog

router = APIRouter(prefix='/api/connectors', tags=['connectors'])
_ROOT = Path(__file__).resolve().parents[3]
_STATE_DIR = _ROOT / 'profile' / 'skillhub' / 'connectors'


def _key(principal) -> str:
    tenant = str(getattr(principal, 'tenant_id', 'default') or 'default')
    user = str(getattr(principal, 'user_id', 'default') or 'default')
    return re.sub(r'[^0-9A-Za-z_.-]', '_', f'{tenant}__{user}')[:120] or 'default'


def _load(principal) -> dict:
    path = _STATE_DIR / f'{_key(principal)}.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _save(principal, data: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _STATE_DIR / f'{_key(principal)}.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _server_view(server, state: dict) -> dict:
    row = state.get(server.id) or {}
    return {
        'id': server.id,
        'name': server.title or server.id,
        'transport': server.transport,
        'configured': True,
        'enabled': bool(row.get('enabled')),
        'tools': int(row.get('tools') or 0),
        'tool_names': list(row.get('tool_names') or []),
        'error': str(row.get('error') or ''),
        'at': str(row.get('at') or ''),
    }


@router.get('')
async def list_connectors(principal=Depends(policies.current_principal)):
    state = _load(principal)
    servers = mcp_servers.servers()
    configured = {server.id for server in servers}
    items = [_server_view(server, state) for server in servers]
    for item in catalog.CONNECTORS:
        if item.get('id') in configured:
            continue
        items.append({
            'id': item['id'], 'name': item.get('name') or item['id'],
            'transport': '', 'configured': False, 'enabled': False,
            'tools': 0, 'tool_names': [], 'error': '',
            'icon': item.get('icon') or '🔗', 'category': item.get('category') or '',
        })
    return {'items': items, 'configured': len(servers),
            'enabled': sum(1 for x in items if x.get('enabled'))}


@router.post('/{connector_id}/connect')
async def connect_connector(connector_id: str,
                            principal=Depends(policies.current_principal)):
    server = mcp_servers.by_id(connector_id)
    if server is None:
        raise HTTPException(status_code=404,
                            detail='该连接器还没有配置 MCP_SERVERS，请先在 .env 添加服务端')
    state = _load(principal)
    try:
        tools = await asyncio.wait_for(mcp_adapter.list_server_tools(server), 40)
    except Exception as exc:                            # noqa: BLE001
        message = f'{type(exc).__name__}: {exc}'[:300]
        state[connector_id] = {'enabled': False, 'tools': 0, 'tool_names': [],
                               'error': message, 'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
        _save(principal, state)
        raise HTTPException(status_code=502, detail=f'连接失败：{message}') from None
    names = [tool.get('name') for tool in tools[:80]]
    state[connector_id] = {'enabled': True, 'tools': len(tools), 'tool_names': names,
                           'error': '', 'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    _save(principal, state)
    return {'id': connector_id, 'enabled': True, 'tools': len(tools),
            'tool_names': names, 'name': server.title or server.id}


@router.post('/{connector_id}/disconnect')
async def disconnect_connector(connector_id: str,
                               principal=Depends(policies.current_principal)):
    if mcp_servers.by_id(connector_id) is None:
        raise HTTPException(status_code=404, detail='连接器不存在')
    state = _load(principal)
    previous = state.get(connector_id) or {}
    state[connector_id] = {**previous, 'enabled': False, 'error': '',
                           'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    _save(principal, state)
    return {'id': connector_id, 'enabled': False}


@router.get('/{connector_id}/tools')
async def connector_tools(connector_id: str,
                          principal=Depends(policies.current_principal)):
    row = _load(principal).get(connector_id) or {}
    return {'id': connector_id, 'enabled': bool(row.get('enabled')),
            'tools': int(row.get('tools') or 0),
            'tool_names': list(row.get('tool_names') or []),
            'error': str(row.get('error') or '')}
