/**
 * 消息平台的接口契约。
 *
 * 字段名逐字对齐 `/api/messaging/*`，不改名。这些结构是**清单驱动**的：
 * 每个渠道（飞书 / 企微 / 个人微信）由 `messaging/registry.py` 描述，
 * 凭据字段和可选项都是数组，界面不知道具体有哪些——只负责把数组画出来。
 * 所以这里必须容忍缺字段（老部署、新增字段），全部标可选。
 */

export interface EnvField {
  key: string;
  env: string;
  /**
   * 这个实例下真正要填的变量名。默认实例等于 `env`；
   * 机器人实例是 `FEISHU_APP_ID__BOT_XZS` 这种带后缀的。
   * 界面上必须显示它，否则用户照着 `env` 填，第二个实例永远读不到凭据。
   */
  env_name?: string;
  label: string;
  required: boolean;
  /** 密钥类字段：后端只给 is_set，永远不给值 */
  secret: boolean;
  is_set: boolean;
  /** 非密钥字段回显的值（可能是默认值） */
  preview: string;
  masked: string;
  help: string;
  url: string;
}

export interface OptionChoice {
  value: string;
  label: string;
}

export interface OptionField {
  key: string;
  label: string;
  /** text | select | switch，后端目前只用 text，但契约允许另外两种 */
  kind: string;
  choices: OptionChoice[];
  help: string;
  value: string;
}

export interface Capabilities {
  card?: boolean;
  stream_edit?: boolean;
  typing?: boolean;
  thread?: boolean;
  reaction?: boolean;
  group?: boolean;
  max_text_chars?: number;
}

/**
 * 派生状态。注意 `state` 和 `state_label` 都由后端算好：
 * `state.py` 用一个纯函数从「开关 + 凭据是否齐全 + 运行态快照」推出最终状态，
 * 颜色映射只写一次，左侧列表和详情页才会永远同色。前端**不许**自己再推一遍。
 */
export type StateTone = "good" | "warn" | "bad" | "muted" | string;

export interface Runtime {
  state: string;
  detail: string;
  error_code: string;
  updated_at: string;
  worker_id: string;
}

export interface PairingRow {
  platform: string;
  user_id: string;
  chat_id: string;
  status: string;
  /** 仅 pending 有意义：剩余秒数 */
  expires_in: number;
  updated_at: string;
}

export interface ChannelEvent {
  direction: string;
  chat_id_hash: string;
  message_id: string;
  status: string;
  error_code: string;
  created_at: string;
}

export interface WeixinLoginState {
  logged_in: boolean;
  account_id: string;
  user_id: string;
  saved_at: number;
  updated_at: string;
}

/** 扫码登录的实时视图（`/login/start` 与 `/login/status` 同一个形状）。 */
export interface WeixinLoginView {
  status: string;
  message: string;
  qrcode?: string;
  qr_url?: string;
  /** 后端把二维码渲染成 SVG data URI，直接塞进 <img src> */
  qr_svg: string;
  account_id: string;
  base_url?: string;
  expires_in: number;
  logged_in?: boolean;
}

export interface ChannelView {
  id: string;
  name: string;
  vendor: string;
  tagline: string;
  description: string;
  docs_url: string;
  priority: number;

  capabilities: Capabilities;

  enabled: boolean;
  configured: boolean;
  /** 缺哪些凭据（人话，用来拼提示条） */
  missing: string[];
  callback_url: string;
  needs_public_url: boolean;

  env: EnvField[];
  options: OptionField[];

  bot_id: string;
  /** 这个渠道属于哪个实例（'' = 默认那份共享凭据） */
  instance_id?: string;
  dm_policy: string;
  group_policy: string;
  allowed_users: string[];
  dm_policies: string[];
  group_policies: string[];

  state: string;
  state_label: string;
  tone: StateTone;
  reason: string;

  runtime: Runtime;
  setup_steps: string[];
  gateway_alive: boolean;

  login?: WeixinLoginState;

  /** 只有详情接口（GET /channels/{id}）带这三块 */
  pairing?: { pending: PairingRow[]; approved: PairingRow[] };
  backlog?: { inbox: number; outbox: number };
  events?: ChannelEvent[];
}

export interface ChannelListPayload {
  channels: ChannelView[];
  gateway_alive: boolean;
  public_base_url: string;
  /** 部署级总开关 MESSAGING_ENABLED */
  enabled: boolean;
  /** 后端回显当前过滤的实例，前端据此丢弃迟到的旧实例响应 */
  instance_id?: string;
}

/* ---------------- 机器人实例 ---------------- */

/** 实例里单个渠道的状态摘要（不含凭据明细，够画 tab 和渠道数就够）。 */
export interface InstanceChannelState {
  id: string;
  name: string;
  enabled: boolean;
  configured: boolean;
  missing: string[];
  state: string;
  state_label: string;
  tone: StateTone;
}

/**
 * 一个实例 = 一个机器人 = 一套 IM 应用。
 * `state`/`state_label`/`tone` 由后端按同一套词表算好，前端不重推。
 */
export interface MessagingInstance {
  instance_id: string;
  /** 默认实例：存量那一份，凭据读 .env 基础键，可以绑任意机器人 */
  builtin: boolean;
  name: string;
  handle: string;
  /** 机器人被删了但配置还在：要看得见、能收拾 */
  bot_missing: boolean;
  env_suffix: string;
  channels: InstanceChannelState[];
  enabled_count: number;
  configured_count: number;
  state: string;
  state_label: string;
  tone: StateTone;
}

export interface InstanceListPayload {
  instances: MessagingInstance[];
  gateway_alive: boolean;
  enabled: boolean;
  public_base_url: string;
}

/** 建实例后要补的环境变量名：当场告诉用户，别让他去猜后缀。 */
export interface EnvRequirement {
  channel: string;
  channel_name: string;
  env: string;
  label: string;
}

export interface CreateInstanceResult {
  ok: boolean;
  instance: MessagingInstance;
  channels: string[];
  env_required: EnvRequirement[];
}

/** PUT 的载荷，字段名就是后端 `ChannelPatch`。 */
export interface ChannelPatch {
  enabled?: boolean;
  bot_id?: string;
  dm_policy?: string;
  group_policy?: string;
  allowed_users?: string[];
  options?: Record<string, string>;
}

/** 表单状态。白名单存原始文本，提交时才切——否则用户打到一半的逗号会被吃掉。 */
export interface ChannelForm {
  bot_id: string;
  dm_policy: string;
  group_policy: string;
  allowed_users: string;
  options: Record<string, string>;
}

export interface BotOption {
  bot_id: string;
  name: string;
  handle?: string;
  status?: string;
}

/* ---------------- 会话身份 ---------------- */

/**
 * 一段 IM 会话（一个群或一段私聊）现在由谁回话。
 *
 * `bot_id` 是**覆盖值**（`/bind` 指定的），空串表示跟随渠道；
 * `effective_bot_id` 才是真正回话的那个。两者都由后端算好并给 `bot_source`，
 * 前端不自己推导——否则卡片、tab、顶栏会各算一套，出现三种答案。
 */
export interface SessionRow {
  platform: string;
  instance: string;
  chat_id: string;
  chat_type: string;
  /** 会话级覆盖；空串 = 跟随渠道默认 */
  bot_id: string;
  effective_bot_id: string;
  effective_bot_name: string;
  /** 覆盖指向的机器人已被删除：名字只能显示 id，要提示收拾 */
  effective_bot_missing: boolean;
  /** session = 这段会话被单独指定过；channel = 跟随渠道 */
  bot_source: "session" | "channel" | string;
  generation: number;
  updated_at: string;
}

export interface SessionListPayload {
  sessions: SessionRow[];
  bots: BotOption[];
  /** 这个渠道当前默认绑的是谁，给「跟随渠道」那一项做文案 */
  channel_bot_id: string;
}
