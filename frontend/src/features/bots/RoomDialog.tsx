/**
 * 新建群聊。
 *
 * 成员上限 6、下限 2 不是前端发明的，是房间调度的约束：少于 2 个没有"讨论"，
 * 多于 6 个每轮消息数会先把上限打满，成员还没轮到就被收束。
 * 所以这两个数字在表单里直接写出来，别让用户点完提交才知道。
 */
import { useEffect, useState } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { CheckRow, Field, TextArea, TextInput } from "./primitives";
import type { BotSummary } from "./types";

export function RoomDialog({
  open,
  bots,
  saving,
  onClose,
  onSubmit,
}: {
  open: boolean;
  bots: BotSummary[];
  saving: boolean;
  onClose: () => void;
  onSubmit: (body: {
    name: string;
    topic: string;
    members: string[];
    max_rounds: number;
    max_messages: number;
  }) => Promise<boolean>;
}) {
  const [name, setName] = useState("");
  const [topic, setTopic] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [rounds, setRounds] = useState("3");
  const [maxMessages, setMaxMessages] = useState("10");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName("");
    setTopic("");
    setMembers([]);
    setRounds("3");
    setMaxMessages("10");
    setError(null);
  }, [open]);

  const candidates = bots.filter((bot) => bot.status !== "archived");

  const submit = async () => {
    if (!name.trim()) {
      setError("房间名不能为空");
      return;
    }
    if (members.length < 2) {
      setError("至少选 2 个机器人");
      return;
    }
    setError(null);
    const ok = await onSubmit({
      name: name.trim(),
      topic: topic.trim(),
      members,
      max_rounds: Math.max(1, Number.parseInt(rounds, 10) || 3),
      max_messages: Math.max(2, Number.parseInt(maxMessages, 10) || 10),
    });
    if (!ok) setError("创建失败，请查看页面提示");
  };

  return (
    <Dialog open={open} onClose={onClose} title="新建群聊" className="max-w-[560px]">
      <div className="flex flex-col gap-3.5">
        <p className="rounded-md bg-rail px-2.5 py-1.5 text-label leading-relaxed text-muted">
          2–6 个机器人，按轮次发言；每轮最多 3 轮、整间最多 10 条消息，全员「无补充」即收束。
        </p>

        <Field label="房间名" htmlFor="room-name" required>
          <TextInput
            id="room-name"
            value={name}
            maxLength={40}
            placeholder="例如：发布评审"
            onChange={(event) => setName(event.target.value)}
          />
        </Field>

        <Field
          label="议题"
          htmlFor="room-topic"
          tip="用 @handle 点名，被点名的人先说。"
        >
          <TextArea
            id="room-topic"
            value={topic}
            maxLength={400}
            placeholder="例如：@reviewer 周五发版，还差哪些阻塞项？"
            onChange={(event) => setTopic(event.target.value)}
          />
        </Field>

        <Field
          label={`成员（已选 ${members.length}，至少 2 个、最多 6 个）`}
          tip={candidates.length < 2 ? "至少要有 2 个机器人才能建群聊。" : undefined}
        >
          <div className="max-h-[168px] overflow-y-auto overscroll-contain rounded-md border border-line bg-rail/40 p-1">
            {candidates.length ? (
              candidates.map((bot) => (
                <CheckRow
                  key={bot.bot_id}
                  checked={members.includes(bot.bot_id)}
                  onChange={(next) => {
                    setMembers((prev) => {
                      if (!next) return prev.filter((id) => id !== bot.bot_id);
                      if (prev.length >= 6) return prev;
                      return [...prev, bot.bot_id];
                    });
                  }}
                  title={bot.name}
                  hint={`@${bot.handle ?? ""}${bot.title ? ` · ${bot.title}` : ""}`}
                  leading={
                    <Avatar
                      size={16}
                      icon={bot.icon}
                      color={bot.color}
                      shape={bot.shape}
                      image={bot.image}
                    />
                  }
                />
              ))
            ) : (
              <p className="px-2 py-2 text-label text-muted">还没有可用机器人</p>
            )}
          </div>
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="最多轮次" htmlFor="room-rounds" tip="每轮每个成员发一次言。">
            <TextInput
              id="room-rounds"
              value={rounds}
              inputMode="numeric"
              onChange={(event) => setRounds(event.target.value)}
            />
          </Field>
          <Field label="最多消息" htmlFor="room-messages" tip="到达后自动收束。">
            <TextInput
              id="room-messages"
              value={maxMessages}
              inputMode="numeric"
              onChange={(event) => setMaxMessages(event.target.value)}
            />
          </Field>
        </div>

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
