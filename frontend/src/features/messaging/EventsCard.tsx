/**
 * 最近事件（审计尾巴）。
 *
 * 只记状态与哈希、不存正文——这一点要写在卡片副标题上，否则运维会以为
 * "事件里能翻到用户说了什么"。表格里的会话与消息都是哈希/截断 ID，
 * 悬停能看到完整值。
 */
import { Badge } from "@/components/ui/Badge";
import { Section, formatTime } from "./primitives";
import type { ChannelEvent } from "./types";

export function EventsCard({ events }: { events: ChannelEvent[] }) {
  return (
    <Section
      icon="clock"
      title="最近事件"
      sub="只记状态与哈希，不保存消息正文"
      actions={<Badge>{events.length} 条</Badge>}
    >
      {events.length ? (
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full table-fixed border-collapse text-secondary">
            <colgroup>
              <col style={{ width: "96px" }} />
              <col style={{ width: "52px" }} />
              <col />
              <col style={{ width: "96px" }} />
              <col style={{ width: "128px" }} />
            </colgroup>
            <thead>
              <tr className="border-b border-line">
                <th scope="col" className="px-1 py-1.5 text-left text-label font-medium text-muted">
                  时间
                </th>
                <th scope="col" className="px-1 py-1.5 text-left text-label font-medium text-muted">
                  方向
                </th>
                <th scope="col" className="px-1 py-1.5 text-left text-label font-medium text-muted">
                  状态
                </th>
                <th scope="col" className="px-1 py-1.5 text-left text-label font-medium text-muted">
                  会话
                </th>
                <th scope="col" className="px-1 py-1.5 text-left text-label font-medium text-muted">
                  消息
                </th>
              </tr>
            </thead>
            <tbody>
              {events.map((event, index) => {
                const failed = Boolean(event.error_code) || event.status === "failed";
                return (
                  <tr
                    key={`${event.created_at}:${event.message_id}:${index}`}
                    className="border-b border-line last:border-0"
                  >
                    <td className="truncate px-1 py-1.5 tabular-nums text-ink-2">
                      {formatTime(event.created_at)}
                    </td>
                    <td className="px-1 py-1.5 text-ink-2">
                      {event.direction === "out" ? "出站" : "入站"}
                    </td>
                    <td className="truncate px-1 py-1.5">
                      <span className={failed ? "text-danger" : "text-ink-2"}>{event.status || "—"}</span>
                      {event.error_code ? (
                        <span className="ml-1 font-mono text-label text-danger">{event.error_code}</span>
                      ) : null}
                    </td>
                    <td
                      className="truncate px-1 py-1.5 font-mono text-label text-muted"
                      title={event.chat_id_hash}
                    >
                      {event.chat_id_hash || "—"}
                    </td>
                    <td
                      className="truncate px-1 py-1.5 font-mono text-label text-muted"
                      title={event.message_id}
                    >
                      {event.message_id ? event.message_id.slice(0, 18) : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-secondary text-muted">
          还没有事件。这个渠道收到第一条消息、或者发出第一条回复之后，记录会出现在这里。
        </p>
      )}
    </Section>
  );
}
