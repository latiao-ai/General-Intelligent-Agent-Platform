import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * 状态标记。
 *
 * 只用于"有事发生"的地方：已启用、推荐、审核中、被拒。
 * 五档语气，颜色全部来自状态色，不许出现品牌色——品牌色只标记当前选中。
 */

export type BadgeTone = "neutral" | "ok" | "warn" | "danger" | "signal";

const tones: Record<BadgeTone, string> = {
  neutral: "bg-sunken text-ink-2",
  ok: "bg-ok-wash text-ok",
  warn: "bg-warn-wash text-warn",
  danger: "bg-danger-wash text-danger",
  signal: "bg-signal-wash text-signal-deep",
};

export function Badge({
  tone = "neutral",
  className,
  children,
}: {
  tone?: BadgeTone;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex h-[18px] shrink-0 items-center rounded-sm px-1.5 text-label font-medium whitespace-nowrap",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
