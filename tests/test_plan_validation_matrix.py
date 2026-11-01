"""校验面覆盖：规划阶段放行的计划，全成功执行后核对也必须能通过。

这条断言本可以一次抓住前面三个 bug——它们都是同一个形状：
**规划阶段放行 → 执行成功 → 核对阶段判未完成**（平台对象已经改好，用户却收到
"本次未完成"）。

这里不测具体文案，而是枚举「计划形状 × 完成条件 × 结果落点」：

- 计划形状：远端写 / 远端读 / 远端删除 / 远端部署 / 本地检索 / 资料转 HTML /
  纯作答，以及它们的常见组合；
- 完成条件：五种程序可核对的条件（含空目标、指向不存在步骤这两种坏写法）+
  模型评价类 + 未知类型；
- 结果落点：platform_change / platform_state / chat / 空。

对每个组合：能通过 `validate_plan` + `check_completion_criteria` +
`check_result_locus` 的，就假设每一步都成功、构造真实回执，再跑
`observation.completion_state` —— 必须全部满足（只有"需要模型评价"的条件允许
落在未检查里）。

新增校验时必须让这张表继续为空：不是改断言，而是让新规则在规划阶段就阻断或
纠正。反过来，被规则拦下的组合也不许是"其实能跑通"的——误杀会在这里露头。
"""
import itertools
import unittest

from webpage_agent.agent import nodes
from webpage_agent.observe import observation
from webpage_agent.capabilities.registry import Tool


async def _noop(args, state):
    return {}


def make(name, key, effect='read', *, local_output=False, file_output=False,
         deployment_output=False):
    return Tool(name, name, name, {'type': 'object'}, _noop, tool_key=key,
                effect=effect, local_output=local_output,
                file_output=file_output, deployment_output=deployment_output,
                server_id=key.split(':')[1] if key.startswith('mcp:') else '')


READ = make('r_read', 'mcp:svc:get', 'read')
WRITE = make('r_write', 'mcp:svc:create', 'write')
DELETE = make('r_drop', 'mcp:svc:delete', 'destructive')
DEPLOY = make('deploy_site', 'mcp:pub:deploy_site', 'write',
              deployment_output=True)
SEARCH = make('web_search', 'local:web:search', 'read')
EXPORT = make('render_sources_html', 'local:export:sources_html', 'write',
              local_output=True)
ANSWER = make('answer_directly', 'local:answers:direct', 'read')
CATALOG = [READ, WRITE, DELETE, DEPLOY, SEARCH, EXPORT, ANSWER]


SHAPES = {
    '远端写': [WRITE],
    '远端读+写': [READ, WRITE],
    '写+回读': [WRITE, READ],
    '写+作答': [WRITE, ANSWER],
    '写+回读+作答': [WRITE, READ, ANSWER],
    '远端读': [READ],
    '远端删除': [DELETE],
    '远端部署': [DEPLOY],
    '本地检索': [SEARCH],
    '检索+转HTML': [SEARCH, EXPORT],
    '仅转HTML': [EXPORT],
    '仅作答': [ANSWER],
}

CRITERIA = {
    '无条件': [],
    '证据=写步骤': [{'kind': 'tool_succeeded', 'target': 'WRITE_STEP'}],
    '证据=工具名': [{'kind': 'tool_succeeded', 'target': 'r_write'}],
    '证据=不存在': [{'kind': 'tool_succeeded', 'target': 's99'}],
    '证据=空目标': [{'kind': 'tool_succeeded', 'target': ''}],
    '产物存在': [{'kind': 'artifact_exists', 'target': '图片节点'}],
    '文件写入': [{'kind': 'file_written', 'target': 'a.md'}],
    '部署回执': [{'kind': 'deployment_receipt', 'target': 'https://x'}],
    '有回答': [{'kind': 'answer_present', 'target': ''}],
    '模型评价': [{'kind': 'content_quality', 'target': '写得好'}],
    '未知条件': [{'kind': 'whatever', 'target': ''}],
    '证据+产物': [{'kind': 'tool_succeeded', 'target': 'WRITE_STEP'},
                  {'kind': 'artifact_exists', 'target': '图片节点'}],
}

LOCI = ['platform_change', 'platform_state', 'chat', '']
MODEL_ONLY = {'content_quality', 'semantic_match'}


def build(shape, criteria, locus):
    steps, writes = [], []
    for index, spec in enumerate(SHAPES[shape], 1):
        steps.append({'step_id': f's{index}', 'tool': spec.name, 'args': {},
                      'depends_on': [f's{index - 1}'] if index > 1 else []})
        if spec.effect in ('write', 'destructive'):
            writes.append(f's{index}')
    resolved = []
    for item in criteria:
        item = dict(item)
        if item.get('target') == 'WRITE_STEP':
            if not writes:
                continue
            item['target'] = writes[-1]
        resolved.append(item)
    return steps, resolved


def simulate(steps):
    """假设每一步都成功：按真实回执的形状拼状态。"""
    specs = {spec.name: spec for spec in CATALOG}
    outputs, results = {}, []
    state: dict = {'step_outputs': outputs}
    for step in steps:
        spec = specs[step['tool']]
        outputs[step['step_id']] = {'__status__': 'succeeded'}
        results.append({'step_id': step['step_id'], 'tool_key': spec.tool_key,
                        'tool_name': spec.name, 'execution_status': 'succeeded',
                        'action_id': f'a_{step["step_id"]}'})
        if spec.local_output:
            state['artifact'] = {'path': '/tmp/out.html',
                                 'html_path': '/tmp/out.html'}
        if spec.deployment_output:
            state['deployment'] = {'status': 'success',
                                   'public_url': 'https://x.test'}
        if spec.tool_key == 'local:answers:direct':
            state['answer'] = '好了'
    return state, results


class ValidationSurfaceTests(unittest.TestCase):
    def test_every_accepted_plan_can_actually_pass_verification(self):
        holes = []
        for shape, (cname, criteria), locus in itertools.product(
                SHAPES, CRITERIA.items(), LOCI):
            steps, wanted = build(shape, criteria, locus)
            normalized, issues = nodes.validate_plan(steps, CATALOG)
            kept, criteria_issues = nodes.check_completion_criteria(
                wanted, normalized, CATALOG, locus)
            issues = issues + criteria_issues + nodes.check_result_locus(
                locus, normalized, CATALOG, kept, after_segment='verify')
            if [i for i in issues if i.get('severity') != 'warning']:
                continue
            state, results = simulate(normalized)
            plan = {'completion_criteria': kept, 'result_locus': locus,
                    'steps': normalized}
            check = observation.completion_state(
                observation.criteria_of(plan, state), state=state,
                results=results, files_exist=lambda path: True)
            if check['unmet']:
                holes.append((shape, cname, locus, [
                    f'{item["kind"]}:{item.get("reason")}'
                    for item in check['unmet']]))
            stray = [item['kind'] for item in check['unchecked']
                     if item['kind'] not in MODEL_ONLY and item['kind'] != 'whatever']
            if stray:
                holes.append((shape, cname, locus, [
                    f'{kind}: unexpectedly unchecked' for kind in stray]))
        self.assertEqual(holes, [], f'校验放行但核对必然失败：{holes[:5]}')

    def test_blocked_plans_really_are_broken(self):
        """反向：被完成条件规则拦下的，不许是"其实能跑通"的计划。"""
        blocked_but_fine = []
        for shape, (cname, criteria), locus in itertools.product(
                SHAPES, CRITERIA.items(), LOCI):
            steps, wanted = build(shape, criteria, locus)
            normalized, _ = nodes.validate_plan(steps, CATALOG)
            _, criteria_issues = nodes.check_completion_criteria(
                wanted, normalized, CATALOG, locus)
            if not [i for i in criteria_issues if i.get('severity') != 'warning']:
                continue
            state, results = simulate(normalized)
            plan = {'completion_criteria': wanted, 'result_locus': locus,
                    'steps': normalized}
            check = observation.completion_state(
                observation.criteria_of(plan, state), state=state,
                results=results, files_exist=lambda path: True)
            if check['ok']:
                blocked_but_fine.append((shape, cname, locus))
        self.assertEqual(blocked_but_fine, [], f'拦了本来能跑的计划：{blocked_but_fine[:5]}')


if __name__ == '__main__':
    unittest.main()
