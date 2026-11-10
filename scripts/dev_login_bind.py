"""本地开发：把登录桩的平台用户绑到本机原来的单用户上（可重复执行）。

为什么需要这一步：`AUTH_MODE=off` 时所有数据都归 `tenant_id='default'` /
`owner_user_id='default'`（本机单用户）。一旦开 `AUTH_MODE=ticket`，登录进来的
是新身份 `u_xxxxxxxx`，对象权限会按归属判 404——你的历史会话和任务就"消失"了。

这个脚本做两件事，都是**只增不改**：

1. `app_users` 里补一行 `user_id='default'`（显示名用 `--name` 指定）；
2. `identities` 里补一行 `beeftv/dev-owner → user_id='default'`（tenant 也为 default），
   让登录桩返回的平台用户直接落到原来的单用户上。

执行：

    python -m scripts.dev_login_bind                 # 默认显示名"本机用户"
    python -m scripts.dev_login_bind --name 浩南
"""
import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT / 'src'))

DEV_SUBJECT = 'dev-owner'
DEV_PROVIDER = 'beeftv'
TARGET_USER = 'default'
TARGET_TENANT = 'default'


def main() -> int:
    parser = argparse.ArgumentParser(description='绑定本地登录桩身份到本机单用户')
    parser.add_argument('--name', default='本机用户', help='账号卡片上显示的名字')
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(_ROOT / '.env')
    dsn = os.getenv('DATABASE_URL', '').strip()
    if not dsn:
        print('DATABASE_URL 未配置，无法绑定', file=sys.stderr)
        return 1

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            'INSERT INTO app_users (user_id, display_name, email, status, '
            'created_at, updated_at) VALUES (%s, %s, %s, %s, now()::text, now()::text) '
            'ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name',
            (TARGET_USER, args.name, '', 'active'))
        # role 给 operator：本机单用户本来就该能看系统状态、登记绑定
        conn.execute(
            'INSERT INTO identities (provider, subject, user_id, tenant_id, '
            'identity_provider_id, email, role, created_at, last_seen_at) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, now()::text, now()::text) '
            'ON CONFLICT (provider, subject) DO UPDATE '
            '  SET user_id = EXCLUDED.user_id, tenant_id = EXCLUDED.tenant_id, '
            '      role = EXCLUDED.role',
            (DEV_PROVIDER, DEV_SUBJECT, TARGET_USER, TARGET_TENANT,
             DEV_PROVIDER, '', 'operator'))

        row = conn.execute(
            'SELECT count(*) FROM sessions WHERE tenant_id = %s AND owner_user_id = %s',
            (TARGET_TENANT, TARGET_USER)).fetchone()
        sessions = row[0] if row else 0

    print(f'绑定完成：{DEV_PROVIDER}/{DEV_SUBJECT} → {TARGET_TENANT}/{TARGET_USER}'
          f'（显示名：{args.name}）')
    print(f'这个身份名下有 {sessions} 个会话')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
