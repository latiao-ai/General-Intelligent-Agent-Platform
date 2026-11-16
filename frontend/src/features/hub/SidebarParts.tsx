/**
 * 侧栏里除导航与会话行之外的两块：分组头、空间列表。
 *
 * 空间那一栏是"用户自己建的容器"，所以有两条硬规矩，旧页踩过：
 *  - 新建/重命名**不用 `window.prompt`**：移动端会被吞掉，而且没法校验长度与重名；
 *  - 「从列表移除」不是删除：后端没有删命名空间的接口，文件夹和文件必须原样留着，
 *    文案要说清这一点，否则用户会以为文件没了。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { Icon } from "./icons";
import { shortTime } from "./format";
import type { Space } from "./spaces";
import type { SessionRow } from "./types";

/** 可折叠的分组头。`children` 放右侧的附加按钮（比如「新建空间」）。 */
export function GroupHead({
  label,
  count,
  collapsed,
  onToggle,
  children,
}: {
  label: string;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-1 px-3 pt-2 pb-1">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        className="flex min-w-0 flex-1 items-center gap-1 text-label text-muted transition-colors duration-150 ease-standard hover:text-ink"
      >
        <Icon
          name="caret"
          size={12}
          className={cn(
            "shrink-0 transition-transform duration-150 ease-standard",
            collapsed && "-rotate-90",
          )}
        />
        <span className="truncate">{label}</span>
        <b className="font-mono font-normal text-muted">{count}</b>
      </button>
      {children}
    </div>
  );
}

const rowBase =
  "flex h-[30px] w-full items-center gap-1.5 rounded-md px-1.5 text-left text-secondary transition-colors duration-150 ease-standard";

type MenuState = { space: Space; top: number; left: number } | null;

export function SpaceList({
  spaces,
  activeId,
  sessions,
  currentId,
  collapsed,
  spaceOfSession,
  onToggleGroup,
  onToggleOpen,
  onCreate,
  onRename,
  onRemove,
  onOpenFolder,
  onNewTask,
  onOpenSession,
  onError,
}: {
  spaces: Space[];
  activeId: string;
  sessions: SessionRow[];
  currentId: string;
  collapsed: boolean;
  /** 某个任务归属的空间 id，没有归属返回空串 */
  spaceOfSession: (sessionId: string) => string;
  onToggleGroup: () => void;
  /** 点一下空间行：只展开/收起，不切工作区 */
  onToggleOpen: (space: Space) => void;
  onCreate: (name: string) => Promise<void>;
  onRename: (space: Space, name: string) => Promise<void>;
  onRemove: (space: Space) => Promise<void>;
  onOpenFolder: (space: Space) => Promise<void>;
  onNewTask: (space: Space) => Promise<void>;
  onOpenSession: (id: string) => void;
  onError: (message: string) => void;
}) {
  /** 正在输入名字的目标：`"new"` 是新建，其余是空间 id */
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState<MenuState>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  useEffect(() => {
    if (!menu) return;
    /**
     * 点菜单内部**不能**关掉自己。
     *
     * `mousedown` 里无条件 `setMenu(null)` 会让菜单在 `click` 之前就卸载，
     * 于是菜单项的 `onClick` 永远收不到事件——看上去像"点了没反应"。
     * 这也是为什么这里必须判断 `contains`。
     */
    const onDown = (event: MouseEvent) => {
      if (menuRef.current?.contains(event.target as Node)) return;
      setMenu(null);
    };
    // 滚动时直接收掉：菜单是 fixed 的，不跟着行跑，留着就会浮在别处
    const onScroll = () => setMenu(null);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenu(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("scroll", onScroll, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("scroll", onScroll, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  /** sessionId → 空间归属；挂在一次 useMemo 里算完，别在每行里做一遍 find */
  const grouped = useMemo(() => {
    const map = new Map<string, SessionRow[]>();
    spaces.forEach((space) => map.set(space.id, []));
    sessions.forEach((session) => {
      const list = map.get(spaceOfSession(session.session_id));
      if (list) list.push(session);
    });
    return map;
  }, [spaces, sessions, spaceOfSession]);

  const startCreate = () => {
    setDraft("");
    setEditing("new");
  };

  const commit = async (space: Space | null) => {
    const name = draft.trim();
    setEditing(null);
    setDraft("");
    if (!name) return;
    setBusy(true);
    try {
      if (space) await onRename(space, name);
      else await onCreate(name);
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const openMenu = (space: Space, anchor: HTMLElement) => {
    const rect = anchor.getBoundingClientRect();
    const WIDTH = 168;
    setMenu({
      space,
      top: rect.bottom + 4,
      left: Math.max(8, Math.min(rect.left, window.innerWidth - WIDTH - 8)),
    });
  };

  const run = async (task: () => Promise<void>) => {
    setMenu(null);
    setBusy(true);
    try {
      await task();
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="shrink-0">
      <GroupHead label="空间" count={spaces.length} collapsed={collapsed} onToggle={onToggleGroup}>
        <button
          type="button"
          onClick={startCreate}
          title="新建空间"
          aria-label="新建空间"
          className={
            "flex h-[20px] w-[20px] shrink-0 items-center justify-center rounded-[5px] text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
          }
        >
          <Icon name="plus" size={13} />
        </button>
      </GroupHead>

      {collapsed ? null : (
        <div className="px-2 pb-2">
          {editing === "new" ? (
            <NameInput
              ref={inputRef}
              value={draft}
              placeholder="空间名，比如「上海项目」"
              disabled={busy}
              onChange={setDraft}
              onCommit={() => void commit(null)}
              onCancel={() => {
                setEditing(null);
                setDraft("");
              }}
            />
          ) : null}

          {!spaces.length && editing !== "new" ? (
            <div className="px-1.5 py-1.5">
              <p className="text-label leading-relaxed text-muted">还没有空间，先建一个</p>
              <button
                type="button"
                onClick={startCreate}
                className="mt-1 flex items-center gap-1 text-label text-signal transition-colors duration-150 ease-standard hover:text-signal-deep"
              >
                <Icon name="plus" size={12} />
                新建空间
              </button>
            </div>
          ) : null}

          {spaces.map((space) => {
            const mine = grouped.get(space.id) ?? [];
            const active = space.id === activeId;
            if (editing === space.id) {
              return (
                <NameInput
                  key={space.id}
                  ref={inputRef}
                  value={draft}
                  placeholder="空间名"
                  disabled={busy}
                  onChange={setDraft}
                  onCommit={() => void commit(space)}
                  onCancel={() => {
                    setEditing(null);
                    setDraft("");
                  }}
                />
              );
            }
            return (
              <div key={space.id}>
                <div className="group relative">
                  <button
                    type="button"
                    onClick={() => onToggleOpen(space)}
                    aria-expanded={space.open}
                    aria-current={active ? "true" : undefined}
                    title={`${space.name}（点一下展开/收起，＋ 在里面新建任务）`}
                    className={cn(
                      rowBase,
                      "pr-12",
                      active ? "bg-signal-wash text-ink" : "text-ink-2 hover:bg-sunken",
                    )}
                  >
                    <Icon
                      name="caret"
                      size={12}
                      className={cn(
                        "shrink-0 text-muted transition-transform duration-150 ease-standard",
                        !space.open && "-rotate-90",
                      )}
                    />
                    <Icon
                      name="folder"
                      size={13}
                      className={cn("shrink-0", active ? "text-signal" : "text-muted")}
                    />
                    <span className="min-w-0 flex-1 truncate">{space.name}</span>
                    <b className="shrink-0 font-mono text-label font-normal text-muted">
                      {mine.length}
                    </b>
                  </button>

                  {/* 悬停才出现的两个动作：新建任务 / 更多。放 fixed 之外会压住名字，所以定宽右对齐 */}
                  <div className="absolute right-1 top-1/2 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition-opacity duration-150 ease-standard focus-within:opacity-100 group-hover:opacity-100">
                    <button
                      type="button"
                      title="新建任务"
                      aria-label={`在「${space.name}」里新建任务`}
                      onClick={() => void run(() => onNewTask(space))}
                      className={miniBtn}
                    >
                      <Icon name="plus" size={12} />
                    </button>
                    <button
                      type="button"
                      title="更多"
                      aria-label={`「${space.name}」的更多操作`}
                      aria-haspopup="menu"
                      onClick={(event) => openMenu(space, event.currentTarget)}
                      className={miniBtn}
                    >
                      <Icon name="dots" size={12} />
                    </button>
                  </div>
                </div>

                {space.open ? (
                  <div className="mt-0.5 mb-1 ml-4 border-l border-line pl-1.5">
                    {mine.length === 0 ? (
                      <p className="px-1.5 py-1 text-label leading-relaxed text-muted">
                        还没有任务，点右侧 ＋ 新建
                      </p>
                    ) : (
                      mine.slice(0, 12).map((session) => (
                        <button
                          key={session.session_id}
                          type="button"
                          onClick={() => onOpenSession(session.session_id)}
                          title={session.title}
                          aria-current={session.session_id === currentId ? "true" : undefined}
                          className={cn(
                            "block w-full rounded-md px-1.5 py-1 text-left transition-colors duration-150 ease-standard",
                            session.session_id === currentId
                              ? "bg-signal-wash text-ink"
                              : "text-ink-2 hover:bg-sunken",
                          )}
                        >
                          <span className="block truncate text-label leading-snug">
                            {session.title}
                          </span>
                          <span className="block truncate text-label text-muted">
                            {shortTime(session.updated_at)}
                          </span>
                        </button>
                      ))
                    )}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}

      {menu ? (
        <div
          ref={menuRef}
          role="menu"
          style={{ top: menu.top, left: menu.left }}
          className="fixed z-50 w-[168px] rounded-lg border border-line bg-paper p-1 shadow-[var(--shadow-overlay)]"
        >
          <MenuItem
            icon="folder"
            label="打开文件夹"
            onClick={() => void run(() => onOpenFolder(menu.space))}
          />
          <MenuItem
            icon="panel"
            label="重命名"
            onClick={() => {
              setMenu(null);
              setDraft(menu.space.name);
              setEditing(menu.space.id);
            }}
          />
          <MenuItem
            icon="close"
            label="从列表移除"
            danger
            onClick={() => void run(() => onRemove(menu.space))}
          />
        </div>
      ) : null}
    </div>
  );
}

/** 就地输入。回车提交、Esc 取消、失焦也算提交（否则输了名字点别处会白输）。 */
function NameInput({
  ref,
  value,
  placeholder,
  disabled,
  onChange,
  onCommit,
  onCancel,
}: {
  ref: React.Ref<HTMLInputElement>;
  value: string;
  placeholder: string;
  disabled: boolean;
  onChange: (value: string) => void;
  onCommit: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="mx-0.5 mb-1 flex items-center gap-1 rounded-md border border-line-strong bg-paper px-1.5">
      <Icon name="folder" size={13} className="shrink-0 text-muted" />
      <input
        ref={ref}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        aria-label={placeholder}
        autoComplete="off"
        maxLength={40}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            onCommit();
          } else if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
          }
        }}
        onBlur={onCommit}
        className="h-[28px] w-full min-w-0 border-0 bg-transparent text-secondary text-ink outline-none placeholder:text-muted"
      />
    </div>
  );
}

function MenuItem({
  icon,
  label,
  danger,
  onClick,
}: {
  icon: string;
  label: string;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-secondary transition-colors duration-150 ease-standard hover:bg-sunken",
        danger ? "text-danger hover:text-danger" : "text-ink-2 hover:text-ink",
      )}
    >
      <Icon name={icon} size={13} />
      {label}
    </button>
  );
}

const miniBtn =
  "flex h-[20px] w-[20px] items-center justify-center rounded-[5px] text-muted transition-colors duration-150 ease-standard hover:bg-paper hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25";
