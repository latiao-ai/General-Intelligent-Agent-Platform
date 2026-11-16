/**
 * 主界面的接口层。
 *
 * 与旧页的三处约定必须一致：
 *  1. `POST /messages` 只要 202 就算接纳，答复由 Worker 后台生成；
 *  2. 答复走 SSE，断线由浏览器带 `Last-Event-ID` 重连，不用前端补游标；
 *  3. `request_id` 是幂等键——网络层失败时**复用同一个键**重发，服务端会
 *     返回原来那个 turn，而不是再接纳一次（否则同一句话会出现两条）。
 */
import { apiRequest } from "@/lib/http";
import type {
  AcceptedMessage,
  AddonsPatch,
  ArtifactView,
  CreditsBalance,
  ExpertItem,
  Me,
  ModelRow,
  ScheduleItem,
  SchedulePayload,
  ScheduleRun,
  SessionAddons,
  SessionRow,
  Timeline,
} from "./types";

/** 幂等键：12 位足够，服务端只做等值比较。 */
export function newRequestId() {
  return "req_" + Math.random().toString(36).slice(2, 12);
}

/** 会话 id 的合法性。服务端允许客户端给新 id，但 `null`/`undefined` 会被
 *  当成字符串写进 URL，于是 `POST /sessions/null/messages` 会建出一条叫
 *  "null" 的会话——答复全落到那条里，界面上就是"发了没反应"。 */
export function validSessionId(id: string | null | undefined): id is string {
  const v = String(id ?? "").trim();
  return v.length >= 6 && v !== "null" && v !== "undefined";
}

export function newSessionId() {
  const raw = globalThis.crypto?.randomUUID?.() ?? "";
  if (raw) return raw.replace(/-/g, "").slice(0, 20);
  return "s_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

export const hubApi = {
  me: () => apiRequest<Me>("/api/me"),

  /**
   * 探活。**不能走 apiRequest**：它遇 401 就抛错，而这里 401 的含义是
   * "服务活着，只是没登录"，把它当成"后端不可达"会把人引到错误方向去查。
   * 同时绕开全局 unauthorized 回调——健康探测不该触发登录跳转。
   */
  health: async (): Promise<boolean> => {
    try {
      const res = await fetch("/api/health", { credentials: "same-origin" });
      return res.status < 500;
    } catch {
      return false;
    }
  },

  sessions: () => apiRequest<{ sessions: SessionRow[] }>("/api/sessions"),

  timeline: (sessionId: string) =>
    apiRequest<Timeline>(`/api/sessions/${encodeURIComponent(sessionId)}/timeline`),

  /**
   * 发一条消息。`content_blocks` 只在带技能胶囊时才传——服务端会据此区分
   * "纯文本"与"带结构化引用"，空数组传过去反而会覆盖掉已有的块。
   */
  send: (sessionId: string, body: {
    request_id: string;
    message: string;
    connector_ids: string[];
    content_blocks?: unknown[];
    addon_selection?: Record<string, unknown>;
    target_task_id?: string;
  }) => apiRequest<AcceptedMessage>(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    body: JSON.stringify(body),
  }),

  turnEventsUrl: (sessionId: string, turnId: string) =>
    `/api/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/events`,

  taskEventsUrl: (taskId: string, after = 0) =>
    `/api/tasks/${encodeURIComponent(taskId)}/events?after=${after}`,

  task: (taskId: string) =>
    apiRequest<{ state?: { artifact?: ArtifactView; deployment?: { public_url?: string; deployment_id?: string } } }>(
      `/api/tasks/${encodeURIComponent(taskId)}`,
    ),

  cancelTask: (taskId: string) =>
    apiRequest<unknown>(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" }),

  models: () => apiRequest<{ models?: ModelRow[]; current?: string }>("/api/models"),

  selectModel: (modelId: string) =>
    apiRequest<unknown>("/api/models/select", {
      method: "POST",
      body: JSON.stringify({ model: modelId }),
    }),

  credits: () => apiRequest<CreditsBalance>("/api/credits/balance"),

  logout: () => apiRequest<unknown>("/api/auth/session", { method: "DELETE" }),
};

/**
 * 定时任务。`enabled` 是**总开关**（服务端 `SCHEDULED_TASKS_ENABLED`），
 * 关着时任务能存但不会被触发——这件事必须显式告诉用户，否则他们会以为
 * 自己写的规则没生效是 bug。
 */
export const scheduleApi = {
  list: (status: "all" | "active" | "paused") =>
    apiRequest<{ tasks: ScheduleItem[]; enabled: boolean }>(`/api/schedules?status=${status}`),

  create: (body: SchedulePayload) =>
    apiRequest<ScheduleItem>("/api/schedules", { method: "POST", body: JSON.stringify(body) }),

  update: (id: string, body: Partial<SchedulePayload>) =>
    apiRequest<ScheduleItem>(`/api/schedules/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  remove: (id: string) =>
    apiRequest<unknown>(`/api/schedules/${encodeURIComponent(id)}`, { method: "DELETE" }),

  trigger: (id: string) =>
    apiRequest<{ run_id?: string }>(`/api/schedules/${encodeURIComponent(id)}/trigger`, {
      method: "POST",
    }),

  runs: (id: string, limit = 30) =>
    apiRequest<{ runs: ScheduleRun[] }>(
      `/api/schedules/${encodeURIComponent(id)}/runs?limit=${limit}`,
    ),

  testDelivery: (id: string) =>
    apiRequest<{ ok?: boolean; detail?: string }>(
      `/api/schedules/${encodeURIComponent(id)}/delivery/test`,
      { method: "POST" },
    ),
};

/* ------------------------------------------------------------------------ *
 * 画像与记忆
 *
 * 三层记忆不是同一个接口，别混：
 *  - `/api/profile`                用户画像（每行「键：值」，优先级高于同名记忆）
 *  - `/api/sessions/{id}/memory`   会话记忆（摘要 + 事实，跟着这一轮对话走）
 *  - `/api/memory`                 用户长期记忆 + 工作区记忆（跨会话/跨目录）
 * ------------------------------------------------------------------------ */

export interface ProfileData {
  text?: string;
  facts?: Record<string, string>;
  version?: number;
}

export interface SessionMemoryData {
  summary?: string;
  facts_text?: string;
  facts?: Record<string, string>;
  /** 已经被压缩进摘要的消息条数；0 表示还没压缩过 */
  upto?: number;
}

export interface LongTermMemoryData {
  user?: { facts_text?: string; facts?: Record<string, string> };
  workspace?: { facts_text?: string; facts?: Record<string, string> };
}

export const memoryApi = {
  profile: () => apiRequest<ProfileData>("/api/profile"),

  saveProfile: (text: string) =>
    apiRequest<{ count?: number; version?: number }>("/api/profile", {
      method: "PUT",
      body: JSON.stringify({ text }),
    }),

  clearProfile: () => apiRequest<unknown>("/api/profile", { method: "DELETE" }),

  session: (sessionId: string) =>
    apiRequest<SessionMemoryData>(`/api/sessions/${encodeURIComponent(sessionId)}/memory`),

  saveSession: (sessionId: string, body: { summary: string; facts: string }) =>
    apiRequest<{ facts?: Record<string, string> }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/memory`,
      { method: "PUT", body: JSON.stringify(body) },
    ),

  clearSession: (sessionId: string) =>
    apiRequest<unknown>(`/api/sessions/${encodeURIComponent(sessionId)}/memory`, {
      method: "DELETE",
    }),

  longTerm: () => apiRequest<LongTermMemoryData>("/api/memory"),

  saveLongTerm: (scope: "user" | "workspace", facts: string) =>
    apiRequest<{ count?: number }>("/api/memory", {
      method: "PUT",
      body: JSON.stringify({ scope, facts }),
    }),

  clearLongTerm: (scope: "user" | "workspace") =>
    apiRequest<unknown>(`/api/memory?scope=${scope}`, { method: "DELETE" }),
};

/* ------------------------------------------------------------------------ *
 * 技能 · 连接器
 *
 * 这两个是"本租户能用什么工具"的两条来源：技能是提示词包，连接器是 MCP 服务。
 * 列表都带 `existing=1` / 只保留 `configured`，避免把商店货架当成"我有的东西"。
 * ------------------------------------------------------------------------ */

export interface SkillItem {
  id: string;
  name?: string;
  description?: string;
  icon?: string;
  /** platform=管理员发布 / custom=我新建 / user=我上传 / skillhub=商店 */
  source?: string;
  /** 已经在磁盘上（=装过）；false 表示"待添加" */
  on_disk?: boolean;
  enabled?: boolean;
  platform?: boolean;
}

export interface ConnectorItem {
  id: string;
  name?: string;
  icon?: string;
  transport?: string;
  tools?: number;
  configured?: boolean;
  enabled?: boolean;
  need_authorize?: boolean;
  description?: string;
}

export const skillApi = {
  list: (q: string) => {
    const params = new URLSearchParams({ kind: "skill", existing: "1" });
    if (q) params.set("q", q);
    return apiRequest<{ items: SkillItem[]; installed_count?: number }>(
      `/api/skills?${params.toString()}`,
    );
  },

  /** 已落盘的开关启停，未落盘的就安装。与旧页卡片上的「+」同一套语义。 */
  toggle: (item: SkillItem) =>
    item.on_disk
      ? apiRequest<unknown>(
          `/api/skills/${encodeURIComponent(item.id)}/enable?enabled=true`,
          { method: "POST" },
        )
      : apiRequest<unknown>(`/api/skills/${encodeURIComponent(item.id)}/install`, {
          method: "POST",
        }),

  /**
   * 上传技能包。必须走裸 fetch：`apiRequest` 会给带 body 的请求塞
   * `Content-Type: application/json`，而 multipart 的 boundary 得让浏览器自己写。
   */
  upload: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/skills/upload", {
      method: "POST",
      credentials: "same-origin",
      body: form,
    });
    const text = await res.text();
    let payload: unknown = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = { detail: text };
    }
    if (!res.ok) {
      const bag = payload as { detail?: unknown } | null;
      const raw = bag?.detail ?? `上传失败（HTTP ${res.status}）`;
      throw new Error(typeof raw === "string" ? raw : JSON.stringify(raw));
    }
    return payload as { id?: string; name?: string };
  },
};

/* ------------------------------------------------------------------------ *
 * 工作区文件（输入框 `@` 引用文件用）
 *
 * 只读、路径一律相对工作区根：绝对路径、`..`、逃出工作区的符号链接服务端直接
 * 400。隐藏文件（`.agent-skills` 这类内部物化目录）不会出现，别在这里再过滤一遍。
 * ------------------------------------------------------------------------ */

export interface WorkspaceEntry {
  name: string;
  /** 相对工作区根的路径，直接当 `@file:<path>` 用 */
  path: string;
  kind: "file" | "dir" | "symlink" | string;
  size?: number;
}

export const workspaceApi = {
  files: (path = ".") =>
    apiRequest<{ entries?: WorkspaceEntry[] }>(
      `/api/workspace/files?path=${encodeURIComponent(path)}`,
    ),
};

export const connectorApi = {
  list: () => apiRequest<{ items: ConnectorItem[] }>("/api/connectors"),

  connect: (id: string) =>
    apiRequest<unknown>(`/api/connectors/${encodeURIComponent(id)}/connect`, { method: "POST" }),

  disconnect: (id: string) =>
    apiRequest<unknown>(`/api/connectors/${encodeURIComponent(id)}/disconnect`, {
      method: "POST",
    }),

  /** 返回授权跳转地址；OAuth 必须整页跳走，不能在抽屉里开新窗口。 */
  startAuth: (id: string) =>
    apiRequest<{ authorize_url?: string; url?: string }>(
      `/api/connectors/${encodeURIComponent(id)}/oauth/start`,
      { method: "POST" },
    ),
};

/* ------------------------------------------------------------------------ *
 * 会话级选择：专家 + 技能
 *
 * 与 `Composer` 里的消息级引用是**两层**，别合并：
 *  - 这一层（`addon_selection`）跟着会话走，切到别的会话就换一套，影响之后每一轮；
 *  - 消息级引用只影响当前这一条，发完就没了。
 * ------------------------------------------------------------------------ */

export const addonsApi = {
  experts: () =>
    apiRequest<{ items: ExpertItem[]; total?: number; builtin?: number; custom?: number }>(
      "/api/experts",
    ),

  createExpert: (body: Record<string, unknown>) =>
    apiRequest<ExpertItem>("/api/experts", { method: "POST", body: JSON.stringify(body) }),

  updateExpert: (id: string, body: Record<string, unknown>) =>
    apiRequest<ExpertItem>(`/api/experts/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  removeExpert: (id: string) =>
    apiRequest<unknown>(`/api/experts/${encodeURIComponent(id)}`, { method: "DELETE" }),

  get: (sessionId: string) =>
    apiRequest<SessionAddons>(`/api/sessions/${encodeURIComponent(sessionId)}/addons`),

  patch: (sessionId: string, body: AddonsPatch) =>
    apiRequest<SessionAddons>(`/api/sessions/${encodeURIComponent(sessionId)}/addons`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
};
