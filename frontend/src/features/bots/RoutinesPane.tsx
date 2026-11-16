/**
 * 例行任务视图。
 *
 * 一页只回答三个问题：它属于谁、什么时候跑、上次跑成什么样。
 * 编辑规则（改时间、立即运行、看运行记录）不在这里——那套规则编辑器在
 * 任务工作台的定时任务页。这里再造一个"简易版"会出现两套语义，
 * 改了一处另一处不说实话。
 */
import { Avatar } from "@/components/ui/Avatar";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Icon } from "./primitives";
import type { BotSummary, Routine } from "./types";

export function RoutinesPane({
  tasks,
  phase,
  error,
  bots,
  onOpenBot,
  onNewRoutine,
  onRetry,
}: {
  tasks: Routine[];
  phase: "loading" | "ready" | "error";
  error: string | null;
  bots: BotSummary[];
  onOpenBot: (botId: string) => void;
  onNewRoutine: (bot: BotSummary) => void;
  onRetry: () => void;
}) {
  if (phase === "loading") {
    return (
      <div className="mx-auto w-full max-w-[760px] space-y-3 px-6 py-5">
        {[0, 1, 2].map((index) => (
          <Skeleton key={index} className="h-16 w-full rounded-xl" />
        ))}
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center px-6">
        <EmptyState
          kind="error"
          message={error ?? "读取例行任务失败"}
          action={
            <Button variant="primary" onClick={onRetry}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  if (!tasks.length) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center px-6">
        <div className="w-full max-w-[420px]">
          <h2 className="text-title font-semibold text-ink">还没有例行任务</h2>
          <p className="mt-1 text-secondary leading-relaxed text-muted">
            在任意机器人的右上角点「例行任务」，或在定时任务页创建时选择归属机器人。到点后结果会作为一条消息回到它的会话。
          </p>
          <div className="mt-3 flex items-center gap-2">
            <Button
              variant="primary"
              onClick={() => {
                if (bots.length) onNewRoutine(bots[0]!);
              }}
              disabled={!bots.length}
            >
              <Icon name="plus" size={13} />
              给机器人加一条
            </Button>
            <a
              href="/api/ui"
              className="inline-flex h-[30px] items-center rounded-md border border-line-strong bg-paper px-3 text-body font-medium text-ink-2 transition-colors duration-150 ease-standard hover:border-muted/45 hover:text-ink"
            >
              去定时任务页
            </a>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-6 py-5">
      <div className="mx-auto flex w-full max-w-[760px] flex-col gap-2.5">
        {tasks.map((task, index) => {
          const bot = bots.find((item) => item.bot_id === task.bot_id);
          return (
            <article
              key={task.schedule_id ?? index}
              className="rounded-xl border border-line bg-paper p-3.5"
            >
              <div className="flex items-start gap-2.5">
                <Avatar size={28} icon={bot?.icon} color={bot?.color} image={bot?.image} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <h3 className="text-body font-semibold text-ink">
                      {task.name ?? "未命名任务"}
                    </h3>
                    <Badge tone={task.enabled ? "ok" : "neutral"}>
                      {task.enabled ? "已启用" : "已停用"}
                    </Badge>
                    <Badge>{bot?.name ?? task.bot_id ?? "未知机器人"}</Badge>
                  </div>
                  <p className="mt-0.5 text-label text-muted">
                    {task.schedule_desc ?? task.rrule ?? "频率未提供"}
                    {task.last_status ? ` · 上次 ${task.last_status}` : ""}
                  </p>
                  {task.prompt ? (
                    <p className="mt-1.5 text-secondary leading-relaxed whitespace-pre-wrap text-ink-2">
                      {task.prompt}
                    </p>
                  ) : null}
                </div>
                {bot ? (
                  <Button size="sm" onClick={() => onOpenBot(bot.bot_id)}>
                    打开机器人
                  </Button>
                ) : null}
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}
