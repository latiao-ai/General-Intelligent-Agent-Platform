/**
 * 左侧轨道：品牌、新建、四个视图入口、搜索、花名册、群聊、出口。
 *
 * 花名册的分组逻辑原样搬自旧页，有三条不显然的规则：
 *  1. 空分区也要画——分区是产品概念，没人不代表它不存在（可以往里放）。
 *  2. 但搜索时跳过空分区——搜"review"时列一堆空标题没有意义。
 *  3. 没有分区时，"全部机器人"这个组头不画；一旦有分区，未分区的那组
 *     要显示成"全部机器人"作为兜底。
 */
import { useMemo, useState } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { Icon, TextInput } from "./primitives";
import type { BotSection, BotSummary, RoomSummary } from "./types";

export type RailView = "chat" | "rooms" | "routines";

const NAV: Array<{ view: RailView; label: string; icon: string }> = [
  { view: "chat", label: "机器人", icon: "robot" },
  { view: "rooms", label: "群聊", icon: "bubble" },
  { view: "routines", label: "例行任务", icon: "clock" },
];

function roomStatusText(status?: string) {
  if (status === "open") return "讨论中";
  if (status === "settled") return "已收束";
  return "已关闭";
}

export function BotRail({
  bots,
  sections,
  rooms,
  enabled,
  loading,
  view,
  activeBotId,
  activeRoomId,
  routineCount,
  onSelectBot,
  onSelectRoom,
  onSelectView,
  onNewBot,
  onNewRoom,
  onRefresh,
  onDeleteSection,
}: {
  bots: BotSummary[];
  sections: BotSection[];
  rooms: RoomSummary[];
  enabled: boolean;
  loading: boolean;
  view: RailView;
  activeBotId: string;
  activeRoomId: string;
  routineCount: number | string;
  onSelectBot: (id: string) => void;
  onSelectRoom: (id: string) => void;
  onSelectView: (view: RailView) => void;
  onNewBot: () => void;
  onNewRoom: () => void;
  onRefresh: () => void;
  onDeleteSection: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const match = (bot: BotSummary) =>
      !needle ||
      `${bot.name} ${bot.handle ?? ""} ${bot.title ?? ""}`.toLowerCase().includes(needle);

    const buckets = new Map<string, BotSummary[]>();
    for (const bot of bots) {
      if (!match(bot)) continue;
      const key = bot.section_id ?? "";
      const list = buckets.get(key);
      if (list) list.push(bot);
      else buckets.set(key, [bot]);
    }

    const order = ["", ...sections.map((section) => section.section_id)];
    const names = new Map(sections.map((section) => [section.section_id, section.name]));

    return order
      .map((key) => {
        const list = (buckets.get(key) ?? []).slice().sort(
          (a, b) => Number(b.pinned ?? 0) - Number(a.pinned ?? 0),
        );
        return {
          key,
          // 有分区时这组装的是"没分区的"，叫"全部机器人"会误导（它只装一两个）
          title: key ? (names.get(key) ?? "分区") : sections.length ? "未分区" : "全部机器人",
          bots: list,
        };
      })
      .filter((group) => {
        // 搜索时不留空组；未分区组在没有分区时也不画组头
        if (needle) return group.bots.length > 0;
        if (!group.key && group.bots.length === 0 && sections.length > 0) return false;
        return true;
      });
  }, [bots, sections, query]);

  const counts: Record<RailView, number> = {
    chat: bots.length,
    rooms: rooms.length,
    routines: typeof routineCount === "number" ? routineCount : 0,
  };

  return (
    <aside className="flex w-[236px] shrink-0 flex-col border-r border-line bg-rail">
      {/* 品牌 */}
      <div className="flex items-center gap-2 px-3 pt-3 pb-2">
        <div
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-signal text-paper"
          aria-hidden="true"
        >
          <Icon name="robot" size={15} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-body font-semibold text-ink">大吕 Bot</div>
          <div className="truncate text-label text-muted">机器人工作台 · Bot Mode</div>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          title="刷新花名册"
          aria-label="刷新花名册"
          className="flex h-6 w-6 items-center justify-center rounded-md text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
        >
          <Icon name="refresh" size={13} />
        </button>
      </div>

      <div className="px-3 pb-2">
        <Button
          variant="primary"
          className="w-full"
          onClick={onNewBot}
          disabled={!enabled}
          title={enabled ? undefined : "机器人能力已关闭（BOT_MODE_ENABLED=false）"}
        >
          <Icon name="plus" size={13} />
          新建机器人
        </Button>
      </div>

      {/* 视图入口 */}
      <nav aria-label="机器人导航" className="flex flex-col gap-0.5 px-2">
        {NAV.map((item) => {
          const active = view === item.view;
          return (
            <button
              key={item.view}
              type="button"
              onClick={() => onSelectView(item.view)}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex h-8 items-center gap-2 rounded-md border border-transparent px-2 text-left text-body transition-colors duration-150 ease-standard",
                "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
                active
                  ? "bg-signal-wash font-medium text-signal-deep"
                  : "text-ink-2 hover:bg-sunken hover:text-ink",
              )}
            >
              <Icon name={item.icon} size={14} />
              <span className="flex-1 truncate">{item.label}</span>
              <span
                className={cn(
                  "text-label tabular-nums",
                  active ? "text-signal-deep" : "text-muted",
                )}
              >
                {counts[item.view]}
              </span>
            </button>
          );
        })}
        <a
          href="/app/plaza"
          title="Bot 广场：挑现成的机器人，一键装到自己名下"
          className="flex h-8 items-center gap-2 rounded-md border border-transparent px-2 text-body text-ink-2 transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
        >
          <Icon name="grid" size={14} />
          <span className="flex-1 truncate">Bot 广场</span>
          <Icon name="chevron" size={12} className="text-muted" />
        </a>
      </nav>

      <div className="mt-2 px-3 pb-1">
        <TextInput
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索机器人 / handle"
          aria-label="搜索机器人"
          className="h-[28px] text-secondary"
        />
      </div>

      {/* 花名册 */}
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 pb-2">
        {loading ? (
          <div className="space-y-1.5 px-1 pt-2">
            {[0, 1, 2, 3].map((index) => (
              <Skeleton key={index} className="h-9" />
            ))}
          </div>
        ) : (
          <>
            {groups.map((group) => {
              const isCollapsed = Boolean(collapsed[group.key]);
              const showHead = Boolean(group.key) || sections.length > 0;
              return (
                <div key={group.key || "__all__"}>
                  {showHead ? (
                    <div className="flex items-center gap-1 px-1 pt-2.5 pb-1">
                      <button
                        type="button"
                        onClick={() =>
                          setCollapsed((prev) => ({ ...prev, [group.key]: !prev[group.key] }))
                        }
                        className="flex min-w-0 flex-1 items-center gap-1 text-left"
                        aria-expanded={!isCollapsed}
                      >
                        <Icon
                          name="chevron"
                          size={11}
                          className={cn(
                            "text-muted transition-transform duration-150 ease-standard",
                            isCollapsed ? "" : "rotate-90",
                          )}
                        />
                        <span className="truncate text-label font-semibold tracking-wide text-muted">
                          {group.title}
                        </span>
                        <span className="text-label tabular-nums text-muted">
                          {group.bots.length}
                        </span>
                      </button>
                      {group.key ? (
                        <button
                          type="button"
                          onClick={() => onDeleteSection(group.key)}
                          title="删除分区"
                          aria-label={`删除分区 ${group.title}`}
                          className="flex h-5 w-5 items-center justify-center rounded text-muted opacity-0 transition-opacity duration-150 hover:bg-sunken hover:text-danger focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal/25 group-hover/rail:opacity-100 [div:hover>&]:opacity-100"
                        >
                          <Icon name="close" size={10} />
                        </button>
                      ) : null}
                    </div>
                  ) : null}

                  {isCollapsed ? null : group.bots.length === 0 ? (
                    <div className="px-2 py-1 text-label text-muted">还没有机器人</div>
                  ) : (
                    group.bots.map((bot) => {
                      const active = bot.bot_id === activeBotId;
                      return (
                        <button
                          key={bot.bot_id}
                          type="button"
                          onClick={() => onSelectBot(bot.bot_id)}
                          className={cn(
                            "flex w-full items-center gap-2 rounded-md border border-transparent px-1.5 py-1.5 text-left transition-colors duration-150 ease-standard",
                            "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
                            active
                              ? "border-signal/25 bg-signal-wash"
                              : "hover:bg-sunken",
                          )}
                        >
                          <Avatar
                            size={26}
                            icon={bot.icon}
                            color={bot.color}
                            shape={bot.shape}
                            image={bot.image}
                          />
                          <span className="min-w-0 flex-1">
                            <span className="flex items-center gap-1">
                              <span
                                className={cn(
                                  "truncate text-body",
                                  active ? "font-medium text-signal-deep" : "text-ink",
                                )}
                              >
                                {bot.name}
                              </span>
                              {bot.pinned ? (
                                <Icon name="pin" size={10} className="text-muted" />
                              ) : null}
                              {bot.hidden ? <Badge>隐藏</Badge> : null}
                            </span>
                            <span className="block truncate text-label text-muted">
                              {bot.last_preview ||
                                `@${bot.handle ?? ""}${bot.title ? ` · ${bot.title}` : ""}`}
                            </span>
                          </span>
                        </button>
                      );
                    })
                  )}
                </div>
              );
            })}

            {bots.length === 0 && !query.trim() ? (
              <p className="px-2 py-3 text-label leading-relaxed text-muted">
                还没有机器人。点上面的「新建机器人」，给它一个名字和人设，它就有自己的会话、记忆和技能。
              </p>
            ) : null}
            {query.trim() && groups.every((group) => group.bots.length === 0) ? (
              <p className="px-2 py-3 text-label leading-relaxed text-muted">
                没有匹配「{query.trim()}」的机器人。换个关键词，或清空搜索。
              </p>
            ) : null}
          </>
        )}

        {/* 群聊 */}
        <div className="mt-1 flex items-center gap-1 px-1 pt-2.5 pb-1">
          <Icon name="chevron" size={11} className="rotate-90 text-muted" />
          <span className="flex-1 text-label font-semibold tracking-wide text-muted">群聊</span>
          <span className="text-label tabular-nums text-muted">{rooms.length}</span>
          <button
            type="button"
            onClick={onNewRoom}
            title="新建群聊"
            aria-label="新建群聊"
            className="flex h-5 w-5 items-center justify-center rounded text-muted transition-colors duration-150 hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal/25"
          >
            <Icon name="plus" size={10} />
          </button>
        </div>

        {rooms.map((room) => {
          const active = room.room_id === activeRoomId;
          const members = room.members ?? [];
          return (
            <button
              key={room.room_id}
              type="button"
              onClick={() => onSelectRoom(room.room_id)}
              className={cn(
                "flex w-full items-center gap-2 rounded-md border border-transparent px-1.5 py-1.5 text-left transition-colors duration-150 ease-standard",
                "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
                active ? "border-signal/25 bg-signal-wash" : "hover:bg-sunken",
              )}
            >
              <span
                className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-lg bg-signal-wash text-signal-deep"
                aria-hidden="true"
              >
                <Icon name="bubble" size={13} />
              </span>
              <span className="min-w-0 flex-1">
                <span
                  className={cn(
                    "block truncate text-body",
                    active ? "font-medium text-signal-deep" : "text-ink",
                  )}
                >
                  {room.name}
                </span>
                <span className="block truncate text-label text-muted">
                  {roomStatusText(room.status)} · {members.length} 人
                  {members.length
                    ? ` · ${members.map((member) => `@${member.handle ?? ""}`).join(" ")}`
                    : ""}
                </span>
              </span>
            </button>
          );
        })}
      </div>

      {/* 出口 */}
      <div className="flex items-center gap-2 border-t border-line px-3 py-2">
        <a
          href="/api/ui"
          title="回到任务工作台"
          className="text-label text-ink-2 transition-colors duration-150 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal/25"
        >
          任务工作台
        </a>
        <span className="text-line-strong" aria-hidden="true">
          ·
        </span>
        <a
          href="/api/ui/messaging"
          title="消息平台：飞书 / 企业微信 / 个人微信"
          className="text-label text-ink-2 transition-colors duration-150 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal/25"
        >
          消息平台
        </a>
        <span className="flex-1" />
        <Badge tone={enabled ? "ok" : "danger"}>{enabled ? "模式 开" : "模式 关"}</Badge>
      </div>
    </aside>
  );
}
