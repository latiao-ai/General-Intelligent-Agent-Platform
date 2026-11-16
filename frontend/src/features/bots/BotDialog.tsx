/**
 * 新建 / 编辑机器人。
 *
 * 一个机器人 = 一行身份 + 一个规范会话 + 一份自己的记忆。所以这个表单
 * 按"它叫什么 → 它是什么人 → 它能用什么 → 它放在哪"的顺序排，不按字段长度排。
 *
 * SOUL（人设）是唯一一个"每轮都会注入系统提示"的字段，tip 里必须说清
 * "改完下一轮生效"——否则用户会以为改了立刻能改变正在跑的那一轮。
 */
import { useEffect, useState } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { cn } from "@/lib/cn";
import { CheckRow, Field, Icon, Select, TextArea, TextInput } from "./primitives";
import type {
  BotPayload,
  BotSection,
  BotSummary,
  ConnectorItem,
  SkillItem,
} from "./types";

const ICONS = ["🤖", "🛠️", "🧠", "📊", "✍️", "🔍", "🧩", "⚡", "🎬", "📦", "🛡️", "🧪", "🎨", "🧭", "📮", "🕹️"];
const COLORS = ["#eef4ff", "#fff3f0", "#eefaf3", "#fdf7ec", "#f6f0ff", "#eef7fb", "#fdf0f6", "#f3f4f6"];

const EMPTY: BotPayload = {
  name: "",
  handle: "",
  title: "",
  description: "",
  soul: "",
  model: "",
  skill_ids: [],
  connector_ids: [],
  section_id: "",
  icon: "🤖",
  color: "#eef4ff",
  shape: "circle",
};

function fromBot(bot: BotSummary): BotPayload {
  return {
    name: bot.name ?? "",
    handle: bot.handle ?? "",
    title: bot.title ?? "",
    description: bot.description ?? "",
    soul: bot.soul ?? "",
    model: bot.model ?? "",
    skill_ids: bot.skill_ids ?? [],
    connector_ids: bot.connector_ids ?? [],
    section_id: bot.section_id ?? "",
    icon: bot.icon ?? "🤖",
    color: bot.color ?? "#eef4ff",
    shape: bot.shape ?? "circle",
  };
}

export function BotDialog({
  open,
  bot,
  sections,
  models,
  skills,
  connectors,
  saving,
  onClose,
  onSubmit,
  onDelete,
  onNewSection,
}: {
  open: boolean;
  bot: BotSummary | null;
  sections: BotSection[];
  models: string[];
  skills: SkillItem[];
  connectors: ConnectorItem[];
  saving: boolean;
  onClose: () => void;
  onSubmit: (payload: BotPayload) => Promise<boolean>;
  onDelete: (bot: BotSummary) => Promise<void>;
  onNewSection: (name: string) => Promise<BotSection | null>;
}) {
  const [form, setForm] = useState<BotPayload>(EMPTY);
  const [sectionDraft, setSectionDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 每次打开都从当前 bot 重新灌一次：上一次取消留下的草稿不该带进来
  useEffect(() => {
    if (!open) return;
    setForm(bot ? fromBot(bot) : EMPTY);
    setSectionDraft(null);
    setError(null);
  }, [open, bot]);

  const patch = (fields: Partial<BotPayload>) => setForm((prev) => ({ ...prev, ...fields }));

  const toggle = (key: "skill_ids" | "connector_ids", id: string, next: boolean) => {
    setForm((prev) => {
      const current = new Set(prev[key]);
      if (next) current.add(id);
      else current.delete(id);
      return { ...prev, [key]: Array.from(current) };
    });
  };

  const submit = async () => {
    if (!form.name.trim()) {
      setError("名字不能为空");
      return;
    }
    setError(null);
    const ok = await onSubmit({ ...form, name: form.name.trim() });
    if (!ok) setError("保存失败，请查看页面上方的提示");
  };

  const configured = connectors.filter((item) => item.configured !== false);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={bot ? `编辑 ${bot.name}` : "新建机器人"}
      className="max-w-[680px]"
    >
      <div className="flex flex-col gap-3.5">
        {bot ? null : (
          <p className="rounded-md bg-rail px-2.5 py-1.5 text-label leading-relaxed text-muted">
            一个机器人 = 一行身份 + 一个规范会话 + 一份自己的记忆。它不会被复制到别的租户。
          </p>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="名字" htmlFor="bot-name" required>
            <TextInput
              id="bot-name"
              value={form.name}
              maxLength={40}
              placeholder="例如：代码审查员"
              onChange={(event) => patch({ name: event.target.value })}
            />
          </Field>
          <Field
            label="handle（@ 用）"
            htmlFor="bot-handle"
            tip="小写字母 / 数字 / - / _，队友之间用它点名。留空从名字推导。"
          >
            <TextInput
              id="bot-handle"
              value={form.handle}
              maxLength={32}
              placeholder="reviewer"
              onChange={(event) => patch({ handle: event.target.value })}
            />
          </Field>
        </div>

        <Field label="一句话职责" htmlFor="bot-title" tip="会显示在花名册和队友的花名册里。">
          <TextInput
            id="bot-title"
            value={form.title}
            maxLength={60}
            placeholder="例如：盯代码质量，只挑会出事的毛病"
            onChange={(event) => patch({ title: event.target.value })}
          />
        </Field>

        <Field label="描述" htmlFor="bot-desc" tip="别人什么时候该找它？">
          <TextArea
            id="bot-desc"
            value={form.description}
            maxLength={400}
            className="min-h-[52px]"
            placeholder="例：上线前审一遍改动，只报会导致事故的问题。"
            onChange={(event) => patch({ description: event.target.value })}
          />
        </Field>

        <Field
          label="人设 / 常驻指令（SOUL）"
          htmlFor="bot-soul"
          tip="这一段每一轮都会注入系统提示；改完下一轮生效（能力纪元会变）。"
        >
          <TextArea
            id="bot-soul"
            value={form.soul}
            maxLength={12000}
            className="min-h-[84px]"
            placeholder="例：你是资深代码审查员。只报会导致线上事故的问题，按严重程度排序，每条给出改法。不确定就说不确定。"
            onChange={(event) => patch({ soul: event.target.value })}
          />
        </Field>

        <Field label="头像">
          <div className="flex flex-wrap gap-1">
            {ICONS.map((icon) => (
              <button
                key={icon}
                type="button"
                onClick={() => patch({ icon })}
                aria-pressed={form.icon === icon}
                aria-label={`头像 ${icon}`}
                className={cn(
                  "flex h-7 w-7 items-center justify-center rounded-md border text-body transition-colors duration-150 ease-standard",
                  form.icon === icon
                    ? "border-signal bg-signal-wash"
                    : "border-line hover:bg-sunken",
                )}
              >
                {icon}
              </button>
            ))}
          </div>
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="主题色">
            <div className="flex flex-wrap gap-1">
              {COLORS.map((color) => (
                <button
                  key={color}
                  type="button"
                  onClick={() => patch({ color })}
                  aria-pressed={form.color === color}
                  aria-label={`主题色 ${color}`}
                  className={cn(
                    "h-[30px] w-[30px] rounded-md border transition-transform duration-150 ease-standard",
                    form.color === color
                      ? "border-signal ring-2 ring-signal/25"
                      : "border-line hover:scale-105",
                  )}
                  style={{ background: color }}
                />
              ))}
            </div>
          </Field>
          <Field label="形状" htmlFor="bot-shape">
            <Select
              id="bot-shape"
              value={form.shape}
              onChange={(event) => patch({ shape: event.target.value })}
            >
              <option value="circle">圆角</option>
              <option value="round">圆形</option>
              <option value="sq">方形</option>
            </Select>
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="模型" htmlFor="bot-model">
            <Select
              id="bot-model"
              value={form.model}
              onChange={(event) => patch({ model: event.target.value })}
            >
              <option value="">跟随中台默认</option>
              {models.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="分区" htmlFor="bot-section">
            <div className="flex gap-1.5">
              <Select
                id="bot-section"
                value={form.section_id}
                onChange={(event) => patch({ section_id: event.target.value })}
              >
                <option value="">未分区</option>
                {sections.map((section) => (
                  <option key={section.section_id} value={section.section_id}>
                    {section.name}
                  </option>
                ))}
              </Select>
              <Button
                className="shrink-0"
                onClick={() => setSectionDraft("")}
                title="新建分区"
              >
                <Icon name="plus" size={12} />
                新建
              </Button>
            </div>
            {sectionDraft !== null ? (
              <div className="mt-1.5 flex gap-1.5">
                <TextInput
                  autoFocus
                  value={sectionDraft}
                  maxLength={40}
                  placeholder="分区名，例如：中台项目"
                  onChange={(event) => setSectionDraft(event.target.value)}
                  onKeyDown={async (event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      const name = sectionDraft.trim();
                      if (!name) return;
                      const created = await onNewSection(name);
                      if (created) {
                        patch({ section_id: created.section_id });
                        setSectionDraft(null);
                      }
                    } else if (event.key === "Escape") {
                      event.stopPropagation();
                      setSectionDraft(null);
                    }
                  }}
                />
                <Button
                  className="shrink-0"
                  onClick={async () => {
                    const name = sectionDraft.trim();
                    if (!name) return;
                    const created = await onNewSection(name);
                    if (created) {
                      patch({ section_id: created.section_id });
                      setSectionDraft(null);
                    }
                  }}
                >
                  确定
                </Button>
              </div>
            ) : null}
          </Field>
        </div>

        <Field
          label="技能（默认启用，可在会话里覆盖）"
          tip={
            skills.length
              ? "只影响这个机器人的默认值；会话里还能再改。"
              : "没有读到已安装的技能。可以在技能页装完再回来。"
          }
        >
          <div className="max-h-[132px] overflow-y-auto overscroll-contain rounded-md border border-line bg-rail/40 p-1">
            {skills.length ? (
              skills.slice(0, 200).map((skill) => (
                <CheckRow
                  key={skill.id}
                  checked={form.skill_ids.includes(skill.id)}
                  onChange={(next) => toggle("skill_ids", skill.id, next)}
                  title={skill.name ?? skill.id}
                  hint={(skill.description ?? "").slice(0, 70)}
                />
              ))
            ) : (
              <p className="px-2 py-2 text-label text-muted">没有可用技能</p>
            )}
          </div>
        </Field>

        <Field
          label="连接器（默认授权给这个机器人）"
          tip={configured.length ? undefined : "还没有配置连接器。"}
        >
          <div className="max-h-[132px] overflow-y-auto overscroll-contain rounded-md border border-line bg-rail/40 p-1">
            {configured.length ? (
              configured.map((item) => (
                <CheckRow
                  key={item.id}
                  checked={form.connector_ids.includes(item.id)}
                  onChange={(next) => toggle("connector_ids", item.id, next)}
                  title={item.name ?? item.id}
                  hint={`${(item.description ?? "").slice(0, 70)}${
                    item.authorized ? "" : " · 未授权"
                  }`}
                />
              ))
            ) : (
              <p className="px-2 py-2 text-label text-muted">还没有配置连接器</p>
            )}
          </div>
        </Field>

        {error ? (
          <p role="alert" className="text-label text-danger">
            {error}
          </p>
        ) : null}

        {/* 预览：改完颜色和头像不用保存就能看见 */}
        <div className="flex items-center gap-2 rounded-lg bg-rail px-3 py-2">
          <Avatar
            size={28}
            icon={form.icon}
            color={form.color}
            shape={form.shape}
          />
          <div className="min-w-0">
            <div className="truncate text-body font-medium text-ink">
              {form.name || "未命名机器人"}
            </div>
            <div className="truncate text-label text-muted">
              @{form.handle || "handle"} · {form.title || "还没写职责"}
            </div>
          </div>
        </div>
      </div>

      <div className="sticky -bottom-4 -mx-5 -mb-4 mt-4 flex items-center gap-2 border-t border-line bg-paper px-5 py-3">
        {bot ? (
          <Button
            variant="danger"
            disabled={saving}
            onClick={() => {
              if (window.confirm(`删除「${bot.name}」？它的对话档案会保留，但花名册里不再出现。`)) {
                void onDelete(bot);
              }
            }}
          >
            <Icon name="trash" size={12} />
            归档
          </Button>
        ) : null}
        <span className="flex-1" />
        <Button onClick={onClose} disabled={saving}>
          取消
        </Button>
        <Button variant="primary" onClick={() => void submit()} disabled={saving}>
          {saving ? "保存中…" : "保存"}
        </Button>
      </div>
    </Dialog>
  );
}
