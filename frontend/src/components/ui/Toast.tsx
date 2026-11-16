import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { cn } from "@/lib/cn";

/**
 * 全局 toast。
 *
 * 与旧页行为对齐：成功 2.2s 自动消失，失败 4.2s。
 * 失败用 role="alert" 让屏幕阅读器立刻读出来，成功用 role="status" 不抢话。
 * 同一时刻只显示一条，新的覆盖旧的——这是旧页的设计，保持一致。
 */

type ToastTone = "ok" | "error";

interface ToastState {
  message: string;
  tone: ToastTone;
  seq: number;
}

const ToastContext = createContext<((message: string, tone?: ToastTone) => void) | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<ToastState | null>(null);
  const timer = useRef<number | null>(null);
  const seq = useRef(0);

  const show = useCallback((message: string, tone: ToastTone = "ok") => {
    seq.current += 1;
    setToast({ message, tone, seq: seq.current });
  }, []);

  useEffect(() => {
    if (!toast) return;
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(
      () => setToast(null),
      toast.tone === "error" ? 4200 : 2200,
    );
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [toast]);

  const value = useMemo(() => show, [show]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed inset-x-0 bottom-6 z-[80] flex justify-center px-4"
        aria-live="polite"
      >
        {toast ? (
          <div
            key={toast.seq}
            role={toast.tone === "error" ? "alert" : "status"}
            className={cn(
              "pointer-events-auto max-w-[520px] rounded-lg border px-3.5 py-2 text-body shadow-overlay",
              toast.tone === "error"
                ? "border-danger/25 bg-danger-wash text-danger"
                : "border-line-strong bg-ink text-paper",
            )}
          >
            {toast.message}
          </div>
        ) : null}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast 必须在 <ToastProvider> 内使用");
  return ctx;
}
