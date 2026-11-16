/**
 * 对话视图。一屏只有一条滚动条，落在消息区；输入器永远吸底。
 *
 * 空态分三种，因为用户要做的事完全不同：
 *   没选机器人 → 去花名册挑一个，或新建
 *   会话里没消息 → 打招呼（给两个可以直接点的开场）
 *   加载失败 → 重试
 * 旧页把它们混成一个大 hero，把一个每天要进来干活的界面写成了宣传页。
 */
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Composer } from "./Composer";
import { MessageRow, TypingRow } from "./MessageRow";
import { Icon } from "./primitives";
import type { BotSummary, ChatMessage } from "./types";

/** 标签短、句子长：按钮上放短标签，点下去填的是整句。 */
const SUGGESTIONS = [
  { label: "让它自我介绍", text: "先做个自我介绍：你是谁、擅长什么、什么时候该找你。" },
  { label: "看它的能力", text: "把你能用的技能和连接器列出来，并说明各自什么时候用。" },
];

export function BotChatPane({
  bot,
  messages,
  draft,
  phase,
  error,
  streaming,
  busy,
  onSend,
  onRetry,
  onNewBot,
  onNewRoom,
}: {
  bot: BotSummary | undefined;
  messages: ChatMessage[];
  draft: string;
  phase: "idle" | "loading" | "ready" | "error";
  error: string | null;
  streaming: boolean;
  busy: boolean;
  onSend: (text: string) => Promise<boolean>;
  onRetry: () => void;
  onNewBot: () => void;
  onNewRoom: () => void;
}) {
  const [input, setInput] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const bottom = useRef<HTMLDivElement>(null);

  // 新消息、流式增量都要跟到底，否则用户发完话看不到回答
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length, draft, streaming]);

  if (phase === "idle") {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center px-6">
        <div className="w-full max-w-[420px]">
          <h1 className="text-title font-semibold text-ink">机器人</h1>
          <p className="mt-1 text-secondary leading-relaxed text-muted">
            每个机器人有自己的人设、模型、技能、记忆与规范会话。它们能互相发消息，也能拉进一个群聊按轮次讨论。
          </p>
          <div className="mt-3 flex items-center gap-2">
            <Button variant="primary" onClick={onNewBot}>
              <Icon name="plus" size={13} />
              新建机器人
            </Button>
            <Button onClick={onNewRoom}>
              <Icon name="bubble" size={13} />
              新建群聊
            </Button>
          </div>
        </div>
      </div>
    );
  }

  if (phase === "loading") {
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-4 px-6 py-5">
        <div className="mx-auto w-full max-w-[760px] space-y-4">
          {[0, 1, 2].map((index) => (
            <div key={index} className="flex gap-2.5">
              <Skeleton className="h-[26px] w-[26px] rounded-lg" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-2.5 w-24" />
                <Skeleton className={index === 1 ? "h-14 w-3/4" : "h-9 w-full"} />
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center px-6">
        <EmptyState
          kind="error"
          message={error ?? "打开会话失败"}
          action={
            <Button variant="primary" onClick={onRetry}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <>
      <div
        ref={scroller}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-6 py-5"
        aria-live="polite"
        aria-label="对话记录"
      >
        {messages.length === 0 && !streaming ? (
          <div className="mx-auto w-full max-w-[760px] rounded-xl border border-dashed border-line-strong bg-rail/40 px-5 py-6">
            <h2 className="text-body font-semibold text-ink">
              和「{bot?.name ?? "它"}」打个招呼
            </h2>
            <p className="mt-1 text-secondary leading-relaxed text-muted">
              这是它的规范会话：一直存在、跨客户端一致。归档后再次打开会自动新建一个同名会话。
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              {SUGGESTIONS.map((item) => (
                <Button
                  key={item.label}
                  onClick={() => setInput(item.text)}
                  title={item.text}
                >
                  {item.label}
                </Button>
              ))}
            </div>
          </div>
        ) : (
          <div className="mx-auto flex w-full max-w-[760px] flex-col gap-4">
            {messages.map((message, index) => (
              <MessageRow key={index} message={message} bot={bot} />
            ))}
            {streaming ? <TypingRow bot={bot} draft={draft} /> : null}
          </div>
        )}
        <div ref={bottom} />
      </div>

      <Composer
        value={input}
        onChange={setInput}
        onSend={async () => {
          const text = input;
          setInput("");
          const ok = await onSend(text);
          // 发送失败时把原文放回输入框：用户不必重打一遍
          if (!ok) setInput(text);
        }}
        placeholder="和这个机器人说点什么…（Enter 发送，Shift+Enter 换行）"
        hint="消息会进入它的规范会话；它可以调用 message_bot 找队友。"
        disabled={busy || !bot}
      />
    </>
  );
}
