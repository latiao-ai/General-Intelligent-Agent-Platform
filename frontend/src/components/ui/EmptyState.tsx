import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * 空状态。
 *
 * 这里区分三种"空"，因为它们对用户的含义完全不同：
 *   empty   —— 广场还没内容，要做的是去发布
 *   noMatch —— 筛选没命中，要做的是放宽条件
 *   error   —— 读不出来，要做的是重试
 * 混成一个"暂无数据"，用户就不知道该干什么。
 */
export type EmptyKind = "empty" | "noMatch" | "error";

const PRESET: Record<EmptyKind, { title: string; hint: string }> = {
  empty: {
    title: "广场还空着",
    hint: "去工作台调好一个 Bot，点右侧「发布到广场」，它会出现在这里，并且在本企业内可复用。",
  },
  noMatch: {
    title: "这个筛选下没有 Bot",
    hint: "换个关键词，或者切回「全部」看看。",
  },
  error: {
    title: "读取不出来",
    hint: "服务端返回了错误。可以重试一次；连续失败请查看控制台日志。",
  },
};

export function EmptyState({
  kind,
  message,
  action,
  className,
}: {
  kind: EmptyKind;
  /** 覆盖默认提示，用来放后端返回的具体错误原因 */
  message?: string;
  action?: ReactNode;
  className?: string;
}) {
  const preset = PRESET[kind];

  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-xl border border-dashed border-line-strong bg-rail/40 px-6 py-12 text-center",
        className,
      )}
    >
      <div
        className={cn(
          "mb-3 flex h-9 w-9 items-center justify-center rounded-lg",
          kind === "error" ? "bg-danger-wash text-danger" : "bg-sunken text-muted",
        )}
        aria-hidden="true"
      >
        {kind === "error" ? (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path
              d="M8 5v4M8 11.5v.01M2 8a6 6 0 1112 0A6 6 0 012 8z"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            />
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path
              d="M2.5 5.5l5.5 3 5.5-3M2.5 10.5l5.5 3 5.5-3"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <rect
              x="2.5"
              y="3"
              width="11"
              height="10"
              rx="1.5"
              stroke="currentColor"
              strokeWidth="1.5"
            />
          </svg>
        )}
      </div>

      <h3 className="text-title font-semibold text-ink">{preset.title}</h3>
      <p className="mt-1 max-w-[380px] text-secondary leading-relaxed text-muted">
        {message || preset.hint}
      </p>
      {action ? <div className="mt-4 flex items-center gap-2">{action}</div> : null}
    </div>
  );
}
