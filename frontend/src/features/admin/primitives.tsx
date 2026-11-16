import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";

/* =============================================================================
 * 控制台的积木
 *
 * 旧页用 innerHTML 拼字符串，同样的"卡片标题 + 右上角提示"在 9 个页签里
 * 写了 14 遍、行高各不相同。这里收成组件，间距只在这里定义一次。
 * ========================================================================== */

export function Panel({
  title,
  hint,
  actions,
  children,
  className,
  bodyClassName,
  as = "section",
}: {
  title?: ReactNode;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  as?: "section" | "div";
}) {
  const Tag = as;
  return (
    <Tag className={cn("overflow-hidden rounded-xl border border-line bg-paper", className)}>
      {title ? (
        <div className="hairline-b flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
          <h2 className="text-body font-semibold text-ink">{title}</h2>
          {hint ? <p className="text-secondary leading-relaxed text-muted">{hint}</p> : null}
          {actions ? <div className="ml-auto flex items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      <div className={cn("p-4", bodyClassName)}>{children}</div>
    </Tag>
  );
}

/** 概览与日志页顶部的数字卡。数字用等宽数字，横排时不跳。 */
export function StatCard({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "ok" | "warn" | "danger";
}) {
  const valueTone =
    tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn" : tone === "danger" ? "text-danger" : "text-ink";
  return (
    <div className="rounded-xl border border-line bg-paper px-4 py-3">
      <div className="text-label text-muted">{label}</div>
      <div
        className={cn(
          "mt-1 text-headline font-semibold tracking-tight tabular-nums",
          valueTone,
        )}
      >
        {value}
      </div>
      {sub ? <div className="mt-0.5 text-label leading-relaxed text-muted">{sub}</div> : null}
    </div>
  );
}

export function Toolbar({ children }: { children: ReactNode }) {
  return <div className="mb-3 flex flex-wrap items-center gap-2">{children}</div>;
}

/** 表头。sticky 是为了用户表 100 行时还能看见列名。 */
export const tableCls = "w-full min-w-[720px] border-collapse text-body";
export const thCls =
  "hairline-b px-3 py-2 text-left text-label font-medium whitespace-nowrap text-muted";
export const thNumCls = cn(thCls, "text-right");
export const tdCls = "border-b border-line/70 px-3 py-2.5 align-middle";
export const tdNumCls = cn(tdCls, "text-right tabular-nums whitespace-nowrap");
export const tdMonoCls = cn(tdCls, "font-mono text-secondary text-ink-2");

export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="-mx-1 overflow-x-auto px-1">{children}</div>;
}

/** 表单字段：标签在上、说明在下。说明写"为什么"，不写"必填"。 */
export function Field({
  label,
  note,
  htmlFor,
  span2,
  children,
  className,
}: {
  label: ReactNode;
  note?: ReactNode;
  htmlFor?: string;
  span2?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col gap-1.5", span2 && "sm:col-span-2", className)}>
      <label htmlFor={htmlFor} className="text-secondary font-medium text-ink-2">
        {label}
      </label>
      {children}
      {note ? <p className="text-label leading-relaxed text-muted">{note}</p> : null}
    </div>
  );
}

export const inputCls =
  "h-[32px] w-full rounded-md border border-line bg-paper px-2.5 text-body text-ink " +
  "placeholder:text-muted/70 outline-none transition-colors duration-150 ease-standard " +
  "hover:border-line-strong focus:border-signal focus:ring-[3px] focus:ring-signal/15 " +
  "disabled:cursor-not-allowed disabled:bg-sunken disabled:text-muted";

export const textareaCls =
  "w-full rounded-md border border-line bg-paper px-2.5 py-2 text-body leading-relaxed text-ink " +
  "placeholder:text-muted/70 outline-none transition-colors duration-150 ease-standard " +
  "hover:border-line-strong focus:border-signal focus:ring-[3px] focus:ring-signal/15 resize-y";

export const selectCls =
  "h-[32px] rounded-md border border-line bg-paper px-2 pr-7 text-body text-ink outline-none " +
  "transition-colors duration-150 ease-standard hover:border-line-strong " +
  "focus:border-signal focus:ring-[3px] focus:ring-signal/15";

export function Grid({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("grid gap-3 sm:grid-cols-2", className)}>{children}</div>;
}

/** 抽屉里的小节标题。旧页叫 .sub。 */
export function Sub({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <h3 className={cn("mt-5 mb-2 text-label font-semibold tracking-wide text-muted", className)}>
      {children}
    </h3>
  );
}

export function KV({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[88px_1fr] gap-x-3 gap-y-1.5 text-body">
      {rows.map(([k, v], i) => (
        <div key={i} className="col-span-2 grid grid-cols-subgrid">
          <dt className="text-label leading-[1.9] text-muted">{k}</dt>
          <dd className="min-w-0 text-ink-2">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Switch({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <span className="relative inline-flex h-[20px] w-[34px] shrink-0 items-center">
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="peer sr-only"
      />
      <span className="pointer-events-none absolute inset-0 rounded-full bg-line-strong transition-colors duration-150 ease-standard peer-checked:bg-signal peer-focus-visible:ring-[3px] peer-focus-visible:ring-signal/25 peer-disabled:opacity-45" />
      <span className="pointer-events-none absolute left-[2px] h-[16px] w-[16px] rounded-full bg-paper shadow-lift transition-transform duration-150 ease-standard peer-checked:translate-x-[14px]" />
    </span>
  );
}

/** 代码/标识。控制台里到处是 id，用等宽字体把它们和句子分开。 */
export function Code({ children }: { children: ReactNode }) {
  return (
    <code className="rounded-sm bg-sunken px-1 py-0.5 font-mono text-label text-ink-2">
      {children}
    </code>
  );
}

export function Tag({ children }: { children: ReactNode }) {
  return (
    <span className="mr-1 inline-flex items-center whitespace-nowrap rounded-sm border border-line px-1.5 text-label text-ink-2">
      {children}
    </span>
  );
}

/** 抽屉的头。抽屉本体只负责滑入与焦点，标题区各页长得不一样（有的带操作按钮）。 */export function DrawerHead({
  title,
  hint,
  onClose,
  actions,
}: {
  title: ReactNode;
  hint?: ReactNode;
  onClose: () => void;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-3 px-5 py-3.5">
      <div className="min-w-0">
        <h2 className="truncate text-title font-semibold text-ink">{title}</h2>
        {hint ? <p className="mt-0.5 truncate text-label text-muted">{hint}</p> : null}
      </div>
      <span className="flex-1" />
      {actions}
      <Button size="sm" variant="ghost" onClick={onClose} aria-label="关闭">
        关闭
      </Button>
    </div>
  );
}

export function Loading({ rows = 3, label = "读取中" }: { rows?: number; label?: string }) {
  return (
    <div className="flex flex-col gap-2" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-8 w-full" />
      ))}
    </div>
  );
}

/* =============================================================================
 * confirm / prompt
 *
 * 旧页用 window.confirm 和 window.prompt。它们的问题不是难看：confirm 拿不到
 * "会删掉多少条记录"这种结构化内容，prompt 在移动端会被浏览器吞掉，
 * 而"驳回理由"这种必填项用 prompt 的空值判断只能靠 return null。
 * 这里换成同一个 Dialog，返回 Promise，调用处仍然是 await。
 * ========================================================================== */

interface ConfirmOptions {
  title: string;
  body?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  tone?: "default" | "danger";
  /** prompt 形态：显示一个输入框，为空时确认按钮禁用 */
  input?: { label: string; placeholder?: string; required?: boolean };
}

type ConfirmFn = (options: ConfirmOptions) => Promise<string | null>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<{
    options: ConfirmOptions;
    resolve: (value: string | null) => void;
  } | null>(null);
  const [text, setText] = useState("");
  const inputId = useId();

  const confirm = useCallback<ConfirmFn>((options) => {
    setText("");
    return new Promise<string | null>((resolve) => {
      setPending({ options, resolve });
    });
  }, []);

  const close = (value: string | null) => {
    pending?.resolve(value);
    setPending(null);
  };

  const options = pending?.options;
  const blocked = Boolean(options?.input?.required) && !text.trim();

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Dialog
        open={Boolean(pending)}
        onClose={() => close(null)}
        title={options?.title ?? ""}
        className="max-w-[460px]"
      >
        {options?.body ? (
          <div className="text-body leading-relaxed text-ink-2">{options.body}</div>
        ) : null}
        {options?.input ? (
          <div className="mt-3 flex flex-col gap-1.5">
            <label htmlFor={inputId} className="text-secondary font-medium text-ink-2">
              {options.input.label}
            </label>
            <input
              id={inputId}
              value={text}
              autoFocus
              placeholder={options.input.placeholder}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !blocked) close(text.trim());
              }}
              className={inputCls}
            />
          </div>
        ) : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button onClick={() => close(null)}>{options?.cancelText ?? "取消"}</Button>
          <Button
            variant={options?.tone === "danger" ? "danger" : "primary"}
            disabled={options?.input ? blocked : false}
            onClick={() => close(options?.input ? text.trim() : "")}
          >
            {options?.confirmText ?? "确定"}
          </Button>
        </div>
      </Dialog>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const fn = useContext(ConfirmContext);
  if (!fn) throw new Error("useConfirm 必须在 ConfirmProvider 内使用");
  return fn;
}

/** 常用组合：一次确认即可，不需要输入。 */
export function useAsk() {
  const confirm = useConfirm();
  return useMemo(
    () => ({
      confirm: (o: ConfirmOptions) => confirm(o).then((v) => v !== null),
      ask: (o: ConfirmOptions) => confirm(o),
    }),
    [confirm],
  );
}

/* =============================================================================
 * 行内操作菜单
 *
 * 表格里一行摆四个按钮的时代结束了：12 列的宽表放不下，而且「删除」和
 * 「详情」挨在一起，误点的代价差着两个数量级。收进一个「更多」里，
 * 破坏性的动作要展开才够得着。
 *
 * 弹层用 position: fixed 而不是 absolute：表格外面套着 overflow-x-auto，
 * absolute 会被裁在滚动容器里，最后一行的菜单直接看不见。
 * ========================================================================== */

export interface RowMenuItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
}

export function RowMenu({ items, label = "更多操作" }: { items: RowMenuItem[]; label?: string }) {
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const WIDTH = 168;

  useEffect(() => {
    if (!pos) return;
    /**
     * 点菜单内部不能关掉自己。
     *
     * 原来是无条件 `setPos(null)`：`mousedown` 就把菜单卸载了，随后的 `click`
     * 落在一个已经不在文档里的节点上，菜单项的 `onClick` 永远不触发——
     * 「点了没反应」就是这么来的。触发按钮也要排除，否则它先被关再被 onClick
     * 重新打开，看起来像抖了一下。
     */
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (popRef.current?.contains(target) || btnRef.current?.contains(target)) return;
      setPos(null);
    };
    // 滚动时直接收掉：菜单是 fixed 的，不跟着行跑，留着就会浮在别处
    const onScroll = () => setPos(null);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPos(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("scroll", onScroll, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("scroll", onScroll, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [pos]);

  const open = () => {
    const rect = btnRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPos({
      top: rect.bottom + 4,
      left: Math.max(8, Math.min(rect.right - WIDTH, window.innerWidth - WIDTH - 8)),
    });
  };

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={Boolean(pos)}
        onClick={() => (pos ? setPos(null) : open())}
        className="inline-flex h-[26px] w-[26px] items-center justify-center rounded-md border border-line-strong bg-paper text-muted transition-colors duration-150 ease-standard hover:border-muted/45 hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
      >
        <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true" fill="currentColor">
          <circle cx="3" cy="8" r="1.4" />
          <circle cx="8" cy="8" r="1.4" />
          <circle cx="13" cy="8" r="1.4" />
        </svg>
      </button>

      {pos ? (
        <div
          role="menu"
          style={{ top: pos.top, left: pos.left, width: WIDTH }}
          onMouseDown={(e) => e.stopPropagation()}
          className="fixed z-[75] overflow-hidden rounded-lg border border-line bg-paper py-1 shadow-overlay"
        >
          {items.map((item) => (
            <button
              key={item.label}
              role="menuitem"
              type="button"
              disabled={item.disabled}
              onClick={() => {
                setPos(null);
                item.onSelect();
              }}
              className={cn(
                "block w-full px-3 py-1.5 text-left text-body transition-colors duration-100",
                item.danger ? "text-danger hover:bg-danger-wash" : "text-ink-2 hover:bg-sunken",
                "disabled:cursor-not-allowed disabled:opacity-45",
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}
