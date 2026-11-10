"""模型选择：列出可用模型、切换当前模型。

模型清单来自 `AVAILABLE_MODELS`（逗号分隔），默认至少包含当前 `LLM_MODEL`。
选择结果写到 `profile/model.json`，进程启动时恢复；切换后下一轮对话/任务生效。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.core.config import settings

router = APIRouter(prefix='/api/models', tags=['models'])
_PATH = Path(__file__).resolve().parents[3] / 'profile' / 'model.json'

_LABELS = {
    'deepseek-v4-1-flash': 'DeepSeek-V4.1-Flash',
    'deepseek-chat': 'DeepSeek-Chat',
    'deepseek-reasoner': 'DeepSeek-Reasoner',
}


def _available() -> list[str]:
    raw = os.getenv('AVAILABLE_MODELS', '')
    ids = [item.strip() for item in raw.split(',') if item.strip()]
    if settings.llm_model and settings.llm_model not in ids:
        ids.insert(0, settings.llm_model)
    return ids


def _label(model_id: str) -> str:
    return _LABELS.get(model_id, model_id)


def restore_model() -> None:
    """启动时恢复上次选择（模型不在可用清单时忽略）。"""
    try:
        saved = json.loads(_PATH.read_text(encoding='utf-8')).get('id')
    except (OSError, ValueError):
        return
    if saved and saved in _available():
        object.__setattr__(settings, 'llm_model', saved)


class SelectModel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=120)


@router.get('')
async def list_models():
    return {'current': settings.llm_model,
            'models': [{'id': item, 'name': _label(item)} for item in _available()]}


@router.post('/select')
async def select_model(payload: SelectModel):
    if payload.id not in _available():
        raise HTTPException(status_code=400, detail='模型不在 AVAILABLE_MODELS 中')
    object.__setattr__(settings, 'llm_model', payload.id)
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps({'id': payload.id}, ensure_ascii=False), encoding='utf-8')
    return {'current': payload.id, 'name': _label(payload.id)}
