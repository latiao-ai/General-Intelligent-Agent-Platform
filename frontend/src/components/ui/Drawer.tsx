import { useRef, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { useModalBehavior } from "./useModalBehavior";

/**
 * 右侧抽屉。
 *
 * 旧页用 `hidden` + class 切换 + 190ms 延迟恢复，这里用同样的时序，
 * 但把"入场/出场"交给 CSS transition，React 只负责挂载状态。
 *
 * 尺寸：宽 480px，小屏退化成底部整宽（避免 375px 上抽屉只剩一条缝）。
 */
export function Drawer({
  open,
  onClose,
  title,
  header,
  footer,
  width = "md",
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  header?: ReactNode;
  footer?: ReactNode;
  /** wide 给"表单本身就是主角"的场景（连接器、技能、广场编辑）：三列并排塞不进 480。 */
  width?: "md" | "wide";
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  useModalBehavior(open, onClose, panelRef);

  return (
    <div
      className={cn(
        "fixed inset-0 z-[60]",
        open ? "pointer-events-auto" : "pointer-events-none",
      )}
      aria-hidden={!open}
      // `aria-hidden` 只让屏幕阅读器忽略，**不会**把里面的按钮从 Tab 序列里拿掉：
      // 抽屉关着时按 Tab 会跳进看不见的表单里。inert 才是真的挡住。
      inert={!open}
    >
      <div
        onClick={onClose}
        className={cn(
          "absolute inset-0 bg-ink/35 transition-opacity duration-200 ease-standard",
          open ? "opacity-100" : "opacity-0",
        )}
      />

      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className={cn(
          "absolute inset-y-0 right-0 flex w-full flex-col bg-paper shadow-overlay outline-none",
          width === "wide"
            ? "sm:w-[min(760px,96vw)] sm:border-l sm:border-line"
            : "sm:w-[480px] sm:border-l sm:border-line",
          "transition-transform duration-200 ease-standard",
          open ? "translate-x-0" : "translate-x-full",
        )}
      >
        {/*
         * `header` 是"自定义整条顶栏"，不是"标题的补充"。以前只渲染 header，
         * 于是不传 header 的调用方会得到一个没有标题、也没有关闭按钮的抽屉——
         * 只能点遮罩或按 Esc 才知道怎么关。这里补一个默认头。
         */}
        <div className="hairline-b shrink-0">
          {header ?? (
            <div className="flex items-center gap-2 px-5 py-3">
              <h2 className="min-w-0 flex-1 truncate text-body font-medium text-ink">{title}</h2>
              <button
                type="button"
                onClick={onClose}
                aria-label="关闭"
                className="-mr-1 flex h-[26px] w-[26px] items-center justify-center rounded-md text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path
                    d="M6.5 6.5l11 11M17.5 6.5l-11 11"
                    stroke="currentColor"
                    strokeWidth="1.7"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </div>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-4">
          {children}
        </div>
        {footer ? (
          <div className="hairline-t shrink-0 bg-rail/60 px-4 py-3">{footer}</div>
        ) : null}
      </div>
    </div>
  );
}
