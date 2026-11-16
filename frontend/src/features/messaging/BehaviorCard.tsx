/**
 * 行为：绑哪个机器人、谁可以跟它说话、渠道自己的零散选项。
 *
 * 这一卡是整页唯一有输入的地方，所以三件事必须做对：
 *  1. 任何改动都要能被"看见"——卡片头挂一个「未保存」，顶栏的保存按钮
 *     也是同一个动作，用户在哪半边都不会找不到。
 *  2. 策略下拉的文字是产品语言（允许所有人 / 仅白名单 / 配对批准 / 静默丢弃 /
 *     关闭 / 仅 @ 时响应）。后端只给 id，文字由这本小字典负责，别在前端
 *     重新发明一套说法。
 *  3. 「发送测试」的目标就是下面那个 home_chat_id。没填时要指名道姓地说
 *     "先在「默认会话 ID」里填一个目标"——所以报错文案在 hook 里是从
 *     option.label 读的，不是在组件里写死。
 */
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { FieldLabel, Icon, Section, Select, Switch, TextArea, TextInput } from "./primitives";
import { splitUsers } from "./useChannels";
import type { BotOption, ChannelForm, ChannelView } from "./types";

const POLICY_LABELS: Record<string, string> = {
  open: "允许所有人",
  allowlist: "仅白名单",
  pair: "配对批准",
  ignore: "静默丢弃",
  disabled: "关闭",
  mention: "仅 @ 时响应",
};

export function BehaviorCard({
  channel,
  form,
  bots,
  lockedBot = false,
  dirty,
  saving,
  testing,
  enabled,
  onEdit,
  onEditOption,
  onSave,
  onTest,
}: {
  channel: ChannelView;
  form: ChannelForm;
  bots: BotOption[];
  /** 机器人实例下身份由 tab 决定，这里不给改（改了 tab 名就和实际回话的人脱钩） */
  lockedBot?: boolean;
  dirty: boolean;
  saving: boolean;
  testing: boolean;
  enabled: boolean;
  onEdit: (patch: Partial<ChannelForm>) => void;
  onEditOption: (key: string, value: string) => void;
  onSave: () => void;
  onTest: () => void;
}) {
  const allowCount = splitUsers(form.allowed_users).length;
  const groupsDisabled = channel.capabilities?.group === false;
  const botName =
    bots.find((bot) => bot.bot_id === form.bot_id)?.name || form.bot_id || "未命名机器人";

  return (
    <Section
      icon="bubble"
      title="行为"
      sub="绑定的机器人决定人设、技能与记忆；授权默认拒绝"
      actions={dirty ? <Badge tone="warn">未保存</Badge> : <span className="text-label text-muted">已同步</span>}
    >
      <div className="grid grid-cols-1 gap-x-4 gap-y-3 md:grid-cols-2">
        <FieldLabel
          htmlFor="f-bot"
          className="md:col-span-2"
          label="绑定机器人"
          hint={
            lockedBot
              ? `这个实例就是为「${botName}」建的，身份由上面那排 tab 定，这里不能改；要换人回话请新建一个实例。`
              : "每个 IM 会话开一条独立的 Agent 会话；机器人的人设、技能、连接器会一起带进来。"
          }
        >
          {lockedBot ? (
            <div className="flex h-[30px] items-center gap-1.5 rounded-md border border-line bg-rail px-2.5 text-body">
              <span className="text-muted" aria-hidden="true">
                <Icon name="users" size={12} />
              </span>
              <span className="min-w-0 flex-1 truncate text-ink">{botName}</span>
              <span className="shrink-0 text-label text-muted">由实例固定</span>
            </div>
          ) : (
            <Select
              id="f-bot"
              value={form.bot_id}
              disabled={!enabled}
              onChange={(event) => onEdit({ bot_id: event.target.value })}
            >
              <option value="">不绑定（通用助手）</option>
              {bots.map((bot) => (
                <option key={bot.bot_id} value={bot.bot_id}>
                  {bot.name}
                  {bot.handle ? ` @${bot.handle}` : ""}
                </option>
              ))}
            </Select>
          )}
        </FieldLabel>

        <FieldLabel htmlFor="f-dm" label="私聊策略">
          <Select
            id="f-dm"
            value={form.dm_policy}
            disabled={!enabled}
            onChange={(event) => onEdit({ dm_policy: event.target.value })}
          >
            {(channel.dm_policies ?? []).map((policy) => (
              <option key={policy} value={policy}>
                {POLICY_LABELS[policy] ?? policy}
              </option>
            ))}
          </Select>
        </FieldLabel>

        <FieldLabel
          htmlFor="f-group"
          label="群聊策略"
          hint={groupsDisabled ? "这个渠道不支持群聊。" : undefined}
        >
          <Select
            id="f-group"
            value={form.group_policy}
            disabled={!enabled}
            onChange={(event) => onEdit({ group_policy: event.target.value })}
          >
            {(channel.group_policies ?? []).map((policy) => (
              <option key={policy} value={policy}>
                {POLICY_LABELS[policy] ?? policy}
              </option>
            ))}
          </Select>
        </FieldLabel>

        <FieldLabel
          htmlFor="f-allow"
          className="md:col-span-2"
          label="用户白名单"
          hint={
            allowCount
              ? `已识别 ${allowCount} 个账号；逗号、中文逗号、换行都算分隔。`
              : "留空表示只有配对批准的会话能对话。没在白名单里的私聊会收到 8 位配对码；群聊不发配对码。"
          }
        >
          <TextArea
            id="f-allow"
            value={form.allowed_users}
            disabled={!enabled}
            placeholder="例如：ou_xxx, ou_yyy  （* 表示全部）"
            onChange={(event) => onEdit({ allowed_users: event.target.value })}
          />
        </FieldLabel>

        {(channel.options ?? []).map((option) => {
          const value = form.options[option.key] ?? "";
          const on = value === "1" || value.toLowerCase() === "true";
          return (
            <FieldLabel
              key={option.key}
              htmlFor={`opt-${option.key}`}
              label={option.label}
              hint={option.help || undefined}
              // 会话 ID 是一长串，占半栏会被截成看不懂的样子
              className={cn(option.key === "home_chat_id" && "md:col-span-2")}
            >
              {option.kind === "select" ? (
                <Select
                  id={`opt-${option.key}`}
                  value={value}
                  disabled={!enabled}
                  onChange={(event) => onEditOption(option.key, event.target.value)}
                >
                  {(option.choices ?? []).map((choice) => (
                    <option key={choice.value} value={choice.value}>
                      {choice.label}
                    </option>
                  ))}
                </Select>
              ) : option.kind === "switch" ? (
                <Switch
                  label={on ? "已开启" : "已关闭"}
                  checked={on}
                  disabled={!enabled}
                  onChange={(next) => onEditOption(option.key, next ? "1" : "0")}
                />
              ) : (
                <TextInput
                  id={`opt-${option.key}`}
                  value={value}
                  disabled={!enabled}
                  // 不拿 help 当 placeholder：它已经作为 hint 显示在下面了，
                  // 两处同一句话会让人以为这是两个不同的字段；也不编示例值，
                  // 每个渠道的 ID 长得都不一样。
                  className="font-mono"
                  onChange={(event) => onEditOption(option.key, event.target.value)}
                />
              )}
            </FieldLabel>
          );
        })}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <span className="mr-auto text-label leading-relaxed text-muted">
          保存后不会立刻生效：Worker 下一个节拍（几秒内）发现配置指纹变了才会重建连接。
        </span>
        <Button onClick={onTest} disabled={!enabled || testing}>
          {testing ? "发送中…" : "发送测试消息"}
        </Button>
        <Button variant="primary" onClick={onSave} disabled={!enabled || saving}>
          {saving ? "保存中…" : "保存配置"}
        </Button>
      </div>
    </Section>
  );
}
