/**
 * 会话身份：这个渠道下最近聊过的会话，各自归哪个机器人。
 *
 * 为什么需要这一卡：渠道上的「绑定机器人」是**一条线**（一个渠道一个人格），
 * 而真实的群是**一堆点**——同一个飞书应用被拉进十个群，客服群和内部群常常
 * 该由不同的人设回答。凭据是应用级的，人格是会话级的，两件事本来就不在一层。
 *
 * 两条不能违反的规则：
 *  1. 下拉里只有两种值：「跟随渠道（X）」和具体某个机器人。空串代表跟随，
 *     不是"不绑定"——渠道没绑机器人时，跟随的结果就是通用助手。
 *  2. 改完不立刻换会话。真正换是对方下一条消息进来时 bridge 发现人格与生效
 *     身份不一致才滚一代；所以文案说"下一条消息起"，不说"已切换"。
 */
import { Badge } from "@/components/ui/Badge";
import { Section, Select, formatTime } from "./primitives";
import type { BotOption, SessionRow } from "./types";

const CHAT_TYPE_LABEL: Record<string, string> = {
  dm: "私聊",
  group: "群聊",
  channel: "频道",
};

export function SessionsCard({
  sessions,
  bots,
  channelBotId,
  phase,
  error,
  busy,
  onBind,
}: {
  sessions: SessionRow[];
  bots: BotOption[];
  /** 渠道默认绑的是谁，用来给「跟随渠道」那一项写清结果 */
  channelBotId: string;
  phase: "loading" | "ready" | "error";
  error: string;
  busy: boolean;
  onBind: (chatId: string, botId: string) => void;
}) {
  const channelBot = bots.find((bot) => bot.bot_id === channelBotId);
  const followLabel = `跟随渠道（${channelBot?.name ?? "不绑定 · 通用助手"}）`;
  const overridden = sessions.filter((row) => row.bot_source === "session").length;

  return (
    <Section
      icon="users"
      title="会话身份"
      sub="给单个群或私聊指定机器人；改完对方下一条消息生效，旧对话仍留在这台机器上"
      actions={
        sessions.length ? (
          <Badge tone={overridden ? "warn" : undefined}>
            {overridden ? `${overridden} / ${sessions.length} 段单独指定` : `${sessions.length} 段会话`}
          </Badge>
        ) : null
      }
    >
      {phase === "error" ? (
        <p className="text-secondary text-danger">会话列表读不到：{error}</p>
      ) : phase === "loading" && !sessions.length ? (
        <p className="text-secondary text-muted">正在读取最近会话…</p>
      ) : sessions.length ? (
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full table-fixed border-collapse text-secondary">
            <colgroup>
              <col />
              <col style={{ width: "64px" }} />
              <col style={{ width: "220px" }} />
              <col style={{ width: "120px" }} />
            </colgroup>
            <thead>
              <tr className="border-b border-line">
                <th scope="col" className="px-1 py-1.5 text-left text-label font-medium text-muted">
                  会话
                </th>
                <th scope="col" className="px-1 py-1.5 text-left text-label font-medium text-muted">
                  类型
                </th>
                <th scope="col" className="px-1 py-1.5 text-left text-label font-medium text-muted">
                  由谁回答
                </th>
                <th scope="col" className="px-1 py-1.5 text-left text-label font-medium text-muted">
                  最近活跃
                </th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((row) => {
                const known = bots.some((bot) => bot.bot_id === row.bot_id);
                return (
                  <tr
                    key={`${row.platform}:${row.instance}:${row.chat_id}`}
                    className="border-b border-line last:border-0"
                  >
                    <td
                      className="truncate px-1 py-1.5 font-mono text-label text-ink-2"
                      title={`${row.chat_id}${row.generation > 1 ? `（第 ${row.generation} 段对话）` : ""}`}
                    >
                      {row.chat_id}
                    </td>
                    <td className="px-1 py-1.5 text-ink-2">
                      {CHAT_TYPE_LABEL[row.chat_type] ?? row.chat_type}
                    </td>
                    <td className="px-1 py-1.5">
                      <Select
                        aria-label={`${row.chat_id} 由谁回答`}
                        value={row.bot_id}
                        disabled={busy}
                        onChange={(event) => onBind(row.chat_id, event.target.value)}
                      >
                        <option value="">{followLabel}</option>
                        {/* 覆盖指向的机器人被删了：不能悄悄显示成"跟随渠道"，
                            要让人看见这条绑定已经失效 */}
                        {row.bot_id && !known ? (
                          <option value={row.bot_id}>（已删除的机器人）</option>
                        ) : null}
                        {bots.map((bot) => (
                          <option key={bot.bot_id} value={bot.bot_id}>
                            {bot.name}
                            {bot.handle ? ` @${bot.handle}` : ""}
                          </option>
                        ))}
                      </Select>
                    </td>
                    <td className="truncate px-1 py-1.5 tabular-nums text-ink-2">
                      {formatTime(row.updated_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mt-2 text-label leading-relaxed text-muted">
            这里只列「和智能体聊过」的会话。要按 @名字 指定，也可以在会话里直接发
            <span className="mx-1 font-mono">/bind 机器人名</span>。
          </p>
        </div>
      ) : (
        <p className="text-secondary text-muted">
          还没有会话。对方先在这个渠道里发一条消息，这里就会列出来。
        </p>
      )}
    </Section>
  );
}
