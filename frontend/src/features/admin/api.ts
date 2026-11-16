import { apiRequest } from "@/lib/http";
import type {
  AuthPlatformStatus,
  ConnectorList,
  CreditData,
  LogEntry,
  LogStatus,
  LoginPlatform,
  Me,
  Overview,
  PlatformConnector,
  PlatformTestResult,
  PlazaData,
  PolicyGroup,
  SkillList,
  UserDetail,
  UserList,
} from "./types";
import type { AuditEntry } from "./format";

/**
 * 控制台的九个端点组。
 *
 * 单独包一层而不是在组件里直接写 fetch：路径都在这里，改一处就够了；
 * 而且 401/403 的处理是全局的（见 lib/http），组件里不该出现第二份判断。
 */

const enc = encodeURIComponent;

export const consoleApi = {
  me: () => apiRequest<Me>("/api/console/me"),

  overview: (days: number) => apiRequest<Overview>(`/api/console/overview?days=${days}`),

  users: (params: URLSearchParams) => apiRequest<UserList>(`/api/console/users?${params}`),
  user: (userId: string, days = 14) =>
    apiRequest<UserDetail>(`/api/console/users/${enc(userId)}?days=${days}`),
  localAccounts: () =>
    apiRequest<{ accounts: { user_id: string; username: string }[] }>(
      "/api/console/local-accounts?limit=500",
    ),
  createUser: (body: {
    username: string;
    password: string;
    display_name: string;
    role: string;
    must_change_password: boolean;
  }) => apiRequest("/api/console/users", { method: "POST", body: JSON.stringify(body) }),
  setPassword: (userId: string, password: string, username: string) =>
    apiRequest<{ username?: string }>(`/api/console/users/${enc(userId)}/password`, {
      method: "POST",
      body: JSON.stringify({ password, username }),
    }),
  unlock: (userId: string) =>
    apiRequest(`/api/console/users/${enc(userId)}/unlock`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  setRole: (userId: string, role: string) =>
    apiRequest<{ role: string; updated: number }>(`/api/console/users/${enc(userId)}/role`, {
      method: "PATCH",
      body: JSON.stringify({ role, reason: "控制台手动调整" }),
    }),
  setStatus: (userId: string, status: string) =>
    apiRequest<{ revoked_sessions: number }>(`/api/console/users/${enc(userId)}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  deleteUser: (userId: string) =>
    apiRequest<{ deleted_rows: number }>(`/api/console/users/${enc(userId)}`, {
      method: "DELETE",
      body: JSON.stringify({ reason: "控制台手动删除", purge: true }),
    }),

  credits: (days: number, tenant: string) => {
    const params = new URLSearchParams({ days: String(days) });
    if (tenant) params.set("tenant", tenant);
    return apiRequest<CreditData>(`/api/console/credits?${params}`);
  },

  platforms: () => apiRequest<{ platforms: LoginPlatform[] }>("/api/console/platforms"),
  createPlatform: (body: unknown) =>
    apiRequest("/api/console/platforms", { method: "POST", body: JSON.stringify(body) }),
  updatePlatform: (id: string, body: unknown) =>
    apiRequest(`/api/console/platforms/${enc(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deletePlatform: (id: string) =>
    apiRequest(`/api/console/platforms/${enc(id)}`, { method: "DELETE" }),
  testPlatform: (id: string, username: string, password: string) =>
    apiRequest<PlatformTestResult>(`/api/console/platforms/${enc(id)}/test`, {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  platformConnectors: () =>
    apiRequest<{ items: PlatformConnector[] }>("/api/console/platform/connectors"),
  connectorBindings: () => apiRequest<ConnectorList>("/api/console/connectors"),
  saveConnector: (body: unknown) =>
    apiRequest("/api/console/platform/connectors", { method: "POST", body: JSON.stringify(body) }),
  toggleConnector: (id: string, enabled: boolean) =>
    apiRequest(`/api/console/platform/connectors/${enc(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  deleteConnector: (id: string) =>
    apiRequest(`/api/console/platform/connectors/${enc(id)}`, { method: "DELETE" }),

  platformSkills: () => apiRequest<SkillList>("/api/console/platform/skills"),
  saveSkill: (body: unknown) =>
    apiRequest("/api/console/platform/skills", { method: "POST", body: JSON.stringify(body) }),
  toggleSkill: (id: string, enabled: boolean) =>
    apiRequest(`/api/console/platform/skills/${enc(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  deleteSkill: (id: string) =>
    apiRequest(`/api/console/platform/skills/${enc(id)}`, { method: "DELETE" }),

  policies: () => apiRequest<{ tenants: PolicyGroup[] }>("/api/console/policies"),
  togglePolicy: (policyId: string, enabled: boolean) =>
    apiRequest(`/api/console/policies/${enc(policyId)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),

  audit: () => apiRequest<{ events: AuditEntry[] }>("/api/console/audit?limit=200"),

  plaza: (status: string, q: string) => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (q) params.set("q", q);
    return apiRequest<PlazaData>(`/api/console/plaza?${params}`);
  },
  createPlazaItem: (body: unknown) =>
    apiRequest("/api/console/plaza", { method: "POST", body: JSON.stringify(body) }),
  updatePlazaItem: (itemId: string, body: unknown) =>
    apiRequest(`/api/console/plaza/${enc(itemId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deletePlazaItem: (itemId: string) =>
    apiRequest(`/api/console/plaza/${enc(itemId)}`, { method: "DELETE" }),

  logStatus: () => apiRequest<LogStatus>("/api/console/logs/status"),
  logHistory: (params: URLSearchParams) =>
    apiRequest<{ entries: LogEntry[]; available?: boolean }>(`/api/console/logs?${params}`),
  setRunLevel: (level: string) =>
    apiRequest<{ level: string }>("/api/console/logs/level", {
      method: "POST",
      body: JSON.stringify({ level }),
    }),

  authPlatforms: () =>
    apiRequest<AuthPlatformStatus>("/api/auth/platform-login/status").catch(() => null),
  platformLogin: (username: string, password: string, platform: string) =>
    apiRequest<{ role?: string; user_id?: string }>("/api/auth/platform-login", {
      method: "POST",
      body: JSON.stringify({ username, password, platform: platform || undefined }),
    }),
};
