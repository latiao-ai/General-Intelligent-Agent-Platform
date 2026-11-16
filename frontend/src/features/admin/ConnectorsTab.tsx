import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { useToast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { consoleApi } from "./api";
import {
  analyzeConnectorDoc,
  analysisFromExisting,
  connectorIdFrom,
  guessIcon,
  readAnyDocText,
} from "./lib/connectorDoc";
import {
  Code,
  DrawerHead,
  Field,
  Grid,
  inputCls,
  Loading,
  Panel,
  selectCls,
  TableWrap,
  tdCls,
  tdMonoCls,
  textareaCls,
  thCls,
  Toolbar,
  useAsk,
} from "./primitives";
import type { ConnectorTenantGroup, PlatformConnector, Transport } from "./types";

/* 三条连接方式。顺序就是推荐顺序：能走 streamable_http 就别退 sse，
   stdio 只在对方压根没有网络地址时才有意义。 */
export const TRANSPORTS: Record<Transport, { label: string; sub: string; note: string }> = {
  streamable_http: {
    label: "Streamable HTTP",
    sub: "一个 /mcp 端点，请求和流式响应同一路",
    note: "远程部署的默认选法。地址通常以 /mcp 结尾。",
  },
  sse: {
    label: "SSE",
    sub: "长连 /sse 收事件，请求 POST 回 /message",
    note: "老一些的服务端只暴露 /sse。地址通常以 /sse 结尾。",
  },
  stdio: {
    label: "stdio",
    sub: "本机起子进程，走标准输入输出",
    note: "没有网络地址，靠一条启动命令拉起进程。",
  },
};

const TP_ORDER: Transport[] = ["streamable_http", "sse", "stdio"];

/** analyzeConnectorDoc 的返回结构（那个模块是逐字搬来的，没有类型）。 */
interface Analysis {
  source: "json" | "text" | "saved";
  serverCount: number;
  name: string;
  description: string;
  transports: { transport: Transport; url: string }[];
  stdio: { command: string; args: string[]; env: Record<string, string>; cwd?: string; pkg?: string; runtime?: string } | null;
  auth: { mode: "none" | "oauth" | "headers"; headers: Record<string, string>; issuer: string; scopes: string[] };
  warnings: { k: string; text: string; tone?: string }[];
}

interface MergedRow {
  id: string;
  name: string;
  icon: string;
  transport: Transport;
  url: string;
  command: string;
  auth: boolean | string;
  enabled: boolean | null;
  shadowed: boolean;
  published: boolean;
  source: string;
  sourceLabel: string;
  tenants: { tenant: string; enabled: boolean; credentialed: boolean }[];
}

/**
 * 连接器。
 *
 * 只保留一张表，一行一个连接器，三种来源并排看：
 *   平台目录 —— 控制台发布，可增删改；
 *   运维配置 —— 写在 .env 的 MCP_SERVERS 里，控制台只读；
 *   用户自建 —— 用户在连接器页自己加的，控制台看不到它的配置。
 * 旧页曾经把它们拆成两张表：运维配好的 libtv 既不在「平台目录」里，
 * 又只有一个 server_id 躺在「谁已经连上」里，看起来像凭空冒出来的。
 */
export function ConnectorsTab() {
  const toast = useToast();
  const ask = useAsk();
  const [published, setPublished] = useState<PlatformConnector[]>([]);
  const [servers, setServers] = useState<{ id: string; name?: string; transport: Transport; url?: string; command?: string; auth?: boolean | string; source?: string; source_label?: string }[]>([]);
  const [groups, setGroups] = useState<ConnectorTenantGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [bindingError, setBindingError] = useState(false);
  const [editing, setEditing] = useState<PlatformConnector | "new" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    // 两份数据一起拿：任何一个挂掉都不该让整张表空掉，所以分开兜错
    const [platform, bindings] = await Promise.all([
      consoleApi.platformConnectors().catch((err) => ({ error: err })),
      consoleApi.connectorBindings().catch(() => null),
    ]);
    if ("error" in platform && platform.error) {
      setError(errorMessage(platform.error));
      setPublished([]);
      setServers([]);
      setGroups([]);
      setLoading(false);
      return;
    }
    const items = (platform as { items?: PlatformConnector[] }).items || [];
    setPublished(items);
    setServers(bindings?.servers || []);
    setGroups(bindings?.tenants || []);
    setBindingError(!bindings);
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const rows = useMemo(() => {
    const map = new Map<string, MergedRow>();
    published.forEach((c) => {
      map.set(c.id, {
        id: c.id,
        name: c.name || c.id,
        icon: c.icon || "🔗",
        transport: c.transport,
        url: c.url || "",
        command: "",
        auth: c.auth ?? false,
        enabled: Boolean(c.enabled),
        shadowed: Boolean(c.shadowed),
        published: true,
        source: "platform",
        sourceLabel: "平台目录",
        tenants: [],
      });
    });
    servers.forEach((s) => {
      const row = map.get(s.id);
      if (row) {
        row.source = s.source || row.source;
        row.sourceLabel = s.source_label || row.sourceLabel;
        row.url = row.url || s.url || "";
        row.command = row.command || s.command || "";
        row.auth = row.auth || Boolean(s.auth);
        return;
      }
      map.set(s.id, {
        id: s.id,
        name: s.name || s.id,
        icon: "🔗",
        transport: s.transport,
        url: s.url || "",
        command: s.command || "",
        auth: Boolean(s.auth),
        enabled: null,
        shadowed: false,
        published: false,
        source: s.source || "",
        sourceLabel: s.source_label || "",
        tenants: [],
      });
    });
    groups.forEach((group) => {
      (group.bindings || []).forEach((b) => {
        let row = map.get(b.server_id);
        if (!row) {
          row = {
            id: b.server_id,
            name: b.name || b.server_id,
            icon: "🔗",
            transport: b.transport,
            url: b.url || "",
            command: b.command || "",
            auth: false,
            enabled: null,
            shadowed: false,
            published: false,
            source: b.source || "user",
            sourceLabel: b.source_label || "用户自建",
            tenants: [],
          };
          map.set(b.server_id, row);
        }
        row.tenants.push({
          tenant: group.tenant_id,
          enabled: Boolean(b.enabled),
          credentialed: Boolean(b.credentialed),
        });
      });
    });
    return [...map.values()];
  }, [groups, published, servers]);

  const toggle = async (row: MergedRow) => {
    try {
      await consoleApi.toggleConnector(row.id, !row.enabled);
      toast(row.enabled ? "已停用，用户页上不再显示" : "已发布");
      await load();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const remove = async (row: MergedRow) => {
    const ok = await ask.confirm({
      title: "删除这个连接器？",
      tone: "danger",
      confirmText: "删除",
      body: (
        <>
          <div className="font-mono text-label text-ink">
            {row.name}（{row.id}）
          </div>
          <p className="mt-2">
            它只是从平台目录里移除：已经连上的用户会失去这个入口，但不会删掉他们本地的授权记录。
          </p>
        </>
      ),
    });
    if (!ok) return;
    try {
      await consoleApi.deleteConnector(row.id);
      toast("已删除");
      await load();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const bound = rows.filter((r) => r.tenants.length).length;

  return (
    <>
      <Panel
        title="连接器"
        hint={
          loading
            ? "读取中…"
            : rows.length
              ? `共 ${rows.length} 个 · ${rows.filter((r) => r.published && r.enabled).length} 个已发布 · ${bound} 个有租户连接`
              : undefined
        }
        actions={
          <Button variant="primary" onClick={() => setEditing("new")}>
            新增连接器
          </Button>
        }
      >
        <Toolbar>
          <Button onClick={() => void load()}>刷新</Button>
          <span className="text-label text-muted">
            平台目录可增删改；运维配置和用户自建只读，来源写在状态列
          </span>
          {bindingError ? (
            <span className="text-label text-warn">连接情况读取失败</span>
          ) : null}
        </Toolbar>

        {error ? (
          <div className="flex flex-col items-start gap-3 py-6">
            <p className="text-body text-ink-2">{error}</p>
            <Button onClick={() => void load()}>重试</Button>
          </div>
        ) : loading ? (
          <Loading rows={5} label="正在加载连接器" />
        ) : rows.length === 0 ? (
          <div className="py-6 text-body leading-relaxed text-muted">
            <p>还没有连接器。</p>
            <p className="mt-1">点「新增连接器」，发布后所有用户的连接器页都会出现。</p>
          </div>
        ) : (
          <TableWrap>
            <table className="w-full min-w-[1080px] border-collapse text-body">
              <thead>
                <tr>
                  <th className={cn(thCls, "min-w-[190px] max-w-[220px]")}>连接器</th>
                  <th className={cn(thCls, "whitespace-nowrap")}>传输</th>
                  <th className={cn(thCls, "min-w-[210px]")}>地址</th>
                  <th className={cn(thCls, "min-w-[190px]")}>来源 / 状态</th>
                  <th className={cn(thCls, "min-w-[190px]")}>各租户连接</th>
                  <th className={thCls} />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="hover:bg-rail/70">
                    <td className={tdCls}>
                      <div className="flex min-w-0 items-center gap-1.5">
                        <span aria-hidden="true" className="shrink-0 text-secondary">
                          {row.icon}
                        </span>
                        {/* 名字长的（「默认服务端（MCP_* 单项配置）」）一行放不下，
                            折行会把「配置）」切成「配」/「置）」，不如截断挂 title。 */}
                        <span className="truncate text-ink" title={row.name}>
                          {row.name}
                        </span>
                      </div>
                      {row.name === row.id ? null : (
                        <div className="font-mono text-label text-muted">{row.id}</div>
                      )}
                    </td>
                    <td className={cn(tdCls, "text-secondary text-muted")}>
                      {/* 标签本身不许折；鉴权另起一行，否则「Streamable HTTP ·」后面
                          孤零零吊一个 OAuth。 */}
                      <span className="whitespace-nowrap">
                        {TRANSPORTS[row.transport]?.label || row.transport}
                      </span>
                      {row.auth ? <span className="block text-label">OAuth 鉴权</span> : null}
                    </td>
                    <td
                      className={cn(tdMonoCls, "max-w-[240px] truncate")}
                      title={row.url || row.command}
                    >
                      {row.url || (row.command ? <Code>{row.command}</Code> : "—")}
                    </td>
                    <td className={tdCls}>
                      <SourceCell row={row} />
                    </td>
                    <td className={tdCls}>
                      <TenantsCell row={row} />
                    </td>
                    <td className={cn(tdCls, "whitespace-nowrap")}>
                      {row.published ? (
                        <div className="flex items-center gap-1">
                          <Button
                            size="sm"
                            onClick={() =>
                              setEditing(published.find((c) => c.id === row.id) ?? null)
                            }
                          >
                            编辑
                          </Button>
                          <Button size="sm" onClick={() => void toggle(row)}>
                            {row.enabled ? "停用" : "启用"}
                          </Button>
                          <Button size="sm" variant="danger" onClick={() => void remove(row)}>
                            删除
                          </Button>
                        </div>
                      ) : (
                        <span className="text-label text-muted">只读</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Panel>

      {editing ? (
        <ConnectorForm
          existing={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
        />
      ) : null}
    </>
  );
}

/** 状态列：平台目录给「已发布 / 已停用」，其余说清「谁配的、改哪里生效」。
 *
 * 说明只留一句短的，整句挂 title：这一列宽 190px，长句会折成「…这里只」/「读」
 * 这种把半个词甩到下一行的样子，比不说更难看。
 */
function SourceCell({ row }: { row: MergedRow }) {
  if (row.published) {
    return (
      <>
        <Badge tone={row.enabled ? "ok" : "danger"}>{row.enabled ? "已发布" : "已停用"}</Badge>
        {row.shadowed ? (
          <div
            className="mt-1 truncate text-label text-muted"
            title="MCP_SERVERS 里有同名服务端，这一条不生效"
          >
            被同名服务端盖住
          </div>
        ) : null}
      </>
    );
  }
  const [note, full] =
    row.source === "ops"
      ? ["改 MCP_SERVERS 生效", "连接器定义写在服务端的 MCP_SERVERS 里，控制台这边只读"]
      : row.source === "shared"
        ? ["改兼容期配置生效", "兼容期留下的单文件连接器配置，控制台这边只读"]
        : ["平台看不到配置", "用户在连接器页自己添加的，平台这边看不到具体配置"];
  return (
    <>
      <Badge>{row.sourceLabel || "用户自建"}</Badge>
      <div className="mt-1 truncate text-label text-muted" title={full}>
        {note}
      </div>
    </>
  );
}

function TenantsCell({ row }: { row: MergedRow }) {
  if (!row.tenants.length) {
    return <span className="text-label text-muted">暂无租户连接</span>;
  }
  return (
    <div className="flex flex-col gap-1">
      {row.tenants.map((t) => (
        <div key={t.tenant} className="flex items-center gap-1.5 whitespace-nowrap">
          <span className="rounded-sm border border-line px-1.5 text-label text-ink-2">
            {t.tenant}
          </span>
          <Badge tone={t.enabled ? "ok" : "danger"}>{t.enabled ? "已连接" : "已断开"}</Badge>
          <span
            className={cn("whitespace-nowrap text-label", t.credentialed ? "text-ok" : "text-muted")}
          >
            {t.credentialed ? "凭据已配" : "无凭据"}
          </span>
        </div>
      ))}
    </div>
  );
}

/* =============================================================================
 * 新增 / 编辑表单
 * ========================================================================== */

function ConnectorForm({
  existing,
  onClose,
  onSaved,
}: {
  existing: PlatformConnector | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const isNew = !existing;
  const [transport, setTransport] = useState<Transport>(
    existing?.transport === "sse" ? "sse" : "streamable_http",
  );
  const [name, setName] = useState(existing?.name ?? "");
  const [desc, setDesc] = useState(existing?.description ?? "");
  const [url, setUrl] = useState(existing?.url ?? "");
  const [auth, setAuth] = useState(existing?.auth === "oauth" ? "oauth" : "");
  const [issuer, setIssuer] = useState(existing?.oauth_issuer ?? "");
  const [scopes, setScopes] = useState((existing?.oauth_scopes || []).join(" "));
  const [headers, setHeaders] = useState(JSON.stringify(existing?.headers || {}, null, 2));
  const [enabled, setEnabled] = useState(existing ? existing.enabled !== false : true);
  const [analysis, setAnalysis] = useState<Analysis | null>(
    existing ? (analysisFromExisting(existing) as Analysis) : null,
  );
  const [hit, setHit] = useState<{ tone: "ok" | "bad"; text: React.ReactNode } | null>(null);
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState("");
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const id = existing?.id || connectorIdFrom(name.trim(), url.trim()) || "";
  const stdio = analysis?.stdio ?? null;

  /* 打开表单那一刻的字段快照。撤销要回到「手填了什么」而不是清空——
     清空等于把管理员之前敲的字也一起删了。 */
  const before = useRef({ name, desc, url, auth, issuer, scopes, headers, transport, analysis });

  const applyAnalysis = (found: Analysis) => {
    setAnalysis(found);
    if (found.name) setName(found.name);
    if (found.description) setDesc(found.description.slice(0, 200));
    const pick = (["streamable_http", "sse"] as Transport[]).find((tp) =>
      (found.transports || []).some((t) => t.transport === tp && t.url),
    );
    if (pick) {
      setTransport(pick);
      const hitUrl = (found.transports || []).find((t) => t.transport === pick && t.url);
      if (hitUrl) setUrl(hitUrl.url);
    } else if (found.stdio) {
      setTransport("stdio");
    }
    if (found.auth.mode === "oauth") {
      setAuth("oauth");
      if (found.auth.issuer) setIssuer(found.auth.issuer);
      if ((found.auth.scopes || []).length) setScopes(found.auth.scopes.join(" "));
      // OAuth 和静态头互斥：上一次解析残留的 Bearer 不能跟着提交上去
      setHeaders("{}");
    } else if (found.auth.mode === "headers") {
      setAuth("");
      setHeaders(JSON.stringify(found.auth.headers, null, 2));
    }
  };

  const undo = () => {
    const b = before.current;
    setName(b.name);
    setDesc(b.desc);
    setUrl(b.url);
    setAuth(b.auth);
    setIssuer(b.issuer);
    setScopes(b.scopes);
    setHeaders(b.headers);
    setTransport(b.transport);
    setAnalysis(b.analysis);
    setHit(null);
  };

  const importText = (raw: string, label: string) => {
    const text = String(raw || "").replace(/\r\n?/g, "\n");
    if (!text.trim()) {
      setHit({ tone: "bad", text: `「${label}」是空的，没有可读的内容。` });
      return;
    }
    const found = analyzeConnectorDoc(text) as Analysis;

    if (!found.transports.length && !found.stdio) {
      setAnalysis(found);
      setHit({
        tone: "bad",
        text: (
          <>
            没从「{label}」里认出连接配置。我找的是：一段 <Code>mcpServers</Code> JSON、一个像 MCP
            端点的地址（含 <Code>/mcp</Code> 或 <Code>/sse</Code>），或一条 <Code>npx</Code> /{" "}
            <Code>uvx</Code> / <Code>python -m</Code> 启动命令。
            {found.name ? `识别到的标题是「${found.name}」，其余字段请手填。` : ""}
          </>
        ),
      });
      if (found.name) setName(found.name);
      return;
    }

    applyAnalysis(found);

    const bits: React.ReactNode[] = [];
    if (found.transports.length) {
      bits.push(
        <span key="tp">
          <b>方式 {found.transports.length} 条</b>{" "}
          {found.transports.map((t) => `${TRANSPORTS[t.transport].label} ${t.url}`).join("；")}
        </span>,
      );
    }
    if (found.stdio) {
      bits.push(
        <span key="stdio">
          <b>另有 stdio</b> {found.stdio.command}（管理台不托管）
        </span>,
      );
    }
    if (found.auth.mode === "headers") {
      const n = Object.keys(found.auth.headers).length;
      if (n) bits.push(<span key="hdr">{`请求头 ${n} 个（在「鉴权与高级」里）`}</span>);
    }
    if (found.auth.mode === "oauth") bits.push(<span key="oauth">鉴权 OAuth（在「鉴权与高级」里）</span>);
    if (found.serverCount > 1) {
      bits.push(<span key="multi">文档里有 {found.serverCount} 个服务，这里只分析了第一个</span>);
    }
    setHit({
      tone: "ok",
      text: (
        <>
          从{found.source === "json" ? " JSON 配置" : "文档正文"}里认出：
          {bits.map((b, i) => (
            <span key={i}>
              {i ? " · " : ""}
              {b}
            </span>
          ))}
          。核对一下再发布。
        </>
      ),
    });
  };

  const handleFile = async (file: File) => {
    try {
      const text = (await readAnyDocText(file)) as string;
      importText(text, file.name || "文件");
    } catch (err) {
      setHit({ tone: "bad", text: (err as Error).message || String(err) });
    }
  };

  // 粘贴：只在焦点不在输入框里时接管，否则会把管理员正常贴进地址栏的内容抢走
  useEffect(() => {
    const onPaste = (event: ClipboardEvent) => {
      const tag = document.activeElement?.tagName || "";
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) return;
      const text = event.clipboardData?.getData("text");
      if (!text || !text.trim()) return;
      event.preventDefault();
      importText(text, "粘贴的内容");
    };
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  });

  const save = async () => {
    if (transport === "stdio") {
      setState("stdio 不从管理台发布：用上面的片段走 MCP_SERVERS。");
      return;
    }
    let parsed: Record<string, string>;
    try {
      parsed = JSON.parse(headers.trim() || "{}") as Record<string, string>;
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("请求头必须是 JSON 对象");
      }
    } catch (err) {
      setState(`请求头不是合法 JSON：${(err as Error).message}`);
      return;
    }
    if (!name.trim()) {
      setState("显示名不能为空");
      return;
    }
    if (!id) {
      setState("显示名拿不出工具名前缀，加几个字母或数字");
      return;
    }
    if (!/^https?:\/\//i.test(url.trim())) {
      setState("MCP 地址要以 http:// 或 https:// 开头");
      return;
    }
    setBusy(true);
    setState("保存中…");
    try {
      await consoleApi.saveConnector({
        id,
        name: name.trim(),
        icon: existing?.icon || guessIcon() || "🔗",
        transport,
        url: url.trim(),
        description: desc.trim(),
        enabled,
        auth,
        oauth_issuer: issuer.trim(),
        oauth_scopes: scopes.split(/\s+/).filter(Boolean),
        headers: parsed,
      });
      toast(isNew ? "已发布，用户连接器页现在就能看到" : "已保存");
      onSaved();
    } catch (err) {
      setState(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const snippet = useMemo(() => {
    const s = stdio || { command: "", args: [] as string[], env: {} as Record<string, string> };
    const entry: Record<string, unknown> = { id: id || "connector", transport: "stdio" };
    entry.stdio_command = s.command || "";
    if ((s.args || []).length) entry.stdio_args = s.args;
    if (s.env && Object.keys(s.env).length) entry.stdio_env = s.env;
    entry.title = name.trim() || id || "connector";
    return JSON.stringify(entry, null, 2);
  }, [id, name, stdio]);

  const copySnippet = async () => {
    try {
      await navigator.clipboard.writeText(snippet);
      toast("片段已复制，交给运维写进 MCP_SERVERS");
    } catch {
      toast("复制失败，请手动选中上面的片段", "error");
    }
  };

  const detected =
    Boolean(analysis) &&
    analysis!.source !== "saved" &&
    (analysis!.transports || []).length + (analysis!.stdio ? 1 : 0) > 0;

  return (
    <Drawer
      open
      onClose={onClose}
      title={isNew ? "新增平台连接器" : `编辑连接器 · ${existing?.name || existing?.id}`}
      width="wide"
      header={
        <DrawerHead
          title={isNew ? "新增平台连接器" : `编辑连接器 · ${existing?.name || existing?.id}`}
          hint={id ? `工具名前缀 ${id}` : "工具名前缀按显示名自动生成"}
          onClose={onClose}
        />
      }
    >
      {/* 拖入对接文档 */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => fileRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            fileRef.current?.click();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const file = e.dataTransfer.files?.[0];
          if (!file) return;
          // 拖文件夹进来时 files[0] 是个 size=0、无 type 的壳，读出来是空字符串
          if (file.size === 0 && !file.type) {
            setHit({
              tone: "bad",
              text: `「${file.name}」看起来是文件夹。请拖里面的具体文档，或另存为 .md / .txt 再传。`,
            });
            return;
          }
          void handleFile(file);
        }}
        className={cn(
          "flex cursor-pointer items-center gap-3 rounded-lg border border-dashed px-3.5 py-3 transition-colors duration-150 ease-standard",
          dragging ? "border-signal bg-signal-wash" : "border-line-strong hover:bg-rail",
        )}
      >
        <span aria-hidden="true" className="text-title">
          📄
        </span>
        <div className="min-w-0">
          <div className="text-body font-medium text-ink">拖入对接文档，自动分析连接条件</div>
          <div className="text-label leading-relaxed text-muted">
            .json / .md / .txt / .docx —— 也可以点这里选文件，或按 ⌘V / Ctrl+V 粘贴
          </div>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept=".json,.md,.markdown,.txt,.yaml,.yml,.docx"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void handleFile(file);
            // 清空才能让「改完同一个文件再传一次」也触发 change
            e.target.value = "";
          }}
        />
      </div>

      {hit ? (
        <div
          className={cn(
            "mt-2 flex items-start gap-3 rounded-md px-3 py-2 text-secondary leading-relaxed",
            hit.tone === "bad" ? "bg-danger-wash text-danger" : "bg-ok-wash text-ok",
          )}
        >
          <div className="min-w-0 flex-1">{hit.text}</div>
          {hit.tone === "ok" ? (
            <button
              type="button"
              onClick={undo}
              className="shrink-0 underline underline-offset-2 hover:no-underline"
            >
              撤销
            </button>
          ) : null}
        </div>
      ) : null}

      {/* 连接条件 */}
      <div className="mt-3 overflow-hidden rounded-lg border border-line">
        <div className="hairline-b flex items-center gap-2 bg-rail/60 px-3 py-2">
          <span className="text-secondary font-medium text-ink-2">连接条件</span>
          <span className="ml-auto text-label text-muted">
            {!analysis
              ? "手填"
              : analysis.source === "saved"
                ? "来自已有配置"
                : analysis.source === "json"
                  ? "来自 JSON 配置"
                  : "来自文档正文"}
          </span>
        </div>
        <div className="px-3 py-2">
          <AnalysisRows analysis={analysis} transport={transport} />
        </div>
      </div>

      <Grid className="mt-3">
        <Field label="显示名" htmlFor="cf-name" span2 note="用户在连接器页看到的名字">
          <input
            id="cf-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="简介" htmlFor="cf-desc" span2 note="一句话说明它能干什么">
          <input
            id="cf-desc"
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            className={inputCls}
          />
        </Field>
      </Grid>

      <div className="mt-3">
        <div className="text-secondary font-medium text-ink-2">连接方式</div>
        <div className="mt-1.5 grid gap-2 sm:grid-cols-3">
          {TP_ORDER.map((tp) => {
            const active = transport === tp;
            const found =
              Boolean(analysis) &&
              (tp === "stdio"
                ? Boolean(analysis!.stdio)
                : (analysis!.transports || []).some((t) => t.transport === tp && t.url));
            return (
              <button
                key={tp}
                type="button"
                aria-pressed={active}
                onClick={() => {
                  setTransport(tp);
                  // 文档里同时给了 /mcp 和 /sse 时，切换方式顺手把地址换过去
                  if (tp !== "stdio" && analysis) {
                    const same = (analysis.transports || []).find((t) => t.transport === tp && t.url);
                    if (same) setUrl(same.url);
                  }
                }}
                className={cn(
                  "flex flex-col items-start gap-0.5 rounded-md border px-2.5 py-2 text-left transition-colors duration-150 ease-standard",
                  active
                    ? "border-signal bg-signal-wash"
                    : "border-line hover:border-line-strong hover:bg-rail",
                )}
              >
                <span className="flex items-center gap-1.5 text-secondary font-medium text-ink">
                  {TRANSPORTS[tp].label}
                  {found ? (
                    <span
                      title="文档里有这条"
                      className="inline-block h-1.5 w-1.5 rounded-full bg-ok"
                    />
                  ) : null}
                </span>
                <span className="text-label leading-snug text-muted">{TRANSPORTS[tp].sub}</span>
              </button>
            );
          })}
        </div>
        <p className="mt-1.5 text-label text-muted">
          {TRANSPORTS[transport].note}
          {detected ? " · 带绿点的是文档里给过的方式" : ""}
        </p>
      </div>

      {transport === "stdio" ? (
        <div className="mt-3">
          <p className="rounded-md bg-warn-wash px-3 py-2 text-secondary leading-relaxed text-warn">
            管理台不托管 stdio：网页里能填一条命令，等于把远程执行开放给所有人。下面这段交给运维写进{" "}
            <Code>MCP_SERVERS</Code>，重启后和平台连接器一样按用户授权。
          </p>
          <pre className="mt-2 overflow-x-auto rounded-md bg-sunken px-3 py-2 font-mono text-label leading-relaxed text-ink-2">
            {snippet}
          </pre>
          <div className="mt-2 flex items-center gap-2">
            <Button size="sm" onClick={() => void copySnippet()}>
              复制片段
            </Button>
            <span className="text-label text-muted">
              粘贴进部署配置的 <Code>MCP_SERVERS</Code> 数组
            </span>
          </div>
        </div>
      ) : (
        <Field label="MCP 地址" htmlFor="cf-url" className="mt-3" note="https://…/mcp —— 中台只会连这个地址">
          <input
            id="cf-url"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className={inputCls}
          />
        </Field>
      )}

      <details className="mt-3 rounded-lg border border-line px-3.5 py-2.5" open={auth === "oauth"}>
        <summary className="cursor-pointer text-secondary font-medium text-ink-2">鉴权与高级</summary>
        <Grid className="mt-3">
          <Field label="授权方式" htmlFor="cf-auth">
            <select
              id="cf-auth"
              value={auth}
              onChange={(e) => setAuth(e.target.value)}
              className={cn(selectCls, "w-full")}
            >
              <option value="">静态凭据 / 无需授权</option>
              <option value="oauth">OAuth（用户各自授权）</option>
            </select>
          </Field>
          <div className="flex items-start pt-6">
            <label className="flex items-center gap-2 text-body text-ink-2">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                className="h-3.5 w-3.5 accent-[var(--color-signal)]"
              />
              立即发布（所有用户的连接器页可见）
            </label>
          </div>

          {auth === "oauth" ? (
            <>
              <Field label="OAuth issuer" htmlFor="cf-issuer" note="如 https://auth.example.com">
                <input
                  id="cf-issuer"
                  value={issuer}
                  onChange={(e) => setIssuer(e.target.value)}
                  className={inputCls}
                />
              </Field>
              <Field label="OAuth scopes" htmlFor="cf-scopes" note="空格分隔，可留空">
                <input
                  id="cf-scopes"
                  value={scopes}
                  onChange={(e) => setScopes(e.target.value)}
                  className={inputCls}
                />
              </Field>
            </>
          ) : (
            <Field
              label="静态请求头（JSON 对象）"
              htmlFor="cf-headers"
              span2
              note="整平台共用的一组头。写在这里等于把同一把钥匙发给所有人——需要按人区分的请用 OAuth。"
            >
              <textarea
                id="cf-headers"
                rows={3}
                value={headers}
                onChange={(e) => setHeaders(e.target.value)}
                className={cn(textareaCls, "font-mono text-label")}
              />
            </Field>
          )}

          <Field
            label="工具名前缀"
            htmlFor="cf-id"
            span2
            note="按显示名自动生成，不用手填。它决定工具叫什么（前缀__工具名），用户的授权记录也挂在它下面，所以建好后不再改。"
          >
            <input
              id="cf-id"
              readOnly
              value={id}
              className={cn(inputCls, "bg-sunken font-mono text-label text-muted")}
            />
          </Field>
        </Grid>
      </details>

      <div className="mt-4 flex items-center gap-3">
        {transport === "stdio" ? null : (
          <Button variant="primary" onClick={() => void save()} disabled={busy}>
            {isNew ? "发布" : "保存"}
          </Button>
        )}
        <Button onClick={onClose}>取消</Button>
        {state ? <span className="text-secondary text-danger">{state}</span> : null}
      </div>
    </Drawer>
  );
}

/** 「连接条件」那块：不管有没有上传文档它都在——编辑已有连接器时它就是当前生效的配置摘要。 */
function AnalysisRows({
  analysis,
  transport,
}: {
  analysis: Analysis | null;
  transport: Transport;
}) {
  if (!analysis) {
    return (
      <p className="py-1 text-secondary leading-relaxed text-muted">
        上传对接文档后，这里会列出连接它需要的全部条件：能用哪几种方式连、各自的地址或启动命令、
        鉴权要求、以及要提前打通的网络。没有文档也能直接手填，填什么这里就显示什么。
      </p>
    );
  }

  const rows: { k: string; v: React.ReactNode; tone?: string }[] = [];
  if (analysis.name) rows.push({ k: "服务", v: analysis.name });
  if (analysis.description) rows.push({ k: "用途", v: analysis.description });

  if (transport === "stdio") {
    const s = analysis.stdio;
    rows.push({ k: "方式", v: "stdio（本机子进程）" });
    if (s?.command) {
      rows.push({ k: "命令", v: <Code>{[s.command, ...(s.args || [])].join(" ")}</Code> });
      const envKeys = Object.keys(s.env || {});
      if (envKeys.length) rows.push({ k: "环境变量", v: <Code>{envKeys.join("、")}</Code> });
      if (s.cwd) rows.push({ k: "工作目录", v: <Code>{s.cwd}</Code> });
      if (s.runtime) rows.push({ k: "运行环境", v: s.runtime });
    } else {
      rows.push({ k: "命令", v: <span className="text-muted">文档里没给启动命令，只能手补</span> });
    }
  } else {
    const found = (analysis.transports || []).find((t) => t.transport === transport);
    rows.push({
      k: "方式",
      v: TRANSPORTS[transport].label + (found ? "" : "（文档里没提这一条）"),
    });
    rows.push({
      k: "地址",
      v: found?.url ? <Code>{found.url}</Code> : <span className="text-muted">未填</span>,
    });
    const others = (analysis.transports || []).filter((t) => t.transport !== transport);
    if (others.length) {
      rows.push({
        k: "也能用",
        v: others.map((t) => `${TRANSPORTS[t.transport].label} ${t.url}`).join("；"),
      });
    }
  }

  const auth = analysis.auth || { mode: "none" as const, headers: {}, issuer: "", scopes: [] };
  if (transport === "stdio") {
    rows.push({ k: "鉴权", v: "不适用：stdio 没有请求头，凭据只能通过 env 传给子进程" });
  } else if (auth.mode === "oauth") {
    rows.push({
      k: "鉴权",
      v: `OAuth（用户各自授权）${auth.issuer ? ` · issuer ${auth.issuer}` : " · 文档里没给 issuer"}${
        (auth.scopes || []).length ? ` · scopes ${auth.scopes.join(" ")}` : ""
      }`,
      tone: auth.issuer ? undefined : "warn",
    });
  } else if (auth.mode === "headers") {
    const keys = Object.keys(auth.headers || {});
    rows.push({
      k: "鉴权",
      v: `静态请求头 ${keys.length} 个：${keys.join("、")}（值在「鉴权与高级」里）`,
    });
  } else {
    rows.push({ k: "鉴权", v: "文档里没提，按「无需授权」处理" });
  }

  (analysis.warnings || []).forEach((w) => {
    // 已经站在 stdio 上了就不用再提醒「它还能用 stdio」；静态凭据那条对子进程也不成立
    if (transport === "stdio" && (w.k === "stdio" || w.k === "凭据")) return;
    rows.push({ k: w.k || "注意", v: w.text, tone: w.tone });
  });

  return (
    <dl className="grid gap-x-3 gap-y-1.5 text-secondary">
      {rows.map((row, i) => (
        <div key={i} className="grid grid-cols-[56px_1fr] items-baseline">
          <dt className="text-label text-muted">{row.k}</dt>
          <dd
            className={cn(
              "min-w-0 leading-relaxed",
              row.tone === "warn" ? "text-warn" : "text-ink-2",
            )}
          >
            {row.v}
          </dd>
        </div>
      ))}
    </dl>
  );
}
