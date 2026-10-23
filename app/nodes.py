"""Planner-Executor 编排。

控制流只有两步：

    START → plan → run_step ↺ → END

`plan` 让模型看着工具清单给出一个步骤序列；`run_step` 每次只执行**一步**，
所以每一步都有独立的阶段事件与耗时。工具本身在 `app/tools.py`。

与旧版固定流水线的差别：走哪条路、搜几次、要不要出报告、要不要部署，
都由模型按输入决定，而不是写死在图的边里。
"""
import json
import re
import time

from app import (context, errors as errors_module, execution_control,
                 ledger as ledger_module, outcome as outcome_module, profile)
from app.config import settings
from app.events import action, emit, stage_end, stage_start, thought
from app.capabilities.base import ToolError
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
    '⑧ 要用前面步骤的输出时，args 里写**绑定对象**：'
    '{"ref": {"step_id": "s2", "pointer": "/project_id"}}（pointer 是受限 JSON 指针）。'
    '绝不能写成 "{{s2.project_id}}" 这类模板字符串——系统不会替你替换它，'
    '远端只会收到一串字面量然后报"不存在"。字面量直接写值即可。'
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
        # 服务端整体不可用（清单里一个能用的都没有）：执行类步骤不该出现在计划里。
        # 这不是"工具不存在"，而是"现在干不了"——所以要停下来问用户/说明缺什么，
        # 而不是把 render_video 跑一遍等它报 E_PROJECT_ARTIFACT_MISSING。
        server_id = known[name].server_id
        effect = known[name].effect
        if server_id and effect != 'read':
            blocked_reason = tool_module.unusable_reason(server_id)
            if blocked_reason:
                issues.append({'code': 'capability_unavailable', 'step_id': step_id,
                               'tool': name, 'detail': blocked_reason})
                continue
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


_PLACEHOLDER = re.compile(r'\{\{.*?\}\}')


def _check_placeholders(args: dict, tool_label: str) -> None:
    """参数里还留着 `{{s2.project_id}}` 这种模板 = 规划器没按绑定语法写。

    真踩过：模型把跨步骤引用写成 `"project_id": "{{s2.project_id}}"`，服务端拿到
    字面量后报"项目不存在"，而错误看起来像服务端的问题。这里在执行前就拦住，
    并直接告诉它正确写法。
    """
    for key, value in (args or {}).items():
        if isinstance(value, str) and _PLACEHOLDER.search(value):
            raise ToolError(
                f'{tool_label} 的参数 {key} 还是未解析的占位符 {value!r}。'
                f'跨步骤取值请写成 {{"ref": {{"step_id": "s2", '
                f'"pointer": "/project_id"}}}}，不要写字面模板')


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
            if resolved is None:
                # 解析不到就别把 None 喂给远端：服务端只会报一句
                # "None is not of type string"，看不出是 planner 写错了引用
                available = [k for k in (source or {}) if k != '__status__']
                raise ToolError(
                    f'步骤 {step.get("step_id")} 的参数 {key} 引用不到值：'
                    f'{ref.get("step_id")} 的 {ref.get("pointer")} 不存在。'
                    f'该步骤实际可用的字段：{", ".join(available) or "（空）"}')
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

    # 能力不可用：模型可能已经把这些步骤删掉了，但**用户仍需知道**为什么没做成
    # 那件事（不然只会看到"已产出 script.md"，以为视频做完了）。
    blocked_notes = list(state.get('capability_blocked') or [])
    for item in issues:
        if item.get('code') == 'capability_unavailable':
            detail = f'{item["tool"]}：{item["detail"]}'
            if detail not in blocked_notes:
                blocked_notes.append(detail)
    if blocked_notes:
        emit(action('有步骤因能力不可用被移除', blocked_notes[0][:120], '⚠️'))

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
                       **({'warnings': warnings} if warnings else {}),
                       **({'blocked': blocked_notes} if blocked_notes else {})},
              'step_index': 0, 'usage': usage, 'status': 'planned'}
    if blocked_notes:
        result['capability_blocked'] = blocked_notes
    if errors:
        result['errors'] = errors
    return result


def next_step(state) -> str:
    """还有没跑完的步骤就继续，否则收尾。"""
    steps = (state.get('plan') or {}).get('steps') or []
    return 'run_step' if state.get('step_index', 0) < len(steps) else 'end'


def final_status(state: dict) -> str:
    """本轮收尾后的**通用**状态（`answered`/`local_ready` 这类展示词不在这一层）。

    判定逻辑在 `app/outcome.py`：状态机词汇封闭、业务结果进 outcome、
    展示标签由 `outcome.label()` 派生。
    """
    return outcome_module.final_status(state)


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
            prepared = await book.admit(step_id=step_id, tool_key=tool.tool_key or name,
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
            action_id = prepared.get('action_id')
            attempt_id = prepared.get('attempt_id')
        try:
            await tool.run(args, state)
        except Exception as exc:                      # noqa: BLE001
            hint = getattr(exc, 'safe_message', None) or type(exc).__name__
            if book is not None and action_id:
                await book.fail_from_exception(
                    step_id=step_id, action_id=action_id,
                    attempt_id=attempt_id, exc=exc, effect=tool.effect)
            results.append({'step_id': step['step_id'], 'tool': name,
                            'status': 'failed', 'error': hint})
            emit(stage_end(step_id, time.perf_counter() - started, '失败', tool.label))
            emit(action('回滚失败', f'{tool.label}：{hint}', '⚠️'))
            continue
        if book is not None and action_id:
            await book.settle(step_id=step_id, action_id=action_id,
                              attempt_id=attempt_id, status='succeeded',
                              receipt={'compensated': step['step_id']},
                              step_status='succeeded')
        results.append({'step_id': step['step_id'], 'tool': name,
                        'status': 'succeeded'})
        emit(stage_end(step_id, time.perf_counter() - started, '已回滚', tool.label))
        emit(action('已回滚一步', tool.label, '↩️'))
    return results


async def _check_boundary(state: dict) -> str:
    '''每步准入前的控制检查：暂停/取消优先于继续执行（文档 §7.2）。

    返回空串表示可以继续；返回状态名表示应当就此停下（由 Hub 收尾）。
    '''
    context = execution_control.require()
    book = ledger_module.current_ledger()
    if context is None or book is None:
        return ''
    try:
        control = await book.store.control_state(context.run_id)
    except Exception:                                  # noqa: BLE001
        return ''
    if control.get('cancel_requested_at'):
        raise execution_control.ControlStopped('运行已被取消', 'cancelled')
    if control.get('pause_requested'):
        raise execution_control.ControlStopped('已请求暂停', 'paused')
    return ''


async def run_step(state):
    """执行计划中的一步。每次只走一步，便于逐步计时与取消。

    本轮改造的三个要点（对应文档阶段 B/C/D/E）：

    1. **准入在动远程之前**：`store.admit_action` 一次事务完成"校验租约与控制请求 +
       建 action + 建 attempt + 置 in_flight"；
    2. **结算是原子推进**：动作、attempt、步骤、快照一次提交；提交失败不推进游标；
    3. **确定性来自显式标注**（`app/errors.py`），不再按异常类名猜"结果未定"。
    """
    steps = (state.get('plan') or {}).get('steps') or []
    index = state.get('step_index', 0)
    if index >= len(steps):
        return {'step_index': index}

    step = steps[index]
    name = step['tool']
    step_id = step.get('step_id') or f's{index + 1}'
    tool = await _tool_by_name(name)
    last = index + 1 >= len(steps)

    if tool is None:
        # 计划里出现了执行时不存在的工具（MCP 掉线、能力被下线）。**不能静默跳过**：
        # 跳过会让后续步骤拿不到输入，最后却可能因为别的产物而"成功"。
        emit(action('工具不可用', name, '⛔'))
        outputs = dict(state.get('step_outputs') or {})
        outputs[step_id] = {'__status__': 'blocked'}
        return {'step_index': len(steps), 'step_label': name,
                'step_outputs': outputs,
                'tool_unavailable': name,
                'status': 'needs_reconcile' if state.get('needs_reconciliation')
                          else 'failed',
                'failure': f'步骤 {step_id} 需要的工具 {name} 当前不可用，'
                           f'后续依赖它的步骤没有执行'}

    outputs = dict(state.get('step_outputs') or {})

    # 边界检查：暂停/取消在准入前先提交时，这一步不得获得调用资格
    await _check_boundary(state)

    # 依赖必须真的成功，否则这一步的输入就是不可信的（文档 §6.3）
    for dep in step.get('depends_on') or []:
        dep_status = (outputs.get(dep) or {}).get('__status__')
        if dep_status != 'succeeded':
            emit(action('依赖未完成', f'{tool.label} 依赖 {dep}（{dep_status or "缺失"}）', '⛔'))
            return {'step_index': len(steps), 'step_label': tool.label,
                    'tool_unavailable': '' if tool else name,
                    'status': 'failed',
                    'failure': f'{tool.label}依赖的步骤 {dep} 没有成功完成'}

    emit(stage_start(name, tool.label))
    started = time.perf_counter()
    args = resolve_inputs(step, outputs)
    try:
        _check_placeholders(args, tool.label)
    except Exception as exc:                       # noqa: BLE001
        emit(stage_end(name, time.perf_counter() - started, '失败', tool.label))
        hint = getattr(exc, 'safe_message', None) or type(exc).__name__
        emit(action(f'{tool.label}失败', hint, '⚠️'))
        outputs[step_id] = {'__status__': 'failed'}
        return {'step_index': len(steps), 'step_label': tool.label,
                'step_outputs': outputs, 'status': 'failed',
                'failure': f'{tool.label}失败：{hint}'}

    book = ledger_module.current_ledger()
    action_id, attempt_id, reused = None, None, False
    if book is not None:
        try:
            admitted = await book.admit(step_id=step_id,
                                        tool_key=tool.tool_key or name,
                                        effect=tool.effect, args=args)
        except execution_control.ControlStopped as exc:
            # 暂停/取消在准入前先提交：这一步不得获得调用资格
            emit(action('停止派发', exc.reason, '⏸️'))
            return {'step_index': index, 'step_label': tool.label,
                    'status': exc.status, 'failure': exc.reason}
        except execution_control.LeaseLost as exc:
            emit(action('执行权已丢失', str(exc), '⛔'))
            raise
        if admitted.get('blocked'):
            emit(action('该步骤结果未定',
                        f'{tool.label}（{admitted["blocked"]}），需核实后再继续', '❓'))
            outputs[step_id] = {'__status__': 'unknown'}
            return {'step_index': len(steps), 'step_label': tool.label,
                    'step_outputs': outputs, 'status': 'needs_reconcile',
                    'needs_reconciliation': True,
                    'failure': f'{tool.label}的结果未确定（{admitted["blocked"]}），'
                               f'为避免重复副作用已停止，请核实后重试'}
        if admitted.get('replay') is not None:
            emit(action('复用已有结果', f'{tool.label}（该动作此前已成功）', '♻️'))
            updates = dict(admitted['replay']) if isinstance(admitted['replay'], dict) else {}
            reused = True
        action_id = admitted.get('action_id')
        attempt_id = admitted.get('attempt_id')

    try:
        if not reused:
            updates = await tool.run(args, state)
    except execution_control.LeaseLost:
        raise
    except Exception as exc:                      # noqa: BLE001
        emit(stage_end(name, time.perf_counter() - started, '失败', tool.label))
        outcome = errors_module.as_outcome(exc)
        hint = outcome.safe_message or type(exc).__name__
        emit(action(f'{tool.label}失败', hint, '⚠️'))
        # 只有"结果未定 + 有副作用"才拦人：只读调用重发一次没有可重复的后果
        blocking = errors_module.is_blocking(outcome.certainty, tool.effect)
        if book is not None and action_id:
            await book.fail_from_exception(
                step_id=step_id, action_id=action_id, attempt_id=attempt_id,
                exc=exc, effect=tool.effect)
            if blocking:
                emit(action('结果不确定', '远端可能已执行，需要核实', '❓'))
        errors = list(state.get('errors', [])) + [
            {'stage': name, 'type': type(exc).__name__,
             'error_code': outcome.error_code, 'certainty': outcome.certainty,
             'effect': tool.effect}]
        # 已经发生的写操作要收回来（文档 §8.4）：只回滚声明了 compensate 且确实
        # 成功的步骤，逆序执行。**结果未定的步骤不自动补偿**——那是猜着再发一次。
        outputs[step_id] = {'__status__': ('unknown' if blocking else 'failed')}
        compensations = []
        if not blocking:
            compensations = await _run_compensations(state, steps[:index + 1],
                                                     outputs, book)
        failed_undo = [r for r in compensations if r['status'] == 'failed']
        note = ''
        if compensations:
            note = f'；已回滚 {len(compensations) - len(failed_undo)} 步'
            if failed_undo:
                note += (f'，{len(failed_undo)} 步回滚失败'
                         f'（{failed_undo[0]["tool"]}：{failed_undo[0].get("error")}）')
        status = ('needs_reconcile' if blocking
                  else 'partially_succeeded' if failed_undo else 'failed')
        return {'step_index': len(steps), 'step_label': tool.label,
                'errors': errors, 'step_outputs': outputs,
                'compensations': compensations,
                'needs_reconciliation': blocking,
                'status': status,
                'failure': f'{tool.label}失败：{hint}{note}'}

    updates = dict(updates)
    # 工具返回的 `status` 是**阶段小结**（searched / fetched / rendered / deployed），
    # 不是 run 终态：run 终态由 outcome 统一判定。
    marker = str(updates.pop('status', '') or '')
    record = {k: v for k, v in updates.items()
              if isinstance(v, (str, int, float, bool, dict, list))}
    record['__status__'] = 'succeeded'
    if marker:
        record['status'] = marker
    outputs[step_id] = record
    updates['step_outputs'] = outputs
    updates['step_index'] = index + 1
    updates['step_label'] = tool.label

    if last:
        merged = dict(state)
        merged.update(updates)
        updates['status'] = final_status(merged)
        updates['outcome'] = outcome_module.build(merged)

    # 结算：动作 + attempt + 步骤 + 快照一次提交。失败就抛出去，不推进游标——
    # "工具已返回但提交失败"时绝不能当作完成继续下一步。
    if book is not None and action_id and not reused:
        next_step_id = ''
        if index + 1 < len(steps):
            next_step_id = steps[index + 1].get('step_id') or ''
        merged = dict(state)
        merged.update(updates)
        await book.settle(step_id=step_id, action_id=action_id,
                          attempt_id=attempt_id, status='succeeded',
                          receipt={k: v for k, v in updates.items()
                                   if isinstance(v, (str, int, float, bool))},
                          step_status='succeeded', state=_snapshot_state(merged),
                          next_step_id=next_step_id)

    emit(stage_end(name, time.perf_counter() - started, marker, tool.label))
    return updates


def _snapshot_state(state: dict) -> dict:
    """快照只留**恢复要用的最小集合**：长正文与文件走产物引用，不进快照。"""
    keep = ('task_id', 'run_id', 'user_input', 'plan', 'step_index',
            'step_outputs', 'artifact', 'prior_artifact', 'documents',
            'prior_documents', 'deployment', 'answer', 'errors', 'status',
            'outcome', 'compensations', 'needs_reconciliation', 'failure')
    snapshot = {key: state.get(key) for key in keep if key in state}
    # documents 里不放大段正文（正文在 artifacts/<task>/sources/*.txt）
    if isinstance(snapshot.get('documents'), list):
        snapshot['documents'] = [
            {k: v for k, v in doc.items() if k not in ('raw_content', 'snippet')}
            for doc in snapshot['documents'] if isinstance(doc, dict)]
    return snapshot
