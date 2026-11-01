"""Run once before API/Worker startup; safe when deployment jobs overlap."""
import asyncio
import psycopg
from webpage_agent.core.config import settings
from webpage_agent.store.pg_store import SCHEMA, MIGRATIONS
from webpage_agent.store import migrations


async def main():
    if not settings.database_url:
        raise RuntimeError('DATABASE_URL is required')
    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        changed = await migrations.apply(conn, baseline=SCHEMA, legacy_ids=MIGRATIONS)
    print('Applied: ' + (', '.join(changed) or 'already current'))


if __name__ == '__main__':
    asyncio.run(main())
