"""本地账号的种子 / 运维脚本：不依赖已有超管会话，直接连库建号。

为什么需要它：控制台「手动建号」要求先以 platform_admin 登录，而第一个管理员
本身可能还不存在——鸡生蛋。这个脚本和 `grant_admin.py` 一样，把「第一把钥匙」
放在 shell 权限上，而不是放到一个谁都能调的接口里。

用法：

    python -m scripts.local_account_seed --username test --password test
    python -m scripts.local_account_seed --username admin --password 12345678 \
        --role platform_admin
    python -m scripts.local_account_seed --list

本机想用 4 位密码（如 test/test），在 .env 里设 LOCAL_AUTH_MIN_LENGTH=4；
生产环境代码强制 >= 8，脚本也拦不住（校验走同一套 `validate_password`）。
也可以用 LOCAL_AUTH_SEED_USERS=user:pass[:role] 让服务首次启动自动建号。
"""
import argparse
import asyncio
import sys

from webpage_agent.api import local_auth
from webpage_agent.core.config import settings
from webpage_agent.store.pg_store import PostgresStore

ROLES = ('member', 'platform_admin')


async def _list(store) -> int:
    data = await store.local_account_list(limit=500)
    rows = data['accounts']
    if not rows:
        print('库里还没有本地账号。')
        return 0
    print(f'{"username":20} {"user_id":22} {"角色/状态":18} 最近登录')
    print('-' * 86)
    for row in rows:
        print(f'{row["username"]:20} {row["user_id"]:22} '
              f'{row["status"]:10} {row["last_login_at"] or "—"}')
    print(f'共 {data["total"]} 个')
    return 0


async def _create(store, args) -> int:
    try:
        name = local_auth.normalize_username(args.username)
        password = local_auth.validate_password(args.password)
        row = await store.create_local_account(
            username=name, password_hash=local_auth.hash_password(password),
            display_name=args.display_name or name, role=args.role,
            tenant_id=settings.local_auth_tenant_id,
            identity_provider_id=settings.local_auth_identity_provider_id,
            must_change_password=args.must_change,
            created_by='cli:seed')
    except local_auth.LocalAuthError as exc:
        print(f'参数不合规：{exc.detail}', file=sys.stderr)
        if '至少' in exc.detail and settings.app_env != 'production':
            print('本机可以在 .env 里加 LOCAL_AUTH_MIN_LENGTH=4 后重试',
                  file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(f'已创建：{row["username"]} → {row["user_id"]} '
          f'（角色 {row["role"]}，租户 {row["tenant_id"]}）')
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description='本地账号建号 / 列表')
    parser.add_argument('--username', help='登录账号（3-32 位小写字母数字 ._-）')
    parser.add_argument('--password', help='登录密码')
    parser.add_argument('--display-name', default='', help='显示名，默认同账号')
    parser.add_argument('--role', choices=ROLES, default='member')
    parser.add_argument('--must-change', action='store_true',
                        help='首次登录强制改密')
    parser.add_argument('--list', action='store_true', help='列出现有本地账号')
    args = parser.parse_args()

    if not settings.database_url:
        print('DATABASE_URL 未配置', file=sys.stderr)
        return 1
    if not args.list and (not args.username or not args.password):
        parser.print_help()
        return 1

    store = PostgresStore(settings.database_url)
    try:
        await store.ready()
        return await (_list(store) if args.list else _create(store, args))
    finally:
        closing = getattr(store, 'close', None)
        if callable(closing):
            await closing()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
