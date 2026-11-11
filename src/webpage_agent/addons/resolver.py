"""每轮解析层：会话选择 → 这一轮真正能用什么。

这是整个迁移的落点。会话里只存"用户勾了什么"（`sessions.addon_selection`），
真正的资源在每轮开始时解析一次：

```
addon_selection {expert_id, skill_ids, connector_ids, model}
        │
        ├── 专家      → 系统提示（人设）+ 默认技能/连接器/模型
        ├── 技能      → 物化进 <workspace>/.agent-skills/<session>/ + 一段索引提示
        └── 连接器    → MCP server 集合（交给既有的 connector_scope + mcp 注入）
```

## 优先级（照 WorkBuddy 的做法）

- **会话显式选择优先于专家预设**：用户在输入框里勾/取消的东西不能被专家覆盖。
- **"没选"与"选了空"是两回事**：`addon_selection` 里没有 `connector_ids` 这个键，
  表示"沿用旧行为"（按会话/消息里的选择走，最终可能是全部已配置）；
  显式 `[]` 表示"这一轮不要任何连接器"。技能同理：显式空数组就是不用技能。
- 每轮重新解析（会话配置随时可改），但 MCP 连接由 `McpManager`/`mcp_adapter`
  复用，解析本身不建立连接、不发网络请求。

解析层只读库和磁盘，**不写**任何状态：物化是重建一个会话级临时目录，属于
"渲染产物"，不是用户数据。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from webpage_agent.addons import experts as experts_module
from webpage_agent.addons import skills as skills_module


def normalize_selection(raw) -> dict:
    """把库里的 addon_selection 收敛成固定形状，缺键=没选（不是空选择）。

    这里也顺手截断长度：这份数据可能来自请求体（第一条消息随 turn 落库），
    不设上限等于让客户端决定库里能塞多长。
    """
    data = raw if isinstance(raw, dict) else {}
    out: dict = {}
    expert_id = str(data.get('expert_id') or '').strip()
    out['expert_id'] = expert_id[:80]
    if 'skill_ids' in data:
        out['skill_ids'] = _clean_ids(data.get('skill_ids'))
    if 'connector_ids' in data:
        out['connector_ids'] = _clean_ids(data.get('connector_ids'))
    model = str(data.get('model') or '').strip()
    out['model'] = model[:120]
    return out


def _clean_ids(values) -> list[str]:
    out, seen = [], set()
    if not isinstance(values, (list, tuple)):
        return out
    for raw in values:
        item = str(raw or '').strip()
        if not item or item in seen or len(item) > 120:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= 100:
            break
    return out


def merge_selection(current: dict, patch: dict) -> dict:
    """PATCH 语义：只覆盖出现过的键。

    - `expert_id: null` → 取消专家；
    - `skill_ids: null` → **删掉**显式选择，回到"用专家预设"（这是"恢复预设"）；
    - `skill_ids: []` → 明确不要技能（连专家预设也不要）；
    - 不传这个键 → 保持原样。

    三种语义必须分开：把 `null` 当成 `[]`，用户一点"恢复预设"就变成"不要技能"。
    """
    merged = normalize_selection(current)
    raw = patch if isinstance(patch, dict) else {}
    if 'expert_id' in raw:
        merged['expert_id'] = str(raw.get('expert_id') or '').strip()
    if 'skill_ids' in raw:
        value = raw.get('skill_ids')
        if value is None:
            merged.pop('skill_ids', None)
        else:
            merged['skill_ids'] = _clean_ids(value)
    if 'connector_ids' in raw:
        value = raw.get('connector_ids')
        if value is None:
            merged.pop('connector_ids', None)
        else:
            merged['connector_ids'] = _clean_ids(value)
    if 'model' in raw:
        merged['model'] = str(raw.get('model') or '').strip()
    return merged


async def resolve(store, *, session_id: str, tenant_id: str, owner_user_id: str,
                  workspace_dir: str = '', selection: dict | None = None,
                  materialize: bool = True) -> dict:
    """解析一次 turn 的资源。返回值是纯数据，调用方按需取用。

    `materialize=False` 用于只读预览（GET 会话选择）：不重建会话技能目录，
    只按磁盘现状算出"生效 / 缺失"，避免一次查询就把别人的目录删了。
    """
    raw = selection if selection is not None else await store.session_addons(session_id)
    addon = normalize_selection(raw)

    expert = None
    if addon.get('expert_id'):
        expert = await experts_module.get_expert(
            store, addon['expert_id'], tenant_id=tenant_id,
            owner_user_id=owner_user_id)

    # 技能：显式列表优先；没这个键才用专家预设。
    skills_explicit = 'skill_ids' in addon
    skill_ids = list(addon.get('skill_ids') or ())
    if not skills_explicit and expert:
        skill_ids = list(expert.get('skill_ids') or ())

    # 连接器：没有这个键 → None（沿用旧行为），有键哪怕是空数组也照办；
    # 专家预设的连接器算"已决定"（用户没单独选时才用），不是"没选"。
    connectors_decided = ('connector_ids' in addon
                          or bool(expert and expert.get('connector_ids')))
    if 'connector_ids' in addon:
        connector_ids: list[str] | None = list(addon.get('connector_ids') or [])
    elif expert and expert.get('connector_ids'):
        connector_ids = list(expert['connector_ids'])
    else:
        connector_ids = None

    model = addon.get('model') or str((expert or {}).get('model') or '')

    materialized = {'dir': '', 'skills': [], 'missing': [],
                    'path': str(Path(workspace_dir or '.') / '.agent-skills')}
    if not materialize:
        # 只读视图（GET /addons）：不碰磁盘，也不因为"没物化"就把技能标成缺失。
        rows, missing = [], []
        for skill_id in skill_ids:
            row = skills_module.get_skill(skill_id)
            if row is None or not row.get('enabled', True):
                missing.append(skill_id)
            else:
                rows.append(row)
        materialized = {'dir': '', 'path': '', 'skills': rows, 'missing': missing,
                        'dry_run': True}
    elif skill_ids:
        if not workspace_dir:
            materialized['missing'] = list(skill_ids)
        else:
            materialized = skills_module.materialize(
                skill_ids, workspace_dir=workspace_dir, session_id=session_id)
    elif workspace_dir:
        # 没选技能也要把上一轮的物化目录清掉：否则模型仍能读到旧技能，
        # 用户"取消了技能却还在用"这种鬼故事就是这么来的。
        base, relative = skills_module.session_dir(workspace_dir, session_id)
        shutil.rmtree(base, ignore_errors=True)
        materialized = {'dir': '', 'path': str(base), 'skills': [], 'missing': []}

    blocks = []
    if expert:
        blocks.append(f"【本轮专家：{expert.get('name') or expert.get('id')}】\n"
                      f"{str(expert.get('system_prompt') or '').strip()}")
    blocks.append(skills_module.render_prompt(materialized))

    return {
        'session_id': session_id,
        'addon_selection': addon,
        'expert': expert,
        'expert_id': (expert or {}).get('id') or addon.get('expert_id') or '',
        'expert_missing': bool(addon.get('expert_id')) and expert is None,
        # 实际生效的技能（含专家预设），供前端展示"当前生效"
        'skill_ids': [row['id'] for row in materialized.get('skills') or []],
        'requested_skill_ids': skill_ids,
        'skills': materialized.get('skills') or [],
        'missing_skill_ids': materialized.get('missing') or [],
        'skills_explicit': skills_explicit,
        'skill_dir': materialized.get('dir') or '',
        'skill_dir_path': materialized.get('path') or '',
        'connector_ids': connector_ids,
        'connectors_explicit': 'connector_ids' in addon,
        # 本轮连接器是否已经由会话/专家定下来。False 才允许退回"消息/会话行里的旧字段"。
        'connectors_decided': connectors_decided,
        'model': model,
        'prompt_block': '\n\n' + '\n\n'.join(block for block in blocks if block),
    }


async def resolve_turn(store, claimed: dict, session_row: dict,
                       workspace_dir: str) -> dict:
    """聊天 turn 的便捷入口：身份/工作目录都从已领取的 turn 与会话行里取。"""
    session_id = str(claimed.get('session_id') or '')
    tenant_id = str(claimed.get('tenant_id') or 'default')
    owner_user_id = str(claimed.get('owner_user_id') or 'default')
    resolved = await resolve(store, session_id=session_id, tenant_id=tenant_id,
                             owner_user_id=owner_user_id,
                             workspace_dir=workspace_dir)
    # 老会话没有 addon_selection 里显式连接器、专家也没预设时，仍尊重"这条消息带了
    # connector_ids"：前端在迁移期两套都在写，服务端以显式消息参数为兜底。
    if not resolved.get('connectors_decided'):
        raw = claimed.get('connector_ids_json')
        if raw in (None, ''):
            raw = (session_row or {}).get('connector_ids_json')
        try:
            parsed = json.loads(raw) if raw not in (None, '') else []
        except (TypeError, ValueError):
            parsed = []
        resolved['connector_ids'] = parsed if isinstance(parsed, list) else []
    return resolved
