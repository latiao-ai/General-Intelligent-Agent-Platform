/**
 * 个人微信扫码登录。
 *
 * 这一块最容易让人困惑的是"登录的是谁"：不是本人微信号，而是一个 iLink bot
 * 身份。所以卡片副标题必须写清楚，否则用户会以为扫完就能用自己的号发消息。
 *
 * 二维码是后端渲染好的 SVG data URI（iLink 返回的是一串 liteapp 链接，
 * 不是图片，必须自己画矩阵）。这里只负责放进 <img>，不做任何解码。
 */
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Section, StateDot } from "./primitives";
import type { ChannelView, WeixinLoginView } from "./types";

const STATUS_TEXT: Record<string, string> = {
  starting: "正在获取二维码",
  wait: "等待扫码",
  scaned: "已扫码，等待手机上确认",
  confirmed: "登录成功",
  expired: "二维码已过期",
  error: "登录失败",
  cancelled: "已取消",
  idle: "尚未登录",
};

const RUNNING = new Set(["starting", "wait", "scaned"]);

export function WeixinLoginCard({
  channel,
  view,
  polling,
  busy,
  onStart,
  onCancel,
  onLogout,
}: {
  channel: ChannelView;
  view: WeixinLoginView | null;
  polling: boolean;
  busy: boolean;
  onStart: () => void;
  onCancel: () => void;
  onLogout: () => void;
}) {
  const savedIn = Boolean(channel.login?.logged_in);
  const status = view?.status ?? (savedIn ? "confirmed" : "idle");
  const loggedIn = status === "confirmed" || savedIn;
  const qr = view?.qr_svg ?? "";
  const scanning = polling || RUNNING.has(status);
  const tone = loggedIn ? "good" : status === "expired" || status === "error" ? "bad" : scanning ? "warn" : "muted";

  const note =
    view?.message ||
    (loggedIn
      ? "Worker 下一次同步会建立长轮询。会话过期（errcode=-14）时需要重新扫码。"
      : "扫码成功后 token 会自动保存；这个渠道不需要公网地址。");

  const minutes = view && view.expires_in > 0 ? Math.max(1, Math.ceil(view.expires_in / 60)) : 0;

  return (
    <Section
      icon="pulse"
      title="扫码登录"
      sub="登录的是 iLink bot 身份，不是本人的微信号；群聊基本收不到消息"
      actions={
        <Badge tone={loggedIn ? "ok" : "warn"}>{loggedIn ? "已登录" : "未登录"}</Badge>
      }
    >
      <div className="flex flex-col gap-4 sm:flex-row">
        <div className="flex h-[168px] w-[168px] shrink-0 items-center justify-center overflow-hidden rounded-lg border border-line bg-paper p-2">
          {qr ? (
            <img
              src={qr}
              alt="微信登录二维码，用手机微信扫一扫"
              className="h-full w-full object-contain"
            />
          ) : (
            <p className="px-3 text-center text-label leading-relaxed text-muted">
              {loggedIn ? (
                <>
                  <span className="block">已登录</span>
                  <span className="block font-mono">
                    {view?.account_id || channel.login?.account_id || ""}
                  </span>
                </>
              ) : (
                <>
                  {/* 手动断行：168px 的框里让浏览器自己折，会在「二维码」中间断开 */}
                  <span className="block">点「扫码登录」</span>
                  <span className="block">然后用微信扫</span>
                </>
              )}
            </p>
          )}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <StateDot tone={tone} size={7} />
            <span className="text-body font-semibold text-ink">{STATUS_TEXT[status] ?? status}</span>
            {scanning && !loggedIn ? (
              <span className="text-label text-muted">正在等待手机端确认…</span>
            ) : null}
          </div>

          <p className="mt-1.5 text-secondary leading-relaxed text-muted">{note}</p>

          <div className="mt-2 space-y-0.5 font-mono text-label text-muted">
            {loggedIn && (view?.account_id || channel.login?.account_id) ? (
              <p className="truncate" title={view?.account_id || channel.login?.account_id}>
                account_id · {view?.account_id || channel.login?.account_id}
              </p>
            ) : null}
            {scanning && minutes ? <p>二维码剩余约 {minutes} 分钟</p> : null}
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={onStart} disabled={busy || scanning}>
              {busy ? "获取中…" : loggedIn ? "重新扫码" : "扫码登录"}
            </Button>
            {scanning ? (
              <Button variant="ghost" onClick={onCancel}>
                取消
              </Button>
            ) : null}
            {savedIn ? (
              <Button variant="danger" onClick={onLogout} disabled={busy}>
                退出登录
              </Button>
            ) : null}
          </div>
        </div>
      </div>
    </Section>
  );
}
