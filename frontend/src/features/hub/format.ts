/**
 * 展示词汇。
 *
 * 后端返回的 `status` 已经是派生好的展示标签，这里只处理两类情况：
 *  1. 任务轮（`turn.status`）的短标签——旧页有一张 22 项的映射表；
 *  2. 工具名 → 中文动作名，以及耗时/时间的格式化。
 *
 * 不在组件里散写这些字符串：旧页同一句"已完成"出现在 6 个地方，其中 2 处
 * 拼成了"完成"。
 */

export const TURN_STATUS: Record<string, string> = {
  answered: "已回答",
  cancelled: "已中断",
  interrupted: "已中断",
  failed: "已失败",
  deployed: "已部署",
  local_ready: "已完成",
  deploy_needs_check: "已完成",
  // 计划没过校验就停下等人补充：不能显示成"已完成"
  waiting_input: "待补充信息",
  waiting_approval: "待确认",
  partially_succeeded: "部分完成",
  // 补偿运行：副作用已收回，与"失败"不是一回事
  compensated: "已撤销",
  // 通用状态兜底
  succeeded: "已完成",
  needs_reconcile: "待核实",
  queued: "排队中",
  running: "执行中",
  reconciling: "核对中",
  paused: "已暂停",
};

/** 目标还没结束：下一句是补充，不能另开任务。 */
export const GOAL_OPEN: Record<string, boolean> = {
  waiting_input: true,
  waiting_approval: true,
  paused: true,
  needs_reconcile: true,
  interrupted: true,
};

/** turn 的终态：Worker 已执行完、答复已落库，可以停止轮询。 */
export const CHAT_TURN_DONE = ["succeeded", "failed", "waiting_clarification", "cancelled"];

export function turnStatusLabel(status: string) {
  return TURN_STATUS[status] || "已完成";
}

/** 侧栏里的时间：`08-13 14:05`。没有年份——列表里都是最近的会话。 */
export function shortTime(iso: string) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 处理中那句"已处理 12s" / "已处理 1m5s"。 */
export function formatElapsed(ms: number) {
  const sec = Math.max(0, Math.round(ms / 1000));
  if (sec < 60) return `已处理 ${sec}s`;
  return `已处理 ${Math.floor(sec / 60)}m${sec % 60}s`;
}

/** 阶段耗时：两位小数，进行中也用同一个格式，数字不跳位。 */
export function seconds(value: number) {
  return `${Math.max(0, value).toFixed(2)}s`;
}

/**
 * 工具调用在"处理过程"里怎么念。
 *
 * 旧页只翻译了 `web_search`，其余把工具名原样贴出来——`mcp__libtv__generate`
 * 这种名字对用户没意义。这里给常见的几条中文名，其余保留原名（至少能对上号，
 * 也能拿去搜日志）。
 */
export function toolLabel(name: string) {
  const key = String(name || "").trim();
  if (!key) return "工具";
  if (key.includes("search")) return "网页搜索";
  if (key.includes("fetch") || key.includes("read_url") || key.includes("crawl")) return "读取网页";
  if (key.includes("image")) return "生成图片";
  if (key.includes("video")) return "生成视频";
  if (key.includes("deploy")) return "部署";
  return key;
}

/** 工具的参数里最值得显示的那一个（搜索词 / 地址）。 */
export function toolMeta(args?: Record<string, unknown>) {
  if (!args) return "";
  const hit = args.query ?? args.q ?? args.keyword ?? args.search ?? args.url ?? args.path;
  return hit ? String(hit) : "";
}
