/**
 * 主界面外壳：顶栏 + 滚动区 + 输入器。
 *
 * 三块状态各有归属，不要互相抄：
 *  - `useHubChat`：会话、时间线、SSE 流（真相在库里）；
 *  - `useSpaces`：置顶/空间/工作区（localStorage + 服务端命名空间）；
 *  - `useAddons`：会话级专家与技能（`sessions.addon_selection`）。
 * 输入框里的引用是第四层，只活一条消息，由 `chips` 持有。
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useToast } from "@/components/ui/Toast";
import { hubApi } from "./api";
import { AddonPicker } from "./AddonPicker";
import { ArtifactCard, ChatProcessCard, MessageRow, PendingRow, ProcessSection } from "./ChatStream";
import { Composer } from "./Composer";
import { Icon } from "./icons";
import { LOGIN_FLASH_KEY, LoginDialog } from "./LoginDialog";
import { Sidebar } from "./Sidebar";
import type { HubDrawer, HubView } from "./Sidebar";
import { MemoryDrawer, ProfileDrawer } from "./MemoryDrawers";
import type { RefChip } from "./mentions";
import { InspireView } from "./InspireView";
import { SchedulesView } from "./SchedulesView";
import { SkillsView } from "./SkillsView";
import { useAddons } from "./useAddons";
import { useSpaces } from "./spaces";
import { useHubChat } from "./useHubChat";
import { Welcome } from "./Welcome";
import type { Me, ModelRow } from "./types";

/** 视图 → hash。chat 不给 hash，保持 `/app/hub` 这个干净地址。 */
const VIEW_HASH: Record<HubView, string> = {
  chat: "",
  skills: "skills",
  schedule: "schedule",
  inspire: "inspire",
};

const VIEW_TITLE: Record<HubView, { title: string; icon: string }> = {
  chat: { title: "", icon: "bot" },
  skills: { title: "技能·连接器", icon: "plug" },
  schedule: { title: "定时任务", icon: "clock" },
  inspire: { title: "灵感社区", icon: "grid" },
};

function viewFromHash(): HubView {
  const raw = window.location.hash.replace(/^#\/?/, "");
  return raw === "skills" || raw === "schedule" || raw === "inspire" ? raw : "chat";
}

export function HubPage() {
  const toast = useToast();
  const [draft, setDraft] = useState("");
  /** 这条消息引用的技能 / 文件 / 连接器。发送成功就清空，失败随草稿一起退回。 */
  const [chips, setChips] = useState<RefChip[]>([]);
  const [me, setMe] = useState<Me | null>(null);
  /**
   * 空间与置顶都按账号隔离，所以键名要等 `/api/me` 回来才定得下来。
   * 工作区信息（当前命名空间、私有根目录）也由这个 hook 独持——同时开两份
   * 状态的话，切完空间侧栏高亮了而顶栏 pill 还写着旧名字。
   */
  const spaceState = useSpaces(String(me?.user_id || ""));
  // 新会话落在当前空间：旧页靠 `window.onNewSession` 回调做同一件事
  const hub = useHubChat({ onSessionCreated: spaceState.assignSession });
  /** 会话级选择（专家 + 技能）。会话 id 一变，这个 hook 自己去拉新的那套。 */
  const addons = useAddons(hub.sessionId);
  const [addonOpen, setAddonOpen] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  /** ⌘K：把侧栏搜索框打开并聚焦（搜索状态在 Sidebar 内部，用自增信号触发） */
  const [searchSignal, setSearchSignal] = useState(0);
  const [models, setModels] = useState<ModelRow[]>([]);
  const [model, setModel] = useState("");
  const [online, setOnline] = useState<boolean | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [view, setView] = useState<HubView>(() => viewFromHash());
  const [drawer, setDrawer] = useState<HubDrawer>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const workPlace = useMemo(() => {
    const ws = spaceState.ws;
    return {
      path: ws?.path || "",
      name: ws?.namespace || "",
      privateRoot: ws?.private_root || "",
    };
  }, [spaceState.ws]);

  /* 视图落在 hash 上：刷新、分享链接、浏览器前进后退都能回到同一页 */
  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const goView = useCallback((next: HubView) => {
    setView(next);
    const suffix = VIEW_HASH[next];
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}${suffix ? `#${suffix}` : ""}`,
    );
  }, []);

  /** 从别的视图切回对话时贴回底部——否则会停在会话最开头，像"记录丢了"。 */
  useEffect(() => {
    if (view !== "chat") return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [view]);

  /**
   * 灵感卡片「引用」：把作品名拼到草稿末尾并切回对话。
   *
   * 只写进输入框、不自动发送——用户还得补一句"照着这个风格来"，
   * 直接发出去只会换来一句莫名其妙的回答（旧页也是这个选择）。
   */
  const quoteToComposer = useCallback(
    (text: string) => {
      setDraft((prev) => (prev.trimEnd() ? `${prev.trimEnd()}\n${text}` : text));
      goView("chat");
      requestAnimationFrame(() => {
        document.querySelector<HTMLTextAreaElement>('textarea[aria-label="消息"]')?.focus();
      });
    },
    [goView],
  );

  /* ---------------- 开屏：身份、模型、工作目录、健康 ---------------- */
  useEffect(() => {
    void (async () => {
      try {
        const data = await hubApi.me();
        setMe(data);
      } catch {
        setMe(null);
      }
      try {
        const data = await hubApi.models();
        setModels(data.models ?? []);
        setModel(String(data.current || ""));
      } catch {
        /* 拿不到模型清单就不显示可选列表，不影响发消息 */
      }
      // 工作目录不在这里拉：`useSpaces` 已经持有它，拉两遍只会不一致
      // health 自己吞掉异常并返回布尔：401 是"没登录"，不是"后端挂了"
      setOnline(await hubApi.health());
    })();
  }, []);

  /* ---------------- 自动贴底 ---------------- */
  const liveAnswer = hub.live?.answer ?? "";
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // 只在用户本来就在底部时跟着滚，否则往上翻历史会被一直拽回去
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (nearBottom) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [hub.messages, liveAnswer, hub.pending, hub.sessionId]);

  const isEmpty = hub.messages.length === 0 && !hub.live && !hub.pending;

  const title = useMemo(() => {
    const hit = hub.sessions.find((s) => s.session_id === hub.sessionId);
    return hit?.title || "新任务";
  }, [hub.sessions, hub.sessionId]);

  /** 空间标签：有命名空间就写名字，否则退回目录名。 */
  const spaceLabel = useMemo(() => {
    const named = workPlace.name && workPlace.name !== "我的空间" ? workPlace.name : "";
    if (named) return `空间：${named}`;
    // 私有根目录的保留名就叫「我的空间」。这里显示它而不是 "目录：default"——
    // 后者是租户目录名，对用户没有意义，还会让人以为选错了地方。
    if (workPlace.name === "我的空间") return "我的空间";
    const base = workPlace.path.split("/").filter(Boolean).pop() || "";
    return base ? `目录：${base}` : "未选择空间";
  }, [workPlace]);

  /** 模型 pill 显示名字而不是 id：`deepseek-v4-1-flash` 是给接口用的。 */
  const modelLabel = useMemo(() => {
    const hit = models.find((m) => m.id === model);
    return hit ? String(hit.name || hit.label || hit.id) : model;
  }, [models, model]);

  const submit = useCallback(async () => {
    const text = draft;
    if (!text.trim() || hub.busy) return;
    // 未登录时不禁用输入框（写了半天才被告知要登录更糟），只拦发送
    if (hub.needLogin) {
      setNotice("还没有登录，发送会失败。先点上面的「登录中台」。");
      return;
    }
    const sentChips = chips;
    // 会话级选择要在发送前取快照：send 会先定会话 id，之后 pending 就被清掉了
    const sentAddons = addons.pending();
    setDraft("");
    setChips([]);
    const result = await hub.send(text, { chips: sentChips, addonSelection: sentAddons });
    if (!result.ok) {
      // 失败就把原文与引用一起放回去，别让用户重打
      setDraft((prev) => prev || text);
      setChips((prev) => (prev.length ? prev : sentChips));
    }
  }, [draft, chips, hub, addons]);

  const pickPrompt = useCallback((prompt: string) => {
    setDraft(prompt);
    document.querySelector<HTMLTextAreaElement>('textarea[aria-label="消息"]')?.focus();
  }, []);

  const selectModel = useCallback(async (id: string) => {
    const previous = model;
    setModel(id);
    try {
      await hubApi.selectModel(id);
    } catch (err) {
      setModel(previous);
      setNotice("切换模型失败：" + errorMessage(err));
    }
  }, [model]);

  const logout = useCallback(async () => {
    try {
      await hubApi.logout();
    } catch {
      /* 已经登出 */
    }
    window.location.reload();
  }, []);

  /**
   * 演示模式（AUTH_MODE=off）没有登录这回事：跳过去只会看到一页 JSON
   * （`{"detail":"OIDC 未启用"}`）。这里仍然打开弹窗，让弹窗把"为什么不用登录、
   * 想开登录该改哪个变量"讲完整——一句话 toast 说不清，而且容易被当成报错。
   */
  const authOff = me?.auth_mode === "off";
  const requestLogin = useCallback(() => setLoginOpen(true), []);

  /* 登录成功后整页重载，提示只能靠 sessionStorage 带过来 */
  useEffect(() => {
    let flash: string | null = null;
    try {
      flash = sessionStorage.getItem(LOGIN_FLASH_KEY);
      if (flash) sessionStorage.removeItem(LOGIN_FLASH_KEY);
    } catch {
      /* 隐私模式没有 storage */
    }
    if (flash) toast(flash, "ok");
  }, [toast]);

  /* 键盘：⌘K 搜会话、⌘N 新会话。与旧页一致，别加 ⌘Enter 发送（和输入法冲突） */
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey)) return;
      const key = event.key.toLowerCase();
      if (key === "k") {
        event.preventDefault();
        setSearchSignal((n) => n + 1);
        return;
      }
      if (key === "n") {
        event.preventDefault();
        hub.newSession();
        goView("chat");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [hub.newSession, goView]);

  const needsLogin = hub.needLogin;
  /**
   * 账号卡。`authenticated` 要和"有没有 user_id"分开看：`AUTH_MODE=off` 时
   * 服务端会给一个默认身份（user_id=default），但那是匿名占位——按 user_id
   * 判断就会把「登录中台」换成「退出登录」，点下去只是刷新一下页面。
   */
  const meLine = me
    ? {
        name: String(me.display_name || me.user_id || "已登录"),
        sub: [me.role, me.tenant_id].filter(Boolean).join(" · ") || "点击查看账号",
        initial: String(me.display_name || me.user_id || "?").slice(0, 1).toUpperCase(),
        authed: !!me.authenticated,
      }
    : null;

  /**
   * "深度思考"卡插在哪一条前面。
   *
   * 流式中答复还没落库，卡就贴在末尾；一轮结束时答复已经进库画成气泡，卡要落在
   * 那条气泡**前面**，否则会出现"答案 → 执行过程"的倒序。
   */
  const cardIndex = useMemo(() => {
    if (!hub.live) return -1;
    if (hub.live.phase !== "done") return hub.messages.length;
    for (let i = hub.messages.length - 1; i >= 0; i -= 1) {
      if (hub.messages[i]?.role === "assistant") return i;
    }
    return hub.messages.length;
  }, [hub.live, hub.messages]);

  return (
    <div className="flex h-screen overflow-hidden bg-paper">
      <Sidebar
        sessions={hub.sessions}
        currentId={hub.sessionId}
        onOpen={(id) => void hub.open(id)}
        onNew={hub.newSession}
        me={meLine}
        onRequestLogin={requestLogin}
        onLogout={() => void logout()}
        view={view}
        onView={goView}
        onDrawer={setDrawer}
        spaceState={spaceState}
        searchSignal={searchSignal}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        {/* 顶栏 */}
        <header className="hairline-b flex h-[46px] shrink-0 items-center gap-2 px-4">
          <Icon
            name={view === "chat" ? "bot" : VIEW_TITLE[view].icon}
            size={14}
            className="text-muted"
          />
          <span className="min-w-0 truncate text-secondary text-ink">
            {view === "chat" ? title : VIEW_TITLE[view].title}
          </span>
          <div className="ml-auto flex items-center gap-3">
            <span
              className={cn(
                "flex items-center gap-1.5 text-label",
                hub.phase === "error" || online === false
                  ? "text-danger"
                  : needsLogin
                    ? "text-warn"
                    : "text-muted",
              )}
              title={online === false ? "后端不可达" : "连接正常"}
            >
              <i
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  hub.phase === "error" || online === false
                    ? "bg-danger"
                    : needsLogin
                      ? "bg-warn"
                      : "bg-ok",
                )}
                aria-hidden="true"
              />
              {online === false ? "后端不可达" : needsLogin ? "未登录" : "就绪"}
            </span>
          </div>
        </header>

        {needsLogin ? (
          <div className="flex flex-wrap items-center gap-2 border-b border-warn/25 bg-warn-wash px-4 py-2 text-secondary text-warn">
            <Icon name="shield" size={14} />
            还没有登录，发送会失败。
            <button
              type="button"
              onClick={requestLogin}
              className="ml-auto inline-flex h-[26px] items-center rounded-md border border-warn/40 px-2 text-label text-warn hover:bg-paper"
            >
              登录中台
            </button>
          </div>
        ) : null}

        {view === "chat" ? (
          <>
        {/* 滚动区 */}
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
          <div
            className={cn(
              "mx-auto w-full max-w-[720px] px-7 pb-6",
              // 空态垂直居中：旧页靠 flex 分配把内容托到中间，内容少时上方留白约 200px，
              // 顶格贴住顶栏会显得像没加载完
              isEmpty && "flex min-h-full flex-col justify-center",
            )}
          >
            {isEmpty ? <Welcome onPick={pickPrompt} /> : null}

            {hub.messages.map((message, index) => (
              <Fragment key={`${index}-${message.role}`}>
                {index === cardIndex ? (
                  <ChatProcessCard state={hub.live!} elapsed={hub.elapsed} />
                ) : null}
                <MessageRow message={message} />
              </Fragment>
            ))}

            {cardIndex === hub.messages.length && hub.live ? (
              <ChatProcessCard state={hub.live} elapsed={hub.elapsed} />
            ) : null}

            {hub.live?.answer ? (
              <div className="mb-4 flex justify-start">
                <div className="max-w-[82%] rounded-xl border border-line bg-paper px-3.5 py-2.5 text-body leading-[1.75] whitespace-pre-wrap break-words text-ink">
                  {hub.live.answer}
                </div>
              </div>
            ) : null}

            {hub.pending && !hub.live?.answer ? <PendingRow text={hub.pending} /> : null}

            {hub.turns.length ? <ProcessSection turns={hub.turns} /> : null}

            {hub.taskArtifact ? (
              <ArtifactCard taskId={hub.taskArtifact.taskId} artifact={hub.taskArtifact.artifact} />
            ) : null}

            <div ref={bottomRef} />
          </div>
        </div>

        {notice ? (
          <div className="mx-auto flex w-full max-w-[720px] items-start gap-2 px-7 pb-1 text-secondary text-danger">
            <span className="min-w-0 flex-1 break-words">{notice}</span>
            <button
              type="button"
              onClick={() => setNotice(null)}
              className="shrink-0 text-label text-muted hover:text-ink"
            >
              知道了
            </button>
          </div>
        ) : null}

        <Composer
          value={draft}
          onChange={setDraft}
          onSend={() => void submit()}
          busy={hub.busy}
          models={models}
          currentModel={model}
          currentModelLabel={modelLabel}
          onSelectModel={(id) => void selectModel(id)}
          spaceLabel={spaceLabel}
          spaceTitle={workPlace.path}
          chips={chips}
          onChipsChange={setChips}
          addons={addons.view}
          onOpenAddons={() => setAddonOpen(true)}
          onRemoveAddonSkill={(id) => addons.toggleSkill(id, false)}
        />
          </>
        ) : view === "schedule" ? (
          <div className="min-h-0 flex-1 overflow-y-auto">
            <SchedulesView />
          </div>
        ) : view === "inspire" ? (
          <div className="min-h-0 flex-1 overflow-y-auto">
            <InspireView onQuote={quoteToComposer} onGoConnectors={() => goView("skills")} />
          </div>
        ) : view === "skills" ? (
          <div className="min-h-0 flex-1 overflow-y-auto">
            <SkillsView onUseExample={quoteToComposer} />
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto" />
        )}
      </main>

      {/* 画像与记忆是"就地看一眼再改"的东西，不占一整个视图 */}
      <ProfileDrawer open={drawer === "profile"} onClose={() => setDrawer(null)} />
      <MemoryDrawer
        open={drawer === "memory"}
        onClose={() => setDrawer(null)}
        sessionId={hub.sessionId}
      />

      {/* 会话资源：专家 + 技能，写进 sessions.addon_selection */}
      <AddonPicker
        open={addonOpen}
        onClose={() => setAddonOpen(false)}
        view={addons.view}
        catalog={addons.catalog}
        experts={addons.experts}
        catalogLoading={addons.catalogLoading}
        saving={addons.saving}
        onExpert={addons.setExpert}
        onToggleSkill={addons.toggleSkill}
        onResetSkills={addons.resetSkills}
        onGoSkills={() => {
          setAddonOpen(false);
          goView("skills");
        }}
        onCatalogChanged={addons.refreshCatalog}
      />

      <LoginDialog open={loginOpen} onClose={() => setLoginOpen(false)} authOff={authOff} />
    </div>
  );
}
