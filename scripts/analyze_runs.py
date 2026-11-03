"""只读分析生产库里的工具调用与规划质量。

回答两个问题：

1. **结果未定率高不高**（`external_tasks.state`）——这是"可能已经在客户系统生效，
   但我们不知道"的唯一信号，按服务端分组看。
2. **规划质量**（`run_decisions`）——修订率、决策密度、决策类型分布、重复决策。

只读：只用 SELECT，不建表、不写库、不迁移。可以被任何人随时跑。

用法::

    python -m scripts.analyze_runs                      # 读 DATABASE_URL
    python -m scripts.analyze_runs --dsn postgresql://…
    python -m scripts.analyze_runs --json               # 机器可读输出
    python -m scripts.analyze_runs --days 7             # 只看最近 7 天（按 created_at）
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_ROOT / 'src'))

# 结果未定的判定依据：external_tasks.state 的合法取值（见 pg_store 的 DDL 与
# core/external_tasks.py 的写入点）。这里只区分"还需要人核实"和"已收敛"。
UNRESOLVED_STATES = ('needs_reconcile',)
OPEN_STATES = ('accepted', 'running', 'materializing')

# tool_key 的两种形态（见 tools/core.py 与 capabilities/registry.py）：
#   mcp:<server_id>:<tool>      远端工具
#   local:<capability>:<action> 本地能力包
# 不用 LIKE '%'——psycopg 会把裸 % 当占位符，split_part 既安全又不需要转义。
SERVER_SQL = ("CASE WHEN split_part(tool_key, ':', 1) IN ('mcp', 'local') "
              "THEN split_part(tool_key, ':', 1) || ':' || split_part(tool_key, ':', 2) "
              "ELSE '(未标注)' END")


def _window(days: int | None) -> tuple[str, list]:
    """created_at 是 TEXT 存的 ISO 时间，按字符串比较即可（同一时区格式）。"""
    if not days:
        return '', []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return ' WHERE created_at >= %s', [since]


async def _fetch(conn, sql, params):
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def collect(dsn: str, days: int | None) -> dict:
    import psycopg
    where, params = _window(days)
    out: dict = {'window_days': days}

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        # ---------- 1. 结果未定率 ----------
        rows = await _fetch(conn, f'SELECT state, count(*) FROM external_tasks{where} '
                                  'GROUP BY state ORDER BY 2 DESC', params)
        by_state = {str(r[0]): int(r[1]) for r in rows}
        total_ext = sum(by_state.values())
        unresolved = sum(by_state.get(s, 0) for s in UNRESOLVED_STATES)
        out['external_tasks'] = {
            'total': total_ext,
            'by_state': by_state,
            'unresolved': unresolved,
            'unresolved_rate': round(unresolved / total_ext, 4) if total_ext else None,
            'still_open': sum(by_state.get(s, 0) for s in OPEN_STATES),
        }

        rows = await _fetch(conn, f'SELECT {SERVER_SQL} AS server, '
                                  'count(*) AS n, '
                                  "count(*) FILTER (WHERE state = 'needs_reconcile') AS bad "
                                  f'FROM external_tasks{where} GROUP BY 1 ORDER BY 3 DESC, 2 DESC',
                            params)
        out['external_tasks']['by_server'] = [
            {'server': str(r[0]), 'total': int(r[1]), 'unresolved': int(r[2]),
             'unresolved_rate': round(int(r[2]) / int(r[1]), 4) if int(r[1]) else None}
            for r in rows]

        # ---------- 2. 规划质量 ----------
        rows = await _fetch(conn, f'SELECT count(*), count(DISTINCT run_id) '
                                  f'FROM run_decisions{where}', params)
        decisions, runs = (int(rows[0][0]), int(rows[0][1])) if rows else (0, 0)

        # 每个 run 的：决策数、模型请求数、决策时参照的计划版本
        # 注意：不能用 committed_plan_version 判"修订过"——它只在决策**提交成功**时
        # 才写值，实测库里一直是 0。真正的修订痕迹在 base_plan_version：一条决策
        # 基于 v2 做，说明这个 run 的计划已经改过一版。
        rows = await _fetch(conn, (
            'SELECT run_id, count(*) AS n, coalesce(sum(provider_requests), 0) AS req, '
            'coalesce(max(base_plan_version), 0) AS bpv '
            f'FROM run_decisions{where} GROUP BY run_id'), params)
        per_run = [{'run_id': r[0], 'decisions': int(r[1]),
                    'provider_requests': int(r[2]), 'max_base_plan_version': int(r[3])}
                   for r in rows]
        revised = [r for r in per_run if r['max_base_plan_version'] > 1]
        out['planning'] = {
            'runs_with_decisions': len(per_run),
            'decisions': decisions,
            'decisions_per_run': round(decisions / len(per_run), 2) if per_run else None,
            'provider_requests_per_run': (
                round(sum(r['provider_requests'] for r in per_run) / len(per_run), 2)
                if per_run else None),
            'revised_runs': len(revised),
            'revision_rate': round(len(revised) / len(per_run), 4) if per_run else None,
        }

        # 决策自身状态分布：candidate / decided / rejected 各有含义，
        # rejected 多说明模型给的计划反复被程序挡下。
        rows = await _fetch(conn, (
            "SELECT coalesce(nullif(status, ''), '(空)') AS st, count(*) "
            f'FROM run_decisions{where} GROUP BY 1 ORDER BY 2 DESC'), params)
        out['planning']['by_status'] = [
            {'status': str(r[0]), 'count': int(r[1])} for r in rows]

        # 决策类型分布（空的 decision_kind 归到 '(空)'）
        rows = await _fetch(conn, (
            "SELECT coalesce(nullif(decision_kind, ''), '(空)') AS kind, count(*), "
            'round(avg(provider_requests), 2) '
            f'FROM run_decisions{where} GROUP BY 1 ORDER BY 2 DESC'), params)
        out['planning']['by_kind'] = [
            {'kind': str(r[0]), 'count': int(r[1]),
             'avg_provider_requests': float(r[2]) if r[2] is not None else None}
            for r in rows]

        # stop 决策的原因分布：这里才看得出"无进展停止"占多少
        stop_where = (where + ' AND ' if where else ' WHERE ') + "decision_kind = 'stop'"
        rows = await _fetch(conn, (
            "SELECT coalesce(nullif(reason, ''), '(空)') AS reason, count(*) "
            f'FROM run_decisions{stop_where} GROUP BY 1 ORDER BY 2 DESC LIMIT 10'), params)
        out['planning']['stop_reasons'] = [
            {'reason': str(r[0]), 'count': int(r[1])} for r in rows]

        # 重复决策自检：(run_id, decision_key) 上有唯一索引，这里永远是 0；
        # 不为 0 说明约束被绕过，是严重的账本问题。
        rows = await _fetch(conn, (
            'SELECT count(*) FROM (SELECT run_id, decision_key '
            f'FROM run_decisions{where} GROUP BY 1, 2 HAVING count(*) > 1) dup'), params)
        out['planning']['duplicate_decisions'] = int(rows[0][0]) if rows else 0

        # 错误码分布：决策失败时留下的线索
        err_where = (where + ' AND ' if where else ' WHERE ') + "error_code <> ''"
        rows = await _fetch(conn, (
            'SELECT error_code, count(*) '
            f'FROM run_decisions{err_where} GROUP BY 1 ORDER BY 2 DESC LIMIT 10'), params)
        out['planning']['decision_errors'] = [
            {'error_code': str(r[0]), 'count': int(r[1])} for r in rows]

        # 假阴性护栏：窗口内 0 行但全表非空 → 时间过滤失效（格式/时区不一致）。
        # 绝不能静默显示成"无记录"，那会被读成"一切正常"。
        if days:
            warnings = []
            for table, hit in (
                    ('external_tasks', out['external_tasks']['total']),
                    ('run_decisions', out['planning']['runs_with_decisions'])):
                rows = await _fetch(conn, f'SELECT count(*) FROM {table}', [])
                everything = int(rows[0][0]) if rows else 0
                if everything and not hit:
                    warnings.append(
                        f'{table} 全表 {everything} 行，但最近 {days} 天窗口内 0 行，'
                        '时间过滤可能失效（created_at 格式/时区不一致）')
            if warnings:
                out['warnings'] = warnings
    return out


def render(data: dict) -> str:
    def pct(v):
        return '—' if v is None else f'{v * 100:.1f}%'

    lines = []
    scope = f'最近 {data["window_days"]} 天' if data.get('window_days') else '全部历史'
    lines.append(f'== 工具调用 ==  （{scope}）')

    ext = data['external_tasks']
    if not ext['total']:
        lines.append('  外部任务：无记录（没有走过异步受理）')
    else:
        lines.append(f'  外部任务 {ext["total"]} 条，结果未定 {ext["unresolved"]} 条 '
                     f'→ 未定率 {pct(ext["unresolved_rate"])}   ← 最该盯的指标')
        lines.append(f'  仍在等待 {ext["still_open"]} 条')
        lines.append('  按状态：' + '，'.join(f'{k}={v}' for k, v in ext['by_state'].items()))
        if ext['by_server']:
            lines.append('  按服务端：')
            for row in ext['by_server']:
                lines.append(f'    {row["server"]:<24} {row["total"]:>6} 条   '
                             f'未定 {row["unresolved"]:>4}  {pct(row["unresolved_rate"])}')

    lines.append('')
    lines.append(f'== 规划质量 ==  （{scope}）')
    plan = data['planning']
    if not plan['runs_with_decisions']:
        lines.append('  决策台账：无记录（模型还没做过自主决策）')
        return '\n'.join(lines)

    lines.append(f'  运行数 {plan["runs_with_decisions"]}，决策 {plan["decisions"]} 次')
    lines.append(f'  每次运行平均：{plan["decisions_per_run"]} 个决策、'
                 f'{plan["provider_requests_per_run"]} 个模型请求')
    lines.append(f'  修订过计划的运行：{plan["revised_runs"]} 个 '
                 f'→ 修订率 {pct(plan["revision_rate"])}   ← 首版计划质量的代理指标')
    if plan.get('by_status'):
        lines.append('  决策状态（rejected 多 = 模型给的计划反复被程序挡下）：')
        for row in plan['by_status']:
            lines.append(f'    {row["status"]:<16} {row["count"]:>6} 次')
    if plan['by_kind']:
        lines.append('  决策类型：')
        for row in plan['by_kind']:
            avg = '' if row['avg_provider_requests'] is None else f'  平均 {row["avg_provider_requests"]} 请求'
            lines.append(f'    {row["kind"]:<16} {row["count"]:>6} 次{avg}')
    if plan['stop_reasons']:
        lines.append('  停止原因（看"无进展"占多少）：')
        for row in plan['stop_reasons']:
            lines.append(f'    {row["reason"]:<28} {row["count"]:>6}')
    if plan['decision_errors']:
        lines.append('  决策错误：')
        for row in plan['decision_errors']:
            lines.append(f'    {row["error_code"]:<28} {row["count"]:>6}')
    if plan['duplicate_decisions']:
        lines.append(f'  ⚠️ 重复决策 {plan["duplicate_decisions"]} 组 —— '
                     '唯一索引被绕过了，账本可能有问题')
    else:
        lines.append('  重复决策：0（(run_id, decision_key) 唯一约束有效）')
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='只读分析：工具调用结果未定率 + 规划质量')
    parser.add_argument('--dsn', default=os.getenv('DATABASE_URL', '').strip(),
                        help='PostgreSQL 连接串，默认读 DATABASE_URL')
    parser.add_argument('--days', type=int, default=0, help='只看最近 N 天，0 = 全部')
    parser.add_argument('--json', action='store_true', help='输出 JSON')
    args = parser.parse_args(argv)

    if not args.dsn:
        print('缺少 --dsn 或 DATABASE_URL', file=sys.stderr)
        return 2
    try:
        data = asyncio.run(collect(args.dsn, args.days or None))
    except Exception as exc:                       # noqa: BLE001  连不上要给人话
        print(f'连接或查询失败：{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
