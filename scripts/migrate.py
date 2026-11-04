"""Run once before API/Worker startup; safe when deployment jobs overlap.

`apply()` 结尾会核对活库结构（`migrations.verify_schema`），所以这个脚本不只是
"把版本号打上"，它同时充当**部署阶段的护栏**：只要有人只改了 `SCHEMA` 却没写
`UPGRADES`，这里就会以非零退出码 + 具体列名失败，而不是让缺列的库上线。
"""
import asyncio
import sys

import psycopg
from webpage_agent.core.config import settings
from webpage_agent.store import migrations
from webpage_agent.store.pg_store import SCHEMA, MIGRATIONS


async def main() -> int:
    if not settings.database_url:
        raise RuntimeError('DATABASE_URL is required')
    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        changed = await migrations.apply(conn, baseline=SCHEMA, legacy_ids=MIGRATIONS)
        report = await migrations.verify_schema(conn, SCHEMA)
    print('Applied: ' + (', '.join(changed) or 'already current'))
    print(f'Schema verified: {report["tables"]} tables / {report["columns"]} columns')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except migrations.SchemaDrift as exc:
        print(f'[migrate] 结构核对失败：{exc}', file=sys.stderr)
        sys.exit(2)
