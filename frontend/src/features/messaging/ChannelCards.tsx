/**
 * 三张只读卡片：接入方式、凭据、运行状态。
 *
 * 它们的共同点是「告诉操作者现在是什么状况」，没有输入控件。所以三张卡的
 * 结构统一：一行一句说明 + 一块内容，不给按钮堆动作——动作在每张卡自己的
 * 场景里才出现（复制回调地址、重载连接）。
 */
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useToast } from "@/components/ui/Toast";
import { Section, formatTime, Icon } from "./primitives";
import type { ChannelView } from "./types";

/* ---------------- 接入方式 ---------------- */

export function AccessCard({ channel }: { channel: ChannelView }) {
  const toast = useToast();
  const steps = channel.setup_steps ?? [];
  const callback = channel.callback_url ?? "";

  const copy = async () => {
    if (!callback) return;
    try {
      await navigator.clipboard.writeText(callback);
      toast("回调地址已复制");
    } catch {
      toast("复制失败，请手动选中复制", "error");
    }
  };

  return (
    <Section
      icon="plug"
      title="接入方式"
      sub={
        channel.needs_public_url
          ? "需要在平台上填一个公网 HTTPS 回调地址"
          : "长轮询取消息，不需要公网地址"
      }
      actions={
        channel.docs_url ? (
          <a
            href={channel.docs_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-[26px] items-center gap-1 rounded-md px-2 text-secondary font-medium text-ink-2 transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25"
          >
            官方文档
            <Icon name="external" size={12} />
          </a>
        ) : null
      }
    >
      {steps.length ? (
        <ol className="flex list-none flex-col gap-2">
          {steps.map((step, index) => (
            <li key={index} className="flex gap-2.5 text-secondary leading-relaxed text-ink-2">
              <span
                className="mt-[2px] flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full bg-signal-wash text-label font-semibold text-signal-deep"
                aria-hidden="true"
              >
                {index + 1}
              </span>
              <span className="min-w-0">{step}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-secondary text-muted">这个渠道没有额外的接入步骤。</p>
      )}

      {channel.needs_public_url ? (
        <div className="mt-3">
          <div className="flex items-center gap-1.5">
            <code
              title={callback || "尚未配置 IM_PUBLIC_BASE_URL"}
              className="min-w-0 flex-1 truncate rounded-md border border-line bg-rail px-2 py-1.5 font-mono text-label text-ink-2"
            >
              {callback || "尚未配置 IM_PUBLIC_BASE_URL"}
            </code>
            <Button size="sm" onClick={() => void copy()} disabled={!callback}>
              <Icon name="copy" size={12} />
              复制
            </Button>
          </div>
          <p className="mt-1 text-label text-muted">
            填到平台后台的「事件订阅 / 回调地址」那一栏。改域名要同步改{" "}
            <code className="font-mono">IM_PUBLIC_BASE_URL</code>。
          </p>
        </div>
      ) : null}
    </Section>
  );
}

/* ---------------- 凭据 ---------------- */

/**
 * 这半年最常被问到的一句话是「密钥是不是填错了」。界面上唯一能回答的是
 * 「配没配」，因为密钥永不回显（后端只给 `is_set`）。所以这一列只画状态，
 * 不画值——把 masking 后的半截密钥显示出来只会让人更困惑。
 */
export function CredentialsCard({ channel }: { channel: ChannelView }) {
  const fields = channel.env ?? [];

  return (
    <Section
      icon="key"
      title="凭据"
      sub={
        channel.id === "weixin"
          ? "扫码成功后 token 自动保存；下面都是可选项"
          : "只从环境变量读取，界面不回显明文"
      }
      actions={
        <Badge tone={channel.configured ? "ok" : "warn"}>
          {channel.configured ? "已齐全" : "待配置"}
        </Badge>
      }
    >
      <ul className="flex list-none flex-col divide-y divide-line">
        {fields.map((field) => (
          <li key={field.key} className="flex items-start gap-3 py-2.5 first:pt-0 last:pb-0">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="text-secondary font-medium text-ink">{field.label}</span>
                {field.required && !field.is_set ? (
                  <span className="text-danger" aria-label="必填">
                    *
                  </span>
                ) : null}
              </div>
              {field.help ? (
                <p className="mt-0.5 text-label leading-relaxed text-muted">{field.help}</p>
              ) : null}
              <p
                className="mt-1 truncate font-mono text-label text-muted"
                title={`${field.env_name || field.env}${field.preview ? ` = ${field.preview}` : ""}`}
              >
                {/*
                  显示 env_name 而不是 env：机器人实例的变量名带 __后缀 ，
                  显示裸名会让人照着填进 .env，然后这个实例永远读不到凭据。
                */}
                {field.env_name || field.env}
                {field.preview ? ` · ${field.preview}` : ""}
              </p>
            </div>
            <Badge
              tone={field.is_set ? "ok" : field.required ? "danger" : "neutral"}
              className="mt-0.5"
            >
              {field.is_set ? "已配置" : "未配置"}
            </Badge>
          </li>
        ))}
      </ul>

      <p className="mt-3 rounded-md bg-rail px-2.5 py-2 text-label leading-relaxed text-muted">
        改 <code className="font-mono">.env</code> 后要重启 Worker 才会生效。
        密钥不会出现在这个页面上，也不要把明文贴进聊天或截图。
      </p>
    </Section>
  );
}

/* ---------------- 运行状态 ---------------- */

/**
 * 老页只画了「状态 / 更新 / Worker / 待处理」四行，把 `runtime.detail` 和
 * `error_code` 丢掉了——而那两行恰恰是排障时唯一有用的东西。这里补上，
 * 并且只在有值时出现，不留空行。
 *
 * 这张卡**不放「重载」按钮**：顶栏一直有一个，需要重载的三种状态
 * （待生效 / 启动失败 / 缺凭据）会各自在顶部弹一条带按钮的提示。
 * 三个地方都放同一个动作，用户反而会怀疑它们不一样。
 */
export function RuntimeCard({ channel }: { channel: ChannelView }) {
  const runtime = channel.runtime ?? { state: "", detail: "", error_code: "", updated_at: "", worker_id: "" };
  const backlog = channel.backlog ?? { inbox: 0, outbox: 0 };
  const errorText = [runtime.error_code, runtime.detail].filter(Boolean).join(" · ");

  const tone =
    channel.tone === "good" ? "ok" : channel.tone === "bad" ? "danger" : channel.tone === "muted" ? "neutral" : "warn";

  return (
    <Section
      icon="pulse"
      title="运行状态"
      sub="由 Worker 写入，这个页面只读"
      actions={<Badge tone={tone}>{channel.state_label}</Badge>}
    >
      <dl className="grid grid-cols-[84px_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-secondary">
        <dt className="text-muted">状态</dt>
        <dd className="min-w-0 break-words text-ink-2">
          <span className="font-mono text-label">{channel.state || "—"}</span>
          {channel.reason ? <span className="ml-1.5 text-muted">· {channel.reason}</span> : null}
        </dd>

        <dt className="text-muted">最近更新</dt>
        <dd className="min-w-0 text-ink-2">{formatTime(runtime.updated_at)}</dd>

        <dt className="text-muted">Worker</dt>
        <dd className="truncate font-mono text-label text-ink-2" title={runtime.worker_id}>
          {runtime.worker_id || "—"}
        </dd>

        <dt className="text-muted">待处理</dt>
        <dd className="text-ink-2 tabular-nums">
          入站 {backlog.inbox ?? 0} · 出站 {backlog.outbox ?? 0}
        </dd>

        {errorText ? (
          <>
            <dt className="text-muted">最近错误</dt>
            <dd className="min-w-0 break-words font-mono text-label text-danger">{errorText}</dd>
          </>
        ) : null}
      </dl>
    </Section>
  );
}
