import { Avatar } from "@/components/ui/Avatar";
import { Badge, type BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import type { MineResponse, PublishStatus } from "@/lib/types";

/**
 * 我的投稿。
 *
 * 这里刻意分成两段：我投稿的（我对它负责）、我装下的（我拥有副本）。
 * 旧页把两段塞进一个列表，只用一个内联标题分隔；语义上是两种东西：
 * 前者可以撤回，后者只能打开。分组后每行只剩一种动作。
 */

const STATUS_TEXT: Record<PublishStatus, string> = {
  pending: "审核中",
  published: "已上架",
  rejected: "未通过",
  offline: "已下架",
  draft: "草稿",
};

const STATUS_TONE: Record<PublishStatus, BadgeTone> = {
  pending: "warn",
  published: "ok",
  rejected: "danger",
  offline: "neutral",
  draft: "neutral",
};

export function MineDialog({
  open,
  data,
  status,
  error,
  onClose,
  onWithdraw,
  onRetry,
  busyId,
}: {
  open: boolean;
  data: MineResponse | null;
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
  onClose: () => void;
  onWithdraw: (itemId: string) => void;
  onRetry: () => void;
  busyId: string | null;
}) {
  const items = data?.items ?? [];
  const installs = data?.installs ?? [];
  const count = `${items.length} 条投稿 · ${installs.length} 个已装`;

  return (
    <Dialog open={open} onClose={onClose} title="我的投稿" description={count}>
      {status === "loading" ? (
        <div className="space-y-2">
          {[0, 1, 2].map((row) => (
            <Skeleton key={row} className="h-14 w-full rounded-lg" />
          ))}
        </div>
      ) : status === "error" ? (
        <EmptyState
          kind="error"
          message={error ?? undefined}
          action={<Button onClick={onRetry}>重试</Button>}
        />
      ) : items.length === 0 && installs.length === 0 ? (
        <EmptyState
          kind="empty"
          message="在机器人工作台里选一个自己调好的 Bot，点右侧「发布到广场」。"
          action={
            <Button variant="primary" onClick={() => (location.href = "/api/ui/bots")}>
              去工作台
            </Button>
          }
        />
      ) : (
        <div className="space-y-5">
          {items.length ? (
            <section>
              <h3 className="mb-1.5 text-label font-medium tracking-wide text-muted uppercase">
                我投稿的
              </h3>
              <ul className="divide-y divide-line rounded-lg border border-line">
                {items.map((item) => (
                  <li key={item.item_id} className="flex items-center gap-2.5 px-3 py-2.5">
                    <Avatar size={34} icon="📦" />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="truncate text-body font-medium text-ink">
                          {item.name}
                        </span>
                        <Badge tone={STATUS_TONE[item.status] ?? "neutral"}>
                          {STATUS_TEXT[item.status] ?? item.status}
                        </Badge>
                        <Badge tone="neutral">
                          {item.scope === "public" ? "全平台" : "本企业"}
                        </Badge>
                      </div>
                      <p className="mt-0.5 truncate text-label text-muted">
                        {item.install_count ?? 0} 人用过 · v{item.version ?? 1}
                        {item.review_note ? ` · ${item.review_note}` : ""}
                      </p>
                    </div>
                    <Button
                      size="sm"
                      onClick={() => {
                        if (item.source_bot_id) {
                          location.href = `/api/ui/bots?bot=${encodeURIComponent(item.source_bot_id)}`;
                        }
                      }}
                      disabled={!item.source_bot_id}
                    >
                      去工作台
                    </Button>
                    {item.status === "pending" || item.status === "published" ? (
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => onWithdraw(item.item_id)}
                        disabled={busyId === item.item_id}
                        aria-label={`撤回「${item.name}」`}
                      >
                        {busyId === item.item_id ? "撤回中…" : "撤回"}
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {installs.length ? (
            <section>
              <h3 className="mb-1.5 text-label font-medium tracking-wide text-muted uppercase">
                已装在我名下
              </h3>
              <ul className="divide-y divide-line rounded-lg border border-line">
                {installs.map((row) => (
                  <li key={row.bot_id} className="flex items-center gap-2.5 px-3 py-2.5">
                    <Avatar size={34} icon={row.icon} color={row.color} />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-body font-medium text-ink">
                        {row.name || row.item_id}
                      </p>
                      <p className="mt-0.5 truncate text-label text-muted">
                        条目 v{row.item_version ?? 1} · 装于{" "}
                        {(row.created_at || "").slice(0, 10) || "—"}
                      </p>
                    </div>
                    <a
                      href={`/api/ui/bots?bot=${encodeURIComponent(row.bot_id)}`}
                      className="inline-flex h-[26px] items-center rounded-md border border-line-strong px-2 text-secondary font-medium text-ink-2 transition-colors duration-150 ease-standard hover:border-signal/40 hover:text-signal-deep focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
                    >
                      打开
                    </a>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      )}
    </Dialog>
  );
}
