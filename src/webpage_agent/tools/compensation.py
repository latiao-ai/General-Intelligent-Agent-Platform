"""把工具声明的撤销动作冻成账本回执。

图计划上的 `compensate` 只活在 `plans.steps` 里。Pi 的原 run 没有这份计划，
手工补偿若只翻计划，列表就是空的。这里在**写动作结算时**把能力包声明的
撤销工具和已经解析好的参数写进回执：之后补偿只读账本，不再问模型该撤什么。

声明形状（挂在 `Tool.compensate`）：

```python
compensate={
    'tool': 'delete_site',
    'args': {'site_id': {'ref': {'pointer': '/site_id'}}},
    'tool_key': 'mcp:publishing:delete_site',  # 可选
    'effect': 'destructive',                   # 可选
}
```

`ref.pointer` 指向**这一次调用自己的返回值**，结算时就解析成字面量。
解析不到就不记——不能留下一份参数是空的撤销步骤。
"""
from __future__ import annotations

_MISSING = object()


def attach_compensate(receipt: dict, tool, updates: dict) -> dict:
    """成功回执上补 `compensate`。只处理有副作用的工具，读工具不记。"""
    effect = str(getattr(tool, 'effect', '') or '')
    if effect not in ('write', 'destructive'):
        return receipt
    frozen = freeze_compensate(tool, updates)
    if frozen:
        receipt['compensate'] = frozen
    return receipt


def freeze_compensate(tool, updates: dict) -> dict | None:
    """把工具上的撤销声明解析成可直接执行的 `{tool, args, tool_key, effect}`。

    解析失败、声明缺工具名、或撤销工具就是自己时返回 None。
    """
    spec = getattr(tool, 'compensate', None)
    if not isinstance(spec, dict):
        return None
    name = str(spec.get('tool') or '').strip()
    own = str(getattr(tool, 'name', '') or '')
    if not name or name == own:
        return None
    raw_args = spec.get('args') if isinstance(spec.get('args'), dict) else {}
    try:
        args = _resolve(raw_args, updates if isinstance(updates, dict) else {})
    except _Unresolved:
        return None
    if not isinstance(args, dict):
        return None
    return {
        'tool': name,
        'args': args,
        'tool_key': str(spec.get('tool_key') or ''),
        'effect': str(spec.get('effect') or 'unknown'),
    }


class _Unresolved(Exception):
    """撤销参数里的指针在这次返回值里不存在。"""


def _resolve(value, updates: dict):
    if _is_self_ref(value):
        found = _walk(updates, str(value['ref'].get('pointer') or ''))
        if found is _MISSING:
            raise _Unresolved()
        return found
    if isinstance(value, dict):
        return {key: _resolve(item, updates) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, updates) for item in value]
    return value


def _is_self_ref(value) -> bool:
    """`{"ref": {"pointer": "/site_id"}}`：没有 step_id，指向本次返回值。"""
    return (isinstance(value, dict) and set(value) == {'ref'}
            and isinstance(value.get('ref'), dict)
            and 'pointer' in value['ref']
            and 'step_id' not in value['ref'])


def _walk(source, pointer: str):
    """受限 JSON 指针。空指针不当成「整份返回值」——那会把回执原样塞进撤销参数。"""
    tokens = [token for token in str(pointer or '').lstrip('/').split('/') if token]
    if not tokens:
        return _MISSING
    value = source
    for raw in tokens:
        token = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            if token not in value:
                return _MISSING
            value = value[token]
        elif isinstance(value, list):
            if not token.isdecimal() or int(token) >= len(value):
                return _MISSING
            value = value[int(token)]
        else:
            return _MISSING
    return value
