import { useCallback, useEffect, useRef, useState } from "react";
import { setAuthHandlers } from "@/lib/http";
import { consoleApi } from "./api";
import { errorMessage } from "@/lib/api";
import type { AuthPlatformStatus, Me } from "./types";

export const TABS = [
  { id: "overview", label: "概览" },
  { id: "users", label: "用户" },
  { id: "credits", label: "积分" },
  { id: "login", label: "登录系统" },
  { id: "connectors", label: "连接器" },
  { id: "skills", label: "技能" },
  { id: "plaza", label: "Bot 广场" },
  { id: "audit", label: "审计" },
  { id: "logs", label: "日志" },
] as const;

export type TabId = (typeof TABS)[number]["id"];

export type Gate =
  | { kind: "loading" }
  | { kind: "login"; message: string }
  | { kind: "denied"; message: string; me: Me | null }
  | { kind: "ready"; me: Me };

/**
 * 准入 + 页签。
 *
 * 未登录和权限不足是两件事，旧页把它们分开了，这里保持：前者要给登录框
 * （控制台自带，不跳走——整站共用一份 session，跳回首页往往还带着旧账号），
 * 后者要说清"你现在是谁、需要什么角色"。
 */
export function useAdmin() {
  const [gate, setGate] = useState<Gate>({ kind: "loading" });
  const [tab, setTabState] = useState<TabId>(() => {
    const raw = (location.hash || "").replace("#", "");
    // 老书签里的 #connect 已经把这一页拆成「连接器」「技能」两个标签
    const normalized = raw === "connect" ? "connectors" : raw;
    return (TABS.some((t) => t.id === normalized) ? normalized : "overview") as TabId;
  });
  const [authPlatforms, setAuthPlatforms] = useState<AuthPlatformStatus | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const showLogin = useCallback((message: string) => {
    // "未登录"和标题重复，没必要再显示一遍；"登录已过期""账号已停用"要留着
    setGate({ kind: "login", message: message === "未登录" ? "" : message });
  }, []);

  const showDenied = useCallback((message: string, me: Me | null) => {
    setGate({ kind: "denied", message, me });
  }, []);

  const refreshIdentity = useCallback(async () => {
    // 直接用 fetch，避免和 apiRequest 的 403 回调互相递归
    let res: Response;
    try {
      res = await fetch("/api/console/me", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
    } catch {
      return;
    }
    if (res.status === 401) {
      showLogin("登录已过期");
      return;
    }
    if (!res.ok) return;
    const me = (await res.json().catch(() => null)) as Me | null;
    if (!me || !mounted.current) return;
    if (!me.allowed) {
      showDenied(`当前角色：${me.role}（${me.user_id}）`, me);
      return;
    }
    setGate({ kind: "ready", me });
  }, [showDenied, showLogin]);

  useEffect(() => {
    setAuthHandlers({ unauthorized: showLogin, forbidden: () => void refreshIdentity() });
  }, [refreshIdentity, showLogin]);

  /** 登录框需要的平台清单。账号密码入口不可用时返回 null，页面退回"去主界面登录"。 */
  const loadAuthPlatforms = useCallback(async () => {
    const data = await consoleApi.authPlatforms();
    if (mounted.current) setAuthPlatforms(data && data.enabled ? data : null);
    return data && data.enabled ? data : null;
  }, []);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const me = await consoleApi.me();
        if (!alive) return;
        if (!me.allowed) {
          setGate({ kind: "denied", message: `当前角色：${me.role}（${me.user_id}）`, me });
          return;
        }
        setGate({ kind: "ready", me });
      } catch (err) {
        if (!alive) return;
        const status = (err as { status?: number }).status;
        if (status === 401) showLogin(errorMessage(err));
        else {
          setGate({ kind: "denied", message: errorMessage(err), me: null });
          void loadAuthPlatforms();
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [loadAuthPlatforms, showLogin]);

  /* 地址栏改 hash 也要跟着切：刷新页面不会触发（hash 只在开屏读一次），
     但把 #logs 这样的链接发给同事、或者手动改地址栏时，界面不能停在原地。 */
  useEffect(() => {
    const onHash = () => {
      const raw = (location.hash || "").replace("#", "");
      const normalized = raw === "connect" ? "connectors" : raw;
      if (TABS.some((t) => t.id === normalized)) setTabState(normalized as TabId);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const setTab = useCallback((next: TabId) => {
    setTabState(next);
    // 地址栏跟着标签走：刷新不丢位置，也能把某个视图直接发给同事
    history.replaceState(null, "", `#${next}`);
  }, []);

  return { gate, tab, setTab, refreshIdentity, showLogin, showDenied, authPlatforms, loadAuthPlatforms };
}
