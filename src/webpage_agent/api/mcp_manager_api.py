"""MCP 服务管理：读取/保存用户自定义远程 MCP 配置。

与 WorkBuddy 的 `~/.workbuddy/mcp.json` 同一形态：

```json
{"mcpServers": {"name": {"type": "streamable-http", "url": "https://.../mcp"}}}
```

中台只接受 HTTP/SSE。stdio 需要在本机起进程，属于运维配置（MCP_SERVERS），
不能让浏览器里的一条 JSON 变成任意命令执行。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from webpage_agent.governance import policies
from webpage_agent.integrations import mcp_servers

router = APIRouter(prefix='/api/mcp', tags=['mcp'])

_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_REMOTE_TYPES = {'streamable-http', 'streamable_http', 'http', 'sse'}


class ConfigBody(BaseModel):
    content: str = Field(max_length=200_000)


def _path() -> Path:
    return mcp_servers.user_config_path()


def _ensure_file() -> Path:
    path = _path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'mcpServers': {}}, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


def _read() -> tuple[Path, str]:
    path = _ensure_file()
    return path, path.read_text(encoding='utf-8')


def _validate(content: str) -> dict:
    try:
        root = json.loads(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='不是合法 JSON：' + str(exc)) from exc
    if not isinstance(root, dict) or isinstance(root, list):
        raise HTTPException(status_code=400, detail='根节点必须是对象')
    servers = root.get('mcpServers')
    if not isinstance(servers, dict):
        raise HTTPException(status_code=400, detail='mcpServers 必须是对象')
    for name, entry in servers.items():
        if not isinstance(name, str) or not _NAME.match(name) or '__' in name:
            raise HTTPException(status_code=400,
                                detail=f'服务名不合法：{name!r}（字母数字 _ . -，且不能含 __）')
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=f'{name} 必须是对象')
        kind = str(entry.get('type') or 'streamable-http').strip().lower()
        if kind == 'stdio' or entry.get('command'):
            raise HTTPException(
                status_code=400,
                detail=f'{name} 是 stdio MCP；中台只接受 HTTP/SSE，'
                       '本地进程请配置 MCP_SERVERS')
        if kind not in _REMOTE_TYPES:
            raise HTTPException(status_code=400,
                                detail=f'{name} 的 type 不支持：{kind}')
        url = str(entry.get('url') or '').strip()
        if not url.startswith(('http://', 'https://')):
            raise HTTPException(status_code=400, detail=f'{name} 缺少合法 url')
        headers = entry.get('headers')
        if headers is not None:
            if not isinstance(headers, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str)
                    for k, v in headers.items()):
                raise HTTPException(status_code=400,
                                    detail=f'{name} 的 headers 必须是字符串键值对象')
        timeout = entry.get('timeout')
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            raise HTTPException(status_code=400, detail=f'{name} 的 timeout 必须是正数')
    return root


@router.get('/config')
async def get_config(principal=Depends(policies.current_principal)):
    path, content = _read()
    return {'path': str(path), 'content': content}


@router.put('/config')
async def save_config(body: ConfigBody,
                      principal=Depends(policies.current_principal)):
    _validate(body.content)
    path = _ensure_file()
    tmp = path.with_suffix('.tmp')
    tmp.write_text(body.content, encoding='utf-8')
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return {'ok': True, 'path': str(path), 'content': body.content}


@router.get('/servers')
async def list_servers(principal=Depends(policies.current_principal)):
    path, content = _read()
    try:
        user_names = set((json.loads(content).get('mcpServers') or {}).keys())
    except (ValueError, AttributeError):
        user_names = set()
    configured = mcp_servers.servers()
    items = []
    for server in configured:
        items.append({
            'id': server.id,
            'name': server.title or server.id,
            'transport': server.transport,
            'url': server.url,
            'source': 'custom' if server.id in user_names else 'system',
        })
    return {'path': str(path), 'items': items}
