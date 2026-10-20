"""Planner-Executor 编排。

控制流只有两步：

    START → plan → run_step ↺ → END

`plan` 让模型看着工具清单给出一个步骤序列；`run_step` 每次只执行**一步**，
所以每一步都有独立的阶段事件与耗时。工具本身在 `app/tools.py`。

与旧版固定流水线的差别：走哪条路、搜几次、要不要出报告、要不要部署，
都由模型按输入决定，而不是写死在图的边里。
"""
import json
import time

from app import context, ledger as ledger_module, profile
from app.config import settings
from app.events import action, emit, stage_end, stage_start, thought
from app.schemas import ExecutionPlan
from app.services import is_small_talk, structured_call, urls_from_input
from app import tools as tool_module

# 步数上限从配置读：预算不该写死在内核常量里（文档 §7）
MAX_STEPS = settings.max_steps

PLANNER_SYSTEM = (
    '你是任务编排器。阅读用户要求与可用工具，输出一个**按顺序执行**的步骤计划。'
    '规则：'
    '① 只能使用工具清单里出现过的名字，不要编造；args 必须符合该工具的参数 schema。'
    '② 每个步骤给 step_id；depends_on 只能指向**更早**的步骤，不要成环。'
    '③ 最多 {max_steps} 步；不要重复完全相同的调用；步数能少不多。'
    '④ 若提供了对话历史，本轮要求可能是对上一轮的补充或修正，要结合上文理解意图。'
    '⑤ 有副作用（write/destructive）的步骤只在用户明确要求时出现，不要顺带执行。'
    '⑥ 有副作用的步骤，若工具清单里有对应的**撤销**工具（例如 deploy_site 对应'
    'delete_site），在 compensate 里写 {"tool": 撤销工具名, "args": {...}}：'
    '它会在后续步骤失败时被逆序执行，用来收回已发生的副作用。args 里的值同样'
    '可以用 ref 指向前序输出（比如站点 ID）。清单里没有撤销工具就留空，不要编造。'
    '⑦ 工具清单里的每条规则都来自对应能力包，按它说的选工具，别混用形态相近的。'
    'profile_updates：只填**本轮用户新说出、且画像里还没有（或已变化）**的个人信息'
    '（姓名、电话、邮箱、职业、偏好、习惯等），键用中文（如"电话"）。'
    '系统提示末端的「[用户画像]」段里已经出现过的事实，一律不要再填一遍——'
    '重复上报会让界面误报"已记住"。没有新信息就留空 {}}。'
    '只记用户明确说出的，不要推断、不要编造。'
)


def planner_system(catalog) -> str:
    """规划器 system = 通用规则 + **候选工具所属能力包**的规则。

    通用部分只讲"计划本身怎么算合法"（工具名、依赖、步数、画像增量）；
    "什么时候该检索、什么时候该成稿、什么时候能部署"这些业务判断由能力包
    自带（`Tool.prompt_rules`）。新增一个能力只要在包里写规则并注册工具，
    内核提示词一个字都不用改——这才是"通用中台"。
    """
    body = PLANNER_SYSTEM.replace('{max_steps}', str(MAX_STEPS))
    blocks = tool_module.planner_rules(catalog)
    if blocks:
        body = f'{body}\n[能力包规则]\n' + '\n'.join(blocks)
    return body


def _catalog_text(catalog) -> str:
    return '\n'.join(tool_module.describe_tool(tool) for tool in catalog)


def fallback_steps(state: dict) -> list[dict]:
    """规划失败时的兜底：**不猜业务**。

    这里以前会拼一条"检索 → 抓正文 → 成稿 → 渲染"的固定链路，等于把网页研究
    业务写死进内核，而且用户只是想问句话也会被拉去跑一分钟检索。
    中台不该替用户决定做什么——退化成一次直接作答，并明说规划失败。
    """
    # 能力裁剪后只剩「检索 → 抓正文 → 转 HTML →（可选）部署」这一条业务链，
    # 兜底不再替用户拼它，直接返回空计划，由 plan() 如实报失败。
    return []


def describe_plan(steps: list[dict], summary: str = '') -> str:
    names = ' → '.join(step.get('tool', '?') for step in steps)
    if summary:
        return f'我的执行计划：{summary}（{names}）'
    return f'我的执行计划：{names}'


def validate_plan(raw_steps: list[dict], catalog) -> tuple[list[dict], list[dict]]:
    """校验计划，返回 (可执行步骤, 结构化错误)。

    文档 §6.3 要求校验工具存在与权限、引用合法、无环、参数类型合理、不超预算。
    这里不去"丢坏的留好的"——计划是一个整体，被引用的一步坏了后面的都不可信，
    所以错误要回给规划器修复，而不是静默拼一个残缺计划去执行。
    """
    known = {tool.name: tool for tool in catalog}
    issues: list[dict] = []
    steps: list[dict] = []
    seen_ids: set[str] = set()

    if len(raw_steps) > MAX_STEPS:
        issues.append({'code': 'too_many_steps',
                       'detail': f'最多 {MAX_STEPS} 步，收到 {len(raw_steps)} 步'})
        raw_steps = raw_steps[:MAX_STEPS]

    for index, raw in enumerate(raw_steps, 1):
        name = str(raw.get('tool') or '').strip()
        step_id = str(raw.get('step_id') or '').strip() or f's{index}'
        if name not in known:
            issues.append({'code': 'unknown_tool', 'step_id': step_id,
                           'tool': name,
                           'detail': f'工具 {name!r} 不在可用清单里'})
            continue
        if step_id in seen_ids:
            issues.append({'code': 'duplicate_step_id', 'step_id': step_id,
                           'detail': '步骤 ID 重复'})
            continue
        seen_ids.add(step_id)
        args = raw.get('args')
        if args is not None and not isinstance(args, dict):
            issues.append({'code': 'bad_args', 'step_id': step_id,
                           'detail': 'args 必须是对象'})
            args = {}
        depends_on = [str(d) for d in (raw.get('depends_on') or [])]
        for dep in depends_on:
            if dep not in seen_ids:
                # 只能依赖**前面**的步骤：指向后面或不存在都会让执行顺序失效
                issues.append({'code': 'bad_dependency', 'step_id': step_id,
                               'detail': f'依赖 {dep!r} 不是更早的步骤'})
        compensate = _validate_compensation(raw.get('compensate'), step_id,
                                            name, known, issues)
        step = {'tool': name, 'step_id': step_id,
                'args': dict(args or {}), 'depends_on': depends_on,
                'why': str(raw.get('why') or ''),
                'tool_key': known[name].tool_key,
                'effect': known[name].effect}
        if compensate:
            step['compensate'] = compensate
        steps.append(step)
    return steps, issues


def _validate_compensation(raw, step_id: str, tool_name: str, known: dict,
                           issues: list[dict]) -> dict | None:
    """校验补偿动作。**不可用就丢掉，但不阻断整份计划。**

    补偿是一道安全网，不是主流程：模型偶尔会写一个不存在的撤销工具。为此把
    整份计划判死（等用户补信息）比"少一道安全网"更糟，所以这里记 warning。
    """
    if not raw:
        return None
    if not isinstance(raw, dict) or not str(raw.get('tool') or '').strip():
        issues.append({'code': 'bad_compensation', 'step_id': step_id,
                       'severity': 'warning',
                       'detail': 'compensate 必须是 {tool, args}'})
        return None
    name = str(raw['tool']).strip()
    if name not in known:
        issues.append({'code': 'unknown_compensation', 'step_id': step_id,
                       'severity': 'warning',
                       'detail': f'补偿工具 {name!r} 不在可用清单里，已忽略'})
        return None
    if name == tool_name:
        issues.append({'code': 'self_compensation', 'step_id': step_id,
                       'severity': 'warning',
                       'detail': '补偿动作不能是这一步自己，已忽略'})
        return None
    args = raw.get('args')
    return {'tool': name, 'args': dict(args) if isinstance(args, dict) else {},
            'tool_key': known[name].tool_key,
            'effect': known[name].effect}


def _valid_steps(raw_steps: list[dict], catalog) -> tuple[list[dict], list[str]]:
    """兼容旧调用点：返回 (步骤, 被丢弃的工具名)。"""
    steps, issues = validate_plan(raw_steps, catalog)
    dropped = [issue.get('tool') or issue['detail'] for issue in issues
               if issue['code'] == 'unknown_tool']
    return steps, dropped


def resolve_inputs(step: dict, outputs: dict) -> dict:
    """把步骤输入里的 ref 绑定解析成真实值（文档 §6.2）。

    只认两种形态：literal 直传、ref 指向前序步骤输出里的某个指针。
    不做表达式求值、不读任意文件路径。
    """
    args = dict(step.get('args') or {})
    for key, value in list(args.items()):
        if not isinstance(value, dict):
            continue
        if 'ref' in value and isinstance(value['ref'], dict):
            ref = value['ref']
            source = outputs.get(str(ref.get('step_id') or ''), {})
            pointer = str(ref.get('pointer') or '').strip('/')
            resolved = source
            for part in filter(None, pointer.split('/')):
                if isinstance(resolved, dict) and part in resolved:
                    resolved = resolved[part]
                else:
                    resolved = None
                    break
            args[key] = resolved
        elif 'literal' in value:
            args[key] = value['literal']
    return args


async def plan(state):
    """产出执行计划；不执行任何工具。"""
    question = state['user_input']

    # 续跑：沿用已保存的计划，不重新规划——重新规划会让"已完成步骤不可改写"
    # 这条约束失效，也白白多花一次模型调用（文档 §7）。
    resumed = state.get('resume_plan') or {}
    if isinstance(resumed, dict) and resumed.get('steps'):
        steps = resumed['steps']
        emit(action('沿用已保存的计划', f'{len(steps)} 步', '↩️'))
        emit(thought(describe_plan(steps, resumed.get('summary') or '续跑')))
        plan = {'steps': steps, 'summary': resumed.get('summary') or '续跑'}
        if resumed.get('kind'):
            # kind=compensation 的计划走的是同一套执行器，但终态与回执不同
            plan['kind'] = resumed['kind']
        return {'plan': plan,
                'step_index': int(resumed.get('step_index') or 0),
                'status': 'planned'}

    # 图片里那条"简单问题、模型已有足够信息 → 直接回答"：
    # 闲聊与确认不需要规划（省一次模型调用），但**回答仍然由模型给出**，
    # 不是固定文案——所以计划里放一步 answer_directly，而不是在这里直接回话。
    if not urls_from_input(question) and is_small_talk(question):
        steps = [{'tool': 'answer_directly', 'step_id': 's1',
                  'args': {'question': question},
                  'depends_on': [], 'why': '闲聊/确认，无需检索与规划',
                  'tool_key': 'local:answers:direct', 'effect': 'read'}]
        emit(action('跳过规划', '闲聊/确认，交给模型直接回答', '⚡'))
        emit(thought(describe_plan(steps, '直接回答')))
        return {'plan': {'steps': steps, 'summary': '直接回答'},
                'step_index': 0, 'status': 'planned'}

    catalog = await tool_module.catalog()
    available, origin = tool_module.usable_documents(state)
    payload = json.dumps({
        '用户要求': question,
        '对话历史': context.select(state.get('history'), purpose='plan'),
        '会话记忆': context.memory_text(state.get('session_memory')),
        # 明确告诉模型手上还有多少料：否则它可能以为要重新检索，
        # 也可能反过来以为已有资料、结果空手去整理。
        '已有资料': (f'{len(available)} 个来源的正文' + (f'（{origin}）' if origin else '')
                     if available else '无'),
        '上一轮已产出 HTML': bool(state.get('prior_artifact')),
        '可用工具': _catalog_text(catalog),
        '部署已开启': settings.deploy_enabled,
    }, ensure_ascii=False)

    usage = list(state.get('usage', []))
    errors = list(state.get('errors', []))
    steps, summary, updates = None, '', {}
    issues: list[dict] = []
    blocking: list[dict] = []
    model_answered = False          # 模型是否真的给出过一份计划（哪怕是坏的）
    try:
        # 最多两轮：第一次照常规划；若整体校验不通过，把结构化错误交回模型修复
        for round_index in range(2):
            execution, call_usage = await structured_call(
                ExecutionPlan,
                profile.with_profile(planner_system(catalog)),
                payload,
                disable_thinking=settings.llm_parse_disable_thinking,
            )
            data = execution.model_dump()
            raw_steps = data.get('steps') or []
            model_answered = model_answered or bool(raw_steps)
            steps, issues = validate_plan(raw_steps, catalog)
            # 只有 blocking 级别的错误才值得再花一次模型调用去修；
            # 补偿工具写错这类 warning 已经丢弃该字段，不需要为它卡住计划。
            blocking = [i for i in issues if i.get('severity') != 'warning']
            summary = data.get('summary') or ''
            updates = data.get('profile_updates') or {}
            usage.append({'stage': 'plan', 'model_name': settings.llm_model,
                          'model': call_usage})
            if not blocking:
                break
            emit(action('计划校验未通过',
                        '；'.join(f'{i["code"]}:{i["detail"]}' for i in blocking)[:120],
                        '⚠️'))
            if round_index == 0:
                # 把错误原文喂回去，让模型在同一份工具清单上修
                payload = json.dumps({
                    '用户要求': question,
                    '上次计划': data.get('steps'),
                    '校验错误': blocking,
                    '要求': '按校验错误修正计划；只能用清单里的工具；'
                            'depends_on 只能指向更早的步骤。',
                    '可用工具': _catalog_text(catalog),
                }, ensure_ascii=False)
    except Exception as exc:
        # 规划失败不该让任务直接崩：退回一条保守的固定计划。
        # 把异常类别透出来——否则界面上只有一个"规划失败"，没法判断是网关超时
        # 还是 schema 两次校验都没过。
        hint = getattr(exc, 'safe_message', None) or type(exc).__name__
        emit(action('规划失败', f'{hint}，改用默认计划', '⚠️'))
        errors = list(state.get('errors', [])) + [
            {'stage': 'plan', 'type': type(exc).__name__}]
        steps = None
        issues = []

    # 两轮修复后仍有结构化错误：**停下来**，不带着坏计划执行。
    #
    # 以前这里只判 `if not steps`，而 validate_plan 返回的是"过滤后"的合法步骤
    # ——通常非空，于是带着坏依赖一路跑下去，被丢掉步骤的 step_id 仍可能被后面
    # 的 ref 引用，resolve_inputs 解析成 None 直接喂给工具（文档 §6.3 明确禁止）。
    if model_answered and blocking:
        detail = '；'.join(f'{item["code"]}:{item["detail"]}'
                          for item in blocking)[:300]
        emit(action('计划无法通过校验', detail, '⛔'))
        emit(thought(f'这份计划修了两轮仍有问题，先停下来确认：{detail}'))
        return {'plan': {'steps': [], 'summary': summary or '计划未通过校验'},
                'step_index': 0, 'usage': usage, 'status': 'waiting_input',
                'failure': f'计划没有通过校验（{detail}）。'
                           f'可以补充更具体的要求，或明确要用的工具再试。',
                'errors': errors}

    if not steps:
        steps, _ = _valid_steps(fallback_steps(state), catalog)
        summary = summary or '降级计划'

    # warning 级问题不阻断执行，但必须留痕：否则"补偿被静默丢掉"没人知道。
    warnings = [item for item in issues if item.get('severity') == 'warning']
    if warnings:
        emit(action('计划有可忽略的问题',
                    '；'.join(f'{i["code"]}:{i["detail"]}' for i in warnings)[:160],
                    '⚠️'))

    # 只播报真正新增/变化的事实：模型常把画像里已有的事实再抄一遍，
    # 若无条件播报，用户会看到"已记住"一条自己本轮根本没说过的话。
    changed = profile.merge(updates)
    if changed:
        emit(action('已记住', profile.describe(changed), '🧠'))
    emit(thought(describe_plan(steps, summary)))
    result = {'plan': {'steps': steps, 'summary': summary,
                       **({'warnings': warnings} if warnings else {})},
              'step_index': 0, 'usage': usage, 'status': 'planned'}
    if errors:
        result['errors'] = errors
    return result


def next_step(state) -> str:
    """还有没跑完的步骤就继续，否则收尾。"""
    steps = (state.get('plan') or {}).get('steps') or []
    return 'run_step' if state.get('step_index', 0) < len(steps) else 'end'


def final_status(state: dict) -> str:
    """把内部状态归一到前端认识的终态。"""
    # 补偿运行没有产物也没有回答，"没产出"不等于失败：它成功就是成功。
    if (state.get('plan') or {}).get('kind') == 'compensation':
        return 'failed' if state.get('failure') else 'compensated'
    if state.get('answer'):
        return 'answered'
    deployment = state.get('deployment') or {}
    if deployment.get('status') == 'success':
        return 'deployed'
    if deployment:
        return 'deploy_needs_check'
    if state.get('artifact'):
        return 'local_ready'
    return 'failed'


async def _tool_by_name(name: str):
    """按名字取工具：先内置，再 MCP 动态清单（含旧名回退）。"""
    return await tool_module.find_tool(name)


async def _run_compensations(state: dict, steps: list[dict], outputs: dict,
                             book) -> list[dict]:
    """逆序执行已成功步骤声明的补偿动作（文档 §8.4）。

    只补偿**已经成功**的步骤：失败/跳过的步骤没有可撤销的副作用；结果未定
    （unknown）的步骤更不能自动去撤——那等于基于猜测再发一次写操作，正是
    账本要避免的事。
    """
    results: list[dict] = []
    for step in reversed(steps):
        comp = step.get('compensate') or {}
        if not comp.get('tool'):
            continue
        if (outputs.get(step.get('step_id')) or {}).get('__status__') != 'succeeded':
            continue
        name = comp['tool']
        step_id = f'undo:{step["step_id"]}'
        tool = await _tool_by_name(name)
        if tool is None:
            results.append({'step_id': step['step_id'], 'tool': name,
                            'status': 'failed',
                            'error': '补偿工具当前不可用（MCP 可能已掉线）'})
            emit(action('补偿无法执行', f'{name} 不可用', '⚠️'))
            continue
        args = resolve_inputs({'args': comp.get('args') or {}}, outputs)
        emit(stage_start(step_id, f'回滚·{tool.label}'))
        started = time.perf_counter()
        action_id, attempt_id = None, None
        if book is not None:
            prepared = book.begin(step_id=step_id, tool_key=tool.tool_key or name,
                                  effect=tool.effect, args=args,
                                  authorized_by='compensation')
            if prepared.get('replay') is not None:
                # 已经撤过一次：再撤一次可能报错，也可能误删新建的东西
                results.append({'step_id': step['step_id'], 'tool': name,
                                'status': 'skipped', 'error': '该补偿此前已执行过'})
                emit(stage_end(step_id, 0, '已执行过', tool.label))
                continue
            if prepared.get('blocked'):
                results.append({'step_id': step['step_id'], 'tool': name,
                                'status': 'failed',
                                'error': f'上次补偿结果未定（{prepared["blocked"]}）'})
                emit(stage_end(step_id, 0, '结果未定', tool.label))
                continue
            action_id = prepared['action_id']
            attempt_id = book.in_flight(action_id)
        try:
            await tool.run(args, state)
        except Exception as exc:                      # noqa: BLE001
            hint = getattr(exc, 'safe_message', None) or type(exc).__name__
            if book is not None and action_id:
                book.fail(action_id, attempt_id, type(exc).__name__)
            results.append({'step_id': step['step_id'], 'tool': name,
                            'status': 'failed', 'error': hint})
            emit(stage_end(step_id, time.perf_counter() - started, '失败', tool.label))
            emit(action('回滚失败', f'{tool.label}：{hint}', '⚠️'))
            continue
        if book is not None and action_id:
            book.succeed(action_id, attempt_id, {'compensated': step['step_id']})
        results.append({'step_id': step['step_id'], 'tool': name,
                        'status': 'succeeded'})
        emit(stage_end(step_id, time.perf_counter() - started, '已回滚', tool.label))
        emit(action('已回滚一步', tool.label, '↩️'))
    return results


async def run_step(state):
    """执行计划中的一步。每次只走一步，便于逐步计时与取消。"""
    steps = (state.get('plan') or {}).get('steps') or []
    index = state.get('step_index', 0)
    if index >= len(steps):
        return {'step_index': index}

    step = steps[index]
    name = step['tool']
    tool = await _tool_by_name(name)
    last = index + 1 >= len(steps)

    if tool is None:
        # 计划里出现了执行时不存在的工具（比如 MCP 掉线）：跳过，别让整轮失败。
        emit(action('跳过未知工具', name, '⚠️'))
        result = {'step_index': index + 1, 'step_label': name}
        if last:
            result['status'] = final_status(state)
        return result

    step_id = step.get('step_id') or f's{index + 1}'
    outputs = dict(state.get('step_outputs') or {})

    # 依赖必须真的成功，否则这一步的输入就是不可信的（文档 §6.3）
    for dep in step.get('depends_on') or []:
        dep_status = (outputs.get(dep) or {}).get('__status__')
        if dep_status != 'succeeded':
            emit(action('依赖未完成', f'{tool.label} 依赖 {dep}（{dep_status or "缺失"}）', '⛔'))
            return {'step_index': len(steps), 'step_label': tool.label,
                    'status': 'failed',
                    'failure': f'{tool.label}依赖的步骤 {dep} 没有成功完成'}

    emit(stage_start(name, tool.label))
    started = time.perf_counter()
    args = resolve_inputs(step, outputs)

    # 账本：调用之前先落 prepared / in_flight，回来再改。崩溃窗口才有据可查。
    book = ledger_module.current_ledger()
    action_id, attempt_id, reused = None, None, False
    if book is not None:
        prepared = book.begin(step_id=step_id, tool_key=tool.tool_key or name,
                              effect=tool.effect, args=args)
        if prepared.get('blocked'):
            # 之前已发出但没拿到确定回执：不能重发，也不能当成功
            emit(action('该步骤结果未定',
                        f'{tool.label}（{prepared["blocked"]}），需核实后再继续', '❓'))
            outputs[step_id] = {'__status__': 'unknown'}
            return {'step_index': len(steps), 'step_label': tool.label,
                    'step_outputs': outputs, 'status': 'failed',
                    'needs_reconciliation': True,
                    'failure': f'{tool.label}的结果未确定（{prepared["blocked"]}），'
                               f'为避免重复副作用已停止，请核实后重试'}
        if prepared.get('replay') is not None:
            # 这个动作之前已经成功：直接复用回执，不再调用远端
            emit(action('复用已有结果', f'{tool.label}（该动作此前已成功）', '♻️'))
            updates = dict(prepared['replay']) if isinstance(prepared['replay'], dict) else {}
            reused = True
        action_id = prepared['action_id']
        if not reused:
            attempt_id = book.in_flight(action_id)

    try:
        if not reused:
            updates = await tool.run(args, state)
    except Exception as exc:
        emit(stage_end(name, time.perf_counter() - started, '失败', tool.label))
        hint = getattr(exc, 'safe_message', None) or type(exc).__name__
        emit(action(f'{tool.label}失败', hint, '⚠️'))
        if book is not None and action_id:
            # 超时/连接中断属于"结果不确定"，不能当失败去重试
            uncertain = type(exc).__name__ in {'TimeoutError', 'AttemptTimeout',
                                               'ConnectError', 'ReadTimeout'}
            if uncertain:
                book.mark_unknown(action_id, attempt_id, type(exc).__name__)
                emit(action('结果不确定', '远端可能已执行，需要核实', '❓'))
            else:
                book.fail(action_id, attempt_id, type(exc).__name__)
        errors = list(state.get('errors', [])) + [
            {'stage': name, 'type': type(exc).__name__}]
        # 出错就中止剩余步骤：后面的多半依赖这一步的产物。
        outputs[step_id] = {'__status__': 'failed'}
        # 已经发生的写操作要收回来（文档 §8.4）。只回滚声明了 compensate 且
        # 确实成功的步骤，逆序执行——先做的后撤。
        compensations = await _run_compensations(state, steps[:index + 1],
                                                 outputs, book)
        failed_undo = [r for r in compensations if r['status'] == 'failed']
        note = ''
        if compensations:
            note = f'；已回滚 {len(compensations) - len(failed_undo)} 步'
            if failed_undo:
                note += (f'，{len(failed_undo)} 步回滚失败'
                         f'（{failed_undo[0]["tool"]}：{failed_undo[0].get("error")}）')
        return {'step_index': len(steps), 'step_label': tool.label,
                'errors': errors, 'step_outputs': outputs,
                'compensations': compensations,
                # 有副作用没撤干净时不能只说"失败"：线下可能留着一个站点。
                'status': 'partially_succeeded' if failed_undo else 'failed',
                'failure': f'{tool.label}失败：{hint}{note}'}

    updates = dict(updates)
    if book is not None and action and not reused:
        book.succeed(action_id, attempt_id,
                     {k: v for k, v in updates.items()
                      if isinstance(v, (str, int, float, bool, dict, list))})
    record = {k: v for k, v in updates.items()
              if isinstance(v, (str, int, float, bool, dict, list))}
    record['__status__'] = 'succeeded'
    outputs[step_id] = record
    updates['step_outputs'] = outputs
    updates['step_index'] = index + 1
    updates['step_label'] = tool.label
    emit(stage_end(name, time.perf_counter() - started,
                   updates.get('status', ''), tool.label))
    if last:
        merged = dict(state)
        merged.update(updates)
        updates['status'] = final_status(merged)
    return updates
