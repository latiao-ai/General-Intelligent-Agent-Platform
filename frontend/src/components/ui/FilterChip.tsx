import { cn } from "@/lib/cn";

/**
 * 分类筛选条。
 *
 * 选中用洗底（signal-wash）而不是实底：整个页面只有一个实底靛青块，
 * 就是主按钮。筛选条件不是"主操作"，不该抢那个位置。
 */
export function FilterChip({
  active,
  count,
  onClick,
  children,
}: {
  active: boolean;
  count?: number;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-secondary",
        "transition-[background-color,border-color,color] duration-150 ease-standard",
        "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
        active
          ? "border-signal/35 bg-signal-wash font-medium text-signal-deep"
          : "border-line-strong bg-paper text-ink-2 hover:bg-sunken hover:text-ink",
      )}
    >
      <span className="max-w-[140px] truncate">{children}</span>
      {typeof count === "number" ? (
        <span
          className={cn(
            "font-mono text-label tabular-nums",
            active ? "text-signal" : "text-muted",
          )}
        >
          {count}
        </span>
      ) : null}
    </button>
  );
}
