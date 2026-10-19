"""身份与权限：把"谁在操作"这条缝留出来。

**当前实现是单用户的**：没有接入真实登录，`current_principal()` 固定返回
default 租户 / default 用户。这一点必须说清楚——它是**可替换的缝**，不是
已经做完的多用户体系。

为什么现在就要留这条缝（文档 §10、§15 阶段 C）：

- 对象权限检查、租户过滤必须从一开始就出现在查询路径上，最后再补字段会漏；
- 但真实认证要身份提供方，本环境没有，所以只做到"缝已经存在、检查已经生效、
  单用户时恒通过"。

接真实登录时只需要替换 `current_principal()` 的实现，其余调用点不用改。
"""
from dataclasses import dataclass

from fastapi import Header, HTTPException

DEFAULT_TENANT = 'default'
DEFAULT_USER = 'default'


@dataclass(frozen=True)
class Principal:
    """一次请求的操作主体。"""
    tenant_id: str = DEFAULT_TENANT
    user_id: str = DEFAULT_USER

    @property
    def anonymous(self) -> bool:
        return not self.user_id or self.user_id == DEFAULT_USER


def current_principal(x_dsh_user: str | None = Header(default=None),
                      x_dsh_tenant: str | None = Header(default=None)
                      ) -> Principal:
    """从请求头取主体。

    **未接入真实认证**：这里仅把请求头当作身份声明，不做签名校验。生产环境
    必须换成经过验证的认证依赖（会话、JWT、网关注入），并且由服务端决定
    tenant/user，而不是信任客户端自报。
    """
    return Principal(tenant_id=x_dsh_tenant or DEFAULT_TENANT,
                     user_id=x_dsh_user or DEFAULT_USER)


def ensure_owner(resource: dict | None, principal: Principal,
                 kind: str = '对象') -> dict:
    """对象级权限检查：不同租户/用户之间不能交叉读取。

    单用户阶段恒通过；多用户接上以后这里就是真正的拦截点。
    """
    if resource is None:
        raise HTTPException(status_code=404, detail=f'{kind}不存在')
    owner = resource.get('owner_user_id') or DEFAULT_USER
    tenant = resource.get('tenant_id') or DEFAULT_TENANT
    if principal.anonymous:
        return resource
    if tenant != principal.tenant_id or owner not in (principal.user_id,
                                                      DEFAULT_USER):
        # 不区分"不存在"和"无权限"，避免泄漏对象是否存在
        raise HTTPException(status_code=404, detail=f'{kind}不存在')
    return resource
