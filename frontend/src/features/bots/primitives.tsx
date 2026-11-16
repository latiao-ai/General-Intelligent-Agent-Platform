/**
 * 表单原语与图标。
 *
 * 这些不是"design system 组件"，只是把同一组类名收在一处——旧页有 48KB 内联
 * 样式，其中相当一部分是同一个输入框被重复描述了十几遍。集中一次，
 * 改一处就全场生效。
 *
 * 规则全部来自 DESIGN.md：控件 30px 高、圆角 8px、轻描边、focus 用外环。
 */
import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { cn } from "@/lib/cn";

const FIELD_BASE =
  "w-full rounded-md border border-line-strong bg-paper px-2.5 text-body text-ink " +
  "transition-[border-color,box-shadow] duration-150 ease-standard " +
  "placeholder:text-muted " +
  "focus:border-signal focus:shadow-[0_0_0_3px_rgb(51_85_127_/_0.10)] focus:outline-none " +
  "disabled:cursor-not-allowed disabled:bg-sunken disabled:opacity-60";

export function TextInput({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(FIELD_BASE, "h-[30px]", className)} {...rest} />;
}

export function TextArea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={cn(FIELD_BASE, "min-h-[64px] resize-y py-1.5 leading-relaxed", className)}
      {...rest}
    />
  );
}

export function Select({ className, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(FIELD_BASE, "h-[30px] cursor-pointer pr-1", className)} {...rest} />;
}

/** 一个字段：标签 + 控件 + 一行解释。`tip` 写"为什么"，不写"必填"。 */
export function Field({
  label,
  htmlFor,
  tip,
  required,
  children,
  className,
}: {
  label: string;
  htmlFor?: string;
  tip?: ReactNode;
  required?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={htmlFor} className="text-label font-medium text-ink-2">
        {label}
        {required ? <span className="ml-0.5 text-danger">*</span> : null}
      </label>
      {children}
      {tip ? <p className="text-label leading-relaxed text-muted">{tip}</p> : null}
    </div>
  );
}

/** 复选行：技能、连接器、群成员都用它，保证三处手感一致。 */
export function CheckRow({
  checked,
  onChange,
  title,
  hint,
  leading,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  title: string;
  hint?: string;
  leading?: ReactNode;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 transition-colors duration-150 ease-standard hover:bg-sunken">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-[2px] h-3.5 w-3.5 shrink-0 accent-signal"
      />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5 text-body font-medium text-ink">
          {leading}
          <span className="truncate">{title}</span>
        </span>
        {hint ? (
          <span className="mt-0.5 block truncate text-label text-muted">{hint}</span>
        ) : null}
      </span>
    </label>
  );
}

/* ---------------- 图标 ---------------- */

const PATHS: Record<string, string> = {
  chevron: "M6 4l4 4-4 4",
  close: "M4 4l8 8M12 4l-8 8",
  plus: "M8 3v10M3 8h10",
  pin: "M9.5 2.5l4 4-2.2.6-2.6 2.6-.3 2.4-1.4 1.4-1.8-3-2.8 2.8",
  send: "M3 8l10-5-4 10-1.6-3.8L3 8z",
  arrowUp: "M8 13V3M4 7l4-4 4 4",
  bubble: "M2.5 8a5.5 5.5 0 1111 0 5.5 5.5 0 01-11 0zM5 14l2-3",
  clock: "M8 4.5V8l2.5 1.5M2 8a6 6 0 1112 0A6 6 0 012 8z",
  grid: "M2.5 2.5h4.5v4.5H2.5zM9 2.5h4.5v4.5H9zM2.5 9h4.5v4.5H2.5zM9 9h4.5v4.5H9z",
  robot: "M5 6.5h6a2 2 0 012 2V12a2 2 0 01-2 2H5a2 2 0 01-2-2V8.5a2 2 0 012-2zM6 3.5h4M8 3.5v3M4 11h.01M12 11h.01",
  refresh: "M13 8a5 5 0 11-1.6-3.7M13 2.5V5h-2.5",
  share: "M6 8.5l4.5 2.5M10.5 4.5L6 7.5M12 2.5a1.5 1.5 0 100 3 1.5 1.5 0 000-3zM4.5 6.5a1.5 1.5 0 100 3 1.5 1.5 0 000-3zM12 10.5a1.5 1.5 0 100 3 1.5 1.5 0 000-3z",
  inbox: "M2.5 9.5l1.8-6h7.4l1.8 6V13H2.5zM2.5 9.5h3l1 1.5h3l1-1.5h3",
  checklist: "M6 4.5h7M6 8h7M6 11.5h7M3 4.5h.01M3 8h.01M3 11.5h.01",
  trash: "M3.5 5h9M6 5V3.5h4V5M5 5l.5 8h5l.5-8",
  robotTeam: "M4 10.5a2 2 0 012-2h4a2 2 0 012 2V12a2 2 0 01-2 2H6a2 2 0 01-2-2zM6 6.5h4M8 4v2.5",
};

export function Icon({
  name,
  size = 14,
  className,
}: {
  name: keyof typeof PATHS | string;
  size?: number;
  className?: string;
}) {
  const d = PATHS[name] ?? PATHS.close;
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
        d={d}
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
