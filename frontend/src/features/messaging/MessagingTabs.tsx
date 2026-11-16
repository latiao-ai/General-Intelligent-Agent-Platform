/**
 * 机器人 tab：这一排是**页面作用域**，不是筛选器。
 *
 * 选中的 tab 决定下面所有卡片配的是谁的接入：
 *  - 默认：存量那一份，凭据读 `.env` 基础键（FEISHU_APP_ID），一个部署一套应用，
 *    回话身份由渠道上的「绑定机器人」决定；
 *  - 机器人：这个机器人独占一套 IM 应用（FEISHU_APP_ID__XZS），回话身份固定是它。
 *
 * 所以 tab 必须一直看得见。把它做成下拉或收进设置，就等于把这个区别藏起来——
 * 那正是上一版最容易被配错的地方（三个渠道 × 多个机器人，顶栏只写渠道名）。
 *
 * 状态点的颜色由后端按实例汇总好（`instances[].tone`），这里不重推：
 * 同一个状态在 tab 和卡片里必须同色。
 */
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { Icon, StateDot } from "./primitives";
import type { MessagingInstance } from "./types";

export function MessagingTabs({
  instances,
  activeId,
  busy,
  disabled,
  onSelect,
  onAdd,
  onRemove,
}: {
  instances: MessagingInstance[];
  activeId: string;
  busy: boolean;
  disabled: boolean;
  onSelect: (instanceId: string) => void;
  onAdd: () => void;
  onRemove: (instance: MessagingInstance) => void;
}) {
  return (
    <div className="flex min-w-0 items-center gap-1 overflow-x-auto px-3 py-1.5">
      {instances.length ? (
        instances.map((instance) => {
          const active = instance.instance_id === activeId;
          const tone = instance.bot_missing ? "bad" : instance.tone;
          return (
            <div key={instance.instance_id || "default"} className="flex shrink-0 items-center">
              <button
                type="button"
                onClick={() => onSelect(instance.instance_id)}
                aria-current={active ? "true" : undefined}
                title={describe(instance)}
                className={cn(
                  "group flex h-[28px] max-w-[220px] items-center gap-1.5 rounded-md border px-2 transition-colors duration-150 ease-standard",
                  "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
                  active
                    ? "border-line bg-paper shadow-lift"
                    : "border-transparent hover:bg-sunken",
                )}
              >
                <StateDot tone={tone} />
                <span
                  className={cn(
                    "truncate text-secondary",
                    active ? "font-semibold text-ink" : "font-medium text-ink-2",
                  )}
                >
                  {instance.name}
                </span>
                {/*
                  状态文字跟着色点走：只有色点的话，红/黄/灰在小圆点上分不清，
                  而这里恰恰是「哪个机器人现在不能用」的第一现场。
                */}
                <span className="shrink-0 text-label text-muted">
                  {instance.bot_missing ? "机器人已删除" : instance.state_label}
                </span>
              </button>
              {active && !instance.builtin ? (
                <button
                  type="button"
                  onClick={() => onRemove(instance)}
                  disabled={busy}
                  title="移除这个机器人的接入实例"
                  aria-label={`移除「${instance.name}」的接入实例`}
                  className="ml-0.5 flex h-[26px] w-[22px] items-center justify-center rounded-md text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-danger focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25 disabled:opacity-40"
                >
                  <Icon name="close" size={11} />
                </button>
              ) : (
                <span className="w-[2px]" aria-hidden="true" />
              )}
            </div>
          );
        })
      ) : (
        <Skeleton className="h-[28px] w-[120px]" />
      )}

      <button
        type="button"
        onClick={onAdd}
        disabled={busy || disabled}
        className="ml-1 flex h-[28px] shrink-0 items-center gap-1 rounded-md border border-dashed border-line-strong px-2 text-label font-medium text-ink-2 transition-colors duration-150 ease-standard hover:border-muted/45 hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Icon name="plus" size={11} />
        添加机器人
      </button>
    </div>
  );
}

function describe(instance: MessagingInstance): string {
  if (instance.bot_missing) {
    return "这个实例对应的机器人已被删除，配置还留着。可以在这里清掉它。";
  }
  if (instance.builtin) {
    return "默认接入：凭据读 .env 的基础键（一个部署一套飞书/企微应用），"
      + "回话身份由每个渠道上的「绑定机器人」决定。";
  }
  const apps = instance.channels.map((item) => item.name).join("、") || "渠道";
  return `${instance.name} 的独立接入：${apps}各用一套应用，`
    + `凭据是 .env 里带 __${instance.env_suffix} 后缀的那几条。`;
}
