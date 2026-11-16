/**
 * 主界面（集成栈 / `data-page="hub"`）的数据形状。
 *
 * 字段名一律照抄后端，不做 camelCase 转换：这些结构横跨
 * `/api/sessions/**`、`/api/tasks/**`、`/api/models`，改名只会让
 * "界面上少一个字段"这种问题多花半小时去对。
 */

/** 侧栏会话行（`GET /api/sessions`）。 */
export interface SessionRow {
  session_id: string;
  title: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}

/** 会话里的一条消息（`timeline.messages`）。 */
export interface HubMessage {
  role: "user" | "assistant" | string;
  content: string;
  /** 结构化内容块（技能胶囊）。旧页只对 user 角色回显成胶囊。 */
  content_blocks?: ResourceBlock[];
}

export interface ResourceBlock {
  type?: string;
  [key: string]: unknown;
}

/** 时间线里的一轮任务（历史回放用的执行过程）。 */
export interface TaskTurn {
  task_id: string;
  run_id: string;
  message: string;
  /** 后端派生好的展示标签（"已回答"/"已完成"…），不要再自己翻译状态码 */
  status: string;
  run_status: string;
  created_at: string;
  artifact: ArtifactView | null;
  live: boolean;
  asked_questions: string[];
  clarification: string;
  events: TaskEvent[];
}

export interface ArtifactView {
  kind: string;
  filename: string;
  size_bytes?: number | null;
  exists: boolean;
}

/** 执行过程的单条事件。字段随 type 变，所以宽进宽出。 */
export interface TaskEvent {
  type: string;
  stage?: string;
  label?: string;
  status?: string;
  seconds?: number;
  at?: number;
  text?: string;
  detail?: string;
  icon?: string;
  report?: DraftReport | null;
  [key: string]: unknown;
}

export interface DraftReport {
  title?: string;
  summary?: string;
  sections?: { heading?: string; paragraphs?: { text?: string }[] }[];
}

/** 会话聊天 turn（`timeline.chat_turns`）。 */
export interface ChatTurn {
  turn_id: string;
  turn_seq?: number;
  kind?: string;
  status: string;
  message?: string;
  answer?: string;
  /** 逐字草稿，轮询兜底时用它覆盖气泡文本 */
  draft?: string;
  target_task_id?: string;
  target_run_id?: string;
  created_at?: string;
}

export interface Timeline {
  session_id: string;
  messages: HubMessage[];
  live_task_id: string;
  live_run_id: string;
  live_after_event_id: number;
  turns: TaskTurn[];
  chat_turns: ChatTurn[];
  active_runs: {
    task_id: string;
    run_id: string;
    message: string;
    run_status: string;
    status: string;
    created_at: string;
  }[];
}

/** `POST /api/sessions/{id}/messages` 的 202 回执。 */
export interface AcceptedMessage {
  turn_id: string;
  message_id?: string;
  status: string;
  /** chat / query / control / ambiguous */
  kind: string;
  target_task_id?: string;
  target_run_id?: string;
}

/** turn SSE 的事件体。 */
export interface TurnEvent {
  type?: string;
  text?: string;
  name?: string;
  args?: Record<string, unknown>;
  detail?: Record<string, string> | string;
  urls?: string[];
  make_visible?: boolean;
  answer?: string;
  status?: string;
  at?: number;
}

export interface ModelRow {
  id: string;
  label?: string;
  name?: string;
  current?: boolean;
  available?: boolean;
}

export interface Me {
  user_id?: string;
  display_name?: string;
  role?: string;
  tenant_id?: string;
  authenticated?: boolean;
  anonymous?: boolean;
  /** off = 本地演示模式，没有登录这回事 */
  auth_mode?: string;
}

export interface CreditsBalance {
  balance?: number;
  currency?: string;
  [key: string]: unknown;
}

/* ------------------------------------------------------------------------ *
 * 定时任务（`/api/schedules/*`）。字段名照后端 `_view()`，不做驼峰转换——
 * 转换层是 bug 的温床，而这边的类型本来就只为这一份数据服务。
 * ------------------------------------------------------------------------ */

export type ScheduleKind = "recurring" | "once";

export interface ScheduleDelivery {
  channel?: string;
  /** 后端脱敏过（带 key 的地址不回显），所以"原样提交"永远不成立 */
  webhook_url?: string;
  events?: string[] | null;
}

export interface ScheduleItem {
  schedule_id: string;
  name: string;
  prompt: string;
  enabled: boolean;
  type: ScheduleKind;
  rrule?: string;
  /** 人话版规则（后端 `rrules.describe` 生成），不要自己再翻译一遍 */
  schedule_desc?: string;
  scheduled_at?: string | null;
  valid_from?: string | null;
  valid_until?: string | null;
  /** 秒级时间戳，不是 ISO 字符串 */
  next_run_at?: number | null;
  last_run_at?: number | null;
  last_status?: string;
  last_error?: string;
  consecutive_failures?: number;
  connector_ids?: string[];
  delivery?: ScheduleDelivery;
  bot_id?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ScheduleRun {
  run_id?: string;
  status: string;
  due_at?: number | null;
  finished_at?: number | null;
  delivery_status?: string;
  delivery_attempts?: number;
  summary?: string;
  error_code?: string;
  error_message?: string;
}

/* ------------------------------------------------------------------------ *
 * 会话级选择（`/api/sessions/{id}/addons` + `/api/experts`）
 *
 * 「会话是选择单元，不是消息」：这里勾的专家与技能写进 `sessions.addon_selection`，
 * 下一轮由服务端解析成「专家人设 + 技能目录」。消息里的 `@` 引用只是这一轮的
 * 临时加点，两者叠加生效，不是同一层配置。
 * ------------------------------------------------------------------------ */

export interface ExpertItem {
  id: string;
  name?: string;
  description?: string;
  /** emoji 或内置图标名 */
  icon?: string;
  color?: string;
  tag?: string;
  /** 内置专家只读，改不了也删不掉 */
  builtin?: boolean;
  editable?: boolean;
  skill_ids?: string[];
  connector_ids?: string[];
  model?: string;
  /** 只有 `/api/experts` 的目录行带；会话视图 `_view()` 刻意不回（装配细节） */
  system_prompt?: string;
}

/** 技能的最小视图：解析层只回这几个字段，够画芯片和列表。 */
export interface AddonSkill {
  id: string;
  name?: string;
  description?: string;
  enabled?: boolean;
  source?: string;
  dir?: string;
}

/**
 * 会话选择的服务端视图。
 *
 * `skills` 是**实际生效**的清单（专家预设 + 显式勾选，且已过滤掉没装/被禁用的），
 * `requested_skill_ids` 是用户显式勾的那部分，`skills_explicit` 说明
 * 「用户动过」还是「跟着专家预设走」——恢复预设就是把它变回 false。
 */
export interface SessionAddons {
  session_id?: string;
  addon_selection?: Record<string, unknown>;
  expert_id?: string;
  expert?: ExpertItem | null;
  /** 会话选的专家已被删除：此时人设静默失效，必须提示 */
  expert_missing?: boolean;
  skill_ids?: string[];
  requested_skill_ids?: string[];
  missing_skill_ids?: string[];
  skills?: AddonSkill[];
  skills_explicit?: boolean;
  connector_ids?: string[] | null;
  connectors_explicit?: boolean;
  connectors_decided?: boolean;
  model?: string;
  notice?: string;
}

/**
 * PATCH 语义（三种不能混）：
 *  - 不传某键 = 沿用（专家预设）；
 *  - 传 `[]`   = 明确不要；
 *  - 传 `null` = 删掉显式键，回落到专家预设。
 */
export interface AddonsPatch {
  expert_id?: string | null;
  skill_ids?: string[] | null;
  connector_ids?: string[] | null;
  model?: string | null;
}

/** 保存时提交的载荷：`update` 走 PATCH，只带变化的字段也合法。 */
export interface SchedulePayload {
  name: string;
  prompt: string;
  type: ScheduleKind;
  rrule?: string;
  scheduled_at?: string;
  valid_until?: string;
  enabled?: boolean;
  delivery?: ScheduleDelivery;
}
