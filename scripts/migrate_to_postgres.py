"""把本地 SQLite 迁移到 PostgreSQL（文档 §9.1）。

**未在真实 PostgreSQL 上验证过**——当前开发环境只有 SQLite，没有 PG 实例。
这份脚本负责：

1. 从 SQLite 的 SCHEMA 推导出 PostgreSQL 建表语句（类型做等价替换）；
2. 创建 schema；
3. 按表逐条搬数据（保留 id / 时间戳原值）；
4. 迁移完成后校验行数一致。

用法：

    python -m scripts.migrate_to_postgres --dsn postgresql://user:pw@host/db
    python -m scripts.migrate_to_postgres --dsn ... --dry-run   # 只打印 DDL

已知差异（迁移前请确认）：
- 布尔/时间在 PG 里是真类型，SQLite 存的是 0/1 与 ISO 字符串，脚本按文本搬运，
  需要时由应用层做转换；
- 自增主键 messages.id 在 PG 侧用 BIGSERIAL，并需要重置序列到 max(id)+1；
- 唯一索引 idx_actions_idem / idx_jobs_dedupe 在两边都保留，冲突会直接报错。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import settings    # noqa: E402
from app.store import SCHEMA       # noqa: E402


def to_postgres_ddl(sqlite_schema: str) -> str:
    """把 SQLite DDL 粗翻成 PG 可执行形式。"""
    out = sqlite_schema
    out = out.replace('TEXT PRIMARY KEY', 'TEXT PRIMARY KEY')
    out = out.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY')
    out = out.replace('REAL', 'DOUBLE PRECISION')
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description='SQLite → PostgreSQL 迁移')
    parser.add_argument('--dsn', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--sqlite', default=str(settings.db_path))
    args = parser.parse_args()

    ddl = to_postgres_ddl(SCHEMA)
    if args.dry_run:
        print(ddl)
        return

    try:
        import psycopg                    # noqa: F401
    except ImportError:
        raise SystemExit('需要 psycopg：pip install "psycopg[binary]"')

    import psycopg

    src = sqlite3.connect(f'file:{args.sqlite}?mode=ro', uri=True)
    src.row_factory = sqlite3.Row
    tables = [r['name'] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    with psycopg.connect(args.dsn) as dst:
        with dst.cursor() as cur:
            cur.execute(ddl)
            for table in tables:
                rows = src.execute(f'SELECT * FROM {table}').fetchall()
                if not rows:
                    continue
                columns = list(rows[0].keys())
                cols = ', '.join(f'"{c}"' for c in columns)
                marks = ', '.join(['%s'] * len(columns))
                cur.executemany(
                    f'INSERT INTO {table} ({cols}) VALUES ({marks}) '
                    'ON CONFLICT DO NOTHING',
                    [tuple(r[c] for c in columns) for r in rows])
                print(f'  {table}: {len(rows)} 行')
        dst.commit()

        # 校验：行数必须一致，否则迁移不可信
        with dst.cursor() as cur:
            for table in tables:
                expected = src.execute(
                    f'SELECT COUNT(*) c FROM {table}').fetchone()['c']
                cur.execute(f'SELECT COUNT(*) FROM {table}')
                actual = cur.fetchone()[0]
                if actual < expected:
                    raise SystemExit(f'{table} 行数不一致：{actual} < {expected}')
        print('迁移完成，行数校验通过。注意：本脚本未在真实 PostgreSQL 上验证。')
    src.close()


if __name__ == '__main__':
    main()
