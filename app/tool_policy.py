"""工具政策：模型只看得到被授权的工具，调用前再查一次。

三层职责（《智能体中台说明书》）：

| 层 | 回答什么问题 |
|---|---|
| 认证 | 你是谁（`app.auth`，不在这里） |
| 授权 | 你能用哪个服务端的哪个工具（**本模块**） |
| 额度 | 还有没有钱/次数（`app/approvals.py` 的预留 + 钱包） |

**授权必须落在代码里，不能落在提示词里。** `MCP_PROMPT_RULES` 是给模型的
提醒，模型不遵守照样出事：上游偷偷加一个 `delete_*`，规划器看到了就会用。
所以这里做的是**结构性**的事：

1. `catalog()` 按主体过滤——模型**看不见**未授权的工具，它连名字都不知道；
2. 调用前重新校验主体、凭据、绑定版本、批准——撤权后立刻阻断新发送；
3. 准入留痕（`tool_authorizations`）——事后能回答"这次调用凭什么被放行"。

三条纪律：

- **模型自报的身份不算数**。主体来自服务端会话/执行上下文；
- **后台 status/cancel 工具也要授权**。"内部工具"不是豁免理由；
- **未知 effect 默认不自动重试**，也不因为 readOnly 就当免费。
"""
import hashlib
import json
import logging
from dataclasses import dataclass, field

from app.config import settings
from app import credentials as credentials_module

logger = logging.getLogger(__name__)

# 批准方式。
APPROVAL_AUTO = 'auto'          # 策略允许，直接放行
APPROVAL_REQUIRED = 'required'  # 必须有明确批准
APPROVAL_NEVER = 'never'        # 永不允许（禁用）

VALID_APPROVAL_MODES = frozenset({APPROVAL_AUTO, APPROVAL_REQUIRED, APPROVAL_NEVER})


class PolicyDenied(RuntimeError):
    """授权不通过。**远端调用计数必须为零**。"""

    def __init__(self, message: str, *, code: str = 'E_POLICY_DENIED') -> None:
        super().__init__(message)
        self.safe_message = message
        self.error_code = code
        self.certainty = 'not_executed'
        self.phase = 'validate'


class ApprovalRequired(PolicyDenied):
    """需要明确批准，但当前没有有效的批准。"""

    def __init__(self, message: str, *, approval_id: str = '',
                 code: str = 'E_APPROVAL_REQUIRED') -> None:
        super().__init__(message, code=code)
        self.approval_id = approval_id


@dataclass(frozen=True)
class ToolPolicy:
    """一条工具政策。

    `tool_key` 为空表示"该租户的默认政策"；`role` / `user_id` 为空表示
    不限定。匹配时**从具体到一般**取第一条命中的。
    """

    policy_id: str
    tenant_id: str
    tool_key: str = ''
    role: str = ''
    user_id: str = ''
    resource_scope: str = ''
    effect: str = 'unknown'
    approval_mode: str = APPROVAL_AUTO
    limits: dict = field(default_factory=dict)
    revision: int = 1
    enabled: bool = True

    def matches(self, *, tenant_id: str, tool_key: str, role: str = '',
                user_id: str = '') -> bool:
        # 匹配范围与启用状态分开：停用的精确政策仍须覆盖服务默认政策，
        # 否则撤权会意外回退到通配放行。
        if self.tenant_id != tenant_id:
            return False
        server_wildcard = (self.tool_key.startswith('mcp:')
                           and self.tool_key.count(':') == 2
                           and self.tool_key.endswith(':*'))
        if self.tool_key and self.tool_key != tool_key and not (
                server_wildcard and tool_key.startswith(self.tool_key[:-1])):
            return False
        if self.role and self.role != role:
            return False
        if self.user_id and self.user_id != user_id:
            return False
        return True


@dataclass
class PolicyDecision:
    """一次授权判定的结果。

    `allowed=False` 时 `reason` 是**人话**（要告诉用户缺什么），不是异常类型名。
    """

    allowed: bool
    reason: str = ''
    policy: ToolPolicy | None = None
    credential: credentials_module.CredentialBinding | None = None
    requires_approval: bool = False
    approval_id: str = ''
    approval_item_key: str = ''
    code: str = ''

    def deny(self, code: str = 'E_POLICY_DENIED') -> 'PolicyDecision':
        return PolicyDecision(allowed=False, reason=self.reason,
                              policy=self.policy, credential=self.credential,
                              requires_approval=self.requires_approval,
                              approval_id=self.approval_id,
                              approval_item_key=self.approval_item_key, code=code)


def specificity(policy: ToolPolicy) -> int:
    """政策的具体程度：越具体优先级越高。

    比较顺序：指定工具 > 指定角色 > 指定用户 > 默认。数值越大越优先。
    """
    score = 0
    if policy.tool_key:
        score += 8 if policy.tool_key.endswith(':*') else 16
    if policy.user_id:
        score += 4
    if policy.role:
        score += 2
    if policy.resource_scope:
        score += 1
    return score


def pick_policy(policies, *, tenant_id: str, tool_key: str, role: str = '',
                user_id: str = '') -> ToolPolicy | None:
    """从政策表里挑出**最具体**的一条命中的。"""
    hits = [p for p in (policies or [])
            if p.matches(tenant_id=tenant_id, tool_key=tool_key, role=role,
                         user_id=user_id)]
    if not hits:
        return None
    hits.sort(key=specificity, reverse=True)
    return hits[0]


def canonical_hash(args: dict) -> str:
    """最终参数的规范化指纹：用于批准与审计。"""
    body = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def resource_scope_of(args: dict, definition: dict | None = None) -> str:
    """从最终参数里提取**资源范围**。

    只有在**声明了**哪些字段代表资源时才提取——没有合同就不能自动证明
    "这个对象归你管"。取不到就返回空串，由调用方按"未证明"处理。
    """
    definition = definition or {}
    fields = definition.get('resource_fields')
    if not fields:
        return ''
    parts = []
    for field_name in fields:
        value = (args or {}).get(field_name)
        if value in (None, ''):
            continue
        parts.append(f'{field_name}={value}')
    return '|'.join(parts)


def resource_hash(scope: str) -> str:
    if not scope:
        return ''
    return hashlib.sha256(scope.encode('utf-8')).hexdigest()[:32]


def default_policy(tool_key: str) -> ToolPolicy:
    """没有任何政策记录时的默认。

    默认**拒绝**：没有登记就意味着"还没授权"。但开发模式（政策未强制）下
    由调用点走兼容路径，不经过这里。
    """
    return ToolPolicy(policy_id='__default__', tenant_id='',
                      tool_key=tool_key, effect='unknown',
                      approval_mode=APPROVAL_NEVER)


def enforcement_enabled() -> bool:
    """政策是否强制执行。关闭时由调用点走"旧的全局凭据"兼容路径。"""
    return bool(settings.tool_policy_enforced)


def cache_ttl() -> float:
    return float(getattr(settings, 'credential_cache_ttl', 30.0) or 0.0)


def describe_decision(decision: PolicyDecision) -> dict:
    """授权判定的可读描述（进事件与审计）。**不含密钥**。"""
    return {
        'allowed': decision.allowed,
        'reason': decision.reason,
        'policy_id': decision.policy.policy_id if decision.policy else '',
        'policy_revision': decision.policy.revision if decision.policy else 0,
        'approval_mode': decision.policy.approval_mode if decision.policy else '',
        'credential_binding': (decision.credential.binding_id
                               if decision.credential else ''),
        'credential_revision': (decision.credential.revision
                                if decision.credential else 0),
        'requires_approval': decision.requires_approval,
        'approval_id': decision.approval_id,
        'code': decision.code,
    }


# ---------- 发现过滤（P2） ----------

def visible_to(policy: ToolPolicy | None, tool_key: str, *, tenant_id: str,
               role: str = '', user_id: str = '') -> bool:
    """这个主体能不能**看见**这个工具。

    看不见比拒绝更重要：模型只要看得见名字，就可能去规划它。名称都不给，
    "上游偷偷加一个 delete_*，规划器看到了就会用"这件事就不会发生。

    停用或明确禁止的政策不可见，需要批准的工具仍可供规划。
    没有匹配政策时，仅在强制模式关闭时默认可见。
    """
    if policy is None:
        return not enforcement_enabled()
    return (policy.enabled and policy.approval_mode != APPROVAL_NEVER
            and policy.matches(tenant_id=tenant_id, tool_key=tool_key,
                               role=role, user_id=user_id))


def filter_catalog(tools, policies, *, tenant_id: str, role: str = '',
                   user_id: str = '') -> tuple[list, list[str]]:
    """按主体过滤工具清单：返回 (可见的工具, 被隐藏的原因说明)。

    被隐藏的工具**连名字都不给模型**，但原因会进事件——运维要能回答
    "为什么模型没看到部署工具"。
    """
    visible, hidden = [], []
    for tool in tools:
        key = getattr(tool, 'tool_key', '') or getattr(tool, 'name', '')
        policy = pick_policy(policies, tenant_id=tenant_id, tool_key=key,
                             role=role, user_id=user_id)
        if visible_to(policy, key, tenant_id=tenant_id, role=role, user_id=user_id):
            visible.append(tool)
        else:
            hidden.append(key)
    return visible, hidden


# ---------- 准入判定（P4） ----------

def scope_satisfied(scope: str, args: dict | None) -> bool:
    """资源范围是逗号分隔的字段名时，这些字段必须出现在最终参数里。

    空范围表示不额外限制。写成句子的范围只是说明，不在这里当表达式执行。
    """
    import re
    text = (scope or '').strip()
    if not text or text in ('*', 'any'):
        return True
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(,[A-Za-z_][A-Za-z0-9_]*)*', text):
        return True
    values = args or {}
    for name in text.split(','):
        if values.get(name) in (None, ''):
            return False
    return True


def decide(*, tenant_id: str, tool_key: str, role: str = '', user_id: str = '',
           policies=None, credential=None,
           require_credential: bool = True,
           args: dict | None = None) -> PolicyDecision:
    """判定"这次调用能不能放行"。

    顺序是有讲究的：**先看政策，再看凭据**。

    1. 没有匹配政策 → 拒绝（"没登记就是没授权"）；
    2. 政策禁用 → 拒绝；
    3. 政策要求批准 → 返回 `requires_approval`，由调用点去预留额度；
    4. 需要凭据但没有 → 拒绝。

    **绝不**因为"缺凭据"就退回进程级全局 token：那是把所有租户的权限
    又合到一起（文档 §7 P3）。生产缺绑定就拒绝。
    """
    policies = list(policies or [])
    policy = pick_policy(policies, tenant_id=tenant_id, tool_key=tool_key,
                         role=role, user_id=user_id)
    if policy is None:
        return PolicyDecision(
            allowed=False,
            reason=f'没有为工具 {tool_key} 登记任何政策，默认拒绝',
            code='E_NO_POLICY').deny('E_NO_POLICY')
    if not policy.enabled:
        return PolicyDecision(
            allowed=False,
            reason=f'工具 {tool_key} 的政策已停用',
            policy=policy, code='E_POLICY_DISABLED').deny('E_POLICY_DISABLED')
    if policy.effect == 'destructive' and policy.approval_mode == APPROVAL_NEVER:
        # 破坏性操作 + 永不批准 = 明确禁止（不是"忘了批准"）。
        return PolicyDecision(
            allowed=False,
            reason=f'工具 {tool_key} 是破坏性操作且政策不允许批准',
            policy=policy, code='E_EFFECT_FORBIDDEN').deny('E_EFFECT_FORBIDDEN')
    if require_credential and credential is None:
        return PolicyDecision(
            allowed=False,
            reason=f'工具 {tool_key} 没有可用的凭据绑定（不退回全局凭据）',
            policy=policy, code='E_NO_CREDENTIAL').deny('E_NO_CREDENTIAL')
    if credential is not None and not credential.scope_allows(tool_key):
        return PolicyDecision(
            allowed=False,
            reason=f'凭据绑定 {credential.binding_id} 的范围不包含工具 {tool_key}',
            policy=policy, credential=credential,
            code='E_CREDENTIAL_SCOPE').deny('E_CREDENTIAL_SCOPE')
    if args is not None and not scope_satisfied(policy.resource_scope, args):
        return PolicyDecision(
            allowed=False,
            reason=f'工具 {tool_key} 的对象不在已登记的资源范围内',
            policy=policy, credential=credential,
            code='E_OBJECT_SCOPE').deny('E_OBJECT_SCOPE')
    if policy.approval_mode == APPROVAL_REQUIRED:
        return PolicyDecision(
            allowed=False,
            reason=f'工具 {tool_key} 需要明确批准后才能调用',
            policy=policy, credential=credential,
            requires_approval=True, code='E_APPROVAL_REQUIRED').deny(
                'E_APPROVAL_REQUIRED')
    if policy.approval_mode == APPROVAL_NEVER:
        return PolicyDecision(
            allowed=False,
            reason=f'工具 {tool_key} 的政策不允许调用',
            policy=policy, credential=credential,
            code='E_APPROVAL_NEVER').deny('E_APPROVAL_NEVER')
    return PolicyDecision(allowed=True,
                          reason=f'政策 {policy.policy_id} 允许调用',
                          policy=policy, credential=credential)
