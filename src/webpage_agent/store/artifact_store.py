"""产物与结果文件只允许落在配置的根目录里。

多台机器部署时，把这个目录挂成共享卷。调用方拿不到绝对路径去拼别的租户目录。
"""
import json
import re
import shutil
import time
from pathlib import Path


class PathDenied(ValueError):
    """路径越出了允许的根目录。"""


# 任务产出的目录名就是 task_id（uuid4 的 hex）。
TASK_DIR_NAME = re.compile(r'[a-f0-9]{32}')


def contained(root, relative: str) -> Path:
    """把相对路径解析到根目录内。绝对路径、`..` 和越界都拒绝。"""
    base = Path(root).resolve()
    text = str(relative or '').strip()
    if not text or text.startswith(('/', '\\')) or ':' in text[:3]:
        raise PathDenied('路径不在产物目录内')
    parts = Path(text).parts
    if any(part in ('..', '') for part in parts):
        raise PathDenied('路径不在产物目录内')
    path = (base / text).resolve()
    if path != base and base not in path.parents:
        raise PathDenied('路径不在产物目录内')
    return path


def gc_orphan_task_dirs(root, *, grace_seconds: float, now_ts: float | None = None,
                        live_task_ids: set[str] | None = None) -> int:
    """删掉磁盘上已经没有对应任务记录的任务目录。

    `_tool_results` 有 manifest 表可以反查孤儿；任务目录（`state.json`、
    `results/`、产出的 HTML）**没有登记表**，只能靠目录名（就是 task_id）
    是否还在 `tasks` 里判断。库重建、任务被删之后，这些目录不会被任何保留策略
    扫到，会一直留在磁盘上。

    安全边界：
    - 只处理名字是 32 位十六进制的普通目录，`_tool_results` 之类不受影响；
    - 符号链接既不跟随也不删除；
    - 宽限期 <= 0 或没有给出在用 task 集合时什么都不做，避免与正在写新目录的
      进程抢跑。
    """
    if grace_seconds <= 0 or live_task_ids is None:
        return 0
    base = Path(root)
    if base.is_symlink() or not base.is_dir():
        return 0
    current = time.time() if now_ts is None else now_ts
    removed = 0
    for entry in base.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        if not TASK_DIR_NAME.fullmatch(entry.name) or entry.name in live_task_ids:
            continue
        try:
            age = current - entry.stat().st_mtime
        except OSError:
            continue
        if age < grace_seconds:
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            continue
        removed += 1
    owners = base / 'owners'
    if owners.is_dir() and not owners.is_symlink():
        for tenant_dir in owners.iterdir():
            if tenant_dir.is_symlink() or not tenant_dir.is_dir():
                continue
            for user_dir in tenant_dir.iterdir():
                if user_dir.is_symlink() or not user_dir.is_dir():
                    continue
                for entry in user_dir.iterdir():
                    if entry.is_symlink() or not entry.is_dir():
                        continue
                    if not TASK_DIR_NAME.fullmatch(entry.name) or entry.name in live_task_ids:
                        continue
                    try:
                        age = current - entry.stat().st_mtime
                    except OSError:
                        continue
                    if age < grace_seconds:
                        continue
                    try:
                        shutil.rmtree(entry)
                    except OSError:
                        continue
                    removed += 1
    return removed


def gc_unregistered(root, *, grace_seconds: float, now_ts: float | None = None,
                    registered_ids: set[str] | None = None) -> int:
    """删掉超过宽限期、且没有 manifest 的结果目录。

    已写完 manifest 的结果不动。宽限期是 0 时什么都不删。
    """
    if grace_seconds <= 0:
        return 0
    base = Path(root) / '_tool_results'
    if base.is_symlink() or not base.is_dir():
        return 0
    current = time.time() if now_ts is None else now_ts
    removed = 0
    for tenant_dir in base.iterdir():
        if tenant_dir.is_symlink() or not tenant_dir.is_dir():
            continue
        for folder in tenant_dir.iterdir():
            if folder.is_symlink() or not folder.is_dir():
                continue
            marker = folder / 'manifest.json'
            if marker.is_file():
                if registered_ids is None:
                    continue
                try:
                    result_id = json.loads(marker.read_text()).get('result_id')
                except (OSError, ValueError, AttributeError):
                    continue
                if not result_id or result_id in registered_ids:
                    continue
            try:
                age = current - folder.stat().st_mtime
            except OSError:
                continue
            if age < grace_seconds:
                continue
            try:
                shutil.rmtree(folder)
            except OSError:
                continue
            removed += 1
    return removed
