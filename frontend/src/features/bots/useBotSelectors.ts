/**
 * 新建/编辑机器人时要用的三个下拉数据源：模型、技能、连接器。
 *
 * 三个请求并发，任何一个失败都降级成空列表——没有选择器也能建机器人，
 * 只是少了几个复选框。这条降级是刻意的：旧页在这里 catch 住不报错，
 * 因为"读不到技能列表"不该让整个弹窗打不开。
 */
import { useEffect, useState } from "react";
import { getConnectors, getModels, getSkills } from "./api";
import type { ConnectorItem, SkillItem } from "./types";

export function useBotSelectors() {
  const [models, setModels] = useState<string[]>([]);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [connectors, setConnectors] = useState<ConnectorItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    void Promise.all([
      getModels().catch(() => ({ models: [] })),
      getSkills().catch(() => ({ items: [] })),
      getConnectors().catch(() => ({ items: [] })),
    ]).then(([modelData, skillData, connectorData]) => {
      if (!alive) return;
      const ids = (modelData.models ?? [])
        .map((item) => (typeof item === "string" ? item : (item.id ?? "")))
        .filter(Boolean);
      setModels(ids);
      setSkills((skillData.items ?? []).filter((x) => x.kind !== "connector").slice(0, 300));
      setConnectors((connectorData.items ?? []).slice(0, 200));
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, []);

  return { models, skills, connectors, loading };
}
