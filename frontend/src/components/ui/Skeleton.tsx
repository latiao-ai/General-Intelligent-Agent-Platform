import { cn } from "@/lib/cn";

/**
 * 骨架屏。
 *
 * 只在"加载中"出现，不能常驻——DESIGN.md 里那条被点名的毛病就是
 * 一个 5px 圆点永不停歇地脉动。这里由 prefers-reduced-motion 兜底。
 */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("skeleton", className)} aria-hidden="true" />;
}

/** 卡片骨架，形状与真实卡片对齐，避免加载完成后布局跳动。 */
export function CardSkeleton() {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-line p-3.5">
      <div className="flex items-center gap-2.5">
        <Skeleton className="h-11 w-11 rounded-lg" />
        <div className="flex-1 space-y-1.5">
          <Skeleton className="h-3 w-1/2" />
          <Skeleton className="h-2.5 w-2/3" />
        </div>
      </div>
      <Skeleton className="h-3 w-4/5" />
      <Skeleton className="h-2.5 w-full" />
      <div className="flex items-center justify-between pt-1">
        <Skeleton className="h-2.5 w-16" />
        <Skeleton className="h-6 w-16 rounded-md" />
      </div>
    </div>
  );
}
