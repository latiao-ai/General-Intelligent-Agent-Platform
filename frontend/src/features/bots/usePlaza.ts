/**
 * Bot 广场的"我这边的状态"。
 *
 * 只做两件事：查我投过哪些稿（按 `source_bot_id` 索引，侧栏用它显示
 * 「已上架 / 审核中」），以及拿分类下拉的选项。
 *
 * 403 / 503 有特殊含义：广场没开或没迁移。这时侧栏应该明确写"未开放"，
 * 而不是显示一个点了会报错的发布按钮。其它错误则保持可用——网络抖一下
 * 不代表功能关了。
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { getMyPlazaItems, getPlazaCategories } from "./api";
import type { CategoryOption, PlazaItem } from "./types";

/** 与后端 `bot/plaza.py` 保持一致；接口读不到时用它兜底，别让下拉框空着。 */
const FALLBACK_CATEGORIES: CategoryOption[] = [
  { id: "writing", name: "写作" },
  { id: "coding", name: "研发" },
  { id: "research", name: "研究" },
  { id: "data", name: "数据" },
  { id: "ops", name: "运维" },
  { id: "marketing", name: "市场" },
  { id: "product", name: "产品" },
  { id: "general", name: "通用" },
];

export function usePlaza() {
  const [byBot, setByBot] = useState<Record<string, PlazaItem>>({});
  /** null = 还在查；false = 明确未开放 */
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [categories, setCategories] = useState<CategoryOption[]>([]);

  const reload = useCallback(async () => {
    try {
      const data = await getMyPlazaItems();
      const map: Record<string, PlazaItem> = {};
      for (const item of data.items ?? []) {
        if (item.source_bot_id) map[item.source_bot_id] = item;
      }
      setByBot(map);
      setEnabled(true);
    } catch (err) {
      setByBot({});
      const status = err instanceof ApiError ? err.status : 0;
      setEnabled(!(status === 403 || status === 503));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    let alive = true;
    getPlazaCategories()
      .then((data) => {
        if (!alive) return;
        const list = (data.categories ?? []).map((item) => ({
          id: item.id,
          name: item.name,
        }));
        if (list.length) setCategories(list);
      })
      .catch(() => {
        /* 下面用兜底 */
      });
    return () => {
      alive = false;
    };
  }, []);

  return {
    byBot,
    enabled,
    categories: categories.length ? categories : FALLBACK_CATEGORIES,
    reload,
  };
}
