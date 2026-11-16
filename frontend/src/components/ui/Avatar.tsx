import { cn } from "@/lib/cn";

/**
 * 头像。
 *
 * 契约与旧页一致：有 image 用图，否则渲染 icon 字符（默认 🤖）打在 color 底上。
 * 三种形状：默认圆角方、round（全圆）、sq（直角）。
 * 加载失败的图不显示裂图——退回 icon。
 */

const SHAPES = {
  round: "rounded-full",
  sq: "rounded-sm",
  default: "rounded-lg",
} as const;

export function Avatar({
  size = 44,
  image,
  icon,
  color,
  shape,
  name,
  className,
}: {
  size?: number;
  image?: string | null;
  icon?: string | null;
  color?: string | null;
  shape?: string | null;
  name?: string;
  className?: string;
}) {
  const radius =
    shape === "round" ? SHAPES.round : shape === "sq" ? SHAPES.sq : SHAPES.default;

  return (
    <div
      className={cn(
        "flex shrink-0 items-center justify-center overflow-hidden select-none",
        radius,
        className,
      )}
      style={{
        width: size,
        height: size,
        background: color || "var(--color-signal-wash)",
        fontSize: Math.round(size * 0.5),
      }}
      aria-hidden={name ? undefined : true}
      role={name ? "img" : undefined}
      aria-label={name}
    >
      {image ? (
        <img
          src={image}
          alt=""
          loading="lazy"
          decoding="async"
          className="h-full w-full object-cover"
          onError={(event) => {
            // 裂图比灰色占位更难看：出错就把 img 摘掉，让下面的 icon 露出来
            event.currentTarget.style.display = "none";
          }}
        />
      ) : (
        <span>{icon || "🤖"}</span>
      )}
    </div>
  );
}
