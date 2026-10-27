"""给用户看的自然语言回执与停止提示。

CLI、会话层、FastAPI 任务中枢三处共用，避免文案与副作用判断各写一份。

回执**按 outcome 与通用状态分支**，不按业务状态名：以前这里是
`if status == 'answered' / 'local_ready' / 'deploy_needs_check'`，每加一类
能力就得在文案里加一个分支，而且这些词还同时被塞进了任务状态机。
现在"这一轮产出了什么"由 `app/outcome.py` 记录，文案只负责把它翻译成人话。
"""
import json

from app import outcome as outcome_module
from app.config import settings


def reply_text(state: dict) -> str:
    """任务结束后的自然语言回执。"""
    state = state or {}
    status = outcome_module.normalize_status(state.get('status'))
    outcome = state.get('outcome') or outcome_module.build(state)
    deployment = state.get('deployment') or {}
    kind = outcome.get('kind')

    if status == 'succeeded':
        if kind == 'deployment':
            if deployment.get('public_url'):
                return f'报告已生成并部署完成，访问地址：{deployment["public_url"]}'
            return '本次执行完成，但部署回执里没有可访问地址，请核对站点列表。'
        if kind == 'answer':
            # 直接回答：answer 本身就是给用户看的完整回执，不再包装
            return state.get('answer') or '（模型没有返回内容）'
        if kind in ('artifact', 'files'):
            return _artifact_reply(state)
        if kind == 'compensation':
            done = [item for item in (state.get('compensations') or [])
                    if item.get('status') == 'succeeded']
            return f'已撤销此前完成的 {len(done)} 个有副作用的步骤。'
        return '本次执行完成，但没有产出内容。'

    if status == 'needs_reconcile':
        # 写请求可能真的成功了，只是回执看不懂：不能报失败，也不能说完成。
        # 文案不能写死"报告/部署"：这里也会被画布、表格、工单这类非发布类写操作
        # 命中，Hardcode 一句"部署结果待确认"会让用户以为系统跑错了流程。
        url = deployment.get('public_url')
        if url:
            return (f'报告已生成，站点地址看起来是 {url}，但服务端回执没能确认，'
                    f'请核对站点列表。')
        reason = state.get('failure') or '有写操作的结果没能确认'
        written = [item for item in (outcome.get('effects') or [])
                   if item.get('effect') == 'write' and item.get('verified')]
        if written:
            return (f'本次停在了这一步：{reason}。'
                    f'前面 {len(written)} 个写操作已经落到服务端并核实过，'
                    f'我没有撤销它们；请先核对再决定是补做还是回退。')
        return f'本次执行结果没能确认：{reason}。'

    if status == 'partially_succeeded':
        # 不能只说"部分完成"：用户要知道哪些副作用没收回来
        reason = state.get('failure') or '部分动作未能撤销'
        return f'本次执行只完成了一部分：{reason}'

    if status == 'failed':
        reason = state.get('failure')
        if reason:
            return f'本次未完成：{reason}。'
        return '本次没有产出可用结果。'

    if status in ('waiting_input', 'waiting_approval'):
        # 计划没过校验就停下来等人补信息——不能说"任务结束"了事
        reason = state.get('failure') or '需要补充信息后才能继续'
        clarified = str(state.get('clarification') or '').strip()
        if clarified and clarified == str(reason).strip():
            # 问题已经作为单独一条消息弹过了（界面上的"需要你补充一句"），
            # 这里再复述一遍就是同一句话刷两屏。只说"停在哪、等你什么"。
            return '先停在这里：等你回上面那条，我就把细节并进同一件事继续。'
        return f'先停在这里：{reason}'

    if status == 'cancelled':
        return '已停止当前任务。已生成的本地产物仍然保留。'
    if status == 'interrupted':
        return '执行中断了（执行者与任务的连接断开）。可重新发起一次。'
    return f'任务结束，状态：{status}'


def _artifact_reply(state: dict) -> str:
    """产出了东西（页面或文件）时的回执。"""
    blocked = ((state.get('plan') or {}).get('blocked') or [])
    note = f'！未完成的部分：{blocked[0]}。' if blocked else ''
    if (state.get('artifact') or {}).get('html_path'):
        what = '资料页面' if (state.get('artifact') or {}).get('kind') == 'sources' \
            else '页面'
        tail = f'想发布就说一句「部署到服务器」。任务 ID：{state.get("task_id")}'
        return f'{what}已生成，保存在本地。{note}{tail}'
    return (f'已产出 {_artifact_name(state) or "工作文件"}。{note}'
            f'任务 ID：{state.get("task_id")}')


def _artifact_name(state: dict) -> str:
    """产物文件名（给回执用，没有就空串）。"""
    artifact = state.get('artifact') or {}
    path = artifact.get('path') or artifact.get('html_path') or ''
    return str(path).rsplit('/', 1)[-1] if path else ''

def cancel_note(artifact_id: str | None) -> str:
    """停止后提示已产生的副作用——取消不会撤销已发布的站点。"""
    notes = ['已停止。已生成的本地产物仍然保留。']
    if not artifact_id:
        return ' '.join(notes)
    folder = settings.artifact_dir / artifact_id
    deployment_file = folder / 'deployment.json'
    if deployment_file.exists():
        try:
            url = json.loads(deployment_file.read_text(encoding='utf-8')).get('public_url')
        except (OSError, ValueError):
            url = None
        if url:
            notes.append(f'注意：停止前部署已完成，站点仍在 {url}，停止不会撤销已发布的内容。')
        return ' '.join(notes)
    state = {}
    state_file = folder / 'state.json'
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
    steps = (state.get('plan') or {}).get('steps') or []
    planned_deploy = any(step.get('effect') in outcome_module.WRITE_EFFECTS
                         for step in steps)
    if settings.deploy_enabled and planned_deploy:
        notes.append('注意：本轮计划包含写操作，停止时机可能落在其中一步，'
                     '服务端结果未知，建议核对站点列表。')
    return ' '.join(notes)
