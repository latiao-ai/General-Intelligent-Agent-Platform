/**
 * 新建 / 编辑自定义专家。
 *
 * 专家 = **系统提示 + 预设技能**，不是第二套执行引擎——这是后端的模型
 * （`addons/experts.py` 文件头），所以表单只收这几样，别在这里发明"专家类型"
 * 之类的概念。内置专家是只读目录，进来只展示、不给保存按钮。
 *
 * 预设技能用勾选而不是填 id：让用户手打 `skill-xxx` 是把内部标识漏到界面上，
 * 而且打错了会在保存后才以「这些技能没安装」的形式报出来。
 */
import { useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { addonsApi } from "./api";
import { Icon } from "./icons";
import type { AddonSkill, ExpertItem } from "./types";

export function ExpertForm({
  initial,
  catalog,
  onCancel,
  onSaved,
}: {
  /** 空 = 新建；有 id 且非 builtin = 编辑 */
  initial: ExpertItem | null;
  catalog: AddonSkill[];
  onCancel: () => void;
  onSaved: (expert: ExpertItem) => void;
}) {
  const readonly = !!initial?.builtin;
  const [name, setName] = useState(String(initial?.name ?? ""));
  const [description, setDescription] = useState(String(initial?.description ?? ""));
  const [prompt, setPrompt] = useState(String(initial?.system_prompt ?? ""));
  /** 标签只用于列表里分类显示，自定义专家一律标「自定义」，不让用户填 */
  const tag = String(initial?.tag || "自定义");
  const [skillIds, setSkillIds] = useState<string[]>(initial?.skill_ids ?? []);
  const [query, setQuery] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const skills = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return catalog;
    return catalog.filter(
      (item) =>
        String(item.name ?? item.id).toLowerCase().includes(q) ||
        String(item.description ?? "").toLowerCase().includes(q),
    );
  }, [catalog, query]);

  const toggle = (id: string) =>
    setSkillIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("专家需要一个名字");
      return;
    }
    if (!prompt.trim()) {
      setError("系统提示不能为空：专家的人设和工作方式都在这里面");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const body = {
        name: trimmed,
        description: description.trim(),
        system_prompt: prompt.trim(),
        skill_ids: skillIds,
        tag,
      };
      const saved = initial?.id
        ? await addonsApi.updateExpert(initial.id, body)
        : await addonsApi.createExpert(body);
      onSaved(saved);
    } catch (err) {
      setError("保存失败：" + errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="flex h-6 items-center gap-1 rounded-md px-1 text-label text-muted hover:text-ink"
        >
          <Icon name="caret" size={12} className="rotate-180" />
          返回
        </button>
        <span className="text-label text-muted">
          {readonly
            ? "内置专家只读：要改就照这个写一份自定义专家"
            : "专家 = 系统提示 + 预设技能，选中后作为会话默认值"}
        </span>
      </div>

      <label className="flex flex-col gap-1">
        <span className="text-label text-muted">名字</span>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          readOnly={readonly}
          maxLength={40}
          placeholder="如：尽调助手"
          className="h-[30px] rounded-md border border-line bg-paper px-2 text-body text-ink outline-none focus:border-signal read-only:bg-sunken read-only:text-muted"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-label text-muted">一句话说明（显示在列表里）</span>
        <input
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          readOnly={readonly}
          maxLength={400}
          placeholder="如：按招股书口径梳理财务与风险"
          className="h-[30px] rounded-md border border-line bg-paper px-2 text-body text-ink outline-none focus:border-signal read-only:bg-sunken read-only:text-muted"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-label text-muted">
          系统提示
          <span className="ml-1 text-muted/80">（人设、口径、交付标准都写这里）</span>
        </span>
        <textarea
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          readOnly={readonly}
          rows={7}
          placeholder="你是一位……，回答时先……，不要……"
          className="resize-y rounded-md border border-line bg-paper px-2 py-1.5 text-body leading-relaxed text-ink outline-none focus:border-signal read-only:bg-sunken read-only:text-muted"
        />
      </label>

      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="text-label text-muted">预设技能</span>
          <span className="text-label text-muted">
            {skillIds.length ? `已选 ${skillIds.length}` : "不选则只用会话里勾的技能"}
          </span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜技能"
            className="ml-auto h-[24px] w-[140px] rounded-md border border-line bg-paper px-2 text-label text-ink outline-none focus:border-signal"
          />
        </div>
        <div className="max-h-[168px] overflow-y-auto rounded-md border border-line">
          {skills.length === 0 ? (
            <p className="px-2 py-3 text-center text-label text-muted">
              {catalog.length ? "没有匹配的技能" : "还没有安装技能"}
            </p>
          ) : (
            skills.map((skill) => {
              const on = skillIds.includes(skill.id);
              return (
                <button
                  key={skill.id}
                  type="button"
                  disabled={readonly}
                  onClick={() => toggle(skill.id)}
                  className={cn(
                    "flex w-full items-start gap-2 border-b border-line px-2 py-1.5 text-left last:border-b-0 transition-colors duration-150",
                    readonly ? "cursor-default" : "hover:bg-sunken",
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
                      <span className="line-clamp-2 block text-label text-muted">
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

      {error ? <p className="text-label text-danger">{error}</p> : null}

      <div className="flex items-center justify-end gap-2 pt-1">
        <Button variant="ghost" size="sm" onClick={onCancel}>
          {readonly ? "关闭" : "取消"}
        </Button>
        {readonly ? null : (
          <Button variant="primary" size="sm" disabled={saving} onClick={() => void submit()}>
            {saving ? "保存中…" : "保存"}
          </Button>
        )}
      </div>
    </div>
  );
}
