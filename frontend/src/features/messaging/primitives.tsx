/**
 * 这一页的原语：图标、表单控件、卡片外壳、状态点、开关。
 *
 * 与 bots 的 primitives 分成两份而不是共用：两份图标集不一样（这边要 plug /
 * key / pulse / shield，那边要 pin / send / inbox）。共用一个 40 个图标的文件
 * 会让每页都拖进另一半用不到的路径。类名规则一致，因为都来自 DESIGN.md。
 */
import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { cn } from "@/lib/cn";
import type { StateTone } from "./types";

/* ---------------- 图标 ---------------- */

const PATHS: Record<string, string> = {
  plug: "M5.5 2.5v3M10.5 2.5v3M3.5 5.5h9v2a4.5 4.5 0 01-9 0zM8 12v2",
  key: "M10.5 5.5a2.5 2.5 0 11-1.8-.7M8.7 4.8L3 10.5V13h2.5v-1.8H7v-1.7l1.7-1.7",
  pulse: "M1.5 8h2.2l1.6-4 2.2 8 1.7-4.5 1 .5h4.3",
  users:
    "M6 7.6a2.1 2.1 0 100-4.2 2.1 2.1 0 000 4.2zM2.4 13.5c0-1.9 1.6-3.1 3.6-3.1s3.6 1.2 3.6 3.1M11 4.2a2 2 0 010 3.7M12.4 10.7c.9.4 1.5 1.4 1.5 2.8",
  clock: "M8 4.5V8l2.5 1.5M2 8a6 6 0 1112 0A6 6 0 012 8z",
  refresh: "M13 8a5 5 0 11-1.6-3.7M13 2.5V5h-2.5",
  shield: "M8 2.2l5 1.8v4.1c0 2.6-2 4.6-5 5.7-3-1.1-5-3.1-5-5.7V4z",
  external: "M9.5 3H13v3.5M13 3L7.5 8.5M11.5 9.5v3h-8v-8h3",
  arrowLeft: "M13 8H3M6.5 4.5L3 8l3.5 3.5",
  bubble: "M2.5 8.2a5.5 5.5 0 1111 0 5.5 5.5 0 01-11 0zM5 14l2-2.8",
  copy: "M5.5 5.5h7.5v7.5H5.5zM3 10.5H2v-7.5h7.5v1",
  check: "M3.5 8.5l3 3 6-7",
  plus: "M8 3.5v9M3.5 8h9",
  close: "M4.5 4.5l7 7M11.5 4.5l-7 7",
};

export function Icon({
  name,
  size = 13,
  className,
}: {
  name: keyof typeof PATHS | string;
  size?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      className={cn("shrink-0", className)}
    >
      <path
        d={PATHS[name] ?? PATHS.bubble}
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/* ---------------- 表单控件 ---------------- */

const FIELD =
  "w-full rounded-md border border-line-strong bg-paper px-2.5 text-body text-ink " +
  "transition-[border-color,box-shadow] duration-150 ease-standard placeholder:text-muted " +
  "focus:border-signal focus:shadow-[0_0_0_3px_rgb(51_85_127_/_0.10)] focus:outline-none " +
  "disabled:cursor-not-allowed disabled:bg-sunken disabled:opacity-60";

export function TextInput({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(FIELD, "h-[30px]", className)} {...rest} />;
}

export function TextArea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={cn(FIELD, "min-h-[62px] resize-y py-1.5 leading-relaxed", className)}
      {...rest}
    />
  );
}

export function Select({ className, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(FIELD, "h-[30px] cursor-pointer pr-1", className)} {...rest} />;
}

/**
 * 一个字段：标签 + 控件 + 一行解释。
 * `hint` 写"为什么"，不写"必填"——必填用星号，解释留给真正的疑问。
 * 标签和控件分开传（而不是把控件塞进 <label>），否则 Select 会跟着标签文字
 * 挤在同一行里。
 */
export function FieldLabel({
  htmlFor,
  label,
  children,
  hint,
  className,
}: {
  htmlFor?: string;
  label: ReactNode;
  children: ReactNode;
  hint?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <label htmlFor={htmlFor} className="text-label font-medium text-ink-2">
        {label}
      </label>
      {children}
      {hint ? <p className="text-label leading-relaxed text-muted">{hint}</p> : null}
    </div>
  );
}

/* ---------------- 卡片外壳 ---------------- */

/**
 * 一屏 7 张卡。每条卡片头都固定成「图标 + 标题 + 一句它解决什么 + 右侧动作」，
 * 位置一致才能在滚动时靠形状认出自己在哪一块，而不用重新读标题。
 */
export function Section({
  icon,
  title,
  sub,
  actions,
  children,
  className,
}: {
  icon: string;
  title: string;
  sub?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("rounded-xl border border-line bg-paper", className)}>
      <header className="flex items-start gap-2.5 px-4 pt-3.5 pb-3">
        <span
          className="mt-px flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-rail text-muted"
          aria-hidden="true"
        >
          <Icon name={icon} />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="text-body font-semibold text-ink">{title}</h2>
          {sub ? <p className="mt-0.5 text-label leading-relaxed text-muted">{sub}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-1.5">{actions}</div> : null}
      </header>
      <div className="border-t border-line px-4 py-3.5">{children}</div>
    </section>
  );
}

/* ---------------- 状态点 ---------------- */

/**
 * 状态点永远和文字一起出现（`state_label`），不单靠颜色传信息——
 * 红绿色盲用户看到的两个点是一样的。
 */
const DOT_TONE: Record<string, string> = {
  good: "bg-ok",
  warn: "bg-warn",
  bad: "bg-danger",
  muted: "bg-line-strong",
};

export function StateDot({
  tone,
  size = 6,
  className,
}: {
  tone: StateTone | string;
  size?: number;
  className?: string;
}) {
  return (
    <span
      aria-hidden="true"
      style={{ width: size, height: size }}
      className={cn("inline-block shrink-0 rounded-full", DOT_TONE[tone] ?? DOT_TONE.warn, className)}
    />
  );
}

/* ---------------- 开关 ---------------- */

/**
 * 开关。用原生 checkbox + role="switch"，键盘（空格）和中途状态都由浏览器管。
 *
 * 老页的开关只有一个 `title`，既没有可见文字也没有可访问名字——读屏念出来是
 * 「复选框，未选中」，没人知道它开关的是什么。这里补上可见的「启用」。
 */
export function Switch({
  checked,
  onChange,
  disabled,
  label,
  busy,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label: string;
  busy?: boolean;
}) {
  return (
    <label
      className={cn(
        "inline-flex items-center gap-2 text-secondary text-ink-2 select-none",
        disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
      )}
    >
      <span className="relative inline-flex h-[20px] w-[34px] shrink-0 items-center">
        <input
          type="checkbox"
          role="switch"
          checked={checked}
          disabled={disabled || busy}
          onChange={(event) => onChange(event.target.checked)}
          className="peer sr-only"
        />
        <span
          className={cn(
            "absolute inset-0 rounded-full bg-line-strong transition-colors duration-150 ease-standard",
            "peer-checked:bg-signal",
            "peer-focus-visible:ring-[3px] peer-focus-visible:ring-signal/25",
          )}
        />
        <span
          className={cn(
            "absolute left-[2px] h-[16px] w-[16px] rounded-full bg-paper shadow-lift",
            "transition-transform duration-150 ease-standard peer-checked:translate-x-[14px]",
          )}
        />
      </span>
      <span>{busy ? "保存中…" : label}</span>
    </label>
  );
}

/* ---------------- 时间 ---------------- */

/** 后端给 ISO 字符串（有时带时区、有时不带），转成本地 `MM-DD HH:MM:SS`。 */
export function formatTime(iso?: string): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}:${pad(date.getSeconds())}`;
}
