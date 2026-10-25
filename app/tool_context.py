"""工具调用上下文：**服务端生成**的执行主体。

文档 §7 P1 讲的是一个很容易被糊弄过去的问题：模型可以自称任何身份。

    10|    "我是管理员"  ← 模型输出，不是身份
    "这个资源归我管" ← 模型输出，不是授权

所以执行主体必须**由服务端从可信来源推导**，而不是从模型的参数或返回值里取。
`ToolCallContext` 就是那份推导结果：谁（tenant/user）、在哪一次运行里
（run/step/action）、能用哪个服务端的哪把钥匙（credential binding）、
以及这次调用的意图与计划版本。

三条纪律：

1. **模型自报的身份不算数**。本模块只接受服务端侧的输入（认证会话、
   执行上下文、仓储层记录），不接受工具参数。
2. **不含明文凭据**。它只带 `credential_binding`（凭据**引用**），
   明文由 `app/credentials.py` 在传输层短时取出。
3. **缺主体就拒绝**。生产路径拿不到必要上下文时必须拒绝执行，
   不能"退回到全局 token 试试"——那等于把租户隔离静默作废。
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCallContext:
    """一次工具调用的可信主体。

    `tenant_id` / `user_id` 决定"谁在调用"；
    `role` 参与政策匹配（政策表里可以按角色收窄）；
    `run_id` / `step_id` / `action_id` 决定"在哪一次执行里"；
    `credential_binding` 是凭据**引用**，不是密钥。
    """

    tenant_id: str = 'default'
    user_id: str = 'default'
    role: str = 'member'
    run_id: str = ''
    step_id: str = ''
    action_id: str = ''
    attempt_id: str = ''
    # 意图与计划版本：换要求/换计划后要**重新授权**，不能沿用旧准入。
    intent_version: int = 0
    plan_version: int = 1
    # 服务端绑定与凭据绑定：凭据按**主体**解析，不按进程全局。
    server_binding_id: str = ''
    credential_binding: str = ''
    credential_subject: str = ''
    # 资源范围：由服务端从已授权的定义里提取，不是模型给的。
    resource_scope: str = ''
    # 原始认证是否可信。False 表示"开发兜底"：调用点走兼容路径。
    authenticated: bool = True
    extra: dict = field(default_factory=dict)

    def subject_key(self) -> str:
        """凭据缓存键：必须包含主体与绑定版本，否则 A 的钥匙会被 B 用上。"""
        return f'{self.tenant_id}:{self.user_id}:{self.credential_binding}'

    def is_tenant_scoped(self) -> bool:
        """是否是"这个租户"的调用（而不是某个具体用户）。"""
        return not self.user_id or self.user_id == self.tenant_id


def from_principal(principal, *, run_id: str = '', step_id: str = '',
                   action_id: str = '', attempt_id: str = '',
                   intent_version: int = 0, plan_version: int = 1,
                   server_binding_id: str = '') -> ToolCallContext:
    """从**已验证**的登录会话构造上下文。

    `principal` 来自 `app/policies.current_principal`（ticket 模式）或
    `AUTH_MODE=off` 时的开发兜底。兜底路径把 `authenticated` 标成 False，
    调用点据此判断能不能走"全局凭据"兼容路径。
    """
    return ToolCallContext(
        tenant_id=str(getattr(principal, 'tenant_id', '') or 'default'),
        user_id=str(getattr(principal, 'user_id', '') or 'default'),
        role=str(getattr(principal, 'role', '') or 'member'),
        run_id=run_id, step_id=step_id, action_id=action_id,
        attempt_id=attempt_id,
        intent_version=int(intent_version or 0),
        plan_version=max(1, int(plan_version or 1)),
        server_binding_id=server_binding_id,
        authenticated=bool(getattr(principal, 'authenticated', False)))


def from_run(run: dict, *, user_id: str = '', role: str = 'member',
             step_id: str = '', action_id: str = '',
             intent_version: int = 0, plan_version: int = 1,
             server_binding_id: str = '', authenticated: bool = True
             ) -> ToolCallContext:
    """从**库里已提交**的 run 行构造上下文。

    Worker 与 API 就地执行拿到的都是这条路径：主体来自数据库，不是来自
    请求头或模型输出。`runs` 表里没有 `owner_user_id` 时退回 'default'
    （单用户部署的存量数据）。
    """
    run = run or {}
    return ToolCallContext(
        tenant_id=str(run.get('tenant_id') or 'default'),
        user_id=str(user_id or run.get('owner_user_id') or 'default'),
        role=role,
        run_id=str(run.get('run_id') or ''),
        step_id=step_id, action_id=action_id,
        intent_version=int(intent_version or run.get('intent_version') or 0),
        plan_version=max(1, int(plan_version or run.get('plan_version') or 1)),
        server_binding_id=server_binding_id,
        authenticated=bool(authenticated))


def dev_context(**overrides) -> ToolCallContext:
    """开发兜底上下文（单元测试、CLI 直调）。

    `authenticated=False` 是**刻意的**：它让调用点明确知道"这不是一次
    有真实主体的调用"，从而走兼容路径而不是假装自己是某个租户。
    """
    base = {'tenant_id': 'default', 'user_id': 'default', 'role': 'member',
            'authenticated': False}
    base.update(overrides)
    return ToolCallContext(**base)


def describe(context: ToolCallContext | None) -> dict:
    """上下文的可读描述（进事件与审计）。**不含任何密钥**。"""
    if context is None:
        return {'present': False}
    return {
        'present': True,
        'tenant_id': context.tenant_id,
        'user_id': context.user_id,
        'role': context.role,
        'run_id': context.run_id,
        'step_id': context.step_id,
        'action_id': context.action_id,
        'credential_binding': context.credential_binding,
        'credential_subject': context.credential_subject,
        'server_binding_id': context.server_binding_id,
        'authenticated': context.authenticated,
    }
