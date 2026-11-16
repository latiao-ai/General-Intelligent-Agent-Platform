/**
 * 群聊视图。
 *
 * 房间是**事件日志**，不是消息列表：`room.opened` / `message.member` /
 * `turn.failed` / `room.settled` 这些东西混在一条时间线上，本身就承载
 * "为什么收束了"这个信息。所以不能只渲染气泡，系统事件必须留在原位。
 *
 * 房间由服务端调度：关掉页面 Worker 也会推进。界面上的「推进一拍」只是把
 * 同一段逻辑提前触发一次，措辞上不写"开始讨论"，避免让人以为是前端在编排。
 */
import { useEffect, useRef, useState } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { Composer } from "./Composer";
import { Icon } from "./primitives";
import type { RoomDetail, RoomMember, RoomSummary } from "./types";

const SETTLE_REASON: Record<string, string> = {
  silent_round: "全员无补充",
  round_limit: "轮次用尽",
  message_limit: "消息数用尽",
  no_members: "没有可用成员",
  all_failed: "全员失败",
};

const NOTE =
  "flex items-center gap-1.5 rounded-md bg-rail px-2.5 py-1.5 text-label leading-relaxed text-muted";

function clock(iso?: string) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

export function RoomPane({
  detail,
  phase,
  error,
  isOpen,
  onSay,
  onRetry,
}: {
  detail: RoomDetail | null;
  phase: "idle" | "loading" | "ready" | "error";
  error: string | null;
  isOpen: boolean;
  onSay: (text: string) => Promise<boolean>;
  onRetry: () => void;
}) {
  const [input, setInput] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  const eventCount = detail?.events?.length ?? 0;

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [eventCount]);

  if (phase === "idle") {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center px-6">
        <p className="max-w-[380px] text-center text-secondary leading-relaxed text-muted">
          左侧选一个群聊，或者点「群聊」下的 ＋ 新建。房间由服务端按轮次推进，关掉页面也会继续。
        </p>
      </div>
    );
  }

  if (phase === "loading") {
    return (
      <div className="mx-auto w-full max-w-[760px] space-y-3 px-6 py-5">
        <Skeleton className="h-6 w-full" />
        <Skeleton className="h-16 w-3/4" />
        <Skeleton className="h-12 w-2/3" />
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center px-6">
        <EmptyState
          kind="error"
          message={error ?? "打开群聊失败"}
          action={
            <Button variant="primary" onClick={onRetry}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  const room = (detail?.room ?? {}) as RoomSummary;
  const members: RoomMember[] = detail?.members ?? [];
  const events = detail?.events ?? [];
  const plan = detail?.plan ?? {};
  const byId = new Map(members.map((member) => [member.bot_id, member]));
  const spoken = events.filter((event) => event.kind === "message.member").length;

  let planText = "";
  if (room.status === "open") {
    if (plan.action === "speak") {
      const member = plan.bot_id ? byId.get(plan.bot_id) : undefined;
      planText = `下一步：${member?.name ?? plan.bot_id ?? "某位成员"} 发言（第 ${Number(plan.round ?? 0) + 1} 轮 / 第 ${plan.position ?? 1} 位${
        plan.reason === "mentioned" ? " · 被点名" : ""
      }）`;
    } else if (plan.action === "settle") {
      planText = `下一步：收束（${SETTLE_REASON[String(plan.reason)] ?? plan.reason ?? ""}）`;
    } else {
      planText = "下一步：继续轮转";
    }
  }

  return (
    <>
      <div
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-6 py-5"
        aria-live="polite"
        aria-label="群聊记录"
      >
        <div className="mx-auto flex w-full max-w-[760px] flex-col gap-3">
          {events.map((event, index) => {
            const payload = (event.payload ?? {}) as Record<string, unknown>;
            if (event.kind === "room.opened") {
              return (
                <div key={index} className={NOTE}>
                  <Icon name="bubble" size={12} />
                  房间已创建 · {members.length} 个成员
                </div>
              );
            }
            if (event.kind === "message.user") {
              return (
                <div key={index} className="flex justify-end">
                  <div className="max-w-[80%]">
                    <div className="mb-1 flex justify-end gap-2 text-label text-muted">
                      <span>你</span>
                      <span className="tabular-nums">{clock(event.created_at)}</span>
                    </div>
                    <div className="rounded-xl rounded-br-sm border border-signal/20 bg-signal-wash px-3 py-2 text-body leading-[1.72] whitespace-pre-wrap break-words text-ink">
                      {String(payload.text ?? "")}
                    </div>
                  </div>
                </div>
              );
            }
            if (event.kind === "message.member") {
              const member = payload.bot_id ? byId.get(String(payload.bot_id)) : undefined;
              const silent = Boolean(payload.silent);
              return (
                <div key={index} className={cn("flex gap-2.5", silent && "opacity-70")}>
                  <Avatar
                    size={26}
                    icon={member?.icon ?? String(payload.icon ?? "")}
                    className="mt-0.5"
                  />
                  <div className="min-w-0 max-w-[80%]">
                    <div className="mb-1 flex flex-wrap items-center gap-1.5 text-label text-muted">
                      <span className="font-semibold text-ink">
                        {member?.name ?? String(payload.name ?? payload.bot_id ?? "成员")}
                      </span>
                      <span>@{member?.handle ?? String(payload.handle ?? "")}</span>
                      <span>第 {Number(payload.round ?? 0) + 1} 轮</span>
                      <span className="tabular-nums">{clock(event.created_at)}</span>
                      {silent ? <Badge>无补充</Badge> : null}
                    </div>
                    <div className="rounded-xl rounded-bl-sm border border-line bg-paper px-3 py-2 text-body leading-[1.72] whitespace-pre-wrap break-words text-ink">
                      {String(payload.text ?? "")}
                    </div>
                  </div>
                </div>
              );
            }
            if (event.kind === "turn.failed") {
              const member = payload.bot_id ? byId.get(String(payload.bot_id)) : undefined;
              return (
                <div key={index} className={cn(NOTE, "bg-danger-wash text-danger")}>
                  <Icon name="close" size={11} />
                  {member?.name ?? String(payload.bot_id ?? "某个成员")} 这一轮没跑成：
                  {String(payload.reason ?? "未知原因")}
                </div>
              );
            }
            if (event.kind === "room.settled") {
              return (
                <div key={index} className={NOTE}>
                  <Icon name="checklist" size={12} />
                  已收束 · {SETTLE_REASON[String(payload.reason)] ?? String(payload.reason ?? "")}
                </div>
              );
            }
            if (event.kind === "room.closed") {
              return (
                <div key={index} className={NOTE}>
                  <Icon name="close" size={11} />
                  房间已关闭
                </div>
              );
            }
            // 未知事件不吞掉：服务端加了新 kind，界面应该显示出来而不是当没发生
            return (
              <div key={index} className={NOTE}>
                {event.kind ?? "未知事件"}
              </div>
            );
          })}

          {planText ? (
            <div className="rounded-md border border-signal/20 bg-signal-wash px-2.5 py-1.5 text-label text-signal-deep">
              {planText} · 已发言 {spoken} / {room.max_messages ?? "—"}
            </div>
          ) : null}
          <div ref={bottom} />
        </div>
      </div>

      <Composer
        value={input}
        onChange={setInput}
        onSend={async () => {
          const text = input;
          setInput("");
          try {
            await onSay(text);
          } catch {
            setInput(text);
          }
        }}
        placeholder={isOpen ? "在群里说一句…" : "房间已结束，只能查看记录"}
        hint={
          isOpen
            ? "房间由服务端调度：你发言后点「推进一拍」，Worker 也会自己推进。"
            : "房间已结束，只能查看记录。"
        }
        muted={!isOpen}
      />
    </>
  );
}
