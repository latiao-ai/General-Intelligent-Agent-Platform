import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { consoleApi } from "./api";
import { Spark } from "./BarChart";
import { compact, fmtPoints, num, when } from "./format";
import {
  Code,
  Loading,
  Panel,
  selectCls,
  TableWrap,
  tdCls,
  tdNumCls,
  thCls,
  thNumCls,
  Toolbar,
} from "./primitives";
import type { CreditData } from "./types";

/** 后端只回有消耗的天，缺的天补 0——否则柱子会被挤成一排等宽方块。 */
function creditSlots(daily: { day: string; points: number }[], days: number) {
  const byDay = new Map(daily.map((item) => [item.day, item.points || 0]));
  const out: { day: string; points: number }[] = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i -= 1) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
      d.getDate(),
    ).padStart(2, "0")}`;
    out.push({ day: key, points: byDay.get(key) || 0 });
  }
  return out;
}

/**
 * 积分。
 *
 * 余额不是"存着的数"：进账记在 credit_grants，消耗按 provider_charges 折算。
 * 所以这一页每个数字都能回到具体时间——进账看流水，消耗看每天的柱子。
 * 接口不存在（旧后端）时如实说，不摆假表格。
 */
export function CreditsTab() {
  const [days, setDays] = useState(7);
  const [tenant, setTenant] = useState("");
  const [data, setData] = useState<CreditData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tenants, setTenants] = useState<string[]>([]);

  const load = useCallback(async (span: number, filter: string) => {
    setLoading(true);
    setError("");
    try {
      const next = await consoleApi.credits(span, filter);
      setData(next);
      // 带租户过滤时后端只回这一个租户，重建下拉会把「全部租户」丢掉
      if (!filter) setTenants(next.tenants || []);
    } catch (err) {
      setError(errorMessage(err));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(days, tenant);
  }, [days, tenant, load]);

  const users = data?.users ?? [];

  return (
    <Panel
      title="积分"
      hint="余额 = 进账 − 消耗（按 provider_charges 折算，1 积分 = ¥0.01）"
      actions={data ? <span className="text-label text-muted">共 {num(data.total)} 人</span> : null}
    >
      <Toolbar>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          aria-label="统计范围"
          className={selectCls}
        >
          <option value={7}>近 7 天</option>
          <option value={14}>近 14 天</option>
          <option value={30}>近 30 天</option>
        </select>
        <select
          value={tenant}
          onChange={(e) => setTenant(e.target.value)}
          aria-label="按租户筛选"
          className={selectCls}
        >
          <option value="">全部租户</option>
          {tenants.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <Button onClick={() => void load(days, tenant)}>刷新</Button>
      </Toolbar>

      {error ? (
        <div className="flex flex-col items-start gap-3 py-6">
          <p className="text-body text-ink-2">{error}</p>
          <Button onClick={() => void load(days, tenant)}>重试</Button>
        </div>
      ) : loading ? (
        <Loading rows={6} label="正在加载积分" />
      ) : users.length === 0 ? (
        <p className="py-8 text-center text-body text-muted">
          {tenant ? "这个租户还没有积分记录" : "还没有积分记录"}
        </p>
      ) : (
        <TableWrap>
          <table className="w-full min-w-[860px] border-collapse text-body">
            <thead>
              <tr>
                <th className={thCls}>用户</th>
                <th className={thCls}>租户</th>
                <th className={thNumCls}>余额</th>
                <th className={thNumCls}>近 {days} 天消耗</th>
                <th className={thNumCls}>调用</th>
                <th className={thNumCls}>token</th>
                <th className={thCls}>每日消耗</th>
                <th className={thCls}>最近变动</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const slots = creditSlots(u.daily || [], days);
                const used = Number(u.used_window) > 0;
                return (
                  <tr key={u.user_id} className="hover:bg-rail/70">
                    <td className={tdCls}>
                      <div className="text-ink">{u.display_name || u.user_id}</div>
                      <div className="font-mono text-label text-muted">{u.email || u.user_id}</div>
                    </td>
                    <td className={tdCls}>
                      <Code>{u.tenant_id || ""}</Code>
                    </td>
                    <td className={tdNumCls}>{fmtPoints(u.balance)}</td>
                    <td className={cn(tdNumCls, used && "text-danger")}>
                      {used ? "-" : ""}
                      {fmtPoints(u.used_window)}
                    </td>
                    <td className={tdNumCls}>{num(u.calls_window)}</td>
                    <td className={tdNumCls}>{compact(u.tokens_window)}</td>
                    <td className={tdCls}>
                      <Spark
                        values={slots.map((s) => s.points)}
                        titles={slots.map((s) => `${s.day} · ${fmtPoints(s.points)} 积分`)}
                      />
                    </td>
                    <td className={cn(tdCls, "whitespace-nowrap text-secondary text-muted")}>
                      {when(u.last_change_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableWrap>
      )}
    </Panel>
  );
}
