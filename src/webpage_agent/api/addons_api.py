"""专家与会话级选择的接口（迁移自 WorkBuddy 的「专家 · 技能 · 连接器」模型）。

三组接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/experts` | 内置 + 自定义专家清单 |
| POST | `/api/experts` | 新建自定义专家（提示词 + 预设技能/连接器/模型） |
| PATCH | `/api/experts/{id}` | 改自定义专家（内置专家只读） |
| DELETE | `/api/experts/{id}` | 删自定义专家（内置专家 400） |
| GET | `/api/sessions/{sid}/addons` | 当前会话的选择 + **实际生效**的资源 |
| PATCH | `/api/sessions/{sid}/addons` | 聊天框里勾选写这里（不是写进消息） |

## 为什么选择挂在会话上

WorkBuddy 的结论：**会话是选择单元，不是消息**。用户在输入框里勾的东西改的是
会话配置，下一轮才生效；消息里的 `@` 只是把"用这个技能"写进提示。照这个模型，
`PATCH /addons` 是唯一的写入口，发送消息时不带选择也不会丢。

技能的安装/删除在 `/api/skills/*`（技能中心），连接器的连接/授权在
`/api/connectors/*`，这里只负责"选了谁"。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from webpage_agent.addons import experts as experts_module
from webpage_agent.addons import resolver
from webpage_agent.governance import policies

router = APIRouter(prefix='/api', tags=['addons'])

MAX_LIST = 50


class ExpertBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='', max_length=80)
    description: str = Field(default='', max_length=400)
    system_prompt: str = Field(default='', max_length=24_000)
    skill_ids: list[str] = Field(default_factory=list)
    connector_ids: list[str] = Field(default_factory=list)
    model: str = Field(default='', max_length=120)
    icon: str = Field(default='🧩', max_length=40)
    color: str = Field(default='#eef4ff', max_length=40)
    tag: str = Field(default='自定义', max_length=40)


class AddonsPatch(BaseModel):
    """只覆盖出现过的键。

    `expert_id: null` = 取消专家；`skill_ids: []` = 明确不要技能；
    **不传** `skill_ids` = 沿用专家预设。这三种语义不能混。
    """

    model_config = ConfigDict(extra='forbid')
    expert_id: str | None = Field(default=None, max_length=80)
    skill_ids: list[str] | None = Field(default=None, max_length=MAX_LIST)
    connector_ids: list[str] | None = Field(default=None, max_length=MAX_LIST)
    model: str | None = Field(default=None, max_length=120)


def _store(request: Request):
    store = getattr(request.app.state, 'store', None)
    if store is None:
        raise HTTPException(status_code=503, detail='服务未就绪：没有数据库连接')
    return store


def _ids(principal) -> tuple[str, str]:
    return (str(getattr(principal, 'tenant_id', 'default') or 'default'),
            str(getattr(principal, 'user_id', 'default') or 'default'))


async def _owned_session(store, session_id: str, principal) -> dict:
    """会话归属检查：与读取消息、订阅 SSE 用同一道闸，不同入口不能各自宽松。"""
    row = await store.session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail='会话不存在')
    return policies.ensure_owner(row, principal, '会话')


# ---------------------------------------------------------------- 专家


@router.get('/experts')
async def list_experts(request: Request,
                       principal=Depends(policies.current_principal)):
    tenant, user = _ids(principal)
    items = await experts_module.list_experts(_store(request), tenant_id=tenant,
                                              owner_user_id=user)
    return {'items': items, 'total': len(items),
            'builtin': sum(1 for item in items if item.get('builtin')),
            'custom': sum(1 for item in items if not item.get('builtin'))}


@router.post('/experts', status_code=201)
async def create_expert(body: ExpertBody, request: Request,
                        principal=Depends(policies.current_principal)):
    tenant, user = _ids(principal)
    try:
        return await experts_module.create_expert(
            _store(request), body.model_dump(), tenant_id=tenant, owner_user_id=user)
    except experts_module.ExpertError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.patch('/experts/{expert_id}')
async def update_expert(expert_id: str, body: ExpertBody, request: Request,
                        principal=Depends(policies.current_principal)):
    tenant, user = _ids(principal)
    payload = body.model_dump(exclude_unset=True)
    try:
        return await experts_module.update_expert(
            _store(request), expert_id, payload, tenant_id=tenant, owner_user_id=user)
    except experts_module.ExpertError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except LookupError:
        raise HTTPException(status_code=404, detail='专家不存在') from None


@router.delete('/experts/{expert_id}')
async def delete_expert(expert_id: str, request: Request,
                        principal=Depends(policies.current_principal)):
    tenant, user = _ids(principal)
    try:
        ok = await experts_module.delete_expert(
            _store(request), expert_id, tenant_id=tenant, owner_user_id=user)
    except experts_module.ExpertError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not ok:
        raise HTTPException(status_code=404, detail='专家不存在')
    return {'id': expert_id, 'deleted': True}


# ---------------------------------------------------------------- 会话选择


def _view(resolved: dict) -> dict:
    """解析结果 → 前端视图。不把整段提示词回给浏览器：它属于服务端装配细节。"""
    expert = resolved.get('expert')
    return {
        'session_id': resolved.get('session_id'),
        'addon_selection': resolved.get('addon_selection') or {},
        'expert_id': resolved.get('expert_id') or '',
        'expert': ({'id': expert.get('id'), 'name': expert.get('name'),
                    'icon': expert.get('icon'), 'description': expert.get('description'),
                    'builtin': bool(expert.get('builtin')),
                    'skill_ids': list(expert.get('skill_ids') or []),
                    'connector_ids': list(expert.get('connector_ids') or []),
                    'model': expert.get('model') or ''}
                   if expert else None),
        'expert_missing': bool(resolved.get('expert_missing')),
        'skill_ids': list(resolved.get('skill_ids') or []),
        'requested_skill_ids': list(resolved.get('requested_skill_ids') or []),
        'missing_skill_ids': list(resolved.get('missing_skill_ids') or []),
        'skills': [{key: row.get(key) for key in
                    ('id', 'name', 'description', 'enabled', 'source', 'dir')}
                   for row in (resolved.get('skills') or [])],
        'skills_explicit': bool(resolved.get('skills_explicit')),
        'connector_ids': resolved.get('connector_ids'),
        'connectors_explicit': bool(resolved.get('connectors_explicit')),
        # True = 连接器已由会话选择/专家预设决定；False = 沿用旧行为（看消息字段）
        'connectors_decided': bool(resolved.get('connectors_decided',
                                                resolved.get('connectors_explicit'))),
        'model': resolved.get('model') or '',
    }


@router.get('/sessions/{session_id}/addons')
async def get_session_addons(session_id: str, request: Request,
                             principal=Depends(policies.current_principal)):
    store = _store(request)
    await _owned_session(store, session_id, principal)
    tenant, user = _ids(principal)
    resolved = await resolver.resolve(store, session_id=session_id, tenant_id=tenant,
                                      owner_user_id=user, materialize=False)
    return _view(resolved)


@router.patch('/sessions/{session_id}/addons')
async def patch_session_addons(session_id: str, body: AddonsPatch, request: Request,
                               principal=Depends(policies.current_principal)):
    """聊天框勾选写这里。合并语义见 `addons.resolver.merge_selection`。"""
    store = _store(request)
    await _owned_session(store, session_id, principal)
    tenant, user = _ids(principal)
    patch = body.model_dump(exclude_unset=True)

    # 专家必须真实存在：存一个失效 id 会让之后的每一轮都悄悄丢掉人设。
    if patch.get('expert_id'):
        found = await experts_module.get_expert(
            store, patch['expert_id'], tenant_id=tenant, owner_user_id=user)
        if found is None:
            raise HTTPException(status_code=400, detail='专家不存在')

    current = await store.session_addons(session_id)
    merged = resolver.merge_selection(current, patch)
    saved = await store.set_session_addons(session_id, merged)
    if saved is None:
        raise HTTPException(status_code=404, detail='会话不存在')
    resolved = await resolver.resolve(store, session_id=session_id, tenant_id=tenant,
                                      owner_user_id=user, selection=saved,
                                      materialize=False)
    view = _view(resolved)
    if view['missing_skill_ids']:
        view['notice'] = ('这些技能没有安装或已禁用：' +
                          '、'.join(view['missing_skill_ids']))
    return view
