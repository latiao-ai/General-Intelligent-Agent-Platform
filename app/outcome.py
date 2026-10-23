"""终态：通用状态机词汇 + outcome 记录 + 派生的展示标签。

三层分工（文档 §3.1「业务结果迁入通用输出」、§7「运行/步骤/动作状态」）：

1. **状态机词汇是封闭的**。`runs.status` / `tasks.status` 只允许
   `RUN_STATUSES` 里的通用值，与接了哪些 MCP 服务端、哪些业务能力无关。
   接第 N 个能力时状态值**一个不加**——差异落在 `outcome` 与 `tool_key` 上，
   而那是数据行，不是枚举值。

2. **outcome 是记录**。这一轮到底产出了什么（产物引用）、发生了哪些有副作用
   的调用、有没有结果没核实。它是数据，可以有无穷多种组合。

3. **标签是视图**。`answered` / `local_ready` / `deployed` 这些词只用于界面与
   回执文案，由 `label()` 从通用状态 + outcome 算出来。新增能力要加展示词，
   改这里和前端文案表即可，**不要**再往任务/运行状态里加值。

以前的写法把第 3 层塞进了第 1 层：`tasks.status` 直接存 `local_ready`，收尾时
再用 `RUN_STATUS_OF.get(status, status)` 折成 run 状态——那个兜底使得"忘了映射"
的新业务词会被**静默写进 run 状态机**，词汇表迟早被业务词撑爆（每加一类能力就
往 `TERMINAL_STATUS` 里加一个值）。而且 `deploy_needs_check` 曾经是个走不到的
僵尸值：老实现只在地址合法时返回成功，否则抛错。这类值一旦进了状态机就很难删。
"""
import logging

logger = logging.getLogger(__name__)

# 非终态：正在排队/执行/对账
RUN_STATUSES = frozenset({
    'queued', 'running', 'reconciling',
    'waiting_input', 'waiting_approval',
    # paused：安全暂停，等人显式 resume。**不是**失败也不是完成。
    'paused',
    'succeeded', 'partially_succeeded', 'needs_reconcile',
    'failed', 'cancelled', 'interrupted',
})
ACTIVE_RUN_STATUSES = frozenset({'queued', 'running', 'reconciling'})
# "本次执行已退出"但"目标还没最终结束"的状态：paused / waiting_input 都属于这一类，
# 前端不再转圈，但也不能当作完成。
STOPPED_RUN_STATUSES = frozenset({'paused', 'waiting_input', 'waiting_approval'})
TERMINAL_RUN_STATUSES = RUN_STATUSES - ACTIVE_RUN_STATUSES
# 需要人工看一眼的状态：结果没核实 / 有副作用没收回。
ATTENTION_RUN_STATUSES = frozenset({'needs_reconcile', 'partially_succeeded'})
# 可以显式恢复的状态（resume 只接受这些）
RESUMABLE_RUN_STATUSES = frozenset({'paused', 'interrupted', 'queued'})

# 历史数据（和可能残留的旧调用点）里的业务终态 → 通用状态。
# 这张表**只增不改语义**：它负责让旧数据继续可读，不是新的状态词汇。
LEGACY_STATUS = {
    'answered': 'succeeded',
    'local_ready': 'succeeded',
    'deployed': 'succeeded',
    'compensated': 'succeeded',
    'deploy_needs_check': 'needs_reconcile',
}

# 哪些动作性质算"有副作用"，要进 outcome.effects。
WRITE_EFFECTS = frozenset({'write', 'destructive'})

# 产物/副作用指向的对象标识，按常见字段名依次取（尽力而为，取不到就空着）。
_TARGET_KEYS = ('deployment_id', 'site_id', 'artifact_id', 'id')


def normalize_status(status) -> str:
    """把历史业务终态归一成通用状态；未知值原样返回（不猜、也不掩盖）。"""
    value = str(status or '').strip()
    return LEGACY_STATUS.get(value, value)


def check_run_status(status) -> str:
    """任务/运行层只接受通用状态。

    越界说明代码里又有人想往状态机里加业务终态。这里**记 error 并退化成
    failed**，而不是静默写库——静默正是上一版状态词汇失控的原因。
    """
    value = normalize_status(status)
    if value in RUN_STATUSES:
        return value
    logger.error('非法的 run 状态 %r：run/task 层只允许通用状态 %s',
                 value, sorted(RUN_STATUSES))
    return 'failed'


def build(state: dict) -> dict:
    """从执行结果汇总 outcome 记录（可持久化、可查询，不含展示用语）。"""
    state = state or {}
    plan = state.get('plan') or {}
    produced = _produced(state)
    effects = _effects(plan, state.get('step_outputs') or {})
    answered = bool(state.get('answer'))
    if plan.get('kind') == 'compensation':
        kind = 'compensation'
    elif state.get('deployment'):
        kind = 'deployment'
    elif answered:
        kind = 'answer'
    elif produced:
        kind = 'artifact'
    else:
        kind = 'none'
    return {
        'kind': kind,
        'produced': produced,
        'effects': effects,
        'answered': answered,
        # 有副作用没核实：写请求可能成功了，只是回执看不懂
        'unverified': any(not item.get('verified') for item in effects),
    }


def final_status(state: dict) -> str:
    """本轮收尾后的**通用**状态。

    只按"产出了什么 / 副作用是否核实"判定，不认识具体业务：
    新增能力只要产出 artifact 或 answer，这里一行都不用改。
    """
    state = state or {}
    plan = state.get('plan') or {}
    if plan.get('kind') == 'compensation':
        return 'failed' if state.get('failure') else 'succeeded'
    deployment = state.get('deployment') or {}
    if deployment:
        return 'succeeded' if deployment.get('status') == 'success' \
            else 'needs_reconcile'
    # 有产出（页面或 write_file 写出的文件）就是成功：不能因为"没生成视频"
    # 就把三个脚本文件说成"没有产出可用结果"。
    if state.get('answer') or _produced(state):
        return 'succeeded'
    return 'failed'


def label(status, state: dict | None = None) -> str:
    """界面与回执用的**展示标签**：业务词只允许出现在这一层。

    通用状态原样返回；`succeeded` 再按 outcome 细分成人话能懂的说法。
    没有也行的东西（比如旧数据的 state 里没有 outcome）就现算一份。
    """
    value = normalize_status(status)
    if value == 'needs_reconcile':
        # 只有真的发生了部署才用部署的说法；其它能力一律通用词
        return 'deploy_needs_check' if (state or {}).get('deployment') else value
    if value != 'succeeded':
        return value
    outcome = (state or {}).get('outcome') or build(state or {})
    return {'answer': 'answered', 'artifact': 'local_ready',
            'deployment': 'deployed', 'compensation': 'compensated'
            }.get(outcome.get('kind'), value)


def view(status, state: dict | None = None) -> dict:
    """接口与事件里携带的三件套：展示标签、通用状态、outcome 记录。

    `status` 保持旧字段名与前端的词汇（展示标签），新增的 `run_status`
    才是状态机里的值。两者不要混用：判流程用 `run_status`，给人看用 `status`。
    """
    state = state or {}
    generic = check_run_status(status)
    return {'status': label(generic, state),
            'run_status': generic,
            'outcome': state.get('outcome') or build(state)}


def _produced(state: dict) -> list[dict]:
    """本轮真正产出的东西：页面产物 + write_file 写出来的文件。

    write_file 的产物以前不算数，于是"写了三个脚本文件"的任务被判成 failed——
    用户看到的却是"本次没有产出可用结果"，自相矛盾。
    """
    produced = []
    artifact = state.get('artifact') or {}
    artifact_path = artifact.get('path') or artifact.get('html_path')
    if artifact_path:
        produced.append({'kind': artifact.get('kind') or 'report',
                         'artifact_id': artifact.get('artifact_id') or '',
                         'path': artifact_path,
                         'sha256': artifact.get('sha256') or '',
                         'size_bytes': artifact.get('size_bytes')})
    for output in (state.get('step_outputs') or {}).values():
        if not isinstance(output, dict):
            continue
        written = output.get('written')
        if isinstance(written, dict) and written.get('path'):
            produced.append({'kind': 'file',
                             'artifact_id': '',
                             'path': written['path'],
                             'sha256': '',
                             'size_bytes': written.get('size_bytes'),
                             'relative': written.get('relative') or ''})
    return produced


def _effects(plan: dict, outputs: dict) -> list[dict]:
    """这一轮真正成功的**有副作用**调用。

    认的是步骤上记录的 `effect`（来自服务端 annotations 或本地声明），
    不是工具名——名字可以改，动作性质不会。
    """
    steps = {step.get('step_id'): step for step in (plan.get('steps') or [])
             if step.get('step_id')}
    effects = []
    for step_id, output in outputs.items():
        if not isinstance(output, dict) or output.get('__status__') != 'succeeded':
            continue
        step = steps.get(step_id) or {}
        effect = step.get('effect')
        if effect not in WRITE_EFFECTS:
            continue
        effects.append({'step_id': step_id,
                        'tool_key': step.get('tool_key') or '',
                        'effect': effect,
                        'target_ref': _target_ref(output),
                        'verified': _verified(output)})
    return effects


def _target_ref(output: dict) -> str:
    for source in (output.get('deployment'), output, output.get('artifact')):
        if not isinstance(source, dict):
            continue
        for key in _TARGET_KEYS:
            value = source.get(key)
            if value:
                return str(value)
    return ''


def _verified(output: dict) -> bool:
    """这一步的副作用是否已核实。

    只有回执里带了状态才可能"未核实"（写请求可能成功了，但回执看不懂）；
    没有状态说明就是"调用成功即视为已核实"。这里不按工具名特判。
    """
    deployment = output.get('deployment')
    if isinstance(deployment, dict) and deployment.get('status'):
        return deployment.get('status') == 'success'
    return True
