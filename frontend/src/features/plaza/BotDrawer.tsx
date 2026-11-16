import { Avatar } from "@/components/ui/Avatar";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import type { BotDetail } from "@/lib/types";

/**
 * Bot 详情抽屉。
 *
 * 信息顺序按"用户决策路径"排：它能做什么 → 详细介绍 → 拿它做这些事 →
 * 它带着什么。旧页也是这个顺序，没改，只是把字号层级从 1.07:1 拉开到
 * 11/12/13/16 四档。
 */
export function BotDrawer({
  open,
  detail,
  status,
  error,
  categoryName,
  busy,
  onClose,
  onUse,
  onUninstall,
  onRetry,
}: {
  open: boolean;
  detail: BotDetail | null;
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
  categoryName: string;
  busy: boolean;
  onClose: () => void;
  onUse: (starter?: string) => void;
  onUninstall: () => void;
  onRetry: () => void;
}) {
  const installed = Boolean(detail?.installed);
  const loading = status === "loading";

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title="Bot 详情"
      header={
        <div className="flex items-start gap-3 px-5 py-4">
          {loading || !detail ? (
            <>
              <Skeleton className="h-13 w-13 shrink-0 rounded-lg" />
              <div className="flex-1 space-y-2 pt-1">
                <Skeleton className="h-3.5 w-2/5" />
                <Skeleton className="h-2.5 w-3/5" />
              </div>
            </>
          ) : (
            <>
              <Avatar
                size={52}
                image={detail.image}
                icon={detail.icon}
                color={detail.color}
                shape={detail.shape}
              />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <h2 className="text-title font-semibold text-ink">{detail.name}</h2>
                  {detail.featured ? <Badge tone="warn">推荐</Badge> : null}
                  {detail.installed ? <Badge tone="ok">已启用</Badge> : null}
                </div>
                <p className="mt-1 truncate font-mono text-label text-muted">
                  @{detail.handle || "bot"} · {categoryName} · {detail.install_count ?? 0} 人用过
                </p>
              </div>
            </>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭详情"
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path
                d="M4 4l8 8M12 4l-8 8"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>
      }
      footer={
        detail ? (
          <div className="flex items-center gap-2">
            <Button variant="primary" onClick={() => onUse()} disabled={busy}>
              {busy ? "打开中…" : installed ? "打开对话" : "使用这个 Bot"}
            </Button>
            {installed ? (
              <Button variant="danger" onClick={onUninstall} disabled={busy}>
                卸载
              </Button>
            ) : null}
            <span className="ml-auto text-label text-muted">
              点「使用」= 在我名下建一份副本
            </span>
          </div>
        ) : null
      }
    >
      {status === "error" ? (
        <EmptyState
          kind="error"
          message={error ?? undefined}
          action={
            <Button variant="secondary" onClick={onRetry}>
              重试
            </Button>
          }
        />
      ) : loading || !detail ? (
        <div className="space-y-6">
          {[0, 1, 2].map((section) => (
            <div key={section} className="space-y-2">
              <Skeleton className="h-2.5 w-20" />
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-4/5" />
            </div>
          ))}
        </div>
      ) : (
        <div className="space-y-6">
          <Section title="它能做什么">
            <p className="text-body text-ink">{detail.title || "作者只写了描述。"}</p>
            <p className="mt-1.5 text-secondary leading-relaxed text-muted">
              {detail.description || ""}
            </p>
            {detail.tags?.length ? (
              <div className="mt-2.5 flex flex-wrap gap-1">
                {detail.tags.map((tag) => (
                  <span
                    key={tag}
                    className="rounded-sm bg-sunken px-1.5 py-0.5 text-label text-ink-2"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            ) : null}
          </Section>

          <Section title="详细介绍">
            {detail.readme ? (
              <p className="whitespace-pre-wrap text-secondary leading-relaxed text-ink-2">
                {detail.readme}
              </p>
            ) : (
              <p className="text-secondary text-muted">作者没有写详细介绍。</p>
            )}
          </Section>

          {detail.starters?.length ? (
            <Section title="拿它做这些事">
              <div className="flex flex-col gap-1.5">
                {detail.starters.map((line, index) => (
                  <button
                    key={`${index}-${line}`}
                    type="button"
                    onClick={() => onUse(line)}
                    disabled={busy}
                    className={cn(
                      "group flex items-start gap-2 rounded-md border border-line bg-paper px-2.5 py-2 text-left",
                      "transition-[background-color,border-color] duration-150 ease-standard",
                      "hover:border-signal/40 hover:bg-signal-wash/60",
                      "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
                      "disabled:cursor-not-allowed disabled:opacity-50",
                    )}
                  >
                    <svg
                      width="12"
                      height="12"
                      viewBox="0 0 16 16"
                      fill="none"
                      className="mt-[3px] shrink-0 text-signal"
                      aria-hidden="true"
                    >
                      <path
                        d="M8 1.8l1.5 3.6 3.9.3-3 2.6.9 3.8L8 10.2l-3.3 1.9.9-3.8-3-2.6 3.9-.3L8 1.8z"
                        stroke="currentColor"
                        strokeWidth="1.3"
                        strokeLinejoin="round"
                      />
                    </svg>
                    <span className="text-secondary leading-relaxed text-ink-2 group-hover:text-ink">
                      {line}
                    </span>
                  </button>
                ))}
              </div>
            </Section>
          ) : null}

          <Section title="它带着什么">
            <dl className="divide-y divide-line rounded-lg border border-line">
              <KV label="人设" value={detail.soul ? "已写好（启用后可改）" : "通用助手"} />
              <KV label="技能" value={`${detail.skill_ids?.length ?? 0} 个`} mono />
              <KV label="连接器" value={`${detail.connector_ids?.length ?? 0} 个`} mono />
              <KV label="模型" value={detail.model || "跟随中台默认"} />
              <KV label="版本" value={`v${detail.version ?? 1}`} mono />
            </dl>
          </Section>

          {detail.soul ? (
            <Section title="人设原文（启用后可以改）">
              <pre className="max-h-[220px] overflow-auto rounded-lg border border-line bg-rail p-2.5 font-mono text-label whitespace-pre-wrap text-ink-2">
                {detail.soul}
              </pre>
            </Section>
          ) : null}
        </div>
      )}
    </Drawer>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-label font-medium tracking-wide text-muted uppercase">
        {title}
      </h3>
      {children}
    </section>
  );
}

function KV({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-3 py-2">
      <dt className="text-secondary text-muted">{label}</dt>
      <dd className={cn("text-body text-ink", mono && "font-mono tabular-nums")}>{value}</dd>
    </div>
  );
}
