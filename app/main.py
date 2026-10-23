import argparse
import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path
import logging

from app.config import settings, check_runtime_keys
from app.events import STAGE_LABELS  # noqa: F401  (对外仍从 app.main 可用)
from app import execution_control
from app import ledger as ledger_module
from app import outcome as outcome_module
from app.capabilities.publishing import deploy_once
from app.graph import build_graph
from app.files import save_json

logger = logging.getLogger(__name__)


async def deploy_task(task_id: str) -> dict:
    """部署已经生成好的 HTML：不重新搜索、不调用模型。

    只给 `scripts.retry_deploy` 这种"结果未定后人工重放一次"的补救场景用。
    界面上的部署按钮走执行器（`Hub.deploy_artifact`），有账本、事件与取消语义；
    这里刻意保持最小：校验开关与哈希，然后调一次部署工具。
    """
    if not settings.deploy_enabled:
        raise RuntimeError('DEPLOY_ENABLED=false，部署已关闭；请在 .env 打开后重启进程')
    if not task_id.isalnum():
        raise ValueError('task_id 只能包含字母数字')
    folder = settings.artifact_dir / task_id
    artifact = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    actual = hashlib.sha256(Path(artifact['html_path']).read_bytes()).hexdigest()
    if actual != artifact['sha256']:
        raise ValueError('HTML 已修改，请重新生成 manifest 后再部署')
    return await deploy_once(artifact)


async def run_task(text: str, progress=None, on_event=None, history=None,
                  on_start=None, prior=None, memory=None, ledger=None,
                  run_id=None, resume_plan=None):
    """跑一次任务。

    progress(node, status, elapsed)  节点级进度
    on_event(event)                  节点内部的细粒度事件（搜索了什么、命中多少），可以是协程
    history                          多轮对话历史，供解析阶段还原指代型追加要求
    on_start(task_id)                任务创建后立刻回调（可以是协程），便于外部记录任务 ID
    """
    check_runtime_keys()
    if not text.strip():
        raise ValueError('请输入主题或 URL')
    if len(text) > 10000:
        raise ValueError('首版输入限制为 10000 字符')
    task_id = uuid.uuid4().hex
    folder = settings.artifact_dir / task_id
    folder.mkdir(parents=True, exist_ok=True)
    if on_start:
        # 早点把 task_id 交出去，外部才能在任务被取消后定位已生成的产物。
        await on_start(task_id)
    resumed = resume_plan or {}
    state = {
        'task_id': task_id, 'user_input': text,
        # 原目标：运行中补充要求时，"最初要做什么"不能被新要求顶掉——
        # 修订计划要同时看到原目标和按序补充（文档 §5 第八步）。
        'original_goal': text,
        'documents': [], 'mcp_calls': [], 'errors': [], 'usage': [],
        'run_id': run_id or '', 'resume_plan': resumed,
        # 三个版本分开：intent_version 是"用户要求版本"，plan_version 是"计划版本"；
        # lease_epoch 不在这里（执行权凭证走 ExecutionContext，不进可落盘的状态）。
        'intent_version': int(resumed.get('intent_version') or 0),
        'plan_version': int(resumed.get('plan_version') or 0),
        'processed_intent_version': int(resumed.get('processed_intent_version') or 0),
        'pending_inputs': [], 'intent_changed': False, 'revision_count': 0,
        # 产物目录：persist_plan 节点提交快照时要记下它（恢复后仍能找到文件）
        'artifact_dir': str(folder),
        'step_index': 0, 'status': 'created',
        'timings': [], 'history': list(history or []),
        # 更早对话的压缩结果；context 装配时注入 system 与规划 payload
        'session_memory': dict(memory or {}),
        # 上一轮的产物与正文：让"部署到服务器""把上面内容整理成 HTML"这类
        # 承接上一轮的指令，在开头一句里也能落到具体的东西上。
        'prior_artifact': (prior or {}).get('artifact') or {},
        'prior_documents': (prior or {}).get('documents') or [],
    }
    started = time.perf_counter()
    marked = started
    ledger_token = ledger_module.bind(ledger)
    try:
        async with asyncio.timeout(settings.task_timeout):
            async for mode, chunk in build_graph().astream(
                state, config={'recursion_limit': settings.graph_recursion_limit},
                stream_mode=['updates', 'custom'],
            ):
                if mode == 'custom':
                    # 节点内部发出的动作事件，直接透传给调用方。
                    if on_event:
                        await on_event(chunk)
                    continue
                for name, update in chunk.items():
                    now = time.perf_counter()
                    if isinstance(update, dict):
                        state.update(update)
                    # 计划的落库**不再依赖这里**：图里有一个显式的
                    # `persist_plan` 节点，在第一处工具调用之前用 CAS 提交。
                    # 以前用 `plan_committed` 布尔值兜在这里，只适合"一次规划"——
                    # 一旦允许修订，第二份计划就可能没经过持久提交就开始执行。
                    # stream_mode='updates' 在该节点跑完后才产出事件，
                    # 所以相邻事件的时间差≈该节点耗时（含少量调度开销）。
                    # run_step 会自我循环多次，用 step_label 记具体是哪一步，
                    # 否则耗时表里会出现一排分不清的 "run_step"。
                    label = (update.get('step_label')
                             if isinstance(update, dict) else None) or name
                    state['timings'].append({
                        'stage': label,
                        'seconds': round(now - marked, 2),
                        'status': state.get('status'),
                    })
                    marked = now
                    if progress:
                        progress(label, state.get('status'), round(now - started, 2))
                    save_json(folder / 'state.json', state)
    except (execution_control.LeaseLost, execution_control.ControlStopped):
        # 控制流异常必须**原样穿过**这一层：把它们包成 RuntimeError 会让
        # "失租"和"暂停"退化成普通失败，中枢再也没法区分该由谁收尾（§4.3、§7.2）。
        raise
    except Exception as exc:
        state['status'] = 'failed'
        state['errors'].append({'stage': 'runtime', 'type': type(exc).__name__})
        # 不打印原始 SDK 异常字符串（可能夹带密钥或内网地址），默认只用类型名定位。
        # 但自写的、明确的提示（如单次尝试超时）会给异常挂 safe_message，这种可以透出。
        hint = getattr(exc, 'safe_message', None)
        detail = type(exc).__name__ + (f'（{hint}）' if hint else '')
        # 堆栈只进服务端日志：给用户的那句话里不放路径与内部实现。
        logger.exception('任务 %s 执行异常', task_id)
        raise RuntimeError(f'任务 {task_id} 失败：{detail}') from None
    finally:
        ledger_module.unbind(ledger_token)
        state['elapsed_seconds'] = round(time.perf_counter() - started, 2)
        # 终态与 outcome 在**一处**收口：
        # - 图内部只写通用状态（succeeded / failed / waiting_input …）；
        # - 中途退出（图没跑到收尾）留下的 planned/created 之类不是终态，
        #   按"到底产出了什么"补一个；
        # - outcome 是记录，展示标签由它在读取时派生（app/outcome.py）。
        status = outcome_module.normalize_status(state.get('status'))
        # 控制状态优先：**有部分产物也不能把 paused / cancelled / needs_reconcile
        # 重算成 succeeded**（文档 §7.3）。只有"中途退出、没有任何终态"时才按产出补。
        if status not in outcome_module.TERMINAL_RUN_STATUSES:
            status = outcome_module.final_status(state)
        elif status in {'paused', 'cancelled', 'needs_reconcile', 'interrupted'}:
            status = status
        state['status'] = outcome_module.check_run_status(status)
        state['outcome'] = outcome_module.build(state)
        save_json(folder / 'state.json', state)
    return state


def format_timings(timings) -> list[str]:
    """把计时记录格式化成人类可读的文本行（CLI 与前端共用标签）。"""
    lines = []
    for item in timings or []:
        label = STAGE_LABELS.get(item.get('stage'), item.get('stage'))
        lines.append(
            f"  {str(label):<8} {float(item.get('seconds', 0)):>7.2f}s  {item.get('status')}"
        )
    return lines


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='研究主题或包含 URL 的描述')
    args = parser.parse_args()
    state = await run_task(
        args.input,
        lambda node, status, elapsed: print(
            f'[{elapsed:7.2f}s] {STAGE_LABELS.get(node, node)}: {status}'),
    )
    print('任务 ID:', state['task_id'])
    print('最终状态:', state['status'])
    if state.get('artifact'):
        print('HTML:', state['artifact']['html_path'])
    if state.get('deployment', {}).get('public_url'):
        print('访问地址:', state['deployment']['public_url'])
    print('各阶段耗时：')
    for line in format_timings(state.get('timings')):
        print(line)
    print('总耗时:', state.get('elapsed_seconds'), '秒')
    if state['status'] in outcome_module.ATTENTION_RUN_STATUSES | {'failed'}:
        print('请查看对应任务的 state.json 和服务端日志。')
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
