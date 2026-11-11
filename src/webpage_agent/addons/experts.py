"""专家库：内置目录 + 库里自定义行，合并成一份可选择的清单。

## 为什么专家不单独做一套执行引擎

WorkBuddy 的"专家"就是一个带人设的 SKILL.md 包，没有自己的执行器。迁移过来照
这个形态：**专家 = 系统提示 + 预设技能/连接器/模型**。会话选中专家后，解析层
把它当成"默认值"，用户显式勾选的技能/连接器优先覆盖。

这样专家不会变成第二套配置系统：想改行为就改提示词与预设，想加能力就用技能，
想接外部系统就用连接器——三条路都指向已有的机制。

内置专家只读（`catalog.EXPERTS`），自定义专家落在 `experts` 表；同名 id 不允许
覆盖内置专家，否则升级会撞车。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from webpage_agent.addons import catalog

MAX_PROMPT_CHARS = 20_000
MAX_NAME_CHARS = 40
MAX_IDS = 40

_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')


class ExpertError(ValueError):
    """专家参数不合法。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ids(values) -> list[str]:
    out, seen = [], set()
    for raw in values or ():
        item = str(raw or '').strip()
        if not item or item in seen or len(item) > 80:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= MAX_IDS:
            break
    return out


def normalize_row(row: dict) -> dict:
    """库行 → 与内置目录同形状的专家视图。"""
    data = dict(row)
    try:
        skill_ids = json.loads(data.pop('skill_ids_json', '[]') or '[]')
    except (TypeError, ValueError):
        skill_ids = []
    try:
        connector_ids = json.loads(data.pop('connector_ids_json', '[]') or '[]')
    except (TypeError, ValueError):
        connector_ids = []
    data['id'] = str(data.get('expert_id') or data.get('id') or '')
    data['skill_ids'] = _ids(skill_ids)
    data['connector_ids'] = _ids(connector_ids)
    data['builtin'] = False
    data['editable'] = True
    data['icon'] = str(data.get('icon') or '🧩')
    data['color'] = str(data.get('color') or '#eef4ff')
    data['tag'] = str(data.get('tag') or '自定义')
    data['featured'] = False
    return data


def validate(payload: dict, *, partial: bool = False) -> dict:
    """校验写接口的入参，返回清洗后的字段（不含 id/时间戳）。"""
    fields: dict = {}
    name = str(payload.get('name') or '').strip()
    if name or not partial:
        if not name:
            raise ExpertError('专家需要一个名字')
        if len(name) > MAX_NAME_CHARS:
            raise ExpertError(f'名字最长 {MAX_NAME_CHARS} 个字')
        fields['name'] = name
    prompt = str(payload.get('system_prompt') or payload.get('prompt') or '').strip()
    if prompt or not partial:
        if not prompt:
            raise ExpertError('专家需要 system_prompt（人设与工作方式）')
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ExpertError(f'系统提示最长 {MAX_PROMPT_CHARS} 字')
        fields['system_prompt'] = prompt
    if 'description' in payload or not partial:
        description = str(payload.get('description') or '').strip()
        if len(description) > 240:
            raise ExpertError('简介最长 240 字')
        fields['description'] = description or (name[:160] if name else '')
    if 'skill_ids' in payload or not partial:
        fields['skill_ids'] = _ids(payload.get('skill_ids'))
    if 'connector_ids' in payload or not partial:
        fields['connector_ids'] = _ids(payload.get('connector_ids'))
    if 'model' in payload or not partial:
        model = str(payload.get('model') or '').strip()
        if len(model) > 80:
            raise ExpertError('模型 id 过长')
        fields['model'] = model
    for key, default in (('icon', '🧩'), ('color', '#eef4ff'), ('tag', '自定义')):
        if key in payload or not partial:
            value = str(payload.get(key) or default).strip()
            fields[key] = value[:40] or default
    return fields


def _check_id(expert_id: str) -> str:
    text = str(expert_id or '').strip()
    if not _ID_RE.match(text):
        raise ExpertError('专家 id 不合法')
    if catalog.get(text) is not None:
        raise ExpertError('不能使用内置专家的 id')
    return text


async def list_experts(store, *, tenant_id: str, owner_user_id: str) -> list[dict]:
    """内置在前、自定义在后；自定义同名同主体只留一行。"""
    items = catalog.all_experts()
    rows = await store.list_expert_rows(tenant_id=tenant_id,
                                        owner_user_id=owner_user_id)
    seen = {item['id'] for item in items}
    for row in rows:
        item = normalize_row(row)
        if not item['id'] or item['id'] in seen:
            continue
        items.append(item)
        seen.add(item['id'])
    return items


async def get_expert(store, expert_id: str, *, tenant_id: str,
                     owner_user_id: str) -> dict | None:
    builtin = catalog.get(expert_id)
    if builtin is not None:
        return builtin
    row = await store.expert_row(expert_id, tenant_id=tenant_id,
                                 owner_user_id=owner_user_id)
    return normalize_row(row) if row else None


async def create_expert(store, payload: dict, *, tenant_id: str,
                        owner_user_id: str) -> dict:
    fields = validate(payload)
    expert_id = str(payload.get('id') or '').strip() or 'exp-' + uuid.uuid4().hex[:10]
    _check_id(expert_id)
    if await store.expert_row(expert_id, tenant_id=tenant_id,
                              owner_user_id=owner_user_id):
        raise ExpertError('专家 id 已存在')
    row = await store.create_expert_row(
        expert_id=expert_id, tenant_id=tenant_id, owner_user_id=owner_user_id,
        created_at=_now(), updated_at=_now(), **fields)
    return normalize_row(row)


async def update_expert(store, expert_id: str, payload: dict, *, tenant_id: str,
                        owner_user_id: str) -> dict:
    _check_id(expert_id)
    fields = validate(payload, partial=True)
    if not fields:
        raise ExpertError('没有要更新的字段')
    fields['updated_at'] = _now()
    row = await store.update_expert_row(expert_id, tenant_id=tenant_id,
                                        owner_user_id=owner_user_id, **fields)
    if row is None:
        raise LookupError('专家不存在')
    return normalize_row(row)


async def delete_expert(store, expert_id: str, *, tenant_id: str,
                        owner_user_id: str) -> bool:
    if catalog.get(expert_id) is not None:
        raise ExpertError('内置专家不能删除')
    return await store.delete_expert_row(expert_id, tenant_id=tenant_id,
                                         owner_user_id=owner_user_id)
