/**
 * 空间（文件夹）+ 置顶 + 分组折叠。
 *
 * 三件事必须先说清楚，否则这块一定会被改坏：
 *
 * 1. **服务端只有「命名空间」，没有文件夹。** `/api/workspace/namespace` 建的是
 *    沙箱里的一级目录；侧栏里「空间」这个名字是前端加的壳，sessionId → spaceId
 *    的归属只记在 localStorage。所以空间**不会跨设备同步**，也**不会**出现在别的
 *    浏览器里。这是现状，不是这一版的偷懒。
 *
 * 2. **键名与旧页逐字一致**（`wb.spaces.v2.<user>`、`wb.assign.v2.<user>`、
 *    `wb.pins.v1`、`wb.groups.v1`、`wb.hidden.v2.<user>`）。改一个字，用户在旧页
 *    建好的空间在新页就凭空消失——而文件夹和文件其实还好端端躺在磁盘上，这种
 *    "数据没丢但看不见"最容易被当成 bug 排查半天。
 *
 * 3. `我的空间` 是沙箱私有根目录的标签（后端 `list_namespaces` 固定返回它），
 *    不是用户建的空间：不进侧栏，也不许被占用。
 *
 * 置顶和折叠状态是**全局**键（旧页也如此）。pins 里存的是 session id，本身就唯一，
 * 跨账号最多多几条死条目；为这个去重置用户已经置顶的东西不值得。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { errorMessage } from "@/lib/api";
import { apiRequest } from "@/lib/http";

/** 沙箱私有根目录的标签。不是用户建的空间。 */
export const RESERVED_SPACE = "我的空间";

export interface Space {
  id: string;
  name: string;
  /** 展开状态（空间里的任务列表是否显示） */
  open: boolean;
  /** 只有"能选本地文件夹"的环境才有值；私有目录模式下是空 */
  path?: string;
}

/** `GET /api/workspace` 的形状，和后端 `_workspace_view` 一一对应。 */
export interface WorkspaceInfo {
  /** 这台机器上能不能弹原生目录选择框 */
  enabled: boolean;
  path: string;
  private_root: string;
  /** 当前命名空间名；停在私有根目录时为空串 */
  namespace: string;
  namespaces: { name: string }[];
}

export type GroupKey = "pin" | "task" | "space";
export type Groups = Record<GroupKey, boolean>;

/* ------------------------------------------------------------------ *
 * 存储
 * ------------------------------------------------------------------ */

const K_PINS = "wb.pins.v1";
const K_GROUPS = "wb.groups.v1";
const K_SPACES = "wb.spaces.v2";
const K_ASSIGN = "wb.assign.v2";
const K_HIDDEN = "wb.hidden.v2";

const userKey = (base: string, user: string) => `${base}.${user || "anon"}`;

/** Safari 无痕模式里 localStorage 读写会直接抛，别让整页跟着白屏。 */
function readJSON<T>(storageKey: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(storageKey);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJSON(storageKey: string, value: unknown) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(value));
  } catch {
    /* 记不住置顶也得让页面能用 */
  }
}

export function newSpaceId() {
  return "sp_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

export function loadPins(): string[] {
  const raw = readJSON<unknown>(K_PINS, []);
  return Array.isArray(raw) ? raw.filter((id): id is string => typeof id === "string") : [];
}

export function loadGroups(): Groups {
  const raw = readJSON<Partial<Groups>>(K_GROUPS, {});
  return { pin: !!raw.pin, task: !!raw.task, space: !!raw.space };
}

export function loadHidden(user: string): string[] {
  const raw = readJSON<unknown>(userKey(K_HIDDEN, user), []);
  return Array.isArray(raw) ? raw.filter((name): name is string => typeof name === "string") : [];
}

/**
 * 读某个账号的空间与归属，并顺手清理脏数据。
 *
 * 清理不是洁癖：旧版本往 localStorage 里写过 `sp_default`（"我的空间"伪装成空间），
 * 也写过指向已删空间的归属。不在这里剔掉，侧栏会冒出无名条目、计数也对不上。
 */
export function loadSpaceState(user: string): {
  spaces: Space[];
  assign: Record<string, string>;
} {
  const rawSpaces = readJSON<unknown>(userKey(K_SPACES, user), []);
  const rawAssign = readJSON<unknown>(userKey(K_ASSIGN, user), {});

  const list = Array.isArray(rawSpaces) ? rawSpaces : [];
  const spaces = list.filter(
    (item): item is Space =>
      !!item &&
      typeof item === "object" &&
      typeof (item as Space).id === "string" &&
      typeof (item as Space).name === "string" &&
      (item as Space).name !== RESERVED_SPACE,
  );
  const alive = new Set(spaces.map((space) => space.id));

  const assign: Record<string, string> = {};
  const source = rawAssign && typeof rawAssign === "object" ? (rawAssign as Record<string, unknown>) : {};
  Object.entries(source).forEach(([sessionId, spaceId]) => {
    if (typeof spaceId === "string" && alive.has(spaceId)) assign[sessionId] = spaceId;
  });

  return { spaces, assign };
}

/* ------------------------------------------------------------------ *
 * 接口
 * ------------------------------------------------------------------ */

/**
 * 读工作区。
 *
 * 用裸 fetch 而不是 `apiRequest`：未登录时这里是 401，那是"还没登录"而不是
 * "出错了"，走统一封装会触发全局 unauthorized 回调，把一次普通的开屏探测
 * 变成"跳到登录页"。
 */
async function fetchWorkspace(): Promise<WorkspaceInfo | null> {
  try {
    const res = await fetch("/api/workspace", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as WorkspaceInfo;
  } catch {
    return null;
  }
}

const postJSON = (path: string, body: unknown) =>
  apiRequest<Record<string, unknown>>(path, { method: "POST", body: JSON.stringify(body) });

/* ------------------------------------------------------------------ *
 * Hook
 * ------------------------------------------------------------------ */

export function useSpaces(user: string) {
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [assign, setAssign] = useState<Record<string, string>>({});
  const [hidden, setHidden] = useState<string[]>([]);
  const [pins, setPins] = useState<string[]>(() => loadPins());
  const [groups, setGroups] = useState<Groups>(() => loadGroups());
  const [ws, setWs] = useState<WorkspaceInfo | null>(null);
  /** 载入完成前不落盘，否则会用空数组把已存的空间覆盖掉 */
  const [ready, setReady] = useState(false);

  /* 换账号就换一整套：两个账号共用一份空间列表是旧页明确修过的坑 */
  useEffect(() => {
    const state = loadSpaceState(user);
    setSpaces(state.spaces);
    setAssign(state.assign);
    setHidden(loadHidden(user));
    setReady(true);
  }, [user]);

  useEffect(() => {
    if (!ready) return;
    writeJSON(userKey(K_SPACES, user), spaces);
    writeJSON(userKey(K_ASSIGN, user), assign);
  }, [ready, user, spaces, assign]);

  useEffect(() => {
    if (!ready) return;
    writeJSON(userKey(K_HIDDEN, user), hidden);
  }, [ready, user, hidden]);

  const refresh = useCallback(async () => {
    const next = await fetchWorkspace();
    setWs(next);
    return next;
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, user]);

  /**
   * 服务端命名空间合并进侧栏。
   *
   * 这是"旧页建的空间在新页也能看到"的唯一通道——本地 localStorage 里并没有它，
   * 只有当 `/api/workspace` 把它列回来时才补一条。
   */
  useEffect(() => {
    if (!ws) return;
    const names = (ws.namespaces || [])
      .map((item) => String(item?.name || ""))
      .filter((name) => name && name !== RESERVED_SPACE && !hidden.includes(name));
    setSpaces((prev) => {
      const missing = names.filter((name) => !prev.some((space) => space.name === name));
      if (!missing.length) return prev;
      // 默认收起：一次接回三五个空间时，全部展开会把「还没有任务，点右侧 ＋ 新建」
      // 铺满整栏；计数角标已经说明了里面有几个任务，想看再点开。
      return prev.concat(missing.map((name) => ({ id: newSpaceId(), name, open: false })));
    });
  }, [ws, hidden]);

  /** 当前空间由服务端命名空间反推，不另存一份状态——两份状态必然对不上。 */
  const activeSpaceId = useMemo(() => {
    const name = ws?.namespace && ws.namespace !== RESERVED_SPACE ? ws.namespace : "";
    if (!name) return "";
    return spaces.find((space) => space.name === name)?.id || "";
  }, [ws, spaces]);

  const isServerSpace = useCallback(
    (space: Space) => !!ws?.namespaces?.some((item) => item.name === space.name),
    [ws],
  );

  const togglePin = useCallback((sessionId: string) => {
    setPins((prev) => {
      const next = prev.includes(sessionId)
        ? prev.filter((id) => id !== sessionId)
        : [sessionId, ...prev];
      writeJSON(K_PINS, next);
      return next;
    });
  }, []);

  const toggleGroup = useCallback((group: GroupKey) => {
    setGroups((prev) => {
      const next = { ...prev, [group]: !prev[group] };
      writeJSON(K_GROUPS, next);
      return next;
    });
  }, []);

  const createSpace = useCallback(
    async (rawName: string) => {
      const name = rawName.trim();
      if (!name) throw new Error("空间名不能为空");
      if (name === RESERVED_SPACE) throw new Error(`「${RESERVED_SPACE}」是系统保留名，换一个`);
      if (name.length > 40) throw new Error("名字太长了，最多 40 个字");
      if (spaces.some((space) => space.name === name)) throw new Error("已经有同名空间了");
      await postJSON("/api/workspace/namespace", { name });
      // 同名空间之前被「从列表移除」过，这里要放开，否则新建完又不见
      setHidden((prev) => prev.filter((item) => item !== name));
      await refresh();
      return name;
    },
    [spaces, refresh],
  );

  const renameSpace = useCallback(
    async (space: Space, rawName: string) => {
      const name = rawName.trim();
      if (!name || name === space.name) return;
      if (name === RESERVED_SPACE) throw new Error(`「${RESERVED_SPACE}」是系统保留名，换一个`);
      if (name.length > 40) throw new Error("名字太长了，最多 40 个字");
      if (spaces.some((item) => item.id !== space.id && item.name === name)) {
        throw new Error("已经有同名空间了");
      }
      if (isServerSpace(space)) {
        await postJSON("/api/workspace/rename", { name: space.name, new_name: name });
      }
      // 先改本地名再 refresh：反过来的话 adopt 会先补一条新名字的空间，
      // 而旧名字那条还在，侧栏里就直接出现两个。
      setSpaces((prev) => prev.map((item) => (item.id === space.id ? { ...item, name } : item)));
      if (isServerSpace(space)) await refresh();
    },
    [spaces, isServerSpace, refresh],
  );

  /** 展开/收起空间里的任务列表。纯本地，不动服务端命名空间。 */
  const toggleSpaceOpen = useCallback((space: Space) => {
    setSpaces((prev) =>
      prev.map((item) => (item.id === space.id ? { ...item, open: !item.open } : item)),
    );
  }, []);

  /**
   * 从侧栏移除。
   *
   * 后端**没有**删除命名空间的接口，所以这里只是"不再列出来"：磁盘上的文件夹和
   * 里面的文件一个字节都不动。恢复方式是重新建一个同名空间（createSpace 会把
   * 它从 hidden 里放出来）。
   */
  const removeSpace = useCallback(
    async (space: Space) => {
      if (isServerSpace(space)) {
        setHidden((prev) => (prev.includes(space.name) ? prev : prev.concat(space.name)));
      }
      setSpaces((prev) => prev.filter((item) => item.id !== space.id));
      setAssign((prev) => {
        const next: Record<string, string> = {};
        Object.entries(prev).forEach(([sessionId, spaceId]) => {
          if (spaceId !== space.id) next[sessionId] = spaceId;
        });
        return next;
      });
    },
    [isServerSpace],
  );

  /** 切到某个空间（服务端切命名空间）。调用方负责随后新建任务。 */
  const enterSpace = useCallback(
    async (space: Space) => {
      if (isServerSpace(space)) {
        await postJSON("/api/workspace/select", { name: space.name });
      } else if (space.path) {
        await postJSON("/api/workspace", { path: space.path });
      }
      setSpaces((prev) => prev.map((item) => (item.id === space.id ? { ...item, open: true } : item)));
      await refresh();
    },
    [isServerSpace, refresh],
  );

  const openFolder = useCallback(
    async (space: Space) => {
      const body = isServerSpace(space) ? { name: space.name } : { path: space.path || "" };
      if (!body.name && !("path" in body && body.path)) throw new Error("这个空间还没有绑定文件夹");
      await postJSON("/api/workspace/open", body);
    },
    [isServerSpace],
  );

  /** 新任务归入当前空间——旧页由 `window.onNewSession` 回调做同一件事。 */
  const assignSession = useCallback(
    (sessionId: string, spaceId?: string) => {
      const target = spaceId ?? activeSpaceId;
      if (!sessionId || !target) return;
      setAssign((prev) => ({ ...prev, [sessionId]: target }));
    },
    [activeSpaceId],
  );

  const spaceOfSession = useCallback(
    (sessionId: string) => {
      const id = assign[sessionId];
      return id && spaces.some((space) => space.id === id) ? id : "";
    },
    [assign, spaces],
  );

  return {
    spaces,
    assign,
    pins,
    groups,
    ws,
    activeSpaceId,
    /** 统一取错误文案，调用方只管 toast */
    message: errorMessage,
    isServerSpace,
    spaceOfSession,
    togglePin,
    toggleGroup,
    toggleSpaceOpen,
    createSpace,
    renameSpace,
    removeSpace,
    enterSpace,
    openFolder,
    assignSession,
    refresh,
  };
}

export type SpacesState = ReturnType<typeof useSpaces>;
