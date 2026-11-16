import { useId, useRef, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { useModalBehavior } from "./useModalBehavior";

/**
 * 居中弹窗。
 *
 * 与抽屉的区别是语义：抽屉看详情（不影响当前页面），弹窗做决策
 * （"我的投稿"是个需要专注的清单）。
 *
 * 标题 id 用 `useId` 而不是写死：一个页面会同时挂多个 Dialog（都关着但
 * 在 DOM 里），写死 `dialog-title` 会出现五个同名 id，屏幕阅读器会把
 * aria-labelledby 指到第一个——读出来的是别的弹窗的标题。
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  className,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
  className?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descId = useId();
  useModalBehavior(open, onClose, panelRef);

  return (
    <div
      className={cn(
        "fixed inset-0 z-[70] flex items-center justify-center p-4",
        open ? "pointer-events-auto" : "pointer-events-none",
      )}
      aria-hidden={!open}
      // `aria-hidden` 只让屏幕阅读器忽略，**不会**把里面的按钮从 Tab 序列里拿掉：
      // 弹窗关着时按 Tab 会跳进看不见的表单里（HubPage 同时挂 6 个 Dialog，
      // 症状是"按了十几次 Tab 才回到可见区域"）。inert 才是真的挡住。
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
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={cn(
          "relative flex max-h-[82vh] w-full max-w-[620px] flex-col overflow-hidden rounded-xl border border-line bg-paper shadow-overlay outline-none",
          "transition-[opacity,transform] duration-200 ease-standard",
          open ? "scale-100 opacity-100" : "scale-[0.98] opacity-0",
          className,
        )}
      >
        <div className="hairline-b flex shrink-0 items-center gap-2 px-5 py-3.5">
          <h2 id={titleId} className="text-title font-semibold text-ink">
            {title}
          </h2>
          {description ? (
            <p id={descId} className="text-secondary text-muted">
              {description}
            </p>
          ) : null}
          <span className="flex-1" />
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="flex h-7 w-7 items-center justify-center rounded-md text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path
                d="M4 4l8 8M12 4l-8 8"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-4">
          {children}
        </div>
      </div>
    </div>
  );
}
