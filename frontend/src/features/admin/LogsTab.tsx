import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { useToast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { consoleApi } from "./api";
import { logDuration, num, shortTime } from "./format";
import {
  Code,
  Panel,
  selectCls,
  StatCard,
  TableWrap,
  tdCls,
  tdMonoCls,
  thCls,
  Toolbar,
} from "./primitives";
import type { LogEntry, LogStatus } from "./types";

const MAX_ROWS = 400;
const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];

/**
 * 日志。
 *
 * 三种节奏必须各自说得清：SSE 实时推送、状态轮询（5s）、Worker 文件轮询（5s）。
 * 离开日志页要把它们全停掉——它们不挂在 DOM 上，切走了也不会自己停，
 * 后台开一夜就是一直在推、一直在存。
 */
export function LogsTab({ active }: { active: boolean }) {
  const toast = useToast();
  const [source, setSource] = useState<"api" | "worker">("api");
  const [minLevel, setMinLevel] = useState("INFO");
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [runLevel, setRunLevel] = useState("INFO");
  const [status, setStatus] = useState<LogStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [rows, setRows] = useState<LogEntry[]>([]);
  const [live, setLive] = useState<{ state: "" | "on" | "off"; text: string }>({
    state: "off",
    text: "连接中…",
  });
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState("");

  const viewRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const pendingRef = useRef<LogEntry[]>([]);
  const pausedRef = useRef(false);
  const levelFocused = useRef(false);

  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  // 搜索输入 350ms 后再重开流：每敲一个字就重连一次服务端会抖
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 350);
    return () => window.clearTimeout(timer);
  }, [query]);

  const append = useCallback((entries: LogEntry[]) => {
    if (!entries.length) return;
    const view = viewRef.current;
    stickRef.current = view
      ? view.scrollHeight - view.scrollTop - view.clientHeight < 48
      : true;
    setRows((prev) => [...prev, ...entries].slice(-MAX_ROWS));
  }, []);

  // 贴底才自动滚：往上翻着看历史时，新日志不能把人拽回底部
  useEffect(() => {
    const view = viewRef.current;
    if (view && stickRef.current) view.scrollTop = view.scrollHeight;
  }, [rows]);

  const loadStatus = useCallback(async () => {
    try {
      const data = await consoleApi.logStatus();
      setStatus(data);
      setStatusError("");
      // 运行级别以服务端为准：别的标签页改过，这里要跟着变
      if (data.log.level && !levelFocused.current) setRunLevel(data.log.level);
    } catch (err) {
      // 状态卡片失败不该把日志本身也拖黑：只把卡片标成"读不到"，流继续
      setStatusError(errorMessage(err));
      setStatus(null);
    }
  }, []);

  const loadHistory = useCallback(
    async (src: "api" | "worker", level: string, q: string) => {
      const params = new URLSearchParams({ limit: "200", level, source: src });
      if (q) params.set("q", q);
      try {
        const data = await consoleApi.logHistory(params);
        setRows([]);
        pendingRef.current = [];
        // 历史是"最新在前"，倒过来塞，页面才是从旧到新往下读
        append((data.entries || []).slice().reverse());
        setError("");
        return data;
      } catch (err) {
        setError(errorMessage(err));
        return null;
      }
    },
    [append],
  );

  // 主循环：筛选一变就重开流。过滤放在服务端做（SSE 的 level/q 参数）——
  // DEBUG 全开时全量是每秒几百条，前端筛不动。
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let es: EventSource | null = null;
    let poll = 0;
    let statusTimer = 0;
    let srcTimer = 0;

    pendingRef.current = [];
    setPaused(false);
    setLive({ state: "off", text: "连接中…" });

    const run = async () => {
      const history = await loadHistory(source, minLevel, debounced);
      if (cancelled) return;
      const view = viewRef.current;
      if (view) view.scrollTop = view.scrollHeight;

      if (source === "worker") {
        // Worker 是另一个进程，本进程的缓冲里没有它的日志，只能读文件
        if (history && history.available === false) {
          setLive({ state: "off", text: "还没生成 worker.log" });
          setError("没有 worker.log。Worker 没启动，或日志目录不是这个。");
          return;
        }
        setLive({ state: "on", text: "每 5 秒读一次文件" });
        poll = window.setInterval(() => {
          if (!pausedRef.current) void loadHistory("worker", minLevel, debounced);
        }, 5000);
        return;
      }

      const params = new URLSearchParams({ level: minLevel });
      if (debounced) params.set("q", debounced);
      es = new EventSource(`/api/console/logs/stream?${params}`);
      es.onopen = () => setLive({ state: "on", text: "实时推送中" });
      es.onmessage = (event) => {
        let entry: LogEntry;
        try {
          entry = JSON.parse(event.data) as LogEntry;
        } catch {
          return;
        }
        if (pausedRef.current) {
          pendingRef.current.push(entry);
          if (pendingRef.current.length > MAX_ROWS) pendingRef.current.shift();
          setLive({
            state: "off",
            text: `已暂停（缓冲区还在攒，已攒 ${pendingRef.current.length} 条）`,
          });
          return;
        }
        append([entry]);
      };
      es.onerror = () => {
        // EventSource 自己会按服务端的 retry 重连，这里只负责把状态说清楚。
        // 401/403 也会走到这里：权限不够时无限重连没意义，顺便提示一句。
        setLive({ state: "off", text: "连接中断，3 秒后重连（若一直如此，检查登录态）" });
      };
    };

    void run();
    void loadStatus();
    statusTimer = window.setInterval(() => void loadStatus(), 5000);
    srcTimer = window.setInterval(() => void loadStatus(), 15000);

    return () => {
      cancelled = true;
      es?.close();
      if (poll) window.clearInterval(poll);
      window.clearInterval(statusTimer);
      window.clearInterval(srcTimer);
    };
  }, [active, append, debounced, loadHistory, loadStatus, minLevel, source]);

  const togglePause = () => {
    setPaused((wasPaused) => {
      const next = !wasPaused;
      if (!next) {
        // 恢复时把攒下的行一次性补上
        if (pendingRef.current.length) {
          append(pendingRef.current);
          pendingRef.current = [];
        }
        setLive({ state: "on", text: source === "worker" ? "每 5 秒读一次文件" : "实时推送中" });
      } else {
        setLive({ state: "off", text: "已暂停（缓冲区还在攒，已攒 0 条）" });
      }
      return next;
    });
  };

  const download = () => {
    if (!rows.length) {
      toast("屏幕上没有日志", "error");
      return;
    }
    const text = rows
      .map((row) => {
        const trace = row.trace ? ` trace=${row.trace}` : "";
        return `${row.ts} ${String(row.level || "").padEnd(7)} ${row.logger || ""}${trace} ${row.message}`;
      })
      .join("\n");
    const blob = new Blob([`${text}\n`], { type: "text/plain;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `logs-${source}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.log`;
    document.body.appendChild(link);
    link.click();
    window.setTimeout(() => {
      URL.revokeObjectURL(link.href);
      link.remove();
    }, 0);
  };

  const changeRunLevel = async (level: string) => {
    setRunLevel(level);
    try {
      const data = await consoleApi.setRunLevel(level);
      toast(`运行级别已切到 ${data.level}`);
    } catch (err) {
      toast(errorMessage(err), "error");
    }
    await loadStatus();
  };

  const process = status?.process;
  const http = status?.http;
  const log = status?.log;
  const rate = Number(http?.window_error_rate || 0) * 100;

  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="进程运行时长"
          value={process ? logDuration(process.uptime_seconds) : "—"}
          sub={
            process
              ? `PID ${process.pid} · 启动于 ${shortTime(process.started_at)}`
              : statusError || "读取中…"
          }
        />
        <StatCard
          label="近 5 分钟请求"
          value={http ? `${Number(http.window_qps || 0).toFixed(2)} req/s` : "—"}
          sub={http ? `累计 ${num(http.total_since_start)} 次 · 窗口 ${num(http.window_requests)} 次` : undefined}
        />
        <StatCard
          label="5xx 错误率"
          value={http ? (rate ? `${rate.toFixed(1)}%` : "0%") : "—"}
          tone={rate >= 5 ? "danger" : rate > 0 ? "warn" : undefined}
          sub={http ? `窗口内 ${num(http.window_errors)} 次 5xx` : undefined}
        />
        <StatCard
          label="P95 响应"
          value={http ? `${num(Math.round(Number(http.window_p95_ms || 0)))} ms` : "—"}
          sub={http ? `P50 ${num(Math.round(Number(http.window_p50_ms || 0)))} ms` : undefined}
        />
      </div>

      <Panel
        title="实时日志"
        actions={
          <span className="flex items-center gap-1.5 text-label text-muted">
            <span
              className={cn(
                "inline-block h-1.5 w-1.5 rounded-full",
                live.state === "on" ? "bg-ok" : live.state === "off" ? "bg-warn" : "bg-line-strong",
              )}
            />
            {live.text}
          </span>
        }
      >
        <Toolbar>
          <select
            value={source}
            onChange={(e) => setSource(e.target.value as "api" | "worker")}
            aria-label="日志来源"
            className={selectCls}
          >
            <option value="api">后端进程（实时推送）</option>
            <option value="worker">Worker（读文件尾部）</option>
          </select>
          <select
            value={minLevel}
            onChange={(e) => setMinLevel(e.target.value)}
            aria-label="最低级别"
            className={selectCls}
          >
            <option value="DEBUG">全部（含 DEBUG）</option>
            <option value="INFO">INFO 及以上</option>
            <option value="WARNING">WARNING 及以上</option>
            <option value="ERROR">ERROR 及以上</option>
          </select>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜消息 / logger / trace"
            aria-label="搜索日志"
            autoComplete="off"
            className={cn(
              "h-[32px] w-[240px] rounded-md border border-line bg-paper px-2.5 text-body outline-none",
              "hover:border-line-strong focus:border-signal focus:ring-[3px] focus:ring-signal/15",
            )}
          />
          <Button onClick={togglePause}>{paused ? "继续滚动" : "暂停滚动"}</Button>
          <Button
            onClick={() => {
              setRows([]);
              pendingRef.current = [];
            }}
          >
            清屏
          </Button>
          <Button onClick={download}>下载本屏</Button>
          <span className="text-label text-muted">本屏 {rows.length} 条</span>
        </Toolbar>

        <div
          ref={viewRef}
          role="log"
          aria-live="polite"
          aria-label="实时日志"
          className="h-[420px] overflow-y-auto overscroll-contain rounded-md border border-line bg-[#0f1720] px-2.5 py-2 font-mono text-[11px] leading-[1.55]"
        >
          {error ? (
            <p className="py-6 text-center text-label text-warn">{error}</p>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-label text-muted">还没有日志。等一会儿，或者放宽级别。</p>
          ) : (
            rows.map((row, i) => <LogRow key={`${row.ts}-${i}`} entry={row} />)
          )}
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-2 text-label text-muted">
          <span>运行级别</span>
          <select
            value={runLevel}
            onChange={(e) => void changeRunLevel(e.target.value)}
            onFocus={() => (levelFocused.current = true)}
            onBlur={() => (levelFocused.current = false)}
            aria-label="运行期日志级别"
            className={cn(selectCls, "h-[26px] text-label")}
          >
            {LEVELS.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
          <span>
            {log
              ? `${log.file_enabled ? "改了立刻生效，重启回到 .env 里的 LOG_LEVEL" : "改了立刻生效（未写文件）"} · 缓冲 ${num(log.size)}/${num(log.capacity)} 条`
              : "—"}
          </span>
          <span className="ml-auto truncate">
            {status && status.files.length
              ? `${log?.dir} · ${status.files.map((f) => `${f.name} ${(f.size / 1024).toFixed(0)}K`).join(" / ")}`
              : log?.file_enabled
                ? log.dir
                : ""}
          </span>
        </div>
      </Panel>

      <Panel title="慢请求（近 5 分钟，>1s）" hint="按路由聚合">
        {http?.window_slow?.length ? (
          <TableWrap>
            <table className="w-full text-body">
              <thead>
                <tr>
                  <th className={thCls}>路由</th>
                  <th className={thCls}>次数</th>
                </tr>
              </thead>
              <tbody>
                {http.window_slow.map((row) => (
                  <tr key={row.route}>
                    <td className={tdMonoCls}>{row.route}</td>
                    <td className={tdCls}>{num(row.count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        ) : (
          <p className="py-4 text-center text-body text-muted">这段时间没有慢请求</p>
        )}
        <p className="mt-2 text-label text-muted">
          日志级别当前为 <Code>{log?.level || "—"}</Code>，来源 {source === "api" ? "进程缓冲" : "worker.log"}
        </p>
      </Panel>
    </div>
  );
}

/** 一行日志。级别用左侧色条 + 文字双重表达，不单靠颜色。 */
function LogRow({ entry }: { entry: LogEntry }) {
  const level = String(entry.level || "INFO").toLowerCase();
  // 终端区是深色底，浅色主题的 text-* 语义色在这里会糊成一片（INFO 会看不见）。
  // 这里直接用一组只在深色底上成立的色值，和上面的设计令牌是两套语境。
  const tone =
    level === "error" || level === "critical"
      ? "text-[#ff9b9b]"
      : level === "warning" || level === "warn"
        ? "text-[#e9c07a]"
        : level === "debug"
          ? "text-[#7d93aa]"
          : "text-[#9db4cb]";
  return (
    <div className="flex items-baseline gap-2 whitespace-pre-wrap break-words py-[1px]">
      <span className="shrink-0 text-[#8fa3b8]">{logClock(entry.ts)}</span>
      <span className={cn("w-[52px] shrink-0 uppercase", tone)}>{entry.level || "INFO"}</span>
      <span className="min-w-0 text-[#d7e2ee]">
        {entry.logger ? <span className="mr-1.5 text-[#7f93a8]">{entry.logger}</span> : null}
        {entry.message}
        {entry.trace ? (
          <span className="ml-1.5 text-[#6c8299]">{String(entry.trace).slice(0, 8)}</span>
        ) : null}
      </span>
    </div>
  );
}

/** 缓冲里是 ISO，文件里是 %Y-%m-%d %H:%M:%S,mmm。一行只显示时分秒。 */
function logClock(iso: string) {
  const text = String(iso || "");
  const isoMatch = text.match(/T(\d{2}:\d{2}:\d{2}(?:\.\d+)?)/);
  if (isoMatch?.[1]) return isoMatch[1];
  const plain = text.match(/(\d{2}:\d{2}:\d{2})(?:[,.](\d+))?/);
  if (plain?.[1]) return plain[1] + (plain[2] ? `.${plain[2].slice(0, 3)}` : "");
  return text.slice(-12);
}
