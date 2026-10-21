"""身份与权限：把"谁在操作"这条缝留出来。

**这条缝现在接上了真实认证**（`app.auth`）：请求里的 token 会去平台换 userinfo，
再映射成本地 `user_id`。`AUTH_MODE=off` 时退回原来的单用户行为，所以本地开发与
既有测试不受影响。

分三层，各自职责不混：

| 层 | 回答什么问题 | 在哪 |
|---|---|---|
| 认证 | 你是谁 | `app.auth.resolve()` |
| 对象权限 | 这条会话/任务/产物是不是你的 | `ensure_owner()` |
| 能力与额度 | 你能不能调这个工具、还有没有钱 | 待接入（见《多租户与能力中台方案.md》） |

`ensure_owner` 是**最后一道**拦截：所有按 ID 读取的接口都必须过它。漏掉一个入口，
就等于那个入口可以靠猜 ID 拿到别人的上下文。
"""
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config import settings

DEFAULT_TENANT = 'default'
DEFAULT_USER = 'default'


@dataclass(frozen=True)
class Principal:
    """一次请求的操作主体。

    `user_id` 是**本地** ID，不是平台 openid/sub。平台标识变过、换过入口，
    历史上下文都不会断——这是接认证时最容易做错的一步。
    """
    tenant_id: str = DEFAULT_TENANT
    user_id: str = DEFAULT_USER
    role: str = 'member'

    @property
    def anonymous(self) -> bool:
        return not self.user_id or self.user_id == DEFAULT_USER


async def current_principal(request: Request) -> Principal:
    """从请求解析主体。

    实现全部在 `app.auth`：这里只做转发，保证 20 多个 `Depends(...)` 调用点
    不用改。要换认证方式只改 auth，不动业务代码。
    """
    from app.auth import resolve
    return await resolve(request)


def ensure_owner(resource: dict | None, principal: Principal,
                 kind: str = '对象') -> dict:
    """对象级权限检查：不同租户/用户之间不能交叉读取。

    **不区分"不存在"和"无权限"**，统一 404：否则攻击者能靠状态码差异枚举出
    哪些 ID 是真实存在的。

    `POLICY_LEGACY_VISIBLE`（默认开）：`owner_user_id='default'` 的存量数据对所
    有登录用户可见。这是单用户升级期的兼容——升级前的数据本来就只属于一个人。
    **一旦有第二个真实用户，必须把它关掉**，否则等于所有历史数据公开。
    """
    if resource is None:
        raise HTTPException(status_code=404, detail=f'{kind}不存在')
    owner = resource.get('owner_user_id') or DEFAULT_USER
    tenant = resource.get('tenant_id') or DEFAULT_TENANT
    if principal.anonymous:
        return resource

    allowed_owners = {principal.user_id}
    if settings.policy_legacy_visible:
        allowed_owners.add(DEFAULT_USER)

    if tenant != principal.tenant_id or owner not in allowed_owners:
        raise HTTPException(status_code=404, detail=f'{kind}不存在')
    return resource
