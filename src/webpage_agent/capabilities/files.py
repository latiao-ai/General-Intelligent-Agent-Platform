"""工作目录里的文件工具：列目录、读、写。路径不能逃出当前用户的工作目录。"""
from __future__ import annotations

import os
from pathlib import Path

from webpage_agent.capabilities.base import ToolError
from webpage_agent.capabilities.registry import tool
from webpage_agent.core import sandbox

CAPABILITY = 'files'
PROMPT_RULES = (
    '【本地文件】用户要看、改自己工作目录里的文件时，用 list_dir / read_file / write_file。'
    '路径一律相对工作目录，不要使用绝对路径或 .. 。'
)

_READ_LIMIT = 200_000
_WRITE_LIMIT = 1_000_000
_LIST_LIMIT = 200


def _root(state: dict) -> Path:
    raw = str((state or {}).get('workspace_dir') or '').strip()
    if raw:
        path = Path(raw)
        if path.is_dir() and not path.is_symlink():
            return path.resolve()
    tenant = str((state or {}).get('tenant_id') or 'default')
    user = str((state or {}).get('owner_user_id') or 'default')
    return sandbox.provision(tenant, user)


def _denied(exc: sandbox.SandboxError) -> ToolError:
    return ToolError(str(exc), certainty='not_executed', error_code='E_PATH_DENIED')


@tool(name='list_dir', label='列出目录', effect='read',
      key='local:files:list_dir',
      description='列出当前工作目录下的文件和子目录。path 相对工作目录，省略表示根。',
      args={'type': 'object',
            'properties': {'path': {'type': 'string'}},
            'required': []})
async def list_dir(args: dict, state: dict) -> dict:
    try:
        root = _root(state)
        folder = sandbox.confine(root, str((args or {}).get('path') or '.'))
        if not folder.is_dir():
            raise sandbox.SandboxError('不是目录')
        entries = []
        for entry in sorted(folder.iterdir(), key=lambda item: item.name):
            if len(entries) >= _LIST_LIMIT:
                break
            if entry.is_symlink():
                kind = 'symlink'
            elif entry.is_dir():
                kind = 'dir'
            else:
                kind = 'file'
            entries.append({'name': entry.name, 'kind': kind})
        rel = '.' if folder == root else str(folder.relative_to(root))
        return {'path': rel, 'entries': entries}
    except sandbox.SandboxError as exc:
        raise _denied(exc) from exc


@tool(name='read_file', label='读取文件', effect='read',
      key='local:files:read_file',
      description='读取当前工作目录里的文本文件。path 相对工作目录。',
      args={'type': 'object',
            'properties': {'path': {'type': 'string'}},
            'required': ['path']})
async def read_file(args: dict, state: dict) -> dict:
    relative = str((args or {}).get('path') or '')
    try:
        root = _root(state)
        fd = sandbox.open_confined(root, relative, os.O_RDONLY)
        try:
            data = os.read(fd, _READ_LIMIT + 1)
        finally:
            os.close(fd)
    except sandbox.SandboxError as exc:
        raise _denied(exc) from exc
    truncated = len(data) > _READ_LIMIT
    text = data[:_READ_LIMIT].decode('utf-8', errors='replace')
    return {'path': relative, 'text': text, 'truncated': truncated}


@tool(name='write_file', label='写入文件', effect='write', file_output=True,
      key='local:files:write_file',
      description='把文本写入当前工作目录。path 相对工作目录，不能逃出该目录。',
      args={'type': 'object',
            'properties': {'path': {'type': 'string'},
                           'text': {'type': 'string'}},
            'required': ['path', 'text']})
async def write_file(args: dict, state: dict) -> dict:
    relative = str((args or {}).get('path') or '')
    text = str((args or {}).get('text') if (args or {}).get('text') is not None else '')
    try:
        if len(text.encode('utf-8')) > _WRITE_LIMIT:
            raise sandbox.SandboxError('内容超过单文件上限')
        root = _root(state)
        path = sandbox.confine(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = sandbox.open_confined(
            root, relative, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            payload = text.encode('utf-8')
            os.write(fd, payload)
        finally:
            os.close(fd)
    except sandbox.SandboxError as exc:
        raise _denied(exc) from exc
    return {'path': relative, 'bytes': len(text.encode('utf-8')),
            'written': [relative]}
