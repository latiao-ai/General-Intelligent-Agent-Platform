"""模型选择：列出可用模型、按用户切换当前模型。

模型清单来自 `AVAILABLE_MODELS`（逗号分隔），默认至少包含当前 `LLM_MODEL`。
选择写在该用户自己的文件里。进程级 `LLM_MODEL` 不被某一次切换改掉，
下一轮对话按当前用户的选择走。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.core import model_choice
from webpage_agent.core.config import settings
from webpage_agent.governance import policies

router = APIRouter(prefix='/api/models', tags=['models'])

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
    """启动钩子。选择按用户保存，不再把某个人的选择写进进程默认模型。"""
    return None


def _current_for(user_id: str) -> str:
    saved = model_choice.load_id(user_id)
    if saved and saved in _available():
        return saved
    return settings.llm_model


class SelectModel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=120)


@router.get('')
async def list_models(request: Request):
    principal = None
    try:
        principal = await policies.current_principal(request)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
    if principal is None:
        current = settings.llm_model
    else:
        current = _current_for(principal.user_id)
    return {'current': current,
            'models': [{'id': item, 'name': _label(item)} for item in _available()]}


@router.post('/select')
async def select_model(payload: SelectModel,
                       principal=Depends(policies.current_principal)):
    if payload.id not in _available():
        raise HTTPException(status_code=400, detail='模型不在 AVAILABLE_MODELS 中')
    model_choice.save(principal.user_id, payload.id)
    return {'current': payload.id, 'name': _label(payload.id)}
