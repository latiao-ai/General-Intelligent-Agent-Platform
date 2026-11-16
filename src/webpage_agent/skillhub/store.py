"""技能安装状态（按主体）持久化。

单机中台用 JSON 文件即可：`<SKILLHUB_STATE_DIR>/<tenant>__<user>.json`。
写入用临时文件 + `os.replace`，避免进程中断留下半份 JSON。

目录**不写死**：这份文件就是"用户装了哪些技能"的唯一记录，删了只能让人重装一遍。
默认落在 `profile/skillhub`，部署时用 `SKILLHUB_STATE_DIR` 指到挂载卷，
重装代码/重新发布才不会连它一起清掉。
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from webpage_agent.core.config import settings

_PROFILE = Path(settings.skillhub_state_dir)
_LOCK = threading.Lock()


def _principal_key(principal) -> str:
    tenant = str(getattr(principal, 'tenant_id', 'default') or 'default')
    user = str(getattr(principal, 'user_id', 'default') or 'default')
    raw = f'{tenant}__{user}'
    return re.sub(r'[^0-9A-Za-z_.-]', '_', raw)[:120] or 'default'


def _path(principal) -> Path:
    return _PROFILE / f'{_principal_key(principal)}.json'


def load(principal) -> dict:
    path = _path(principal)
    with _LOCK:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
    return {
        'installed': [str(x) for x in (data.get('installed') or []) if str(x).strip()],
        'custom': [x for x in (data.get('custom') or []) if isinstance(x, dict) and x.get('id')],
    }


def save(principal, data: dict) -> None:
    path = _path(principal)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'installed': list(dict.fromkeys(data.get('installed') or [])),
        'custom': list(data.get('custom') or []),
    }
    tmp = path.with_suffix('.tmp')
    with _LOCK:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, path)
