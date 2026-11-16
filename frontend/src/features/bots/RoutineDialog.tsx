/**
 * 给一个机器人加例行任务。
 *
 * 这里故意只做"归属这个机器人的简单周期任务"。完整编辑（改规则、立即运行、
 * 看运行记录）在任务工作台的定时任务页——两套规则编辑器迟早会说出不同的话。
 *
 * RRULE 用预设 + 自定义兜底：绝大多数人每天 9 点跑一次，不该被要求写 RFC 5545。
 */
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Field, Select, TextArea, TextInput } from "./primitives";
import type { BotSummary } from "./types";

const PRESETS: Array<{ value: string; label: string }> = [
  { value: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0", label: "每天 09:00" },
  { value: "FREQ=DAILY;BYHOUR=18;BYMINUTE=0", label: "每天 18:00" },
  { value: "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0", label: "每周一 09:00" },
  { value: "FREQ=HOURLY", label: "每小时" },
  { value: "custom", label: "自定义 RRULE…" },
];

export function RoutineDialog({
  open,
  bot,
  saving,
  onClose,
  onSubmit,
}: {
  open: boolean;
  bot: BotSummary | null;
  saving: boolean;
  onClose: () => void;
  onSubmit: (body: { name: string; prompt: string; rrule: string }) => Promise<boolean>;
}) {
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [preset, setPreset] = useState(PRESETS[0]!.value);
  const [custom, setCustom] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName("");
    setPrompt("");
    setPreset(PRESETS[0]!.value);
    setCustom("");
    setError(null);
  }, [open]);

  const submit = async () => {
    if (!name.trim() || !prompt.trim()) {
      setError("任务名和「要它做什么」都不能为空");
      return;
    }
    const rrule = preset === "custom" ? custom.trim() : preset;
    if (!rrule) {
      setError("请填写 RRULE");
      return;
    }
    setError(null);
    const ok = await onSubmit({ name: name.trim(), prompt: prompt.trim(), rrule });
    if (!ok) setError("创建失败，请查看页面提示");
  };

  return (
    <Dialog open={open} onClose={onClose} title="例行任务" className="max-w-[560px]">
      <div className="flex flex-col gap-3.5">
        <p className="rounded-md bg-rail px-2.5 py-1.5 text-label leading-relaxed text-muted">
          归属：{bot?.name ?? "未选择"}（@{bot?.handle ?? ""}）。到点后结果会作为一条消息回到
          它的规范会话。
        </p>

        <Field label="任务名" htmlFor="rt-name" required>
          <TextInput
            id="rt-name"
            value={name}
            maxLength={60}
            placeholder="例如：每日热点日报"
            onChange={(event) => setName(event.target.value)}
          />
        </Field>

        <Field label="要它做什么" htmlFor="rt-prompt" required>
          <TextArea
            id="rt-prompt"
            value={prompt}
            maxLength={10000}
            className="min-h-[72px]"
            placeholder="例：检索今天 AI 领域最重要的 5 条动态，每条给出来源与一句影响判断。"
            onChange={(event) => setPrompt(event.target.value)}
          />
        </Field>

        <Field label="频率" htmlFor="rt-preset">
          <Select
            id="rt-preset"
            value={preset}
            onChange={(event) => setPreset(event.target.value)}
          >
            {PRESETS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </Select>
        </Field>

        {preset === "custom" ? (
          <Field
            label="RRULE"
            htmlFor="rt-custom"
            tip="支持 FREQ=HOURLY/DAILY/WEEKLY/MONTHLY/YEARLY，可选 INTERVAL、BYDAY、BYHOUR、BYMINUTE。"
          >
            <TextInput
              id="rt-custom"
              value={custom}
              placeholder="FREQ=DAILY;BYHOUR=9;BYMINUTE=0"
              onChange={(event) => setCustom(event.target.value)}
            />
          </Field>
        ) : null}

        {error ? (
          <p role="alert" className="text-label text-danger">
            {error}
          </p>
        ) : null}
      </div>

      <div className="sticky -bottom-4 -mx-5 -mb-4 mt-4 flex items-center gap-2 border-t border-line bg-paper px-5 py-3">
        <span className="flex-1" />
        <Button onClick={onClose} disabled={saving}>
          取消
        </Button>
        <Button variant="primary" onClick={() => void submit()} disabled={saving}>
          {saving ? "创建中…" : "创建"}
        </Button>
      </div>
    </Dialog>
  );
}
