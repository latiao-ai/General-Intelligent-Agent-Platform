import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { useToast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { consoleApi } from "./api";
import { BarChart } from "./BarChart";
import { auditDetail, auditLabel, compact, money, num, roleLabel, shortTime, when } from "./format";
import {
  Code,
  DrawerHead,
  Field,
  Grid,
  inputCls,
  KV,
  Loading,
  Panel,
  RowMenu,
  selectCls,
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
import type { UserDetail, UserRow } from "./types";

type DrawerState =
  | { kind: "detail"; userId: string }
  | { kind: "create" }
  | { kind: "reset"; userId: string }
  | null;

/**
 * 用户。
 *
 * 这一页是控制台里唯一会"删东西"的地方，所以三件事必须说清楚：
 *   1. 删之前先问详情接口要 purge_preview——不知道会清掉多少条就点删除，
 *      和赌博没区别；
 *   2. 角色下拉里保留历史角色（operator）。不在选项里时下拉会静默显示成
 *      member，看起来像"权限已经被改了"；
 *   3. 重置密码对来自外部身份源的人不是"改上游密码"，而是给同一个中台账号
 *      补一条本地凭据。表单文案必须讲明白，否则会被当成改别人的平台密码。
 */
export function UsersTab({ scope, focusUserId }: { scope: string; focusUserId?: string | null }) {
  const canGrantAdmin = scope === "all";
  const ask = useAsk();
  const toast = useToast();

  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [tenant, setTenant] = useState("");
  const [rows, setRows] = useState<UserRow[]>([]);
  const [total, setTotal] = useState(0);
  const [tenants, setTenants] = useState<string[]>([]);
  const [localByUser, setLocalByUser] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [drawer, setDrawer] = useState<DrawerState>(() =>
    focusUserId ? { kind: "detail", userId: focusUserId } : null,
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const params = new URLSearchParams({ limit: "100" });
    if (query.trim()) params.set("q", query.trim());
    if (status) params.set("status", status);
    if (tenant) params.set("tenant", tenant);
    try {
      const data = await consoleApi.users(params);
      setRows(data.users || []);
      setTotal(data.total || 0);
    } catch (err) {
      setError(errorMessage(err));
      setRows([]);
    } finally {
      setLoading(false);
    }

    if (scope === "all") {
      // 本地账号列表用来判断"这个人有没有本地密码"——旧页靠 platforms 里有没有
      // local 去猜，猜错就会给不该出现的人显示"解锁"
      try {
        const local = await consoleApi.localAccounts();
        setLocalByUser(
          Object.fromEntries((local.accounts || []).map((a) => [a.user_id, a.username])),
        );
      } catch {
        setLocalByUser({});
      }
    }
  }, [query, scope, status, tenant]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    consoleApi
      .overview(14)
      .then((d) => setTenants((d.tenants || []).map((t) => t.tenant_id)))
      .catch(() => setTenants([]));
  }, []);

  const changeRole = async (user: UserRow, role: string) => {
    if (role === user.role) return;
    const ok = await ask.confirm({
      title: `把角色改成「${roleLabel(role)}」？`,
      body: (
        <>
          <div className="font-mono text-label text-ink">{user.user_id}</div>
          <p className="mt-2">
            角色每次请求现读，保存后立即生效，不需要对方重新登录。
            {role === "platform_admin" ? " 超级管理员可以看到并修改所有租户的数据。" : ""}
          </p>
        </>
      ),
      confirmText: "保存",
    });
    if (!ok) return;
    try {
      const result = await consoleApi.setRole(user.user_id, role);
      toast(`已设为「${roleLabel(result.role)}」（影响 ${result.updated} 条身份）`);
      await load();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const changeStatus = async (user: UserRow) => {
    const next = user.status === "active" ? "disabled" : "active";
    const ok = await ask.confirm({
      title: `${next === "disabled" ? "停用" : "启用"}这个账号？`,
      body:
        next === "disabled" ? (
          <>
            <div className="font-mono text-label text-ink">{user.user_id}</div>
            <p className="mt-2 text-danger">停用会立即撤销对方所有登录会话。</p>
          </>
        ) : (
          <div className="font-mono text-label text-ink">{user.user_id}</div>
        ),
      confirmText: next === "disabled" ? "停用" : "启用",
      tone: next === "disabled" ? "danger" : "default",
    });
    if (!ok) return;
    try {
      const result = await consoleApi.setStatus(user.user_id, next);
      toast(
        next === "disabled" ? `已停用，撤销 ${result.revoked_sessions} 个会话` : "已启用",
      );
      await load();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const unlock = async (userId: string) => {
    try {
      await consoleApi.unlock(userId);
      toast("已解锁");
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  /* 删除是唯一不可逆的动作：先取一次详情，把"会清掉多少条"放到确认框里 */
  const remove = async (user: UserRow) => {
    let preview: { total: number } | null = null;
    try {
      const detail = await consoleApi.user(user.user_id, 1);
      preview = detail.purge_preview ?? null;
    } catch {
      /* 预览拿不到就退化成不带条数的确认 */
    }
    const ok = await ask.confirm({
      title: "彻底删除这个账号？",
      tone: "danger",
      confirmText: "删除",
      body: (
        <>
          <div className="font-mono text-label text-ink">
            {user.display_name ? `${user.display_name}（${user.user_id}）` : user.user_id}
          </div>
          <p className="mt-2">
            会清空：账号本身、登录会话、身份绑定、全部会话与消息、任务、运行记录、产物文件、
            凭据与私有工作目录。
          </p>
          {preview ? (
            <p className="mt-2 font-medium text-danger">
              预计清除 {num(preview.total)} 条数据库记录。
            </p>
          ) : null}
          <p className="mt-2 text-danger">此操作不可恢复。</p>
        </>
      ),
    });
    if (!ok) return;
    try {
      const result = await consoleApi.deleteUser(user.user_id);
      toast(`已删除，清除 ${num(result.deleted_rows)} 条记录`);
      setDrawer(null);
      await load();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const tenantOptions = useMemo(
    () => tenants.map((t) => ({ value: t, label: t })),
    [tenants],
  );

  return (
    <>
      <Panel
        title="用户"
        hint={loading ? "读取中…" : `共 ${num(total)} 人`}
        actions={
          <Button variant="primary" onClick={() => setDrawer({ kind: "create" })}>
            创建用户
          </Button>
        }
      >
        <Toolbar>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void load();
            }}
            placeholder="搜用户 ID / 昵称 / 邮箱"
            aria-label="搜索用户"
            className={cn(inputCls, "w-[260px]")}
          />
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            aria-label="按状态筛选"
            className={selectCls}
          >
            <option value="">全部状态</option>
            <option value="active">正常</option>
            <option value="disabled">已停用</option>
          </select>
          <select
            value={tenant}
            onChange={(e) => setTenant(e.target.value)}
            aria-label="按租户筛选"
            className={selectCls}
          >
            <option value="">全部租户</option>
            {tenantOptions.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <Button onClick={() => void load()}>查询</Button>
          <span className="text-label text-muted sm:hidden">左右滑动看全部列</span>
        </Toolbar>

        {error ? (
          <div className="flex flex-col items-start gap-3 py-6">
            <p className="text-body text-ink-2">{error}</p>
            <Button onClick={() => void load()}>重试</Button>
          </div>
        ) : loading && rows.length === 0 ? (
          <Loading rows={6} label="正在加载用户" />
        ) : rows.length === 0 ? (
          <p className="py-8 text-center text-body text-muted">
            {query || status || tenant ? "没有匹配的用户，放宽条件再试" : "还没有用户"}
          </p>
        ) : (
          <TableWrap>
            <table className="w-full min-w-[1080px] border-collapse text-body">
              <thead>
                <tr>
                  <th className={thCls}>用户</th>
                  <th className={thCls}>租户</th>
                  <th className={thCls}>子平台</th>
                  <th className={thCls}>角色</th>
                  <th className={thCls}>状态</th>
                  <th className={thCls}>最近活跃</th>
                  <th className={thNumCls}>会话</th>
                  <th className={thNumCls}>消息</th>
                  <th className={thNumCls}>任务</th>
                  <th className={thNumCls}>Token</th>
                  <th className={thNumCls}>花费</th>
                  <th className={thCls} />
                </tr>
              </thead>
              <tbody>
                {rows.map((u) => {
                  const active = u.status === "active";
                  const localName = localByUser[u.user_id];
                  return (
                    <tr key={u.user_id} className="hover:bg-rail/70">
                      <td className={tdCls}>
                        <div className="text-ink">{u.display_name || u.user_id}</div>
                        {u.display_name ? (
                          <div className="font-mono text-label text-muted">{u.user_id}</div>
                        ) : null}
                      </td>
                      <td className={tdCls}>
                        {(u.tenants || []).length ? (
                          (u.tenants || []).map((t) => <Tag key={t}>{t}</Tag>)
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                      <td className={tdCls}>
                        {(u.platforms || []).length ? (
                          (u.platforms || []).map((p) => <Tag key={p}>{p}</Tag>)
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                      <td className={tdCls}>
                        <select
                          value={u.role}
                          onChange={(e) => void changeRole(u, e.target.value)}
                          aria-label={`${u.user_id} 的角色`}
                          className={cn(selectCls, "h-[28px] text-secondary")}
                        >
                          {!["member", "platform_admin"].includes(u.role) ? (
                            <option value={u.role}>{roleLabel(u.role)}（历史）</option>
                          ) : null}
                          <option value="member">{roleLabel("member")}</option>
                          {canGrantAdmin ? (
                            <option value="platform_admin">{roleLabel("platform_admin")}</option>
                          ) : null}
                        </select>
                      </td>
                      <td className={tdCls}>
                        <Badge tone={active ? "ok" : "danger"}>{active ? "正常" : "已停用"}</Badge>
                      </td>
                      <td className={cn(tdCls, "whitespace-nowrap text-secondary")}>
                        {when(u.last_active_at)}
                        {u.last_active_source === "login" ? (
                          <span className="ml-1 text-label text-muted" title="只登录过，没开过会话">
                            登录
                          </span>
                        ) : null}
                      </td>
                      <td className={tdNumCls}>{num(u.sessions)}</td>
                      <td className={tdNumCls}>{num(u.messages)}</td>
                      <td className={tdNumCls}>
                        {num(u.tasks)}
                        {u.failed_tasks ? (
                          <span className="text-label text-muted"> / {num(u.failed_tasks)}</span>
                        ) : null}
                      </td>
                      <td className={tdNumCls}>{compact(u.input_tokens + u.output_tokens)}</td>
                      <td className={tdNumCls}>{money(u.charged_micros)}</td>
                      <td className={cn(tdCls, "whitespace-nowrap")}>
                        <div className="flex items-center gap-1">
                          <Button
                            size="sm"
                            onClick={() => setDrawer({ kind: "detail", userId: u.user_id })}
                          >
                            详情
                          </Button>
                          <RowMenu
                            label={`${u.user_id} 的更多操作`}
                            items={[
                              {
                                label: active ? "停用账号" : "启用账号",
                                onSelect: () => void changeStatus(u),
                              },
                              ...(canGrantAdmin
                                ? [
                                    {
                                      label: "重置密码",
                                      onSelect: () => setDrawer({ kind: "reset", userId: u.user_id }),
                                    },
                                    ...(localName
                                      ? [{ label: "解除登录锁定", onSelect: () => void unlock(u.user_id) }]
                                      : []),
                                    {
                                      label: "删除账号",
                                      danger: true,
                                      onSelect: () => void remove(u),
                                    },
                                  ]
                                : []),
                            ]}
                          />
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Panel>

      {drawer?.kind === "detail" ? (
        <UserDetailDrawer
          userId={drawer.userId}
          scope={scope}
          localName={localByUser[drawer.userId]}
          onClose={() => setDrawer(null)}
          onChanged={() => void load()}
          onReset={(userId) => setDrawer({ kind: "reset", userId })}
        />
      ) : null}

      {drawer?.kind === "create" ? (
        <CreateUserDrawer
          onClose={() => setDrawer(null)}
          onCreated={() => void load()}
        />
      ) : null}

      {drawer?.kind === "reset" ? (
        <ResetPasswordDrawer
          user={rows.find((r) => r.user_id === drawer.userId) ?? null}
          userId={drawer.userId}
          localName={localByUser[drawer.userId]}
          onClose={() => setDrawer(null)}
          onDone={() => void load()}
        />
      ) : null}
    </>
  );
}

/* ---------- 详情抽屉 ---------- */

function UserDetailDrawer({
  userId,
  scope,
  localName,
  onClose,
  onChanged,
  onReset,
}: {
  userId: string;
  scope: string;
  localName?: string;
  onClose: () => void;
  onChanged: () => void;
  onReset: (userId: string) => void;
}) {
  const ask = useAsk();
  const toast = useToast();
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    consoleApi
      .user(userId, 14)
      .then((d) => alive && setDetail(d))
      .catch((err) => alive && setError(errorMessage(err)));
    return () => {
      alive = false;
    };
  }, [userId]);

  const remove = async () => {
    const who = detail?.user?.display_name
      ? `${detail.user.display_name}（${userId}）`
      : userId;
    const ok = await ask.confirm({
      title: "彻底删除这个账号？",
      tone: "danger",
      confirmText: "删除",
      body: (
        <>
          <div className="font-mono text-label text-ink">{who}</div>
          <p className="mt-2">
            删除会清空该用户的全部会话、消息、任务、运行记录、产物、凭据与账号本身，不可恢复。
          </p>
          {detail?.purge_preview ? (
            <p className="mt-2 font-medium text-danger">
              预计清除 {num(detail.purge_preview.total)} 条数据库记录。
            </p>
          ) : null}
        </>
      ),
    });
    if (!ok) return;
    try {
      await consoleApi.deleteUser(userId);
      onChanged();
      onClose();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const user = detail?.user;
  const activity = detail?.activity ?? [];
  const msgTotal = activity.reduce((acc, row) => acc + Number(row.messages || 0), 0);
  const taskTotal = activity.reduce((acc, row) => acc + Number(row.tasks || 0), 0);

  return (
    <Drawer
      open
      onClose={onClose}
      title="用户详情"
      width="wide"
      header={<DrawerHead title={userId} hint={user?.display_name} onClose={onClose} />}
    >
      {error ? (
        <p className="py-8 text-center text-body text-ink-2">{error}</p>
      ) : !detail || !user ? (
        <Loading rows={6} label="正在加载用户详情" />
      ) : (
        <div className="pb-6">
          <KV
            rows={[
              ["昵称", user.display_name || "—"],
              ["邮箱", user.email || "—"],
              ["状态", user.status === "active" ? "正常" : "已停用"],
              ["创建时间", shortTime(user["created_at" as keyof UserRow] as string)],
              ["在线登录", `${num(detail.active_logins)} 个未撤销会话`],
              ["最近登录", detail.last_login_at ? shortTime(detail.last_login_at) : "—"],
              ["会话数", `${num((detail.sessions || []).length)}（最近 20 条）`],
            ]}
          />

          <Sub>活跃度 · 近 14 天 {num(msgTotal)} 条消息 / {num(taskTotal)} 个任务</Sub>
          <BarChart rows={activity} field="messages" height={112} unit=" 条" />

          <Sub>身份绑定（子平台）</Sub>
          {(detail.identities || []).length ? (
            <TableWrap>
              <table className="w-full text-body">
                <thead>
                  <tr>
                    <th className={thCls}>子平台</th>
                    <th className={thCls}>租户</th>
                    <th className={thCls}>角色</th>
                    <th className={thCls}>外部标识</th>
                    <th className={thCls}>最近登录</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.identities || []).map((i, idx) => (
                    <tr key={idx}>
                      <td className={tdCls}>
                        <Tag>{i.identity_provider_id || i.provider || "—"}</Tag>
                      </td>
                      <td className={cn(tdMonoCls, "text-label")}>{i.tenant_id}</td>
                      <td className={tdCls} title={i.role}>
                        {roleLabel(i.role)}
                      </td>
                      <td className={cn(tdMonoCls, "text-label")}>
                        {(i.subject || "").slice(0, 16)}
                      </td>
                      <td className={cn(tdCls, "text-secondary")}>{when(i.last_seen_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          ) : (
            <p className="text-body text-muted">没有绑定身份</p>
          )}

          <Sub>最近任务</Sub>
          {(detail.tasks || []).length ? (
            <TableWrap>
              <table className="w-full text-body">
                <thead>
                  <tr>
                    <th className={thCls}>任务</th>
                    <th className={thCls}>状态</th>
                    <th className={thCls}>内容</th>
                    <th className={thCls}>时间</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.tasks || []).map((t) => (
                    <tr key={t.task_id}>
                      <td className={cn(tdMonoCls, "text-label")}>{(t.task_id || "").slice(0, 14)}</td>
                      <td className={tdCls}>{t.status}</td>
                      <td className={cn(tdCls, "text-secondary text-muted")}>
                        {(t.message || "").slice(0, 40)}
                      </td>
                      <td className={cn(tdCls, "text-secondary")}>{when(t.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          ) : (
            <p className="text-body text-muted">还没有任务</p>
          )}

          <Sub>管理动作（针对此人）</Sub>
          {(detail.audit || []).length ? (
            <TableWrap>
              <table className="w-full text-body">
                <thead>
                  <tr>
                    <th className={thCls}>时间</th>
                    <th className={thCls}>动作</th>
                    <th className={thCls}>操作人</th>
                    <th className={thCls}>内容</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.audit || []).map((e, idx) => (
                    <tr key={idx}>
                      <td className={cn(tdCls, "text-secondary text-muted")}>{when(e.created_at)}</td>
                      <td className={tdCls}>{auditLabel[e.type || ""] || e.type || "未知"}</td>
                      <td className={cn(tdMonoCls, "text-label")}>{e.operator_id || "—"}</td>
                      <td className={cn(tdCls, "text-secondary text-muted")}>{auditDetail(e)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          ) : (
            <p className="text-body text-muted">还没有针对此人的管理动作</p>
          )}

          {scope === "all" && localName ? (
            <>
              <Sub>本地登录</Sub>
              <div className="flex flex-wrap items-center gap-2">
                <Button onClick={() => onReset(userId)}>重置密码</Button>
                <Button
                  onClick={async () => {
                    try {
                      await consoleApi.unlock(userId);
                      toast("已解锁");
                    } catch (err) {
                      toast(errorMessage(err), "error");
                    }
                  }}
                >
                  解锁
                </Button>
                <span className="text-label text-muted">
                  登录账号：<Code>{localName}</Code>
                </span>
              </div>
            </>
          ) : null}

          {scope === "all" ? (
            <>
              <Sub>危险操作</Sub>
              <div className="flex items-start gap-4 rounded-lg border border-danger/35 bg-danger-wash/50 p-3.5">
                <div className="min-w-0 flex-1">
                  <div className="text-body font-medium text-ink">删除账号（清空全部数据）</div>
                  <p className="mt-1 text-secondary leading-relaxed text-muted">
                    删除会清空该用户的全部会话、消息、任务、运行记录、产物、凭据与账号本身，
                    不可恢复。任何身份源、任何状态的用户都可以直接删除。
                  </p>
                  {detail.purge_preview ? (
                    <p className="mt-1.5 text-label text-muted">
                      预计清除：会话 {num(detail.purge_preview.sessions)} ·
                      消息 {num(detail.purge_preview.messages)} ·
                      任务 {num(detail.purge_preview.tasks)} ·
                      运行 {num(detail.purge_preview.runs)} ·
                      产物 {num(detail.purge_preview.artifacts)} ·
                      登录 {num(detail.purge_preview.logins)}（共{" "}
                      {num(detail.purge_preview.total)} 条记录）
                    </p>
                  ) : null}
                </div>
                <Button variant="danger" onClick={() => void remove()}>
                  删除账号
                </Button>
              </div>
            </>
          ) : null}
        </div>
      )}
    </Drawer>
  );
}

/* ---------- 创建用户 ---------- */

function CreateUserDrawer({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [display, setDisplay] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [role, setRole] = useState("member");
  const [mustChange, setMustChange] = useState("0");
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!username.trim() || !password) {
      setHint("账号和密码不能为空");
      return;
    }
    if (password !== confirm) {
      setHint("两次密码不一致");
      return;
    }
    setBusy(true);
    setHint("创建中…");
    try {
      await consoleApi.createUser({
        username: username.trim(),
        password,
        display_name: display.trim(),
        role,
        must_change_password: mustChange === "1",
      });
      toast("用户已创建");
      onCreated();
      onClose();
    } catch (err) {
      setHint(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer
      open
      onClose={onClose}
      title="创建用户"
      header={<DrawerHead title="创建用户" hint="账号密码由你设定" onClose={onClose} />}
    >
      <Grid>
        <Field
          label="登录账号"
          htmlFor="cu-name"
          note="身份源固定为 local，与其他平台同名账号不会合并"
        >
          <input
            id="cu-name"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="3-32 位小写字母/数字/._-"
            autoComplete="off"
            className={inputCls}
          />
        </Field>
        <Field label="显示名" htmlFor="cu-display" note="留空则与账号相同">
          <input
            id="cu-display"
            value={display}
            onChange={(e) => setDisplay(e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="密码" htmlFor="cu-pass">
          <input
            id="cu-pass"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            className={inputCls}
          />
        </Field>
        <Field label="确认密码" htmlFor="cu-pass2">
          <input
            id="cu-pass2"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            className={inputCls}
          />
        </Field>
        <Field label="角色" htmlFor="cu-role">
          <select
            id="cu-role"
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className={cn(selectCls, "w-full")}
          >
            <option value="member">普通用户（member）</option>
            <option value="platform_admin">超级管理员（platform_admin）</option>
          </select>
        </Field>
        <Field label="首次登录" htmlFor="cu-must">
          <select
            id="cu-must"
            value={mustChange}
            onChange={(e) => setMustChange(e.target.value)}
            className={cn(selectCls, "w-full")}
          >
            <option value="0">不强制改密</option>
            <option value="1">强制修改密码</option>
          </select>
        </Field>
      </Grid>

      <div className="mt-4 flex items-center gap-3">
        <Button variant="primary" onClick={() => void submit()} disabled={busy}>
          创建
        </Button>
        {hint ? <span className="text-secondary text-danger">{hint}</span> : null}
      </div>
    </Drawer>
  );
}

/* ---------- 重置密码 ---------- */

function ResetPasswordDrawer({
  user,
  userId,
  localName,
  onClose,
  onDone,
}: {
  user: UserRow | null;
  userId: string;
  localName?: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const existing = localName || "";
  const suggested = suggestLoginName(user, userId);
  const [name, setName] = useState(existing || suggested);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!password) {
      setHint("请输入新密码");
      return;
    }
    if (password !== confirm) {
      setHint("两次密码不一致");
      return;
    }
    setBusy(true);
    setHint("提交中…");
    try {
      const result = await consoleApi.setPassword(userId, password, existing ? "" : name.trim());
      toast(`密码已设置，登录账号：${result.username || existing || name}`);
      onDone();
      onClose();
    } catch (err) {
      setHint(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const who = user?.display_name ? `${user.display_name}（${userId}）` : userId;

  return (
    <Drawer
      open
      onClose={onClose}
      title="重置密码"
      header={<DrawerHead title="重置密码" hint={who} onClose={onClose} />}
    >
      <p className="mb-3 text-body leading-relaxed text-ink-2">
        {existing ? (
          "该用户已有本地登录账号。重置后立即生效，并撤销其全部登录会话。"
        ) : (
          <>
            该用户来自外部身份源，中台不保管其上游密码。这里会为
            <b>同一个中台账号</b>新增「本地账号」登录方式：用户在登录页选「本地账号」用下面的
            账号密码进入，角色、租户与历史数据全部保留，不影响原有登录方式。
          </>
        )}
      </p>

      <Grid>
        <Field
          label="登录账号"
          htmlFor="rp-name"
          note={existing ? "已有账号的登录名不可修改" : "3-32 位小写字母 / 数字 / ._-，可改成更好记的名字"}
        >
          <input
            id="rp-name"
            value={name}
            disabled={Boolean(existing)}
            onChange={(e) => setName(e.target.value)}
            autoComplete="off"
            className={inputCls}
          />
        </Field>
        <div />
        <Field label="新密码" htmlFor="rp-pass">
          <input
            id="rp-pass"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            className={inputCls}
          />
        </Field>
        <Field label="确认密码" htmlFor="rp-pass2">
          <input
            id="rp-pass2"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            className={inputCls}
          />
        </Field>
      </Grid>

      <div className="mt-4 flex items-center gap-3">
        <Button variant="primary" onClick={() => void submit()} disabled={busy}>
          确认重置
        </Button>
        {hint ? <span className="text-secondary text-danger">{hint}</span> : null}
      </div>
    </Drawer>
  );
}

/** 从显示名推一个能用的登录名；推不出来就用 user_id。 */
function suggestLoginName(user: UserRow | null, userId: string): string {
  const raw = String(user?.display_name || "").trim().toLowerCase();
  return /^[a-z0-9._-]{3,32}$/.test(raw) ? raw : userId;
}

