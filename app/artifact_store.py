"""产物与结果文件只允许落在配置的根目录里。

多台机器部署时，把这个目录挂成共享卷。调用方拿不到绝对路径去拼别的租户目录。
"""
import shutil
import time
from pathlib import Path


class PathDenied(ValueError):
    """路径越出了允许的根目录。"""


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


def gc_unregistered(root, *, grace_seconds: float, now_ts: float | None = None) -> int:
    """删掉超过宽限期、且没有 manifest 的结果目录。

    已写完 manifest 的结果不动。宽限期是 0 时什么都不删。
    """
    if grace_seconds <= 0:
        return 0
    base = Path(root) / '_tool_results'
    if not base.is_dir():
        return 0
    current = time.time() if now_ts is None else now_ts
    removed = 0
    for tenant_dir in base.iterdir():
        if not tenant_dir.is_dir():
            continue
        for folder in tenant_dir.iterdir():
            if not folder.is_dir() or (folder / 'manifest.json').is_file():
                continue
            try:
                age = current - folder.stat().st_mtime
            except OSError:
                continue
            if age < grace_seconds:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed
