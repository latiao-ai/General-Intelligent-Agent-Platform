/**
 * Bot 广场的数据契约。
 *
 * 全部字段来自 `/api/bot-plaza*` 的真实返回，对照
 * `src/webpage_agent/api/web/plaza.html` 的使用方式逐个核对过。
 * 只声明页面真正读到的字段——多声明一个字段，就等于替后端许下一个
 * 它没有承诺过的约定。
 */

export interface BotItem {
  item_id: string;
  name: string;
  handle?: string | null;
  category?: string | null;
  title?: string | null;
  description?: string | null;
  tags?: string[] | null;
  /** 头像：有 image 用图，否则用 icon 字符 + color 底色 */
  image?: string | null;
  icon?: string | null;
  color?: string | null;
  shape?: "round" | "sq" | string | null;
  install_count?: number | null;
  installed?: boolean | null;
  featured?: boolean | null;
  version?: number | null;
}

/** 详情比列表多出这几个字段，只有打开抽屉时才拿得到。 */
export interface BotDetail extends BotItem {
  readme?: string | null;
  starters?: string[] | null;
  soul?: string | null;
  skill_ids?: string[] | null;
  connector_ids?: string[] | null;
  model?: string | null;
}

export interface BotCategory {
  id: string;
  name: string;
  count?: number | null;
}

export interface PlazaListResponse {
  items?: BotItem[] | null;
  categories?: BotCategory[] | null;
  total?: number | null;
  installed_count?: number | null;
}

export type PlazaOrder = "featured" | "popular" | "new";

export interface InstallResponse {
  bot_id: string;
  name?: string | null;
  installed?: boolean | null;
  /** 服务端给的目标地址；缺失时按 bot_id 自行拼 /api/ui/bots?bot= */
  chat_url?: string | null;
}

export type PublishStatus = "pending" | "published" | "rejected" | "offline" | "draft";

export interface MineSubmission {
  item_id: string;
  name: string;
  status: PublishStatus;
  scope?: "public" | "tenant" | string | null;
  install_count?: number | null;
  version?: number | null;
  review_note?: string | null;
  source_bot_id?: string | null;
}

export interface MineInstall {
  item_id: string;
  bot_id: string;
  name?: string | null;
  icon?: string | null;
  color?: string | null;
  item_version?: number | null;
  created_at?: string | null;
}

export interface MineResponse {
  items?: MineSubmission[] | null;
  installs?: MineInstall[] | null;
}
