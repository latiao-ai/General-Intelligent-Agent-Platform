/**
 * 会话级「专家 · 技能」选择（`sessions.addon_selection`）。
 *
 * 一句话：**会话是选择单元，不是消息**。这里选的东西写进会话配置，下一轮
 * 由服务端解析成「专家人设 + 技能目录」；输入框里的 `@` 只是给这一轮加点，
 * 两层叠加生效，互不替代。
 *
 * 三条语义（旧页踩过，一样不能改）：
 *  1. **新会话还没有会话行**，PATCH 必然 404。选择先攒在 `pending`，随第一条
 *     消息以 `addon_selection` 发出，服务端与 turn 同事务落库；
 *  2. **「没选」≠「选了空」**：恢复专家预设发 `skill_ids: null`（删掉显式键），
 *     不是发 `[]`（那是"明确一个都不要"）；
 *  3. **连接器不在这里管**。消息级连接器在输入框的引用条里（本轮生效），
 *     会话级连接器没有对应界面——所以永远不发 `connector_ids`，服务端沿用旧值。
 *     发一个猜出来的数组会把用户在别处配好的连接器整个换掉。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { addonsApi, skillApi } from "./api";
import type { AddonSkill, AddonsPatch, ExpertItem, SessionAddons } from "./types";

export interface AddonsView {
  expert: ExpertItem | null;
  /** 实际生效的技能（专家预设 + 显式勾选，且已过滤没装/被禁用的） */
  skills: AddonSkill[];
  /** 技能清单是不是用户显式勾出来的（false = 跟着专家预设走） */
  explicit: boolean;
}

function has(obj: AddonsPatch, key: keyof AddonsPatch): boolean {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

function findSkill(catalog: AddonSkill[], id: string): AddonSkill | null {
  return catalog.find((item) => item.id === id) ?? null;
}

/** 草稿态（还没有会话行）的视图：先用本地 pending + 专家预设凑出来。 */
function draftView(
  pending: AddonsPatch,
  experts: ExpertItem[],
  catalog: AddonSkill[],
): AddonsView {
  const expertId = has(pending, "expert_id") ? String(pending.expert_id ?? "") : "";
  const expert = experts.find((item) => item.id === expertId) ?? null;
  const preset = expert?.skill_ids ?? [];
  const ids = has(pending, "skill_ids") ? (pending.skill_ids ?? []) : preset;
  return {
    expert,
    skills: ids.map((id) => findSkill(catalog, id)).filter((item): item is AddonSkill => !!item),
    explicit: has(pending, "skill_ids"),
  };
}

export function useAddons(sessionId: string) {
  const toast = useToast();
  const [server, setServer] = useState<SessionAddons | null>(null);
  const [pending, setPending] = useState<AddonsPatch>({});
  const [catalog, setCatalog] = useState<AddonSkill[]>([]);
  const [experts, setExperts] = useState<ExpertItem[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  /** 会话选的专家已被删除：人设静默失效，必须让人看见 */
  const [expertMissing, setExpertMissing] = useState(false);

  /** 切会话时丢弃上一个会话的回包（慢网络下会串台） */
  const epochRef = useRef(0);

  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true);
    try {
      const [skillRes, expertRes] = await Promise.allSettled([
        skillApi.list(""),
        addonsApi.experts(),
      ]);
      if (skillRes.status === "fulfilled") {
        setCatalog(
          (skillRes.value.items ?? [])
            .filter((item) => item.on_disk && item.enabled !== false)
            .map((item) => ({
              id: item.id,
              name: item.name,
              description: item.description,
              enabled: item.enabled,
              source: item.source,
            })),
        );
      }
      if (expertRes.status === "fulfilled") {
        setExperts(expertRes.value.items ?? []);
      }
    } finally {
      setCatalogLoading(false);
    }
  }, []);

  const refresh = useCallback(async (id: string): Promise<boolean> => {
    if (!id) {
      setServer(null);
      setExpertMissing(false);
      return true;
    }
    const epoch = ++epochRef.current;
    try {
      const data = await addonsApi.get(id);
      if (epoch !== epochRef.current) return false;
      setServer(data);
      setExpertMissing(!!data.expert_missing);
      return true;
    } catch {
      // 404：会话刚建还没落库；401 之类会在别处统一处理。这里保留现有胶囊，
      // 不因为一次读失败就把界面清空——那会让人以为自己选的东西丢了。
      return false;
    }
  }, []);

  /* 开屏与切换会话：拉目录 + 服务端状态 */
  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  useEffect(() => {
    let alive = true;
    if (!sessionId) {
      setServer(null);
      setExpertMissing(false);
      // 回到新会话：草稿清空，从零开始攒
      setPending({});
      return;
    }
    /**
     * 先清掉上一个会话的视图，避免切会话时短暂看到别人的专家。
     * **pending 要等服务端真的读到了才清**：`send()` 是先定会话 id 再 POST，
     * 这个 effect 会在 POST 之前跑一次、GET 到 404。若此时就清 pending，
     * 刚选好的专家/技能会在发送瞬间消失，看起来像"没生效"。
     */
    setServer(null);
    void (async () => {
      const ok = await refresh(sessionId);
      if (alive && ok) setPending({});
    })();
    return () => {
      alive = false;
    };
  }, [sessionId, refresh]);

  /**
   * 视图来源：有服务端状态就用它，否则用本地草稿推导。
   * 不用 `sessionId ? server : draft` 判断——会话 id 刚生成、GET 还没回来那一拍
   * server 是 null，会闪一下空白。
   */
  const view: AddonsView = server
    ? {
        expert: server.expert ?? null,
        skills: server.skills ?? [],
        explicit: !!server.skills_explicit,
      }
    : draftView(pending, experts, catalog);

  /**
   * 写选择。
   *
   * 有会话行就 PATCH（服务端是唯一真相）；没有就攒进 pending。
   * `null` 一律当成「删掉这个显式键」，不做 `Object.assign` 保留 null——
   * 那样草稿视图会把 null 又当成 []，变成"一个都不要"。
   */
  const patch = useCallback(
    async (next: AddonsPatch, okText?: string) => {
      const merge = (prev: AddonsPatch): AddonsPatch => {
        const out: AddonsPatch = { ...prev };
        for (const key of Object.keys(next) as (keyof AddonsPatch)[]) {
          const value = next[key];
          if (value === null || value === undefined) delete out[key];
          else (out as Record<string, unknown>)[key] = value;
        }
        return out;
      };

      if (!sessionId) {
        setPending(merge);
        if (okText) toast(okText, "ok");
        return;
      }

      const epoch = ++epochRef.current;
      setSaving(true);
      try {
        const data = await addonsApi.patch(sessionId, next);
        if (epoch !== epochRef.current) return;
        setServer(data);
        setExpertMissing(!!data.expert_missing);
        if (data.notice) toast(data.notice, "error");
        else if (okText) toast(okText, "ok");
      } catch (err) {
        // 会话行还没落库（刚发出第一条消息的那一两秒）会 404，等一下再点就行
        if (err instanceof ApiError && err.status === 404) {
          toast("会话还在创建中，稍后再选一次", "error");
        } else {
          toast("保存选择失败：" + errorMessage(err), "error");
        }
      } finally {
        if (epoch === epochRef.current) setSaving(false);
      }
    },
    [sessionId, toast],
  );

  const setExpert = useCallback(
    (expertId: string) => {
      if (!expertId) {
        // 取消专家：**不动**显式勾选的技能。用户勾的东西是他自己定的，
        // 取消人设不该顺手把技能也抹掉。
        void patch({ expert_id: null }, "已取消专家");
        return;
      }
      // 选专家时清掉显式技能：专家自带预设，留着旧的显式清单会让预设完全失效
      void patch({ expert_id: expertId, skill_ids: null }, "已切换专家");
    },
    [patch],
  );

  const toggleSkill = useCallback(
    (skillId: string, wanted: boolean) => {
      const ids = view.skills.map((item) => item.id).filter((id) => id !== skillId);
      if (wanted) ids.push(skillId);
      void patch({ skill_ids: ids }, wanted ? "已加入当前会话" : "已从当前会话移除");
    },
    [patch, view.skills],
  );

  /** 恢复专家预设：发 null，不是 []。没有专家时等同于清空显式选择。 */
  const resetSkills = useCallback(() => {
    void patch({ skill_ids: null }, "已恢复专家预设");
  }, [patch]);

  /**
   * 发送时带出去的选择。
   *
   * 只在新会话（还没有会话行）时有值。返回一次快照后由调用方在会话 id 落地时
   * 丢弃——同一份草稿不能带进第二条消息，那时服务端已经自己记着了。
   */
  const pendingPatch = useCallback((): AddonsPatch | null => {
    const out: AddonsPatch = {};
    if (has(pending, "expert_id")) out.expert_id = pending.expert_id;
    if (has(pending, "skill_ids")) out.skill_ids = pending.skill_ids;
    return Object.keys(out).length ? out : null;
  }, [pending]);

  return {
    view,
    catalog,
    experts,
    catalogLoading,
    saving,
    expertMissing,
    pending: pendingPatch,
    resetSkills,
    setExpert,
    toggleSkill,
    refreshCatalog: () => void loadCatalog(),
  };
}
