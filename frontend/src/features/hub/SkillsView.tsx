/**
 * 技能 · 连接器。
 *
 * 两条原则来自旧页，别改：
 *  1. **只列真实存在的东西**。技能带 `existing=1`、连接器只留 `configured`——
 *     否则整排"商店里可能有的"会混进"我有的"，用户会以为已经能用了。
 *  2. 卡片右侧的「+」不是"查看"，是**动作**：未落盘的技能点了就安装、落盘的点了
 *     就启用；连接器未配置的会跳去配置、需授权的会跳 OAuth。所以点卡片主体
 *     才是看详情，点「+」不能冒泡。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { useToast } from "@/components/ui/Toast";
import { cn } from "@/lib/cn";
import { connectorApi, skillApi, type ConnectorItem, type SkillItem } from "./api";
import { Icon } from "./icons";

const SKILL_SOURCE_LABEL: Record<string, string> = {
  platform: "管理员发布",
  custom: "我新建的",
  user: "我上传的",
  skillhub: "SkillHub",
};

/** 技能状态：来源 + 装没装/开没开。版本号对用户没有决策价值，不显示。 */
function skillStatus(item: SkillItem) {
  return [
    SKILL_SOURCE_LABEL[item.source || ""] || "技能",
    item.on_disk ? (item.enabled ? "已启用" : "已停用") : "待添加",
  ].join(" · ");
}

const CONNECTOR_EXAMPLES: Record<string, string[]> = {
  libtv: [
    "查看我在 LibTV 中的项目",
    "用 LibTV 根据这张参考图制作一段产品视频",
    "把我指定的 LibTV 画布中的已有作品整理成导演提案",
    "根据剧本用 LibTV 出图、Blender 白模运镜，再转成实拍视频",
  ],
};

type Tab = "skill" | "connector";

export function SkillsView({ onUseExample }: { onUseExample: (text: string) => void }) {
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("skill");
  const [query, setQuery] = useState("");
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [connectors, setConnectors] = useState<ConnectorItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 技能和连接器分开存：联合类型在 JSX 里收窄不了，`detail.transport` 会报错
  const [skillDetail, setSkillDetail] = useState<SkillItem | null>(null);
  const [connectorDetail, setConnectorDetail] = useState<ConnectorItem | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loadSkills = useCallback(async (q: string) => {
    const data = await skillApi.list(q);
    // 后端已经按 existing 筛过；这里再兜一层，老进程没重载时也不会刷出一排商店货
    setSkills((data.items || []).filter((item) => item.on_disk || item.platform));
  }, []);

  const loadConnectors = useCallback(async () => {
    const data = await connectorApi.list();
    setConnectors((data.items || []).filter((item) => item.configured));
  }, []);

  const reload = useCallback(
    async (q = query) => {
      setLoading(true);
      try {
        if (tab === "skill") await loadSkills(q);
        else await loadConnectors();
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "读取失败");
      } finally {
        setLoading(false);
      }
    },
    [tab, query, loadSkills, loadConnectors],
  );

  useEffect(() => {
    void reload();
    // 搜索关键字变化时不重置 loading，交给 debounce 下的同一条路径
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  // 搜索加 250ms 防抖：技能列表在后端是按 q 过滤的，每敲一个字都请求会打满日志
  useEffect(() => {
    if (tab !== "skill") return;
    const timer = window.setTimeout(() => {
      void reload(query);
    }, 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, tab]);

  const toggleSkill = async (item: SkillItem) => {
    setBusyId(item.id);
    try {
      await skillApi.toggle(item);
      toast(item.on_disk ? `已启用「${item.name || item.id}」` : `已添加「${item.name || item.id}」`);
      await loadSkills(query);
      setSkillDetail(null);
    } catch (err) {
      toast(`操作失败：${err instanceof Error ? err.message : "网络错误"}`, "error");
    } finally {
      setBusyId(null);
    }
  };

  const connect = async (item: ConnectorItem) => {
    if (item.need_authorize) return startAuth(item);
    setBusyId(item.id);
    try {
      await connectorApi.connect(item.id);
      toast(`已连接「${item.name || item.id}」`);
      await loadConnectors();
      setConnectorDetail(null);
    } catch (err) {
      toast(`连接失败：${err instanceof Error ? err.message : "网络错误"}`, "error");
    } finally {
      setBusyId(null);
    }
  };

  const disconnect = async (item: ConnectorItem) => {
    setBusyId(item.id);
    try {
      await connectorApi.disconnect(item.id);
      toast(`已解绑「${item.name || item.id}」`);
      await loadConnectors();
      setConnectorDetail(null);
    } catch (err) {
      toast(`解绑失败：${err instanceof Error ? err.message : "网络错误"}`, "error");
    } finally {
      setBusyId(null);
    }
  };

  const startAuth = async (item: ConnectorItem) => {
    try {
      const res = await connectorApi.startAuth(item.id);
      const url = res.authorize_url || res.url;
      // OAuth 必须整页跳转：新窗口里拿到的 cookie 回不到本页
      if (url) window.location.assign(url);
      else toast("没有拿到授权地址，请检查连接器配置", "error");
    } catch (err) {
      toast(`发起授权失败：${err instanceof Error ? err.message : "网络错误"}`, "error");
    }
  };

  const upload = async (file: File) => {
    try {
      const res = await skillApi.upload(file);
      toast(`已上传「${res.name || res.id || file.name}」`);
      setTab("skill");
      await loadSkills(query);
    } catch (err) {
      toast(`上传失败：${err instanceof Error ? err.message : "网络错误"}`, "error");
    }
  };

  const q = query.trim().toLowerCase();
  const shownSkills = q
    ? skills.filter((item) => `${item.name} ${item.id} ${item.description}`.toLowerCase().includes(q))
    : skills;
  const shownConnectors = q
    ? connectors.filter((item) => `${item.name} ${item.id}`.toLowerCase().includes(q))
    : connectors;

  const emptyText =
    tab === "skill"
      ? q
        ? `没有找到和「${query}」相关的技能`
        : "还没有技能。管理员发布的技能、你上传或新建的技能都会出现在这里。"
      : "中台还没有已配置的 MCP 服务。";

  return (
    <div className="mx-auto w-full max-w-[1000px] px-7 py-6">
      {/* 工具条 */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="flex gap-0.5 rounded-md bg-sunken p-0.5" role="tablist" aria-label="技能或连接器">
          {(
            [
              ["skill", "技能"],
              ["connector", "连接器"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={tab === value}
              onClick={() => {
                setTab(value);
                setQuery("");
              }}
              className={cn(
                "flex h-[26px] items-center gap-1 rounded-sm px-2.5 text-secondary transition-colors duration-150 ease-standard",
                tab === value
                  ? "bg-paper text-ink shadow-[0_1px_2px_rgba(22,32,46,0.08)]"
                  : "text-muted hover:text-ink",
              )}
            >
              <Icon name={value === "skill" ? "slash" : "plug"} size={12} />
              {label}
            </button>
          ))}
        </div>

        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={tab === "skill" ? "搜索技能" : "搜索连接器"}
          aria-label="搜索"
          className="h-[30px] w-[190px] rounded-md border border-line bg-paper px-2.5 text-body text-ink placeholder:text-muted focus-visible:border-signal/50 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/15"
        />

        {tab === "skill" ? (
          <div className="ml-auto flex items-center gap-1.5">
            <input
              ref={fileRef}
              type="file"
              accept=".zip,.tar.gz,.tgz,.md"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void upload(file);
                e.target.value = "";
              }}
            />
            <Button variant="primary" onClick={() => fileRef.current?.click()}>
              ＋ 上传技能包
            </Button>
          </div>
        ) : null}
      </div>

      {error ? (
        <div className="mb-3 rounded-md border border-danger/25 bg-danger-wash px-3 py-2 text-secondary text-danger">
          读取失败：{error}
        </div>
      ) : null}

      {loading && !(tab === "skill" ? skills.length : connectors.length) ? (
        <p className="py-12 text-center text-secondary text-muted">加载中…</p>
      ) : (tab === "skill" ? shownSkills.length : shownConnectors.length) === 0 ? (
        <p className="py-12 text-center text-secondary text-muted">{emptyText}</p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {tab === "skill"
            ? shownSkills.map((item) => (
                <SkillCard
                  key={item.id}
                  item={item}
                  busy={busyId === item.id}
                  onOpen={() => setSkillDetail(item)}
                  onAction={() => void toggleSkill(item)}
                />
              ))
            : shownConnectors.map((item) => (
                <ConnectorCard
                  key={item.id}
                  item={item}
                  busy={busyId === item.id}
                  onOpen={() => setConnectorDetail(item)}
                  onAction={() => void connect(item)}
                />
              ))}
        </div>
      )}

      {/* 技能详情 */}
      {skillDetail ? (
        <Drawer open onClose={() => setSkillDetail(null)} title={skillDetail.name || skillDetail.id}>
          <div className="flex flex-col gap-3">
            <p className="text-secondary leading-relaxed text-muted">
              {skillDetail.description || "这个技能还没有写说明。"}
            </p>
            <div className="flex items-center gap-2 text-label text-muted">
              <span>{skillStatus(skillDetail)}</span>
            </div>
            <div className="mt-1 flex items-center gap-2">
              {skillDetail.on_disk && skillDetail.enabled ? (
                <Button variant="secondary" disabled>
                  已启用
                </Button>
              ) : (
                <Button variant="primary" disabled={busyId === skillDetail.id} onClick={() => void toggleSkill(skillDetail)}>
                  {skillDetail.on_disk ? "启用这个技能" : "添加到我的技能"}
                </Button>
              )}
            </div>
          </div>
        </Drawer>
      ) : null}

      {/* 连接器详情 */}
      {connectorDetail ? (
        <Drawer open onClose={() => setConnectorDetail(null)} title={connectorDetail.name || connectorDetail.id}>
          <div className="flex flex-col gap-3">
            <p className="text-secondary leading-relaxed text-muted">
              {connectorDetail.description || `${connectorDetail.transport || "MCP"} 连接器。连接后可把它的工具加入当前会话。`}
            </p>
            <div className="text-label text-muted">
              {connectorDetail.transport || "mcp"} · {connectorDetail.tools || 0} 个工具
              {connectorDetail.need_authorize
                ? " · 待授权"
                : connectorDetail.enabled
                  ? " · 已连接"
                  : " · 未连接"}
            </div>

            {CONNECTOR_EXAMPLES[connectorDetail.id]?.length ? (
              <div className="mt-1">
                <div className="mb-1.5 text-label text-ink-2">💡 试试这样用</div>
                <div className="flex flex-col gap-1">
                  {CONNECTOR_EXAMPLES[connectorDetail.id]!.map((text) => (
                    <button
                      key={text}
                      type="button"
                      onClick={() => {
                        setConnectorDetail(null);
                        onUseExample(text);
                      }}
                      className="rounded-md border border-line px-2.5 py-1.5 text-left text-secondary text-ink-2 transition-colors duration-150 ease-standard hover:border-line-strong hover:bg-rail"
                    >
                      {text}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            <div className="mt-1 flex items-center gap-2">
              <Button
                variant="primary"
                disabled={busyId === connectorDetail.id}
                onClick={() => void connect(connectorDetail)}
              >
                {connectorDetail.need_authorize
                  ? "授权"
                  : connectorDetail.enabled
                    ? "重新连接"
                    : "连接"}
              </Button>
              {connectorDetail.enabled && !connectorDetail.need_authorize ? (
                <Button
                  variant="ghost"
                  className="text-danger hover:bg-danger-wash"
                  disabled={busyId === connectorDetail.id}
                  onClick={() => void disconnect(connectorDetail)}
                >
                  解绑
                </Button>
              ) : null}
            </div>
          </div>
        </Drawer>
      ) : null}
    </div>
  );
}

/* ---------------- 卡片 ---------------- */


/** 技能包里的图标是 base64 图片（data URL），连接器给的可能是 emoji。 */
function ItemIcon({ icon, fallback }: { icon?: string; fallback: string }) {
  if (icon && icon.startsWith("data:")) {
    return <img src={icon} alt="" className="mt-0.5 h-5 w-5 shrink-0 rounded-[4px] object-cover" />;
  }
  return (
    <span className="mt-0.5 shrink-0 text-body" aria-hidden="true">
      {icon || fallback}
    </span>
  );
}

const cardCls =
  "flex items-start gap-2.5 rounded-lg border border-line bg-paper px-3 py-2.5 text-left transition-colors duration-150 ease-standard hover:border-line-strong";

function SkillCard({
  item,
  busy,
  onOpen,
  onAction,
}: {
  item: SkillItem;
  busy: boolean;
  onOpen: () => void;
  onAction: () => void;
}) {
  const done = Boolean(item.on_disk && item.enabled);
  return (
    <div className={cardCls}>
      <button type="button" onClick={onOpen} className="flex min-w-0 flex-1 items-start gap-2.5 text-left">
        <ItemIcon icon={item.icon} fallback="🧩" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-body font-medium text-ink">{item.name || item.id}</span>
          <span className="mt-0.5 line-clamp-2 block text-secondary leading-relaxed text-muted">
            {item.description || "还没有写说明"}
          </span>
          <span className="mt-1 block text-label text-muted">{skillStatus(item)}</span>
        </span>
      </button>

      <button
        type="button"
        disabled={busy || done}
        onClick={onAction}
        title={done ? "已启用" : item.on_disk ? "启用这个技能" : "添加到我的技能"}
        aria-label={done ? "已启用" : "添加或启用"}
        className={cn(
          "mt-0.5 flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-md border text-label transition-colors duration-150 ease-standard",
          done
            ? "border-ok/30 bg-ok-wash text-ok"
            : "border-line-strong bg-paper text-ink-2 hover:border-signal/40 hover:bg-signal-wash hover:text-signal",
          (busy || done) && "cursor-default",
        )}
      >
        {done ? "✓" : "+"}
      </button>
    </div>
  );
}

function ConnectorCard({
  item,
  busy,
  onOpen,
  onAction,
}: {
  item: ConnectorItem;
  busy: boolean;
  onOpen: () => void;
  onAction: () => void;
}) {
  const status = item.need_authorize
    ? "待授权"
    : item.enabled
      ? "已连接"
      : item.configured
        ? "未连接"
        : "未添加";
  return (
    <div className={cardCls}>
      <button type="button" onClick={onOpen} className="flex min-w-0 flex-1 items-start gap-2.5 text-left">
        <ItemIcon icon={item.icon} fallback={item.transport === "stdio" ? "🗄️" : "🔗"} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-body font-medium text-ink">{item.name || item.id}</span>
          <span className="mt-0.5 block text-secondary leading-relaxed text-muted">
            {item.configured ? `${item.transport || "mcp"} · ${item.tools || 0} 个工具` : "未配置"}
          </span>
          <span className="mt-1 block text-label text-muted">{status}</span>
        </span>
      </button>

      <button
        type="button"
        disabled={busy || (item.enabled && !item.need_authorize)}
        onClick={onAction}
        title={item.need_authorize ? "授权" : item.enabled ? "已连接" : "连接并启用"}
        aria-label={item.need_authorize ? "授权" : "连接"}
        className={cn(
          "mt-0.5 flex h-[26px] shrink-0 items-center justify-center rounded-md border px-2 text-label transition-colors duration-150 ease-standard",
          item.need_authorize
            ? "border-warn/40 bg-warn-wash text-warn hover:border-warn/60"
            : item.enabled
              ? "border-ok/30 bg-ok-wash px-2 text-ok"
              : "border-line-strong bg-paper text-ink-2 hover:border-signal/40 hover:bg-signal-wash hover:text-signal",
        )}
      >
        {item.need_authorize ? "授权" : item.enabled ? "✓" : "+"}
      </button>
    </div>
  );
}
