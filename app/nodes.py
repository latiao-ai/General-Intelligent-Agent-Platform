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

from app import profile
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
    '④ 寒暄、确认、纯常识问答（如"什么是 ACID"）不需要检索：只用 answer_directly。'
    '⑤ 只有用户明确要求发布到服务器/要访问地址时，才在最后加 deploy_site；'
    '若"部署"只是报告的研究主题（如"数据库的部署方式"），不要加。'
    '⑥ 最多 8 步；不要重复完全相同的调用；步数能少不多。'
    '⑦ 若提供了对话历史，本轮要求可能是对上一轮的补充或修正，要结合上文理解意图。'
    'profile_updates：若用户透露了需要记住的个人信息（姓名、电话、邮箱、职业、'
    '偏好、习惯等），以键值对填进去，键用中文（如"电话"）；没有就留空 {}。'
    '只记用户明确说出的，不要推断、不要编造。'
)


def _catalog_text(catalog) -> str:
    lines = []
    for tool in catalog:
        schema = json.dumps(tool.args, ensure_ascii=False)
        lines.append(f'- {tool.name}：{tool.description}\n  参数：{schema}')
    return '\n'.join(lines)


# 出现这些词才认为"用户要的是一份文档/报告"。
REPORT_HINTS = ('报告', '文档', 'html', '网页', '页面', '白皮书', '综述',
                '总结', '整理成', '生成一份', '写一份', '出一份')
# 出现这些词说明"需要联网查一下"，但未必需要文档。
RESEARCH_HINTS = REPORT_HINTS + ('整理', '调研', '研究', '对比', '比较', '分析',
                                 '梳理', '查一下', '查下', '查询', '搜一下', '搜索',
                                 '资料', '汇总', '评估', '盘点', '如何', '怎么', '怎样')


def fallback_steps(state: dict) -> list[dict]:
    """规划失败、或模型给了不可用计划时的兜底。

    要点是**别乱猜**：只有当输入明显是研究/报告诉求（含链接或研究类字眼）时才
    退到检索链路；其余一律退到"直接回答"。早先这里无条件走检索，结果用户发一句
    "暂停"也会被拿去搜一分钟——比答得不好糟得多。
    """
    text = state.get('user_input') or ''
    urls = urls_from_input(text)
    wants_report = any(hint in text.lower() for hint in REPORT_HINTS)
    needs_lookup = wants_report or any(hint in text for hint in RESEARCH_HINTS)

    if urls:
        first = {'tool': 'fetch_pages', 'args': {'urls': urls}, 'why': '用户直接给了链接'}
    elif needs_lookup:
        first = {'tool': 'web_search', 'args': {'query': text}, 'why': '需要外部资料'}
    else:
        return [{'tool': 'answer_directly', 'args': {'question': text},
                 'why': '不像需要联网的问题，直接回答'}]

    tail = ([{'tool': 'compose_report', 'args': {'topic': text}, 'why': '整理成报告'},
             {'tool': 'render_html', 'args': {}, 'why': '产出 HTML'}]
            if wants_report else
            [{'tool': 'answer_with_sources', 'args': {'question': text},
              'why': '在聊天里带来源作答'}])
    steps = [first]
    if first['tool'] != 'fetch_pages':      # 避免连着两步抓同一个东西
        steps.append({'tool': 'fetch_pages', 'args': {}, 'why': '补齐正文'})
    return steps + tail


def describe_plan(steps: list[dict], summary: str = '') -> str:
    names = ' → '.join(step.get('tool', '?') for step in steps)
    if summary:
        return f'我的执行计划：{summary}（{names}）'
    return f'我的执行计划：{names}'


def _valid_steps(raw_steps: list[dict], catalog) -> list[dict]:
    """只保留工具真实存在的步骤，args 不是对象就退回空对象。"""
    known = {tool.name for tool in catalog}
    cleaned = []
    for step in raw_steps:
        name = str(step.get('tool') or '').strip()
        if name not in known:
            continue
        args = step.get('args')
        cleaned.append({'tool': name,
                        'args': args if isinstance(args, dict) else {},
                        'why': str(step.get('why') or '')})
    return cleaned[:MAX_STEPS]


async def plan(state):
    """产出执行计划；不执行任何工具。"""
    question = state['user_input']

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
        '对话历史': (state.get('history') or [])[-8:],
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
    try:
        execution, call_usage = await structured_call(
            ExecutionPlan,
            profile.with_profile(PLANNER_SYSTEM),
            payload,
            disable_thinking=settings.llm_parse_disable_thinking,
        )
        data = execution.model_dump()
        steps = _valid_steps(data.get('steps') or [], catalog)
        summary = data.get('summary') or ''
        updates = data.get('profile_updates') or {}
        usage.append({'stage': 'plan', 'model_name': settings.llm_model,
                      'model': call_usage})
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
        steps = _valid_steps(fallback_steps(state), catalog)
        summary = summary or '默认计划'

    if updates:
        profile.merge(updates)
        emit(action('已记住', profile.describe(updates), '🧠'))
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

    emit(stage_start(name, tool.label))
    started = time.perf_counter()
    try:
        updates = await tool.run(dict(step.get('args') or {}), state)
    except Exception as exc:
        emit(stage_end(name, time.perf_counter() - started, '失败', tool.label))
        hint = getattr(exc, 'safe_message', None) or type(exc).__name__
        emit(action(f'{tool.label}失败', hint, '⚠️'))
        errors = list(state.get('errors', [])) + [
            {'stage': name, 'type': type(exc).__name__}]
        # 出错就中止剩余步骤：后面的多半依赖这一步的产物。
        return {'step_index': len(steps), 'step_label': tool.label,
                'errors': errors, 'status': 'failed',
                'failure': f'{tool.label}失败：{hint}'}

    updates = dict(updates)
    updates['step_index'] = index + 1
    updates['step_label'] = tool.label
    emit(stage_end(name, time.perf_counter() - started,
                   updates.get('status', ''), tool.label))
    if last:
        merged = dict(state)
        merged.update(updates)
        updates['status'] = final_status(merged)
    return updates
