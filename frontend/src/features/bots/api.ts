/**
 * 机器人工作台的接口层。
 *
 * 六个视图共用同一组端点，所以路径拼接集中在这里——散在各组件里迟早会出现
 * 同一处 `encodeURIComponent` 漏写的情况。
 *
 * `fields` 这一层把后端的 `null` 归一成 `undefined`：React 里 `?? ''` 就够了，
 * 但如果字段是 `null` 又恰好被 `Object.keys` 遍历（比如 PATCH 载荷），
 * 就会把一个 null 写进数据库。归一一次，后面都省心。
 */
import { api } from "@/lib/api";
import type {
  BotChatPayload,
  BotPayload,
  BotSection,
  BotSummary,
  CategoryOption,
  ConnectorItem,
  PlazaItem,
  RosterPayload,
  RoomDetail,
  RoomStepResult,
  Routine,
  SkillItem,
  Turn,
} from "./types";

const enc = encodeURIComponent;

function clean<T>(value: T): T {
  if (Array.isArray(value)) return value.map((item) => clean(item)) as unknown as T;
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
      if (raw === null) continue;
      out[key] = clean(raw);
    }
    return out as T;
  }
  return value;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  return clean(await api<T>(path, init));
}

/* ---------------- 花名册 ---------------- */
export const getRoster = () => call<RosterPayload>("/api/bots");

export const createBot = (payload: BotPayload) =>
  call<BotSummary>("/api/bots", { method: "POST", body: JSON.stringify(payload) });

export const updateBot = (botId: string, payload: Partial<BotPayload> | Record<string, unknown>) =>
  call<BotSummary>(`/api/bots/${enc(botId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });

/** 只归档，不删档案：消息是审计资产，删了就没法复盘"它当时说了什么"。 */
export const archiveBot = (botId: string) =>
  call<unknown>(`/api/bots/${enc(botId)}?archive=1`, { method: "DELETE" });

/* ---------------- 分区 ---------------- */
export const createSection = (name: string, position: number) =>
  call<BotSection>("/api/bot-sections", {
    method: "POST",
    body: JSON.stringify({ name, position }),
  });

export const deleteSection = (sectionId: string) =>
  call<unknown>(`/api/bot-sections/${enc(sectionId)}`, { method: "DELETE" });

/* ---------------- 会话 ---------------- */
export const getBotChat = (botId: string) => call<BotChatPayload>(`/api/bots/${enc(botId)}/chat`);

export const sendBotMessage = (botId: string, message: string, requestId: string) =>
  call<{ session_id?: string; turn_id?: string }>(`/api/bots/${enc(botId)}/message`, {
    method: "POST",
    body: JSON.stringify({ message, request_id: requestId }),
  });

export const getBotRoutines = (botId: string) =>
  call<{ items?: Routine[] }>(`/api/bots/${enc(botId)}/routines`);

/** SSE 地址给 EventSource 用，不走 fetch。 */
export const turnEventsUrl = (sessionId: string, turnId: string) =>
  `/api/sessions/${enc(sessionId)}/turns/${enc(turnId)}/events`;

/* ---------------- 群聊 ---------------- */
export const getRoom = (roomId: string) => call<RoomDetail>(`/api/bot-rooms/${enc(roomId)}`);

export const createRoom = (body: {
  name: string;
  topic: string;
  members: string[];
  max_rounds: number;
  max_messages: number;
}) =>
  call<{ room_id: string }>("/api/bot-rooms", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const stepRoom = (roomId: string) =>
  call<RoomStepResult>(`/api/bot-rooms/${enc(roomId)}/step`, { method: "POST" });

export const closeRoom = (roomId: string) =>
  call<unknown>(`/api/bot-rooms/${enc(roomId)}/close`, { method: "POST" });

export const sendRoomMessage = (roomId: string, message: string) =>
  call<unknown>(`/api/bot-rooms/${enc(roomId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });

/* ---------------- 例行任务 ---------------- */
export const getSchedules = () => call<{ tasks?: Routine[] }>("/api/schedules");

export const createRoutine = (body: {
  name: string;
  prompt: string;
  rrule: string;
  bot_id: string;
}) =>
  call<unknown>("/api/schedules", {
    method: "POST",
    body: JSON.stringify({
      ...body,
      type: "recurring",
      enabled: true,
    }),
  });

/* ---------------- Bot 广场 ---------------- */
export const getMyPlazaItems = () => call<{ items?: PlazaItem[] }>("/api/bot-plaza/mine");

export const getPlazaCategories = () =>
  call<{ categories?: CategoryOption[] }>("/api/bot-plaza?limit=1");

export const publishBot = (
  botId: string,
  body: { category: string; tags: string[]; readme: string; starters: string[] },
) =>
  call<{ status?: string }>(`/api/bots/${enc(botId)}/publish`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const withdrawBot = (botId: string) =>
  call<unknown>(`/api/bots/${enc(botId)}/publish`, { method: "DELETE" });

/* ---------------- 选择器数据 ---------------- */
export const getModels = () =>
  call<{ models?: Array<string | { id?: string }> }>("/api/models");

export const getSkills = () => call<{ items?: SkillItem[] }>("/api/skills?installed=1&kind=skill");

export const getConnectors = () => call<{ items?: ConnectorItem[] }>("/api/connectors");

export type { Turn };
