/**
 * 一个最小的 JSON 客户端。
 *
 * 从 plaza.html 里的 `api()` 原样搬过来，行为必须一致：
 *  - 带 credentials: 'same-origin'（登录态靠 cookie）
 *  - 非 2xx 时把后端 detail 变成 Error.message，因为界面要直接显示这句话
 *  - 后端偶尔返回非 JSON（网关错误页），不能因此抛 SyntaxError 掩盖真实状态码
 */

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const jsonHeaders = { "Content-Type": "application/json" };

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { ...jsonHeaders, ...(init.headers as Record<string, string> | undefined) },
  });

  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    // 不是 JSON：保留原文，后面当成 detail 用
    data = { detail: text };
  }

  if (!res.ok) {
    const bag = data as { detail?: unknown; error?: unknown } | null;
    const raw = bag?.detail ?? bag?.error ?? `HTTP ${res.status}`;
    const message = typeof raw === "string" ? raw : JSON.stringify(raw);
    throw new ApiError(message, res.status);
  }

  return data as T;
}

/** 把错误统一成一句能显示的话。 */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}
