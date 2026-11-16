/**
 * 左轨：三个渠道 + 网关运行状态 + 回任务台。
 *
 * 渠道标识（飞 / 企 / 微）用中性方块，不再像老页那样每家用一个饱和渐变
 * （蓝→深蓝、青→深青、绿→深绿）。三个渐变在 232px 的窄栏里互相打，
 * 反而看不出哪个是"当前选中"。这里选中的信号只给一处：白底 + 描边。
 *
 * 状态点的颜色来自后端派生的 `tone`，旁边永远跟着 `state_label` 文字。
 */
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { Icon, StateDot } from "./primitives";
import type { BotOption, ChannelView } from "./types";

export function MessagingRail({
  channels,
  selected,
  bots,
  showBot,
  gatewayAlive,
  loading,
  onSelect,
  onRefresh,
}: {
  channels: ChannelView[];
  selected: string;
  bots: BotOption[];
  /**
   * 在「默认」tab 上才显示每个渠道绑的机器人。机器人实例下所有渠道的身份
   * 都一样（就是 tab 上那个），每行都缀一遍纯属噪声。
   */
  showBot: boolean;
  gatewayAlive: boolean;
  loading: boolean;
  onSelect: (id: string) => void;
  onRefresh: () => void;
}) {
  /** bot_id → 名字。花名册读不到时返回空串，列表退化成只显示状态文字。 */
  const botName = (id: string) => (id ? (bots.find((bot) => bot.bot_id === id)?.name ?? "") : "");
  return (
    <aside className="flex w-[232px] shrink-0 flex-col border-r border-line bg-rail">
      <div className="flex items-center gap-2 px-3 pt-3 pb-2">
        <div
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-signal text-paper"
          aria-hidden="true"
        >
          <Icon name="bubble" size={15} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-body font-semibold text-ink">消息平台</div>
          <div className="truncate text-label text-muted">飞书 · 企业微信 · 个人微信</div>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          title="刷新状态"
          aria-label="刷新状态"
          className="flex h-6 w-6 items-center justify-center rounded-md text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
        >
          <Icon name="refresh" />
        </button>
      </div>

      <div className="px-3 pb-2">
        <a
          href="/"
          className="flex h-[30px] items-center gap-1.5 rounded-md border border-line-strong bg-paper px-2.5 text-secondary font-medium text-ink-2 transition-colors duration-150 ease-standard hover:border-muted/45 hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
        >
          <Icon name="arrowLeft" size={12} />
          回到任务台
        </a>
      </div>

      <div className="flex items-center gap-1.5 px-4 pt-2 pb-1.5">
        <span className="text-label font-semibold tracking-wide text-muted">渠道</span>
        <span className="ml-auto text-label text-muted">{channels.length}</span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 pb-2">
        {loading && !channels.length ? (
          <div className="flex flex-col gap-1.5 px-1 py-1">
            <Skeleton className="h-[52px]" />
            <Skeleton className="h-[52px]" />
            <Skeleton className="h-[52px]" />
          </div>
        ) : (
          <ul className="flex list-none flex-col gap-0.5">
            {channels.map((channel) => {
              const isActive = channel.id === selected;
              const bound = showBot ? botName(channel.bot_id) : "";
              return (
                <li key={channel.id}>
                  <button
                    type="button"
                    onClick={() => onSelect(channel.id)}
                    aria-current={isActive ? "true" : undefined}
                    className={cn(
                      "flex w-full items-center gap-2.5 rounded-lg border p-2 text-left transition-colors duration-150 ease-standard",
                      "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
                      isActive
                        ? "border-line bg-paper shadow-lift"
                        : "border-transparent hover:bg-sunken",
                    )}
                  >
                    <span
                      className={cn(
                        "flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-secondary font-semibold",
                        isActive
                          ? "bg-signal-wash text-signal-deep"
                          : "bg-paper text-ink-2 ring-1 ring-line",
                      )}
                      aria-hidden="true"
                    >
                      {channel.name.slice(0, 1)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5">
                        <span
                          className={cn(
                            "truncate text-body",
                            isActive ? "font-semibold text-ink" : "font-medium text-ink-2",
                          )}
                        >
                          {channel.name}
                        </span>
                        <StateDot tone={channel.tone} />
                      </span>
                      {/*
                        状态文字已经说了「未启用」，不再叠一个「停用」徽章。
                        后面缀上绑定的机器人：三个渠道各自绑不同 bot 时，
                        光看「未启用 / 运行中」分不清哪一条是哪个身份。
                      */}
                      <span className="mt-0.5 block truncate text-label text-muted">
                        {channel.state_label}
                        {bound ? (
                          <>
                            <span className="mx-1 text-line-strong" aria-hidden="true">
                              ·
                            </span>
                            {bound}
                          </>
                        ) : null}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="border-t border-line px-3 py-2">
        <div className="flex items-center gap-2" aria-live="polite">
          <StateDot tone={gatewayAlive ? "good" : "bad"} />
          <span className="text-label text-muted">
            消息网关 <b className="font-medium text-ink-2">{gatewayAlive ? "运行中" : "未运行"}</b>
          </span>
        </div>
      </div>
    </aside>
  );
}
