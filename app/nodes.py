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

MAX_STEPS = 8

PLANNER_SYSTEM = (
    '你是任务编排器。阅读用户要求与可用工具，输出一个**按顺序执行**的步骤计划。'
    '规则：'
    '① 只能使用工具清单里出现过的名字，不要编造；args 必须符合该工具的参数 schema。'
    '② 需要外部或实时事实（最新、价格、版本、新闻）或用户要求调研时，先检索：'
    'web_search（用户已给 URL 就直接 fetch_pages），需要正文再用 fetch_pages 补抓。'
    '但若「已有资料」里已经有正文，而用户只是要把这些内容整理/转换成文档或回答，'
    '就不要再检索，直接进入收尾步骤——重复检索既慢又会引入无关来源。'
    '③ 检索完怎么收尾，取决于用户要的成果形态：'
    '只想"问个明白"（问怎么做/是什么/哪个好，但没有要文件）→ answer_with_sources，'
    '在聊天里带来源作答，不要为这种需求生成 HTML；'
    '明确要报告/HTML/文档/页面 → compose_report → render_html。'
    '④ "把资料/搜索结果存成网页"（要的是原始资料本身，不要提炼）→ render_sources_html；'
    '要的是**整理后的报告**才用 compose_report → render_html。两者别混。'
    '⑤ **名词解释、原理、常识、做法建议**这类问题，如果你自己有把握，就直接用'
    'answer_directly 作答，不要为了保险去检索——把"XX 是什么/为什么/怎么用"都变成'
    '一次联网搜索既慢又没必要。只有下列情况才必须检索：'
    '涉及具体数字/价格/日期/版本/排名、明确要"最新/现在/今年"、提到你不确定的新名词'
    '或圈内事件、或者用户明说要查资料。拿不准时先直接答，并在回答里说明"未联网核实"。'
    '寒暄、确认一律 answer_directly。'
    '⑥ 只有用户明确要求发布到服务器/要访问地址时，才在最后加 deploy_site；'
    '若"部署"只是报告的研究主题（如"数据库的部署方式"），不要加。'
    '⑦ 调用查询类 MCP 工具时，只有不带过滤参数（如 keyword）的结果才是全量清单；'
    '删改类操作（delete_*/update_*）之后若要告诉用户"还剩什么/还剩几个"，'
    '必须再加一步**不带过滤参数**的查询（如 mcp__list_sites 且参数为空），'
    '绝不能拿操作前的过滤结果或操作返回体去推断。'
    '⑧ 最多 8 步；不要重复完全相同的调用；步数能少不多。'
    '⑨ 若提供了对话历史，本轮要求可能是对上一轮的补充或修正，要结合上文理解意图。'
    'profile_updates：只填**本轮用户新说出、且画像里还没有（或已变化）**的个人信息'
    '（姓名、电话、邮箱、职业、偏好、习惯等），键用中文（如"电话"）。'
    '系统提示末端的「[用户画像]」段里已经出现过的事实，一律不要再填一遍——'
    '重复上报会让界面误报"已记住"。没有新信息就留空 {}}。'
    '只记用户明确说出的，不要推断、不要编造。'
)


def _catalog_text(catalog) -> str:
    return '\n'.join(tool_module.describe_tool(tool) for tool in catalog)


def fallback_steps(state: dict) -> list[dict]:
    """规划失败时的兜底：**不猜业务**。

    这里以前会拼一条"检索 → 抓正文 → 成稿 → 渲染"的固定链路，等于把网页研究
    业务写死进内核，而且用户只是想问句话也会被拉去跑一分钟检索。
    中台不该替用户决定做什么——退化成一次直接作答，并明说规划失败。
    """
    return [{'tool': 'answer_directly',
             'args': {'question': state.get('user_input') or ''},
             'why': '规划失败，降级为直接作答'}]


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
        steps.append({'tool': name, 'step_id': step_id,
                      'args': dict(args or {}), 'depends_on': depends_on,
                      'why': str(raw.get('why') or ''),
                      'tool_key': known[name].tool_key,
                      'effect': known[name].effect})
    return steps, issues


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
        return {'plan': {'steps': steps,
                         'summary': resumed.get('summary') or '续跑'},
                'step_index': int(resumed.get('step_index') or 0),
                'status': 'planned'}

    # 明显的寒暄不必花一次模型调用去规划：直接给一步"直接回答"。
    # 这一步放在发现工具之前——否则为了判一句"你好"还要去探一次 MCP 服务端。
    if not urls_from_input(question) and is_small_talk(question):
        emit(action('跳过规划', '识别为寒暄，直接回答', '⚡'))
        steps = [{'tool': 'answer_directly',
                  'args': {'question': question}, 'why': '寒暄/确认'}]
        emit(thought(describe_plan(steps)))
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
    try:
        # 最多两轮：第一次照常规划；若整体校验不通过，把结构化错误交回模型修复
        for round_index in range(2):
            execution, call_usage = await structured_call(
                ExecutionPlan,
                profile.with_profile(PLANNER_SYSTEM),
                payload,
                disable_thinking=settings.llm_parse_disable_thinking,
            )
            data = execution.model_dump()
            steps, issues = validate_plan(data.get('steps') or [], catalog)
            summary = data.get('summary') or ''
            updates = data.get('profile_updates') or {}
            usage.append({'stage': 'plan', 'model_name': settings.llm_model,
                          'model': call_usage})
            if not issues:
                break
            emit(action('计划校验未通过',
                        '；'.join(f'{i["code"]}:{i["detail"]}' for i in issues)[:120],
                        '⚠️'))
            if round_index == 0:
                # 把错误原文喂回去，让模型在同一份工具清单上修
                payload = json.dumps({
                    '用户要求': question,
                    '上次计划': data.get('steps'),
                    '校验错误': issues,
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
    if not steps:
        steps, _ = _valid_steps(fallback_steps(state), catalog)
        summary = summary or '降级计划'

    # 只播报真正新增/变化的事实：模型常把画像里已有的事实再抄一遍，
    # 若无条件播报，用户会看到"已记住"一条自己本轮根本没说过的话。
    changed = profile.merge(updates)
    if changed:
        emit(action('已记住', profile.describe(changed), '🧠'))
    emit(thought(describe_plan(steps, summary)))
    result = {'plan': {'steps': steps, 'summary': summary},
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


async def run_step(state):
    """执行计划中的一步。每次只走一步，便于逐步计时与取消。"""
    steps = (state.get('plan') or {}).get('steps') or []
    index = state.get('step_index', 0)
    if index >= len(steps):
        return {'step_index': index}

    step = steps[index]
    name = step['tool']
    tool = tool_module.BUILTIN_BY_NAME.get(name)
    if tool is None:
        tool = {t.name: t for t in await tool_module.mcp_tools()}.get(name)
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
        return {'step_index': len(steps), 'step_label': tool.label,
                'errors': errors, 'status': 'failed', 'step_outputs': outputs,
                'failure': f'{tool.label}失败：{hint}'}

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
