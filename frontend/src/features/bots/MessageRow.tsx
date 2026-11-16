/**
 * 一条消息。
 *
 * 会话里有四种东西，颜色和位置都不同，因为它们的**归属方不同**：
 *   user      —— 你自己说的，右对齐、洗底
 *   assistant —— 这个机器人说的，左对齐、它的头像
 *   teammate  —— 另一个机器人发给它的私信/回执，role 是 user 但不是你打的字
 *   cron      —— 定时任务投递回来的结果
 *
 * 旧页在这件事上做对了一半：它区分了 cross / cron，但把队友消息画成
 * "assistant + ✉️ 头像"。这里保持同样的视觉语言，但加上 `alias` 语义：
 * 屏幕阅读器读出来的是"来自 reviewer 的队友消息"，不是"机器人说"。
 */
import { Avatar } from "@/components/ui/Avatar";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/cn";
import { Markdown } from "@/lib/markdown";
import type { BotSummary, ChatMessage, MessageKind } from "./types";

const TEAMMATE_PREFIX = /^(Message from 🤖|Reply from 🤖|\[队友答复)/;
const CRON_PREFIX = /^\[Cronjob|^\[Cron delivery:/;
const ENVELOPE_LINE = /^(Message from|Reply from)[^\n]*\n+/;
const WHO = /🤖\s*([^（(\n]+)[（(]@([A-Za-z0-9_-]+)/;

export function classify(message: ChatMessage): MessageKind {
  const text = String(message.content ?? "");
  const role = message.role ?? "assistant";
  if (role === "user") return TEAMMATE_PREFIX.test(text) ? "teammate" : "user";
  return CRON_PREFIX.test(text) ? "cron" : "assistant";
}

const SHELL = "flex gap-2.5 items-start";
const BUBBLE =
  "rounded-xl border px-3 py-2 text-body leading-[1.72] whitespace-pre-wrap break-words";

export function MessageRow({
  message,
  bot,
}: {
  message: ChatMessage;
  bot: BotSummary | undefined;
}) {
  const text = String(message.content ?? "");
  const kind = classify(message);

  if (kind === "user") {
    return (
      <div className={cn(SHELL, "justify-end")}>
        <div className="max-w-[86%]">
          <div className="mb-1 flex justify-end">
            <span className="text-label text-muted">你</span>
          </div>
          <div className={cn(BUBBLE, "rounded-br-sm border-signal/20 bg-signal-wash text-ink")}>
            {text}
          </div>
        </div>
      </div>
    );
  }

  if (kind === "teammate") {
    const who = text.match(WHO);
    const body = text.replace(ENVELOPE_LINE, "");
    return (
      <div className={SHELL}>
        <span
          className="mt-0.5 flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-lg bg-rail text-muted"
          aria-hidden="true"
        >
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
            <rect
              x="2"
              y="4"
              width="12"
              height="8.5"
              rx="1.5"
              stroke="currentColor"
              strokeWidth="1.4"
            />
            <path d="M2.5 5l5.5 3.6L13.5 5" stroke="currentColor" strokeWidth="1.4" />
          </svg>
        </span>
        <div className="min-w-0 max-w-[86%]">
          <div className="mb-1 flex flex-wrap items-center gap-1.5">
            <span className="text-label font-semibold text-ink">{(who?.[1] ?? "队友").trim()}</span>
            <Badge>队友消息</Badge>
            {who?.[2] ? <Badge>@{who[2]}</Badge> : null}
          </div>
          <div className={cn(BUBBLE, "rounded-bl-sm border-line bg-rail text-ink")}>
            <Markdown text={body} />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={SHELL}>
      <Avatar
        size={26}
        icon={bot?.icon}
        color={bot?.color}
        shape={bot?.shape}
        image={bot?.image}
        className="mt-0.5"
      />
      <div className="min-w-0 max-w-[86%]">
        <div className="mb-1 flex flex-wrap items-center gap-1.5">
          <span className="text-label font-semibold text-ink">{bot?.name ?? "机器人"}</span>
          {kind === "cron" ? <Badge tone="warn">定时任务</Badge> : null}
        </div>
        <div className={cn(BUBBLE, "rounded-bl-sm border-line bg-paper text-ink")}>
          <Markdown text={text} />
        </div>
      </div>
    </div>
  );
}

/**
 * 正在处理。SSE 有增量就用增量替换，否则显示"正在处理…"。
 * 不做无限脉动的小圆点——旧页那个 5px 常驻动画正是 DESIGN.md 点名要清掉的。
 */
export function TypingRow({ bot, draft }: { bot: BotSummary | undefined; draft: string }) {
  return (
    <div className={SHELL}>
      <Avatar
        size={26}
        icon={bot?.icon}
        color={bot?.color}
        shape={bot?.shape}
        image={bot?.image}
        className="mt-0.5"
      />
      <div className="min-w-0 max-w-[86%]">
        <div className="mb-1 text-label font-semibold text-ink">{bot?.name ?? "机器人"}</div>
        <div className={cn(BUBBLE, "rounded-bl-sm border-line bg-paper text-ink")}>
          {draft ? (
            <>
              {draft}
              <span className="ml-0.5 inline-block h-3 w-[2px] translate-y-[2px] animate-pulse bg-signal align-middle" />
            </>
          ) : (
            <span className="text-muted">正在处理…</span>
          )}
        </div>
      </div>
    </div>
  );
}
