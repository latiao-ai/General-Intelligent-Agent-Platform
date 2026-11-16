/**
 * 机器人工作台 —— 总装配。
 *
 * 三栏 + 四个弹窗，视图只有三个（对话 / 群聊 / 例行任务）。所有状态都在这里
 * 收口，子组件全是受控的——旧页 50 个全局函数互相改 `S` 的状态，
 * 想追一个按钮点了之后谁改了什么都得全文搜。
 *
 * 深链：`?bot=<id>&say=<开场白>` 从 Bot 广场点「使用」跳过来时带，
 * 找不到就退化成第一个机器人（链接过期不该白屏）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useToast } from "@/components/ui/Toast";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/api";
import {
  archiveBot,
  createBot,
  createRoom,
  createSection,
  deleteSection,
  publishBot,
  updateBot,
  withdrawBot,
} from "./api";
import { BotChatPane } from "./BotChatPane";
import { BotDialog } from "./BotDialog";
import { BotRail, type RailView } from "./BotRail";
import { BotSidebar } from "./BotSidebar";
import { Icon } from "./primitives";
import { PublishDialog } from "./PublishDialog";
import { RoomDialog } from "./RoomDialog";
import { RoomPane } from "./RoomPane";
import { RoutineDialog } from "./RoutineDialog";
import { RoutinesPane } from "./RoutinesPane";
import { useBotChat } from "./useBotChat";
import { useBotSelectors } from "./useBotSelectors";
import { useBotRoutines, useCreateRoutine, useRoutineTasks } from "./useRoutines";
import { usePlaza } from "./usePlaza";
import { useRoom } from "./useRoom";
import { useRoster } from "./useRoster";
import type { BotPayload, BotSummary, RoomDetail } from "./types";

const ROOM_STATUS_TEXT: Record<string, string> = {
  open: "讨论中",
  settled: "已收束",
  closed: "已关闭",
};

export function BotsPage() {
  const toast = useToast();
  const roster = useRoster();
  const selectors = useBotSelectors();
  const plaza = usePlaza();
  const chat = useBotChat();
  const room = useRoom();
  const routineTasks = useRoutineTasks();
  const routineCreator = useCreateRoutine();

  const [view, setView] = useState<RailView>("chat");
  const [activeBotId, setActiveBotId] = useState("");
  const [activeRoomId, setActiveRoomId] = useState("");
  const [chatInputKey, setChatInputKey] = useState(0);

  const [botDialog, setBotDialog] = useState<{ open: boolean; bot: BotSummary | null }>({
    open: false,
    bot: null,
  });
  const [roomDialog, setRoomDialog] = useState(false);
  const [routineDialog, setRoutineDialog] = useState<{ open: boolean; bot: BotSummary | null }>({
    open: false,
    bot: null,
  });
  const [publishDialog, setPublishDialog] = useState<{ open: boolean; bot: BotSummary | null }>({
    open: false,
    bot: null,
  });
  const [saving, setSaving] = useState(false);

  const activeBot = useMemo(
    () => roster.bots.find((bot) => bot.bot_id === activeBotId),
    [roster.bots, activeBotId],
  );
  const sideRoutines = useBotRoutines(activeBotId || "");

  /* ---- 错误统一弹一次：hook 里只存，不直接调 UI ---- */
  useEffect(() => {
    if (!chat.error) return;
    toast(chat.error, "error");
    chat.clearError();
    // clearError 故意不进依赖：它是稳定引用，进依赖会让 effect 重跑
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.error]);

  useEffect(() => {
    if (!room.error) return;
    toast(room.error, "error");
    room.clearError();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room.error]);

  /* ---- 选中 ---- */
  const selectBot = useCallback(
    async (botId: string) => {
      if (!botId) return;
      setView("chat");
      setActiveBotId(botId);
      setActiveRoomId("");
      room.reset();
      setChatInputKey((prev) => prev + 1);
      await chat.open(botId);
    },
    [chat, room],
  );

  const selectRoom = useCallback(
    async (roomId: string) => {
      if (!roomId) return;
      setView("rooms");
      setActiveRoomId(roomId);
      setActiveBotId("");
      chat.reset();
      await room.open(roomId);
    },
    [chat, room],
  );

  const selectView = useCallback(
    (next: RailView) => {
      if (next === "routines") {
        setView("routines");
        setActiveBotId("");
        setActiveRoomId("");
        chat.reset();
        room.reset();
        void routineTasks.reload();
        return;
      }
      if (next === "chat") {
        if (roster.bots.length) {
          void selectBot(activeBotId || roster.bots[0]!.bot_id);
        } else {
          setView("chat");
          setActiveBotId("");
          chat.reset();
        }
        return;
      }
      if (roster.rooms.length) {
        void selectRoom(activeRoomId || roster.rooms[0]!.room_id);
      } else {
        setRoomDialog(true);
        toast("还没有群聊，先建一个", "error");
      }
    },
    [activeBotId, activeRoomId, chat, roster.bots, roster.rooms, room, routineTasks, selectBot, selectRoom, toast],
  );

  /* ---- 深链：只跑一次，等花名册到位 ---- */
  const deepLinkDone = useRef(false);
  useEffect(() => {
    if (deepLinkDone.current || roster.loading) return;
    deepLinkDone.current = true;
    const params = new URLSearchParams(window.location.search);
    const wantBot = params.get("bot") ?? "";
    const say = params.get("say") ?? "";
    const picked = roster.bots.find((bot) => bot.bot_id === wantBot);
    if (picked) {
      void selectBot(picked.bot_id);
    } else if (roster.bots.length) {
      void selectBot(roster.bots[0]!.bot_id);
    }
    if (say) {
      // 开场白只填不自动发送：用户还有机会改一个字，或者干脆不发
      window.setTimeout(() => {
        const area = document.querySelector<HTMLTextAreaElement>(
          'textarea[aria-label^="和这个机器人"]',
        );
        if (!area) return;
        const setter = Object.getOwnPropertyDescriptor(
          HTMLTextAreaElement.prototype,
          "value",
        )?.set;
        setter?.call(area, say);
        area.dispatchEvent(new Event("input", { bubbles: true }));
        area.focus();
      }, 60);
    }
  }, [roster.loading, roster.bots, selectBot]);

  /* ---- 机器人增删改 ---- */
  const saveBot = async (payload: BotPayload): Promise<boolean> => {
    setSaving(true);
    try {
      if (botDialog.bot) {
        await updateBot(botDialog.bot.bot_id, payload);
        toast("已保存");
        setBotDialog({ open: false, bot: null });
        await roster.refresh();
        if (activeBotId) await chat.open(activeBotId);
      } else {
        const created = await createBot(payload);
        toast("机器人已创建");
        setBotDialog({ open: false, bot: null });
        await roster.refresh();
        if (created.bot_id) await selectBot(created.bot_id);
      }
      return true;
    } catch (err) {
      toast(errorMessage(err), "error");
      return false;
    } finally {
      setSaving(false);
    }
  };

  const removeBot = async (bot: BotSummary) => {
    setSaving(true);
    try {
      await archiveBot(bot.bot_id);
      setBotDialog({ open: false, bot: null });
      await roster.refresh();
      if (activeBotId === bot.bot_id) {
        setActiveBotId("");
        chat.reset();
      }
      toast("已归档");
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      setSaving(false);
    }
  };

  const patchActiveBot = async (fields: Record<string, unknown>) => {
    if (!activeBotId) return;
    try {
      await updateBot(activeBotId, fields);
      await roster.refresh();
      await chat.open(activeBotId);
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const addSection = async (name: string) => {
    try {
      const created = await createSection(name, roster.sections.length);
      await roster.refresh();
      toast("分区已创建");
      return created;
    } catch (err) {
      toast(errorMessage(err), "error");
      return null;
    }
  };

  const removeSection = async (sectionId: string) => {
    if (!window.confirm("删除这个分区？里面的机器人会回到「未分区」，对话不会丢。")) return;
    try {
      await deleteSection(sectionId);
      await roster.refresh();
      toast("分区已删除");
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  /* ---- 群聊 ---- */
  const saveRoom = async (body: {
    name: string;
    topic: string;
    members: string[];
    max_rounds: number;
    max_messages: number;
  }): Promise<boolean> => {
    setSaving(true);
    try {
      const created = await createRoom(body);
      setRoomDialog(false);
      await roster.refresh();
      if (created.room_id) {
        await selectRoom(created.room_id);
        toast("群聊已创建；点「推进一拍」或等 Worker 自动推进");
        window.setTimeout(() => {
          void room.step().catch(() => {
            /* 失败已经通过 room.error 弹过 */
          });
        }, 400);
      }
      return true;
    } catch (err) {
      toast(errorMessage(err), "error");
      return false;
    } finally {
      setSaving(false);
    }
  };

  const stepRoom = async () => {
    try {
      const res = await room.step();
      if (!res) return;
      if (res.action === "speak") toast(`已派发：${res.bot_name ?? res.bot_id ?? "成员"} 发言`);
      else if (res.action === "settle") toast(`房间已收束（${res.reason ?? ""}）`);
      else if (res.action === "waiting") toast("上一个成员还没说完，稍等");
      else if (res.action === "lease_held") toast("另一个调度者正在推进，稍等");
    } catch {
      /* room.error 已弹 */
    }
  };

  const closeRoom = async () => {
    if (!window.confirm("结束这个群聊？之后不能再发言，但记录保留。")) return;
    try {
      await room.close();
      await roster.refresh();
      toast("房间已结束");
    } catch {
      /* room.error 已弹 */
    }
  };

  /* ---- 例行任务 ---- */
  const saveRoutine = async (body: {
    name: string;
    prompt: string;
    rrule: string;
  }): Promise<boolean> => {
    const bot = routineDialog.bot;
    if (!bot) return false;
    try {
      await routineCreator.submit(bot.bot_id, body.name, body.prompt, body.rrule);
      setRoutineDialog({ open: false, bot: null });
      toast("例行任务已创建");
      if (activeBotId === bot.bot_id) {
        // 侧栏那块要跟着变，最简单是重开一次会话
        await chat.open(bot.bot_id);
      }
      return true;
    } catch (err) {
      toast(errorMessage(err), "error");
      return false;
    }
  };

  /* ---- 广场 ---- */
  const submitPublish = async (body: {
    category: string;
    tags: string[];
    readme: string;
    starters: string[];
  }): Promise<boolean> => {
    const bot = publishDialog.bot;
    if (!bot) return false;
    setSaving(true);
    try {
      const res = await publishBot(bot.bot_id, body);
      setPublishDialog({ open: false, bot: null });
      await plaza.reload();
      toast(res.status === "pending" ? "已提交，等管理员审核后出现在广场" : "已提交");
      return true;
    } catch (err) {
      toast(errorMessage(err), "error");
      return false;
    } finally {
      setSaving(false);
    }
  };

  const withdrawPublish = async () => {
    const bot = publishDialog.bot;
    if (!bot) return;
    setSaving(true);
    try {
      await withdrawBot(bot.bot_id);
      setPublishDialog({ open: false, bot: null });
      await plaza.reload();
      toast("已撤回");
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      setSaving(false);
    }
  };

  const openNewBot = () => {
    if (!roster.enabled) {
      toast("机器人能力已关闭", "error");
      return;
    }
    setBotDialog({ open: true, bot: null });
  };

  const openRoomCreator = () => {
    if (!roster.enabled) {
      toast("机器人能力已关闭", "error");
      return;
    }
    if (roster.bots.filter((bot) => bot.status !== "archived").length < 2) {
      toast("至少要有 2 个机器人才能建群聊", "error");
      return;
    }
    setRoomDialog(true);
  };

  /* ---- 顶栏 ---- */
  const header = renderHeader({
    view,
    activeBot,
    chatDetail: chat.detail,
    epoch: chat.epoch,
    roomDetail: room.detail,
    roomStatus: room.status,
    routineCount: routineTasks.tasks.length,
    onPin: () => void patchActiveBot({ pinned: !activeBot?.pinned }),
    onHide: () => void patchActiveBot({ hidden: !activeBot?.hidden }),
    onEdit: () => activeBot && setBotDialog({ open: true, bot: activeBot }),
    onRoutine: () => activeBot && setRoutineDialog({ open: true, bot: activeBot }),
    onStep: () => void stepRoom(),
    onCloseRoom: () => void closeRoom(),
    onRefreshRoom: () => void room.reload(),
    onOpenMember: (botId) => void selectBot(botId),
  });

  return (
    <div className="flex h-screen overflow-hidden bg-paper">
      <BotRail
        bots={roster.bots}
        sections={roster.sections}
        rooms={roster.rooms}
        enabled={roster.enabled}
        loading={roster.loading}
        view={view}
        activeBotId={activeBotId}
        activeRoomId={activeRoomId}
        routineCount={routineTasks.tasks.length}
        onSelectBot={(id) => void selectBot(id)}
        onSelectRoom={(id) => void selectRoom(id)}
        onSelectView={selectView}
        onNewBot={openNewBot}
        onNewRoom={openRoomCreator}
        onRefresh={() => {
          void roster.refresh().then(
            () => toast("已刷新"),
            () => {
              /* roster.error 会在下方以错误态显示 */
            },
          );
        }}
        onDeleteSection={(id) => void removeSection(id)}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex min-h-[60px] shrink-0 items-center gap-2.5 border-b border-line px-4">
          {header}
        </header>

        <div className="flex min-h-0 flex-1">
          <section className="flex min-w-0 flex-1 flex-col">
            {roster.error && !roster.bots.length ? (
              <div className="flex flex-1 items-center justify-center px-6">
                <div className="max-w-[380px] text-center">
                  <p className="text-body font-medium text-ink">花名册读取失败</p>
                  <p className="mt-1 text-secondary text-muted">{roster.error}</p>
                  <Button
                    variant="primary"
                    className="mt-3"
                    onClick={() => void roster.refresh().catch(() => {})}
                  >
                    重试
                  </Button>
                </div>
              </div>
            ) : view === "chat" ? (
              <BotChatPane
                key={`${activeBotId}:${chatInputKey}`}
                bot={activeBot}
                messages={chat.messages}
                draft={chat.draft}
                phase={activeBotId ? chat.phase : "idle"}
                error={chat.error}
                streaming={chat.streaming}
                busy={chat.busy}
                onSend={chat.send}
                onRetry={() => void chat.open(activeBotId)}
                onNewBot={openNewBot}
                onNewRoom={openRoomCreator}
              />
            ) : view === "rooms" ? (
              <RoomPane
                detail={room.detail}
                phase={activeRoomId ? room.phase : "idle"}
                error={room.error}
                isOpen={room.isOpen}
                onSay={room.say}
                onRetry={() => void room.open(activeRoomId)}
              />
            ) : (
              <RoutinesPane
                tasks={routineTasks.tasks}
                phase={routineTasks.phase}
                error={routineTasks.error}
                bots={roster.bots}
                onOpenBot={(botId) => void selectBot(botId)}
                onNewRoutine={(bot) => setRoutineDialog({ open: true, bot })}
                onRetry={() => void routineTasks.reload()}
              />
            )}
          </section>

          {view === "chat" ? (
            <BotSidebar
              detail={chat.detail}
              messageCount={chat.messages.length}
              epoch={chat.epoch}
              bots={roster.bots}
              skills={selectors.skills}
              connectors={selectors.connectors}
              routines={sideRoutines.items}
              routinesLoading={sideRoutines.loading}
              plazaItem={activeBot ? plaza.byBot[activeBot.bot_id] : undefined}
              plazaEnabled={plaza.enabled}
              onPublish={(bot) => setPublishDialog({ open: true, bot })}
              onOpenRoom={(roomId) => void selectRoom(roomId)}
              onNewRoutine={(bot) => setRoutineDialog({ open: true, bot })}
            />
          ) : view === "rooms" ? (
            <RoomSide detail={room.detail} onOpenBot={(botId) => void selectBot(botId)} />
          ) : (
            <RoutineSide />
          )}
        </div>
      </main>

      <BotDialog
        open={botDialog.open}
        bot={botDialog.bot}
        sections={roster.sections}
        models={selectors.models}
        skills={selectors.skills}
        connectors={selectors.connectors}
        saving={saving}
        onClose={() => setBotDialog({ open: false, bot: null })}
        onSubmit={saveBot}
        onDelete={removeBot}
        onNewSection={addSection}
      />
      <RoomDialog
        open={roomDialog}
        bots={roster.bots}
        saving={saving}
        onClose={() => setRoomDialog(false)}
        onSubmit={saveRoom}
      />
      <RoutineDialog
        open={routineDialog.open}
        bot={routineDialog.bot}
        saving={routineCreator.saving}
        onClose={() => setRoutineDialog({ open: false, bot: null })}
        onSubmit={saveRoutine}
      />
      <PublishDialog
        open={publishDialog.open}
        bot={publishDialog.bot}
        item={publishDialog.bot ? plaza.byBot[publishDialog.bot.bot_id] : undefined}
        categories={plaza.categories}
        saving={saving}
        onClose={() => setPublishDialog({ open: false, bot: null })}
        onSubmit={submitPublish}
        onWithdraw={withdrawPublish}
      />
    </div>
  );
}

/* ==========================================================================
 * 顶栏
 * ======================================================================== */

function renderHeader({
  view,
  activeBot,
  chatDetail,
  epoch,
  roomDetail,
  roomStatus,
  routineCount,
  onPin,
  onHide,
  onEdit,
  onRoutine,
  onStep,
  onCloseRoom,
  onRefreshRoom,
  onOpenMember,
}: {
  view: RailView;
  activeBot: BotSummary | undefined;
  chatDetail: { session_id?: string; chat_session_id?: string } | null;
  epoch: string;
  roomDetail: RoomDetail | null;
  roomStatus: string;
  routineCount: number;
  onPin: () => void;
  onHide: () => void;
  onEdit: () => void;
  onRoutine: () => void;
  onStep: () => void;
  onCloseRoom: () => void;
  onRefreshRoom: () => void;
  onOpenMember: (botId: string) => void;
}) {
  if (view === "routines") {
    return (
      <>
        <span className="flex h-[30px] w-[30px] items-center justify-center rounded-lg bg-rail text-muted">
          <Icon name="clock" size={15} />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="text-title font-semibold text-ink">例行任务</h1>
          <p className="truncate text-label text-muted">
            结果投递回归属机器人的规范会话；它读到结果后自己决定要不要跟进
            {routineCount ? ` · 共 ${routineCount} 条` : ""}
          </p>
        </div>
        <a
          href="/api/ui"
          className="inline-flex h-[26px] items-center rounded-md border border-line-strong bg-paper px-2 text-secondary font-medium text-ink-2 transition-colors duration-150 ease-standard hover:border-muted/45 hover:text-ink"
        >
          去定时任务页
        </a>
      </>
    );
  }

  if (view === "rooms") {
    const room = roomDetail?.room;
    const members = roomDetail?.members ?? [];
    const open = roomStatus === "open";
    return (
      <>
        <span className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-lg bg-signal-wash text-signal-deep">
          <Icon name="bubble" size={15} />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="flex items-center gap-1.5 text-title font-semibold text-ink">
            <span className="truncate">{room?.name ?? "群聊"}</span>
            {room ? (
              <Badge tone={open ? "ok" : "neutral"}>
                {ROOM_STATUS_TEXT[roomStatus] ?? roomStatus}
              </Badge>
            ) : null}
          </h1>
          <p className="truncate text-label text-muted">
            {room
              ? `${room.topic || "没有写议题"} · 上限 ${room.max_rounds ?? "—"} 轮 / ${
                  room.max_messages ?? "—"
                } 条`
              : "左侧选一个群聊"}
          </p>
        </div>
        {members.length ? (
          <div className="mr-1 flex items-center -space-x-1">
            {members.slice(0, 6).map((member) => (
              <button
                key={member.bot_id}
                type="button"
                title={member.name}
                aria-label={`打开 ${member.name ?? member.bot_id}`}
                onClick={() => onOpenMember(member.bot_id)}
                className="rounded-full ring-2 ring-paper transition-transform duration-150 ease-standard hover:-translate-y-0.5 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/40"
              >
                <Avatar size={24} icon={member.icon} />
              </button>
            ))}
          </div>
        ) : null}
        <Button size="sm" onClick={onStep} disabled={!open}>
          推进一拍
        </Button>
        <Button size="sm" onClick={onCloseRoom} disabled={!open}>
          结束
        </Button>
        <Button size="sm" variant="ghost" onClick={onRefreshRoom} aria-label="刷新房间">
          <Icon name="refresh" size={12} />
        </Button>
      </>
    );
  }

  if (!activeBot) {
    return (
      <>
        <span className="flex h-[30px] w-[30px] items-center justify-center rounded-lg bg-rail text-muted">
          <Icon name="robot" size={15} />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="text-title font-semibold text-ink">机器人</h1>
          <p className="truncate text-label text-muted">从花名册里选一个机器人开始对话</p>
        </div>
      </>
    );
  }

  const sessionId = chatDetail?.session_id ?? chatDetail?.chat_session_id ?? "";

  return (
    <>
      <Avatar
        size={30}
        icon={activeBot.icon}
        color={activeBot.color}
        shape={activeBot.shape}
        image={activeBot.image}
      />
      <div className="min-w-0 flex-1">
        <h1 className="flex items-center gap-1.5 text-title font-semibold text-ink">
          <span className="truncate">{activeBot.name}</span>
          <Badge tone="signal">@{activeBot.handle ?? ""}</Badge>
          {activeBot.status === "archived" ? <Badge tone="warn">已归档</Badge> : null}
          {activeBot.hidden ? <Badge>已隐藏</Badge> : null}
        </h1>
        <p className="truncate text-label text-muted">
          {activeBot.title || activeBot.description || "这个机器人还没有写职责"}
          {epoch ? ` · 能力纪元 ${epoch}` : ""}
          {sessionId ? ` · 会话 …${sessionId.slice(-8)}` : ""}
        </p>
      </div>
      <Button size="sm" onClick={onPin}>
        {activeBot.pinned ? "取消置顶" : "置顶"}
      </Button>
      <Button size="sm" onClick={onHide}>
        {activeBot.hidden ? "取消隐藏" : "隐藏"}
      </Button>
      <Button size="sm" onClick={onEdit}>
        编辑
      </Button>
      <Button size="sm" onClick={onRoutine} title="给它加一条例行任务">
        <Icon name="clock" size={12} />
        例行任务
      </Button>
    </>
  );
}

/* ==========================================================================
 * 群聊 / 例行任务的右侧栏
 * ======================================================================== */

const CARD = "rounded-xl border border-line bg-paper p-3";
const H3 = "mb-1.5 text-label font-semibold tracking-wide text-muted";
const KV_ROW = "flex items-baseline justify-between gap-3 py-0.5 text-secondary";

function RoomSide({
  detail,
  onOpenBot,
}: {
  detail: RoomDetail | null;
  onOpenBot: (botId: string) => void;
}) {
  const room = detail?.room;
  const members = detail?.members ?? [];
  const events = detail?.events ?? [];
  const plan = detail?.plan ?? {};
  const spoken = events.filter((event) => event.kind === "message.member").length;

  let next = "等待下一步";
  if (room && room.status !== "open") {
    next = room.status === "settled" ? "已收束" : "已关闭";
  } else if (plan.action === "speak") {
    const member = members.find((item) => item.bot_id === plan.bot_id);
    next = `${member?.name ?? plan.bot_id ?? "成员"} 发言（第 ${Number(plan.round ?? 0) + 1} 轮 / 第 ${plan.position ?? 1} 位）`;
  } else if (plan.action === "settle") {
    next = `收束（${plan.reason ?? ""}）`;
  }

  return (
    <aside className="hidden w-[296px] shrink-0 overflow-y-auto overscroll-contain border-l border-line bg-rail px-4 py-4 xl:block">
      <h3 className={H3}>房间</h3>
      <div className={CARD}>
        <div className="text-body font-semibold text-ink">{room?.name ?? "—"}</div>
        <p className="mt-0.5 text-label leading-relaxed text-muted">
          {room?.topic || "没有写议题"}
        </p>
        <div className="mt-2 border-t border-line pt-1.5">
          <div className={KV_ROW}>
            <span className="text-muted">状态</span>
            <b>{room ? (ROOM_STATUS_TEXT[room.status ?? ""] ?? room.status) : "—"}</b>
          </div>
          <div className={KV_ROW}>
            <span className="text-muted">发言数 / 上限</span>
            <b className="tabular-nums">
              {spoken} / {room?.max_messages ?? "—"}
            </b>
          </div>
          <div className={KV_ROW}>
            <span className="text-muted">轮次上限</span>
            <b>{room?.max_rounds ?? "—"}</b>
          </div>
          <div className={cn(KV_ROW, "items-start")}>
            <span className="shrink-0 text-muted">下一步</span>
            <b className="text-right">{next}</b>
          </div>
          <div className={KV_ROW}>
            <span className="text-muted">调度者</span>
            <b>Worker（关页面也推进）</b>
          </div>
        </div>
      </div>

      <h3 className={cn(H3, "mt-4")}>成员</h3>
      <div className={CARD}>
        {members.length ? (
          <div className="flex flex-col gap-1.5">
            {members.map((member) => (
              <div key={member.bot_id} className="flex items-center gap-2">
                <Avatar size={22} icon={member.icon} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-secondary font-medium text-ink">
                    {member.name}
                  </div>
                  <div className="truncate text-label text-muted">@{member.handle ?? ""}</div>
                </div>
                <Button size="sm" onClick={() => onOpenBot(member.bot_id)}>
                  打开
                </Button>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-label text-muted">还没有成员</p>
        )}
      </div>

      <h3 className={cn(H3, "mt-4")}>房间规则</h3>
      <div className={CARD}>
        <p className="text-label leading-[1.8] text-muted">
          每个成员只发一轮言；没有新信息回「（无补充）」。
          <br />
          全员无补充、轮次或消息用尽即自动收束；用户每说一次重新开一轮。
        </p>
      </div>
    </aside>
  );
}

function RoutineSide() {
  return (
    <aside className="hidden w-[296px] shrink-0 overflow-y-auto overscroll-contain border-l border-line bg-rail px-4 py-4 xl:block">
      <h3 className={H3}>例行任务怎么跑</h3>
      <div className={CARD}>
        <ol className="flex list-none flex-col gap-1.5 text-label leading-[1.8] text-muted">
          <li>1. 到点后按普通定时任务执行（带归属机器人的人设、技能、模型）。</li>
          <li>2. 成功后结果作为一轮新消息回到它的规范会话，它自己决定要不要跟进。</li>
          <li>3. 失败只留一条消息，不再花一次模型调用。</li>
          <li>4. 完整规则（立即运行 / 运行记录 / 推送）在任务工作台的定时任务页。</li>
        </ol>
      </div>
      <h3 className={cn(H3, "mt-4")}>上限</h3>
      <div className={CARD}>
        <div className={KV_ROW}>
          <span className="text-muted">同一任务同时运行</span>
          <b>1</b>
        </div>
        <div className={KV_ROW}>
          <span className="text-muted">结果回投</span>
          <b>按 occurrence 去重</b>
        </div>
        <div className={KV_ROW}>
          <span className="text-muted">机器人转述深度</span>
          <b>最多 2 跳</b>
        </div>
      </div>
    </aside>
  );
}
