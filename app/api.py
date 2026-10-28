"""FastAPI + SSE 服务：把智能体的执行过程以事件流推给前端。

节点一发出事件就立刻推送，延迟只在网络往返量级，且天然支持多客户端同时订阅同
一个任务。

启动：
    uvicorn app.api:app --reload --port 8000
或：
    python -m scripts.serve_api
"""
import asyncio
import json
import os
import mimetypes
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import BaseModel, Field

from app import idempotency, memory as memory_module, outcome as outcome_module
from app import runtime_check
from app import scene_context as scene_context_module
from app import task_notify
from app import auth, policies, profile
from app import run_inputs as run_inputs_module
from app import tools as tool_module
from app.api_hub import CLOSE, Hub
from app.config import settings
from app.pg_store import SessionGoalConflict
from app.logging_setup import setup_logging

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / 'web'

# 模型调用或外部工具可能上百秒没有任何事件，必须定期发心跳，
# 否则连接会被中间的代理/网关按空闲超时掐断。
KEEPALIVE_SECONDS = 15

hub = Hub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程生命周期：启动时对账、退出时收尾。

    对账**不能放在模块导入处**（原来是 `RECOVERY = await hub.reconcile()`）：那样
    `uvicorn --reload` 一次、或者起第二个副本，就会把别的进程正在跑的任务判成
    "结果未知"并清掉租约，于是立刻又被领走 —— 正好制造它想避免的重复执行。
    现在只处理**自己名下的或租约已过期**的运行。
    """
    setup_logging('api')
    problems = runtime_check.issues()
    if problems:
        raise RuntimeError('；'.join(problems))
    from app import async_contracts
    contract_summary = async_contracts.summary()
    logger.info(
        '身份模式 %s，工具政策 %s，结果信封 %s，异步运行 %s，可恢复合同 %s，并发上限 %s',
        settings.auth_mode, settings.tool_policy_enforced,
        settings.result_envelope_v2, settings.async_tool_runtime_enabled,
        contract_summary['recoverable'], settings.max_concurrent_runs)
    if settings.async_tool_runtime_enabled and contract_summary['recoverable'] == 0:
        logger.warning(
            '异步运行已打开，但没有可恢复合同。只返回「已受理」的长任务在重启后不能继续跟踪')
    # 先确保仓储就绪（PG 会在这里打开连接池并建表）：连不上库应当在启动日志里
    # 直接失败，而不是等第一个请求到了才炸
    ready = getattr(hub.store, 'ready', None)
    if callable(ready):
        await ready()
    # 认证依赖要在请求里拿仓储解析中台会话，但 auth 不能 import api（会成环），
    # 所以挂在 app.state 上按请求取。
    app.state.store = hub.store
    stats = await hub.reconcile(owner=hub.owner_id)
    app.state.recovery = stats
    purged = await hub.store.purge_request_keys()
    logger.info('API 启动完成，对账结果 %s，清理过期幂等键 %s 条', stats, purged)
    task_notify.start(settings.database_url)
    try:
        yield
    finally:
        await task_notify.stop()
        await hub.close()
        from app.integrations.mcp_adapter import close_stdio_sessions
        await close_stdio_sessions()
        # 连接池也要显式关：否则退出时池里的连接靠 GC 收，日志里看不到
        # "到底关没关"，测速/压测反复起停还会在服务端堆出空闲连接。
        close_store = getattr(hub.store, 'close', None)
        if callable(close_store):
            closing = close_store()
            if hasattr(closing, '__await__'):
                await closing
        logger.info('API 已关闭，数据库连接池已收尾')


app = FastAPI(title='通用 Agent 执行中台 API', version='2.0', lifespan=lifespan)


@app.middleware('http')
async def guard_origin(request: Request, call_next):
    if runtime_check.origin_rejected(request.method, request.headers.get('origin', '')):
        return JSONResponse(status_code=403, content={'detail': 'Origin 不在允许列表'})
    return await call_next(request)


class ProfileRequest(BaseModel):
    # 画像就是一份 Markdown，直接收原文：手改、粘贴、整段重写都走同一条路。
    text: str = Field(default='', max_length=20000)
    version: int | None = None


class MemoryRequest(BaseModel):
    summary: str = Field(default='', max_length=4000)
    # 关键事实用「键：值」多行文本编辑，与画像保持同一种手感
    facts: str = Field(default='', max_length=4000)


class RunInputRequest(BaseModel):
    """给**当前任务**的补充要求。

    `request_id` 由客户端生成并持久保存：响应丢失时用同一个 ID 重发，服务端会
    返回原来那条输入（版本不再增长），而不是再加一版要求。
    """
    # 只限制"能安全当键用"的字符与长度上限：下限交给客户端自觉，不能因为
    # 客户端用了短 ID 就把它判成非法请求（幂等才是这条约束的目的）。
    request_id: str = Field(min_length=1, max_length=64,
                            pattern=r'^[A-Za-z0-9_.:-]+$')
    message: str = Field(min_length=1, max_length=10000)
    # 可选：客户端拿着的 run_id/task_id。给了就校验归属，防止把输入转发到别的 run
    task_id: str | None = Field(default=None, max_length=64)


class PlatformContextRequest(BaseModel):
    """Any registered platform can preview its signed scene without starting work."""
    context: dict | None = None
    context_assertion: str | None = Field(default=None, max_length=8192)


class TaskRequest(PlatformContextRequest):
    message: str = Field(min_length=1, max_length=10000)
    session_id: str | None = None
    # 也可以放在 Idempotency-Key 头里；两处都给时以头为准。
    idempotency_key: str | None = Field(default=None, max_length=200)
    # 平台后端签名过的当前项目 / 选中对象。ticket 模式下必须带 context_assertion。


def sse(event: dict) -> str:
    """SSE 帧。事件类型放在 JSON 里，前端统一用 onmessage 处理。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class ExchangeRequest(BaseModel):
    """平台后端签发的**登录票据**（JWT，一次性、短期）。

    注意：票据只能拿来兑换，**不能**当业务接口的凭证用。
    """
    ticket: str = Field(min_length=1, max_length=8192)


# ---------- 身份 ----------
# 浏览器侧必须有 cookie：EventSource 与 <a download> 都发不出自定义请求头，
# 只支持 Authorization 的话 SSE 事件流和产物下载会全部 401。

SESSION_COOKIE_PATH = '/'


def _set_session_cookie(response: Response, token: str, max_age: int) -> None:
    """写中台会话 cookie。

    HttpOnly 挡住脚本读取；SameSite 默认 lax（同源代理场景够用）；Secure 由
    `AUTH_COOKIE_SECURE` 控制（本地 http 调试时需要关掉，否则浏览器不存）。
    """
    response.set_cookie(
        settings.auth_session_cookie, token, max_age=max_age,
        httponly=True, samesite=settings.auth_cookie_samesite,
        secure=settings.auth_cookie_secure, path=SESSION_COOKIE_PATH)


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.auth_session_cookie, path=SESSION_COOKIE_PATH)


@app.get('/api/me')
async def whoami(principal=Depends(policies.current_principal)):
    """当前主体。身份来自**已验证的中台登录会话**（ticket 模式）。"""
    return {'tenant_id': principal.tenant_id,
            'user_id': principal.user_id,
            'role': principal.role,
            'platform_id': principal.platform_id,
            'authenticated': principal.authenticated,
            'anonymous': principal.anonymous,
            'auth_mode': settings.auth_mode}


@app.post('/api/auth/exchange')
async def exchange_ticket(payload: ExchangeRequest, request: Request,
                          response: Response):
    """平台票据 → 中台登录会话。

    这是**唯一**接受外部票据的入口。票据一次性：同一张票据第二次来会得到 409，
    平台应重新签发一张（而不是重试同一张）。
    """
    if settings.auth_mode == 'off':
        raise HTTPException(status_code=400, detail='AUTH_MODE=off，未启用认证')
    store = getattr(request.app.state, 'store', None)
    if store is None:
        raise HTTPException(status_code=503, detail='身份仓储未就绪')
    try:
        result = await auth.exchange(
            payload.ticket, store,
            user_agent=request.headers.get('user-agent', ''),
            remote_addr=(request.client.host if request.client else ''))
    except auth.ExchangeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.reason) from exc

    _set_session_cookie(response, result['token'], settings.auth_session_ttl)
    # 凭证原文只在 Set-Cookie 里出现一次，响应体不再重复（避免被日志/前端留存）
    return {'user_id': result['user_id'],
            'tenant_id': result['tenant_id'],
            'role': result['role'],
            'platform_id': result['platform_id'],
            'connection_id': result['connection_id'],
            'auth_session_id': result['auth_session_id'],
            'expires_at': result['expires_at']}


@app.delete('/api/auth/session')
async def drop_session(request: Request, response: Response):
    """退出登录：撤销数据库里的会话，并清 cookie。已连上的 SSE 会在下个检查点断开。"""
    store = getattr(request.app.state, 'store', None)
    revoked = False
    try:
        revoked = await auth.revoke(request, store) if store else False
    except Exception:                                   # noqa: BLE001
        logger.warning('撤销登录会话失败', exc_info=True)
    _clear_session_cookie(response)
    return {'ok': True, 'revoked': revoked}


# ---------- 对象权限 ----------
# 订阅 SSE、下载产物、读取画像、读取结果全部执行对象权限。
# 以前只有 /api/runs 与 /api/artifacts 做了检查，其余接口知道 ID 就能读别人的
# 会话——而 session_id 是客户端传入的 12 位十六进制，猜中即接管上下文。


async def _owned_task(task_id: str, principal):
    record = await hub.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    policies.ensure_owner({'tenant_id': record.tenant_id,
                           'owner_user_id': record.owner_user_id},
                          principal, '任务')
    return record


async def _owned_session(session_id: str, principal) -> dict:
    row = await hub.store.session(session_id)
    if row is None:
        # 有些旧数据只有 messages 没有 sessions 行；按"没有"处理更安全
        raise HTTPException(status_code=404, detail='会话不存在')
    return policies.ensure_owner(row, principal, '会话')


async def _owned_run(run_id: str, principal) -> dict:
    run = await hub.store.run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='运行不存在')
    meta = await hub.store.task_meta(run['task_id']) or {}
    resource = dict(run)
    resource['owner_user_id'] = meta.get('owner_user_id') or 'default'
    resource['tenant_id'] = run.get('tenant_id') or 'default'
    policies.ensure_owner(resource, principal, '运行')
    return run


async def _owned_result(result_id: str, principal) -> dict:
    """结果只认登记行。没有登记，或不是当前企业的当前用户，都是 404。"""
    manifest = await hub.store.result_manifest(result_id)
    return policies.ensure_owner(manifest, principal, '结果')


async def _owned_part(result_id: str, part_id: str, principal) -> tuple[dict, dict]:
    """分块必须属于这条已通过归属检查的结果。磁盘清单不算数。"""
    manifest = await _owned_result(result_id, principal)
    rows = await hub.store.result_parts(result_id)
    part = next((item for item in rows
                 if str(item.get('part_id') or '') == part_id
                 and str(item.get('result_id') or '') == str(manifest.get('result_id') or '')),
                None)
    if not isinstance(part, dict):
        raise HTTPException(status_code=404, detail='分块不存在')
    return manifest, part


@contextmanager
def _profile_scope(principal):
    """把画像读写限定在当前主体下（profile/<user>.md）。"""
    token = profile.use(principal.user_id)
    try:
        yield
    finally:
        profile.reset(token)


@app.get('/', response_class=HTMLResponse)
async def index():
    # 页面是内联 JS 的单文件，一旦被浏览器缓存就会出现"代码改了但界面还是旧的"，
    # 排查时极易被误导。这里明确不缓存。
    return HTMLResponse((WEB_DIR / 'index.html').read_text(encoding='utf-8'),
                        headers={'Cache-Control': 'no-store, must-revalidate'})


@app.post('/api/tasks')
async def create_task(payload: TaskRequest, request: Request,
                      principal=Depends(policies.current_principal)):
    """开一轮任务。带上 session_id 即可实现多轮（追加要求）。

    幂等：客户端用 `Idempotency-Key` 头（或请求体 `idempotency_key`）表达
    "这是我那个请求的重试"。同一个键 + 同样的内容只会产生一个任务；
    键相同但内容不同返回 409，而不是悄悄建第二个任务。
    """
    client_key = (request.headers.get('idempotency-key')
                  or payload.idempotency_key or '').strip()
    request_spec = None
    try:
        if client_key:
            key = idempotency.validate_key(client_key)
            request_spec = {
                'scope': idempotency.scope_key(principal.tenant_id, principal.user_id,
                                               'tasks.create', key),
                'client_key': key,
                'fingerprint': idempotency.fingerprint({
                    'message': payload.message, 'session_id': payload.session_id,
                    'context_hash': scene_context_module.canonical_hash(payload.context)
                    if payload.context else ''}),
            }
            existing = await hub.store.request_key(request_spec['scope'])
            if existing and float(existing['expires_at']) >= time.time():
                if existing['fingerprint'] != request_spec['fingerprint']:
                    raise idempotency.IdempotencyConflict('该幂等键已用于不同内容的请求')
                if existing.get('task_id'):
                    record = await _owned_task(existing['task_id'], principal)
                    # 已验收过的请求可以重放，不要求重试时现场票仍未过期。
                    return {**_creation_response(record, run_id=existing['run_id']),
                            'idempotent_replay': True}
        return await _start_task(payload, principal, request_spec=request_spec)
    except idempotency.IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except idempotency.IdempotencyInFlight as exc:
        raise HTTPException(status_code=409, detail=str(exc),
                            headers={'Retry-After': '1'}) from None
    except idempotency.IdempotencyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _scene_for(payload: PlatformContextRequest, principal) -> dict | None:
    """ticket 模式下，带了现场就必须有平台签名。没带现场则不注入。"""
    if not payload.context:
        return None
    if settings.auth_mode == 'ticket' or payload.context_assertion:
        try:
            return scene_context_module.verify(
                payload.context_assertion or '', payload.context,
                tenant_id=principal.tenant_id,
                **({'subject': principal.subject,
                    'platform_id': principal.platform_id,
                    'connection_id': principal.connection_id}
                   if principal.authenticated else {}))
        except scene_context_module.SceneError as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from None
    return {'verified': False,
            'context_hash': scene_context_module.canonical_hash(payload.context),
            'context': payload.context}


@app.post('/api/context/verify')
async def verify_platform_context(payload: PlatformContextRequest,
                                  principal=Depends(policies.current_principal)):
    """Verify a platform scene for UI readiness; no task, model or tool execution.

    Uses exactly the same verifier as task admission. Platform fields stay opaque
    here: identity, trust registration and hash binding define the contract.
    """
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail='需要已验证的平台登录会话')
    if not payload.context or not payload.context_assertion:
        raise HTTPException(status_code=400, detail='缺少现场上下文或签名证明')
    return _scene_for(payload, principal)


async def _start_task(payload: TaskRequest, principal, *, request_spec=None) -> dict:
    scene = _scene_for(payload, principal)
    try:
        record = await hub.start(payload.message, payload.session_id, principal,
                                 scene=scene, request_spec=request_spec)
    except SessionGoalConflict as exc:
        raise HTTPException(status_code=409, detail={
            'code': 'session_goal_open', 'message': str(exc),
            'task_id': exc.task_id, 'run_id': exc.run_id,
            'inputs_url': f'/api/runs/{exc.run_id}/inputs'}) from None
    except PermissionError:
        # 传入的 session_id 不属于当前主体：按项目约定回 404，不区分"不存在/无权限"
        raise HTTPException(status_code=404, detail='会话不存在') from None
    return _creation_response(record)


def _creation_response(record, *, run_id=None) -> dict:
    # status 是展示标签（旧字段名不变），run_status 才是状态机里的通用状态
    return {'task_id': record.task_id, 'session_id': record.session_id,
            'run_id': run_id or record.run_id,
            **outcome_module.view(record.status, record.state or {}),
            **({'idempotent_replay': True} if record.idempotent_replay else {})}


@app.get('/api/tasks/{task_id}')
async def get_task(task_id: str,
                   principal=Depends(policies.current_principal)):
    return (await _owned_task(task_id, principal)).summary()


@app.post('/api/tasks/{task_id}/cancel')
async def cancel_task(task_id: str,
                      principal=Depends(policies.current_principal)):
    record = await _owned_task(task_id, principal)
    if not await hub.cancel(record.task_id):
        # 老实说"没停成"，而不是把 run 标成 cancelled 让界面以为停了
        raise HTTPException(status_code=409, detail='该任务已经结束，无法取消')
    return {'cancelled': True}


@app.get('/api/tasks/{task_id}/events')
async def task_events(task_id: str, request: Request,
                      after: str | None = Query(default=None),
                      principal=Depends(policies.current_principal)):
    record = await _owned_task(task_id, principal)

    # 游标：优先用 Last-Event-ID 头，其次 ?after=，从最后确认的 seq 之后补发
    cursor = request.headers.get('last-event-id') or after or '0'

    async def stream():
        queue, history = await record.subscribe()
        try:
            last_seq = int(cursor)
        except (TypeError, ValueError):
            last_seq = 0
        try:
            # 先补发库里已有的事件：晚连上来、甚至服务重启后再连，都能看到完整过程。
            for seq, event in history:
                if seq is not None and seq <= last_seq:
                    continue          # 断线前已收到过的，不重复推
                if seq is not None:
                    last_seq = seq
                # 带 id: 让标准 EventSource 记住 Last-Event-ID
                yield (f'id: {last_seq}\n' if seq is not None else '') + sse(event)
            if record.done:
                yield sse({'type': 'end',
                           **outcome_module.view(record.status, record.state or {})})
                return
            last_auth_check = time.time()
            last_ping = time.time()
            wake = task_notify.subscribe(record.task_id)

            async def replay_persisted() -> list[str]:
                nonlocal last_seq
                chunks = []
                try:
                    for seq, event in await hub.store.events(record.task_id):
                        if seq is None or seq <= last_seq:
                            continue
                        last_seq = seq
                        chunks.append(f'id: {last_seq}\n' + sse(event))
                except Exception:                   # noqa: BLE001
                    logger.warning('补读持久事件失败 task=%s', task_id, exc_info=True)
                return chunks

            while True:
                getter = asyncio.create_task(queue.get())
                waiter = asyncio.create_task(wake.wait())
                done, pending = await asyncio.wait(
                    {getter, waiter}, timeout=KEEPALIVE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED)
                for pending_task in pending:
                    pending_task.cancel()
                    try:
                        await pending_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                notified = waiter in done
                if notified:
                    wake.clear()
                got_item = getter in done and not getter.cancelled()
                if notified or not got_item:
                    for chunk in await replay_persisted():
                        yield chunk
                if not got_item:
                    # 用真实事件而不是 SSE 注释做心跳，前端才能判断连接还在。
                    # 通知只负责叫醒；序号仍以数据库为准。
                    if time.time() - last_ping >= KEEPALIVE_SECONDS:
                        last_ping = time.time()
                        if time.time() - last_auth_check >= settings.auth_sse_recheck:
                            last_auth_check = time.time()
                            try:
                                await policies.current_principal(request)
                            except HTTPException:
                                yield sse({'type': 'end', 'status': 'interrupted',
                                           'detail': '登录已失效，请重新登录'})
                                return
                        yield sse({'type': 'ping'})
                    continue
                item = getter.result()
                if item is CLOSE:
                    yield sse({'type': 'end', 'status': outcome_module.label(
                        record.status, record.state or {}),
                        'run_status': outcome_module.normalize_status(record.status)})
                    return
                seq, event = item
                if seq is not None:
                    if seq <= last_seq:
                        continue
                    last_seq = seq
                yield (f'id: {last_seq}\n' if seq is not None else '') + sse(event)
        finally:
            if 'wake' in locals():
                task_notify.unsubscribe(record.task_id, wake)
            record.unsubscribe(queue)

    return StreamingResponse(stream(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        # 关掉 nginx 一类反向代理的响应缓冲，否则事件会被攒着一起发。
        'X-Accel-Buffering': 'no',
    })


@app.post('/api/tasks/{task_id}/deploy')
async def deploy(task_id: str,
                 principal=Depends(policies.current_principal)):
    """部署按钮：**走执行器**，不再直接调 deploy_task。

    直接调等于开了一个后门：没有账本 action（崩在中途不知道发没发出去）、
    没有事件序号（界面看不到过程）、不经过策略检查，暂停/取消也管不到它。
    现在它是一次正常执行：建 run → 一步计划 → 账本 + 事件 + 取消语义全都复用。
    """
    record = await _owned_task(task_id, principal)
    artifact = (record.state or {}).get('artifact') or {}
    if not (artifact.get('path') or artifact.get('html_path')):
        raise HTTPException(status_code=409, detail='该任务还没有可交付的产物')
    if not settings.deploy_enabled:
        raise HTTPException(status_code=409,
                            detail='部署已关闭（DEPLOY_ENABLED=false）')
    try:
        result = await hub.deploy_artifact(record, artifact, principal)
    except Exception as exc:                     # noqa: BLE001
        logger.exception('部署失败 task=%s', task_id)
        raise HTTPException(status_code=502,
                            detail=f'部署失败（{type(exc).__name__}）') from None
    return result


@app.get('/api/tasks/{task_id}/artifact')
async def artifact(task_id: str,
                   principal=Depends(policies.current_principal)):
    """下载/预览任务产物。

    文件路径以任务 state 里记的 `artifact.html_path` 为准——报告是 index.html，
    "资料转 HTML" 产出的是 sources.html；早先这里写死 index.html，导致资料页 404。
    """
    record = await _owned_task(task_id, principal)
    meta = (record.state or {}).get('artifact') or {}
    recorded = meta.get('path') or meta.get('html_path')
    if recorded:
        path = Path(recorded)
    elif record.artifact_id:
        # 旧任务没有记录路径，按约定回退
        path = settings.artifact_dir / record.artifact_id / 'index.html'
    else:
        raise HTTPException(status_code=404, detail='产物不存在')
    if not path.exists():
        raise HTTPException(status_code=404, detail='产物不存在')
    # 媒体类型别写死 text/html：视频产物那样会被浏览器当成网页而放不出来。
    # 优先级：产物自己声明的 mime → 按扩展名推断 → 兜底二进制流。
    media_type = (meta.get('mime')
                  or mimetypes.guess_type(path.name)[0]
                  or 'application/octet-stream')
    inline = meta.get('kind') in {'video', 'audio'} or media_type.startswith(
        ('video/', 'audio/', 'text/html'))
    return FileResponse(path, media_type=media_type, filename=path.name,
                        content_disposition_type='inline' if inline else 'attachment')


@app.get('/api/runs/{run_id}')
async def read_run(run_id: str,
                   principal=Depends(policies.current_principal)):
    """run 的状态、计划与步骤（文档 §13）。"""
    run = await _owned_run(run_id, principal)
    plan = await hub.store.plan(run_id) or {}
    return {'run': run, 'plan': plan,
            'steps': await hub.store.steps_of(run_id),
            'actions': [a for a in (await hub.store.actions_of(run_id))]}


@app.post('/api/runs/{run_id}/pause')
async def pause_run(run_id: str,
                    principal=Depends(policies.current_principal)):
    """请求暂停。返回的是**库里的真实结果**，不是"已受理"。

    queued 的 run 立刻变 paused；running 的只登记请求，由执行者在下一处安全
    边界退出——所以这里回的 `status` 可能仍是 running（文档 §7.1）。
    """
    await _owned_run(run_id, principal)
    result = await hub.pause(run_id)
    if not result.get('ok'):
        raise HTTPException(
            status_code=409,
            detail=f'该运行当前状态为 {result.get("status") or "未知"}，不能暂停')
    return {'paused': True, **result}


@app.post('/api/runs/{run_id}/resume')
async def resume_run(run_id: str,
                     principal=Depends(policies.current_principal)):
    await _owned_run(run_id, principal)
    record = await hub.resume(run_id)
    if record is None:
        raise HTTPException(status_code=409, detail='没有可续跑的计划')
    return {'resumed': True, 'task_id': record.task_id, 'run_id': record.run_id}


def _input_error(exc) -> HTTPException:
    """把补充输入的业务错误翻译成 HTTP 状态码（文档 §6 错误合同）。"""
    code = getattr(exc, 'code', '')
    status = {'input_request_conflict': 409, 'run_finished': 409,
              'run_cancelled': 409, 'run_not_current': 409,
              'run_not_accepting': 409,
              'input_too_long': 422, 'input_empty': 422,
              'input_backlog': 429}.get(code, 400)
    headers = {'Retry-After': '2'} if status == 429 else None
    return HTTPException(status_code=status,
                         detail=f'{code}: {getattr(exc, "message", str(exc))}',
                         headers=headers)


def _inputs_enabled() -> None:
    if not settings.run_inputs_enabled:
        # 关掉时按"没有这个接口"处理，而不是 403：前端不需要区分"未开放/无权限"
        raise HTTPException(status_code=404, detail='运行中补充输入未开放')


@app.post('/api/runs/{run_id}/inputs', status_code=202)
async def append_run_input(run_id: str, payload: RunInputRequest,
                           principal=Depends(policies.current_principal)):
    """给当前任务补一句要求。**接受**只代表已落库，不代表已纳入计划。

    三种状态不能混用（文档 §2.2）：
    - `accepted`：数据库已提交，等执行者在下一个边界读取；
    - `applied`：基于新要求的计划已提交；
    - 业务完成：那是另一件事，要等 run 真正结束。
    """
    _inputs_enabled()
    await _owned_run(run_id, principal)
    try:
        result = await hub.append_run_input(
            run_id, request_id=payload.request_id, message=payload.message,
            principal=principal, task_id=payload.task_id or '')
    except run_inputs_module.InputConflict as exc:
        raise _input_error(exc) from None
    except run_inputs_module.InputRejected as exc:
        raise _input_error(exc) from None
    return {'input_id': result['input_id'], 'task_id': result['task_id'],
            'run_id': result['run_id'], 'input_seq': result['input_seq'],
            'intent_version': result['intent_version'],
            'processed_intent_version': result['processed_intent_version'],
            'status': result['status'], 'replayed': result['replayed']}


@app.get('/api/runs/{run_id}/inputs')
async def read_run_inputs(run_id: str, after_seq: int = 0, limit: int = 50,
                          principal=Depends(policies.current_principal)):
    """按序号回读补充输入与处理状态（前端切回来/重连后补齐显示）。"""
    _inputs_enabled()
    await _owned_run(run_id, principal)
    return await hub.run_inputs(run_id, after_seq=after_seq, limit=limit)


@app.post('/api/runs/{run_id}/compensate')
async def compensate_run(run_id: str,
                         principal=Depends(policies.current_principal)):
    """撤销这次运行**已经完成**的有副作用步骤（文档 §8.4）。

    撤销是一次独立执行：新建 run、走账本、发事件，原 run 只记 `compensated_at`。
    取消不会撤销已经发出去的写操作，要收回副作用就走这里。
    """
    await _owned_run(run_id, principal)
    try:
        result = await hub.compensate(run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if result is None:
        raise HTTPException(status_code=409, detail='没有可补偿的步骤')
    return {'compensated': True, **result}


@app.post('/api/runs/{run_id}/cancel')
async def cancel_run(run_id: str,
                     principal=Depends(policies.current_principal)):
    """取消：停止后续步骤，回执说明在途动作状态（文档 §8.4）。

    跨进程取消靠库里的 cancel_requested_at，执行者轮询到才会真的停。所以这里
    只承诺"请求已送达"，不再无条件把 run 标成 cancelled 假装成功。
    """
    await _owned_run(run_id, principal)
    if not await hub.cancel_run(run_id):
        raise HTTPException(status_code=409, detail='该运行已经结束，无法取消')
    return {'cancelled': True,
            'detail': '取消请求已发出；在途动作的回执仍会记录，不会重复发送'}


@app.get('/api/artifacts/{artifact_id}')
async def read_artifact(artifact_id: str,
                        principal=Depends(policies.current_principal)):
    """按引用读取产物元信息（文档 §13）。

    文件内容仍走任务产物下载接口；这里给的是"这个东西是什么、多大、
    哈希多少"的权威记录，便于界面与审计引用。
    """
    meta = await hub.store.artifact(artifact_id)
    if meta is None:
        raise HTTPException(status_code=404, detail='产物不存在')
    run = await hub.store.run(meta['run_id']) or {}
    policies.ensure_owner(
        {'tenant_id': run.get('tenant_id', 'default'),
         'owner_user_id': run.get('owner_user_id', 'default')}, principal, '产物')
    return meta


@app.get('/api/recovery')
async def recovery_status(principal=Depends(policies.current_principal)):
    _require_operator(principal)
    return getattr(app.state, 'recovery', {})


@app.get('/api/health')
async def health(principal=Depends(policies.current_principal)):
    """存活与态势：队列积压、待核实动作、失败作业都能看到。

    没有这个口子，上面那些状态只能手工查库——排障成本高到没人会去查。
    """
    _require_operator(principal)
    try:
        stats = await hub.store.stats()
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(status_code=503,
                            detail=f'数据库不可用（{type(exc).__name__}）') from None
    from app import async_contracts
    body = {'ok': True, 'owner': hub.owner_id,
            'recovery': getattr(app.state, 'recovery', {}),
            'mcp': tool_module.mcp_status(),
            'governance': {
                'auth_mode': settings.auth_mode,
                'dev_compat': settings.auth_dev_compat,
                'tool_policy_enforced': settings.tool_policy_enforced,
                'result_envelope_v2': settings.result_envelope_v2,
                'async_tool_runtime_enabled': settings.async_tool_runtime_enabled,
                'async_contracts': async_contracts.summary(),
                'max_concurrent_runs': settings.max_concurrent_runs,
                'inflight': hub._inflight(),
            },
            **stats}
    pool_stats = getattr(hub.store, 'pool_stats', None)
    if callable(pool_stats):
        # 连接池态势：只有 PostgreSQL 实现有池，SQLite 没有这个口子
        try:
            body['db_pool'] = pool_stats()
        except Exception:                        # noqa: BLE001
            body['db_pool'] = {'error': '取连接池状态失败'}
    return body


@app.get('/api/health/live')
async def health_live():
    """进程还在。不查数据库。"""
    return {'ok': True}


@app.get('/api/health/ready')
async def health_ready():
    """数据库能读才算就绪。"""
    try:
        await hub.store.stats()
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(status_code=503,
                            detail=f'数据库不可用（{type(exc).__name__}）') from None
    return {'ok': True}


@app.get('/api/profile')
async def read_profile(principal=Depends(policies.current_principal)):
    """当前的用户画像：原文 + 解析后的事实 + 版本号。"""
    with _profile_scope(principal):
        return {'text': profile.raw(), 'facts': profile.load(),
                'version': profile.version()}


@app.put('/api/profile')
async def write_profile(payload: ProfileRequest,
                        principal=Depends(policies.current_principal)):
    """整体覆盖画像。只解析"键：值"行，头部元信息由服务端重写。"""
    with _profile_scope(principal):
        facts = profile.parse(payload.text)
        try:
            version = profile.save(facts, expected_version=payload.version)
        except profile.ProfileConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
    return {'version': version, 'facts': facts, 'count': len(facts)}


@app.delete('/api/profile')
async def delete_profile(principal=Depends(policies.current_principal)):
    with _profile_scope(principal):
        profile.clear()
    return {'cleared': True}


@app.get('/api/sessions/{session_id}/memory')
async def read_memory(session_id: str,
                      principal=Depends(policies.current_principal)):
    """会话记忆：更早对话的压缩结果，可读可改。"""
    await _owned_session(session_id, principal)
    state = await hub.store.memory(session_id)
    return {'summary': state['summary'], 'facts': state['facts'],
            'facts_text': memory_module.render_facts(state['facts']),
            'upto': state['upto']}


@app.put('/api/sessions/{session_id}/memory')
async def write_memory(session_id: str, payload: MemoryRequest,
                       principal=Depends(policies.current_principal)):
    """手动改写会话记忆（摘要 + 关键事实）。"""
    await _owned_session(session_id, principal)
    facts = memory_module.parse_facts(payload.facts)
    current = await hub.store.memory(session_id)
    upto = current['upto']
    await hub.store.set_memory(session_id, summary=payload.summary, facts=facts,
                               upto=upto)
    return {'summary': payload.summary, 'facts': facts, 'upto': upto}


@app.delete('/api/sessions/{session_id}/memory')
async def delete_memory(session_id: str,
                        principal=Depends(policies.current_principal)):
    """清空会话记忆，下轮会从原文重新开始累积。"""
    await _owned_session(session_id, principal)
    await hub.store.clear_memory(session_id)
    return {'cleared': True}


@app.get('/api/sessions')
async def list_sessions(principal=Depends(policies.current_principal)):
    """会话列表（侧边栏用）：按最近活跃排序，含标题与消息数。"""
    return {'sessions': await hub.list_sessions(principal=principal)}


@app.get('/api/sessions/{session_id}/messages')
async def session_messages(session_id: str,
                           principal=Depends(policies.current_principal)):
    await _owned_session(session_id, principal)
    return {'session_id': session_id, 'messages': await hub.history(session_id)}


@app.get('/api/sessions/{session_id}/timeline')
async def session_timeline(session_id: str,
                           principal=Depends(policies.current_principal)):
    """会话回放：消息气泡 + 每轮执行过程事件，供切换会话时重建步骤面板。"""
    await _owned_session(session_id, principal)
    return await hub.session_timeline(session_id)


def _require_operator(principal) -> None:
    """运维身份来自 AUTH_OPERATORS 或库里的 operator 角色，不来自票据自称。"""
    raw = os.getenv('AUTH_OPERATORS', 'default')
    allowed = {item.strip() for item in raw.split(',') if item.strip()}
    if principal.user_id in allowed or principal.role == 'operator':
        return
    raise HTTPException(status_code=403, detail='需要运维身份（AUTH_OPERATORS）')


def _own_tenant(principal, tenant_id: str) -> None:
    if tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail='只能管理本企业的接入')


class ServerBindingRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=80)
    server_id: str = Field(min_length=1, max_length=80)
    transport: str = 'streamable_http'
    config_ref: str = ''
    enabled: bool = True


class CredentialBindingRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=80)
    server_id: str = Field(min_length=1, max_length=80)
    subject_kind: str = 'tenant'
    subject_id: str = ''
    credential_ref: str = Field(min_length=1, max_length=500)
    platform_id: str = Field(default='', max_length=80)


class ToolPolicyRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=80)
    tool_key: str = Field(min_length=1, max_length=200)
    role: str = ''
    user_id: str = ''
    approval_mode: str = 'auto'
    enabled: bool = True


class ApproveRequest(BaseModel):
    manifest_hash: str = Field(min_length=16, max_length=128)


def _public_approval(row: dict) -> dict:
    manifest = row.get('manifest_json') or row.get('manifest') or ''
    if isinstance(manifest, str) and manifest:
        try:
            manifest = json.loads(manifest)
        except ValueError:
            manifest = {}
    return {'approval_id': row.get('approval_id'),
            'run_id': row.get('run_id'),
            'status': row.get('status'),
            'manifest_hash': row.get('manifest_hash'),
            'manifest': manifest,
            'expires_at': row.get('expires_at')}


@app.post('/api/admin/server-bindings')
async def bind_server(payload: ServerBindingRequest,
                      principal=Depends(policies.current_principal)):
    _require_operator(principal)
    _own_tenant(principal, payload.tenant_id)
    row = await hub.store.upsert_server_binding(
        tenant_id=payload.tenant_id, server_id=payload.server_id,
        transport=payload.transport, config_ref=payload.config_ref,
        enabled=payload.enabled)
    return {'binding_id': row.get('binding_id'), 'server_id': row.get('server_id'),
            'enabled': row.get('enabled'), 'revision': row.get('revision')}


@app.post('/api/admin/credential-bindings')
async def bind_credential(payload: CredentialBindingRequest,
                          principal=Depends(policies.current_principal)):
    """只保存 credential_ref。响应里不回显引用里的明文。"""
    _require_operator(principal)
    _own_tenant(principal, payload.tenant_id)
    server = await hub.store.server_binding(payload.tenant_id, payload.server_id)
    if not server:
        raise HTTPException(status_code=404, detail='先登记服务端绑定')
    subject = payload.subject_id or payload.tenant_id
    row = await hub.store.upsert_credential_binding(
        tenant_id=payload.tenant_id, subject_kind=payload.subject_kind,
        subject_id=subject, server_binding_id=str(server.get('binding_id') or ''),
        credential_ref=payload.credential_ref,
        scope={'platforms': [payload.platform_id]} if payload.platform_id else {})
    return {'binding_id': row.get('binding_id'),
            'server_binding_id': row.get('server_binding_id'),
            'subject_kind': row.get('subject_kind'),
            'status': row.get('status')}


@app.post('/api/admin/tool-policies')
async def bind_policy(payload: ToolPolicyRequest,
                      principal=Depends(policies.current_principal)):
    _require_operator(principal)
    _own_tenant(principal, payload.tenant_id)
    row = await hub.store.upsert_tool_policy(
        tenant_id=payload.tenant_id, tool_key=payload.tool_key,
        role=payload.role, user_id=payload.user_id,
        approval_mode=payload.approval_mode, enabled=payload.enabled)
    return {'policy_id': row.get('policy_id'), 'tool_key': row.get('tool_key'),
            'enabled': row.get('enabled'), 'approval_mode': row.get('approval_mode')}


@app.get('/api/runs/{run_id}/approvals')
async def list_run_approvals(run_id: str,
                             principal=Depends(policies.current_principal)):
    run = await hub.store.run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail='运行不存在')
    await _owned_task(run['task_id'], principal)
    rows = await hub.store.approvals_for(
        tenant_id=principal.tenant_id, owner_user_id=principal.user_id)
    mine = [row for row in rows if row.get('run_id') == run_id]
    return {'approvals': [_public_approval(row) for row in mine]}


@app.get('/api/runs/{run_id}/external-tasks')
async def list_external_tasks(run_id: str,
                              principal=Depends(policies.current_principal)):
    run = await hub.store.run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail='运行不存在')
    await _owned_task(run['task_id'], principal)
    rows = await hub.store.external_tasks_of_run(run_id)
    return {'external_tasks': [
        {'external_task_id': row.get('external_task_id'),
         'remote_task_id': row.get('remote_task_id'),
         'state': row.get('state'),
         'tool_key': row.get('tool_key')} for row in rows]}


async def _owned_approval(approval_id: str, principal) -> dict:
    row = await hub.store.approval(approval_id)
    if not row or str(row.get('tenant_id') or '') != principal.tenant_id:
        raise HTTPException(status_code=404, detail='批准不存在')
    owner = str(row.get('owner_user_id') or '')
    requester = str(row.get('requester_user_id') or '')
    if principal.user_id not in {owner, requester}:
        raise HTTPException(status_code=404, detail='批准不存在')
    return row


@app.post('/api/approvals/{approval_id}/approve')
async def approve(approval_id: str, payload: ApproveRequest,
                  principal=Depends(policies.current_principal)):
    await _owned_approval(approval_id, principal)
    from app import approvals as approvals_module
    try:
        updated = await hub.store.approve_approval(
            approval_id, approver_user_id=principal.user_id,
            expected_manifest_hash=payload.manifest_hash)
    except approvals_module.ApprovalConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    body = _public_approval(updated)
    body['resumed'] = bool(updated.get('resumed'))
    return body


@app.post('/api/approvals/{approval_id}/reject')
async def reject_approval(approval_id: str,
                          principal=Depends(policies.current_principal)):
    await _owned_approval(approval_id, principal)
    updated = await hub.store.revoke_approval(
        approval_id, approver_user_id=principal.user_id, reject=True)
    return _public_approval(updated)


@app.get('/api/approvals/{approval_id}')
async def read_approval(approval_id: str,
                        principal=Depends(policies.current_principal)):
    return _public_approval(await _owned_approval(approval_id, principal))


@app.get('/api/ops/metrics')
async def ops_metrics(principal=Depends(policies.current_principal)):
    """队列、未知动作、批准积压、外部任务。不含费用。"""
    _require_operator(principal)
    try:
        stats = await hub.store.stats()
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(status_code=503,
                            detail=f'数据库不可用（{type(exc).__name__}）') from None
    return stats


@app.post('/api/ops/gc-results')
async def gc_results(principal=Depends(policies.current_principal)):
    """只删除超过宽限期、且没有 manifest 的结果目录。"""
    _require_operator(principal)
    from app import artifact_store
    removed = artifact_store.gc_unregistered(
        settings.artifact_dir, grace_seconds=settings.result_gc_grace_seconds)
    return {'removed': removed}


@app.get('/api/results/{result_id}')
async def read_staged_result(result_id: str,
                             principal=Depends(policies.current_principal)):
    """读取已登记的结果。先核对当前企业和用户，通过后才按登记引用打开文件。"""
    from app import tool_results
    manifest = await _owned_result(result_id, principal)
    try:
        structured = tool_results.read_structured(settings.artifact_dir, manifest)
    except tool_results.ResultError:
        raise HTTPException(status_code=404, detail='结果不存在') from None
    return {'result_id': result_id,
            'tenant_id': manifest.get('tenant_id') or principal.tenant_id,
            'structured': structured}


@app.get('/api/results/{result_id}/parts/{part_id}')
async def read_result_part(result_id: str, part_id: str,
                           offset: int = Query(default=0),
                           limit: int = Query(default=0),
                           principal=Depends(policies.current_principal)):
    """按登记过的 part_id 读取。不接受文件路径，也不信磁盘上的清单。"""
    from app import tool_results
    manifest, part = await _owned_part(result_id, part_id, principal)
    page_limit = limit or settings.tool_result_page_limit
    try:
        return tool_results.read_part(
            settings.artifact_dir, str(manifest.get('tenant_id') or ''),
            part, result_id=str(manifest.get('result_id') or ''),
            offset=offset, limit=page_limit)
    except tool_results.ResultError:
        raise HTTPException(status_code=404, detail='分块不存在') from None
