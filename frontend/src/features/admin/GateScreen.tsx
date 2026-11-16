import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { errorMessage } from "@/lib/api";
import { consoleApi } from "./api";
import { Field, inputCls, Panel, selectCls } from "./primitives";
import { roleLabel } from "./format";
import type { AuthPlatformStatus } from "./types";

/**
 * 登录/权限不足的准入页。
 *
 * 控制台自带登录框这件事是刻意保留的：整站共用一份 agent_session，
 * 从 /admin 跳到首页再登录，往往还带着旧账号的 cookie，人被卡在门外却以为
 * 是权限没配。留在这里换人，换完 location.replace 回当前页签。
 */
export function GateScreen({
  kind,
  message,
  platforms,
  onNeedPlatforms,
  role,
}: {
  kind: "login" | "denied";
  message: string;
  platforms: AuthPlatformStatus | null;
  onNeedPlatforms: () => void;
  role?: string;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [platform, setPlatform] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (kind === "login") onNeedPlatforms();
  }, [kind, onNeedPlatforms]);

  useEffect(() => {
    if (platforms && platforms.platforms.length > 0) {
      setPlatform((current) => current || platforms.platforms[0]?.platform_id || "");
    }
  }, [platforms]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    if (!username.trim() || !password) {
      setError("账号和密码都要填");
      return;
    }
    setBusy(true);
    try {
      const payload = await consoleApi.platformLogin(username.trim(), password, platform);
      // 换上的不是超管：留在原地把原因说清，否则刷新后又被推回来，看起来像"登不进去"
      if (payload.role !== "platform_admin" && payload.role !== "operator") {
        setError(
          `已登录 ${payload.user_id || ""}（角色 ${roleLabel(payload.role)}），` +
            "这个账号进不了控制台，请换超级管理员账号。",
        );
        return;
      }
      location.replace(location.pathname + (location.hash || ""));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const multi = (platforms?.platforms.length ?? 0) > 1;

  return (
    <div className="mx-auto flex min-h-screen max-w-[720px] flex-col justify-center px-6 py-16">
      <Panel
        title={kind === "login" ? "需要登录" : "没有管理权限"}
        hint={kind === "login" ? undefined : role ? `当前角色 ${roleLabel(role)}` : undefined}
      >
        <div className="flex flex-col gap-4 text-body leading-relaxed text-ink-2">
          {kind === "login" ? (
            <>
              <p>控制台依赖中台登录会话，当前这个浏览器上没有有效会话。</p>
              <p>用平台账号在下面登录，登录成功会直接回到控制台。</p>
            </>
          ) : (
            <>
              <p>当前账号的角色不足以进入控制台。</p>
              <p>
                控制台需要「超级管理员」角色（
                <code className="font-mono text-label">platform_admin</code>
                ）。用超级管理员账号在下面登录，登录成功会直接回到这里。
              </p>
            </>
          )}
          {message ? <p className="text-label text-muted">{message}</p> : null}

          {platforms ? (
            <form onSubmit={submit} className="flex flex-col gap-3">
              {multi ? (
                <Field label="登录平台" htmlFor="gate-platform">
                  <select
                    id="gate-platform"
                    value={platform}
                    onChange={(e) => setPlatform(e.target.value)}
                    className={selectCls + " w-full"}
                  >
                    {platforms.platforms.map((p) => (
                      <option key={p.platform_id} value={p.platform_id}>
                        {p.display_name || p.platform_id}
                      </option>
                    ))}
                  </select>
                </Field>
              ) : null}
              <Field label="账号" htmlFor="gate-user">
                <input
                  id="gate-user"
                  value={username}
                  autoComplete="username"
                  onChange={(e) => setUsername(e.target.value)}
                  className={inputCls}
                />
              </Field>
              <Field label="密码" htmlFor="gate-pass">
                <input
                  id="gate-pass"
                  type="password"
                  value={password}
                  autoComplete="current-password"
                  onChange={(e) => setPassword(e.target.value)}
                  className={inputCls}
                />
              </Field>
              {error ? (
                <p role="alert" className="text-secondary text-danger">
                  {error}
                </p>
              ) : null}
              <div className="flex items-center gap-3">
                <Button type="submit" variant="primary" disabled={busy}>
                  {busy ? "登录中…" : "登录"}
                </Button>
                <span className="text-label text-muted">密码交给平台校验，中台不保存</span>
              </div>
            </form>
          ) : (
            <div className="flex items-center gap-3">
              <a href="/?next=%2Fadmin">
                <Button variant="primary">去主界面登录</Button>
              </a>
              <span className="text-label text-muted">登录后会自动回到这里</span>
            </div>
          )}
        </div>
      </Panel>
    </div>
  );
}
