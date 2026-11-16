import { memo } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/cn";
import type { BotItem } from "@/lib/types";

/**
 * 广场卡片。
 *
 * 可访问性上的一个取舍：整卡可点，但卡片里还有一个「使用」按钮。
 * 如果给 <article> 加 role="button"，就成了按钮套按钮，屏幕阅读器
 * 会念出两个层级同名的控件。所以这里用"覆盖层"：一个绝对定位的透明
 * <button> 铺满卡片负责打开详情，内容层 pointer-events-none，
 * 只有真正的按钮恢复 pointer-events-auto。
 */
export const BotCard = memo(function BotCard({
  item,
  categoryName,
  busy,
  onOpen,
  onUse,
}: {
  item: BotItem;
  categoryName: string;
  busy: boolean;
  onOpen: () => void;
  onUse: () => void;
}) {
  const installed = Boolean(item.installed);
  const subtitle = item.title || item.description || "还没有写职责";
  const tags = (item.tags ?? []).slice(0, 3);

  return (
    <article
      className={cn(
        "group relative flex flex-col rounded-lg border border-line bg-paper p-3.5",
        "transition-[transform,border-color,box-shadow] duration-150 ease-standard",
        "hover:-translate-y-0.5 hover:border-line-strong hover:shadow-hover",
        "focus-within:border-signal/45",
      )}
    >
      <button
        type="button"
        onClick={onOpen}
        aria-label={`查看「${item.name}」详情`}
        className="absolute inset-0 z-0 cursor-pointer rounded-lg focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25 focus-visible:ring-inset"
      />

      <div className="pointer-events-none relative z-[1] flex min-h-0 flex-1 flex-col">
        {/* 头部：头像 + 名称 + handle */}
        <div className="flex items-start gap-2.5">
          <Avatar
            size={44}
            image={item.image}
            icon={item.icon}
            color={item.color}
            shape={item.shape}
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <h3 className="truncate text-body font-semibold text-ink">{item.name}</h3>
              {item.featured ? (
                <span className="shrink-0 text-warn" title="平台推荐">
                  <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                    <path d="M8 1.5l1.9 4.1 4.5.6-3.3 3.1.8 4.4L8 11.6l-3.9 2.1.8-4.4L1.6 6.2l4.5-.6L8 1.5z" />
                  </svg>
                  <span className="sr-only">平台推荐</span>
                </span>
              ) : null}
              {installed ? <Badge tone="ok">已启用</Badge> : null}
            </div>
            <p className="mt-0.5 truncate font-mono text-label text-muted">
              @{item.handle || "bot"} · {categoryName}
            </p>
          </div>
        </div>

        {/* 职责一句话 */}
        <p className="mt-2.5 line-clamp-1 text-body text-ink">{subtitle}</p>

        {/* 描述两行 */}
        <p className="mt-1 line-clamp-2 min-h-[2.6em] text-secondary leading-relaxed text-muted">
          {item.description || ""}
        </p>

        {tags.length ? (
          <div className="mt-2 flex flex-wrap gap-1">
            {tags.map((tag) => (
              <span
                key={tag}
                className="rounded-sm bg-sunken px-1.5 py-0.5 text-label text-ink-2"
              >
                {tag}
              </span>
            ))}
          </div>
        ) : null}

        {/* 底部：用量 + 动作 */}
        <div className="mt-auto flex items-center justify-between gap-2 pt-3">
          <span className="text-label text-muted">
            <b className="font-mono font-medium text-ink-2 tabular-nums">
              {item.install_count ?? 0}
            </b>{" "}
            人用过
          </span>
          <button
            type="button"
            disabled={busy}
            onClick={onUse}
            className={cn(
              "pointer-events-auto inline-flex h-[26px] items-center gap-1 rounded-md border px-2 text-secondary font-medium",
              "transition-[background-color,border-color,color] duration-150 ease-standard",
              "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
              "disabled:cursor-not-allowed disabled:opacity-50",
              installed
                ? "border-line-strong bg-paper text-ink-2 hover:border-signal/40 hover:text-signal-deep"
                : "border-transparent bg-signal text-paper hover:bg-signal-deep",
            )}
          >
            {busy ? "打开中…" : installed ? "打开对话" : "使用"}
            <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path
                d="M3 8h9M8.5 4.5L12 8l-3.5 3.5"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      </div>
    </article>
  );
});
