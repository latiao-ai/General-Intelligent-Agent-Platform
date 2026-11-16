import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { CardSkeleton } from "@/components/ui/Skeleton";
import { EmptyState } from "@/components/ui/EmptyState";
import { FilterChip } from "@/components/ui/FilterChip";
import { useToast } from "@/components/ui/Toast";
import { cn } from "@/lib/cn";
import type { PlazaOrder } from "@/lib/types";
import { BotCard } from "./BotCard";
import { BotDrawer } from "./BotDrawer";
import { MineDialog } from "./MineDialog";
import {
  installBot,
  uninstallBot,
  useBotDetail,
  useMine,
  usePlaza,
} from "./usePlaza";

/**
 * Bot 广场。
 *
 * 与旧页的两处刻意差异（都是 DESIGN.md 的规则落地）：
 *  1. 没有 hero。旧页用「把对的人放到对的位置」当 h1，那是营销页的写法；
 *     这里 h1 就是「Bot 广场」，一句话说明 + 紧凑统计，把屏幕让给内容。
 *  2. 所有状态都有位置：加载（骨架）、空（还没人发布）、无结果（筛选没命中）、
 *     出错（带重试）。旧页把后三者挤在同一个空态里。
 */
export function PlazaPage() {
  const toast = useToast();
  const plaza = usePlaza();
  const detail = useBotDetail();
  const mine = useMine();

  const [busyItemId, setBusyItemId] = useState<string | null>(null);
  const [withdrawingId, setWithdrawingId] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  /* 快捷键：`/` 聚焦搜索（旧页也有，保持一致） */
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const el = document.activeElement;
      const typing =
        el instanceof HTMLInputElement ||
        el instanceof HTMLTextAreaElement ||
        (el instanceof HTMLElement && el.isContentEditable);
      if (typing) return;
      event.preventDefault();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const categoryName = useCallback(
    (id?: string | null) => {
      if (!id) return "通用";
      return plaza.data.categories.find((c) => c.id === id)?.name ?? "通用";
    },
    [plaza.data.categories],
  );

  /** 启用：装一份副本 → 带着开场白或直接跳到会话。 */
  const handleUse = useCallback(
    async (itemId: string, starter?: string) => {
      setBusyItemId(itemId);
      try {
        const res = await installBot(itemId);
        const target = res.chat_url || `/api/ui/bots?bot=${encodeURIComponent(res.bot_id)}`;

        if (starter) {
          location.href = `${target}${target.includes("?") ? "&" : "?"}say=${encodeURIComponent(starter)}`;
          return;
        }

        toast(
          res.installed
            ? `已启用「${res.name || "这个 Bot"}」，正在打开对话…`
            : "你已经有它了，正在打开原来的会话…",
        );
        window.setTimeout(() => {
          location.href = target;
        }, 420);
      } catch (err) {
        toast(err instanceof Error ? err.message : String(err), "error");
        setBusyItemId(null);
      }
    },
    [toast],
  );

  const handleUninstall = useCallback(async () => {
    if (!detail.itemId) return;
    if (!window.confirm("卸载会归档这个机器人（对话历史保留）。要继续吗？")) return;

    const id = detail.itemId;
    setBusyItemId(id);
    try {
      await uninstallBot(id);
      toast("已卸载，机器人已归档");
      plaza.markUninstalled(id);
      detail.close();
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusyItemId(null);
    }
  }, [detail, plaza, toast]);

  const handleWithdraw = useCallback(
    async (itemId: string) => {
      if (!window.confirm("撤回后这条会从广场下架（已启用它的用户不受影响）。继续吗？")) {
        return;
      }
      setWithdrawingId(itemId);
      try {
        await mine.withdraw(itemId);
        toast("已撤回");
        plaza.reload();
      } catch (err) {
        toast(err instanceof Error ? err.message : String(err), "error");
      } finally {
        setWithdrawingId(null);
      }
    },
    [mine, plaza, toast],
  );

  const loading = plaza.status === "loading";
  const categoryTotal = plaza.data.categories.reduce((sum, c) => sum + (c.count ?? 0), 0);
  const showSkeleton = loading && plaza.data.items.length === 0;

  return (
    <div className="min-h-dvh bg-paper">
      {/* ---------------- 顶栏 ---------------- */}
      <header className="hairline-b sticky top-0 z-40 bg-paper/92 backdrop-blur-sm">
        <div className="mx-auto flex h-14 max-w-[1200px] items-center gap-3 px-5">
          <div className="flex min-w-0 items-center gap-2.5">
            <div
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-signal text-paper"
              aria-hidden="true"
            >
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
                <path
                  d="M2.5 5.5l5.5 3 5.5-3M2.5 10.5l5.5 3 5.5-3"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <rect
                  x="2.5"
                  y="3"
                  width="11"
                  height="10"
                  rx="2"
                  stroke="currentColor"
                  strokeWidth="1.5"
                />
              </svg>
            </div>
            <div className="min-w-0">
              <h1 className="text-title leading-tight font-semibold text-ink">Bot 广场</h1>
              <p className="hidden truncate text-label text-muted sm:block">
                挑一个现成的 AI 同事，点开就能干活
              </p>
            </div>
          </div>

          {/* 搜索 */}
          <label className="relative ml-2 flex h-[30px] max-w-[400px] min-w-0 flex-1 items-center rounded-md border border-line-strong bg-rail/70 transition-[border-color,background-color,box-shadow] duration-150 ease-standard focus-within:border-signal focus-within:bg-paper focus-within:ring-[3px] focus-within:ring-signal/10">
            <svg
              width="13"
              height="13"
              viewBox="0 0 16 16"
              fill="none"
              className="ml-2.5 shrink-0 text-muted"
              aria-hidden="true"
            >
              <circle cx="7" cy="7" r="4.2" stroke="currentColor" strokeWidth="1.5" />
              <path
                d="M10.2 10.2L13.5 13.5"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
            <input
              ref={searchRef}
              value={plaza.input}
              onChange={(event) => plaza.setInput(event.target.value)}
              placeholder="搜名字、职责、标签…"
              autoComplete="off"
              aria-label="搜索 Bot"
              className="h-full min-w-0 flex-1 bg-transparent px-2 text-body text-ink outline-none placeholder:text-muted"
            />
            <kbd className="mr-2 hidden shrink-0 rounded-sm border border-line-strong bg-paper px-1 font-mono text-label text-muted sm:block">
              /
            </kbd>
          </label>

          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            <Button variant="ghost" onClick={mine.show}>
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path
                  d="M8 2v8M5 7l3 3 3-3M3 12.5h10"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <span className="hidden sm:inline">我的投稿</span>
            </Button>
            <Button variant="secondary" onClick={() => (location.href = "/api/ui/bots")}>
              <span className="hidden sm:inline">我的机器人</span>
              <span className="sm:hidden">工作台</span>
            </Button>
            <Button variant="primary" onClick={() => (location.href = "/api/ui/bots")}>
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path
                  d="M8 3.5v9M3.5 8h9"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
              </svg>
              <span className="hidden sm:inline">创建</span>
            </Button>
          </div>
        </div>
      </header>

      {/* ---------------- 主体 ---------------- */}
      <main className="mx-auto max-w-[1200px] px-5 pb-16">
        {/* 工具栏：统计 + 排序。旧页把统计做成 hero 大数字，这里压成一行 meta。 */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 py-3">
          <p className="text-secondary text-muted">
            <Num>{plaza.data.total}</Num> 个可用的 Bot
            <Sep />
            <Num>{plaza.data.categories.filter((c) => (c.count ?? 0) > 0).length}</Num> 个分类
            <Sep />
            <Num>{plaza.data.installedCount}</Num> 个已在我名下
          </p>

          <div className="ml-auto flex items-center gap-2">
            <label htmlFor="order" className="text-label text-muted">
              排序
            </label>
            <select
              id="order"
              value={plaza.query.order}
              onChange={(event) => plaza.setOrder(event.target.value as PlazaOrder)}
              className="h-[26px] rounded-md border border-line-strong bg-paper px-1.5 text-secondary text-ink-2 outline-none transition-colors duration-150 ease-standard hover:border-muted/45 focus-visible:border-signal focus-visible:ring-[3px] focus-visible:ring-signal/15"
            >
              <option value="featured">推荐优先</option>
              <option value="popular">最常被启用</option>
              <option value="new">最新上架</option>
            </select>
          </div>
        </div>

        {/* 分类 */}
        {plaza.data.categories.length ? (
          <div className="flex flex-wrap gap-1.5 pb-3">
            <FilterChip
              active={plaza.query.category === ""}
              count={categoryTotal}
              onClick={() => plaza.setCategory("")}
            >
              全部
            </FilterChip>
            {plaza.data.categories.map((cat) => (
              <FilterChip
                key={cat.id}
                active={plaza.query.category === cat.id}
                count={cat.count ?? 0}
                onClick={() => plaza.setCategory(cat.id)}
              >
                {cat.name}
              </FilterChip>
            ))}
          </div>
        ) : null}

        {/* 内容 */}
        {showSkeleton ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <CardSkeleton key={i} />
            ))}
          </div>
        ) : plaza.status === "error" ? (
          <EmptyState
            kind="error"
            message={plaza.error ?? undefined}
            action={<Button onClick={plaza.reload}>重试</Button>}
          />
        ) : plaza.data.items.length === 0 ? (
          <EmptyState
            kind={plaza.query.q || plaza.query.category ? "noMatch" : "empty"}
            action={
              plaza.query.q || plaza.query.category ? (
                <Button
                  onClick={() => {
                    plaza.setInput("");
                    plaza.setCategory("");
                  }}
                >
                  清空筛选
                </Button>
              ) : (
                <Button variant="primary" onClick={() => (location.href = "/api/ui/bots")}>
                  去工作台
                </Button>
              )
            }
          />
        ) : (
          <div
            className={cn(
              "grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3",
              // 换筛选时轻微降低不透明度，说明"正在换"，但不遮住内容
              loading && "opacity-60 transition-opacity duration-150",
            )}
            aria-busy={loading}
          >
            {plaza.data.items.map((item) => (
              <BotCard
                key={item.item_id}
                item={item}
                categoryName={categoryName(item.category)}
                busy={busyItemId === item.item_id}
                onOpen={() => void detail.open(item.item_id)}
                onUse={() => void handleUse(item.item_id)}
              />
            ))}
          </div>
        )}
      </main>

      <BotDrawer
        open={detail.itemId !== null}
        detail={detail.detail}
        status={detail.status}
        error={detail.error}
        categoryName={categoryName(detail.detail?.category)}
        busy={busyItemId === detail.itemId}
        onClose={detail.close}
        onUse={(starter) => {
          if (detail.itemId) void handleUse(detail.itemId, starter);
        }}
        onUninstall={() => void handleUninstall()}
        onRetry={() => {
          if (detail.itemId) void detail.open(detail.itemId);
        }}
      />

      <MineDialog
        open={mine.open}
        data={mine.data}
        status={mine.status}
        error={mine.error}
        onClose={mine.hide}
        onWithdraw={(itemId) => void handleWithdraw(itemId)}
        onRetry={() => void mine.reload()}
        busyId={withdrawingId}
      />
    </div>
  );
}

function Num({ children }: { children: React.ReactNode }) {
  return <b className="font-mono font-medium text-ink-2 tabular-nums">{children}</b>;
}

function Sep() {
  return <span className="mx-1.5 text-line-strong" aria-hidden="true">·</span>;
}
