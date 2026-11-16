/**
 * 会话资源选择弹层：专家 · 技能。
 *
 * 和输入框里的 `＋ / @` 是**两层配置**，界面上必须说清楚，否则用户会以为
 * 自己只是"给这条消息加了个技能"：
 *  - 这里选的东西写进会话（`sessions.addon_selection`），本轮及以后每一轮都生效；
 *  - 输入框引用条里的只作用于当前这一条消息，发完就清空。
 *
 * 专家不是执行引擎，是「系统提示 + 预设技能」（见后端 `addons/experts.py`）。
 * 所以选中专家后技能清单会被预设覆盖；用户再单独勾技能就是在预设上做加减，
 * 「恢复专家预设」把这份加减撤掉（发 `skill_ids: null`，不是 `[]`）。
 */
import { useEffect, useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { useToast } from "@/components/ui/Toast";
import { addonsApi } from "./api";
import { ExpertForm } from "./ExpertForm";
import { Icon } from "./icons";
import type { AddonSkill, ExpertItem } from "./types";
import type { AddonsView } from "./useAddons";

type Tab = "expert" | "skill";

export function AddonPicker({
  open,
  onClose,
  view,
  catalog,
  experts,
  catalogLoading,
  saving,
  onExpert,
  onToggleSkill,
  onResetSkills,
  onGoSkills,
  onCatalogChanged,
}: {
  open: boolean;
  onClose: () => void;
  view: AddonsView;
  catalog: AddonSkill[];
  experts: ExpertItem[];
  catalogLoading: boolean;
  saving: boolean;
  onExpert: (id: string) => void;
  onToggleSkill: (id: string, wanted: boolean) => void;
  onResetSkills: () => void;
  onGoSkills: () => void;
  /** 新建/删除专家后让上层重拉目录 */
  onCatalogChanged: () => void;
}) {
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("expert");
  const [query, setQuery] = useState("");
  /** `undefined` = 看列表；`null` = 新建；对象 = 查看/编辑 */
  const [editing, setEditing] = useState<ExpertItem | null | undefined>(undefined);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [removing, setRemoving] = useState(false);

  /* 每次打开都回到列表、清掉上次的搜索词和半截输入 */
  useEffect(() => {
    if (!open) return;
    setEditing(undefined);
    setQuery("");
    setConfirmDelete(null);
    setTab("expert");
  }, [open]);

  const selected = new Set(view.skills.map((item) => item.id));
  const skills = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return catalog;
    return catalog.filter(
      (item) =>
        String(item.name ?? item.id).toLowerCase().includes(q) ||
        String(item.description ?? "").toLowerCase().includes(q),
    );
  }, [catalog, query]);

  const removeExpert = async (id: string) => {
    setRemoving(true);
    try {
      await addonsApi.removeExpert(id);
      toast("已删除专家", "ok");
      setConfirmDelete(null);
      if (view.expert?.id === id) onExpert("");
      onCatalogChanged();
    } catch (err) {
      toast("删除失败：" + errorMessage(err), "error");
    } finally {
      setRemoving(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={
        editing === undefined
          ? "会话资源"
          : editing === null
            ? "新建专家"
            : editing.builtin
              ? "内置专家"
              : "编辑专家"
      }
      description={editing === undefined ? "对这个会话生效" : undefined}
      className="max-w-[580px]"
    >
      {editing !== undefined ? (
        <ExpertForm
          initial={editing}
          catalog={catalog}
          onCancel={() => setEditing(undefined)}
          onSaved={(expert) => {
            toast(editing?.id ? "专家已更新" : "专家已创建", "ok");
            setEditing(undefined);
            onCatalogChanged();
            // 刚建好的专家直接选上：新建的目的就是"用它"
            if (!editing?.id) onExpert(String(expert.id ?? ""));
          }}
        />
      ) : (
        <div className="flex flex-col gap-3">
          {/* tab */}
          <div className="flex items-center gap-1 border-b border-line pb-2">
            {(
              [
                ["expert", "专家"],
                ["skill", "技能"],
              ] as [Tab, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={tab === key}
                onClick={() => setTab(key)}
                className={cn(
                  "h-[26px] rounded-md px-2.5 text-label transition-colors duration-150",
                  tab === key ? "bg-sunken text-ink" : "text-muted hover:text-ink",
                )}
              >
                {label}
                {key === "skill" && view.skills.length ? (
                  <span className="ml-1 text-muted">{view.skills.length}</span>
                ) : null}
              </button>
            ))}
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto"
              onClick={() => (tab === "expert" ? setEditing(null) : onGoSkills())}
            >
              {tab === "expert" ? "新建专家" : "装技能"}
            </Button>
          </div>

          {tab === "expert" ? (
            <div className="flex flex-col gap-1.5">
              <button
                type="button"
                onClick={() => onExpert("")}
                className={cn(
                  "flex h-[30px] w-full items-center gap-2 rounded-md border px-2 text-left text-label transition-colors duration-150",
                  view.expert
                    ? "border-line text-muted hover:bg-sunken"
                    : "border-signal bg-signal/[0.06] text-ink",
                )}
              >
                <Icon name="dots" size={13} className="shrink-0" />
                <span className="flex-1">不用专家（只按技能与会话上下文）</span>
                {view.expert ? null : <Icon name="check" size={12} className="text-signal" />}
              </button>

              {catalogLoading && !experts.length ? (
                <p className="py-6 text-center text-label text-muted">读取中…</p>
              ) : null}

              {experts.map((expert) => {
                const on = view.expert?.id === expert.id;
                const builtin = !!expert.builtin;
                return (
                  <div
                    key={expert.id}
                    className={cn(
                      "relative rounded-lg border px-2.5 py-2 transition-colors duration-150",
                      on ? "border-signal bg-signal/[0.06]" : "border-line hover:border-line-strong",
                    )}
                  >
                    {/* 整行可点：覆盖层 + 内容 pointer-events-none，避免 button 套 button */}
                    <button
                      type="button"
                      onClick={() => onExpert(expert.id)}
                      disabled={saving}
                      aria-label={`使用专家 ${expert.name || expert.id}`}
                      aria-pressed={on}
                      className="absolute inset-0 rounded-lg focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
                    />
                    <div className="pointer-events-none flex items-start gap-2">
                      <span className="mt-[1px] text-[17px] leading-[1.2]" aria-hidden="true">
                        {expert.icon || "🧩"}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-baseline gap-1.5">
                          <span className="truncate text-secondary font-medium text-ink">
                            {expert.name || expert.id}
                          </span>
                          <span className="shrink-0 text-label text-muted">
                            {expert.tag || (builtin ? "内置" : "自定义")}
                          </span>
                          {on ? (
                            <span className="shrink-0 text-label text-signal">当前</span>
                          ) : null}
                        </span>
                        {expert.description ? (
                          <span className="mt-0.5 line-clamp-2 block text-label leading-[1.55] text-muted">
                            {expert.description}
                          </span>
                        ) : null}
                        <span className="mt-0.5 block text-label text-muted">
                          {expert.skill_ids?.length
                            ? `预设 ${expert.skill_ids.length} 个技能`
                            : "无预设技能"}
                        </span>
                      </span>
                    </div>

                    <div className="pointer-events-auto absolute top-1.5 right-1.5 flex items-center gap-0.5">
                      <button
                        type="button"
                        onClick={() => setEditing(expert)}
                        title={builtin ? "查看提示词" : "编辑"}
                        aria-label={builtin ? `查看 ${expert.name}` : `编辑 ${expert.name}`}
                        className="flex h-[22px] w-[22px] items-center justify-center rounded-[4px] text-muted hover:bg-sunken hover:text-ink"
                      >
                        <Icon name={builtin ? "doc" : "gear"} size={12} />
                      </button>
                      {builtin ? null : confirmDelete === expert.id ? (
                        <>
                          <button
                            type="button"
                            disabled={removing}
                            onClick={() => void removeExpert(expert.id)}
                            className="h-[22px] rounded-[4px] px-1.5 text-label text-danger hover:bg-danger-wash"
                          >
                            确认删除
                          </button>
                          <button
                            type="button"
                            onClick={() => setConfirmDelete(null)}
                            className="h-[22px] rounded-[4px] px-1.5 text-label text-muted hover:bg-sunken"
                          >
                            取消
                          </button>
                        </>
                      ) : (
                        <button
                          type="button"
                          onClick={() => setConfirmDelete(expert.id)}
                          title="删除"
                          aria-label={`删除 ${expert.name}`}
                          className="flex h-[22px] w-[22px] items-center justify-center rounded-[4px] text-muted hover:bg-danger-wash hover:text-danger"
                        >
                          <Icon name="close" size={11} />
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜技能"
                  className="h-[26px] min-w-0 flex-1 rounded-md border border-line bg-paper px-2 text-label text-ink outline-none focus:border-signal"
                />
                {view.explicit ? (
                  <button
                    type="button"
                    onClick={onResetSkills}
                    disabled={saving}
                    className="shrink-0 text-label text-signal hover:underline disabled:opacity-50"
                  >
                    恢复专家预设
                  </button>
                ) : (
                  <span className="shrink-0 text-label text-muted">
                    {view.expert ? "跟随专家预设" : "未选技能"}
                  </span>
                )}
              </div>

              <div className="max-h-[min(320px,46vh)] overflow-y-auto rounded-md border border-line">
                {catalogLoading && !catalog.length ? (
                  <p className="px-2 py-6 text-center text-label text-muted">读取中…</p>
                ) : skills.length === 0 ? (
                  <p className="px-2 py-6 text-center text-label text-muted">
                    {catalog.length
                      ? "没有匹配的技能"
                      : "还没有装技能。点右上角「装技能」去技能中心，或上传一个带 SKILL.md 的技能包。"}
                  </p>
                ) : (
                  skills.map((skill) => {
                    const on = selected.has(skill.id);
                    return (
                      <button
                        key={skill.id}
                        type="button"
                        onClick={() => onToggleSkill(skill.id, !on)}
                        disabled={saving}
                        className={cn(
                          "flex w-full items-start gap-2 border-b border-line px-2 py-1.5 text-left transition-colors duration-150 last:border-b-0",
                          "hover:bg-sunken disabled:cursor-wait",
                          on && "bg-signal/[0.06]",
                        )}
                      >
                        <span
                          className={cn(
                            "mt-[2px] flex h-[13px] w-[13px] shrink-0 items-center justify-center rounded-[3px] border",
                            on ? "border-signal bg-signal text-paper" : "border-line-strong",
                          )}
                          aria-hidden="true"
                        >
                          {on ? <Icon name="check" size={9} strokeWidth={2.6} /> : null}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-secondary text-ink">
                            {skill.name || skill.id}
                          </span>
                          {skill.description ? (
                            <span className="line-clamp-2 block text-label leading-[1.55] text-muted">
                              {skill.description}
                            </span>
                          ) : null}
                        </span>
                      </button>
                    );
                  })
                )}
              </div>
            </div>
          )}

          <p className="text-label leading-[1.6] text-muted">
            会话级选择：影响这个会话之后的每一轮。只想给**这一条**消息加点东西，用输入框的
            <span className="mx-1 rounded-[3px] bg-sunken px-1">＋</span>
            或
            <span className="mx-1 rounded-[3px] bg-sunken px-1">@</span>。
          </p>
        </div>
      )}
    </Dialog>
  );
}
