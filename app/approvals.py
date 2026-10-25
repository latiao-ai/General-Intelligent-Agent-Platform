"""结构化批准：模型只能**提出**，人能**确认**，额度按动作原子预留。

为什么不能靠自然语言（《智能体中台说明书》）：

用户说"你继续吧"，模型在下一步就可能调用一个代价完全不同的工具。让模型
自己声明"用户已同意"等于让被约束方来当裁判。所以：

- **模型只能提出 approval request**。请求里是一份**有限清单**：哪些工具、
  哪些对象、精确参数或有限参数范围、目标数量、期限、次数；
- **批准由服务端记录**：谁来批、什么时候批、批的是哪份清单的哪一个 hash；
- **消费在事务里**：按 `(approval_id, action_id, manifest_item_key)` 预留，
  两个并发动作不能超用同一份批准；同一个 action 的重放不重复扣。

额度释放的规则只有一条：**可靠地确认没执行才释放**。结果未知（unknown）
必须**保持预留**——把它释放给第二个动作，等于让一笔可能已经花掉的钱被花两次。
"""
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)

# 批准状态机。
STATUS_PENDING = 'pending'
STATUS_APPROVED = 'approved'
STATUS_REJECTED = 'rejected'
STATUS_REVOKED = 'revoked'
STATUS_EXPIRED = 'expired'
STATUS_CONSUMED = 'consumed'

# 预留状态。
RESERVED = 'reserved'
CONSUMED = 'consumed'
RELEASED = 'released'
# unknown 时保持 reserved：既不消费也不释放。


class ApprovalConflict(RuntimeError):
    """批准被并发占用或内容冲突（调用方应回 409）。"""


class ApprovalMissing(RuntimeError):
    """没有可用的批准。"""


@dataclass
class ApprovalManifest:
    """一份**有限**的批准清单。

    `items` 是这次批准覆盖的动作项，每项含：
    - `key`：清单内的稳定标识（用于唯一约束）；
    - `tool_key`：允许的工具；
    - `args_hash`：最终参数的**精确**指纹（首版不做范围匹配）；
    - `resource_scope`：资源范围声明；
    - `units`：额度（次数/数量），默认 1。

    首版刻意**不**开放任意规则 DSL：能表达"这批对象、这个参数、这么多次"
    就够用，表达力越大，被模型误解的机会越多。
    """

    items: list[dict] = field(default_factory=list)
    expires_at: float = 0.0
    max_actions: int = 0
    limits: dict = field(default_factory=dict)

    def hash(self) -> str:
        body = json.dumps({'items': self.items, 'expires_at': self.expires_at,
                           'limits': self.limits,
                           'max_actions': int(self.max_actions or 0)},
                          ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(body.encode('utf-8')).hexdigest()

    def item_for(self, *, tool_key: str, args_hash: str) -> dict | None:
        """找到与这次调用**完全匹配**的清单项。

        参数变了（换模型、换对象、换数量）就匹配不到——那需要重新批准。
        """
        for item in self.items:
            if str(item.get('tool_key') or '') != tool_key:
                continue
            if str(item.get('args_hash') or '') == args_hash:
                return item
        return None


def build_manifest(*, tool_key: str, args: dict, resource_scope: str = '',
                   units: int = 1, ttl_seconds: int = 0,
                   item_key: str = '', limits: dict | None = None) -> ApprovalManifest:
    """从一次调用的最终参数构造清单。"""
    import time
    ttl = ttl_seconds or int(getattr(settings, 'approval_ttl_seconds', 900))
    body = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    args_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
    key = item_key or f'{tool_key}:{args_hash[:12]}'
    return ApprovalManifest(
        items=[{'key': key, 'tool_key': tool_key, 'args_hash': args_hash,
                'resource_scope': resource_scope, 'units': int(units or 1)}],
        expires_at=time.time() + ttl,
        max_actions=int(getattr(settings, 'approval_max_actions', 20)),
        limits=dict(limits or {}))


def new_approval_id() -> str:
    return 'apr_' + uuid.uuid4().hex[:16]


def is_valid(row: dict, *, now_ts: float) -> bool:
    """这份批准现在还能用吗？"""
    if not row:
        return False
    if str(row.get('status') or '') != STATUS_APPROVED:
        return False
    expires = float(row.get('expires_at') or 0)
    if expires and expires <= now_ts:
        return False
    return True


def parse_manifest(row: dict) -> ApprovalManifest:
    raw = row.get('manifest_json') or '{}'
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        data = {}
    items = data.get('items') or []
    return ApprovalManifest(
        items=[item for item in items if isinstance(item, dict)],
        expires_at=float(data.get('expires_at') or row.get('expires_at') or 0),
        max_actions=int(data.get('max_actions') or 0),
        limits=data.get('limits') or {})


def manifest_hash(manifest: ApprovalManifest) -> str:
    return manifest.hash()


def describe(row: dict) -> dict:
    """批准的可读视图（给 API 与界面）。**不含**密钥或原始正文。"""
    if not row:
        return {}
    manifest = parse_manifest(row)
    return {
        'approval_id': row.get('approval_id'),
        'status': row.get('status'),
        'run_id': row.get('run_id'),
        'requester_user_id': row.get('requester_user_id'),
        'approver_user_id': row.get('approver_user_id'),
        'manifest_hash': row.get('manifest_hash'),
        'policy_revision': row.get('policy_revision'),
        'expires_at': row.get('expires_at'),
        'limits': manifest.limits,
        'items': [{'key': item.get('key'), 'tool_key': item.get('tool_key'),
                   'resource_scope': item.get('resource_scope'),
                   'units': item.get('units')}
                  for item in manifest.items],
        'created_at': row.get('created_at'),
        'approved_at': row.get('approved_at'),
        'revoked_at': row.get('revoked_at'),
    }


def scope_text(manifest: ApprovalManifest) -> str:
    """一句话说明这份批准覆盖什么（给事件与界面）。"""
    if not manifest.items:
        return '（空清单）'
    parts = []
    for item in manifest.items[:3]:
        text = str(item.get('tool_key') or '')
        scope = str(item.get('resource_scope') or '')
        if scope:
            text += f' · {scope}'
        units = item.get('units')
        if units and int(units) > 1:
            text += f' ×{units}'
        parts.append(text)
    return '；'.join(parts)
