/**
 * 消息平台的接口层。
 *
 * 路径集中在这里，不散在各组件：这一页有 9 个端点，其中三个是同一渠道的
 * login 子资源，拼错一个只会在运行时 404。
 *
 * 与 bots 的 api 层不同，这里**不**做 null → undefined 的归一：鉴权失败、
 * 数据库没就绪时后端返回的是 `{detail}`（由 lib/api 抛成 ApiError），
 * 业务字段都是清单里声明的字符串/布尔/数组，没有可空的 PATCH 载荷需要清洗。
 */
import { api } from "@/lib/api";
import type {
  BotOption,
  ChannelListPayload,
  ChannelPatch,
  ChannelView,
  CreateInstanceResult,
  InstanceListPayload,
  SessionListPayload,
  WeixinLoginView,
} from "./types";

const enc = encodeURIComponent;

/**
 * 实例是**查询参数**而不是路径段：`/channels/feishu` 这个资源没变，
 * 变的只是"哪一份"。默认实例（''）不带参数，老调用方和深链继续有效。
 */
const q = (instance?: string) => (instance ? `?instance=${enc(instance)}` : "");

/* ---------------- 渠道 ---------------- */

export const getChannels = (instance = "") =>
  api<ChannelListPayload>(`/api/messaging/channels${q(instance)}`);

export const getChannel = (id: string, instance = "") =>
  api<ChannelView>(`/api/messaging/channels/${enc(id)}${q(instance)}`);

export const patchChannel = (id: string, body: ChannelPatch, instance = "") =>
  api<ChannelView>(`/api/messaging/channels/${enc(id)}${q(instance)}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });

/** 发一条测试消息：只进出行队列，真正发出去的是 Worker。 */
export const testChannel = (id: string, chatId: string, instance = "") =>
  api<{ ok: boolean; queued: boolean; target: string }>(
    `/api/messaging/channels/${enc(id)}/test${q(instance)}`,
    { method: "POST", body: JSON.stringify({ chat_id: chatId }) },
  );

/** 让 Worker 重建连接。配置保存后不会自动生效，要不重启 Worker，要不点这个。 */
export const reloadChannel = (id: string, instance = "") =>
  api<{ ok: boolean; note: string }>(
    `/api/messaging/channels/${enc(id)}/reload${q(instance)}`,
    { method: "POST" },
  );

/* ---------------- 机器人实例 ---------------- */

export const getInstances = () =>
  api<InstanceListPayload>("/api/messaging/instances");

export const createInstance = (botId: string) =>
  api<CreateInstanceResult>("/api/messaging/instances", {
    method: "POST",
    body: JSON.stringify({ bot_id: botId }),
  });

export const deleteInstance = (instanceId: string) =>
  api<{ ok: boolean; removed: number; note: string }>(
    `/api/messaging/instances/${enc(instanceId)}`,
    { method: "DELETE" },
  );

/* ---------------- 会话身份 ---------------- */

/**
 * 这个渠道（这个实例）最近的会话，以及每段会话由谁回话。
 * 一次带回机器人花名册：卡片的下拉和 tab 上的名字必须同源，否则会出现
 * 「下拉里的 X助手」和「tab 上的 X助手」是两个不同 id 的怪事。
 */
export const getSessions = (channelId: string, instance = "", limit = 30) => {
  const params = new URLSearchParams({ channel_id: channelId, limit: String(limit) });
  if (instance) params.set("instance", instance);
  return api<SessionListPayload>(`/api/messaging/sessions?${params.toString()}`);
};

/** `botId=''` = 恢复跟随渠道默认。只写覆盖值，真正换会话是下一条消息的事。 */
export const bindSession = (body: {
  channel_id: string;
  instance: string;
  chat_id: string;
  bot_id: string;
}) =>
  api<{ ok: boolean; chat_id: string; bot_id: string; note: string }>(
    "/api/messaging/sessions",
    { method: "PUT", body: JSON.stringify(body) },
  );

/* ---------------- 配对 ---------------- */

export const approvePairing = (code: string) =>
  api<{ ok: boolean; platform?: string; user_id?: string }>(
    "/api/messaging/pairing/approve",
    { method: "POST", body: JSON.stringify({ code }) },
  );

/**
 * 撤销授权。
 * 老页面把 `platform|user_id` 拼成一个字符串塞进 data 属性再 split('|') 还原，
 * 而 user_id 本身可能含 `|`（企业微信的 UserID 允许），这里改成直接传两个参数。
 */
export const revokePairing = (platform: string, userId: string) =>
  api<{ ok: boolean }>("/api/messaging/pairing/revoke", {
    method: "POST",
    body: JSON.stringify({ platform, user_id: userId }),
  });

/* ---------------- 个人微信扫码登录 ---------------- */

export const weixinLoginStart = () =>
  api<WeixinLoginView>("/api/messaging/channels/weixin/login/start", { method: "POST" });

export const weixinLoginStatus = () =>
  api<WeixinLoginView>("/api/messaging/channels/weixin/login/status");

export const weixinLoginCancel = () =>
  api<unknown>("/api/messaging/channels/weixin/login/cancel", { method: "POST" });

export const weixinLoginLogout = () =>
  api<unknown>("/api/messaging/channels/weixin/login/logout", { method: "POST" });

/* ---------------- 绑定机器人用的花名册 ---------------- */

/**
 * `/api/bots` 返回的是 `{items, sections, rooms}`，**不是** `{bots}`。
 * 老 messaging.html 读的是 `data.bots || []`，所以「绑定机器人」下拉框
 * 一直是空的——只有「不绑定（通用助手）」一个选项，谁都没发现。
 */
export const getBots = () =>
  api<{ items?: BotOption[] }>("/api/bots").then((data) => data.items ?? []);
