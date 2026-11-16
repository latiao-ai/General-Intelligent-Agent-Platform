/**
 * 数字、金额、时间的显示口径。
 *
 * 逐条搬自 admin.html:1812-1901。这些格式化函数的取舍是业务判断，不是工具函数：
 * 比如 `money()` 在小于一分钱时故意多给两位小数，否则看板上会把「花了 0.003 元」
 * 显示成 ¥0.00，读起来像没花钱。
 */

export function num(value: unknown): string {
  return Number(value || 0).toLocaleString("zh-CN");
}

export function compact(value: unknown): string {
  const n = Number(value || 0);
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}k`;
  return num(n);
}

/** 后端给的是「百万分之一元」。 */
export function money(micros: unknown): string {
  const yuan = Number(micros || 0) / 1e6;
  if (yuan === 0) return "¥0";
  if (yuan < 0.01) return `¥${yuan.toFixed(4)}`;
  if (yuan < 1) return `¥${yuan.toFixed(3)}`;
  return `¥${yuan.toFixed(2)}`;
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** 相对时间。列表里读「多久没来了」比读绝对时间快。 */
export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 19).replace("T", " ");
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${d.getMonth() + 1}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** 绝对时间。审计要的是「哪一刻」，「3 小时前」没法拿去和日志对。 */
export function shortTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 19).replace("T", " ");
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

export function fmtPoints(value: unknown): string {
  return Number(value || 0).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** 秒数 → 「3 天 4 小时」。日志页的进程时长用它，不显示 284571 秒。 */
export function logDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${s % 60} 秒`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时 ${m % 60} 分`;
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`;
}

/**
 * 角色对外的中文名。
 *
 * 库里存的是 member / platform_admin（存量还有 operator）。界面只说人话，
 * 写回后端的永远是原值——所以这个函数只用于显示，不要拿它的结果去提交。
 * 认不出来的角色原样返回，免得把历史角色吞成「普通用户」看起来像权限被改了。
 */
export const ROLE_LABELS: Record<string, string> = {
  member: "普通用户",
  platform_admin: "超级管理员",
  operator: "运营（历史角色）",
};

export function roleLabel(role: string | null | undefined): string {
  const key = String(role ?? "").trim();
  if (!key) return "—";
  return ROLE_LABELS[key] ?? key;
}

export const auditLabel: Record<string, string> = {
  console_user_role: "角色变更",
  console_user_status: "账号状态",
  console_user_delete: "删除账号",
  console_policy_toggle: "工具政策",
  console_platform_create: "新增登录平台",
  console_platform_update: "修改登录平台",
  console_platform_delete: "删除登录平台",
  admin_user_status: "账号状态（旧）",
};

export interface AuditEntry {
  type?: string;
  user_id?: string;
  policy_id?: string;
  tool_key?: string;
  created_at?: string;
  operator_id?: string;
  role?: string;
  reason?: string;
  status?: string;
  enabled?: boolean;
  previous_status?: string;
  previous?: { role?: string }[];
  retained_accounts?: number;
  retained?: { sessions?: number; messages?: number };
  display_name?: string;
  platform_id?: string;
  base_url?: string;
  fields?: string[];
  after?: { enabled?: boolean };
}

/** 审计条目只有 type，具体发生了什么得按类型拼。 */
export function auditDetail(e: AuditEntry): string {
  if (e.type === "console_platform_delete") {
    return `删除（保留 ${num(e.retained_accounts)} 个已绑定账号）`;
  }
  if (e.type === "console_platform_create") {
    return `${e.display_name || e.platform_id} · ${e.base_url || ""}`;
  }
  if (e.type === "console_platform_update") {
    const fields = (e.fields || []).join("/");
    const tail = e.after?.enabled === undefined ? "" : e.after.enabled ? "（启用）" : "（停用）";
    return `${fields || "未改字段"}${tail}`;
  }
  if (e.type === "console_user_delete") {
    const kept = e.retained || {};
    return `删除（保留 ${num(kept.sessions)} 会话 / ${num(kept.messages)} 消息）`;
  }
  if (e.type === "console_user_role") {
    const before = (e.previous || []).map((p) => p.role).filter(Boolean);
    const from = [...new Set(before)].map(roleLabel).join("/") || "—";
    return `${from} → ${roleLabel(e.role)}${e.reason ? `（${e.reason}）` : ""}`;
  }
  if (e.previous_status !== undefined) return `${e.previous_status} → ${e.status}`;
  if (e.enabled !== undefined) return e.enabled ? "恢复" : "暂停";
  return "";
}
