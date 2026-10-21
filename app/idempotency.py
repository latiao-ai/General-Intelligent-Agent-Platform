"""创建任务的请求幂等。

问题很具体：用户双击"发送"、前端超时重试、或者中间代理重放一次，服务端看到的
是**两个一模一样的请求**。没有幂等键时就会建两个任务、跑两遍计划；如果计划里
有 `deploy_site`，线上真的会多出一个站点。

所以 `POST /api/tasks` 接受一个客户端生成的稳定键（`Idempotency-Key` 头，或
请求体的 `idempotency_key`），服务端按"主体 + 接口 + 键"占位：

| 情况 | 行为 |
|---|---|
| 键没出现过 | 占位 → 正常建任务 → 回填响应 |
| 键出现过、内容是同一个 | 直接返回上次的响应（`idempotent_replay: true`） |
| 键出现过、内容不同 | 409：这个键已经被用掉了，不能换内容复用 |
| 键占位了但没回填响应（进程崩在中间） | 按已有 task_id 重新组装响应，不新建任务 |

键只在 TTL 内有效（默认 24 小时）。这是有意的：永久记住会让客户端在某天
重用同一个键时莫名拿到一个陈年任务。
"""
import hashlib
import json
import time
from dataclasses import dataclass

# 键的长度上限。太长的键没有意义，还会被拿来塞垃圾数据。
MAX_KEY_LENGTH = 200


class IdempotencyError(RuntimeError):
    """键本身不合法（空、超长、含控制字符）。接口层映射为 400。"""


class IdempotencyConflict(IdempotencyError):
    """同一个键被用于不同的请求内容。接口层映射为 409。"""


class IdempotencyInFlight(IdempotencyError):
    """同一个键的请求正在处理中（占位有了、响应还没回填）。接口层映射为 409。"""


def fingerprint(payload) -> str:
    """请求内容指纹。只覆盖会影响执行结果的字段（消息 + 会话）。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def scope_key(tenant_id: str, owner_user_id: str, endpoint: str,
              client_key: str) -> str:
    """作用域化存储键：不同租户/用户/接口用同一个字符串也互不干扰。"""
    raw = f'{tenant_id}|{owner_user_id}|{endpoint}|{client_key}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def validate_key(client_key: str) -> str:
    key = (client_key or '').strip()
    if not key:
        raise IdempotencyError('幂等键不能为空')
    if len(key) > MAX_KEY_LENGTH:
        raise IdempotencyError(f'幂等键最长 {MAX_KEY_LENGTH} 个字符')
    if not key.isprintable():
        raise IdempotencyError('幂等键只能包含可打印字符')
    return key


@dataclass
class Reservation:
    """一次幂等占位。调用方要么 `complete`，要么 `release`。"""

    store: object
    scope: str
    row: dict
    created: bool = True

    @property
    def replayed(self) -> bool:
        return bool(self.row.get('response') or self.row.get('task_id'))

    @property
    async def response(self) -> dict | None:
        """上次的响应；占位后崩溃的情况按已存的 task/run 现场重组。"""
        if self.row.get('response'):
            return self.row['response']
        task_id = self.row.get('task_id')
        if not task_id:
            return None
        data = await self.store.task(task_id) or {}
        return {'task_id': task_id,
                'session_id': self.row.get('session_id') or data.get('session_id'),
                'run_id': self.row.get('run_id') or '',
                'status': data.get('status') or 'unknown'}

    async def complete(self, response: dict, *, task_id: str = '',
                       session_id: str = '', run_id: str = '') -> None:
        await self.store.complete_request_key(
            self.scope, response, task_id=task_id,
            session_id=session_id, run_id=run_id)
        self.row['response'] = response

    async def release(self) -> None:
        """失败撤销占位。不撤销的话，同一个键会永远返回一个不存在的任务。"""
        await self.store.release_request_key(self.scope)


async def reserve(store, *, principal, endpoint: str, client_key: str,
                  payload: dict, ttl_seconds: float) -> Reservation:
    """占位或取回已有记录。

    并发重复请求时，只有一个能插入成功；另一个读到同一行且没有响应——那是
    "正在处理中"。这里不排队等它：返回冲突让客户端稍后重试，比挂在服务端
    等一个未知长度的任务干净。
    """
    key = validate_key(client_key)
    scope = scope_key(getattr(principal, 'tenant_id', 'default'),
                      getattr(principal, 'user_id', 'default'),
                      endpoint, key)
    row = await store.reserve_request_key(
        scope, client_key=key,
        tenant_id=getattr(principal, 'tenant_id', 'default'),
        owner_user_id=getattr(principal, 'user_id', 'default'),
        endpoint=endpoint, fingerprint=fingerprint(payload),
        expires_at=time.time() + max(60.0, float(ttl_seconds)))

    if row['fingerprint'] != fingerprint(payload):
        raise IdempotencyConflict(
            '该幂等键已用于另一个请求（内容不同），请换一个键或直接重发原请求')
    created = bool(row.get('created', True))
    if not created and not row.get('response') and not row.get('task_id'):
        # 并发的重复请求：另一个请求占了这个键，但还没建完任务。
        # 不在这里排队等它——让客户端稍后重试，比挂住一个请求干净。
        raise IdempotencyInFlight('同一幂等键的请求正在处理中，请稍后重试')
    return Reservation(store=store, scope=scope, row=row, created=created)
