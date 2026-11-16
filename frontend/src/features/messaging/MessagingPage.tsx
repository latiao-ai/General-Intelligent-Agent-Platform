/**
 * 消息平台 —— 总装配。
 *
 * 骨架与 bots 页一致（左轨 + 顶栏 + 一列卡片），因为操作者在两个页面之间来回
 * 跳：左边永远是"有哪些"、顶栏永远是"现在是哪个/能对它做什么"、正文永远
 * 从上往下讲完一件事。位置一致比好看重要。
 *
 * 三个只在页面层做的决定：
 *  1. 切换渠道、撤销授权、退出登录都要二次确认，且用弹窗而不是 `confirm()`：
 *     原生确认框在 Electron / PWA 里样式不可控，也拦不住自动化的误点。
 *  2. 提示条（banner）按「先说全局、再说这个渠道」排序。全局问题（能力没开、
 *     网关没跑）会同时影响三个渠道，说一遍就够，不用每个渠道各说一遍。
 *  3. 状态色只用在 dot / badge / 错误文字上。正文一律 ink，别让整页发红。
 */
import { useState, type ReactNode } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { AccessCard, CredentialsCard, RuntimeCard } from "./ChannelCards";
import { BehaviorCard } from "./BehaviorCard";
import { EventsCard } from "./EventsCard";
import { FieldLabel, Icon, Select, Switch } from "./primitives";
import { MessagingRail } from "./MessagingRail";
import { MessagingTabs } from "./MessagingTabs";
import { PairingCard } from "./PairingCard";
import { SessionsCard } from "./SessionsCard";
import { useChannels } from "./useChannels";
import { useWeixinLogin } from "./useWeixinLogin";
import { WeixinLoginCard } from "./WeixinLoginCard";
import type { ChannelView, CreateInstanceResult, MessagingInstance, PairingRow } from "./types";

/** 状态点 / 徽章的 tone → Badge 的 tone。后端只给 good/warn/bad/muted 四档。 */
function badgeTone(tone: string): "ok" | "warn" | "danger" | "neutral" {
  if (tone === "good") return "ok";
  if (tone === "bad") return "danger";
  if (tone === "muted") return "neutral";
  return "warn";
}

export function MessagingPage() {
  const channels = useChannels();
  const [pendingChannel, setPendingChannel] = useState<string | null>(null);
  const [pendingInstance, setPendingInstance] = useState<string | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<PairingRow | null>(null);
  const [logoutOpen, setLogoutOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<MessagingInstance | null>(null);
  const [created, setCreated] = useState<CreateInstanceResult | null>(null);
  const [addBotId, setAddBotId] = useState("");

  /** 已经开了独立接入的机器人不能再开一次（后端也会 409，但别让他先选到）。 */
  const availableBots = channels.bots.filter(
    (bot) => !channels.instances.some((item) => item.instance_id === bot.bot_id),
  );

  const channel = channels.detail;
  const isWeixin = channel?.id === "weixin";
  /** 机器人实例下，回话身份是固定的（实例 = 机器人），不给下拉。 */
  const lockedBot = Boolean(channels.instanceId);
  const login = useWeixinLogin(isWeixin, () => channels.sync());

  /**
   * 「这个渠道现在是以谁的身份回话」——页面级作用域标识。
   *
   * 取 `form` 而不是 `channel`：机器人下拉是这一页唯一会改变"谁在回话"的字段，
   * 改完还没保存时，顶栏必须显示**即将生效**的那一个，否则用户存完回头一看
   * 顶栏没变，会以为自己没点成功。
   */
  const boundBotId = channels.form?.bot_id ?? channel?.bot_id ?? "";
  const boundBot = boundBotId ? channels.bots.find((bot) => bot.bot_id === boundBotId) : undefined;
  const botLabel = boundBotId ? (boundBot?.name ?? "机器人（花名册未加载）") : "不绑定 · 通用助手";

  /**
   * 点顶栏那枚身份 pill 去哪：
   *  - 默认实例：去「行为」卡改绑定（那里是唯一能改的地方）；
   *  - 机器人实例：身份改不了，真正要动的是凭据，所以去凭据卡。
   */
  const jumpToBot = () => {
    if (lockedBot) {
      document.getElementById("card-credentials")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    document.getElementById("card-behavior")?.scrollIntoView({ behavior: "smooth", block: "start" });
    // 卡片滑到位置再聚焦，否则浏览器会为了露出焦点把页面再滚一次
    window.setTimeout(() => document.getElementById("f-bot")?.focus({ preventScroll: true }), 320);
  };

  const requestSelect = (id: string) => {
    if (id === channels.selected) return;
    if (channels.dirty) {
      setPendingChannel(id);
      return;
    }
    channels.select(id);
  };

  /** 换 tab 也是换作用域：有未保存的改动时同样要拦一次。 */
  const requestInstance = (next: string) => {
    if (next === channels.instanceId) return;
    if (channels.dirty) {
      setPendingInstance(next);
      return;
    }
    channels.selectInstance(next);
  };

  return (
    <div className="flex h-screen overflow-hidden bg-paper">
      <MessagingRail
        channels={channels.channels}
        selected={channels.selected}
        bots={channels.bots}
        showBot={!channels.instanceId}
        gatewayAlive={channels.gatewayAlive}
        loading={channels.listPhase === "loading"}
        onSelect={requestSelect}
        onRefresh={() => void channels.refresh()}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        {/*
          tab 条固定在顶部、不随正文滚动：它是「现在配的是谁」的唯一常驻提示。
          一旦滚走，用户低头看图、抬头改值，中间那几步就没有参照物了。
        */}
        <div className="shrink-0 border-b border-line bg-paper">
          <MessagingTabs
            instances={channels.instances}
            activeId={channels.instanceId}
            busy={channels.busy === "instance"}
            disabled={!channels.messagingEnabled}
            onSelect={requestInstance}
            onAdd={() => {
              setAddBotId("");
              setAddOpen(true);
            }}
            onRemove={(instance) => setRemoveTarget(instance)}
          />
        </div>
        <header className="flex min-h-[60px] shrink-0 flex-wrap items-center gap-2.5 border-b border-line px-4 py-2">
          {channel ? (
            <>
              <span
                className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-lg bg-signal-wash text-body font-semibold text-signal-deep"
                aria-hidden="true"
              >
                {channel.name.slice(0, 1)}
              </span>
              <div className="min-w-0 flex-1">
                <h1 className="flex items-center gap-1.5 text-title font-semibold text-ink">
                  <span className="truncate">{channel.name}</span>
                  <Badge tone={badgeTone(channel.tone)}>{channel.state_label}</Badge>
                  {channels.dirty ? <Badge tone="warn">未保存</Badge> : null}
                </h1>
                <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-label">
                  <button
                    type="button"
                    onClick={jumpToBot}
                    title={
                      lockedBot
                        ? `「${botLabel}」是这一排 tab 定的身份，不能在这里改；点一下去补它的凭据`
                        : boundBotId
                          ? `这个渠道收到的消息由「${botLabel}」作答${boundBot?.handle ? ` （@${boundBot.handle}）` : ""}；点一下去「行为」里改`
                          : "没有绑定机器人：消息会交给通用助手，没有人设、技能和记忆；点一下去绑定"
                    }
                    className={cn(
                      "inline-flex h-[19px] shrink-0 items-center gap-1 rounded-full border px-1.5",
                      "transition-colors duration-150 ease-standard",
                      "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
                      boundBotId
                        ? "border-line bg-paper text-ink-2 hover:border-line-strong hover:text-ink"
                        : "border-warn/40 bg-warn-wash text-warn hover:border-warn/60",
                    )}
                  >
                    <Icon name="users" size={11} />
                    <span className="max-w-[180px] truncate">{botLabel}</span>
                  </button>
                  <span className="truncate text-muted" title={channel.tagline}>
                    {channel.tagline}
                  </span>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Switch
                  label="启用"
                  busy={channels.busy === "enable"}
                  checked={channels.enablePending ?? Boolean(channel.enabled)}
                  disabled={!channels.messagingEnabled}
                  onChange={(next) => void channels.setEnabled(next)}
                />
                <Button
                  size="sm"
                  onClick={() => void channels.reload()}
                  disabled={!channels.messagingEnabled || channels.busy === "reload"}
                >
                  <Icon name="refresh" size={12} />
                  {channels.busy === "reload" ? "请求中…" : "重载"}
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => void channels.save()}
                  disabled={!channels.messagingEnabled || channels.busy === "save"}
                >
                  {channels.busy === "save" ? "保存中…" : "保存配置"}
                </Button>
              </div>
            </>
          ) : (
            <>
              <Skeleton className="h-[30px] w-[30px] rounded-lg" />
              <div className="flex-1 space-y-1.5">
                <Skeleton className="h-3.5 w-32" />
                <Skeleton className="h-2.5 w-56" />
              </div>
            </>
          )}
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain bg-rail">
          <div className="mx-auto flex w-full max-w-[880px] flex-col gap-3 px-4 py-4">
            <Banners channel={channel} channels={channels} onReload={() => void channels.reload()} />

            {channels.listPhase === "error" && !channels.channels.length ? (
              <PanelError
                title="渠道清单读不出来"
                message={channels.listError}
                onRetry={() => void channels.sync()}
              />
            ) : channels.detailPhase === "error" ? (
              <PanelError
                title="这个渠道的详情读不出来"
                message={channels.detailError}
                onRetry={() => void channels.reloadDetail(channels.selected, channels.instanceId)}
              />
            ) : !channel || !channels.form ? (
              <CanvasSkeleton />
            ) : (
              <>
                <AccessCard channel={channel} />
                <div id="card-credentials" className="scroll-mt-3">
                  <CredentialsCard channel={channel} />
                </div>
                {isWeixin ? (
                  <WeixinLoginCard
                    channel={channel}
                    view={login.view}
                    polling={login.polling}
                    busy={login.busy}
                    onStart={() => void login.start()}
                    onCancel={() => void login.cancel()}
                    onLogout={() => setLogoutOpen(true)}
                  />
                ) : null}
                <div id="card-behavior" className="scroll-mt-3">
                  <BehaviorCard
                    channel={channel}
                    form={channels.form}
                    bots={channels.bots}
                    lockedBot={lockedBot}
                    dirty={channels.dirty}
                    saving={channels.busy === "save"}
                    testing={channels.busy === "test"}
                    enabled={channels.messagingEnabled}
                    onEdit={channels.edit}
                    onEditOption={channels.editOption}
                    onSave={() => void channels.save()}
                    onTest={() => void channels.sendTest()}
                  />
                </div>
                <RuntimeCard channel={channel} />
                <SessionsCard
                  sessions={channels.sessions}
                  bots={channels.sessionBots}
                  channelBotId={channels.channelBotId}
                  phase={channels.sessionsPhase}
                  error={channels.sessionsError}
                  busy={channels.busy === "session"}
                  onBind={(chatId, botId) => void channels.bindChat(chatId, botId)}
                />
                <PairingCard
                  pairing={channel.pairing}
                  busy={channels.busy === "pair"}
                  onApprove={channels.approve}
                  onRevoke={(row) => setRevokeTarget(row)}
                />
                <EventsCard events={channel.events ?? []} />

                <p className="px-1 pt-1 pb-4 text-label leading-relaxed text-muted">
                  收发消息的是 Worker 进程里的网关，不是这个页面。关掉页面不会断线；
                  但 Worker 停了就一条也发不出去。
                </p>
              </>
            )}
          </div>
        </div>
      </main>

      {/* ---- 未保存就切渠道 ---- */}
      <Dialog
        open={Boolean(pendingChannel)}
        onClose={() => setPendingChannel(null)}
        title="还有没保存的修改"
        description="切换渠道会丢掉它们"
        className="max-w-[480px]"
      >
        <p className="text-body leading-relaxed text-ink-2">
          「{channel?.name}」这条渠道（回话身份：{botLabel}）上有改动还没保存。继续切换的话这些改动会消失，
          服务端仍是上一次保存的值。
        </p>
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
          <Button onClick={() => setPendingChannel(null)}>继续编辑</Button>
          <Button
            onClick={() => {
              const target = pendingChannel;
              setPendingChannel(null);
              if (target) channels.select(target);
            }}
            className="border-warn/40 text-warn hover:border-warn/60 hover:bg-warn-wash"
          >
            放弃改动并切换
          </Button>
        </div>
      </Dialog>

      {/* ---- 未保存就切 tab ---- */}
      <Dialog
        open={pendingInstance !== null}
        onClose={() => setPendingInstance(null)}
        title="还有没保存的修改"
        description="换一个机器人会丢掉它们"
        className="max-w-[480px]"
      >
        <p className="text-body leading-relaxed text-ink-2">
          当前「{channels.activeInstance?.name ?? "默认"}」下有改动还没保存。切换 tab 会换掉整套
          渠道配置和凭据来源，这些改动会消失，服务端仍是上一次保存的值。
        </p>
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
          <Button onClick={() => setPendingInstance(null)}>继续编辑</Button>
          <Button
            onClick={() => {
              const target = pendingInstance;
              setPendingInstance(null);
              if (target !== null) channels.selectInstance(target);
            }}
            className="border-warn/40 text-warn hover:border-warn/60 hover:bg-warn-wash"
          >
            放弃改动并切换
          </Button>
        </div>
      </Dialog>

      {/* ---- 添加机器人实例 ---- */}
      <Dialog
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title="给一个机器人开独立接入"
        description="它会拥有自己的一套 IM 应用与凭据"
        className="max-w-[520px]"
      >
        {availableBots.length ? (
          <>
            <FieldLabel
              htmlFor="f-new-bot"
              label="选一个机器人"
              hint="这个机器人以后用单独的飞书/企微应用收发消息，和「默认」那套互不影响。"
            >
              <Select
                id="f-new-bot"
                value={addBotId}
                onChange={(event) => setAddBotId(event.target.value)}
              >
                <option value="">请选择…</option>
                {availableBots.map((bot) => (
                  <option key={bot.bot_id} value={bot.bot_id}>
                    {bot.name}
                    {bot.handle ? ` @${bot.handle}` : ""}
                  </option>
                ))}
              </Select>
            </FieldLabel>
            <p className="mt-3 rounded-md bg-rail px-2.5 py-2 text-label leading-relaxed text-muted">
              建完会立刻告诉你需要在 <code className="font-mono">.env</code> 里补哪几条变量
              （形如 <code className="font-mono">FEISHU_APP_ID__XZS</code>）。这些变量没填之前，
              实例状态会停在「待配置」——<b className="font-medium text-ink-2">不会</b>偷偷回落到默认那套应用。
            </p>
            <div className="mt-4 flex items-center justify-end gap-2">
              <Button onClick={() => setAddOpen(false)}>取消</Button>
              <Button
                variant="primary"
                disabled={!addBotId || channels.busy === "instance"}
                onClick={() => {
                  void (async () => {
                    const result = await channels.createInstance(addBotId);
                    if (result) {
                      setAddOpen(false);
                      setCreated(result);
                    }
                  })();
                }}
              >
                {channels.busy === "instance" ? "创建中…" : "创建实例"}
              </Button>
            </div>
          </>
        ) : (
          <>
            <p className="text-body leading-relaxed text-ink-2">
              没有可选的机器人：要么花名册是空的，要么每个机器人都已经有独立接入了。
            </p>
            <p className="mt-2 text-label leading-relaxed text-muted">
              先去「机器人」页建一个（一个机器人就是一个回话身份：人设、技能、记忆都挂在它身上），
              再回来给它开接入。
            </p>
            <div className="mt-4 flex items-center justify-end gap-2">
              <Button onClick={() => setAddOpen(false)}>知道了</Button>
            </div>
          </>
        )}
      </Dialog>

      {/* ---- 建完之后：要补哪些环境变量 ---- */}
      <Dialog
        open={Boolean(created)}
        onClose={() => setCreated(null)}
        title={`「${created?.instance.name ?? ""}」的接入已开好`}
        description="还差几步才能真正收发消息"
        className="max-w-[520px]"
      >
        <p className="text-body leading-relaxed text-ink-2">
          在平台后台给它<b className="font-semibold text-ink">单独建一个应用</b>，
          然后把这几个变量写进 <code className="rounded-sm bg-ink/[0.06] px-1 font-mono text-label">.env</code>，
          重启 Worker：
        </p>
        <ul className="mt-3 flex list-none flex-col divide-y divide-line rounded-md border border-line">
          {(created?.env_required ?? []).map((item) => (
            <li key={item.env} className="flex items-center gap-2 px-2.5 py-2">
              <code className="min-w-0 flex-1 truncate font-mono text-label text-ink" title={item.env}>
                {item.env}
              </code>
              <span className="shrink-0 text-label text-muted">
                {item.channel_name} · {item.label}
              </span>
            </li>
          ))}
        </ul>
        <p className="mt-3 text-label leading-relaxed text-muted">
          回调地址和默认实例是同一个（<code className="font-mono">/webhook/feishu</code>），
          服务端靠验签判断这条消息属于哪个应用，不用另开路径。
        </p>
        <div className="mt-4 flex items-center justify-end gap-2">
          <Button variant="primary" onClick={() => setCreated(null)}>
            知道了
          </Button>
        </div>
      </Dialog>

      {/* ---- 移除实例 ---- */}
      <Dialog
        open={Boolean(removeTarget)}
        onClose={() => setRemoveTarget(null)}
        title={`移除「${removeTarget?.name ?? ""}」的接入？`}
        className="max-w-[480px]"
      >
        <p className="text-body leading-relaxed text-ink-2">
          会删掉这个机器人在飞书/企微上的配置与运行状态，Worker 下一个节拍就会停掉它的连接。
        </p>
        <p className="mt-2 text-label leading-relaxed text-muted">
          <b className="font-medium text-ink-2">不会删：</b>
          <code className="font-mono">.env</code> 里那几条凭据、已经聊过的会话记录。
          想彻底清干净，把凭据也一起删掉。
        </p>
        <div className="mt-4 flex items-center justify-end gap-2">
          <Button onClick={() => setRemoveTarget(null)}>取消</Button>
          <Button
            variant="danger"
            onClick={() => {
              const target = removeTarget;
              setRemoveTarget(null);
              if (target) void channels.removeInstance(target.instance_id);
            }}
          >
            移除
          </Button>
        </div>
      </Dialog>

      {/* ---- 撤销授权 ---- */}
      <Dialog
        open={Boolean(revokeTarget)}
        onClose={() => setRevokeTarget(null)}
        title="撤销这个人的授权？"
        className="max-w-[480px]"
      >
        <p className="text-body leading-relaxed text-ink-2">
          撤销后
          <b className="font-semibold text-ink"> {revokeTarget?.user_id} </b>
          不能再通过 {revokeTarget?.platform} 驱动 Agent，我们会给他发一条「授权已撤销」。
          想要再放行，需要他重新发消息拿配对码。
        </p>
        <div className="mt-4 flex items-center justify-end gap-2">
          <Button onClick={() => setRevokeTarget(null)}>取消</Button>
          <Button
            variant="danger"
            onClick={() => {
              const row = revokeTarget;
              setRevokeTarget(null);
              if (row) void channels.revoke(row);
            }}
          >
            撤销授权
          </Button>
        </div>
      </Dialog>

      {/* ---- 退出个人微信 ---- */}
      <Dialog
        open={logoutOpen}
        onClose={() => setLogoutOpen(false)}
        title="退出个人微信登录？"
        className="max-w-[480px]"
      >
        <p className="text-body leading-relaxed text-ink-2">
          会清掉本地保存的 token，Worker 随之停掉这个渠道。已经产生的会话记录不会删除，
          重新扫码后还能继续用原来的会话。
        </p>
        <div className="mt-4 flex items-center justify-end gap-2">
          <Button onClick={() => setLogoutOpen(false)}>取消</Button>
          <Button
            variant="danger"
            onClick={() => {
              setLogoutOpen(false);
              void login.logout();
            }}
          >
            退出登录
          </Button>
        </div>
      </Dialog>
    </div>
  );
}

/* ==========================================================================
 * 顶部提示条
 * ======================================================================== */

function Banners({
  channel,
  channels,
  onReload,
}: {
  channel: ChannelView | null;
  channels: ReturnType<typeof useChannels>;
  onReload: () => void;
}) {
  if (!channels.messagingEnabled) {
    return (
      <Banner tone="danger" icon="shield" title="消息平台能力未开放">
        部署配置里是 <Code>MESSAGING_ENABLED=false</Code>。改成 true 之后重启 API 与 Worker，
        这一页的所有开关才会生效。
      </Banner>
    );
  }

  if (!channels.gatewayAlive) {
    return (
      <Banner tone="warn" icon="pulse" title="消息网关没在运行">
        渠道配置可以照常保存，但现在一条消息也收发不了。启动执行器之后才会生效：
        <Code>python -m scripts.run_worker</Code>。
      </Banner>
    );
  }

  if (!channel) return null;

  if (channel.enabled && !channel.configured) {
    return (
      <Banner tone="warn" icon="key" title="还缺少接入信息">
        缺 <b className="font-medium text-ink">{channel.missing.join("、") || "凭据"}</b>。
        补进 <Code>.env</Code> 之后重启 Worker，或者保存后点「重载」。
      </Banner>
    );
  }

  if (channel.enabled && (channel.state === "startup_failed" || channel.state === "fatal")) {
    return (
      <Banner tone="danger" icon="shield" title="上次启动失败" action={<ReloadButton onReload={onReload} />}>
        {channel.runtime?.error_code ? (
          <Code>{channel.runtime.error_code}</Code>
        ) : null}
        {channel.runtime?.detail || channel.reason || "Worker 没能建立连接，修好配置后点重载。"}
      </Banner>
    );
  }

  if (channel.enabled && channel.state === "pending_restart") {
    return (
      <Banner tone="warn" icon="refresh" title="配置已保存，等待生效" action={<ReloadButton onReload={onReload} />}>
        {channel.reason || "Worker 下一个节拍会重建连接。"}
      </Banner>
    );
  }

  if (!channel.enabled) {
    return (
      <Banner tone="warn" icon="pulse" title="这个渠道现在是停用的">
        打开右上角的「启用」开关就会立刻保存并让 Worker 重连；凭据没配齐的话，状态会停在「待配置」。
      </Banner>
    );
  }

  return null;
}

function Banner({
  tone,
  icon,
  title,
  children,
  action,
}: {
  tone: "warn" | "danger" | "ok";
  icon: string;
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  const skin = {
    warn: "border-warn/30 bg-warn-wash text-warn",
    danger: "border-danger/30 bg-danger-wash text-danger",
    ok: "border-ok/30 bg-ok-wash text-ok",
  }[tone];

  return (
    <div
      role="status"
      className={cn("flex items-start gap-2.5 rounded-lg border px-3 py-2.5", skin)}
    >
      <span className="mt-[2px]" aria-hidden="true">
        <Icon name={icon} />
      </span>
      <p className="min-w-0 flex-1 text-secondary leading-relaxed text-ink-2">
        <b className="font-semibold text-ink">{title}</b>
        <span className="mx-1.5 text-muted">·</span>
        {children}
      </p>
      {action}
    </div>
  );
}

function ReloadButton({ onReload }: { onReload: () => void }) {
  return (
    <Button size="sm" onClick={onReload} className="shrink-0">
      重载
    </Button>
  );
}

function Code({ children }: { children: ReactNode }) {
  return (
    <code className="rounded-sm bg-ink/[0.06] px-1 py-px font-mono text-label text-ink">
      {children}
    </code>
  );
}

/* ==========================================================================
 * 加载 / 错误
 * ======================================================================== */

function CanvasSkeleton() {
  return (
    <div className="flex flex-col gap-3" aria-busy="true" aria-label="正在加载渠道详情">
      <div className="rounded-xl border border-line bg-paper p-4">
        <div className="flex items-center gap-2.5">
          <Skeleton className="h-6 w-6 rounded-md" />
          <div className="flex-1 space-y-1.5">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-2.5 w-52" />
          </div>
        </div>
        <Skeleton className="mt-4 h-[132px] w-full" />
      </div>
      <div className="rounded-xl border border-line bg-paper p-4">
        <div className="flex items-center gap-2.5">
          <Skeleton className="h-6 w-6 rounded-md" />
          <div className="flex-1 space-y-1.5">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-2.5 w-44" />
          </div>
        </div>
        <Skeleton className="mt-4 h-[116px] w-full" />
      </div>
      <div className="rounded-xl border border-line bg-paper p-4">
        <Skeleton className="h-3.5 w-28" />
        <Skeleton className="mt-3 h-[168px] w-full" />
      </div>
    </div>
  );
}

function PanelError({
  title,
  message,
  onRetry,
}: {
  title: string;
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="rounded-xl border border-line bg-paper px-5 py-8">
      <div className="flex items-start gap-2.5">
        <span className="mt-[2px] flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-danger-wash text-danger">
          <Icon name="shield" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="text-body font-semibold text-ink">{title}</h2>
          <p className="mt-1 text-secondary leading-relaxed text-muted">{message}</p>
          <Button className="mt-3" onClick={onRetry}>
            重试
          </Button>
        </div>
      </div>
    </div>
  );
}
