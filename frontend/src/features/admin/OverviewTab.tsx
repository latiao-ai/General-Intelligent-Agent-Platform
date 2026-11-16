import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { consoleApi } from "./api";
import { BarChart } from "./BarChart";
import { money, num } from "./format";
import { Loading, Panel, StatCard, TableWrap, tdCls, tdNumCls, thCls, thNumCls } from "./primitives";
import type { Overview } from "./types";

const RANGES = [7, 14, 30];

/**
 * 概览。
 *
 * 旧页把「近 14 天」写死在 state 里，界面上没有入口能改——接口明明支持 days。
 * 这里补成三档，因为"上周出了什么事"和"这个月趋势"看的根本不是同一张图。
 */
export function OverviewTab() {
  const [days, setDays] = useState(14);
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (span: number) => {
    setLoading(true);
    setError("");
    try {
      const next = await consoleApi.overview(span);
      setData(next);
    } catch (err) {
      setError(errorMessage(err));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(days);
  }, [days, load]);

  // 转屏/改窗口宽度后重画：图表是按容器真实宽度生成的
  useEffect(() => {
    let timer = 0;
    const onResize = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => void load(days), 250);
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("resize", onResize);
    };
  }, [days, load]);

  const rangePicker = (
    <div className="flex items-center gap-1 rounded-md border border-line p-0.5">
      {RANGES.map((span) => (
        <button
          key={span}
          type="button"
          onClick={() => setDays(span)}
          aria-pressed={days === span}
          className={cn(
            "h-[24px] rounded-[5px] px-2 text-label font-medium transition-colors duration-150 ease-standard",
            days === span ? "bg-signal text-paper" : "text-ink-2 hover:bg-sunken",
          )}
        >
          {span} 天
        </button>
      ))}
    </div>
  );

  if (error) {
    return (
      <Panel title="概览" actions={rangePicker}>
        <div className="flex flex-col items-start gap-3 py-4">
          <p className="text-body text-ink-2">{error}</p>
          <Button onClick={() => void load(days)}>重试</Button>
        </div>
      </Panel>
    );
  }

  if (loading || !data) {
    return (
      <Panel title="概览" actions={rangePicker}>
        <Loading rows={4} label="正在加载概览" />
      </Panel>
    );
  }

  const totals = data.totals ?? { total: 0, active: 0, disabled: 0 };
  const trend = data.trend ?? [];
  const today = trend[trend.length - 1];
  const sum = (field: "messages" | "requests" | "charged_micros") =>
    trend.reduce((acc, row) => acc + Number(row[field] || 0), 0);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-title font-semibold text-ink">
          近 {data.days} 天
          <span className="ml-2 text-label font-normal text-muted">
            数据每小时汇总一次，最新一批可能还没落库
          </span>
        </h2>
        {rangePicker}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="用户总数"
          value={num(totals.total)}
          sub={`正常 ${num(totals.active)} · 停用 ${num(totals.disabled)}`}
        />
        <StatCard
          label="今日活跃"
          value={num(today?.active_users)}
          sub={`会话 ${num(today?.sessions)} 个`}
        />
        <StatCard label="消息量" value={num(sum("messages"))} sub={`近 ${data.days} 天累计`} />
        <StatCard
          label="模型调用"
          value={num(sum("requests"))}
          sub={`花费 ${money(sum("charged_micros"))}`}
        />
      </div>

      <Panel title="每天的消息量" hint={`近 ${data.days} 天`}>
        <BarChart rows={trend} field="messages" unit=" 条" />
      </Panel>

      <Panel title="每天的活跃用户" hint="按会话最后更新时间去重">
        <BarChart rows={trend} field="active_users" color="var(--color-ok)" unit=" 人" />
      </Panel>

      <Panel title="租户分布" hint="按已绑定身份统计">
        {data.tenants.length === 0 ? (
          <p className="py-4 text-center text-body text-muted">还没有租户绑定记录</p>
        ) : (
          <TableWrap>
            <table className="w-full text-body">
              <thead>
                <tr>
                  <th className={thCls}>租户</th>
                  <th className={thNumCls}>用户数</th>
                </tr>
              </thead>
              <tbody>
                {data.tenants.map((t) => (
                  <tr key={t.tenant_id}>
                    <td className={cn(tdCls, "font-mono text-secondary")}>{t.tenant_id}</td>
                    <td className={tdNumCls}>{num(t.users)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Panel>
    </div>
  );
}
