"""给某个用户授予 / 回收管理角色。控制台的"第一把钥匙"就靠它。

为什么是脚本而不是接口：**第一个管理员不能由接口产生**。任何"提升第一个人"的
接口，要么谁都能调（等于没有权限），要么需要一个前置管理员（死循环）。所以这一步
必须有 shell 访问权——这恰好就是运维的信任边界。

用法：

    python -m scripts.grant_admin --list
    python -m scripts.grant_admin --user u_abc123 --role platform_admin
    python -m scripts.grant_admin --user u_abc123 --role member
    python -m scripts.grant_admin --user u_abc123 --role operator --tenant tenant-a

角色说明：

| 角色 | 能做什么 |
|---|---|
| `platform_admin` | 控制台全部视图，跨租户 |
| `operator` | 只看本租户用户；不能授予 platform_admin |
| `member` | 普通用户，进不了控制台 |

两条硬保护与接口层一致：不能降掉最后一个 `platform_admin`；`--tenant` 留空时
改的是这个用户的**全部**身份（同一个 user_id 可能在多个企业下都有绑定）。
"""
import argparse
import asyncio
import sys

from webpage_agent.core.config import settings
from webpage_agent.store.pg_store import PostgresStore

ROLES = ('member', 'operator', 'platform_admin')


async def _list(store) -> int:
    async with store._lease() as conn:
        rows = await store._all(
            conn,
            'SELECT u.user_id, u.display_name, u.status, i.tenant_id, i.role, '
            '       i.last_seen_at '
            'FROM app_users u LEFT JOIN identities i ON i.user_id=u.user_id '
            'ORDER BY i.role DESC, u.user_id', ())
    if not rows:
        print('库里还没有用户。先让目标用户从平台登录一次，才有 identities 记录。')
        return 0
    print(f'{"user_id":24} {"角色":15} {"租户":14} {"状态":8} 昵称')
    print('-' * 92)
    for row in rows:
        print(f'{row["user_id"]:24} {row["role"] or "-":15} '
              f'{row["tenant_id"] or "-":14} {row["status"] or "-":8} '
              f'{row["display_name"] or ""}')
    return 0


async def _grant(store, *, user_id: str, role: str, tenant_id: str, reason: str) -> int:
    try:
        result = await store.set_user_role(
            user_id=user_id, role=role, tenant_id=tenant_id,
            actor_id='cli:grant_admin', reason=reason)
    except LookupError as exc:
        print(f'[grant] 失败：{exc}', file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f'[grant] 拒绝：{exc}', file=sys.stderr)
        return 3
    print(f'已把 {user_id} 设为 {result["role"]}'
          f'（范围 {result["tenant_id"]}，影响 {result["updated"]} 条身份记录）')
    print('对方不用重新登录：角色是每次请求现读 identities 的。')
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description='管理控制台的管理员授权')
    parser.add_argument('--list', action='store_true', help='列出所有用户与角色')
    parser.add_argument('--user', help='目标 user_id（app_users.user_id）')
    parser.add_argument('--role', choices=ROLES, help='要授予的角色')
    parser.add_argument('--tenant', default='', help='只改这个租户下的身份；留空表示全部')
    parser.add_argument('--reason', default='cli', help='写进审计的原因')
    args = parser.parse_args()

    if not settings.database_url:
        print('DATABASE_URL 未配置', file=sys.stderr)
        return 1

    store = PostgresStore(settings.database_url)
    try:
        await store.ready()
        if args.list:
            return await _list(store)
        if not args.user or not args.role:
            parser.print_help()
            return 1
        return await _grant(store, user_id=args.user, role=args.role,
                            tenant_id=args.tenant, reason=args.reason)
    finally:
        closing = getattr(store, 'close', None)
        if callable(closing):
            await closing()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
