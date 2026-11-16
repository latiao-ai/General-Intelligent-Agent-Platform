import { ApiError } from "./api";

/**
 * JSON 请求，外加两颗全局钩子。
 *
 * 控制台的 401/403 不是"这个请求失败了"，而是"你现在是谁变了"：
 *   - 401：会话没了，整页要切成登录框；
 *   - 403：会话还在但角色不够（可能在另一个标签页换了账号）。
 * 页面头顶的身份是开屏时渲染的，这时候已经过期了。所以统一在这里回调，
 * 由页面决定怎么刷新，而不是让每个调用方各写一遍。
 */

type Handler = (message: string) => void;

let onUnauthorized: Handler | null = null;
let onForbidden: Handler | null = null;

export function setAuthHandlers(next: { unauthorized?: Handler; forbidden?: Handler }) {
  onUnauthorized = next.unauthorized ?? null;
  onForbidden = next.forbidden ?? null;
}

export async function apiRequest<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(init.headers as Record<string, string> | undefined),
    },
  });

  const text = await res.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    // 网关错误页之类：保留状态码，别让 SyntaxError 掩盖真实原因
    payload = { detail: text };
  }

  if (!res.ok) {
    const bag = payload as { detail?: unknown; error?: unknown } | null;
    const raw = bag?.detail ?? bag?.error ?? `请求失败（HTTP ${res.status}）`;
    const message = typeof raw === "string" ? raw : JSON.stringify(raw);
    if (res.status === 401) onUnauthorized?.(message);
    else if (res.status === 403) onForbidden?.(message);
    throw new ApiError(message, res.status);
  }

  return payload as T;
}
