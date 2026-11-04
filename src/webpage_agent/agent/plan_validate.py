"""计划校验。

判断一份候选计划合不合法：工具在不在、依赖和引用形状、确认边界、结果落点、
完成条件、修订时哪些目标字段可以改。不调模型，也不执行工具。

规划节点 `plan` / `revise_plan` 仍在 `nodes`：它们负责问模型和提交，校验规则
集中在这里。
"""
import json

from webpage_agent.agent.refs import _PLACEHOLDER, _iter_bindings, _iter_strings
from webpage_agent.core.config import settings
from webpage_agent.tools import core as tool_module


# 步数上限从配置读：预算不该写死在内核常量里（文档 §7）
MAX_STEPS = settings.max_steps


def _completed_step_definitions(outputs, steps):
    known = {step['step_id']: step for step in steps}
    return [known.get(key, {'step_id': key, 'tool': ''})
            for key, value in outputs.items() if value.get('__status__') == 'succeeded']


def validate_plan(raw_steps: list[dict], catalog, *, completed_steps=(),
                  after_segment: str = '', observation_goal: str = '') -> tuple[list[dict], list[dict]]:
    """校验计划，返回 (可执行步骤, 结构化错误)。

    文档 §6.3 要求校验工具存在与权限、引用合法、无环、参数类型合理、不超预算。
    这里不去"丢坏的留好的"——计划是一个整体，被引用的一步坏了后面的都不可信，
    所以错误要回给规划器修复，而不是静默拼一个残缺计划去执行。

    `after_segment` / `observation_goal` 可选：给了才检查"提议必须停在确认边界"。
    不给（老的调用点、单测）就只做结构化校验，不引入跟片段有关的判决。
    """
    known = {tool.name: tool for tool in catalog}
    issues: list[dict] = []
    steps: list[dict] = []
    sources = {step['step_id']: step for step in completed_steps}
    seen_ids: set[str] = set(sources)

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
        for path, binding in _iter_bindings(step['args']):
            source = sources.get(str(binding.get('step_id') or ''))
            if source is None:
                issues.append({'code': 'bad_reference', 'step_id': step_id,
                               'detail': f'{path} 引用的步骤不是已完成或更早的步骤'})
            else:
                source_tool = known.get(source['tool'])
                pointer = binding.get('pointer') or ''
                if not isinstance(pointer, str) or (source_tool and not tool_module.output_pointer_possible(getattr(source_tool, 'output_schema', None), pointer)):
                    issues.append({'code': 'bad_output_reference', 'step_id': step_id,
                                   'detail': f'{path} 的引用 {pointer!r} 不符合 {source["tool"]} 声明的返回结构'})
        # 字面模板（`{{s1.revision}}`）不是绑定：系统不会替换它，服务端会拿到一串
        # 花括号。放到执行时才查等于把"能提前修的规划错误"变成一次失败的远端调用，
        # 所以在这里就当成阻断级错误交回模型。
        for path, text in _iter_strings(step['args']):
            if _PLACEHOLDER.search(text):
                issues.append({'code': 'unresolved_template', 'step_id': step_id,
                               'detail': f'参数 {path} 还是字面模板 {text!r}：跨步骤取值'
                                         '要写成 {"ref": {"step_id": "更早的步骤", '
                                         '"pointer": "/字段"}}，不要写 {{...}} 模板'})
        steps.append(step)
        sources[step_id] = step
    issues.extend(check_confirmation_boundary(steps, catalog,
                                              after_segment=after_segment,
                                              observation_goal=observation_goal))
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


RESULT_LOCI = ('chat', 'platform_state', 'platform_change')


def _is_mcp(tool) -> bool:
    if getattr(tool, 'server_id', ''):
        return True
    return str(getattr(tool, 'tool_key', '') or '').startswith('mcp:')


def _is_write(tool) -> bool:
    return str(getattr(tool, 'effect', '') or '') in ('write', 'destructive')


def _is_public(tool) -> bool:
    return getattr(tool, 'scope', '') == 'public'


def check_confirmation_boundary(steps: list[dict], catalog, *,
                                after_segment: str = '',
                                observation_goal: str = '') -> list[dict]:
    """确认边界：提议与提交不能塞进同一片段；提议之后必须停在确认边界。

    真实流程只有一条：片段 A 做准备/提议 → 停在 `after_segment=observe` → 用户在平台上
    确认 → 片段 B 才执行确认类工具（付费下单、提交/下发）。哪些工具属于"确认后执行"、
    哪些是"触发确认的提议"，由服务端在配置里声明（`confirmation_tools` /
    `proposal_tools`）；内核不认识任何业务名字。

    两个方向都要管，因为两头都真踩过：

    1. **自导自演**：`canvas.generation.propose` 与 `canvas.generation.submit` 同片段，
       模型把"用户确认后提交"写进 why 就当作确认已经发生，服务端直接拒绝
       （`production_authorization_required`）→ `confirmation_bypasses_user`。
    2. **提议不留白**：片段以提议结尾却标成 `after_segment=verify`，审查器按"必须覆盖
       全部要求"把正确计划判死，规划器再去补 submit，等于被逼着走越权那条路
       → `proposal_needs_user_confirmation`，要求改成 observe 并写清等什么确认。

    判据保守：确认类工具只在"同片段更早出现提议"或"同片段还有该服务端写操作"时才算
    越权（确认之后只读核对再提交是允许的）；提议规则只在调用方给了片段元数据时生效。
    """
    from webpage_agent.integrations import mcp_servers
    known = {tool.name: tool for tool in catalog}
    declared: dict[str, tuple[set, set]] = {}

    def config_of(server_id: str) -> tuple[set, set]:
        if server_id not in declared:
            server = mcp_servers.by_id(server_id) if server_id else None
            def names(attribute):
                return {str(x).strip() for x in (getattr(server, attribute, ()) or ())
                        if str(x).strip()}
            declared[server_id] = (names('confirmation_tools'), names('proposal_tools'))
        return declared[server_id]

    def answers_to(tool, names: set) -> bool:
        if not names:
            return False
        key = str(getattr(tool, 'tool_key', '') or '')
        bare = key.rsplit(':', 1)[-1] if key else ''
        return bool(names & {key, bare, str(getattr(tool, 'name', '') or '')})

    issues: list[dict] = []
    confirming: set[str] = set()
    proposals: list[tuple[int, dict]] = []
    for index, step in enumerate(steps):
        tool = known.get(step.get('tool'))
        server_id = getattr(tool, 'server_id', '') if tool is not None else ''
        if not server_id:
            continue
        confirms, proposes = config_of(server_id)
        if answers_to(tool, confirms):
            confirming.add(step['step_id'])
            continue
        if answers_to(tool, proposes):
            proposals.append((index, step))

    for index, step in enumerate(steps):
        step_id = step['step_id']
        if step_id not in confirming:
            continue
        tool = known[step.get('tool')]
        server_id = tool.server_id
        _, proposals_of_server = config_of(server_id)
        proposed_before = []
        for other in steps[:index]:
            other_tool = known.get(other.get('tool'))
            if other_tool is not None and other_tool.server_id == server_id \
                    and answers_to(other_tool, proposals_of_server):
                proposed_before.append(other['step_id'])
        writes_beside = []
        for other in steps:
            if other is step:
                continue
            other_tool = known.get(other.get('tool'))
            if other_tool is not None and other_tool.server_id == server_id \
                    and _is_write(other_tool):
                writes_beside.append(other['step_id'])
        if proposed_before:
            issues.append({'code': 'confirmation_bypasses_user', 'step_id': step_id,
                           'tool': tool.name,
                           'detail': (f'{tool.name} 需要用户在平台确认后才能执行，但同一片段里 '
                                      f'{proposed_before[0]} 才刚登记提议；提议与提交必须分属不同片段：'
                                      '提议后以 after_segment=observe 停下等确认，确认后再提交')})
        elif writes_beside:
            issues.append({'code': 'confirmation_bypasses_user', 'step_id': step_id,
                           'tool': tool.name,
                           'detail': (f'{tool.name} 必须单独成段执行，同一片段里还有写操作 '
                                      f'{writes_beside[0]}；先写入并停下等用户确认，'
                                      '确认后只需只读核对再提交')})

    # 片段里有确认类工具时，上面已经判过越权；提议规则只在"纯准备片段"上生效。
    if proposals and after_segment and not confirming:
        index, step = proposals[-1]
        trailing = [other['step_id'] for other in steps[index + 1:]]
        cut = after_segment != 'observe'
        if cut or trailing or not observation_goal.strip():
            if cut:
                detail = (f'{step["step_id"]} 登记了提议，但片段 after_segment={after_segment}：'
                          '提议之后必须停下等用户确认，把 after_segment 设为 observe 并在 '
                          'observation_goal 写清要等用户确认什么；确认后再用下一次片段提交')
            elif trailing:
                detail = (f'{step["step_id"]} 登记了提议，之后还有 {trailing[0]}：'
                          '提议就是等用户确认的地方，后面的步骤要留到确认后的下一次片段')
            else:
                detail = (f'{step["step_id"]} 登记了提议并以 observe 结束，但没有写 '
                          'observation_goal：要说明等用户确认的是哪件事，否则无法判断该不该继续')
            issues.append({'code': 'proposal_needs_user_confirmation',
                           'step_id': step['step_id'], 'tool': step.get('tool') or '',
                           'detail': detail})
    return issues


def check_result_locus(locus: str, steps: list[dict], catalog,
                       criteria: list | None = None, *,
                       after_segment: str = 'verify',
                       completed_write_servers=()) -> list[dict]:
    """计划里的工具必须和声明的结果落点一致。

    空落点只在步骤已经用了公开网页工具或平台工具时才拦住，让旧的本地测试计划
    仍能跑。模型把平台修改错标成 chat 时，这一步看不出来，靠规划提示词。
    """
    known = {tool.name: tool for tool in catalog}
    chosen = [known[str(step.get('tool') or '')]
              for step in steps or []
              if str(step.get('tool') or '') in known]
    names = [str(step.get('tool') or '') for step in steps or []]
    locus = str(locus or '').strip()
    if not locus:
        if any(_is_public(tool) or _is_mcp(tool) for tool in chosen):
            return [{'code': 'bad_result_locus',
                     'detail': '用了公开网页或平台工具时，result_locus 必须是 '
                               'chat、platform_state 或 platform_change'}]
        return []
    if locus not in RESULT_LOCI:
        return [{'code': 'bad_result_locus',
                 'detail': 'result_locus 必须是 chat、platform_state 或 platform_change'}]

    # 两边同源（都由 `names` 派生），长度必然相等：加 strict 是免费的自证
    public = [name for name, tool in zip(names, [
        known.get(name) for name in names], strict=True)
              if tool is not None and _is_public(tool)]
    mcp_writes = [tool for tool in chosen if _is_mcp(tool) and _is_write(tool)]
    mcp_reads = [tool for tool in chosen if _is_mcp(tool) and tool.effect == 'read']
    explain_only = names == ['answer_directly']
    issues: list[dict] = []
    if locus == 'chat' and mcp_writes:
        issues.append({
            'code': 'locus_mismatch',
            'detail': 'result_locus=chat 时不能调用对方平台的写操作。'
                      '用户要改对方产品就改为 platform_change'})
    if locus == 'platform_state':
        if mcp_writes:
            issues.append({'code': 'platform_state_cannot_write',
                           'detail': 'platform_state 只能读取平台状态，不能修改平台对象'})
        if public:
            issues.append({
                'code': 'public_web_for_platform_state',
                'detail': '要读的数据在对方平台里，不能用网页搜索、抓页或资料转 HTML'})
        if not mcp_reads and not explain_only:
            issues.append({
                'code': 'platform_state_needs_read',
                'detail': 'platform_state 要用对方平台的只读工具；没有这种工具时'
                          '只留一步 answer_directly，说明看不到'})
    if locus == 'platform_change':
        preparation = after_segment == 'observe' and bool(mcp_reads) and not public
        readback = bool(mcp_reads) and not public and all(
            t.server_id in completed_write_servers for t in mcp_reads)
        if not mcp_writes and not explain_only and not preparation and not readback:
            issues.append({
                'code': 'platform_change_needs_write',
                'detail': '对方产品上的修改需要 write 或 destructive 的平台工具。'
                          '没有这种工具时只留一步 answer_directly 说明缺什么，'
                          '不要用网页搜索充数'})
        elif mcp_writes:
            targets = set()
            for item in criteria or []:
                if isinstance(item, dict) and item.get('kind') == 'tool_succeeded':
                    targets.add(str(item.get('target') or ''))
            write_names = {tool.name for tool in mcp_writes}
            write_targets = write_names | {tool.tool_key for tool in mcp_writes}
            write_targets.update(str(step.get('step_id') or '') for step in steps
                                 if step.get('tool') in write_names)
            write_targets.discard('')
            if not targets & write_targets:
                issues.append({
                    'code': 'platform_change_needs_evidence',
                    'detail': 'platform_change 的完成条件要用 tool_succeeded 指向实际的写工具'
                              + f'（{", ".join(sorted(write_names))}）'})
    return issues


def _completed_write_servers(state):
    return {str(row.get('tool_key') or '').split(':', 2)[1]
            for row in state.get('actions_snapshot') or []
            if row.get('status') == 'succeeded' and row.get('effect') in ('write', 'destructive')
            and str(row.get('tool_key') or '').startswith('mcp:')
            and len(str(row.get('tool_key')).split(':', 2)) == 3}


# 只能在**本地**核对、远端调用永远满足不了的条件。
_LOCAL_OUTPUT_CRITERIA = ('artifact_exists', 'file_written')


def produces_local_output(tool) -> bool:
    """这份工具会不会在本地留下产物（文件 / artifact）。

    由**能力包自己声明**（`Tool.local_output`），不按工具名猜：远端 MCP 工具改的是
    对方平台，`answer_directly` 只发一句话，两者都不是产物来源。
    """
    return bool(getattr(tool, 'local_output', False))


def produces_local_file(tool) -> bool:
    """这份工具会不会写出本地文件（`file_written` 的核对依据）。"""
    return bool(getattr(tool, 'file_output', False))


def produces_deployment(tool) -> bool:
    """这份工具会不会产出部署回执（由能力包声明 `Tool.deployment_output`）。"""
    return bool(getattr(tool, 'deployment_output', False))


def check_completion_criteria(criteria, steps, catalog, locus: str = '',
                              completed_steps=()) -> tuple[list[dict], list[dict]]:
    """完成条件必须能和**这份计划真能产出的东西**对上。

    三类问题，处理方式不同：

    - **结构上不可能满足**（平台落点计划里的 `artifact_exists`）：丢掉并留 warning。
      平台上的修改已经做完了，却因为"本地没有文件"报未完成，是最伤人的一种假失败。
    - **指向不存在的东西**（`tool_succeeded` 指向这份计划里没有的步骤/工具，或没写
      目标）：阻断，交回模型修——它可以改条件，也可以补上缺的那一步。
    - **要证据但计划里没有产出方**（`deployment_receipt` 没有部署步骤、
      `answer_present` 没有作答步骤）：同样阻断，让模型补步骤或改条件。

    判据一律来自工具自己声明的能力（`local_output` / `deployment_output`），不按
    工具名猜；`content_quality` 这类需要模型评价的条件不受影响。
    """
    known = {tool.name: tool for tool in catalog}
    plan_steps = list(steps or [])
    specs = [known.get(str(step.get('tool') or '')) for step in plan_steps]
    step_ids = {str(step.get('step_id') or '') for step in plan_steps}
    target_names = set(step_ids)
    target_names.update(str(spec.name) for spec in specs if spec is not None)
    target_names.update(str(spec.tool_key) for spec in specs if spec is not None)
    # A revision contains only remaining work. Retained criteria may refer to an
    # already successful action by its public tool name or stable key as well
    # as its step ID; all three identities refer to the same recorded action.
    for step in completed_steps or []:
        target_names.update(str(step.get(key) or '')
                            for key in ('step_id', 'tool', 'tool_key'))
    target_names.discard('')
    has_local_output = any(produces_local_output(spec) for spec in specs
                           if spec is not None)
    has_local_file = any(produces_local_file(spec) for spec in specs
                         if spec is not None)
    has_deployment = any(produces_deployment(spec) for spec in specs
                         if spec is not None)
    def _answers(spec) -> bool:
        # 作答能力包有两个工具（直接作答 / 联网作答），都可能写 state['answer']
        return (str(getattr(spec, 'capability', '') or '') == 'answers'
                or str(getattr(spec, 'tool_key', '') or '').startswith('local:answers:'))

    done_specs = [known.get(str(step.get('tool') or ''))
                  for step in completed_steps or []]
    has_answer = any(_answers(spec) for spec in [*specs, *done_specs]
                     if spec is not None)
    platform = locus in ('platform_state', 'platform_change')

    kept: list[dict] = []
    issues: list[dict] = []
    for item in criteria or []:
        kind = str((item or {}).get('kind') or '')
        target = str((item or {}).get('target') or '')
        satisfiable = {'artifact_exists': has_local_output,
                       'file_written': has_local_file}.get(kind)
        if kind in _LOCAL_OUTPUT_CRITERIA and not satisfiable:
            if platform:
                issues.append({
                    'code': 'criterion_not_applicable', 'severity': 'warning',
                    'detail': f'完成条件 {kind} 要在本地核对，但这份计划只有对方平台'
                              '的调用与作答，本地不会产出文件，已忽略；平台上的修改'
                              '请用 tool_succeeded 指向写步骤'})
                continue
            issues.append({
                'code': 'criterion_needs_local_step',
                'detail': f'完成条件 {kind} 要在本地核对，但这份计划里没有任何能产出'
                          '本地文件的步骤：补上对应步骤，或者去掉这条条件'})
            continue
        if kind == 'deployment_receipt' and not has_deployment:
            issues.append({
                'code': 'criterion_needs_deploy_step',
                'detail': '完成条件 deployment_receipt 要有真正执行部署的步骤，'
                          '这份计划里没有：补上部署步骤，或者去掉这条条件'})
            continue
        if kind == 'answer_present' and not has_answer:
            issues.append({
                'code': 'criterion_needs_answer_step',
                'detail': '完成条件 answer_present 要有作答步骤（answer_directly），'
                          '这份计划里没有：补上收尾作答，或者去掉这条条件'})
            continue
        if kind == 'tool_succeeded':
            if not target:
                issues.append({
                    'code': 'criterion_without_target',
                    'detail': '完成条件 tool_succeeded 必须写 target（要核对的步骤编号'
                              '或工具名），否则核对时无从匹配'})
                continue
            if target not in target_names:
                issues.append({
                    'code': 'criterion_target_unknown',
                    'detail': f'完成条件 tool_succeeded 指向 {target!r}，但这份计划里'
                              f'没有这个步骤或工具（可用：{", ".join(sorted(target_names))[:120]}）'})
                continue
        kept.append(item)
    return kept, issues


def _valid_steps(raw_steps: list[dict], catalog) -> tuple[list[dict], list[str]]:
    """兼容旧调用点：返回 (步骤, 被丢弃的工具名)。"""
    steps, issues = validate_plan(raw_steps, catalog)
    dropped = [issue.get('tool') or issue['detail'] for issue in issues
               if issue['code'] == 'unknown_tool']
    return steps, dropped


def revision_metadata(data: dict, previous: dict, fields_set=None) -> tuple[dict, list[dict]]:
    """两种修订共用的目标约束。省略字段继承，完成条件只能累加。"""
    provided = set(data) if fields_set is None else set(fields_set)
    old_locus = str(previous.get('result_locus') or '')
    proposed = str(data.get('result_locus') or old_locus)
    issues = []
    if old_locus and proposed != old_locus:
        issues.append({'code': 'result_locus_changed',
                       'detail': '修订不能改变原目标的结果落点；如需改变目标，请先结束当前目标再新建任务'})
    criteria, seen = [], set()
    replacement_ids = {}
    for step in previous.get('steps') or []:
        if step.get('step_id') in (data.get('superseded') or []):
            replacements = [new for new in (data.get('steps') or [])
                            if new.get('tool') == step.get('tool')]
            if len(replacements) == 1:
                replacement_ids[step['step_id']] = replacements[0]['step_id']
    for item in [*(previous.get('completion_criteria') or []),
                 *(data.get('completion_criteria') or [])]:
        item = dict(item)
        if item.get('kind') == 'tool_succeeded' and item.get('target') in replacement_ids:
            item['target'] = replacement_ids[item['target']]
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            criteria.append(item)
    segment = (data.get('after_segment') if 'after_segment' in provided
               else previous.get('after_segment')) or 'verify'
    goal = (data.get('observation_goal') if 'observation_goal' in provided
            else previous.get('observation_goal')) or ''
    if segment not in ('observe', 'verify') or (segment == 'observe' and not goal.strip()):
        issues.append({'code': 'bad_segment_policy',
                       'detail': '片段策略只能是 observe/verify，observe 必须给出观察目标'})
    return {'result_locus': old_locus or proposed,
            'completion_criteria': criteria, 'after_segment': segment,
            **({'request_review_required': True} if previous.get('request_review_required') else {}),
            'observation_goal': goal if segment == 'observe' else ''}, issues
