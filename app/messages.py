"""给用户看的自然语言回执与停止提示。

CLI、Streamlit 会话层、FastAPI 任务中枢三处共用，避免文案与副作用判断各写一份。
"""
import json

from app.config import settings


def reply_text(state: dict) -> str:
    """任务结束后的自然语言回执。"""
    status = state.get('status')
    deployment = state.get('deployment') or {}
    if deployment.get('public_url'):
        return f'报告已生成并部署完成，访问地址：{deployment["public_url"]}'
    if status == 'local_ready':
        return ('报告已生成，保存在本地。需要发布可以点「部署到服务器」，'
                f'或直接说"部署到服务器"。任务 ID：{state.get("task_id")}')
    if status == 'failed':
        return '资料不足或产物校验未通过，本次没有生成报告。可换个更具体的说法再试。'
    if status == 'deploy_needs_check':
        return '报告已生成，但部署结果待确认，请核对服务端站点列表。'
    return f'任务结束，状态：{status}'


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
    plan = state.get('plan') or {}
    if settings.deploy_enabled and plan.get('auto_deploy'):
        notes.append('注意：本次要求自动部署，停止时机可能落在部署阶段，'
                     '服务端结果未知，建议核对站点列表。')
    return ' '.join(notes)
