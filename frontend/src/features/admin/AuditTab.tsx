import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { consoleApi } from "./api";
import { auditDetail, auditLabel, shortTime, when } from "./format";
import {
  Loading,
  Panel,
  TableWrap,
  tdCls,
  tdMonoCls,
  thCls,
} from "./primitives";
import type { AuditEntry } from "./format";

/**
 * 审计。
 *
 * 表格里给的是相对时间（"3 小时前"读起来快），但 title 上必须挂绝对时间：
 * 审计的用途就是和日志对齐，"3 小时前"没法拿去比对。
 */
export function AuditTab() {
  const [events, setEvents] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await consoleApi.audit();
      setEvents(data.events || []);
    } catch (err) {
      setError(errorMessage(err));
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Panel
      title="管理动作审计"
      hint={loading ? "读取中…" : `最近 ${events.length} 条`}
      actions={<Button onClick={() => void load()}>刷新</Button>}
    >
      {error ? (
        <div className="flex flex-col items-start gap-3 py-6">
          <p className="text-body text-ink-2">{error}</p>
          <Button onClick={() => void load()}>重试</Button>
        </div>
      ) : loading ? (
        <Loading rows={6} label="正在加载审计" />
      ) : events.length === 0 ? (
        <p className="py-8 text-center text-body text-muted">还没有管理动作记录</p>
      ) : (
        <TableWrap>
          <table className="w-full min-w-[820px] border-collapse text-body">
            <thead>
              <tr>
                <th className={thCls}>时间</th>
                <th className={thCls}>动作</th>
                <th className={thCls}>操作人</th>
                <th className={thCls}>对象</th>
                <th className={thCls}>内容</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e, i) => {
                const target = e.user_id || e.policy_id || e.tool_key || e.platform_id || "";
                return (
                  <tr key={i}>
                    <td
                      className={cn(tdCls, "whitespace-nowrap text-secondary text-muted")}
                      title={shortTime(e.created_at)}
                    >
                      {when(e.created_at)}
                    </td>
                    <td className={tdCls}>{auditLabel[e.type || ""] || e.type || "未知"}</td>
                    <td className={cn(tdMonoCls, "text-label")}>{e.operator_id || "—"}</td>
                    <td className={cn(tdMonoCls, "text-label")}>{target}</td>
                    <td className={cn(tdCls, "text-secondary text-muted")}>{auditDetail(e)}</td>
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
