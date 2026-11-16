/**
 * 控制台接口的契约。
 *
 * 字段名一律照抄后端（snake_case），不做驼峰转换：这些名字在审计日志、
 * 文档和排障对话里是同一个词，转换一次就多一份对不上的可能。
 */

export interface Me {
  user_id: string;
  tenant_id: string;
  role: string;
  scope: string;
  allowed: boolean;
  auth_mode?: string;
}

export interface TrendRow {
  day: string;
  messages: number;
  active_users: number;
  sessions: number;
  requests: number;
  charged_micros: number;
}

export interface Overview {
  days: number;
  totals: { total: number; active: number; disabled: number };
  trend: TrendRow[];
  tenants: { tenant_id: string; users: number }[];
}

export interface UserRow {
  user_id: string;
  display_name: string;
  email?: string;
  status: string;
  role: string;
  tenants: string[];
  platforms: string[];
  last_active_at?: string;
  last_active_source?: string;
  sessions: number;
  messages: number;
  tasks: number;
  failed_tasks: number;
  input_tokens: number;
  output_tokens: number;
  charged_micros: number;
}

export interface UserList {
  users: UserRow[];
  total: number;
}

export interface PurgePreview {
  sessions: number;
  messages: number;
  tasks: number;
  runs: number;
  artifacts: number;
  logins: number;
  total: number;
}

export interface Identity {
  identity_provider_id?: string;
  provider?: string;
  tenant_id: string;
  role: string;
  subject?: string;
  last_seen_at?: string;
}

export interface UserTask {
  task_id: string;
  status: string;
  message?: string;
  created_at: string;
}

export interface ActivityRow {
  day: string;
  messages: number;
  tasks: number;
}

export interface UserDetail {
  user: UserRow;
  identities: Identity[];
  tasks: UserTask[];
  activity: ActivityRow[];
  audit: import("./format").AuditEntry[];
  active_logins: number;
  last_login_at?: string;
  sessions: unknown[];
  purge_preview?: PurgePreview;
}

export interface CreditRow {
  user_id: string;
  display_name?: string;
  email?: string;
  tenant_id?: string;
  balance: number;
  used_window: number;
  calls_window: number;
  tokens_window: number;
  daily: { day: string; points: number }[];
  last_change_at?: string;
}

export interface CreditData {
  users: CreditRow[];
  total: number;
  tenants: string[];
}

export type Transport = "streamable_http" | "sse" | "stdio";

export interface PlatformConnector {
  id: string;
  name: string;
  icon?: string;
  description?: string;
  transport: Transport;
  url?: string;
  enabled: boolean;
  shadowed?: boolean;
  auth?: string | false;
  headers?: Record<string, string>;
  oauth_issuer?: string;
  oauth_scopes?: string[];
}

export interface ConnectorServer {
  id: string;
  name?: string;
  transport: Transport;
  url?: string;
  command?: string;
  auth?: boolean | string;
  source?: string;
  source_label?: string;
}

export interface TenantBinding {
  server_id: string;
  name?: string;
  transport: Transport;
  url?: string;
  command?: string;
  source?: string;
  source_label?: string;
  enabled?: boolean;
  credentialed?: boolean;
}

export interface ConnectorTenantGroup {
  tenant_id: string;
  bindings: TenantBinding[];
}

export interface ConnectorList {
  servers: ConnectorServer[];
  tenants: ConnectorTenantGroup[];
}

export interface PlatformSkill {
  id: string;
  name: string;
  icon?: string;
  category: string;
  version: string;
  description?: string;
  overview?: string;
  examples?: string[];
  prompt?: string;
  enabled: boolean;
  installed_by_default?: boolean;
  shadowed?: boolean;
}

export interface SkillList {
  items: PlatformSkill[];
  categories: string[];
}

export interface Policy {
  policy_id: string;
  tool_key?: string;
  role?: string;
  user_id?: string;
  effect: string;
  approval_mode: string;
  enabled: boolean;
}

export interface PolicyGroup {
  tenant_id: string;
  policies: Policy[];
}

export interface PlatformConfig {
  login_path?: string;
  username_field?: string;
  password_field?: string;
  subject_path?: string;
  display_name_path?: string;
  status_path?: string;
  role_path?: string;
  timeout_seconds?: number;
}

export interface LoginPlatform {
  platform_id: string;
  display_name?: string;
  base_url?: string;
  tenant_id: string;
  identity_provider_id: string;
  note?: string;
  is_default?: boolean;
  login_mode?: string;
  enabled: boolean;
  accounts: number;
  last_login_at?: string;
  config?: PlatformConfig;
}

export interface PlatformTestResult {
  ok: boolean;
  detail?: string;
  display_name?: string;
  subject?: string;
  role?: string;
  identity_provider_id?: string;
  elapsed_ms: number;
}

export interface PlazaCategory {
  id: string;
  name: string;
}

export interface PlazaItem {
  item_id: string;
  name: string;
  handle?: string;
  title?: string;
  description?: string;
  soul?: string;
  model?: string;
  icon?: string;
  category: string;
  tags?: string[];
  readme?: string;
  starters?: string[];
  scope?: string;
  status: string;
  featured?: boolean;
  position?: number;
  review_note?: string;
  source_bot_id?: string;
  publisher_tenant?: string;
  install_count: number;
  install_rows: number;
  updated_at?: string;
}

export interface PlazaData {
  items: PlazaItem[];
  counts: Record<string, number>;
  categories?: PlazaCategory[];
}

export interface LogEntry {
  ts: string;
  level: string;
  logger?: string;
  message?: string;
  trace?: string;
}

export interface LogStatus {
  process: { uptime_seconds: number; pid: number; started_at: string };
  http: {
    window_qps: number;
    total_since_start: number;
    window_requests: number;
    window_error_rate: number;
    window_errors: number;
    window_p95_ms: number;
    window_p50_ms: number;
    window_slow?: { route: string; count: number }[];
  };
  log: { level: string; file_enabled: boolean; size: number; capacity: number; dir: string };
  files: { name: string; size: number }[];
}

export interface AuthPlatformStatus {
  enabled: boolean;
  platforms: { platform_id: string; display_name?: string }[];
}
