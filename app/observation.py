"""工具结果驱动的观察层：**程序先看事实，模型只在需要判断时才被调用**。

为什么需要它：现有执行器只会"计划里有什么就跑什么"。于是模型必须在第一份计划里
就把还没发生的事猜出来——素材 ID、片段区间、要读哪个文件。真实结果是"先列清单，
再挑片段"，猜出来的 ID 全是假的，跑到一半才发现，只能整轮失败。

这个模块只做三件事，都是纯逻辑（不碰数据库、不调模型）：

1. `build_result()`：把一次工具调用归一成 `ToolResult` 合同（执行状态、阶段、
   完整结果引用、有界摘要、证据引用、是否全量）；
2. `collect_observation()` + `route_after_step()`：程序判断"这一步之后该往哪走"
   ——版本变了去修订、控制请求走既有路径、结果未定先核实、参数能直接绑定就继续、
   片段结束才叫模型；
3. `completion_state()`：把"任务算不算做完"拆成可程序核对的条件，
   缺证据就如实说"未检查"，绝不因为文件存在就宣称画面合格。

三条纪律写在这里，避免以后被"看起来更省事"的做法改回去：

- **不用工具名关键词猜要不要观察**。片段边界由候选计划自己声明（`after_segment`），
  执行器只按声明统一处理。
- **程序发现条件，不猜业务替代方案**。清单为空时程序只报告"没有可用素材"，
  补查还是问用户由模型决定；绝不静默凑三个不存在的 ID。
- **模型没读到的东西不能算看过**。元数据（文件名、时长、标签）不等于内容；
  没有 inspect/review 能力时必须如实说"无法判断画面是否合适"。
"""
import hashlib
import json
import logging
import re

logger = logging.getLogger(__name__)

# 摘要里"保留结构"的深度与宽度：嵌套数组只留前 N 项，但**长度一定要给出**，
# 否则模型会把"取了前 3 项"当成"一共只有 3 项"。
SUMMARY_ITEMS = 3
SUMMARY_DEPTH = 3
SUMMARY_CHARS = 2400
# 单条结果的文本摘要上限（字符）。超过的走 data_ref 受控读取。
SUMMARY_TEXT_CHARS = 600

# 执行阶段：工具支持时才有，缺失一律当**未知**，不能由模型补成事实。
PHASES = ('accepted', 'processing', 'completed', 'materialized')

# 观察路由（与文档 §4 第二步的路由表一一对应）
ROUTE_STEP = 'run_step'                 # 继续下一步（确定参数，无需模型）
ROUTE_REVISE = 'revise_plan'            # 用户要求变了
ROUTE_DECIDE = 'decide_next'            # 片段结束/需要语义判断
ROUTE_VERIFY = 'verify_completion'      # 全部片段结束，先程序核对
ROUTE_CONTROL = 'control'              # 暂停/取消/失租：走既有控制路径
ROUTE_RECONCILE = 'needs_reconcile'     # 写操作结果未定
ROUTE_WAIT = 'wait_async'              # 外部任务已受理未完成：程序查询，不调模型
ROUTES = (ROUTE_STEP, ROUTE_REVISE, ROUTE_DECIDE, ROUTE_VERIFY,
          ROUTE_CONTROL, ROUTE_RECONCILE, ROUTE_WAIT)

# 完成条件里"必须由模型看图/看文才能下结论"的类型：程序核对不了，
# 只能标"未检查"并要求真实内容读取。
MODEL_ONLY_CRITERIA = ('content_quality', 'semantic_match', 'subjective')


def _text(value, limit: int = SUMMARY_TEXT_CHARS) -> str:
    text = ' '.join(str(value or '').split())
    return text if len(text) <= limit else text[:limit] + '…'


def summarize(payload, *, items: int = SUMMARY_ITEMS, depth: int = SUMMARY_DEPTH):
    """有界摘要：**保留结构**而不是按字符截断。

    以前 `tool_results_text()` 直接切字符，长清单只剩前几百个字，模型看到的是
    "素材列表：a、b、c" 而不知道还有 40 个；它就会以为清单只有三条。
    这里显式给出长度、分页标志与每一项的关键字段。
    """
    if isinstance(payload, list):
        head = [summarize(item, items=items, depth=depth - 1)
                for item in payload[:items]]
        return {'items': head, 'total_items': len(payload),
                'truncated': len(payload) > items}
    if isinstance(payload, dict):
        if depth <= 0:
            return {'keys': sorted(str(k) for k in payload)[:12],
                    'total_keys': len(payload)}
        out = {}
        for key, value in payload.items():
            out[str(key)] = summarize(value, items=items, depth=depth - 1)
        return out
    if isinstance(payload, str):
        return _text(payload)
    return payload


def pagination(payload) -> dict:
    """识别常见分页信号（不同服务端写法都收）。

    只报告"数据可能不全"，不推测还差多少：宁可让模型补查一次，
    也不要让它拿着前 20 条宣称"全部都比较过了"。
    """
    if not isinstance(payload, dict):
        return {}
    keys = {str(k).lower(): v for k, v in payload.items()}
    out = {}
    for name in ('total', 'total_count', 'count', 'total_items'):
        if isinstance(keys.get(name), int):
            out['declared_total'] = keys[name]
    for name in ('next_cursor', 'next_page_token', 'next', 'cursor'):
        value = keys.get(name)
        if value:
            out['next_read_ref'] = str(value)
    for name in ('has_more', 'has_next', 'more'):
        if isinstance(keys.get(name), bool):
            out['has_more'] = keys[name]
    truncated = keys.get('truncated')
    if isinstance(truncated, bool):
        out['truncated'] = truncated
    return out


def _count_items(payload) -> int | None:
    """尽量数出这批结果到底有多少条（数不出来就返回 None，别猜）。"""
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    best = None
    for value in payload.values():
        if isinstance(value, list):
            best = max(best or 0, len(value))
    return best


def build_result(*, step_id: str, tool_key: str, action_id: str = '',
                 attempt_id: str = '', payload=None, execution_status: str = 'succeeded',
                 summary: dict | None = None, artifact_refs=None,
                 external_receipt_refs=None, data_ref: str = '',
                 evidence_refs=None, phase: str = '', error: dict | None = None,
                 complete: bool | None = None) -> dict:
    """平台归一的 `ToolResult` 合同（文档 §4 第一步）。

    不要求所有 MCP 服务端原生返回同一 schema：缺的字段留空/留未知，
    **不能由模型补成事实**。`data_ref` 指向完整原始结果的授权引用，
    模型只看摘要，需要细节时按引用受控读取。
    """
    page = pagination(payload)
    result = {
        'step_id': step_id,
        'tool_key': tool_key,
        'action_id': action_id,
        'attempt_id': attempt_id,
        'execution_status': execution_status,
        'phase': phase or 'completed',
        'summary': summary if summary is not None else summarize(payload),
        'artifact_refs': [ref for ref in (artifact_refs or []) if ref],
        'external_receipt_refs': [ref for ref in (external_receipt_refs or []) if ref],
        'evidence_refs': [ref for ref in (evidence_refs or []) if ref],
        'data_ref': data_ref,
        'pagination': page,
        'complete': _is_complete(page, payload) if complete is None else bool(complete),
        'error': error or {},
    }
    if phase and phase not in PHASES:
        result['phase_note'] = f'未知阶段 {phase}，按未完成处理'
        result['complete'] = False
    return result


def _is_complete(page: dict, payload) -> bool:
    """这批结果是不是"全部"。

    以前只要回执里有 `total` 就判为不完整，于是**每一条**带总数的返回都被当成
    分页结果，观察节点就永远在叫模型补查。正确做法是拿总数和真实条数比：
    相等就是拿到了全部；比不出来才保守地说"不完整"。
    """
    if page.get('has_more') or page.get('next_read_ref') or page.get('truncated'):
        return False
    declared = page.get('declared_total')
    if declared is None:
        return True
    counted = _count_items(payload)
    if counted is None:
        return False
    return int(counted) >= int(declared)


def evidence_of(result: dict) -> list[str]:
    """结果里可以直接当证据用的引用（产物 / 外部回执）。"""
    refs = list(result.get('artifact_refs') or [])
    refs.extend(result.get('external_receipt_refs') or [])
    refs.extend(result.get('evidence_refs') or [])
    if result.get('data_ref'):
        refs.append(result['data_ref'])
    return refs


def is_write(effect: str | None) -> bool:
    return (effect or '').lower() in ('write', 'destructive', 'external')


def collect_observation(*, state: dict, plan: dict | None = None,
                        step_index: int = 0, actions=None,
                        control: dict | None = None,
                        intent_version: int = 0,
                        pending_inputs: int = 0) -> dict:
    """采集"这一步之后的事实"（纯函数，不调模型）。

    返回的 `signals` 是**条件**不是结论：能不能继续由 `route_after_step()` 决定。
    """
    plan = plan or {}
    steps = plan.get('steps') or []
    actions = list(actions or [])
    control = control or {}
    results = [item for item in (state.get('tool_results') or []) if isinstance(item, dict)]
    unknown = [item for item in actions
               if str(item.get('status')) in ('in_flight', 'unknown')]
    waiting = [item for item in results
               if str(item.get('phase') or '') in ('accepted', 'processing')
               or (item.get('execution_status') == 'accepted')]
    empty_lists = [item for item in results if _looks_empty(item)]
    incomplete = [item for item in results if item.get('complete') is False]
    plan_intent = plan.get('intent_version')
    signals = {
        'intent_changed': bool(plan_intent) and int(intent_version or 0) != int(plan_intent),
        'pending_inputs': int(pending_inputs or 0),
        'paused': bool(control.get('pause_requested')),
        'cancelled': bool(control.get('cancel_requested_at')),
        'unknown_actions': [{'step_id': item.get('step_id'),
                             'tool_key': item.get('tool_key'),
                             'effect': item.get('effect')} for item in unknown],
        'unknown_write': any(is_write(item.get('effect')) for item in unknown),
        'waiting_async': waiting,
        'empty_results': empty_lists,
        'incomplete_results': incomplete,
        'segment_end': bool(steps) and int(step_index or 0) >= len(steps),
        'steps_left': max(0, len(steps) - int(step_index or 0)),
        'after_segment': str(plan.get('after_segment') or 'verify'),
        'observation_goal': str(plan.get('observation_goal') or ''),
        # 失败分类：只有"确认未执行"的本地错误允许有限观察修复（文档 §10）
        'repairable_errors': [item for item in (state.get('errors') or [])
                              if str((item or {}).get('certainty') or '') == 'not_executed'],
        'results': results,
        'evidence': [ref for item in results for ref in evidence_of(item)],
    }
    # 被动发现的新事实（文档 §4 第三步）：只报告，不猜替代方案
    reasons = []
    if signals['empty_results']:
        reasons.append('empty_result')
    if signals['incomplete_results']:
        reasons.append('result_incomplete')
    if signals['repairable_errors']:
        reasons.append('repairable_error')
    signals['reasons'] = reasons
    return signals


def _looks_empty(result: dict) -> bool:
    summary = result.get('summary')
    if isinstance(summary, dict):
        if summary.get('total_items') == 0 or summary.get('items') == []:
            return True
        for key in ('items', 'results', 'data'):
            value = summary.get(key)
            if isinstance(value, dict) and value.get('total_items') == 0:
                return True
    if isinstance(summary, list) and not summary:
        return True
    return False


def route_after_step(signals: dict) -> tuple[str, str]:
    """程序路由（文档 §4 第二步的路由表）。返回 `(route, 原因)`。

    顺序是有意的：**控制与未知优先**——它们代表"这一步不许再往下走"，
    不能被"片段结束该问模型了"盖过去。
    """
    if signals.get('cancelled') or signals.get('paused'):
        return ROUTE_CONTROL, '控制请求优先'
    if signals.get('intent_changed'):
        return ROUTE_REVISE, '用户要求已更新'
    if signals.get('unknown_write'):
        return ROUTE_RECONCILE, '写操作结果未定，先核实'
    if signals.get('waiting_async'):
        return ROUTE_WAIT, '外部任务已受理未完成，等原任务'
    if signals.get('repairable_errors') and signals.get('segment_end'):
        return ROUTE_DECIDE, '本地错误可有限修复'
    if not signals.get('segment_end'):
        return ROUTE_STEP, '片段内还有确定步骤'
    if signals.get('after_segment') == 'observe':
        return ROUTE_DECIDE, '片段声明的观察点到了'
    if signals.get('reasons'):
        return ROUTE_DECIDE, '结果里有需要判断的新事实'
    return ROUTE_VERIFY, '片段结束，先程序核对完成条件'


# ---------- 完成条件核对（文档 §11） ----------

def criteria_of(plan: dict | None, state: dict | None = None) -> list[dict]:
    """取本任务的可验证条件：计划里声明的 + 由产出自动推导的。

    自动推导的那部分是"确定性条件"，来自真实回执（产物路径、部署地址），
    不是模型的自述。
    """
    criteria = [dict(item) for item in ((plan or {}).get('completion_criteria') or [])
                if isinstance(item, dict)]
    state = state or {}
    if not criteria:
        artifact = state.get('artifact') or {}
        path = artifact.get('path') or artifact.get('html_path')
        if path:
            criteria.append({'kind': 'artifact_exists', 'target': path,
                             'detail': '本轮声明的产物必须真实存在'})
        deployment = state.get('deployment') or {}
        if deployment.get('status') == 'success':
            criteria.append({'kind': 'deployment_receipt',
                             'target': deployment.get('public_url')
                             or deployment.get('site_id') or '',
                             'detail': '部署必须有成功回执与可访问地址'})
    return criteria


def completion_state(criteria: list[dict], *, state: dict, results=None,
                     files_exist=None) -> dict:
    """逐条核对完成条件，返回 `verified / unmet / unchecked`。

    `files_exist(path) -> bool` 由调用方注入：观察层不做 IO，方便单测。
    """
    verified, unmet, unchecked = [], [], []
    results = list(results or [])
    for index, criterion in enumerate(criteria):
        kind = str(criterion.get('kind') or '')
        target = str(criterion.get('target') or '')
        entry = {'index': index, 'kind': kind, 'target': target,
                 'detail': str(criterion.get('detail') or '')}
        if kind in MODEL_ONLY_CRITERIA or criterion.get('requires_model'):
            entry['reason'] = '需要真实内容读取与模型评价'
            unchecked.append(entry)
            continue
        if kind == 'artifact_exists':
            # `target` 常常是**人话描述**（"HTML 页面"）而不是路径。以它为准去查
            # 文件，就会把"其实已经产出了"判成未满足——比不检查更糟。
            # 所以：路径形态才当路径用，否则回到本轮声明的产物引用。
            artifact = state.get('artifact') or {}
            declared = artifact.get('path') or artifact.get('html_path') or ''
            candidates = [declared] if declared else []
            if looks_like_path(target):
                candidates.append(target)
            if not candidates:
                entry['reason'] = '没有声明产物'
                unmet.append(entry)
            elif files_exist is not None and not any(files_exist(p_) for p_ in candidates):
                entry['reason'] = '产物路径不存在'
                unmet.append(entry)
            else:
                entry['evidence'] = [f'artifact:{p_}' for p_ in candidates][:3]
                verified.append(entry)
            continue
        if kind == 'file_written':
            written = _written_files(state)
            if target and target in written:
                entry['evidence'] = [f'file:{target}']
                verified.append(entry)
            elif written:
                entry['evidence'] = [f'file:{name}' for name in written][:5]
                verified.append(entry)
            else:
                entry['reason'] = '没有文件产出记录'
                unmet.append(entry)
            continue
        if kind == 'deployment_receipt':
            deployment = state.get('deployment') or {}
            if deployment.get('status') == 'success' and (
                    deployment.get('public_url') or deployment.get('site_id')):
                entry['evidence'] = [f'deployment:{deployment.get("public_url") or deployment.get("site_id")}']
                verified.append(entry)
            elif deployment:
                entry['reason'] = '部署回执未确认成功'
                unmet.append(entry)
            else:
                entry['reason'] = '没有部署回执'
                unmet.append(entry)
            continue
        if kind == 'answer_present':
            if state.get('answer'):
                entry['evidence'] = ['answer']
                verified.append(entry)
            else:
                entry['reason'] = '没有回答内容'
                unmet.append(entry)
            continue
        if kind == 'tool_succeeded':
            # 模型写的通常是工具名（`web_search`）或 step_id，而结果里记的是
            # tool_key（`local:web:search`）。三种写法都要认，否则"明明成功了"
            # 也会被判成未满足——那比不检查更糟：它会把好任务判死。
            def _matches(item: dict) -> bool:
                if item.get('execution_status') != 'succeeded':
                    return False
                keys = {str(item.get('tool_key') or ''), str(item.get('step_id') or '')}
                keys.update(str(item.get('tool_key') or '').split(':')[-1:])
                return bool(target) and target in keys
            matched = [item for item in results if _matches(item)]
            if matched:
                entry['evidence'] = evidence_of(matched[0]) or [
                    f'action:{matched[0].get("action_id")}']
                verified.append(entry)
            else:
                entry['reason'] = f'没有成功的 {target} 调用'
                unmet.append(entry)
            continue
        entry['reason'] = f'未知条件类型 {kind}，按未检查处理'
        unchecked.append(entry)
    return {'verified': verified, 'unmet': unmet, 'unchecked': unchecked,
            'ok': not unmet, 'complete_evidence': not unmet and not unchecked}


def looks_like_path(value: str) -> bool:
    """这段文本是在指一个文件路径，还是在描述一个东西？

    完成条件是模型写的自然语言，`target` 可能是 `"HTML 页面"`。把它当路径去查
    文件只会得到"不存在"，于是明明做完了也报未满足。
    """
    text = str(value or '').strip()
    if not text or ' ' in text and '/' not in text:
        return False
    if '/' in text or text.startswith(('.', '~')):
        return True
    return bool(re.search(r'\.[A-Za-z0-9]{2,5}$', text))


def _written_files(state: dict) -> list[str]:
    names = []
    for output in (state.get('step_outputs') or {}).values():
        if not isinstance(output, dict):
            continue
        written = output.get('written') or {}
        if isinstance(written, dict) and written.get('relative'):
            names.append(str(written['relative']))
    return names


# ---------- 无进展指纹（文档 §12） ----------

def progress_pair(*, tool_key: str, args: dict | None, result: dict | None) -> str:
    """一次调用的"有没有新东西"签名：**同样的查询 + 同样的结果**才算没进展。

    为什么不把结果水位放进指纹：每次调用都会推进水位，那样任何两次调用都"不同"，
    保护形同虚设。真正要拦的是"同一个查询、拿回同一份结果、还继续问"。
    """
    body = json.dumps({'tool': tool_key or '',
                       'args': args or {},
                       'result': (result or {}).get('summary')},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]


def is_repeated(pair: str, seen) -> bool:
    return bool(pair) and pair in list(seen or [])


def criteria_text(criteria_state: dict) -> str:
    """给模型看的完成条件现状（简短，供判定 finish 时引用）。"""
    lines = []
    for label, items in (('已验证', criteria_state.get('verified')),
                         ('未满足', criteria_state.get('unmet')),
                         ('未检查', criteria_state.get('unchecked'))):
        for item in items or []:
            extra = item.get('reason') or ', '.join(item.get('evidence') or [])
            lines.append(f'- [{label}] {item.get("kind")} {item.get("target")}'
                         + (f'（{extra}）' if extra else ''))
    return '\n'.join(lines) or '- 本任务没有声明完成条件'
