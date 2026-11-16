/**
 * 右侧详情栏：一个机器人的"档案"。
 *
 * 六块内容，顺序是从"它是谁"到"它对外发生了什么"：
 * 身份 → 能力 → 收件箱 → 参与的群聊 → 例行任务 → 广场投递状态。
 *
 * 这栏在 1280px 以下会折起来（DESIGN.md 的断点），因为三栏挤在 1024 宽里
 * 主体对话会窄到没法读。折起来不是"隐藏功能"：所有操作在顶栏和弹窗里都有入口。
 */
import { Avatar } from "@/components/ui/Avatar";
import { Badge, type BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { Icon } from "./primitives";
import type {
  BotChatPayload,
  BotSummary,
  ConnectorItem,
  InboxRow,
  PlazaItem,
  Routine,
  RoomSummary,
  SkillItem,
} from "./types";

const SEND_STATUS: Record<string, { text: string; tone: BadgeTone }> = {
  queued: { text: "排队中", tone: "warn" },
  claimed: { text: "对方处理中", tone: "signal" },
  settled: { text: "已答复", tone: "ok" },
  failed: { text: "失败", tone: "danger" },
  refused: { text: "未投出", tone: "danger" },
  ambiguous: { text: "待确认", tone: "warn" },
};

const PLAZA_STATUS: Record<string, { text: string; tone: BadgeTone }> = {
  draft: { text: "草稿", tone: "neutral" },
  pending: { text: "审核中", tone: "warn" },
  published: { text: "已上架", tone: "ok" },
  rejected: { text: "未通过", tone: "danger" },
  offline: { text: "已下架", tone: "neutral" },
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-4 first:mt-0">
      <h3 className="mb-1.5 text-label font-semibold tracking-wide text-muted">{title}</h3>
      {children}
    </section>
  );
}

function Card({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("rounded-xl border border-line bg-paper p-3", className)}>{children}</div>
  );
}

function KV({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5 text-secondary">
      <span className="shrink-0 text-muted">{label}</span>
      <span className="min-w-0 truncate text-right font-medium text-ink">{children}</span>
    </div>
  );
}

function Dot({ tone }: { tone: "ok" | "warn" | "danger" | "none" }) {
  return (
    <span
      className={cn(
        "mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full",
        tone === "ok" && "bg-ok",
        tone === "warn" && "bg-warn",
        tone === "danger" && "bg-danger",
        tone === "none" && "bg-line-strong",
      )}
      aria-hidden="true"
    />
  );
}

export function BotSidebar({
  detail,
  messageCount,
  epoch,
  bots,
  skills,
  connectors,
  routines,
  routinesLoading,
  plazaItem,
  plazaEnabled,
  onPublish,
  onOpenRoom,
  onNewRoutine,
}: {
  detail: BotChatPayload | null;
  messageCount: number;
  epoch: string;
  bots: BotSummary[];
  skills: SkillItem[];
  connectors: ConnectorItem[];
  routines: Routine[];
  routinesLoading: boolean;
  plazaItem: PlazaItem | undefined;
  plazaEnabled: boolean | null;
  onPublish: (bot: BotSummary) => void;
  onOpenRoom: (roomId: string) => void;
  onNewRoutine: (bot: BotSummary) => void;
}) {
  const bot = detail?.bot;
  if (!bot) {
    return (
      <aside className="hidden w-[296px] shrink-0 border-l border-line bg-rail px-4 py-4 xl:block">
        <p className="text-label leading-relaxed text-muted">
          选一个机器人后，这里显示它的身份、能力、收件箱和对外投递状态。
        </p>
      </aside>
    );
  }

  const skillTags = (bot.skill_ids ?? []).map((id) => {
    const hit = skills.find((item) => item.id === id);
    return hit?.name ?? id;
  });
  const connectorTags = (bot.connector_ids ?? []).map((id) => {
    const hit = connectors.find((item) => item.id === id);
    return hit?.name ?? hit?.id ?? id;
  });

  const inbox: InboxRow[] = detail.inbox ?? [];
  const rooms: RoomSummary[] = detail.rooms ?? [];
  const sessionId = detail.session_id ?? detail.chat_session_id ?? "";
  const plazaStatus = plazaItem?.status ? PLAZA_STATUS[plazaItem.status] : undefined;

  return (
    <aside className="hidden w-[296px] shrink-0 overflow-y-auto overscroll-contain border-l border-line bg-rail px-4 py-4 xl:block">
      <Section title="身份">
        <Card>
          <div className="flex items-center gap-2">
            <Avatar
              size={28}
              icon={bot.icon}
              color={bot.color}
              shape={bot.shape}
              image={bot.image}
            />
            <div className="min-w-0">
              <div className="truncate text-body font-semibold text-ink">{bot.name}</div>
              <div className="truncate text-label text-muted">@{bot.handle ?? ""}</div>
            </div>
          </div>
          <p className="mt-2 text-secondary leading-relaxed text-muted">
            {bot.description || "还没有写描述；队友花名册里只会看到名字。"}
          </p>
          <div className="mt-2 border-t border-line pt-1.5">
            <KV label="规范会话">
              <span className="font-mono text-label">…{sessionId.slice(-14) || "—"}</span>
            </KV>
            <KV label="消息数">{messageCount}</KV>
            <KV label="能力纪元">
              <span className="font-mono text-label">{epoch || "—"}</span>
            </KV>
            <KV label="模型">{bot.model || "跟随中台默认"}</KV>
          </div>
        </Card>
      </Section>

      <Section title="能力">
        <Card>
          <div className="text-label text-muted">技能</div>
          <div className="mt-1 flex flex-wrap gap-1">
            {skillTags.length ? (
              skillTags.map((name) => (
                <span
                  key={name}
                  className="rounded-sm bg-sunken px-1.5 py-0.5 text-label text-ink-2"
                >
                  {name}
                </span>
              ))
            ) : (
              <span className="text-label text-muted">没有预设技能</span>
            )}
          </div>
          <div className="mt-2 text-label text-muted">连接器</div>
          <div className="mt-1 flex flex-wrap gap-1">
            {connectorTags.length ? (
              connectorTags.map((name) => (
                <span
                  key={name}
                  className="rounded-sm bg-sunken px-1.5 py-0.5 text-label text-ink-2"
                >
                  {name}
                </span>
              ))
            ) : (
              <span className="text-label text-muted">没有预设连接器</span>
            )}
          </div>
        </Card>
      </Section>

      <Section title="收件箱（别人发给它）">
        <Card>
          {inbox.length ? (
            <div className="flex flex-col gap-2">
              {inbox.slice(0, 8).map((row, index) => {
                const status = String(row.status ?? "");
                const preset = SEND_STATUS[status];
                const fromName = row.from_bot
                  ? (bots.find((item) => item.bot_id === row.from_bot)?.name ??
                    String(row.from_bot))
                  : "系统投递";
                const body = String(row.body ?? "").replace(/\s+/g, " ").slice(0, 60);
                return (
                  <div key={index} className="flex gap-2">
                    <Dot
                      tone={
                        status === "failed" || status === "refused"
                          ? "danger"
                          : status === "settled"
                            ? "ok"
                            : "warn"
                      }
                    />
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-1 text-label">
                        <span className="text-ink-2">来自 {fromName}</span>
                        <Badge tone={preset?.tone ?? "neutral"}>
                          {preset?.text ?? status}
                        </Badge>
                      </div>
                      <div className="truncate text-label text-muted">
                        {body}
                        {row.reason ? ` · ${String(row.reason)}` : ""}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-label text-muted">还没有投递记录</p>
          )}
        </Card>
      </Section>

      <Section title="它参与的群聊">
        <Card>
          {rooms.length ? (
            <div className="flex flex-col gap-1">
              {rooms.map((room) => (
                <div
                  key={room.room_id}
                  className="flex items-center justify-between gap-2 text-secondary"
                >
                  <span className="min-w-0 truncate">
                    {room.name}
                    {room.status === "open" ? "" : "（已结束）"}
                  </span>
                  <Button size="sm" onClick={() => onOpenRoom(room.room_id)}>
                    打开
                  </Button>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-label text-muted">还没有群聊</p>
          )}
        </Card>
      </Section>

      <Section title="例行任务">
        <Card>
          {routinesLoading ? (
            <div className="space-y-1.5">
              <Skeleton className="h-3 w-2/3" />
              <Skeleton className="h-3 w-1/2" />
            </div>
          ) : routines.length ? (
            <div className="flex flex-col gap-2">
              {routines.map((routine, index) => (
                <div key={routine.schedule_id ?? index} className="flex gap-2">
                  <Dot tone={routine.enabled ? "ok" : "none"} />
                  <div className="min-w-0">
                    <div className="truncate text-label font-medium text-ink">
                      {routine.name ?? "未命名"}
                    </div>
                    <div className="truncate text-label text-muted">
                      {routine.enabled
                        ? routine.type === "once"
                          ? "一次性"
                          : "周期"
                        : "已停用"}
                      {routine.last_status ? ` · ${routine.last_status}` : ""}
                    </div>
                  </div>
                </div>
              ))}
              <Button size="sm" className="mt-1" onClick={() => onNewRoutine(bot)}>
                <Icon name="plus" size={11} />
                加一条
              </Button>
            </div>
          ) : (
            <div>
              <p className="text-label leading-relaxed text-muted">还没有例行任务</p>
              <Button size="sm" className="mt-1.5" onClick={() => onNewRoutine(bot)}>
                <Icon name="plus" size={11} />
                加一条
              </Button>
            </div>
          )}
        </Card>
      </Section>

      <Section title="Bot 广场">
        <Card>
          {plazaEnabled === false ? (
            <p className="text-label text-muted">Bot 广场未开放（BOT_PLAZA_ENABLED=false）</p>
          ) : plazaStatus ? (
            <div>
              <KV label="状态">
                <Badge tone={plazaStatus.tone}>{plazaStatus.text}</Badge>
              </KV>
              <KV label="版本">v{plazaItem?.version ?? 1}</KV>
              <KV label="启用人数">{plazaItem?.install_count ?? 0}</KV>
              <KV label="可见范围">{plazaItem?.scope === "public" ? "全平台" : "本企业"}</KV>
              {plazaItem?.review_note ? (
                <p className="mt-1.5 text-label leading-relaxed text-warn">
                  审核意见：{plazaItem.review_note}
                </p>
              ) : null}
              <div className="mt-2 flex flex-wrap gap-1.5">
                <Button size="sm" onClick={() => onPublish(bot)}>
                  {plazaItem?.status === "published" ? "更新内容" : "重新提交"}
                </Button>
                <a
                  href="/app/plaza?mine=1"
                  target="_blank"
                  rel="noopener"
                  className="inline-flex h-[26px] items-center rounded-md border border-line-strong bg-paper px-2 text-secondary font-medium text-ink-2 transition-colors duration-150 ease-standard hover:border-muted/45 hover:text-ink"
                >
                  去广场看
                </a>
              </div>
            </div>
          ) : plazaEnabled === null ? (
            <Skeleton className="h-3 w-2/3" />
          ) : (
            <div>
              <p className="text-label leading-relaxed text-muted">
                把它发布到广场：别人点一下就得到一份同样的副本，你的人设与这个会话都不会被改。
              </p>
              <Button size="sm" className="mt-2" onClick={() => onPublish(bot)}>
                <Icon name="share" size={11} />
                发布到广场
              </Button>
            </div>
          )}
        </Card>
      </Section>
    </aside>
  );
}
