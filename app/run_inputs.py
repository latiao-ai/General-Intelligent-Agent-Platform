"""运行中补充输入：按序组装、决策快照、消费辅助。

为什么单独一个模块：**"用户要求"必须能变成一个可比较的版本**，而不是散在
提示词里的一段自由文本。这个模块只做三件纯逻辑的事，不碰数据库、不碰模型：

1. `payload_hash()`：把一条补充的内容归一成指纹（幂等重试用）；
2. `build_decision_input()`：把"原目标 + 按序补充 + 已提交计划 + 已发生事实 +
   在途/未定动作"组装成**一次模型决策的输入**，并冻结它的 `intent_version`；
3. `revision_rules()` / `validate_revision()`：修订计划必须遵守的几条硬规则
   （已执行动作不可重写、旧计划未执行步骤标 superseded、新步骤 ID 不得覆盖旧
   输出、已完成的外部效果必须引用原动作）。

版本三分法（文档 §5 第七步）在这里体现得最直接：

| 版本 | 代表什么 | 谁改 |
|---|---|---|
| `lease_epoch` | 哪个执行者有权推进 | 领取时 +1（app/pg_store.claim_run） |
| `intent_version` | 用户要求更新到哪一版 | 接受一条补充时 +1 |
| `plan_version` | 当前采用哪份计划 | 提交一份计划时 +1 |

它们不能互相替代：补充不需要重新领取租约；新计划也不需要用户重新提需求。
"""
import hashlib
import logging

logger = logging.getLogger(__name__)

# 补充内容上限（字符）。与接口层的校验共用同一个常量，避免两处数字漂移。
MAX_INPUT_CHARS = 4000
# 一次决策里最多带多少条补充原文：再多就要求用户合并（不静默截断）。
MAX_INPUTS_IN_DECISION = 20


class InputRejected(RuntimeError):
    """补充被拒绝（状态不接受/内容非法/目标不对）。映射成 4xx。"""

    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class InputConflict(InputRejected):
    """同一个 request_id 提交了不同内容。"""

    def __init__(self, message: str = '') -> None:
        super().__init__('input_request_conflict',
                         message or '同一个 request_id 提交了不同内容')


class InputBacklog(InputRejected):
    """待处理补充超限。"""

    def __init__(self, message: str = '') -> None:
        super().__init__('input_backlog', message or '待处理补充过多')


def payload_hash(text: str) -> str:
    """内容指纹：只用于判断"同一次重试"，不用于判断"语义相同"。"""
    body = ' '.join((text or '').split())
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def normalize_inputs(inputs) -> list[dict]:
    """按 input_seq 排序并只保留需要的字段（顺序即语义，不能依赖数据库返回序）。"""
    items = []
    for item in inputs or []:
        if not isinstance(item, dict):
            continue
        message = str(item.get('message') or '').strip()
        if not message:
            continue
        items.append({
            'input_id': str(item.get('input_id') or ''),
            'input_seq': int(item.get('input_seq') or 0),
            'message': message,
            'intent_version': int(item.get('intent_version') or 0),
            'status': str(item.get('processing_status')
                          or item.get('status') or 'accepted'),
            'created_at': str(item.get('created_at') or ''),
        })
    return sorted(items, key=lambda x: x['input_seq'])


def inputs_text(inputs, *, limit: int = MAX_INPUTS_IN_DECISION) -> str:
    """把补充按序拼成一段给人/给模型看的文本。

    超限时**不截断内容**：截断等于假装理解了没读到的要求。改为明确写出
    "还有 N 条没显示，请合并后重发"，让调用方进入等待而不是猜。
    """
    items = normalize_inputs(inputs)
    shown = items[:limit]
    lines = [f'{index}. {item["message"]}'
             for index, item in enumerate(shown, 1)]
    if len(items) > len(shown):
        lines.append(f'（另有 {len(items) - len(shown)} 条补充未列出：'
                     f'一次决策最多处理 {limit} 条，请合并后再发）')
    return '\n'.join(lines)


def latest_intent_version(inputs, fallback: int = 0) -> int:
    """这批补充里最高的要求版本（没有就用 fallback）。

    注意：用它是为了**核对**"模型看到的补充是否对得上版本"，不是用来代替
    `runs.intent_version` 做门禁——门禁永远以数据库为准。
    """
    items = normalize_inputs(inputs)
    if not items:
        return int(fallback or 0)
    return max(int(item['intent_version'] or 0) for item in items)


def build_decision_input(*, original_goal: str, inputs=None, plan: dict | None = None,
                         step_outputs: dict | None = None,
                         actions=None, intent_version: int = 0,
                         processed_intent_version: int = 0,
                         artifacts: list | None = None,
                         tools: list | None = None,
                         budget: dict | None = None) -> dict:
    """组装一次模型决策的输入（**纯函数**，可单测）。

    关键在于"内容与版本相互对应"：`intent_version` 与 `inputs` 必须来自同一次
    读取。读完补充再单独取一次"最新版本"贴上去，就会出现"计划自称覆盖了 v3，
    实际只读到 v2 的补充"——这类错误在事后审计里极难发现。
    """
    items = normalize_inputs(inputs)
    done, unknown = [], []
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        entry = {'step_id': action.get('step_id') or '',
                 'tool_key': action.get('tool_key') or '',
                 'effect': action.get('effect') or 'unknown',
                 'status': action.get('status') or '',
                 'decision_intent_version': int(
                     action.get('decision_intent_version') or 0)}
        if entry['status'] == 'succeeded':
            done.append(entry)
        elif entry['status'] in ('in_flight', 'unknown'):
            # 结果未定的动作必须独立列出：修订计划不能假设它没发生
            unknown.append(entry)
    return {
        '原目标': original_goal or '',
        '按序补充': [{'seq': item['input_seq'], '要求': item['message'],
                      'intent_version': item['intent_version']}
                     for item in items],
        '补充文本': inputs_text(items),
        '决策依据版本': int(intent_version or 0),
        '已处理版本': int(processed_intent_version or 0),
        '当前计划': {
            'plan_version': int((plan or {}).get('version') or 0),
            'intent_version': int((plan or {}).get('intent_version') or 0),
            'summary': (plan or {}).get('goal') or '',
            'steps': [{'step_id': step.get('step_id'), 'tool': step.get('tool'),
                       'status': step.get('status', '')}
                      for step in ((plan or {}).get('steps') or [])],
        },
        '已完成动作': done,
        '在途或未知动作': unknown,
        '步骤输出': {k: v for k, v in (step_outputs or {}).items()
                     if isinstance(v, dict)},
        '已有产物': list(artifacts or []),
        '可用工具': list(tools or []),
        '预算': dict(budget or {}),
    }


def revision_rules() -> str:
    """修订计划时必须遵守的规则（喂给模型，也在本地校验里再查一遍）。"""
    return (
        '1) 已经执行成功的步骤不能被重写或删除，它们的输出通过 ref 引用；\n'
        '2) 原计划里还没执行的步骤一律标 superseded，不要改写它们的 step_id；\n'
        '3) 新步骤的 step_id 必须在本 run 内唯一，推荐 p<计划版本>_s<序号>；\n'
        '4) 可以引用已成功步骤的输出，不要为了拿旧结果把旧步骤再跑一遍；\n'
        '5) 依赖结果未定动作的新步骤不要排，先等核实；\n'
        '6) 补充要求不等于扩大权限：原来没有的工具/发布权限不会因为补充而获得；\n'
        '7) 已经完成的外部效果（例如已发布）要用 ref 指回原动作/产物，不要重发；\n'
        '8) 如果确实要覆盖或重新发布，那是新的业务动作，按权限规则处理。'
    )


def validate_revision(steps, *, plan_version: int, done_steps, unknown_steps,
                      known_tools) -> tuple[list[dict], list[dict]]:
    """修订计划的本地校验：返回 (保留的步骤, 问题列表)。

    只做"能确定是错的"那部分检查；语义是否真的满足补充要求，仍然由模型判断
    ——但下面每一条都是**已经踩过的坑**，不能指望模型自己遵守。
    """
    issues: list[dict] = []
    kept: list[dict] = []
    seen_ids: set[str] = set()
    unknown_ids = {item.get('step_id') for item in unknown_steps or []}
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get('step_id') or '')
        tool = str(step.get('tool') or '')
        if not step_id:
            issues.append({'code': 'missing_step_id',
                           'detail': '新步骤必须有 step_id'})
            continue
        if step_id in done_steps:
            issues.append({'code': 'overwrites_executed_step',
                           'detail': f'{step_id} 已经执行过，不能重写'})
            continue
        if step_id in seen_ids:
            issues.append({'code': 'duplicate_step_id',
                           'detail': f'{step_id} 在修订计划里重复'})
            continue
        if known_tools is not None and tool not in known_tools:
            issues.append({'code': 'unknown_tool',
                           'detail': f'{tool} 不在可用工具清单里'})
            continue
        depends = [str(dep) for dep in (step.get('depends_on') or [])]
        if any(dep in unknown_ids for dep in depends):
            issues.append({'code': 'depends_on_unknown_action',
                           'detail': f'{step_id} 依赖了结果未定的步骤'})
            continue
        seen_ids.add(step_id)
        kept.append(step)
    return kept, issues


