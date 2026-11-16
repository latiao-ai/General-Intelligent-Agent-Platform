import { useCallback, useEffect, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { useToast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { consoleApi } from "./api";
import { num, shortTime, when } from "./format";
import {
  Code,
  DrawerHead,
  Field,
  Grid,
  inputCls,
  Loading,
  Panel,
  Sub,
  TableWrap,
  Tag,
  tdCls,
  tdMonoCls,
  tdNumCls,
  thCls,
  thNumCls,
  Toolbar,
  useAsk,
} from "./primitives";
import type { LoginPlatform, PlatformConfig, PlatformTestResult } from "./types";

const DEFAULT_CONFIG: PlatformConfig = {
  login_path: "/auth/login",
  username_field: "username",
  password_field: "password",
  subject_path: "data.user.id",
  display_name_path: "data.user.username",
  status_path: "data.user.status",
  role_path: "data.user.role",
  timeout_seconds: 10,
};

/**
 * 登录系统。
 *
 * 「字段映射」默认收起：只有对方返回结构和 beeftv 不一样时才需要改，
 * 常显会让 90% 的人去猜这八格要不要填。但「用账号密码试一次」必须留在外面——
 * 配完不试，等于把问题留给第一个来登录的同事。
 */
export function PlatformsTab() {
  const toast = useToast();
  const [items, setItems] = useState<LoginPlatform[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<{ platformId: string } | "new" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await consoleApi.platforms();
      setItems(data.platforms || []);
    } catch (err) {
      setError(errorMessage(err));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (p: LoginPlatform) => {
    try {
      await consoleApi.updatePlatform(p.platform_id, { enabled: !p.enabled });
      toast(p.enabled ? "已停用" : "已启用");
      await load();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const enabledCount = items.filter((p) => p.enabled).length;

  return (
    <>
      <Panel
        title="登录平台"
        hint={
          loading
            ? "读取中…"
            : items.length
              ? `共 ${items.length} 个，其中 ${enabledCount} 个启用`
              : "还没有登记任何平台"
        }
        actions={
          <Button variant="primary" onClick={() => setEditing("new")}>
            新增平台
          </Button>
        }
      >
        <Toolbar>
          <Button onClick={() => void load()}>刷新</Button>
          <span className="text-label text-muted">
            决定「拿哪个平台的账号能登录中台」。配置改完先试一次再让人用。
          </span>
        </Toolbar>

        {error ? (
          <div className="flex flex-col items-start gap-3 py-6">
            <p className="text-body text-ink-2">{error}</p>
            <Button onClick={() => void load()}>重试</Button>
          </div>
        ) : loading ? (
          <Loading rows={4} label="正在加载平台" />
        ) : items.length === 0 ? (
          <div className="py-6 text-body leading-relaxed text-muted">
            <p>还没有平台。登录入口现在靠环境变量（BEEFTV_API_BASE）撑着。</p>
            <p className="mt-1">在这里新增一个平台，就可以把它废掉。</p>
          </div>
        ) : (
          <TableWrap>
            <table className="w-full min-w-[900px] border-collapse text-body">
              <thead>
                <tr>
                  <th className={thCls}>平台</th>
                  <th className={thCls}>方式</th>
                  <th className={thCls}>接口地址</th>
                  <th className={thCls}>租户 / 身份源</th>
                  <th className={thNumCls}>已绑定账号</th>
                  <th className={thCls}>最近登录</th>
                  <th className={thCls}>状态</th>
                  <th className={thCls} />
                </tr>
              </thead>
              <tbody>
                {items.map((p) => (
                  <tr key={p.platform_id} className="hover:bg-rail/70">
                    <td className={tdCls}>
                      <div className="flex items-center gap-1.5">
                        <span className="text-ink">{p.display_name || p.platform_id}</span>
                        {p.is_default ? <Tag>默认</Tag> : null}
                      </div>
                      <div className="font-mono text-label text-muted">{p.platform_id}</div>
                    </td>
                    <td className={tdCls}>
                      <Tag>
                        {p.login_mode === "password_http" ? "账号密码" : p.login_mode || "—"}
                      </Tag>
                    </td>
                    <td className={cn(tdMonoCls, "max-w-[260px] truncate")} title={p.base_url}>
                      {p.base_url || "—"}
                    </td>
                    <td className={tdCls}>
                      <Code>{p.tenant_id}</Code>
                      <div className="font-mono text-label text-muted">{p.identity_provider_id}</div>
                    </td>
                    <td className={tdNumCls}>{num(p.accounts)}</td>
                    <td className={cn(tdCls, "whitespace-nowrap text-secondary")}>
                      {when(p.last_login_at)}
                    </td>
                    <td className={tdCls}>
                      <Badge tone={p.enabled ? "ok" : "danger"}>
                        {p.enabled ? "启用" : "已停用"}
                      </Badge>
                    </td>
                    <td className={cn(tdCls, "whitespace-nowrap")}>
                      <div className="flex items-center gap-1">
                        <Button size="sm" onClick={() => setEditing({ platformId: p.platform_id })}>
                          编辑
                        </Button>
                        <Button size="sm" onClick={() => void toggle(p)}>
                          {p.enabled ? "停用" : "启用"}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Panel>

      <Panel title="这套机制怎么工作" className="mt-3">
        <dl className="grid gap-x-4 gap-y-2.5 text-body sm:grid-cols-[110px_1fr]">
          <dt className="text-label leading-[1.9] text-muted">验证方式</dt>
          <dd className="text-ink-2">
            中台把账号密码转给平台自己的登录接口校验，<b>不保存也不校验密码</b>。校验通过后按平台
            返回的用户标识绑定身份、发中台会话。
          </dd>
          <dt className="text-label leading-[1.9] text-muted">身份源</dt>
          <dd className="text-ink-2">
            同一个身份源下的多个入口，登录后落到同一个中台账号。想让两个平台共用账号，把「身份源」
            填成一样。
          </dd>
          <dt className="text-label leading-[1.9] text-muted">租户</dt>
          <dd className="text-ink-2">
            从这个平台首次登录的人，默认落在这个租户里。已有绑定的人不受影响。
          </dd>
          <dt className="text-label leading-[1.9] text-muted">票据 / OIDC</dt>
          <dd className="text-ink-2">
            这两种接入方式还不在这里管：它们要公钥或客户端密钥，目前仍走 <Code>AUTH_CONNECTIONS</Code> /{" "}
            <Code>OIDC_*</Code> 配置。
          </dd>
        </dl>
      </Panel>

      {editing ? (
        <PlatformForm
          existing={
            editing === "new"
              ? null
              : items.find((p) => p.platform_id === editing.platformId) ?? null
          }
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

function PlatformForm({
  existing,
  onClose,
  onSaved,
}: {
  existing: LoginPlatform | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const ask = useAsk();
  const isNew = !existing;
  const config = { ...DEFAULT_CONFIG, ...(existing?.config || {}) };

  const [form, setForm] = useState({
    platform_id: existing?.platform_id ?? "",
    display_name: existing?.display_name ?? "",
    base_url: existing?.base_url ?? "",
    tenant_id: existing?.tenant_id ?? "default",
    identity_provider_id: existing?.identity_provider_id ?? "",
    note: existing?.note ?? "",
    is_default: Boolean(existing?.is_default),
  });
  const [cfg, setCfg] = useState<Required<PlatformConfig>>({
    login_path: config.login_path || "",
    username_field: config.username_field || "",
    password_field: config.password_field || "",
    subject_path: config.subject_path || "",
    display_name_path: config.display_name_path || "",
    status_path: config.status_path || "",
    role_path: config.role_path || "",
    timeout_seconds: config.timeout_seconds ?? 10,
  });
  const [state, setState] = useState("");
  const [busy, setBusy] = useState(false);

  const [testOpen, setTestOpen] = useState(false);
  const [testUser, setTestUser] = useState("");
  const [testPass, setTestPass] = useState("");
  const [testResult, setTestResult] = useState<PlatformTestResult | null>(null);
  const [testError, setTestError] = useState("");

  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  const setC = <K extends keyof typeof cfg>(key: K, value: (typeof cfg)[K]) =>
    setCfg((c) => ({ ...c, [key]: value }));

  const save = async () => {
    const body = {
      ...form,
      identity_provider_id: form.identity_provider_id.trim(),
      config: { ...cfg, timeout_seconds: Number(cfg.timeout_seconds || 10) },
    };
    setBusy(true);
    setState("保存中…");
    setTestError("");
    try {
      if (isNew) {
        await consoleApi.createPlatform(body);
        toast("平台已创建");
      } else {
        const patch: Record<string, unknown> = { ...body };
        delete patch.platform_id;
        await consoleApi.updatePlatform(existing.platform_id, patch);
        toast("已保存");
      }
      onSaved();
    } catch (err) {
      setState("");
      setTestError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const runTest = async () => {
    if (isNew) {
      setTestResult(null);
      setTestError("先创建平台，再试登录。测试要读已保存的配置。");
      return;
    }
    setTestError("");
    setTestResult(null);
    try {
      const result = await consoleApi.testPlatform(
        existing.platform_id,
        testUser.trim(),
        testPass,
      );
      setTestResult(result);
    } catch (err) {
      setTestError(errorMessage(err));
    }
  };

  const remove = async () => {
    const typed = await ask.ask({
      title: `删除「${existing?.display_name || existing?.platform_id}」`,
      tone: "danger",
      confirmText: "删除",
      body: (
        <>
          <p>删除后这个入口就登不进来了。已经建立的账号和历史会话都还在，只是不能再从这里登录。</p>
          <p className="mt-2 text-danger">不可撤销。</p>
        </>
      ),
      input: {
        label: `输入平台标识 ${existing?.platform_id} 确认`,
        placeholder: existing?.platform_id,
        required: true,
      },
    });
    if (typed === null) return;
    if (typed !== existing?.platform_id) {
      toast("输入不一致，已取消", "error");
      return;
    }
    try {
      await consoleApi.deletePlatform(existing.platform_id);
      toast("已删除");
      onSaved();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  return (
    <Drawer
      open
      onClose={onClose}
      title={isNew ? "新增登录平台" : `编辑平台 · ${existing?.platform_id}`}
      width="wide"
      header={
        <DrawerHead
          title={isNew ? "新增登录平台" : `编辑平台 · ${existing?.platform_id}`}
          hint={isNew ? "建好后 platform_id 不能改" : "标识是审计与身份记录里的历史值"}
          onClose={onClose}
        />
      }
    >
      <Grid>
        <Field
          label="平台标识"
          htmlFor="pf-id"
          note={isNew ? "小写字母开头，用于审计与身份记录，建好后不能改" : "建好后不能改"}
        >
          <input
            id="pf-id"
            value={form.platform_id}
            readOnly={!isNew}
            onChange={(e) => set("platform_id", e.target.value)}
            className={cn(inputCls, !isNew && "bg-sunken text-muted")}
          />
        </Field>
        <Field label="显示名" htmlFor="pf-name" note="登录页上给人看的名字">
          <input
            id="pf-name"
            value={form.display_name}
            onChange={(e) => set("display_name", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field
          label="平台接口根地址"
          htmlFor="pf-base"
          span2
          note="例如 http://127.0.0.1:8004/api —— 中台会在这里拼登录路径"
        >
          <input
            id="pf-base"
            type="url"
            value={form.base_url}
            onChange={(e) => set("base_url", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="租户" htmlFor="pf-tenant" note="首次从这个平台登录的人落在哪个租户">
          <input
            id="pf-tenant"
            value={form.tenant_id}
            onChange={(e) => set("tenant_id", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="身份源" htmlFor="pf-idp" note="两个平台填同一个值 = 共用账号；留空则等于平台标识">
          <input
            id="pf-idp"
            value={form.identity_provider_id}
            onChange={(e) => set("identity_provider_id", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="备注" htmlFor="pf-note" span2>
          <input
            id="pf-note"
            value={form.note}
            onChange={(e) => set("note", e.target.value)}
            className={inputCls}
          />
        </Field>
      </Grid>

      <label className="mt-3 flex items-center gap-2 text-body text-ink-2">
        <input
          type="checkbox"
          checked={form.is_default}
          onChange={(e) => set("is_default", e.target.checked)}
          className="h-3.5 w-3.5 accent-[var(--color-signal)]"
        />
        设为默认平台{isNew ? "（第一个平台自动成为默认）" : ""}
      </label>

      <details className="mt-4 rounded-lg border border-line px-3.5 py-2.5">
        <summary className="cursor-pointer text-secondary font-medium text-ink-2">
          字段映射（平台返回的结构和 beeftv 不一样时才需要改）
        </summary>
        <Grid className="mt-3">
          <Field label="登录路径" htmlFor="cf-login" note="拼在根地址后面">
            <input
              id="cf-login"
              value={cfg.login_path}
              onChange={(e) => setC("login_path", e.target.value)}
              className={inputCls}
            />
          </Field>
          <Field label="超时（秒）" htmlFor="cf-timeout" note="1-30">
            <input
              id="cf-timeout"
              type="number"
              min={1}
              max={30}
              value={cfg.timeout_seconds}
              onChange={(e) => setC("timeout_seconds", Number(e.target.value))}
              className={inputCls}
            />
          </Field>
          <Field label="请求里的账号字段" htmlFor="cf-user">
            <input
              id="cf-user"
              value={cfg.username_field}
              onChange={(e) => setC("username_field", e.target.value)}
              className={inputCls}
            />
          </Field>
          <Field label="请求里的密码字段" htmlFor="cf-pass">
            <input
              id="cf-pass"
              value={cfg.password_field}
              onChange={(e) => setC("password_field", e.target.value)}
              className={inputCls}
            />
          </Field>
          <Field label="用户标识取值路径" htmlFor="cf-subject" note="点路径，如 data.user.id">
            <input
              id="cf-subject"
              value={cfg.subject_path}
              onChange={(e) => setC("subject_path", e.target.value)}
              className={inputCls}
            />
          </Field>
          <Field label="昵称取值路径" htmlFor="cf-name">
            <input
              id="cf-name"
              value={cfg.display_name_path}
              onChange={(e) => setC("display_name_path", e.target.value)}
              className={inputCls}
            />
          </Field>
          <Field label="状态取值路径" htmlFor="cf-status" note="取到的值不是 active 就拒绝登录">
            <input
              id="cf-status"
              value={cfg.status_path}
              onChange={(e) => setC("status_path", e.target.value)}
              className={inputCls}
            />
          </Field>
          <Field label="角色取值路径" htmlFor="cf-role" note="admin/owner/operator → operator，其余 member">
            <input
              id="cf-role"
              value={cfg.role_path}
              onChange={(e) => setC("role_path", e.target.value)}
              className={inputCls}
            />
          </Field>
        </Grid>
      </details>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={() => void save()} disabled={busy}>
          {isNew ? "创建" : "保存"}
        </Button>
        <Button
          onClick={() => {
            setTestOpen((v) => !v);
            setTestError("");
          }}
        >
          用账号密码试一次
        </Button>
        {state ? <span className="text-secondary text-muted">{state}</span> : null}
      </div>

      {testOpen ? (
        <div className="mt-3 rounded-lg border border-line bg-rail/60 p-3">
          <Grid>
            <Field label="测试账号" htmlFor="pf-test-user" note="只用于这次校验，不落库、不建账号">
              <input
                id="pf-test-user"
                value={testUser}
                onChange={(e) => setTestUser(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void runTest();
                }}
                className={inputCls}
              />
            </Field>
            <Field label="测试密码" htmlFor="pf-test-pass">
              <input
                id="pf-test-pass"
                type="password"
                value={testPass}
                onChange={(e) => setTestPass(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void runTest();
                }}
                className={inputCls}
              />
            </Field>
          </Grid>
          <Button className="mt-2" size="sm" onClick={() => void runTest()}>
            开始校验
          </Button>
          {testError ? (
            <p className="mt-2 rounded-md bg-danger-wash px-2.5 py-1.5 text-secondary text-danger">
              {testError}
            </p>
          ) : null}
          {testResult ? (
            <div
              className={cn(
                "mt-2 rounded-md px-2.5 py-1.5 text-secondary leading-relaxed",
                testResult.ok ? "bg-ok-wash text-ok" : "bg-danger-wash text-danger",
              )}
            >
              {testResult.ok ? (
                <>
                  校验通过：<b>{testResult.display_name}</b>（标识 {testResult.subject}，角色{" "}
                  {testResult.role}，身份源 {testResult.identity_provider_id}，
                  {num(testResult.elapsed_ms)}ms）
                </>
              ) : (
                <>
                  校验失败：{testResult.detail}（{num(testResult.elapsed_ms)}ms）
                </>
              )}
            </div>
          ) : null}
        </div>
      ) : null}

      {!isNew ? (
        <>
          <Sub>危险操作</Sub>
          <div className="flex items-start gap-4 rounded-lg border border-danger/35 bg-danger-wash/50 p-3.5">
            <div className="min-w-0 flex-1">
              <div className="text-body font-medium text-ink">删除这个平台</div>
              <p className="mt-1 text-secondary leading-relaxed text-muted">
                删掉后这个入口就登不进来了。已经建立的账号和历史会话都还在，只是不能再从这里登录。
              </p>
              <p className="mt-1 text-label text-muted">
                最近一次登录：{existing?.last_login_at ? shortTime(existing.last_login_at) : "—"}
              </p>
            </div>
            <Button variant="danger" onClick={() => void remove()}>
              删除
            </Button>
          </div>
        </>
      ) : null}

      {existing ? (
        <p className="mt-4 text-label text-muted">
          已绑定 {num(existing.accounts)} 个账号 · 登录方式{" "}
          <Code>{existing.login_mode || "password_http"}</Code>
        </p>
      ) : null}
    </Drawer>
  );
}
