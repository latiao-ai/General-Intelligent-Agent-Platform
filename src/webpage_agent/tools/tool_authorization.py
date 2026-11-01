"""工具授权的**单一装配点**：把政策、凭据、批准接到执行链上。

为什么单独一个模块（文档 §7 P4 / §8 A3）：

授权判定散落在调用链各处时，最容易出的错是**三处各判一次、结论不一致**：
`catalog()` 说这个工具可见，`run_step` 说缺凭据，`mcp_adapter` 又按进程级
token 发了出去。于是"模型看不见但远端被调用了"或者"明明有政策却走了全局
凭据"这类事故谁都说不清是谁放行的。

这里把装配集中起来：

- `resolve_binding()`：按**主体**解析凭据绑定（不退回全局 token）；
- `authorize_call()`：出一次判定，产出可留痕的授权凭据；
- `check_revocations()`：发送前再核对一次——发现后撤权要能阻断**新发送**。

三条纪律：

1. **拒绝路径的远端调用计数必须是零**。判定发生在调用之前。
2. **模型自报的身份无效**。主体只来自 `ToolCallContext`。
3. **批准按 action + 清单项预留**，同 action 的重放不重复扣。
"""
import logging
import time

from webpage_agent.core import credentials as credentials_module
from webpage_agent.tools import tool_policy as policy_module

logger = logging.getLogger(__name__)


class AuthorizationDenied(RuntimeError):
    """授权不通过。**远端调用计数必须为零**。

    继承 `PolicyDenied` 的语义（确定性 = not_executed）：本地就挡住了，
    远端根本没被碰到。
    """

    def __init__(self, message: str, *, code: str = 'E_POLICY_DENIED') -> None:
        super().__init__(message)
        self.safe_message = message
        self.error_code = code
        self.certainty = 'not_executed'
        self.phase = 'validate'


class ApprovalRequired(AuthorizationDenied):
    """需要明确批准，但当前没有有效的批准。

    这**不是失败**：动作还没有发出，用户确认后可以继续。
    """

    def __init__(self, message: str, *, approval_id: str = '',
                 code: str = 'E_APPROVAL_REQUIRED') -> None:
        super().__init__(message, code=code)
        self.approval_id = approval_id


async def resolve_binding(store, *, tenant_id: str, user_id: str,
                          server_binding_id: str = '',
                          subject_kind: str = '', platform_id: str = '',
                          owner_tenant_id: str = '') -> credentials_module.CredentialBinding | None:
    """按主体取凭据绑定。**取不到就是没有**，不退回全局 token。

    优先级：用户级绑定 > 租户级绑定。同一个主体可以换一把新钥匙（轮换），
    但不能换到别人的钥匙上——`CredentialBinding.matches()` 负责这件事。

    `owner_tenant_id` 是**归属租户**（绑定行挂在谁名下），只在通用服务的共享
    绑定上与调用租户不同：查库用它，租户维度仍按 `tenant_id`（调用方）判断。
    """
    if store is None:
        return None
    try:
        bindings = await store.credential_bindings(
            tenant_id=owner_tenant_id or tenant_id,
            server_binding_id=server_binding_id)
    except Exception:                                # noqa: BLE001
        logger.warning('读取凭据绑定失败 tenant=%s', tenant_id, exc_info=True)
        return None
    active = [b for b in bindings if b.get('status') == 'active']
    # 用户级优先：租户级是"这个租户共用一把"，用户级是"只给这一个人"。
    order = {'user': 0, 'tenant': 1}
    if subject_kind:
        active = [b for b in active if b.get('subject_kind') == subject_kind]
    active.sort(key=lambda b: (order.get(str(b.get('subject_kind')), 2),
                                str(b.get('binding_id'))))
    for row in active:
        binding = credentials_module.CredentialBinding(
            binding_id=str(row.get('binding_id') or ''),
            tenant_id=str(row.get('tenant_id') or 'default'),
            subject_kind=str(row.get('subject_kind') or 'tenant'),
            subject_id=str(row.get('subject_id') or 'default'),
            server_binding_id=str(row.get('server_binding_id') or ''),
            credential_ref=str(row.get('credential_ref') or ''),
            scope=credentials_module.parse_scope(row.get('scope')),
            revision=int(row.get('revision') or 1),
            status=str(row.get('status') or 'active'))
        if binding.matches(tenant_id=tenant_id, subject_id=user_id, platform_id=platform_id):
            return binding
    return None


async def load_policies(store, tenant_id: str) -> list[policy_module.ToolPolicy]:
    """读该租户的政策表，转成 `ToolPolicy`。"""
    if store is None:
        return []
    try:
        rows = await store.tool_policies(tenant_id)
    except Exception:                                # noqa: BLE001
        logger.warning('读取工具政策失败 tenant=%s', tenant_id, exc_info=True)
        return []
    out = []
    for row in rows:
        out.append(policy_module.ToolPolicy(
            policy_id=str(row.get('policy_id') or ''),
            tenant_id=str(row.get('tenant_id') or 'default'),
            tool_key=str(row.get('tool_key') or ''),
            role=str(row.get('role') or ''),
            user_id=str(row.get('user_id') or ''),
            resource_scope=str(row.get('resource_scope') or ''),
            effect=str(row.get('effect') or 'unknown'),
            approval_mode=str(row.get('approval_mode') or policy_module.APPROVAL_AUTO),
            limits=row.get('limits') or {},
            revision=int(row.get('revision') or 1),
            enabled=bool(row.get('enabled', True))))
    return out


UNIVERSAL_POLICY_ID = 'builtin:universal'


def _with_universal_default(policies, context, tool_key: str):
    """通用服务：没有登记政策时补一条**内置默认放行**。

    为什么不能像平台私有服务那样"没政策就拒绝"：通用服务的绑定是跨平台的，
    每接一个新平台/租户都要补一条政策，等于把"谁都能用"这件事又变成人工步骤，
    忘了补就是"部署功能突然不可用"。

    但仍然**只补默认、不夺权**：租户只要登记了命中这条工具的政策
    （哪怕是一条 `approval_mode='never'` 的拒绝政策），就按政策走。
    内置政策刻意用 `effect='unknown'` + `auto`：`effect='destructive'` 撞上
    "破坏性 + 永不批准"那条规则会被直接判死。
    """
    if policy_module.pick_policy(policies, tenant_id=context.tenant_id,
                                 tool_key=tool_key, role=context.role,
                                 user_id=context.user_id) is not None:
        return policies
    return list(policies) + [policy_module.ToolPolicy(
        policy_id=UNIVERSAL_POLICY_ID, tenant_id=context.tenant_id,
        tool_key=tool_key, effect='unknown',
        approval_mode=policy_module.APPROVAL_AUTO)]


def usable_approval(row: dict, *, tool_key: str, args_hash: str,
                    now_ts: float) -> dict | None:
    """清单项的工具和参数指纹都对上，并且批准本身还有效。"""
    from webpage_agent.governance import approvals as approvals_module
    if not approvals_module.is_valid(row, now_ts=now_ts):
        return None
    item = approvals_module.parse_manifest(row).item_for(
        tool_key=tool_key, args_hash=args_hash)
    if not item:
        return None
    return {'approval_id': str(row.get('approval_id') or ''),
            'item_key': str(item.get('key') or 'call')}


async def matching_approval(store, context, *, tool_key: str,
                            args: dict | None) -> dict | None:
    if store is None or context is None or not getattr(context, 'run_id', ''):
        return None
    if not hasattr(store, 'approvals_for'):
        return None
    args_hash = policy_module.canonical_hash(args or {})
    rows = await store.approvals_for(
        tenant_id=context.tenant_id, status='approved',
        owner_user_id=context.user_id)
    now_ts = time.time()
    for row in rows:
        if str(row.get('run_id') or '') != context.run_id:
            continue
        found = usable_approval(row, tool_key=tool_key, args_hash=args_hash,
                                now_ts=now_ts)
        if found:
            return found
    return None


async def authorize_call(store, context, *, tool_key: str, effect: str = 'unknown',
                         args: dict | None = None, action_id: str = '',
                         resource_scope: str = '',
                         honor_approval: bool = True) -> policy_module.PolicyDecision:
    """判定这次调用能不能发出去。

    返回**允许**的判定（带凭据与政策），不允许时抛 `AuthorizationDenied`。
    抛而不是返回 `allowed=False`：调用点必须在**发送之前**中止，
    静默返回 False 很容易被写成"继续跑吧"。
    """
    if not policy_module.enforcement_enabled():
        # 政策未强制：整条授权链走兼容路径（开发模式 / 旧部署）。
        # **不能只看 context is None**——有账本、有 run 行时 context 也是有的，
        # 但政策表是空的，decide() 会以"没有登记任何政策"默认拒绝，
        # 那等于把开关关掉之后反而什么都不许调。
        return policy_module.PolicyDecision(
            allowed=True, reason='政策未强制执行（开发兼容路径）')

    if context is None:
        # 强制模式下没有可信主体 = 拒绝。**不退回全局 token**。
        raise AuthorizationDenied(
            '缺少可信的执行主体，拒绝调用远端工具',
            code='E_NO_CONTEXT')

    policies = await load_policies(store, context.tenant_id)
    from webpage_agent.core import mcp_connections
    server_id = mcp_connections.server_id_of(tool_key)
    if server_id:
        credential = await mcp_connections.binding_for_server(
            store, tenant_id=context.tenant_id, user_id=context.user_id,
            server_id=server_id, platform_id=context.platform_id)
        require_credential = True
        if credential is not None and credential.universal:
            policies = _with_universal_default(policies, context, tool_key)
    else:
        # 本地工具不拿别的服务端的钥匙来凑「有凭据」。
        credential = None
        require_credential = False
    decision = policy_module.decide(
        tenant_id=context.tenant_id, tool_key=tool_key,
        role=context.role, user_id=context.user_id,
        policies=policies, credential=credential,
        require_credential=require_credential, args=args)
    if decision.requires_approval and honor_approval:
        matched = await matching_approval(
            store, context, tool_key=tool_key, args=args)
        if matched:
            return policy_module.PolicyDecision(
                allowed=True, reason='已有与本次参数匹配的批准',
                policy=decision.policy, credential=decision.credential,
                approval_id=matched['approval_id'],
                approval_item_key=matched['item_key'])
    if decision.requires_approval:
        raise ApprovalRequired(decision.reason, code=decision.code)
    if not decision.allowed:
        raise AuthorizationDenied(decision.reason, code=decision.code)
    return decision


async def check_revocations(store, decision: policy_module.PolicyDecision,
                            *, tool_key: str, tenant_id: str,
                            user_id: str, role: str = '', platform_id: str = '') -> None:
    """发送前再核对一次：政策被停用 / 凭据被撤销 → 阻断新发送。

    为什么要有第二次检查：发现（`catalog`）与发送之间隔着一次模型规划，
    人在这个窗口里撤了权。第一步通过不代表现在还能发。
    """
    if store is None or not policy_module.enforcement_enabled():
        return
    policies = await load_policies(store, tenant_id)
    current = policy_module.pick_policy(policies, tenant_id=tenant_id,
                                        tool_key=tool_key, role=role,
                                        user_id=user_id)
    universal = bool(decision.credential is not None
                     and getattr(decision.credential, 'universal', False))
    if current is None:
        # 通用服务没有登记政策是**正常状态**（内置默认放行）；平台私有服务
        # 没有政策说明政策被删了，必须拦住。
        if not universal:
            raise AuthorizationDenied(
                f'工具 {tool_key} 的政策在发送前被删除，拒绝发送',
                code='E_POLICY_REVOKED')
    elif not current.enabled:
        raise AuthorizationDenied(
            f'工具 {tool_key} 的政策在发送前被停用，拒绝发送',
            code='E_POLICY_REVOKED')
    if decision.credential is not None:
        from webpage_agent.core import mcp_connections
        try:
            binding = await mcp_connections.binding_for_server(
                store, tenant_id=tenant_id, user_id=user_id,
                server_id=mcp_connections.server_id_of(tool_key), platform_id=platform_id)
        except Exception as exc:
            raise AuthorizationDenied('发送前无法确认平台服务绑定，拒绝发送',
                                      code='E_CREDENTIAL_LOOKUP_FAILED') from exc
        if binding is None or binding.binding_id != decision.credential.binding_id:
            raise AuthorizationDenied('平台服务绑定已变更，拒绝发送', code='E_CREDENTIAL_REVOKED')
        binding_id = decision.credential.binding_id
        try:
            # 共享绑定挂在归属租户下：按调用租户查会查不到，被误判成"已删除"。
            rows = await store.credential_bindings(
                tenant_id=str(binding.tenant_id or tenant_id))
        except Exception as exc:                     # noqa: BLE001
            raise AuthorizationDenied(
                '发送前无法确认凭据是否仍有效，拒绝发送',
                code='E_CREDENTIAL_LOOKUP_FAILED') from exc
        for row in rows:
            if str(row.get('binding_id') or '') != binding_id:
                continue
            if str(row.get('status') or '') != 'active':
                raise AuthorizationDenied(
                    f'凭据绑定 {binding_id} 在发送前被撤销，拒绝发送',
                    code='E_CREDENTIAL_REVOKED')
            if int(row.get('revision') or 1) != decision.credential.revision:
                raise AuthorizationDenied(
                    f'凭据绑定 {binding_id} 在发送前被轮换，拒绝发送',
                    code='E_CREDENTIAL_REVOKED')
            return
        raise AuthorizationDenied(
            f'凭据绑定 {binding_id} 在发送前被删除，拒绝发送',
            code='E_CREDENTIAL_REVOKED')


async def record_authorization(store, context, *, tool_key: str,
                               action_id: str, args: dict | None = None,
                               decision: policy_module.PolicyDecision | None = None,
                               schema_hash: str = '', resource_scope: str = '',
                               approval_id: str = '') -> str:
    """留痕："这次调用凭什么被放行"。"""
    if store is None:
        return ''
    args_hash = policy_module.canonical_hash(args or {})
    resource = resource_scope or ''
    credential = decision.credential if decision else None
    try:
        return await store.record_authorization(
            action_id=action_id, tenant_id=context.tenant_id,
            user_id=context.user_id, tool_key=tool_key,
            schema_hash=schema_hash, args_hash=args_hash,
            resource_hash=policy_module.resource_hash(resource),
            credential_binding=credential.binding_id if credential else '',
            credential_subject=credential.subject_id if credential else '',
            credential_revision=credential.revision if credential else 0,
            policy_revision=(decision.policy.revision if decision and decision.policy else 0),
            approval_id=approval_id,
            expires_at=0.0, status='admitted')
    except Exception:                                # noqa: BLE001
        logger.warning('记录准入授权失败 action=%s', action_id, exc_info=True)
        return ''
