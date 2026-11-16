import { useEffect, useRef } from "react";

/**
 * 弹层共用的行为：ESC 关闭、打开时移焦、关闭时把焦点还给触发元素。
 *
 * 不做完整焦点陷阱——那需要遍历可聚焦元素并拦截 Tab，容易写错反而更糟。
 * 这里保证三件最基本的事：键盘能进、能出、关上后不会掉到文档顶部。
 */
export function useModalBehavior(
  open: boolean,
  onClose: () => void,
  panelRef: React.RefObject<HTMLElement | null>,
) {
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;

    restoreRef.current = document.activeElement as HTMLElement | null;
    // 等抽屉入场动画起步后再移焦，否则 Safari 会把滚动位置拽回去
    const timer = window.setTimeout(() => {
      const panel = panelRef.current;
      if (!panel) return;
      const first = panel.querySelector<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      (first ?? panel).focus({ preventScroll: true });
    }, 30);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);

    // 弹层打开时锁住背景滚动，避免滚轮把底下的列表带着走
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      restoreRef.current?.focus?.({ preventScroll: true });
    };
  }, [open, onClose, panelRef]);
}
