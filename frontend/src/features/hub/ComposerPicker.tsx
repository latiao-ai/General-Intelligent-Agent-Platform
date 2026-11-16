/**
 * 输入框上方的选择面板：`＋` 打开它，`@` 也打开它。
 *
 * 只有一种面板（不是旧页那种「＋ 菜单 → 二级子菜单」）：`＋` 与 `@` 的区别只是
 * 前者从零搜、后者带着已经打出来的字过滤。少一层菜单，就少一次「点了没反应」。
 *
 * 键盘全交给外面：输入框里的 ↑↓/Enter 和面板搜索框里的必须是同一套行为，
 * 分两处实现迟早会不一致。
 */
import { useEffect, useRef } from "react";
import { cn } from "@/lib/cn";
import { Icon, type IconName } from "./icons";

export interface PickerItem {
  key: string;
  label: string;
  /** 右侧的次要信息：工具数、文件大小、所在目录 */
  hint?: string;
  /** 标签旁的补充说明：已在引用里 / 待授权 */
  note?: string;
  /** 分组名，用来插分隔标题 */
  group: string;
  icon: IconName;
  disabled?: boolean;
  onPick: () => void;
}

export function ComposerPicker({
  items,
  active,
  loading,
  error,
  query,
  searchable,
  footer,
  onQuery,
  onKeyDown,
  onHover,
}: {
  items: PickerItem[];
  active: number;
  loading?: boolean;
  error?: string | null;
  query: string;
  /** `＋` 打开时要给一个搜索框；`@` 打开时用户在输入框里已经打着了 */
  searchable: boolean;
  footer?: string;
  onQuery: (next: string) => void;
  onKeyDown: (event: React.KeyboardEvent) => void;
  onHover: (index: number) => void;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (searchable) inputRef.current?.focus();
  }, [searchable]);

  // 键盘移到看不见的地方时滚回来。`block: nearest` 不会把整页带着滚。
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>('[data-active="true"]');
    el?.scrollIntoView({ block: "nearest" });
  }, [active, items.length]);

  let lastGroup = "";

  return (
    <div
      className={cn(
        "absolute bottom-[calc(100%+8px)] left-0 z-40 flex w-[min(420px,calc(100vw-56px))] flex-col overflow-hidden",
        // 面板是往上长的：不给上限，连接器一多就会顶出视口，搜索框跑到屏幕外
        "max-h-[min(440px,calc(100vh-200px))]",
        "rounded-xl border border-line bg-paper shadow-[var(--shadow-overlay)]",
      )}
      role="listbox"
      aria-label="选择要引用的内容"
    >
      {searchable ? (
        <div className="hairline-b flex shrink-0 items-center gap-2 px-2.5 py-2">
          <Icon name="search" size={13} className="shrink-0 text-muted" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => onQuery(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="搜索技能、文件、连接器"
            aria-label="搜索要引用的内容"
            spellCheck={false}
            className="min-w-0 flex-1 border-0 bg-transparent text-secondary text-ink outline-none placeholder:text-muted"
          />
        </div>
      ) : null}

      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto py-1">
        {loading ? (
          <p className="px-3 py-3 text-label text-muted">读取中…</p>
        ) : error ? (
          <p className="px-3 py-3 text-label leading-relaxed text-danger">{error}</p>
        ) : items.length === 0 ? (
          <p className="px-3 py-3 text-label leading-relaxed text-muted">
            {query.trim() ? `没有匹配「${query.trim()}」的内容` : "这里还没有可引用的内容"}
          </p>
        ) : (
          items.map((item, index) => {
            const header = item.group !== lastGroup ? item.group : null;
            lastGroup = item.group;
            return (
              <div key={item.key}>
                {header ? (
                  <p className="px-2.5 pt-1.5 pb-0.5 text-label text-muted">{header}</p>
                ) : null}
                <button
                  type="button"
                  role="option"
                  aria-selected={index === active}
                  data-active={index === active ? "true" : undefined}
                  disabled={item.disabled}
                  onMouseMove={() => onHover(index)}
                  onClick={() => item.onPick()}
                  className={cn(
                    "flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition-colors duration-100",
                    item.disabled
                      ? "cursor-not-allowed text-muted"
                      : index === active
                        ? "bg-sunken text-ink"
                        : "text-ink-2",
                  )}
                >
                  <Icon name={item.icon} size={13} className="shrink-0 text-muted" />
                  <span className="min-w-0 flex-1 truncate text-secondary">{item.label}</span>
                  {item.note ? (
                    <span className="shrink-0 text-label text-muted">{item.note}</span>
                  ) : null}
                  {item.hint ? (
                    <span className="shrink-0 font-mono text-label text-muted">{item.hint}</span>
                  ) : null}
                </button>
              </div>
            );
          })
        )}
      </div>

      {footer ? (
        <p className="hairline-t shrink-0 px-2.5 py-1.5 text-label leading-relaxed text-muted">
          {footer}
        </p>
      ) : null}
    </div>
  );
}
