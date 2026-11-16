import { useMemo } from "react";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/cn";
import { AuditTab } from "./AuditTab";
import { ConnectorsTab } from "./ConnectorsTab";
import { CreditsTab } from "./CreditsTab";
import { GateScreen } from "./GateScreen";
import { LogsTab } from "./LogsTab";
import { OverviewTab } from "./OverviewTab";
import { PlatformsTab } from "./PlatformsTab";
import { PlazaTab } from "./PlazaTab";
import { SkillsTab } from "./SkillsTab";
import { TABS, useAdmin } from "./useAdmin";
import { UsersTab } from "./UsersTab";

/**
 * 控制台外壳。
 *
 * 与旧页的结构差异只有一处：页签内容按需挂载，切走就卸载。
 * 旧页是 9 个 section 全在 DOM 里、靠 hidden 切换，日志页的 SSE 因此必须
 * 手动 close——漏一处，后台就一直在推。组件卸载天然带走了它。
 */
export function AdminPage() {
  const { gate, tab, setTab, authPlatforms, loadAuthPlatforms } = useAdmin();

  // /admin?user=<id>：直接把某个人的详情抽屉打开，方便把链接发给同事定位问题
  const focusUser = useMemo(
    () => (typeof location === "undefined" ? null : new URLSearchParams(location.search).get("user")),
    [],
  );

  if (gate.kind === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center text-body text-muted">
        正在确认身份…
      </div>
    );
  }

  if (gate.kind !== "ready") {
    return (
      <GateScreen
        kind={gate.kind}
        message={gate.message}
        platforms={authPlatforms}
        onNeedPlatforms={() => void loadAuthPlatforms()}
        role={gate.kind === "denied" ? gate.me?.role : undefined}
      />
    );
  }

  const me = gate.me;
  const badged = me.scope === "all";
  const operator = me.role === "operator";
  const roleLabel = badged ? "跨租户超管" : operator ? "本企业运维" : "普通用户";

  return (
    <div className="min-h-screen bg-rail">
      <header className="sticky top-0 z-30 border-b border-line bg-paper/95 backdrop-blur supports-[backdrop-filter]:bg-paper/80">
        <div className="mx-auto flex max-w-[1240px] flex-wrap items-center gap-x-3 gap-y-1 px-5 pt-3">
          <div className="mr-auto flex items-baseline gap-2.5">
            <h1 className="text-title font-semibold tracking-tight text-ink">管理控制台</h1>
            <span className="text-label text-muted">智能体中台</span>
            <a
              href="/"
              className="text-label text-signal underline-offset-2 hover:underline focus-visible:underline"
            >
              去对话界面 →
            </a>
          </div>
          <div className="flex items-center gap-2 text-label text-muted">
            <span className="font-mono text-ink-2">{me.user_id}</span>
            <Badge tone={badged ? "signal" : operator ? "warn" : "neutral"}>{roleLabel}</Badge>
            <span>租户 {me.tenant_id}</span>
          </div>
        </div>

        <nav
          role="tablist"
          aria-label="控制台分区"
          className="mx-auto flex max-w-[1240px] gap-0.5 overflow-x-auto px-5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {TABS.map((item) => {
            const selected = tab === item.id;
            return (
              <button
                key={item.id}
                role="tab"
                type="button"
                aria-selected={selected}
                onClick={() => setTab(item.id)}
                className={cn(
                  "shrink-0 whitespace-nowrap border-b-2 px-3 py-2 text-body transition-colors duration-150 ease-standard",
                  selected
                    ? "border-signal font-medium text-signal"
                    : "border-transparent text-ink-2 hover:text-ink",
                )}
              >
                {item.label}
              </button>
            );
          })}
        </nav>
      </header>

      <main className="mx-auto max-w-[1240px] px-5 pt-4 pb-16">
        {tab === "overview" ? <OverviewTab /> : null}
        {tab === "users" ? <UsersTab scope={me.scope} focusUserId={focusUser} /> : null}
        {tab === "credits" ? <CreditsTab /> : null}
        {tab === "login" ? <PlatformsTab /> : null}
        {tab === "connectors" ? <ConnectorsTab /> : null}
        {tab === "skills" ? <SkillsTab /> : null}
        {tab === "plaza" ? <PlazaTab /> : null}
        {tab === "audit" ? <AuditTab /> : null}
        {tab === "logs" ? <LogsTab active={tab === "logs"} /> : null}
      </main>
    </div>
  );
}
