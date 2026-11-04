"""步骤参数里的引用。

两件事：

- 把 `{"ref": {"step_id", "pointer"}}` 解析成前序步骤的真实值，解析不到就报
  `E_INPUT_REFERENCE`，不把 None 发给远端。
- 引用修复只允许改未执行步骤的 pointer，工具、依赖和字面参数必须保持原样。

`validate_plan` 用这里的遍历器检查引用形状；`run_step` 在执行前调用
`resolve_inputs`。
"""
import json
import re
from pathlib import Path

from webpage_agent.capabilities.base import ToolError


_PLACEHOLDER = re.compile(r'\{\{.*?\}\}')


def _check_placeholders(args: dict, tool_label: str) -> None:
    """参数里还留着 `{{s2.project_id}}` 这种模板 = 规划器没按绑定语法写。

    真踩过：模型把跨步骤引用写成 `"project_id": "{{s2.project_id}}"`，服务端拿到
    字面量后报"项目不存在"，而错误看起来像服务端的问题。这里在执行前就拦住，
    并直接告诉它正确写法。嵌套对象/数组里也要查——模板最常见的落点恰恰是
    `patch` 这类里层字段。
    """
    for path, value in _iter_strings(args):
        if _PLACEHOLDER.search(value):
            raise ToolError(
                f'{tool_label} 的参数 {path} 还是未解析的占位符 {value!r}。'
                f'跨步骤取值请写成 {{"ref": {{"step_id": "s2", '
                f'"pointer": "/project_id"}}}}，不要写字面模板')


def _iter_strings(value, path: str = ''):
    """递归产出 (路径, 字符串值)，路径用于把错误指到具体字段。"""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(item, f'{path}.{key}' if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_strings(item, f'{path}[{index}]')


_MISSING = object()


def _iter_bindings(value, path=''):
    if _is_binding(value):
        yield path, value['ref']
    elif isinstance(value, dict):
        if set(value) == {'literal'}: return
        for key, item in value.items():
            yield from _iter_bindings(item, f'{path}.{key}' if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_bindings(item, f'{path}[{index}]')


def _is_binding(value) -> bool:
    """`{"ref": {...}}` 这种绑定对象——要求**只有一个键**。

    收紧到"整份对象就是绑定"而不是"对象里有 ref 键"，是为了能安全地递归：
    一旦允许"带 ref 键的普通对象"，下游任何参数只要恰好有个叫 ref 的字段
    就会被误当成绑定改写掉。
    """
    return (isinstance(value, dict) and set(value) == {'ref'}
            and isinstance(value.get('ref'), dict)
            and 'step_id' in value['ref'])


def _walk_pointer(source, pointer: str):
    """受限 JSON 指针取值：支持对象键与数组下标，取不到返回 `_MISSING`。

    老实现只认 dict 键，所以 `/models/0/id` 这种"列表第 0 个的 id"永远取不到——
    而模型目录、列表类工具返回的恰恰都是数组，规划器按最自然的方式写指针，
    换来的却是一句"引用不到值"。
    """
    value = source
    for raw in str(pointer or '').lstrip('/').split('/'):
        if not raw:
            continue
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


def _unwrap_saved(container, depth: int = 0):
    """把落盘结构剥到"服务端这次返回的那一层"。

    落盘形状不统一：只调了一个工具时是 `{"payload": {...}}`，一"步"里带了多次
    调用（读+查模型+写）时是 `{"payload": [{tool, payload}, ...]}`。模型写指针
    时对着的一律是最里层那次调用的 payload，所以这里一路往里剥。
    """
    if depth > 4:
        return container
    if isinstance(container, dict):
        items = container.get('items')
        if isinstance(items, list) and items:
            return _unwrap_saved(items[-1], depth + 1)
        if 'payload' in container and (set(container) & {'tool', 'server'}
                                       or set(container) == {'payload'}):
            return _unwrap_saved(container['payload'], depth + 1)
        return container
    if isinstance(container, list) and container:
        return _unwrap_saved(container[-1], depth + 1)
    return container


def _saved_result_views(record: dict, artifact_dir: str) -> list:
    """前序步骤落盘的完整结果，按"模型看到的那一层"依次排列。

    为什么要读文件：步骤输出只保留**标量**（嵌套结构进快照会让 checkpoint 爆炸），
    完整结果落在 `results/<step>.json` 里。模型在观察里看到、也是它写指针时对着
    的，正是最外层 payload 或 payload.result——所以指针在标量层落空后要再找一次。

    为什么还要再往下钻两层：很多工具的实体比 `result` 再深一层。`canvas.get`
    返回的是 `{op, result: {canvasId, canvas}}`，节点在 `result.canvas.nodes[]`。
    模型读观察时只会看到"画布里有几个节点"，写指针就按最自然的 `/nodes/0/...`
    开头——少掉 `result.canvas` 这层前缀。只认前两层的旧实现在这里必然落空，
    报出来的还是一句"引用不到值"。

    返回 `(前缀, 视图)`：前缀只用于报错时告诉模型该从哪层写起。顺序固定为
    "先浅后深"，让浅层同名字段优先命中，避免深层视图把 `/id` 这类通用指针
    悄悄解析成另一个对象。
    """
    ref = str(((record or {}).get('tool_result') or {}).get('data_ref') or '')
    if not ref or not artifact_dir:
        return []
    root = Path(artifact_dir).resolve()
    path = Path(ref)
    if not path.is_absolute():
        path = root / ref
    path = path.resolve()
    if not path.is_relative_to(root):
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:                                    # noqa: BLE001
        return []
    payload = _unwrap_saved(data)
    if not isinstance(payload, dict):
        return []
    views: list[tuple[str, object]] = [('', payload)]
    inner = payload.get('result')
    if isinstance(inner, (dict, list)):
        views.append(('/result', inner))
    seen = {id(payload)} | ({id(inner)} if isinstance(inner, (dict, list)) else set())
    queue = list(views)
    for _ in range(2):
        nxt = []
        for prefix, view in queue:
            if not isinstance(view, dict):
                continue
            for key, item in view.items():
                if not isinstance(item, (dict, list)) or id(item) in seen:
                    continue
                seen.add(id(item))
                views.append((f'{prefix}/{key}', item))
                nxt.append((f'{prefix}/{key}', item))
        if not nxt:
            break
        queue = nxt
    return views


def _resolve_ref(ref: dict, outputs: dict, step_id: str, path: str,
                 artifact_dir: str = ''):
    ref_step = str(ref.get('step_id') or '')
    record = outputs.get(ref_step) or {}
    pointer = ref.get('pointer') or ''
    resolved = _walk_pointer(record, pointer)
    if resolved is _MISSING:
        matches = [(prefix, _walk_pointer(view, pointer))
                   for prefix, view in _saved_result_views(record, artifact_dir)]
        matches = [(prefix, value) for prefix, value in matches if value is not _MISSING]
        if matches:
            if any(value != matches[0][1] for _, value in matches[1:]):
                raise ToolError(f'步骤 {step_id} 的参数 {path} 引用有歧义：'
                                f'{ref_step} 的 {pointer} 匹配多个不同对象，请使用完整路径',
                                certainty='not_executed', phase='validate', error_code='E_INPUT_REFERENCE')
            resolved = matches[0][1]
    if resolved is _MISSING:
        # 解析不到就别把 None 喂给远端：服务端只会报一句
        # "None is not of type string"，看不出是 planner 写错了引用
        available = [k for k in record if k != '__status__'] \
            if isinstance(record, dict) else []
        detail = f'该步骤的步骤输出里可用的是：{", ".join(available) or "（空）"}'
        views = _saved_result_views(record, artifact_dir)
        if views:
            head = views[0][1]
            nested = [k for k in head if not str(k).startswith('__')]
            detail += (f'；服务端返回的原始结果里可用的是：'
                       f'{", ".join(nested) or "（空）"}')
            # 只报顶层键还不够：实体的真层在下钻得到的视图里（canvas 的节点
            # 在 /result/canvas/nodes），把可用前缀一并给出，下一轮才改得对。
            prefixes = []
            for prefix, view in views[1:]:
                if not isinstance(view, dict) or not prefix:
                    continue
                keys = [f'{prefix}/{k}' for k in view
                        if not str(k).startswith('__')]
                prefixes.extend(keys[:6])
            if prefixes:
                detail += (f'；可以引用的路径前缀：'
                           f'{", ".join(prefixes[:10])}')
        raise ToolError(
            f'步骤 {step_id} 的参数 {path} 引用不到值：'
            f'{ref_step} 的 {pointer} 不存在。{detail}',
            certainty='not_executed', phase='validate', error_code='E_INPUT_REFERENCE')
    return resolved


def _resolve_value(value, outputs: dict, step_id: str, path: str,
                   artifact_dir: str = ''):
    """递归解析参数里的绑定。

    为什么必须递归：MCP 工具的参数普遍是嵌套对象（`patch.model`、
    `filter.date`），跨步骤取值就落在里层。只在最外层找 ref，里层那个
    `{"ref": {...}}` 会原样发给服务端，换回一句 JSON Schema 校验失败
    "`{'ref': ...}` is not of type 'string'"——看起来像模型传错了类型，
    实际是调度器没解析。
    """
    if _is_binding(value):
        return _resolve_ref(value['ref'], outputs, step_id, path, artifact_dir)
    if isinstance(value, dict):
        if set(value) == {'literal'}:
            return value['literal']
        return {key: _resolve_value(item, outputs, step_id,
                                    f'{path}.{key}' if path else str(key),
                                    artifact_dir)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, outputs, step_id, f'{path}[{index}]',
                               artifact_dir)
                for index, item in enumerate(value)]
    return value


def resolve_inputs(step: dict, outputs: dict, artifact_dir: str = '') -> dict:
    """把步骤输入里的 ref 绑定解析成真实值（文档 §6.2）。

    只认两种形态：literal 直传、ref 指向前序步骤输出里的某个指针。
    不做表达式求值、不读任意文件路径。绑定可以出现在参数的**任意层级**。
    `artifact_dir` 给了才允许读回前序步骤落盘的完整结果（见
    `_saved_result_views`）——不给时行为与只认步骤输出一致。
    """
    args = dict(step.get('args') or {})
    step_id = str(step.get('step_id') or '')
    return {key: _resolve_value(value, outputs, step_id, str(key), artifact_dir)
            for key, value in args.items()}


def _reference_failures(state):
    current = {step.get('step_id') for step in (state.get('plan') or {}).get('steps') or []}
    return [error for error in state.get('errors') or []
            if error.get('error_code') == 'E_INPUT_REFERENCE' and not error.get('resolved')
            and error.get('step_id') in current]


def _reference_repair_context(state):
    failures = _reference_failures(state)
    if not failures: return {}
    outputs = state.get('step_outputs') or {}
    remaining = [step for step in (state.get('plan') or {}).get('steps') or []
                 if (outputs.get(step.get('step_id')) or {}).get('__status__') != 'succeeded']
    sources = {str(ref.get('step_id') or '') for step in remaining
               for _, ref in _iter_bindings(step.get('args') or {})}
    paths = {}
    def visit(value, prefix, found, depth=0):
        if depth > 7 or len(found) >= 100: return
        if prefix: found.append(prefix)
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).startswith('__') or key == 'tool_result': continue
                escaped = str(key).replace('~', '~0').replace('/', '~1')
                visit(item, prefix + '/' + escaped, found, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:8]):
                visit(item, prefix + '/' + str(index), found, depth + 1)
    for source in sorted(sources):
        record = outputs.get(source) or {}
        views = _saved_result_views(record, str(state.get('artifact_dir') or ''))
        found = []
        visit(views[0][1] if views else record, '', found)
        paths[source] = found
    return {'错误': failures, '待执行的原步骤': remaining, '真实结果可用路径': paths,
            '修复限制': '只返回待执行原步骤，顺序、step_id、工具、依赖、字面参数必须保持原样。'
                        '仅修正出错步骤的 ref.pointer，ref.step_id 不得更换。'
                        '必须使用真实回执里的路径，不得重复创建已创建的对象。'}


def _same_binding_arguments(original, candidate):
    if _is_binding(original):
        return (_is_binding(candidate) and set(original['ref']) == set(candidate['ref'])
                and {k: v for k, v in original['ref'].items() if k != 'pointer'}
                == {k: v for k, v in candidate['ref'].items() if k != 'pointer'}
                and isinstance(candidate['ref'].get('pointer'), str))
    if isinstance(original, dict):
        if set(original) == {'literal'}: return original == candidate
        return (isinstance(candidate, dict) and set(original) == set(candidate)
                and all(_same_binding_arguments(value, candidate[key]) for key, value in original.items()))
    if isinstance(original, list):
        return (isinstance(candidate, list) and len(original) == len(candidate)
                and all(_same_binding_arguments(a, b)
                        for a, b in zip(original, candidate, strict=True)))
    return type(original) is type(candidate) and original == candidate


def _align_repair_steps(candidate, original):
    """把模型给的修复步骤对齐到**原步骤**：缺的按原样补回，多出来的判不合格。

    为什么要对齐：`step_id` 在 `PlanStep` 里是可以省略的（服务端按顺序补号），
    模型修引用时经常只写"哪一步、改哪个 pointer"——编号要么省略、要么从 s1 重新
    数、要么把没动过的步骤整段略掉。按字面比 step_id 会把这些**合法修复**当成
    "想重做创建"拒掉，用户看到一句"引用修复未通过校验"，而对象其实早就建好了。
    对齐只补编号与依赖、不动参数：能不能改仍然由后面的比对说了算。
    """
    if not isinstance(candidate, list) or not candidate:
        return None
    order = [str(step.get('step_id') or '') for step in original]
    by_id = {str(step.get('step_id') or ''): step for step in original}
    supplied = {}
    for index, step in enumerate(candidate):
        if not isinstance(step, dict):
            return None
        raw = str(step.get('step_id') or '')
        if raw and raw in by_id and raw not in supplied:
            key = raw
        elif index < len(order) and order[index] not in supplied:
            key = order[index]
        else:
            return None        # 多出来的步骤：那是改计划，不是修引用
        supplied[key] = {**step, 'step_id': key}
    aligned = []
    for key in order:
        base = by_id[key]
        step = supplied.get(key)
        if step is None:
            aligned.append(dict(base))          # 模型没动它：保留原样
            continue
        if not step.get('depends_on'):
            step = {**step, 'depends_on': list(base.get('depends_on') or [])}
        aligned.append(step)
    return aligned


def _prepared_reference_steps(state, candidate):
    """对齐并校验一次引用修复，返回 (对齐后的步骤, 问题列表)。"""
    repair = _reference_repair_context(state)
    if not repair:
        return None, []
    original = repair['待执行的原步骤']
    bad = {'code': 'unsafe_reference_repair', 'detail': '引用修复只能更正未执行步骤的路径，不能重做创建、改变工具或字面参数'}
    aligned = _align_repair_steps(candidate, original)
    if aligned is None:
        return None, [{'code': 'unsafe_reference_repair',
                       'detail': '引用修复只能返回待执行的这些步骤：编号可以省略或重排，'
                                 '但不能新增、替换或漏掉计划里的步骤'}]
    failed = {error.get('step_id') for error in _reference_failures(state)}
    # `_align_repair_steps` 保证按原步骤一一对齐；长度不等说明对齐逻辑坏了，
    # 静默截断会让"少了一步"这种真问题看起来像"修复通过"。
    for before, after in zip(original, aligned, strict=True):
        if (before.get('tool') != after.get('tool')
                or list(before.get('depends_on') or []) != list(after.get('depends_on') or [])
                or (before.get('compensate') or None) != (after.get('compensate') or None)):
            return aligned, [bad]
        if before['step_id'] in failed:
            if not _same_binding_arguments(before.get('args', {}), after.get('args', {})): return aligned, [bad]
            replacements = dict(_iter_bindings(after.get('args', {})))
            for path, binding in _iter_bindings(before.get('args', {})):
                try:
                    _resolve_ref(binding, state.get('step_outputs') or {}, before['step_id'], path,
                                 str(state.get('artifact_dir') or ''))
                except ToolError:
                    continue
                if replacements[path] != binding:
                    return aligned, [bad]
            try:
                resolve_inputs(after, state.get('step_outputs') or {}, str(state.get('artifact_dir') or ''))
            except ToolError as exc:
                return aligned, [{'code': 'bad_reference_repair', 'detail': exc.safe_message}]
        elif before.get('args', {}) != after.get('args', {}): return aligned, [bad]
    return aligned, []


def _reference_revision_issues(state, candidate):
    """兼容入口：只要问题列表（用例与调用点用它判断"能不能执行这份修复"）。"""
    return _prepared_reference_steps(state, candidate)[1]
