"""把历史数据里的"业务终态"迁移成通用状态（状态机词汇收口）。

背景：`tasks.status` / `runs.status` 以前直接存 `answered` / `local_ready` /
`deployed` / `deploy_needs_check` / `compensated`。这些是**展示词**，不该进
状态机——每加一类能力就得往里加一个值。现在：

- 库里只存通用状态（`app/outcome.py:RUN_STATUSES`）；
- 展示标签在读取时由 outcome 派生；
- 本脚本把旧行改写成通用值，并把 `deploy_needs_check` 那批被折成 `failed`
  的 run 纠正成 `needs_reconcile`（那是"结果待核实"，不是失败）。

用法：

```bash
python -m scripts.migrate_status_vocabulary              # 只报告，不改数据
python -m scripts.migrate_status_vocabulary --apply      # 真的写库
```

读的是 `.env` 里的 `DATABASE_URL`（只支持 PostgreSQL）。
"""
import argparse
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from app import outcome as outcome_module          # noqa: E402
from app.config import settings                    # noqa: E402

# 迁移后应当只剩这些值；出现别的说明还有代码在写业务词
TARGET_STATUSES = sorted(outcome_module.RUN_STATUSES)


def connect():
    dsn = (settings.database_url or '').strip()
    if not dsn.startswith(('postgresql://', 'postgres://')):
        raise SystemExit('需要 DATABASE_URL=postgresql://…（SQLite 已删除）')
    import psycopg
    return psycopg.connect(dsn, autocommit=True)


def _sql(sql: str) -> str:
    return sql.replace('?', '%s')


def _query(conn, sql, params=()):
    cur = conn.execute(_sql(sql), params)
    return cur.fetchall()


def _distinct(conn, table):
    rows = _query(conn, f'SELECT status, COUNT(*) FROM {table} '
                        f'GROUP BY status ORDER BY COUNT(*) DESC')
    return {row[0]: int(row[1]) for row in rows}


def _apply(conn, sql, params=()):
    cur = conn.execute(_sql(sql), params)
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true',
                       help='真的写库；不加只做只读检查')
    args = parser.parse_args(argv)

    conn = connect()
    try:
        return _run(conn, apply=args.apply)
    finally:
        try:
            conn.close()
        except Exception:                          # noqa: BLE001
            pass


def _run(conn, *, apply: bool) -> int:
    print(f'数据源：postgresql（{settings.database_url}）')

    tasks_before = _distinct(conn, 'tasks')
    runs_before = _distinct(conn, 'runs')
    print('\n迁移前 tasks.status：', tasks_before)
    print('迁移前 runs.status ：', runs_before)

    # 1) 先记下"部署待确认"那批任务：它们的 run 当年被折成了 failed，要单独纠正
    rethink = [row[0] for row in _query(
        conn, "SELECT task_id FROM tasks WHERE status='deploy_needs_check'")]

    plan = []
    for legacy, target in outcome_module.LEGACY_STATUS.items():
        count = tasks_before.get(legacy, 0)
        if count:
            plan.append(('tasks', legacy, target, count))

    unknown_tasks = set(tasks_before) - set(TARGET_STATUSES) - set(
        outcome_module.LEGACY_STATUS)
    unknown_runs = set(runs_before) - set(TARGET_STATUSES) - set(
        outcome_module.LEGACY_STATUS)

    if not apply:
        print('\n将要执行（--apply 才会写）：')
        for table, legacy, target, count in plan:
            print(f'  {table}: {legacy} → {target}  共 {count} 行')
        if rethink:
            print(f'  runs: 这 {len(rethink)} 个任务对应的 run 若为 failed，'
                  f'改判 needs_reconcile')
        if unknown_tasks or unknown_runs:
            print('\n⚠️ 还有无法识别的状态值（本脚本不动它们，请人工确认）：')
            for value in sorted(unknown_tasks):
                print(f'  tasks.{value} × {tasks_before[value]}')
            for value in sorted(unknown_runs):
                print(f'  runs.{value} × {runs_before[value]}')
        return 0

    changed = 0
    if rethink:
        # 先改 run：此时 tasks 里还留着 deploy_needs_check，能按 task_id 关联
        changed += _apply(conn,
                          "UPDATE runs SET status='needs_reconcile' "
                          "WHERE status='failed' AND task_id IN "
                          "(SELECT task_id FROM tasks "
                          " WHERE status='deploy_needs_check')")
    for legacy, target in outcome_module.LEGACY_STATUS.items():
        changed += _apply(conn, 'UPDATE tasks SET status=? WHERE status=?',
                          (target, legacy))
        changed += _apply(conn, 'UPDATE runs SET status=? WHERE status=?',
                          (target, legacy))

    print(f'\n已更新 {changed} 行。')
    print('迁移后 tasks.status：', _distinct(conn, 'tasks'))
    print('迁移后 runs.status ：', _distinct(conn, 'runs'))

    tasks_after = set(_distinct(conn, 'tasks'))
    runs_after = set(_distinct(conn, 'runs'))
    left = (tasks_after | runs_after) - set(TARGET_STATUSES)
    if left:
        print(f'⚠️ 仍有非通用状态：{sorted(left)}（本脚本不猜它们该变成什么）')
        return 1
    print('✅ 状态词汇已收口到通用集合。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
