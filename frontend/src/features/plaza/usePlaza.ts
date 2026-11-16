import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "@/lib/api";
import type {
  BotCategory,
  BotDetail,
  BotItem,
  InstallResponse,
  MineResponse,
  PlazaListResponse,
  PlazaOrder,
} from "@/lib/types";

/**
 * 广场的列表状态。
 *
 * 两件必须做对的事：
 *  1. 防抖 220ms —— 旧页是同样的值，输入"数据分析"不该打四次请求。
 *  2. 竞态保护 —— 用户快速切分类时，先发的请求可能后回来。用自增序号
 *     丢弃过期响应，否则界面会显示上一次筛选的结果。
 */

export type LoadStatus = "idle" | "loading" | "ready" | "error";

export interface PlazaQuery {
  q: string;
  category: string;
  order: PlazaOrder;
}

const EMPTY = {
  items: [] as BotItem[],
  categories: [] as BotCategory[],
  total: 0,
  installedCount: 0,
};

export function usePlaza(initialQuery?: Partial<PlazaQuery>) {
  const [query, setQuery] = useState<PlazaQuery>({
    q: initialQuery?.q ?? "",
    category: initialQuery?.category ?? "",
    order: initialQuery?.order ?? "featured",
  });
  const [input, setInput] = useState(query.q);

  const [data, setData] = useState(EMPTY);
  const [status, setStatus] = useState<LoadStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  const requestSeq = useRef(0);

  const load = useCallback(async (next: PlazaQuery) => {
    const seq = ++requestSeq.current;
    setStatus("loading");
    setError(null);

    const params = new URLSearchParams();
    if (next.q) params.set("q", next.q);
    if (next.category) params.set("category", next.category);
    params.set("order", next.order);

    try {
      const res = await api<PlazaListResponse>(`/api/bot-plaza?${params.toString()}`);
      if (seq !== requestSeq.current) return; // 过期响应，丢掉
      setData({
        items: res.items ?? [],
        categories: res.categories ?? [],
        total: res.total ?? 0,
        installedCount: res.installed_count ?? 0,
      });
      setStatus("ready");
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(err));
      setStatus("error");
    }
  }, []);

  // 首次加载 + query 变化时重取
  useEffect(() => {
    void load(query);
  }, [query, load]);

  // 输入防抖：只有稳定的输入才更新 query
  useEffect(() => {
    if (input === query.q) return;
    const timer = window.setTimeout(() => {
      setQuery((prev) => ({ ...prev, q: input }));
    }, 220);
    return () => window.clearTimeout(timer);
  }, [input, query.q]);

  const setCategory = useCallback((category: string) => {
    setQuery((prev) => ({ ...prev, category }));
  }, []);

  const setOrder = useCallback((order: PlazaOrder) => {
    setQuery((prev) => ({ ...prev, order }));
  }, []);

  const reload = useCallback(() => {
    void load(query);
  }, [load, query]);

  /** 启用后原地更新那一条的 installed 标记，避免整页重取导致滚动位置跳动。 */
  const markInstalled = useCallback((itemId: string) => {
    setData((prev) => ({
      ...prev,
      installedCount: prev.installedCount + 1,
      items: prev.items.map((item) =>
        item.item_id === itemId ? { ...item, installed: true } : item,
      ),
    }));
  }, []);

  const markUninstalled = useCallback((itemId: string) => {
    setData((prev) => ({
      ...prev,
      installedCount: Math.max(0, prev.installedCount - 1),
      items: prev.items.map((item) =>
        item.item_id === itemId ? { ...item, installed: false } : item,
      ),
    }));
  }, []);

  return {
    query,
    input,
    setInput,
    setCategory,
    setOrder,
    data,
    status,
    error,
    reload,
    markInstalled,
    markUninstalled,
  };
}

/* ------------------------------------------------------------------ */
/* 详情                                                                */
/* ------------------------------------------------------------------ */

export function useBotDetail() {
  const [itemId, setItemId] = useState<string | null>(null);
  const [detail, setDetail] = useState<BotDetail | null>(null);
  const [status, setStatus] = useState<LoadStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const open = useCallback(async (id: string) => {
    setItemId(id);
    setDetail(null);
    setStatus("loading");
    setError(null);
    const mine = ++seq.current;
    try {
      const res = await api<BotDetail>(`/api/bot-plaza/${encodeURIComponent(id)}`);
      if (mine !== seq.current) return;
      setDetail(res);
      setStatus("ready");
    } catch (err) {
      if (mine !== seq.current) return;
      setError(errorMessage(err));
      setStatus("error");
    }
  }, []);

  const close = useCallback(() => {
    seq.current += 1; // 让在途请求作废
    setItemId(null);
    setDetail(null);
    setStatus("idle");
    setError(null);
  }, []);

  return { itemId, detail, status, error, open, close };
}

/* ------------------------------------------------------------------ */
/* 我的投稿                                                            */
/* ------------------------------------------------------------------ */

export function useMine() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<MineResponse | null>(null);
  const [status, setStatus] = useState<LoadStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setStatus("loading");
    setError(null);
    try {
      const res = await api<MineResponse>("/api/bot-plaza/mine");
      setData(res);
      setStatus("ready");
    } catch (err) {
      setError(errorMessage(err));
      setStatus("error");
    }
  }, []);

  const show = useCallback(() => {
    setOpen(true);
    void load();
  }, [load]);

  const hide = useCallback(() => setOpen(false), []);

  const withdraw = useCallback(
    async (itemId: string) => {
      await api(`/api/bot-plaza/mine/${encodeURIComponent(itemId)}`, { method: "DELETE" });
      await load();
    },
    [load],
  );

  return { open, data, status, error, show, hide, reload: load, withdraw };
}

/* ------------------------------------------------------------------ */
/* 启用 / 卸载                                                         */
/* ------------------------------------------------------------------ */

/** 装一份副本，拿到要跳转的会话地址。 */
export async function installBot(itemId: string): Promise<InstallResponse> {
  return api<InstallResponse>(`/api/bot-plaza/${encodeURIComponent(itemId)}/install`, {
    method: "POST",
  });
}

export async function uninstallBot(itemId: string): Promise<void> {
  await api(`/api/bot-plaza/${encodeURIComponent(itemId)}/install`, { method: "DELETE" });
}
