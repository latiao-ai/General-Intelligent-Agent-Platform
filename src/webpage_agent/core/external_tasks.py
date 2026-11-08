"""已受理的外部任务：登记原 remote id，由 Worker 查询这一条，不重新提交。"""
import logging
import time
import uuid

from webpage_agent.core.config import settings

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
    from webpage_agent.governance import execution_control
    from webpage_agent.store import ledger as ledger_module
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
            # 第八阶段收口第 9 点：item_key 用 step_id（与 settle_action_accepted
            # 一致），命中 (run_id, action_id, item_key) 唯一约束即幂等——共享
            # 网关已在 accepted 结算里登记过，这里不会重复建第二个外部任务。
            item_key=str(item.get('step_id') or item.get('item_key') or str(index)),
            deadline_at=now + float(settings.external_wait_deadline),
            next_poll_at=now + float(settings.external_poll_base_seconds))
        wrote = True
    if wrote and hasattr(store, 'update_run'):
        # 执行者侧写入：这里持有 `context`（正在跑的这次执行），必须过租约门禁，
        # 否则一个已被接管的旧执行者还能把新执行者的 waiting_external 改回 1。
        fence = getattr(store, 'update_run_owned', None)
        if callable(fence):
            await fence(context.run_id, lease_owner=context.lease_owner,
                        lease_epoch=context.lease_epoch, waiting_external=1)
        else:
            await store.update_run(context.run_id, waiting_external=1)
    return wrote


async def _default_querier(store, task: dict, contract):
    from webpage_agent.core import mcp_connections
    from webpage_agent.integrations import mcp_adapter, mcp_servers, mcp_tasks
    # 标准 MCP Tasks 走协议请求 `tasks/get`；合同路线走服务端声明的查询工具。
    # 两条路都只回一个字典，下游的状态映射与落盘完全不区分。
    protocol = str(contract.contract_id or '') == mcp_tasks.PROTOCOL_CONTRACT_ID
    tool_name = ''
    if protocol:
        server_id = mcp_tasks.server_of_tool_key(str(task.get('tool_key') or ''))
    else:
        parts = str(contract.status_tool_key or '').split(':')
        server_id = parts[1] if len(parts) >= 3 and parts[0] == 'mcp' else ''
        tool_name = parts[-1] if parts else ''
        if not tool_name or not contract.status_id_argument:
            raise RuntimeError('合同没有状态查询工具')
    run = await store.run(str(task.get('run_id') or ''))
    token = await mcp_connections.bearer_for(
        store, tenant_id=str(task.get('tenant_id') or 'default'),
        user_id=str(task.get('owner_user_id') or 'default'),
        server_id=server_id, platform_id=str((run or {}).get('platform_id') or ''))
    server = mcp_servers.by_id(server_id) if server_id else None
    if protocol:
        return await mcp_adapter.task_status(
            str(task.get('remote_task_id') or ''), server=server, token=token)
    return await mcp_adapter.call_tool(
        tool_name, {contract.status_id_argument: task.get('remote_task_id')},
        server=server, token=token)


def _materialize(task: dict, contract, payload: dict) -> dict:
    """把远端结果物化到**稳定文件身份**，返回待登记的 staged（第八阶段收口第 8 点）。

    用 `stage_result` 产出标准结构（result_id / parts / raw_ref / completeness），
    result_id 由 external_task_id 派生（`ext-<external_task_id>`），跨进程、跨
    重试一致——同一任务重试物化永远写同一个位置，幂等、可重试。先可靠保存结果，
    再由调用方在结算事务里 `commit_result_in_tx` 登记可消费引用——数据库只引用
    已可靠保存的内容。

    返回的 staged 交给 `complete_external_task_and_wake` 在同一事务登记，不能
    仅保存状态包/文件路径就标记 completed（否则读回时拿不到可消费结果）。
    """
    from webpage_agent.core import async_contracts
    from webpage_agent.tools import tool_results as tr
    output = async_contracts.pointer(payload, contract.output_pointer) if contract else payload
    result_id = 'ext-' + str(task.get('external_task_id') or 'result')
    tenant_id = str(task.get('tenant_id') or 'default')
    # 用标准采集结构：structured 统一保存可 JSON 化的结果（非对象结果包成
    # {'result': ...}，保证交付时 read_structured 能读回），不丢。
    structured = output if isinstance(output, dict) else {'result': output}
    captured = tr.CapturedResult(structured=structured)
    return tr.stage_result(
        settings.artifact_dir, captured, tenant_id=tenant_id, result_id=result_id)


def _envelope_for(task: dict, staged: dict) -> dict:
    """构造结果登记信封（v2），供 commit_result_in_tx 使用。

    归属来自 external task（租户/用户/run/step/action/tool_key），不是内容 hash
    或目录名；业务状态是 succeeded（结果已确认完成并物化）。
    """
    return {
        'tool_key': str(task.get('tool_key') or ''),
        'schema_hash': '',
        'contract_version': str(task.get('contract_version') or ''),
        'transport_status': 'ok',
        'business_status': 'succeeded',
        'verification_status': 'not_applicable',
        'summary': {},
        'structured_ref': str(staged.get('raw_ref') or ''),
    }


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
    """外部任务有结论时唤醒 run。

    这里的调用者是**轮询器**（Worker 的 `poll_due`），不是这条 run 的执行者：
    轮询用的是 `external_tasks` 自己的 `poll_epoch` 抢占，与 run 的租约是两套
    凭证。所以这是**管理动作**，有意保留越权写（`update_run` 而非 `_owned`）：
    外部任务可能在执行者换人之后才回结果，而唤醒的正当性来自"外部状态变了"，
    不来自"我还持有这条 run 的租约"。把它改成 `_owned` 会让回结果永远唤不醒。
    """
    if not hasattr(store, 'external_tasks_of_run'):
        return
    rows = await store.external_tasks_of_run(run_id)
    if any(str(row.get('state') or '') in _OPEN for row in rows):
        # 管理动作：只标"还在等外部任务"，见上方说明
        await store.update_run(run_id, waiting_external=1)
        return
    run = await store.run(run_id) if hasattr(store, 'run') else None
    status = str((run or {}).get('status') or '')
    if failed:
        if status in ('waiting_input', 'running', 'queued'):
            # 管理动作：外部任务没有可查询的合同/超期，必须把 run 交给人工核实。
            # 这里清 lease_owner/lease_expires_at 是有意的：原执行者已经无从收尾。
            await store.update_run(run_id, status='needs_reconcile', waiting_external=0,
                                   error=failed, lease_owner='', lease_expires_at=None)
        return
    if status == 'waiting_input':
        # 管理动作：把"等外部结果"的 run 放回队列，让执行者继续
        await store.update_run(run_id, status='queued', waiting_external=0,
                               error='', lease_owner='', lease_expires_at=None)


async def request_external_cancel(store, *, external_task_id: str,
                                  worker_id: str) -> dict:
    """请求取消一个已受理的外部任务（第八阶段第 9 点）。

    区分三种情况：
    - 远端取消**请求**：持久化 cancel_requested 意图，调用合同取消能力；
    - 远端**已确认取消**：查询确认后把 external_task 标 `cancelled`；
    - 远端不支持取消 / 未确认：如实保留 `running` 状态，**不声称已取消**。

    返回 `{requested, confirmed, state}`。
    """
    if store is None or not hasattr(store, 'request_external_cancel'):
        return {'requested': False, 'confirmed': False, 'state': ''}
    row = await store.request_external_cancel(external_task_id)
    if row is None:
        # 已终态或不存在：没有可取消的开放任务。
        return {'requested': False, 'confirmed': False,
                'state': str(row.get('state') or '') if row else ''}
    task = dict(row)
    from webpage_agent.core import async_contracts
    from webpage_agent.integrations import mcp_tasks as _task_protocol
    contract = async_contracts.match_id(
        str(task.get('contract_id') or ''), int(task.get('contract_version') or 0))
    protocol = str(task.get('contract_id') or '') == _task_protocol.PROTOCOL_CONTRACT_ID
    if contract is None:
        return {'requested': True, 'confirmed': False,
                'state': str(task.get('state') or '')}
    # 调用合同取消能力（协议任务走 tasks/cancel，合同路线走 cancel_tool_key）。
    try:
        if protocol:
            from webpage_agent.integrations import mcp_adapter
            await mcp_adapter.cancel_task(str(task.get('remote_task_id') or ''))
        elif contract.cancel_tool_key:
            from webpage_agent.integrations import mcp_adapter
            parts = str(contract.cancel_tool_key or '').split(':')
            tool_name = parts[-1] if parts else ''
            if tool_name:
                await mcp_adapter.call_tool(
                    tool_name, {contract.status_id_argument:
                                task.get('remote_task_id')})
        else:
            # 远端不支持取消：如实保留状态，不声称已取消。
            return {'requested': True, 'confirmed': False,
                    'state': str(task.get('state') or 'running')}
    except Exception as exc:                       # noqa: BLE001
        logger.warning('请求远端取消失败 id=%s', external_task_id, exc_info=True)
        return {'requested': True, 'confirmed': False,
                'state': str(task.get('state') or 'running')}
    # 查询确认：远端是否真的取消了。
    confirmed = False
    try:
        epoch = int(task.get('poll_epoch') or 0)
        payload = await _default_querier(store, task, contract)
        mapped = async_contracts.map_status(
            async_contracts.pointer(payload, contract.status_pointer), contract)
        confirmed = mapped == 'cancelled'
    except Exception:                              # noqa: BLE001
        confirmed = False
    if hasattr(store, 'confirm_external_cancel'):
        written = await store.confirm_external_cancel(
            external_task_id, epoch, owner=worker_id, confirmed=confirmed)
        if written:
            task = written
    return {'requested': True, 'confirmed': confirmed,
            'state': str(task.get('state') or '')}


async def poll_due(store, *, worker_id: str, querier=None, limit: int | None = None) -> int:
    """查询到期的外部任务。查询失败会再试；未知状态不会记成成功。

    第八阶段第 6/7/8 点收紧：
    - 领取用**带门禁**的 `claim_external_task_poll`（重查开放状态/到期/租约）；
    - 查询用 `external_poll_timeout` 限时，且**短于**领取时给的租约（续租兜底，
      不维持「查询 90s vs 租约 60s」的冲突）；
    - 完成走 `complete_external_task_and_wake`（终态 + 唤醒同事务），poller
      **不直接改写 Pi messages**；由获得执行权的宿主吸收结果注入 toolResult；
    - 结果物化失败进 materializing（有重试期限），不标记本地完成。
    """
    if not settings.async_tool_runtime_enabled or store is None:
        return 0
    if not hasattr(store, 'due_external_tasks'):
        return 0
    from webpage_agent.core import async_contracts
    import asyncio
    ask = querier or (lambda task, contract: _default_querier(store, task, contract))
    rows = await store.due_external_tasks(
        limit=limit or settings.external_poll_concurrency)
    # 查询限时必须短于轮询租约：否则查询还没回来，租约先过期，另一个 Worker 又领
    # 同一条任务重复查询。租约 = 查询限时 × 2（至少 60s）。
    query_timeout = float(getattr(settings, 'external_poll_timeout', 30) or 30)
    poll_lease = max(60.0, query_timeout * 2)
    done = 0
    now = time.time()
    for task in rows:
        claimed = await store.claim_external_task_poll(
            task['external_task_id'], worker_id, lease_seconds=poll_lease)
        if not claimed:
            continue
        epoch = int(claimed.get('poll_epoch') or 0)
        task = claimed
        contract = async_contracts.match_id(
            str(task.get('contract_id') or ''), int(task.get('contract_version') or 0))
        from webpage_agent.integrations import mcp_tasks as _task_protocol
        protocol = str(task.get('contract_id') or '') == _task_protocol.PROTOCOL_CONTRACT_ID
        # 协议任务没有"状态查询工具"（查询是 tasks/get 请求），不能按合同那条
        # 路的要求把它排除掉；其它情况仍要求合同写明可查询的工具。
        if contract is None or not contract.recoverable or (
                not protocol and not contract.status_tool_key):
            await store.complete_external_task_and_wake(
                task['external_task_id'], epoch, owner=worker_id,
                state='needs_reconcile',
                error_code='no_contract')
            done += 1
            continue
        # 合同自己的轮询间隔与截止时间真正参与调度。
        poll_interval = float(getattr(contract, 'poll_interval_seconds', 5) or 5)
        deadline = float(task.get('deadline_at') or 0)
        if deadline and now > deadline and str(task.get('state')) != 'materializing':
            await store.complete_external_task_and_wake(
                task['external_task_id'], epoch, owner=worker_id,
                state='needs_reconcile',
                error_code='deadline')
            done += 1
            continue
        try:
            async with asyncio.timeout(query_timeout):
                payload = await ask(task, contract)
        except asyncio.TimeoutError:
            logger.warning('查询外部任务超时 id=%s', task.get('external_task_id'))
            version = int(task.get('version') or 1)
            await store.finish_external_poll(
                task['external_task_id'], epoch, owner=worker_id,
                state='running',
                error_code='query_timeout', last_observed_at=now,
                next_poll_at=now + backoff_seconds(version), version=version + 1)
            done += 1
            continue
        except Exception:                            # noqa: BLE001  查询失败要重试，不判结论
            logger.warning('查询外部任务失败 id=%s', task.get('external_task_id'),
                           exc_info=True)
            version = int(task.get('version') or 1)
            await store.finish_external_poll(
                task['external_task_id'], epoch, owner=worker_id,
                state='running',
                error_code='query_failed', last_observed_at=now,
                next_poll_at=now + backoff_seconds(version), version=version + 1)
            done += 1
            continue
        if not isinstance(payload, dict):
            payload = {}
        # 第八阶段收口第 8 点：远端 reported completed 但结果未就绪（取结果失败）
        # → 进入 materializing 重试，**不误判 succeeded**。结果获取/存储失败有
        # 重试期限（backoff），不标记本地完成。
        if payload.get('result_pending'):
            await store.finish_external_poll(
                task['external_task_id'], epoch, owner=worker_id,
                state='materializing',
                error_code='result_pending', last_observed_at=now,
                next_poll_at=now + backoff_seconds(version), version=version + 1)
            done += 1
            continue
        mapped = async_contracts.map_status(
            async_contracts.pointer(payload, contract.status_pointer), contract)
        version = int(task.get('version') or 1)
        if mapped in ('accepted', 'running', 'unknown'):
            await store.finish_external_poll(
                task['external_task_id'], epoch, owner=worker_id,
                state='running',
                error_code='' if mapped != 'unknown' else 'unknown_status',
                last_observed_at=now, next_poll_at=now + backoff_seconds(version),
                version=version + 1)
            done += 1
            continue
        if mapped == 'succeeded':
            try:
                staged = _materialize(task, contract, payload)
            except OSError:
                logger.warning('外部结果落盘失败 id=%s', task.get('external_task_id'),
                               exc_info=True)
                # 第八阶段第 7 点：结果获取/存储失败进 materializing（有重试期限），
                # 不能标记本地完成。
                await store.finish_external_poll(
                    task['external_task_id'], epoch, owner=worker_id,
                    state='materializing',
                    error_code='materialize_failed', last_observed_at=now,
                    next_poll_at=now + backoff_seconds(version), version=version + 1)
                done += 1
                continue
            # 第八阶段收口第 8 点：结果已可靠落盘（staged）。在同一事务里完成
            # 标准结果存储登记 + 终态 + 唤醒。不能仅保存路径就标记 completed。
            envelope = _envelope_for(task, staged)
            result_id = str(staged.get('result_id') or
                            'ext-' + str(task.get('external_task_id') or 'result'))
            written = await store.complete_external_task_and_wake(
                task['external_task_id'], epoch, owner=worker_id,
                state='completed',
                output_manifest_ref=str(staged.get('raw_ref') or ''),
                last_result_id=result_id,
                staged=staged, envelope=envelope)
            done += 1
            continue
        # failed / cancelled：终态 + 同事务唤醒（或待核实）。
        await store.complete_external_task_and_wake(
            task['external_task_id'], epoch, owner=worker_id,
            state='failed' if mapped == 'failed' else 'cancelled',
            error_code=mapped)
        done += 1
    return done
