/**
 * 机器人工作台的接口契约。
 *
 * 字段名与 `/api/bots/*` 的返回逐字对齐，不做"顺手改成驼峰"这种美化——
 * 一旦前端改了名，后端一加字段就没人知道该同步哪里。唯一的例外是
 * 把可空的 `null` 统一成 `undefined`（在 api 层做），因为 React 里
 * `value ?? ''` 比 `value === null ? '' : value` 好读。
 */

export interface BotSummary {
  bot_id: string;
  name: string;
  handle?: string;
  title?: string;
  description?: string;
  soul?: string;
  model?: string;
  skill_ids?: string[];
  connector_ids?: string[];
  section_id?: string;
  icon?: string;
  color?: string;
  shape?: string;
  image?: string;
  /** 服务端给的是 0/1，不是 boolean */
  pinned?: boolean | number;
  hidden?: boolean | number;
  status?: string;
  /** 花名册里那行灰字：最后一条消息的摘要，没有时退化成 @handle · title */
  last_preview?: string;
  chat_updated_at?: string;
}

export interface BotSection {
  section_id: string;
  name: string;
  position?: number;
}

export interface RoomMember {
  bot_id: string;
  handle?: string;
  name?: string;
  icon?: string;
}

export interface RoomSummary {
  room_id: string;
  name: string;
  topic?: string;
  status?: "open" | "settled" | "closed" | string;
  members?: RoomMember[];
  max_rounds?: number;
  max_messages?: number;
}

export interface RosterPayload {
  enabled?: boolean;
  items?: BotSummary[];
  sections?: BotSection[];
  rooms?: RoomSummary[];
}

export type MessageRole = "user" | "assistant" | "system";

export interface ChatMessage {
  role?: MessageRole | string;
  content?: string;
  created_at?: string;
}

export interface Turn {
  turn_id?: string;
  status?: string;
}

/** 投递收据：别的机器人给它发消息后的回执，`from_bot` 为空表示系统投递。 */
export interface InboxRow {
  from_bot?: string;
  status?: string;
  body?: string;
  reason?: string;
}

export interface BotChatPayload {
  bot?: BotSummary;
  session_id?: string;
  chat_session_id?: string;
  epoch?: string;
  messages?: ChatMessage[];
  turns?: Turn[];
  rooms?: RoomSummary[];
  inbox?: InboxRow[];
}

/**
 * 队友私信 / 定时任务的结果也会落进同一个会话，且 role 都是 user。
 * 界面必须把它们和"用户自己打的字"分开画，否则用户会以为那句话是自己说的。
 */
export type MessageKind = "user" | "assistant" | "teammate" | "cron";

export interface RoomEvent {
  kind?: string;
  payload?: Record<string, unknown>;
  created_at?: string;
}

export interface RoomPlan {
  action?: "speak" | "settle" | string;
  bot_id?: string;
  round?: number;
  position?: number;
  reason?: string;
}

export interface RoomDetail {
  room?: RoomSummary;
  members?: RoomMember[];
  events?: RoomEvent[];
  plan?: RoomPlan;
}

export interface RoomStepResult {
  action?: string;
  bot_id?: string;
  bot_name?: string;
  reason?: string;
}

export interface Routine {
  schedule_id?: string;
  name?: string;
  prompt?: string;
  enabled?: boolean;
  type?: string;
  last_status?: string | null;
  next_run_at?: string | null;
  rrule?: string;
  schedule_desc?: string;
  bot_id?: string;
}

export type PlazaStatus = "draft" | "pending" | "published" | "rejected" | "offline";

export interface PlazaItem {
  item_id?: string;
  source_bot_id?: string;
  status?: PlazaStatus | string;
  version?: number;
  install_count?: number;
  scope?: string;
  review_note?: string;
  category?: string;
  tags?: string[];
  readme?: string;
  starters?: string[];
}

export interface SkillItem {
  id: string;
  name?: string;
  description?: string;
  kind?: string;
}

export interface ConnectorItem {
  id: string;
  name?: string;
  description?: string;
  configured?: boolean;
  authorized?: boolean;
}

export interface CategoryOption {
  id: string;
  name: string;
}

/** 新建 / 编辑机器人时提交的载荷，字段名就是后端 `BotBody`。 */
export interface BotPayload {
  name: string;
  handle: string;
  title: string;
  description: string;
  soul: string;
  model: string;
  skill_ids: string[];
  connector_ids: string[];
  section_id: string;
  icon: string;
  color: string;
  shape: string;
}
