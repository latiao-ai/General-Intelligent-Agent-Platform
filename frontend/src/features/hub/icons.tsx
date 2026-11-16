/**
 * 图标：24×24 线性图标，`stroke=currentColor`。
 *
 * 路径逐字搬自旧页的 `<symbol>` 定义——用户对这几个形状已经形成肌肉记忆
 * （尤其右下角那个"发送"箭头和左上角的机器人），换一套几何不如照抄。
 */
import { cn } from "@/lib/cn";

const ICONS: Record<string, string[]> = {
  plus: ["M12 5v14M5 12h14"],
  search: ["M11 11m-7 0a7 7 0 1 0 14 0a7 7 0 1 0-14 0", "M20 20l-3.2-3.2"],
  panel: ["M3 7a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3z", "M9 4v16"],
  bot: ["M4 11a4 4 0 0 1 4-4h8a4 4 0 0 1 4 4v4a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z", "M12 3v4", "M9.5 16.4h5"],
  message: [
    "M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H10l-4.4 3.4A.8.8 0 0 1 4.3 18.8z",
    "M8.4 9.2h7.2M8.4 12.4h4.6",
  ],
  spark: [
    "M12 3.2l1.9 5.5 5.5 1.9-5.5 1.9L12 18l-1.9-5.5L4.6 10.6l5.5-1.9z",
    "M18.4 16.6l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z",
  ],
  archive: [
    "M3 6a1.4 1.4 0 0 1 1.4-1.5h15.2A1.4 1.4 0 0 1 21 6v2.5a1.4 1.4 0 0 1-1.4 1.4H4.4A1.4 1.4 0 0 1 3 8.5z",
    "M5 9v9.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V9",
    "M10 13h4",
  ],
  grid: [
    "M5.6 4h1.4a1.6 1.6 0 0 1 1.6 1.6v1.4a1.6 1.6 0 0 1-1.6 1.6H5.6A1.6 1.6 0 0 1 4 7V5.6A1.6 1.6 0 0 1 5.6 4z",
    "M15.2 4H18.4A1.6 1.6 0 0 1 20 5.6V7a1.6 1.6 0 0 1-1.6 1.6h-3.2A1.6 1.6 0 0 1 13.6 7V5.6A1.6 1.6 0 0 1 15.2 4z",
    "M5.6 13.6H7a1.6 1.6 0 0 1 1.6 1.6v3.2A1.6 1.6 0 0 1 7 20H5.6A1.6 1.6 0 0 1 4 18.4v-3.2a1.6 1.6 0 0 1 1.6-1.6z",
    "M15.2 13.6h3.2A1.6 1.6 0 0 1 20 15.2v3.2A1.6 1.6 0 0 1 18.4 20h-3.2a1.6 1.6 0 0 1-1.6-1.6v-3.2a1.6 1.6 0 0 1 1.6-1.6z",
  ],
  dots: ["M5.5 12h.01M12 12h.01M18.5 12h.01"],
  chevron: ["M6 9.5l6 6 6-6"],
  close: ["M6.5 6.5l11 11M17.5 6.5l-11 11"],
  arrowUp: ["M12 19V5.5", "M6.2 11.3L12 5.5l5.8 5.8"],
  external: ["M14 4h6v6", "M20 4l-8.5 8.5", "M18 14.5V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3.5"],
  slash: ["M14.5 4.5l-5 15"],
  users: ["M9.5 5.1a3.4 3.4 0 1 0 0 6.8 3.4 3.4 0 0 0 0-6.8z", "M3.6 20a6 6 0 0 1 11.8 0", "M16.4 5.4a3.2 3.2 0 0 1 0 6.2", "M17.6 14.4a5.6 5.6 0 0 1 3.2 5"],
  pin: ["M9.4 3.8h5.2l-.9 6.4 3.9 3.1H6.4l3.9-3.1z", "M12 13.3V20"],
  gear: [
    "M12 8.9a3.1 3.1 0 1 0 0 6.2 3.1 3.1 0 0 0 0-6.2z",
    "M12 3.6v2.2M12 18.2v2.2M4.8 12H7M17 12h2.2M6.9 6.9l1.6 1.6M15.5 15.5l1.6 1.6M17.1 6.9l-1.6 1.6M8.5 15.5l-1.6 1.6",
  ],
  mic: ["M9 6a3 3 0 0 1 6 0v4.5a3 3 0 0 1-6 0z", "M5.5 11.5a6.5 6.5 0 0 0 13 0", "M12 18v3"],
  shield: ["M12 3.6l7 2.6v5.3c0 4.3-2.9 7.4-7 8.9-4.1-1.5-7-4.6-7-8.9V6.2z", "M9.2 12.1l2 2 3.6-3.9"],
  refresh: ["M20 12a8 8 0 1 1-2.6-5.9", "M20 4v4.4h-4.4"],
  doc: ["M13.5 3.5H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z", "M13.5 3.5V9H19", "M9 13h6M9 16.5h4"],
  pulse: ["M3 12h4l2.2-5.5 3.2 11L15 12h6"],
  paperclip: [
    "M19.4 11.6l-7.3 7.3a4.6 4.6 0 0 1-6.5-6.5l7.8-7.8a3 3 0 1 1 4.2 4.2l-7.6 7.6a1.5 1.5 0 0 1-2.1-2.1l6.8-6.8",
  ],
  logout: ["M14 4.5h4a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-4", "M10 8l-4 4 4 4", "M6 12h9"],
  key: ["M8.2 11.6a4.2 4.2 0 1 0 0 8.4 4.2 4.2 0 0 0 0-8.4z", "M11.3 12.7l8.4-8.4", "M16.4 4.6l3 3", "M14.6 9.4l2.4 2.4"],
  plug: ["M9 3.2v4.6M15 3.2v4.6", "M6.6 7.8h10.8v3.4a5.4 5.4 0 0 1-10.8 0z", "M12 16.6v4.2"],
  folder: ["M4 8.9A1.9 1.9 0 0 1 5.9 7h3.3l1.9 2.3h7A1.9 1.9 0 0 1 20 11.2v6.9a1.9 1.9 0 0 1-1.9 1.9H5.9A1.9 1.9 0 0 1 4 18.1z"],
  caret: ["M9.5 6l6 6-6 6"],
  clock: ["M12 3.6a8.4 8.4 0 1 0 0 16.8 8.4 8.4 0 0 0 0-16.8z", "M12 7.6V12l3 1.9"],
  check: ["M5 12.5l4.2 4.2L19 7"],
  dot: ["M12 12h.01"],
};

export type IconName = keyof typeof ICONS;

export function Icon({
  name,
  size = 16,
  className,
  strokeWidth = 1.7,
}: {
  name: IconName | string;
  size?: number;
  className?: string;
  strokeWidth?: number;
}) {
  const paths = ICONS[name] ?? ICONS["dot"] ?? [];
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={cn("shrink-0", className)}
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {paths.map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

/** 品牌标记：三个白色圆点连成的"分发"形状，与旧页 `<use href="#i-brand">` 一致。 */
export function BrandMark({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="6.6" r="2.9" fill="#fff" />
      <circle cx="6.6" cy="16.4" r="2.9" fill="#fff" opacity=".82" />
      <circle cx="17.4" cy="16.4" r="2.9" fill="#fff" opacity=".82" />
      <path
        d="M12 9.5v3.2M9.6 14.9l-1.2.9M14.4 14.9l1.2.9"
        stroke="#fff"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}
