/**
 * 配对请求。
 *
 * 流程值得写在卡片上而不只在文档里：陌生人在私聊里说话 → 收到 8 位码 →
 * 管理员在这批准 → 对方收到「配对成功」。所以这张卡有一个输入框，
 * 而不是只有一张列表：批准是这里唯一要做的动作。
 *
 * 撤销用行内按钮 + 二次确认（确认弹窗在页面层），不直接执行——
 * 撤销会把对方踢下线，误点一下代价太高。
 */
import { useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Section, TextInput } from "./primitives";
import type { PairingRow } from "./types";

export function PairingCard({
  pairing,
  busy,
  onApprove,
  onRevoke,
}: {
  pairing: { pending: PairingRow[]; approved: PairingRow[] } | undefined;
  busy: boolean;
  onApprove: (code: string) => Promise<boolean>;
  onRevoke: (row: PairingRow) => void;
}) {
  const [code, setCode] = useState("");
  const pending = pairing?.pending ?? [];
  const approved = pairing?.approved ?? [];

  const submit = async () => {
    const ok = await onApprove(code);
    if (ok) setCode("");
  };

  return (
    <Section
      icon="users"
      title="配对请求"
      sub="陌生人在私聊里会收到 8 位配对码；批准后才允许驱动 Agent"
      actions={
        <Badge tone={pending.length ? "warn" : "neutral"}>待批 {pending.length}</Badge>
      }
    >
      <form
        className="flex items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <TextInput
          value={code}
          maxLength={16}
          inputMode="numeric"
          autoComplete="off"
          aria-label="配对码"
          placeholder="输入 8 位配对码"
          className="max-w-[240px] font-mono tracking-[0.12em]"
          onChange={(event) => setCode(event.target.value)}
        />
        <Button type="submit" disabled={busy || !code.trim()}>
          {busy ? "处理中…" : "批准"}
        </Button>
      </form>

      <div className="mt-3">
        {pending.length ? (
          <ul className="flex list-none flex-col divide-y divide-line">
            {pending.map((row) => {
              const expired = row.expires_in <= 0;
              return (
                <li key={`${row.platform}:${row.user_id}`} className="flex items-center gap-2.5 py-2">
                  <Badge tone={expired ? "neutral" : "warn"} className="shrink-0">
                    {row.platform}
                  </Badge>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-secondary text-ink" title={row.user_id}>
                      {row.user_id}
                    </p>
                    <p className="truncate text-label text-muted" title={row.chat_id}>
                      {row.chat_id}
                      {expired
                        ? " · 已过期，让对方重新发一条消息"
                        : ` · ${Math.ceil(row.expires_in / 60)} 分钟内有效`}
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-secondary text-muted">
            没有待批请求。对方在私聊里发第一句话之后会出现在这里。
          </p>
        )}
      </div>

      {approved.length ? (
        <div className="mt-4 border-t border-line pt-3">
          <p className="text-label font-semibold tracking-wide text-muted">已批准 · {approved.length}</p>
          <ul className="mt-1.5 flex list-none flex-col divide-y divide-line">
            {approved.map((row) => (
              <li key={`${row.platform}:${row.user_id}`} className="flex items-center gap-2.5 py-2">
                <Badge tone="ok" className="shrink-0">
                  {row.platform}
                </Badge>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-secondary text-ink" title={row.user_id}>
                    {row.user_id}
                  </p>
                  <p className="truncate text-label text-muted" title={row.chat_id}>
                    {row.chat_id}
                  </p>
                </div>
                <Button size="sm" variant="danger" onClick={() => onRevoke(row)} disabled={busy}>
                  撤销
                </Button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Section>
  );
}
