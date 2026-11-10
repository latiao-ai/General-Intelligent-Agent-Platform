"""每用户私有目录，以及关在目录里的路径解析。

ticket 模式下每个已登录用户有一块按「租户 + 用户」命名的私有目录。
文件工具、Pi 子进程的工作目录、产物和工具结果都落在这里面。
`..`、绝对路径、以及指向目录外的符号链接一律拒绝。

本地单用户（`AUTH_MODE=off`，主体是 default/default）仍把产物写在
`ARTIFACT_DIR/<task_id>`，避免改掉既有测试和单机目录。
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import re
import sys
from pathlib import Path

from webpage_agent.core.config import settings

_SCOPE: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    'artifact_scope', default=('default', 'default'))
_WORKSPACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    'workspace_dir', default='')

_SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_TASK = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


class SandboxError(ValueError):
    """路径越出了允许的根目录。"""


def segment(value: str) -> str:
    """目录名。本身就是安全记号就原样用，否则用哈希，避免截断后撞车或带上 `..`。"""
    text = str(value or 'default')
    if _SEGMENT.fullmatch(text):
        return text
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def use_scope(tenant_id: str, user_id: str) -> contextvars.Token:
    return _SCOPE.set((str(tenant_id or 'default'), str(user_id or 'default')))


def reset_scope(token: contextvars.Token) -> None:
    _SCOPE.reset(token)


def current_scope() -> tuple[str, str]:
    return _SCOPE.get()


def use_workspace(path: str) -> contextvars.Token:
    return _WORKSPACE.set(str(path or ''))


def reset_workspace(token: contextvars.Token) -> None:
    _WORKSPACE.reset(token)


def current_workspace() -> str:
    return _WORKSPACE.get()


def is_legacy(tenant_id: str, user_id: str) -> bool:
    """本地单用户（没有真实 user_id）仍走原来的共享产物根。"""
    return str(user_id or 'default') == 'default'


def local_pick_allowed() -> bool:
    """原生「打开本机文件夹」只在开发机的 macOS 上开放。生产环境不让用户点服务器目录。"""
    return sys.platform == 'darwin' and settings.app_env != 'production'


def workspace_root() -> Path:
    root = Path(settings.workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    if root.is_symlink() or _is_symlink(resolved):
        raise SandboxError('工作目录根不能是符号链接')
    return resolved


def provision(tenant_id: str, user_id: str) -> Path:
    """创建并返回该用户的私有根目录。根本身若是符号链接则拒绝，不跟着走。"""
    root = workspace_root()
    path = root / segment(tenant_id) / segment(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SandboxError('私有目录不能是符号链接')
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    if not inside(root, resolved) or path.is_symlink():
        raise SandboxError('私有目录越出工作目录根')
    return resolved


def inside(root: Path, path: Path) -> bool:
    base = root.resolve()
    return path == base or base in path.parents


def stored_path_ok(path: str, private: Path) -> bool:
    """已保存的工作目录还能不能用。生产环境只接受私有根下面的目录。"""
    text = str(path or '').strip()
    if not text or '\x00' in text:
        return False
    try:
        resolved = Path(text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not resolved.is_dir() or _is_symlink(Path(text).expanduser()):
        return False
    if inside(private, resolved):
        return True
    return local_pick_allowed()


def namespace_dir(private: Path, name: str) -> Path:
    """在私有根下新建一个命名空间。名字只是标签，不拿来当路径。"""
    label = str(name or '').strip()
    if (not label or len(label) > 80 or '\x00' in label
            or '/' in label or '\\' in label or label in ('.', '..')):
        raise SandboxError('命名空间名称不合法')
    slug = re.sub(r'[^A-Za-z0-9_.-]+', '-', label).strip('.-')[:40] or 'space'
    digest = hashlib.sha256(label.encode('utf-8')).hexdigest()[:8]
    folder = private / 'spaces' / f'{slug}-{digest}'
    if folder.is_symlink() or folder.parent.is_symlink():
        raise SandboxError('命名空间不能是符号链接')
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / '.space-name'
    marker.write_text(label, encoding='utf-8')
    resolved = folder.resolve()
    if not inside(private, resolved):
        raise SandboxError('命名空间越出私有目录')
    return resolved


def list_namespaces(private: Path) -> list[dict]:
    spaces = private / 'spaces'
    found = [{'name': '我的空间', 'path': str(private)}]
    if not spaces.is_dir() or spaces.is_symlink():
        return found
    for entry in sorted(spaces.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry.is_symlink():
            continue
        resolved = entry.resolve()
        if not inside(private, resolved):
            continue
        try:
            label = (entry / '.space-name').read_text(encoding='utf-8').strip()
        except OSError:
            label = entry.name
        if label:
            found.append({'name': label, 'path': str(resolved)})
    return found


def namespace_path(private: Path, name: str) -> Path:
    label = str(name or '').strip()
    if label in ('', '我的空间', '默认'):
        return private
    for item in list_namespaces(private):
        if item['name'] == label:
            return Path(item['path'])
    raise SandboxError('命名空间不存在')


def confine(root: Path, relative: str) -> Path:
    """把相对路径解析到 root 内。绝对路径、`..`、逃出的符号链接都拒绝。"""
    base = _real_directory(root)
    text = str(relative if relative is not None else '.').strip().replace('\\', '/')
    if not text or '\x00' in text:
        raise SandboxError('路径不合法')
    if text.startswith('/') or re.match(r'^[A-Za-z]:', text):
        raise SandboxError('不允许绝对路径')
    parts = [part for part in text.split('/') if part not in ('', '.')]
    if any(part == '..' for part in parts):
        raise SandboxError('不允许路径穿越')
    current = base
    for part in parts:
        if part in ('.', '..'):
            raise SandboxError('不允许路径穿越')
        nxt = current / part
        if nxt.is_symlink():
            target = nxt.resolve()
            if not inside(base, target):
                raise SandboxError('符号链接越出工作目录')
            current = target
            continue
        current = nxt
    if current.exists():
        resolved = current.resolve()
    else:
        parent = current.parent.resolve()
        if not inside(base, parent):
            raise SandboxError('路径越出工作目录')
        resolved = parent / current.name
    if not inside(base, resolved):
        raise SandboxError('路径越出工作目录')
    return resolved


def open_confined(root: Path, relative: str, flags: int, mode: int = 0o644) -> int:
    """打开文件并再核对一次实际路径，避免检查之后被换成指向外部的符号链接。"""
    path = confine(root, relative)
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags | nofollow, mode)
    except OSError as exc:
        raise SandboxError('无法打开该路径') from exc
    try:
        opened = Path(os.readlink(f'/proc/self/fd/{fd}')).resolve()
    except OSError:
        os.close(fd)
        raise SandboxError('无法确认打开的文件') from None
    if not inside(_real_directory(root), opened):
        os.close(fd)
        raise SandboxError('路径越出工作目录')
    return fd


def owner_root(tenant_id: str | None = None, user_id: str | None = None) -> Path:
    """产物与工具结果的磁盘根。default/default 保持原来的 ARTIFACT_DIR。"""
    if tenant_id is None or user_id is None:
        tenant_id, user_id = current_scope()
    base = Path(settings.artifact_dir)
    if is_legacy(tenant_id, user_id):
        return base
    path = base / 'owners' / segment(str(tenant_id)) / segment(str(user_id))
    return path


def relative_owner(tenant_id: str, user_id: str, name: str) -> str:
    safe = _task_name(name)
    return f'owners/{segment(tenant_id)}/{segment(user_id)}/{safe}'


def task_dir(task_id: str, tenant_id: str | None = None,
             user_id: str | None = None) -> Path:
    if tenant_id is None or user_id is None:
        tenant_id, user_id = current_scope()
    return owner_root(tenant_id, user_id) / _task_name(task_id)


def contained_file(root: Path, candidate: Path) -> Path:
    """产物下载用：解析后的文件必须落在该用户的产物根里，并且不是逃出去的链接。"""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    base = base.resolve()
    if candidate.is_symlink():
        raise SandboxError('产物路径不能是符号链接')
    resolved = candidate.resolve()
    if not inside(base, resolved) or not resolved.is_file():
        raise SandboxError('产物不在该用户目录内')
    return resolved


def _task_name(task_id: str) -> str:
    name = re.sub(r'[^A-Za-z0-9_-]', '', str(task_id or ''))[:64]
    if not name or not _TASK.fullmatch(name):
        raise SandboxError('task_id 非法')
    return name


def _real_directory(root: Path) -> Path:
    if root.is_symlink():
        raise SandboxError('工作目录不能是符号链接')
    if not root.is_dir():
        raise SandboxError('工作目录不存在')
    resolved = root.resolve()
    if not resolved.is_dir():
        raise SandboxError('工作目录不存在')
    return resolved


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False
