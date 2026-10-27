"""已受理的外部任务：登记原 remote id，由 Worker 查询这一条，不重新提交。"""
import logging
import time
import uuid

from app.config import settings

logger = logging.getLogger(__name__)

_OPEN = ('accepted', 'running', 'materializing')


def _remote_id(item: dict) -> str:
    for key in ('remote_task_id', 'remote_id', 'job_id', 'task_id'):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    summary = item.get('summary')
    if isinstance(summary, dict):
        return _remote_id(summary)
    return ''


def apply_outcome(state: dict, *, action_id: str, remote_id: str,
                  mapped: str, payload: dict | None) -> dict:
    """把查询结果写回快照里的工具回执。未知状态不会被写成成功。"""
    updated = dict(state or {})
    results = []
    for item in updated.get('tool_results') or []:
        if not isinstance(item, dict):
            results.append(item)
            continue
        row = dict(item)
        same_action = action_id and str(row.get('action_id') or '') == action_id
        same_remote = remote_id and str(row.get('remote_task_id') or '') == remote_id
        if same_action or same_remote:
            if mapped == 'succeeded':
                row['phase'] = 'completed'
                row['execution_status'] = 'succeeded'
            elif mapped == 'failed':
                row['phase'] = 'failed'
                row['execution_status'] = 'failed'
            elif mapped == 'cancelled':
                row['phase'] = 'cancelled'
                row['execution_status'] = 'failed'
            else:
                row['phase'] = 'needs_reconcile'
                row['execution_status'] = 'unknown'
            if payload is not None:
                row['external_outcome'] = payload
        results.append(row)
    updated['tool_results'] = results
    return updated


def backoff_seconds(version: int) -> float:
    base = float(settings.external_poll_base_seconds)
    cap = float(settings.external_poll_max_seconds)
    return min(cap, base * (2 ** min(max(int(version), 0), 5)))


async def remember_waiting(state: dict, waiting: list) -> bool:
    """把已受理任务写入 external_tasks。没有 remote id 时不假装能跟踪。"""
    if not settings.async_tool_runtime_enabled or not waiting:
        return False
    from app import execution_control
    from app import ledger as ledger_module
    book = ledger_module.current_ledger()
    context = execution_control.require()
    store = getattr(book, 'store', None) if book is not None else None
    if store is None or context is None or not hasattr(store, 'register_external_task'):
        return False
    run = await store.run(context.run_id)
    if not run:
        return False
    wrote = False
    now = time.time()
    for index, item in enumerate(waiting):
        if not isinstance(item, dict):
            continue
        remote = _remote_id(item)
        if not remote:
            continue
        action_id = str(item.get('action_id') or f'async-{index}')
        await store.register_external_task(
            external_task_id='et_' + uuid.uuid4().hex[:16],
            remote_task_id=remote,
            tenant_id=str(run.get('tenant_id') or 'default'),
            owner_user_id=str(run.get('owner_user_id') or 'default'),
            server_binding_id=str(item.get('server_binding_id') or ''),
            credential_binding=str(item.get('credential_binding') or ''),
            credential_subject=str(run.get('owner_user_id') or ''),
            tool_key=str(item.get('tool_key') or ''),
            contract_id=str(item.get('contract_id') or 'unspecified'),
            contract_version=int(item.get('contract_version') or 1),
            run_id=context.run_id,
            step_id=str(item.get('step_id') or ''),
            action_id=action_id,
            accepted_result_id=str(item.get('result_id') or ''),
            submission_key=str(item.get('submission_key') or remote),
            item_key=str(item.get('item_key') or str(index)),
            deadline_at=now + float(settings.external_wait_deadline),
            next_poll_at=now + float(settings.external_poll_base_seconds))
        wrote = True
    if wrote and hasattr(store, 'update_run'):
        await store.update_run(context.run_id, waiting_external=1)
    return wrote


async def _default_querier(store, task: dict, contract):
    from app import mcp_connections
    from app.integrations import mcp_adapter, mcp_servers
    parts = str(contract.status_tool_key or '').split(':')
    server_id = parts[1] if len(parts) >= 3 and parts[0] == 'mcp' else ''
    tool_name = parts[-1] if parts else ''
    if not tool_name or not contract.status_id_argument:
        raise RuntimeError('合同没有状态查询工具')
    token = await mcp_connections.bearer_for(
        store, tenant_id=str(task.get('tenant_id') or 'default'),
        user_id=str(task.get('owner_user_id') or 'default'),
        server_id=server_id)
    server = mcp_servers.by_id(server_id) if server_id else None
    return await mcp_adapter.call_tool(
        tool_name, {contract.status_id_argument: task.get('remote_task_id')},
        server=server, token=token)


def _materialize(task: dict, contract, payload: dict) -> str:
    from app import async_contracts
    from app import tool_results
    output = async_contracts.pointer(payload, contract.output_pointer) if contract else payload
    logical_id = 'ext-' + str(task.get('external_task_id') or 'result')
    folder = tool_results.result_dir(
        settings.artifact_dir, str(task.get('tenant_id') or 'default'),
        logical_id)
    folder.mkdir(parents=True, exist_ok=True)
    import json
    body = json.dumps(output, ensure_ascii=False, default=str)
    (folder / 'structured.json').write_text(body, encoding='utf-8')
    (folder / 'manifest.json').write_text(json.dumps({
        'result_id': logical_id, 'registered': True, 'created_at': time.time(),
    }, ensure_ascii=False), encoding='utf-8')
    return str(folder)


async def _patch_snapshot(store, task: dict, mapped: str, payload) -> None:
    if not hasattr(store, 'run_snapshot') or not hasattr(store, 'save_snapshot'):
        return
    snapshot = await store.run_snapshot(task['run_id']) or {}
    state = apply_outcome(snapshot.get('state') or {},
                          action_id=str(task.get('action_id') or ''),
                          remote_id=str(task.get('remote_task_id') or ''),
                          mapped=mapped, payload=payload if isinstance(payload, dict) else None)
    await store.save_snapshot(
        task['run_id'], plan_version=int(snapshot.get('plan_version') or 1),
        state=state, next_step_id=str(snapshot.get('next_step_id') or ''))


async def _wake_run(store, run_id: str, *, failed: str = '') -> None:
    if not hasattr(store, 'external_tasks_of_run'):
        return
    rows = await store.external_tasks_of_run(run_id)
    if any(str(row.get('state') or '') in _OPEN for row in rows):
        await store.update_run(run_id, waiting_external=1)
        return
    run = await store.run(run_id) if hasattr(store, 'run') else None
    status = str((run or {}).get('status') or '')
    if failed:
        if status in ('waiting_input', 'running', 'queued'):
            await store.update_run(run_id, status='needs_reconcile', waiting_external=0,
                                   error=failed, lease_owner='', lease_expires_at=None)
        return
    if status == 'waiting_input':
        await store.update_run(run_id, status='queued', waiting_external=0,
                               error='', lease_owner='', lease_expires_at=None)


async def poll_due(store, *, worker_id: str, querier=None, limit: int | None = None) -> int:
    """查询到期的外部任务。查询失败会再试；未知状态不会记成成功。"""
    if not settings.async_tool_runtime_enabled or store is None:
        return 0
    if not hasattr(store, 'due_external_tasks'):
        return 0
    from app import async_contracts
    ask = querier or (lambda task, contract: _default_querier(store, task, contract))
    rows = await store.due_external_tasks(
        limit=limit or settings.external_poll_concurrency)
    done = 0
    now = time.time()
    for task in rows:
        claimed = await store.claim_external_task_poll(
            task['external_task_id'], worker_id)
        if not claimed:
            continue
        epoch = int(claimed.get('poll_epoch') or 0)
        task = claimed
        run = await store.run(task['run_id']) if hasattr(store, 'run') else {}
        if str((run or {}).get('status') or '') == 'running':
            await store.finish_external_poll(
                task['external_task_id'], epoch, next_poll_at=now + 2)
            continue
        contract = async_contracts.match_id(
            str(task.get('contract_id') or ''), int(task.get('contract_version') or 0))
        if contract is None or not contract.recoverable or not contract.status_tool_key:
            await store.finish_external_poll(
                task['external_task_id'], epoch, state='needs_reconcile',
                error_code='no_contract', last_observed_at=now)
            await _patch_snapshot(store, task, 'needs_reconcile', None)
            await _wake_run(store, task['run_id'], failed='外部任务没有可查询的合同')
            done += 1
            continue
        deadline = float(task.get('deadline_at') or 0)
        if deadline and now > deadline and str(task.get('state')) != 'materializing':
            await store.finish_external_poll(
                task['external_task_id'], epoch, state='needs_reconcile',
                error_code='deadline', last_observed_at=now)
            await _patch_snapshot(store, task, 'needs_reconcile', None)
            await _wake_run(store, task['run_id'],
                            failed='外部任务超过观察期限，不能判定为失败或成功')
            done += 1
            continue
        try:
            payload = await ask(task, contract)
        except Exception as exc:                     # noqa: BLE001
            logger.warning('查询外部任务失败 id=%s', task.get('external_task_id'),
                           exc_info=True)
            version = int(task.get('version') or 1)
            await store.finish_external_poll(
                task['external_task_id'], epoch, state='running',
                error_code='query_failed', last_observed_at=now,
                next_poll_at=now + backoff_seconds(version), version=version + 1)
            done += 1
            continue
        if not isinstance(payload, dict):
            payload = {}
        mapped = async_contracts.map_status(
            async_contracts.pointer(payload, contract.status_pointer), contract)
        version = int(task.get('version') or 1)
        if mapped in ('accepted', 'running', 'unknown'):
            await store.finish_external_poll(
                task['external_task_id'], epoch, state='running',
                error_code='' if mapped != 'unknown' else 'unknown_status',
                last_observed_at=now, next_poll_at=now + backoff_seconds(version),
                version=version + 1)
            done += 1
            continue
        if mapped == 'succeeded':
            try:
                ref = _materialize(task, contract, payload)
            except OSError:
                logger.warning('外部结果落盘失败 id=%s', task.get('external_task_id'),
                               exc_info=True)
                await store.finish_external_poll(
                    task['external_task_id'], epoch, state='materializing',
                    error_code='materialize_failed', last_observed_at=now,
                    next_poll_at=now + backoff_seconds(version), version=version + 1)
                done += 1
                continue
            written = await store.finish_external_poll(
                task['external_task_id'], epoch, state='completed',
                error_code='', output_manifest_ref=ref, last_observed_at=now,
                version=version + 1)
            if written:
                await _patch_snapshot(store, task, 'succeeded', payload)
                await _wake_run(store, task['run_id'])
            done += 1
            continue
        written = await store.finish_external_poll(
            task['external_task_id'], epoch,
            state='failed' if mapped == 'failed' else 'cancelled',
            error_code=mapped, last_observed_at=now, version=version + 1)
        if written:
            await _patch_snapshot(store, task, mapped, payload)
            await _wake_run(store, task['run_id'])
        done += 1
    return done
