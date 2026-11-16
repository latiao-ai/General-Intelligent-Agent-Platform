/**
 * 对话区：气泡 + 聊天过程的"深度思考"卡 + 待答复占位 + 任务轮的"执行过程"。
 *
 * 顺序照旧页：气泡 → 过程卡 → 占位 → 执行过程 → 产物。旧页靠 DOM 顺序插节点
 * 实现（`insertBefore(wrap, pending)`），React 里用渲染顺序表达同一个意思。
 */
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { Markdown } from "@/lib/markdown";
import { Icon } from "./icons";
import { seconds as fmtSeconds, turnStatusLabel } from "./format";
import { stripFileMarkers } from "./mentions";
import type { ArtifactView, HubMessage, TaskEvent, TaskTurn } from "./types";
import type { ChatProcess } from "./useHubChat";

/* ---------------- 气泡 ---------------- */

/** `content_blocks` 里 `_meta` 的形状（旧页 `WB_BLOCKS_HTML` 读的是同一份）。 */
interface BlockMeta {
  mentionType?: string;
  type?: string;
  displayText?: string;
  skillId?: string;
  path?: string;
  kind?: string;
}

/**
 * 历史消息里的引用胶囊（只读）。
 *
 * 只认 `resource_link`：`text` 块就是正文本身，再渲染一遍会和气泡文字重复。
 */
function BlockChips({ blocks }: { blocks?: HubMessage["content_blocks"] }) {
  const links = (blocks ?? []).filter((block) => block?.type === "resource_link");
  if (!links.length) return null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-1">
      {links.map((block, index) => {
        const meta = (block._meta ?? {}) as BlockMeta;
        const kind = meta.mentionType || meta.type || "";
        const label =
          meta.displayText ||
          String(block.title ?? "") ||
          meta.skillId ||
          meta.path ||
          "引用";
        const isFile = kind === "file";
        const isDir = isFile && meta.kind === "dir";
        return (
          <span
            key={`${label}-${index}`}
            className="inline-flex h-[22px] max-w-[220px] items-center gap-1 rounded-md bg-paper/70 px-1.5 text-label text-ink-2"
            title={isFile ? meta.path || label : label}
          >
            <Icon
              name={isFile ? (isDir ? "folder" : "doc") : "slash"}
              size={11}
              className="shrink-0 text-muted"
            />
            <span className="min-w-0 truncate">{label}</span>
          </span>
        );
      })}
    </div>
  );
}

export function MessageRow({ message }: { message: HubMessage }) {
  const mine = message.role === "user";
  return (
    <div className={cn("mb-4 flex", mine ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[82%] rounded-xl px-3.5 py-2.5 text-body leading-[1.75] whitespace-pre-wrap break-words",
          mine
            ? "bg-signal-wash text-ink"
            : "border border-line bg-paper text-ink",
        )}
      >
        {/* 自己的输入原样保留换行；模型输出走 Markdown，否则 **、| 表格 | 会裸露 */}
        {mine ? (
          <>
            {stripFileMarkers(message.content)}
            <BlockChips blocks={message.content_blocks} />
          </>
        ) : (
          <Markdown text={message.content} />
        )}
      </div>
    </div>
  );
}

/* ---------------- 聊天过程卡 ---------------- */

export function ChatProcessCard({ state, elapsed }: { state: ChatProcess; elapsed: string }) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [collapsed, setCollapsed] = useState(state.collapsed);

  // 流式追加思考正文时贴底，否则只能看到开头
  useEffect(() => {
    if (collapsed) return;
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [state.reasoning, collapsed]);

  useEffect(() => {
    setCollapsed(state.collapsed);
  }, [state.collapsed]);

  const title =
    state.phase === "done"
      ? state.reasoning
        ? "深度思考"
        : "处理过程"
      : "深度思考中";

  return (
    <div className="mb-3">
      <div className="flex items-center gap-2">
        <span className="flex h-5 w-5 items-center justify-center rounded-md bg-sunken text-muted">
          <Icon name="bot" size={13} />
        </span>
        <b className="text-secondary font-medium text-ink-2">网页研究智能体</b>
        <span className="font-mono text-label text-muted">{elapsed || "已处理 0s"}</span>
      </div>

      {state.phase !== "done" && !state.reasoning && state.steps.length === 0 ? (
        <p className="mt-1 flex items-center gap-1.5 text-secondary text-muted">
          <Dots />
          深度思考中
        </p>
      ) : null}

      {state.reasoning ? (
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          aria-expanded={!collapsed}
          className="mt-1 flex items-center gap-1 text-secondary text-muted transition-colors duration-150 ease-standard hover:text-ink"
        >
          <span>{title}</span>
          <Icon
            name="chevron"
            size={13}
            className={cn("transition-transform duration-150 ease-standard", collapsed && "-rotate-90")}
          />
        </button>
      ) : null}

      {state.reasoning && !collapsed ? (
        <div
          ref={bodyRef}
          className="mt-1 max-h-[220px] overflow-y-auto rounded-md border border-line bg-rail px-2.5 py-2 text-label leading-relaxed whitespace-pre-wrap break-words text-muted"
        >
          {state.reasoning}
        </div>
      ) : null}

      {state.steps.length ? (
        <ul className="mt-1 flex flex-col gap-0.5">
          {state.steps.map((step) => (
            <li
              key={step.id}
              className={cn(
                "flex items-start gap-2 text-secondary leading-relaxed",
                step.tone === "warn" ? "text-warn" : "text-ink-2",
              )}
            >
              <span aria-hidden="true" className="w-4 shrink-0 text-center">
                {step.icon}
              </span>
              <span className="min-w-0 flex-1 break-words">{step.text}</span>
              <span
                className={cn(
                  "shrink-0 font-mono text-label",
                  step.done ? "text-ok" : step.tone === "warn" ? "text-warn" : "text-muted",
                )}
              >
                {step.meta}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** 三个跳动的点：只有"正在发生什么"的地方才用它。 */
function Dots({ className }: { className?: string }) {
  return (
    <span className={cn("flex gap-1", className)} aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <i
          key={i}
          className="h-1 w-1 animate-bounce rounded-full bg-muted"
          style={{ animationDelay: `${i * 0.14}s`, animationDuration: "1s" }}
        />
      ))}
    </span>
  );
}

/** 等待答复的占位：三个点 + 一句话。 */
export function PendingRow({ text }: { text: string }) {
  return (
    <div className="mb-4 flex items-center gap-2 text-secondary text-muted" role="status">
      <Dots />
      {text}
    </div>
  );
}

/* ---------------- 任务轮的"执行过程" ---------------- */

interface StageRow {
  label: string;
  status: string;
  seconds: number;
  done: boolean;
}

/** 把一轮任务的事件折成可显示的行：阶段、思考、动作。 */
function foldEvents(events: TaskEvent[]) {
  const stages: StageRow[] = [];
  const notes: { kind: "thought" | "action"; icon: string; text: string; detail: string }[] = [];
  let total = 0;
  for (const ev of events) {
    if (ev.type === "stage_start") {
      stages.push({
        label: String(ev.label || ev.stage || ""),
        status: "进行中…",
        seconds: 0,
        done: false,
      });
    } else if (ev.type === "stage_end") {
      // 从后往前找同名未完成的阶段：并行阶段可能同名，取最近的
      for (let i = stages.length - 1; i >= 0; i -= 1) {
        if (stages[i]!.label === String(ev.label || ev.stage || "") && !stages[i]!.done) {
          stages[i] = {
            label: stages[i]!.label,
            status: String(ev.status || "完成"),
            seconds: Number(ev.seconds) || 0,
            done: true,
          };
          break;
        }
      }
      total += Number(ev.seconds) || 0;
    } else if (ev.type === "thought") {
      notes.push({ kind: "thought", icon: "💭", text: String(ev.text ?? ""), detail: "" });
    } else if (ev.type === "action") {
      notes.push({
        kind: "action",
        icon: String(ev.icon || "•"),
        text: String(ev.label ?? ""),
        detail: String(ev.detail ?? ""),
      });
    }
  }
  return { stages, notes, total };
}

export function ProcessSection({ turns }: { turns: TaskTurn[] }) {
  if (!turns.length) return null;
  const total = turns.reduce((sum, t) => sum + foldEvents(t.events ?? []).total, 0);
  return (
    <section className="mt-2 rounded-xl border border-line bg-paper" aria-label="执行过程">
      <div className="hairline-b flex items-center gap-2 px-3 py-2 text-secondary text-ink-2">
        <span className="h-1.5 w-1.5 rounded-full bg-muted" aria-hidden="true" />
        执行过程 · 历史会话（共 {turns.length} 轮，累计 {fmtSeconds(total)}）
      </div>
      <div className="px-3 py-2">
        {turns.map((turn, index) => (
          <TaskTurnRow key={turn.task_id} turn={turn} index={index + 1} />
        ))}
      </div>
    </section>
  );
}

function TaskTurnRow({ turn, index }: { turn: TaskTurn; index: number }) {
  const [open, setOpen] = useState(false);
  const { stages, notes, total } = foldEvents(turn.events ?? []);
  const label = turnStatusLabel(turn.run_status || turn.status);
  return (
    <div className="border-b border-line py-1.5 last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 rounded-md px-1 py-1 text-left text-secondary transition-colors duration-150 ease-standard hover:bg-rail"
      >
        <Icon
          name="chevron"
          size={13}
          className={cn("text-muted transition-transform duration-150 ease-standard", !open && "-rotate-90")}
        />
        <span className="text-ink-2">第 {index} 轮</span>
        <span className="ml-auto font-mono text-label text-muted">
          {label} · {fmtSeconds(total)}
        </span>
      </button>
      {open ? (
        <div className="mt-1 ml-4 space-y-0.5 border-l border-line pl-3">
          <p className="mb-1 text-label leading-relaxed text-muted">{turn.message}</p>
          {stages.map((stage, i) => (
            <div key={`${stage.label}-${i}`} className="flex items-baseline gap-2 text-secondary">
              <span aria-hidden="true">🔹</span>
              <span className="min-w-0 flex-1 truncate text-ink-2">
                {stage.label}
                <span className="text-muted"> · {stage.status}</span>
              </span>
              <span className="shrink-0 font-mono text-label text-muted">
                {fmtSeconds(stage.seconds)}
              </span>
            </div>
          ))}
          {notes.map((note, i) => (
            <div key={`n-${i}`} className="flex items-baseline gap-2 text-secondary">
              <span aria-hidden="true">{note.icon}</span>
              <span className="min-w-0 flex-1 break-words text-ink-2">
                {note.text}
                {note.detail ? <span className="text-muted">：{note.detail}</span> : null}
              </span>
            </div>
          ))}
          {turn.clarification ? (
            <p className="text-label leading-relaxed text-warn">{turn.clarification}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/* ---------------- 产物 ---------------- */

export function ArtifactCard({ taskId, artifact }: { taskId: string; artifact: ArtifactView }) {
  const isVideo = artifact.kind === "video";
  const label =
    isVideo ? "视频成片" : artifact.kind === "sources" ? "资料页面" : "报告产物";
  const href = `/api/tasks/${encodeURIComponent(taskId)}/artifact`;
  return (
    <section className="mt-3 rounded-xl border border-line bg-paper p-3" aria-label={label}>
      <div className="flex flex-wrap items-center gap-2">
        <Icon name="doc" size={15} className="text-muted" />
        <b className="text-body font-semibold text-ink">{label}</b>
        <span className="text-label text-muted">{artifact.filename}</span>
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="ml-auto inline-flex h-[26px] items-center gap-1 rounded-md border border-line-strong px-2 text-secondary text-ink-2 transition-colors duration-150 ease-standard hover:border-signal hover:text-signal"
        >
          <Icon name="external" size={13} />
          {isVideo ? "下载 MP4 视频" : "打开 / 下载 HTML"}
        </a>
      </div>
      {isVideo ? (
        <video
          controls
          preload="metadata"
          src={href}
          className="mt-2 w-full max-w-[960px] rounded-lg"
        />
      ) : null}
      <p className="mt-1 text-label leading-relaxed text-muted">
        {isVideo ? "已生成中文旁白、动画和字幕。" : "想发布的说一句「部署到服务器」即可。"}
      </p>
    </section>
  );
}
