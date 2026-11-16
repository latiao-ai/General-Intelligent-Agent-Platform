/**
 * 定时任务（docs/scheduled-tasks.md）。
 *
 * 与旧页的三处行为必须一致：
 *  1. `rrule` 是**本地时间**语义。`BYHOUR=9` 指服务器/用户所在时区的 9 点，
 *     所以构造规则时直接取 `<input type=time>` 的值，不要转 UTC——转了就会
 *     差 8 小时，而用户只会觉得"我设的 9 点它 1 点跑"。
 *  2. 编辑已有任务时，推送地址**留空表示沿用**。后端回显的是脱敏地址
 *     （`https://…***`），把它当成真实地址提交会覆盖掉真正的 key。
 *  3. `enabled` 是服务端总开关（SCHEDULED_TASKS_ENABLED）。关着时任务能存
 *     但不触发，所以必须显式提示——否则用户会去查自己的规则写错了没有。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { useToast } from "@/components/ui/Toast";
import { cn } from "@/lib/cn";
import { scheduleApi } from "./api";
import type { ScheduleItem, ScheduleKind, SchedulePayload, ScheduleRun } from "./types";

/* ---------------- 常量：与后端取值一一对应，不要自创 ---------------- */

const WEEK: [string, string][] = [
  ["MO", "一"],
  ["TU", "二"],
  ["WE", "三"],
  ["TH", "四"],
  ["FR", "五"],
  ["SA", "六"],
  ["SU", "日"],
];

const FREQS: { value: string; label: string }[] = [
  { value: "hourly", label: "每小时" },
  { value: "daily", label: "每天" },
  { value: "weekly", label: "每周" },
  { value: "monthly", label: "每月" },
  { value: "yearly", label: "每年" },
  { value: "once", label: "一次性" },
];

/** 运行状态。后端只认这几个值，前端不做兜底翻译。 */
const RUN_STATUS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  skipped: "已跳过",
  deferred: "已延后",
  paused: "待确认",
};

const DELIVERY_LABEL: Record<string, string> = {
  sent: "已推送",
  failed: "推送失败",
  queued: "待推送",
};

const CHANNELS: { value: string; label: string }[] = [
  { value: "", label: "不推送" },
  { value: "wecom", label: "企业微信群机器人" },
  { value: "feishu", label: "飞书自定义机器人" },
  { value: "webhook", label: "通用 Webhook" },
];

/* ---------------- 时间与规则 ---------------- */

const pad = (n: number) => String(n).padStart(2, "0");

/** 后端的时间戳是**秒**，不是毫秒。直接 `new Date(ts)` 会得到 1970 年。 */
function fmtTs(ts?: number | null) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "—";
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function relTs(ts?: number | null) {
  if (!ts) return "从未";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

/** 绝对路径太长会把整行撑开，只留最后两段；完整路径挂 title。 */
function shortPath(path: string) {
  const parts = path.split("/").filter(Boolean);
  return parts.length <= 2 ? path : `…/${parts.slice(-2).join("/")}`;
}

/** ISO → `datetime-local` 需要的本地时间字符串（不能用 toISOString，那是 UTC）。 */
function toLocalInput(iso?: string | null) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

interface ParsedRule {
  freq: string;
  interval: number;
  days: string[];
  monthDay: number;
  month: number;
  time: string;
}

/** `FREQ=WEEKLY;BYDAY=MO,WE;BYHOUR=9;BYMINUTE=0` → 表单值。 */
function parseRule(text: string): ParsedRule {
  const map: Record<string, string> = {
    HOURLY: "hourly",
    DAILY: "daily",
    WEEKLY: "weekly",
    MONTHLY: "monthly",
    YEARLY: "yearly",
  };
  const fields: Record<string, string> = {};
  (text || "").split(";").forEach((chunk) => {
    const i = chunk.indexOf("=");
    if (i > 0) fields[chunk.slice(0, i).trim().toUpperCase()] = chunk.slice(i + 1).trim();
  });
  const out: ParsedRule = { freq: "daily", interval: 1, days: [], monthDay: 1, month: 0, time: "09:00" };
  if (fields.FREQ) out.freq = map[fields.FREQ] || "daily";
  if (fields.INTERVAL) out.interval = Math.max(1, parseInt(fields.INTERVAL, 10) || 1);
  if (fields.BYDAY) {
    out.days = fields.BYDAY.split(",")
      .map((s) => s.trim().toUpperCase())
      .filter((s) => /^(MO|TU|WE|TH|FR|SA|SU)$/.test(s));
  }
  if (fields.BYMONTHDAY) out.monthDay = parseInt(fields.BYMONTHDAY.split(",")[0] ?? "1", 10) || 1;
  if (fields.BYMONTH) out.month = parseInt(fields.BYMONTH.split(",")[0] ?? "1", 10) || 0;
  if (fields.BYHOUR !== undefined) {
    const hour = parseInt(fields.BYHOUR.split(",")[0] ?? "0", 10) || 0;
    const minute = parseInt((fields.BYMINUTE || "0").split(",")[0] ?? "0", 10) || 0;
    out.time = `${pad(hour)}:${pad(minute)}`;
  }
  return out;
}

/** 表单值 → `rrule`。星期为空时抛错（后端会 422，但这里拦下来更好懂）。 */
function buildRule(freq: string, intervalRaw: string, time: string, days: Set<string>, monthDay: string, month: string) {
  const interval = Math.max(1, parseInt(intervalRaw || "1", 10) || 1);
  const [hour, minute] = (time || "09:00").split(":").map(Number);
  const parts = ["FREQ=" + freq.toUpperCase()];
  if (interval > 1) parts.push("INTERVAL=" + interval);
  if (freq === "weekly") {
    const picked = WEEK.map(([code]) => code).filter((code) => days.has(code));
    if (!picked.length) throw new Error("请至少选择一个星期");
    parts.push("BYDAY=" + picked.join(","));
  }
  if (freq === "monthly" || freq === "yearly") {
    const day = Math.min(31, Math.max(1, parseInt(monthDay || "1", 10) || 1));
    parts.push("BYMONTHDAY=" + day);
  }
  if (freq === "yearly") parts.push("BYMONTH=" + month);
  if (freq !== "hourly") {
    parts.push("BYHOUR=" + (Number.isFinite(hour) ? hour : 9));
    parts.push("BYMINUTE=" + (Number.isFinite(minute) ? minute : 0));
  }
  return parts.join(";");
}

/* ---------------- 表单 ---------------- */

interface FormState {
  name: string;
  prompt: string;
  freq: string;
  interval: string;
  days: Set<string>;
  time: string;
  monthDay: string;
  month: string;
  once: string;
  untilMode: "long" | "date";
  until: string;
  channel: string;
  url: string;
}

function emptyForm(): FormState {
  return {
    name: "",
    prompt: "",
    freq: "daily",
    interval: "1",
    days: new Set(["MO"]),
    time: "09:00",
    monthDay: "1",
    month: String(new Date().getMonth() + 1),
    once: "",
    untilMode: "long",
    until: "",
    channel: "",
    url: "",
  };
}

function formFromItem(item: ScheduleItem): FormState {
  const parsed = parseRule(item.type === "recurring" ? item.rrule || "" : "");
  return {
    name: item.name,
    prompt: item.prompt,
    freq: item.type === "once" ? "once" : parsed.freq,
    interval: String(parsed.interval),
    days: new Set(parsed.days.length ? parsed.days : ["MO"]),
    time: parsed.time || "09:00",
    monthDay: String(parsed.monthDay),
    month: String(parsed.month || new Date().getMonth() + 1),
    once: toLocalInput(item.scheduled_at),
    untilMode: item.valid_until ? "date" : "long",
    until: item.valid_until ? String(item.valid_until).slice(0, 10) : "",
    channel: item.delivery?.channel || "",
    url: "",
  };
}

/* ---------------- 主视图 ---------------- */

export function SchedulesView() {
  const toast = useToast();
  const [items, setItems] = useState<ScheduleItem[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [filter, setFilter] = useState<"all" | "active" | "paused">("all");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState<ScheduleItem | null | undefined>(undefined);
  const [runsFor, setRunsFor] = useState<ScheduleItem | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await scheduleApi.list(filter);
      setItems(data.tasks || []);
      setEnabled(Boolean(data.enabled));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    void load();
  }, [load]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((it) => `${it.name} ${it.prompt}`.toLowerCase().includes(q));
  }, [items, query]);

  const toggle = async (item: ScheduleItem, next: boolean) => {
    try {
      await scheduleApi.update(item.schedule_id, { enabled: next });
      toast(next ? "已启用" : "已停用");
    } catch (err) {
      toast(`操作失败：${err instanceof Error ? err.message : ""}`, "error");
    }
    void load();
  };

  const runNow = async (item: ScheduleItem) => {
    try {
      await scheduleApi.trigger(item.schedule_id);
      toast("已提交执行");
      void load();
      setRunsFor(item);
    } catch (err) {
      toast(`触发失败：${err instanceof Error ? err.message : ""}`, "error");
    }
  };

  const remove = async (item: ScheduleItem) => {
    // 与旧页一致的确认文案：运行记录会一起没，这一点必须先说
    if (!window.confirm(`删除定时任务「${item.name}」？运行记录会一并删除。`)) return;
    try {
      await scheduleApi.remove(item.schedule_id);
      toast("已删除");
      void load();
    } catch (err) {
      toast(`删除失败：${err instanceof Error ? err.message : ""}`, "error");
    }
  };

  return (
    <div className="mx-auto w-full max-w-[900px] px-7 py-6">
      {/* 工具条 */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="flex gap-0.5 rounded-md bg-sunken p-0.5" role="tablist" aria-label="按状态筛选">
          {(
            [
              ["all", "全部"],
              ["active", "已启用"],
              ["paused", "已停用"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={filter === value}
              onClick={() => setFilter(value)}
              className={cn(
                "h-[26px] rounded-sm px-2.5 text-secondary transition-colors duration-150 ease-standard",
                filter === value
                  ? "bg-paper text-ink shadow-[0_1px_2px_rgba(22,32,46,0.08)]"
                  : "text-muted hover:text-ink",
              )}
            >
              {label}
            </button>
          ))}
        </div>

        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索任务"
          aria-label="搜索定时任务"
          className="h-[30px] w-[180px] rounded-md border border-line bg-paper px-2.5 text-body text-ink placeholder:text-muted focus-visible:border-signal/50 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/15"
        />

        <Button variant="primary" className="ml-auto" onClick={() => setEditing(null)}>
          ＋ 新建任务
        </Button>
      </div>

      {/* 总开关关闭时，任务存得下但不会跑——不说清楚就会被当成 bug */}
      {enabled ? null : (
        <div className="mb-3 rounded-md border border-warn/25 bg-warn-wash px-3 py-2 text-secondary text-warn">
          定时任务总开关未开启（SCHEDULED_TASKS_ENABLED）。任务可以保存，但不会被触发；开开关后需重启
          Worker 生效。
        </div>
      )}

      {error ? (
        <div className="mb-3 rounded-md border border-danger/25 bg-danger-wash px-3 py-2 text-secondary text-danger">
          加载失败：{error}
        </div>
      ) : null}

      {loading && !items.length ? (
        <p className="py-10 text-center text-secondary text-muted">加载中…</p>
      ) : visible.length === 0 ? (
        <p className="py-10 text-center text-secondary text-muted">
          {items.length ? "没有匹配的任务，换个关键词试试。" : "还没有定时任务，点右上角「新建任务」开始。"}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {visible.map((item) => (
            <ScheduleCard
              key={item.schedule_id}
              item={item}
              onToggle={(next) => void toggle(item, next)}
              onRun={() => void runNow(item)}
              onRuns={() => setRunsFor(item)}
              onEdit={() => setEditing(item)}
              onDelete={() => void remove(item)}
            />
          ))}
        </ul>
      )}

      {editing !== undefined ? (
        <ScheduleEditor
          item={editing}
          onClose={() => setEditing(undefined)}
          onSaved={() => {
            setEditing(undefined);
            void load();
          }}
        />
      ) : null}

      {runsFor ? <RunsDrawer item={runsFor} onClose={() => setRunsFor(null)} /> : null}
    </div>
  );
}

/* ---------------- 卡片 ---------------- */

function ScheduleCard({
  item,
  onToggle,
  onRun,
  onRuns,
  onEdit,
  onDelete,
}: {
  item: ScheduleItem;
  onToggle: (next: boolean) => void;
  onRun: () => void;
  onRuns: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const status = item.last_status ? RUN_STATUS[item.last_status] || item.last_status : "";
  const meta: string[] = [item.schedule_desc || ""];
  meta.push(`下次 ${item.enabled ? fmtTs(item.next_run_at) : "—"}`);
  if (item.last_run_at) meta.push(`上次 ${relTs(item.last_run_at)}${status ? ` · ${status}` : ""}`);
  if (item.delivery?.channel) meta.push(`推送 ${item.delivery.channel}`);

  return (
    <li className="rounded-lg border border-line bg-paper px-3.5 py-3 transition-colors duration-150 ease-standard hover:border-line-strong">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="truncate text-body font-medium text-ink">{item.name}</span>
            {item.enabled ? (
              item.consecutive_failures ? (
                <Badge tone="warn">连续失败 {item.consecutive_failures} 次</Badge>
              ) : (
                <Badge tone="ok">已启用</Badge>
              )
            ) : (
              <Badge>已停用</Badge>
            )}
            {item.bot_id ? <Badge tone="signal">机器人例行</Badge> : null}
          </div>

          <p className="mt-1 line-clamp-2 text-secondary leading-relaxed text-muted">{item.prompt}</p>

          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-label text-muted">
            {meta.filter(Boolean).map((part, index) => (
              <span key={index} className="flex items-center gap-2">
                {index > 0 ? <i className="h-[3px] w-[3px] rounded-full bg-line-strong" aria-hidden="true" /> : null}
                {part}
              </span>
            ))}
          </div>

          {item.last_status === "failed" && item.last_error ? (
            <p className="mt-1 text-label leading-relaxed text-danger">{item.last_error}</p>
          ) : null}
        </div>

        {/* 开关：自绘的，原生 checkbox 在 11px 文案里显得太大 */}
        <button
          type="button"
          role="switch"
          aria-checked={item.enabled}
          aria-label={item.enabled ? "停用" : "启用"}
          onClick={() => onToggle(!item.enabled)}
          className={cn(
            "relative mt-0.5 h-[18px] w-[32px] shrink-0 rounded-full transition-colors duration-150 ease-standard",
            item.enabled ? "bg-signal" : "bg-line-strong",
          )}
        >
          <span
            className={cn(
              "absolute top-[2px] h-[14px] w-[14px] rounded-full bg-paper transition-[left] duration-150 ease-standard",
              item.enabled ? "left-[16px]" : "left-[2px]",
            )}
          />
        </button>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <Button size="sm" variant="ghost" onClick={onRun}>
          立即运行
        </Button>
        <Button size="sm" variant="ghost" onClick={onRuns}>
          运行记录
        </Button>
        <Button size="sm" variant="ghost" onClick={onEdit}>
          编辑
        </Button>
        <Button size="sm" variant="ghost" className="ml-auto text-danger hover:bg-danger-wash" onClick={onDelete}>
          删除
        </Button>
      </div>
    </li>
  );
}

/* ---------------- 编辑器 ---------------- */

const inputCls =
  "h-[30px] w-full rounded-md border border-line bg-paper px-2.5 text-body text-ink " +
  "placeholder:text-muted focus-visible:border-signal/50 focus-visible:outline-none " +
  "focus-visible:ring-[3px] focus-visible:ring-signal/15";
const areaCls =
  "w-full resize-y rounded-md border border-line bg-paper px-2.5 py-2 text-body leading-relaxed text-ink " +
  "placeholder:text-muted focus-visible:border-signal/50 focus-visible:outline-none " +
  "focus-visible:ring-[3px] focus-visible:ring-signal/15";
const labelCls = "flex flex-col gap-1 text-label text-muted";

function ScheduleEditor({
  item,
  onClose,
  onSaved,
}: {
  item: ScheduleItem | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState<FormState>(() => (item ? formFromItem(item) : emptyForm()));
  const [saving, setSaving] = useState(false);
  const [workspace, setWorkspace] = useState("跟随当前工作空间");

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  useEffect(() => {
    // 只用来把"任务跑在哪个目录"说清楚，拿不到也不影响保存
    void (async () => {
      try {
        const res = await fetch("/api/workspace", { credentials: "same-origin" });
        if (!res.ok) return;
        const data = (await res.json()) as { path?: string };
        if (data?.path) setWorkspace(data.path);
      } catch {
        /* 忽略 */
      }
    })();
  }, []);

  const freq = form.freq;
  const showWeekly = freq === "weekly";
  const showTime = freq !== "hourly" && freq !== "once";
  const showMonthDay = freq === "monthly" || freq === "yearly";
  const showMonth = freq === "yearly";
  const showOnce = freq === "once";
  const showUntil = form.untilMode === "date";
  const showPush = Boolean(form.channel);

  const save = async () => {
    const name = form.name.trim();
    const prompt = form.prompt.trim();
    if (!name) return toast("请填写任务名称", "error");
    if (!prompt) return toast("请填写提示词", "error");

    const payload: SchedulePayload = { name, prompt, type: "recurring" as ScheduleKind };
    if (freq === "once") {
      if (!form.once) return toast("请选择执行时间", "error");
      payload.type = "once";
      payload.scheduled_at = form.once;
      payload.rrule = "";
    } else {
      payload.type = "recurring";
      payload.scheduled_at = "";
      try {
        payload.rrule = buildRule(freq, form.interval, form.time, form.days, form.monthDay, form.month);
      } catch (err) {
        return toast(err instanceof Error ? err.message : "规则不合法", "error");
      }
    }
    payload.valid_until =
      form.untilMode === "date" && form.until
        ? new Date(`${form.until}T23:59:59`).toISOString()
        : "";

    // 渠道没变又没填新地址 → 不带 delivery，让后端沿用库里已保存的（含真 key）
    const current = item?.delivery || {};
    if (!form.channel) {
      payload.delivery = { channel: "", webhook_url: "", events: null };
    } else if (form.url.trim() || form.channel !== (current.channel || "")) {
      payload.delivery = { channel: form.channel, webhook_url: form.url.trim(), events: null };
    }

    setSaving(true);
    try {
      if (item) await scheduleApi.update(item.schedule_id, payload);
      else await scheduleApi.create(payload);
      toast(item ? "已保存" : "已创建");
      onSaved();
    } catch (err) {
      toast(`保存失败：${err instanceof Error ? err.message : ""}`, "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      open
      onClose={onClose}
      title={item ? "编辑定时任务" : "新建定时任务"}
      width="wide"
      footer={
        <div className="flex w-full items-center gap-2">
          {item && form.channel ? (
            <Button
              variant="ghost"
              onClick={() => {
                void (async () => {
                  try {
                    const res = await scheduleApi.testDelivery(item.schedule_id);
                    toast(res?.detail || (res?.ok === false ? "推送失败" : "已发送测试消息"), res?.ok === false ? "error" : "ok");
                  } catch (err) {
                    toast(`测试推送失败：${err instanceof Error ? err.message : ""}`, "error");
                  }
                })();
              }}
            >
              测试推送
            </Button>
          ) : null}
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" disabled={saving} onClick={() => void save()}>
            {saving ? "保存中…" : "保存"}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3">
        <label className={labelCls}>
          任务名称
          <input
            value={form.name}
            maxLength={60}
            onChange={(e) => set("name", e.target.value)}
            placeholder="例如：每日 AI 新闻推送"
            className={inputCls}
          />
        </label>

        <label className={labelCls}>
          提示词
          <textarea
            value={form.prompt}
            rows={3}
            maxLength={10000}
            onChange={(e) => set("prompt", e.target.value)}
            placeholder="到点后让助手做什么，例如：关注当天 AI 领域的重要动态，整理成 5 条要点"
            className={areaCls}
          />
        </label>

        <div className="grid grid-cols-2 gap-3">
          <label className={labelCls}>
            重复
            <select value={freq} onChange={(e) => set("freq", e.target.value)} className={inputCls}>
              {FREQS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
          {freq === "once" ? null : (
            <label className={labelCls}>
              间隔
              <input
                type="number"
                min={1}
                max={366}
                value={form.interval}
                onChange={(e) => set("interval", e.target.value)}
                className={inputCls}
              />
            </label>
          )}
        </div>

        {showWeekly ? (
          <div className="flex flex-col gap-1.5">
            <span className="text-label text-muted">星期</span>
            <div className="flex flex-wrap gap-1">
              {WEEK.map(([code, label]) => {
                const on = form.days.has(code);
                return (
                  <button
                    key={code}
                    type="button"
                    aria-pressed={on}
                    onClick={() => {
                      const next = new Set(form.days);
                      if (on) next.delete(code);
                      else next.add(code);
                      set("days", next);
                    }}
                    className={cn(
                      "h-[26px] rounded-md border px-2 text-secondary transition-colors duration-150 ease-standard",
                      on
                        ? "border-signal/40 bg-signal-wash text-signal"
                        : "border-line bg-paper text-muted hover:border-line-strong hover:text-ink",
                    )}
                  >
                    周{label}
                  </button>
                );
              })}
            </div>
          </div>
        ) : null}

        <div className="grid grid-cols-3 gap-3">
          {showTime ? (
            <label className={labelCls}>
              时间
              <input type="time" value={form.time} onChange={(e) => set("time", e.target.value)} className={inputCls} />
            </label>
          ) : null}
          {showMonthDay ? (
            <label className={labelCls}>
              每月几日
              <input
                type="number"
                min={1}
                max={31}
                value={form.monthDay}
                onChange={(e) => set("monthDay", e.target.value)}
                className={inputCls}
              />
            </label>
          ) : null}
          {showMonth ? (
            <label className={labelCls}>
              月份
              <select value={form.month} onChange={(e) => set("month", e.target.value)} className={inputCls}>
                {Array.from({ length: 12 }, (_, i) => i + 1).map((m) => (
                  <option key={m} value={String(m)}>
                    {m} 月
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>

        {showOnce ? (
          <label className={labelCls}>
            执行时间
            <input
              type="datetime-local"
              value={form.once}
              onChange={(e) => set("once", e.target.value)}
              className={inputCls}
            />
          </label>
        ) : null}

        <div className="grid grid-cols-2 gap-3">
          <label className={labelCls}>
            有效期
            <select
              value={form.untilMode}
              onChange={(e) => set("untilMode", e.target.value as "long" | "date")}
              className={inputCls}
            >
              <option value="long">长期有效</option>
              <option value="date">截止日期</option>
            </select>
          </label>
          {showUntil ? (
            <label className={labelCls}>
              截止日期
              <input type="date" value={form.until} onChange={(e) => set("until", e.target.value)} className={inputCls} />
            </label>
          ) : null}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <label className={labelCls}>
            消息推送
            <select value={form.channel} onChange={(e) => set("channel", e.target.value)} className={inputCls}>
              {CHANNELS.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </label>
          {showPush ? (
            <label className={labelCls}>
              推送地址
              <input
                value={form.url}
                onChange={(e) => set("url", e.target.value)}
                placeholder={item?.delivery?.channel ? "留空则沿用已保存的地址" : "https://…"}
                className={inputCls}
              />
            </label>
          ) : null}
        </div>

        <p className="truncate text-label leading-relaxed text-muted" title={workspace}>
          工作空间：{shortPath(workspace)}（执行时按该账号当前工作空间）
        </p>
        <p className="text-label leading-relaxed text-muted">
          提示：任务以你的身份在后台执行；需要审批的工具会停下并推送「待确认」。
        </p>
      </div>
    </Drawer>
  );
}

/* ---------------- 运行记录 ---------------- */

function RunsDrawer({ item, onClose }: { item: ScheduleItem; onClose: () => void }) {
  const [runs, setRuns] = useState<ScheduleRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const data = await scheduleApi.runs(item.schedule_id);
        setRuns(data.runs || []);
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载失败");
      }
    })();
  }, [item.schedule_id]);

  return (
    <Drawer open onClose={onClose} title={`运行记录 · ${item.name}`}>
      {error ? <p className="text-secondary text-danger">加载失败：{error}</p> : null}
      {!error && runs === null ? <p className="text-secondary text-muted">加载中…</p> : null}
      {runs && runs.length === 0 ? <p className="text-secondary text-muted">还没有运行记录。</p> : null}
      {runs && runs.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {runs.map((run, index) => {
            const label = RUN_STATUS[run.status] || run.status;
            const delivery = run.delivery_status ? DELIVERY_LABEL[run.delivery_status] || run.delivery_status : "";
            const meta = [`计划 ${fmtTs(run.due_at)}`];
            if (run.finished_at) meta.push(`完成 ${fmtTs(run.finished_at)}`);
            if (delivery) meta.push(delivery);
            if ((run.delivery_attempts || 0) > 1) meta.push(`尝试 ${run.delivery_attempts} 次`);
            const bad = run.status === "failed";
            return (
              <li key={run.run_id || index} className="rounded-md border border-line px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className={cn("text-secondary font-medium", bad ? "text-danger" : "text-ink")}>{label}</span>
                  <span className="text-label text-muted">{meta.join(" · ")}</span>
                </div>
                {run.summary ? (
                  <p className="mt-1 text-secondary leading-relaxed text-muted">{run.summary}</p>
                ) : null}
                {run.error_code ? (
                  <p className="mt-1 text-label leading-relaxed text-danger">
                    {run.error_code}
                    {run.error_message ? `：${run.error_message}` : ""}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </Drawer>
  );
}
