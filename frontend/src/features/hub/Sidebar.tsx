/**
 * 左侧栏：品牌、导航、置顶/任务、空间、账号。
 *
 * 导航有三种落点，视觉上必须一致（都是 30px 行、同样的图标与悬停），
 * 否则用户得先点一下才知道哪个是"换页面"、哪个是"弹窗"：
 *  - link  ：离开本页（大吕 Bot / 消息平台 / 管理控制台）
 *  - view  ：在本页主区域切换（技能·连接器 / 定时任务 / 灵感社区）
 *  - drawer：就地弹抽屉（我的画像 / 记忆与进化）
 *
 * **任务列表与空间分开滚动**（旧页是一个滚动容器）。理由：任务最多翻到 20 条，
 * 而空间通常只有几个；塞在同一个容器里，空间会被任务挤到滚动区之外，"新建空间"
 * 这个入口就变得像不存在。分开后任务区自己滚，空间固定在下方。
 */
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { useToast } from "@/components/ui/Toast";
import { Icon, BrandMark } from "./icons";
import { shortTime } from "./format";
import { GroupHead, SpaceList } from "./SidebarParts";
import type { SpacesState } from "./spaces";
import type { SessionRow } from "./types";

/** 主区域当前显示什么。`chat` 是对话，其余是第二批补上的整页视图。 */
export type HubView = "chat" | "skills" | "schedule" | "inspire";
/** 就地弹出的抽屉。 */
export type HubDrawer = "profile" | "memory" | null;

type NavItem =
  | { label: string; kind: "link"; href: string; icon: string; title: string }
  | { label: string; kind: "view"; view: HubView; icon: string }
  | { label: string; kind: "drawer"; drawer: Exclude<HubDrawer, null>; icon: string };

const NAV: NavItem[] = [
  { label: "大吕 Bot", kind: "link", href: "/app/bots", icon: "bot", title: "多个有独立人设/记忆/技能的 AI 同事" },
  { label: "消息平台", kind: "link", href: "/app/messaging", icon: "message", title: "把飞书 / 企业微信 / 个人微信接到中台" },
  { label: "技能·连接器", kind: "view", view: "skills", icon: "plug" },
  { label: "定时任务", kind: "view", view: "schedule", icon: "clock" },
  { label: "我的画像", kind: "drawer", drawer: "profile", icon: "spark" },
  { label: "记忆与进化", kind: "drawer", drawer: "memory", icon: "archive" },
  { label: "管理控制台", kind: "link", href: "/app/admin", icon: "shield", title: "用户、积分、连接器、技能、日志" },
  { label: "灵感社区", kind: "view", view: "inspire", icon: "grid" },
];

const PAGE_SIZE = 20;

export function Sidebar({
  sessions,
  currentId,
  onOpen,
  onNew,
  me,
  onRequestLogin,
  onLogout,
  view,
  onView,
  onDrawer,
  spaceState,
  searchSignal,
}: {
  sessions: SessionRow[];
  currentId: string;
  onOpen: (id: string) => void;
  onNew: () => void;
  me: { name: string; sub: string; initial: string; authed: boolean } | null;
  onRequestLogin: () => void;
  onLogout: () => void;
  view: HubView;
  onView: (next: HubView) => void;
  onDrawer: (next: HubDrawer) => void;
  /** 置顶、折叠、空间（含服务端命名空间）都由 hook 统一持有 */
  spaceState: SpacesState;
  /** ⌘K：每次自增都把搜索框打开并聚焦（搜索状态住在这里，外面只发信号） */
  searchSignal?: number;
}) {
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const toast = useToast();

  const {
    spaces,
    pins,
    groups,
    activeSpaceId,
    spaceOfSession,
    togglePin,
    toggleGroup,
    toggleSpaceOpen,
    createSpace,
    renameSpace,
    removeSpace,
    enterSpace,
    openFolder,
  } = spaceState;

  useEffect(() => {
    if (searching) searchRef.current?.focus();
  }, [searching]);

  /* ⌘K：只在信号非零时打开，初次挂载不该自动弹搜索 */
  useEffect(() => {
    if (!searchSignal) return;
    setSearching(true);
    setMenuOpen(false);
  }, [searchSignal]);

  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const q = query.trim().toLowerCase();
  const filtered = q ? sessions.filter((s) => String(s.title).toLowerCase().includes(q)) : sessions;

  /* 置顶的排最前且不参与分页——置顶的意义就是"不管翻到哪都在眼前" */
  const pinnedSet = new Set(pins);
  const pinnedSessions = filtered.filter((s) => pinnedSet.has(s.session_id));
  const restSessions = filtered.filter((s) => !pinnedSet.has(s.session_id));
  const restVisible = expanded ? restSessions : restSessions.slice(0, PAGE_SIZE);
  const restHidden = restSessions.length - restVisible.length;
  const moreLabel =
    restHidden > 0
      ? `查看更多（${restHidden}）`
      : expanded && restSessions.length > PAGE_SIZE
        ? "收起"
        : "";

  /**
   * 一行任务。
   *
   * 外层必须是 `div` 而不是 `button`：行内还有个置顶按钮，`button` 里嵌
   * `button` 是非法的，浏览器会把内层甩到外面，点击区域也变得莫名其妙。
   */
  const sessionRow = (s: SessionRow, pinned: boolean) => (
    <div key={s.session_id} className="group relative mb-0.5">
      <button
        type="button"
        onClick={() => onOpen(s.session_id)}
        aria-current={s.session_id === currentId ? "true" : undefined}
        className={cn(
          "block w-full rounded-md py-1.5 pl-2 pr-7 text-left transition-colors duration-150 ease-standard",
          s.session_id === currentId
            ? "bg-signal-wash text-ink"
            : "text-ink-2 hover:bg-sunken",
        )}
      >
        <span className="block truncate text-secondary leading-snug">{s.title}</span>
        <span className="mt-0.5 block truncate text-label text-muted">
          {s.message_count} 条消息 · {shortTime(s.updated_at)}
        </span>
      </button>
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          togglePin(s.session_id);
        }}
        aria-pressed={pinned}
        title={pinned ? "取消置顶" : "置顶"}
        aria-label={pinned ? `取消置顶「${s.title}」` : `置顶「${s.title}」`}
        className={cn(
          "absolute right-1 top-1.5 flex h-[20px] w-[20px] items-center justify-center rounded-[5px] transition-opacity duration-150 ease-standard hover:bg-paper focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
          pinned
            ? "text-signal opacity-100"
            : "text-muted opacity-0 group-hover:opacity-100 hover:text-ink",
        )}
      >
        <Icon name="pin" size={12} />
      </button>
    </div>
  );

  return (
    <aside
      className="flex h-full w-[248px] shrink-0 flex-col border-r border-line bg-rail"
      aria-label="侧边栏"
    >
      {/* 品牌 */}
      <div className="flex items-center gap-2 px-3 pt-3 pb-2">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-signal">
          <BrandMark size={16} />
        </span>
        <span className="min-w-0 flex-1">
          <b className="block truncate text-secondary font-semibold text-ink">集成栈</b>
          <small className="block truncate text-label text-muted">网页研究智能体</small>
        </span>
        <button
          type="button"
          onClick={() => setSearching((v) => !v)}
          title="搜索会话"
          aria-label="搜索会话"
          aria-pressed={searching}
          className={iconBtn}
        >
          <Icon name="search" size={15} />
        </button>
      </div>

      {/* 新建 */}
      <div className="px-3 pb-2">
        <button
          type="button"
          onClick={onNew}
          className="flex h-[34px] w-full items-center gap-2 rounded-md bg-signal px-2.5 text-body font-medium text-paper transition-colors duration-150 ease-standard hover:bg-signal-deep focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/30"
        >
          <Icon name="plus" size={15} />
          新建任务
        </button>
      </div>

      {searching ? (
        <div className="mx-3 mb-2 flex items-center gap-1.5 rounded-md border border-line-strong bg-paper px-2">
          <Icon name="search" size={13} className="text-muted" />
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索会话标题"
            aria-label="搜索会话标题"
            autoComplete="off"
            className="h-[28px] w-full min-w-0 border-0 bg-transparent text-secondary text-ink outline-none placeholder:text-muted"
          />
          <button
            type="button"
            onClick={() => {
              setQuery("");
              setSearching(false);
            }}
            aria-label="关闭搜索"
            className="text-muted hover:text-ink"
          >
            <Icon name="close" size={13} />
          </button>
        </div>
      ) : null}

      {/* 导航 */}
      <nav className="px-2 pb-1" aria-label="主导航">
        {NAV.map((item) => {
          const active = item.kind === "view" && item.view === view;
          const className = cn(
            "flex h-[30px] w-full items-center gap-2 rounded-md px-2 text-left text-secondary transition-colors duration-150 ease-standard",
            active ? "bg-signal-wash text-ink" : "text-ink-2 hover:bg-sunken hover:text-ink",
          );
          const inner = (
            <>
              <Icon
                name={item.icon}
                size={15}
                className={active ? "text-signal" : "text-muted"}
              />
              <span className="truncate">{item.label}</span>
            </>
          );

          if (item.kind === "link") {
            return (
              <a key={item.label} href={item.href} title={item.title} className={className}>
                {inner}
              </a>
            );
          }
          return (
            <button
              key={item.label}
              type="button"
              aria-current={active ? "page" : undefined}
              onClick={() =>
                item.kind === "view" ? onView(item.view) : onDrawer(item.drawer)
              }
              className={className}
            >
              {inner}
            </button>
          );
        })}
      </nav>

      {/* 任务与置顶 */}
      <div className="min-h-0 flex-1 overflow-y-auto pb-1">
        {filtered.length === 0 ? (
          <p className="px-3 py-2 text-label leading-relaxed text-muted">
            {q ? "没有匹配的会话" : "还没有历史会话"}
          </p>
        ) : null}

        {/* 置顶为空时整组不出现：一个永远 0 的分组头只会占地方 */}
        {pinnedSessions.length ? (
          <>
            <GroupHead
              label="置顶"
              count={pinnedSessions.length}
              collapsed={groups.pin}
              onToggle={() => toggleGroup("pin")}
            />
            {groups.pin ? null : (
              <div className="px-2">{pinnedSessions.map((s) => sessionRow(s, true))}</div>
            )}
          </>
        ) : null}

        {restSessions.length ? (
          <>
            <GroupHead
              label="任务"
              count={restSessions.length}
              collapsed={groups.task}
              onToggle={() => toggleGroup("task")}
            />
            {groups.task ? null : (
              <div className="px-2">
                {restVisible.map((s) => sessionRow(s, false))}
                {moreLabel ? (
                  <button
                    type="button"
                    onClick={() => setExpanded((v) => !v)}
                    className="mt-1 w-full rounded-md py-1 text-label text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink"
                  >
                    {moreLabel}
                  </button>
                ) : null}
              </div>
            )}
          </>
        ) : null}
      </div>

      {/* 空间：固定在任务区下方，自己滚 */}
      <div className="max-h-[42vh] shrink-0 overflow-y-auto border-t border-line">
        <SpaceList
          spaces={spaces}
          activeId={activeSpaceId}
          sessions={sessions}
          currentId={currentId}
          collapsed={groups.space}
          spaceOfSession={spaceOfSession}
          onToggleGroup={() => toggleGroup("space")}
          onToggleOpen={toggleSpaceOpen}
          onCreate={async (name) => {
            await createSpace(name);
            toast(`已新建空间「${name}」`);
          }}
          onRename={async (space, name) => {
            await renameSpace(space, name);
            toast(`已重命名为「${name}」`);
          }}
          onRemove={async (space) => {
            await removeSpace(space);
            // 说清楚文件还在：这句文案是"别慌"的依据，不能省
            toast(`已从列表移除「${space.name}」，文件夹和文件未删除`);
          }}
          onOpenFolder={async (space) => {
            await openFolder(space);
            toast("已打开文件夹");
          }}
          onNewTask={async (space) => {
            await enterSpace(space);
            onNew();
            toast(`已切到「${space.name}」，可以新建任务`);
          }}
          onOpenSession={onOpen}
          onError={(message) => toast(message, "error")}
        />
      </div>

      {/* 账号 */}
      <div className="relative border-t border-line p-2" ref={menuRef}>
        <button
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          aria-expanded={menuOpen}
          aria-haspopup="menu"
          className="flex w-full items-center gap-2 rounded-md px-1.5 py-1.5 text-left transition-colors duration-150 ease-standard hover:bg-sunken"
        >
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-signal-wash text-label font-semibold text-signal">
            {me?.initial || "?"}
          </span>
          <span className="min-w-0 flex-1">
            <b className="block truncate text-secondary font-medium text-ink">{me?.name || "未登录"}</b>
            <small className="block truncate text-label text-muted">{me?.sub || "点击查看账号"}</small>
          </span>
          <Icon name="gear" size={14} className="text-muted" />
        </button>

        {menuOpen ? (
          <div
            role="menu"
            className="absolute bottom-[calc(100%-4px)] left-2 z-30 w-[220px] rounded-lg border border-line bg-paper p-1 shadow-[var(--shadow-overlay)]"
          >
            {/* 按 authenticated 而不是"有没有 user_id"：off 模式会给一个匿名默认身份，
                那种情况下显示「退出登录」是骗人的 */}
            {me?.authed ? (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  // 菜单项动作可能开弹窗/整页跳转，菜单必须自己收起来：
                  // 弹窗盖在上面而菜单从左边露一截，看起来像两个层都卡住了
                  setMenuOpen(false);
                  onLogout();
                }}
                className={menuItem}
              >
                <Icon name="logout" size={14} />
                退出登录
              </button>
            ) : (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onRequestLogin();
                }}
                className={menuItem}
              >
                <Icon name="key" size={14} />
                登录中台
              </button>
            )}
            <a role="menuitem" href="/" onClick={() => setMenuOpen(false)} className={menuItem}>
              <Icon name="panel" size={14} />
              旧版工作台
            </a>
          </div>
        ) : null}
      </div>
    </aside>
  );
}

const iconBtn =
  "flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-md text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25";

const menuItem =
  "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-secondary text-ink-2 transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink";
