import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * 按钮。
 *
 * 规则来自 DESIGN.md：高度 30px、圆角 8px、无静态阴影。
 * 四档权重：primary（每屏最多一个）/ secondary / ghost / danger。
 * focus 用外环补齐，不依赖浏览器默认 outline。
 */

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const base =
  "inline-flex items-center justify-center gap-1.5 rounded-md border font-medium " +
  "transition-[background-color,border-color,color,box-shadow] duration-150 ease-standard " +
  "disabled:cursor-not-allowed disabled:opacity-45 " +
  "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25";

const variants: Record<Variant, string> = {
  primary: "border-transparent bg-signal text-paper hover:bg-signal-deep",
  secondary: "border-line-strong bg-paper text-ink-2 hover:border-muted/45 hover:text-ink",
  ghost: "border-transparent bg-transparent text-ink-2 hover:bg-sunken hover:text-ink",
  danger: "border-transparent bg-danger-wash text-danger hover:bg-danger hover:text-paper",
};

const sizes: Record<Size, string> = {
  sm: "h-[26px] px-2 text-secondary",
  md: "h-[30px] px-3 text-body",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  children?: ReactNode;
}

export function Button({
  variant = "secondary",
  size = "md",
  className,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cn(base, variants[variant], sizes[size], className)}
      {...rest}
    />
  );
}
