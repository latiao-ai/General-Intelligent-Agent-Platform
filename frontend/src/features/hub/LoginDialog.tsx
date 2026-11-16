/**
 * 登录弹窗：平台账号 / 本地账号 / 短信验证码 / SSO 四条路。
 *
 * 为什么四种都留：它们覆盖的是不同部署形态，不是"多此一举"——
 *  - 平台账号：中台嵌在客户产品里，账号本来就在对方系统；
 *  - 本地账号：中台自己发号（`local_auth`，scrypt 校验），也能配成首次强制改密；
 *  - 短信：手机号就是身份，客户不愿意开账号体系时用；
 *  - SSO：一条都没配时的兜底，整页跳转（OAuth 不能在弹窗里开新窗口）。
 *
 * 三个必须照旧的地方：
 *  1. **不要用 `apiRequest`**：它把 401 当"会话失效"回调出去，而这里 401 的含义
 *     是"账号或密码错"。登录接口单独走裸 fetch；
 *  2. 本地账号 403 + `code=PASSWORD_CHANGE_REQUIRED` 要切到改密表单，而不是报错；
 *  3. 成功后写一次 `wb.login.flash` 再 reload——reload 之后页面已经拿不到这次的
 *     响应了，闪烁提示只能靠 sessionStorage 传。
 *
 * 与旧页有意不同：旧页登录成功后把 `platform_admin/operator` 直接送到 `/admin`。
 * 这里不跳——hub 与 admin 已经是两个独立应用，在 hub 点登录的人是想继续聊。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { CaptchaSlider } from "./CaptchaSlider";

type Mode = "platform" | "local" | "change" | "sms";

const FLASH_KEY = "wb.login.flash";

interface PlatformChoice {
  platform_id: string;
  display_name?: string;
}

interface Statuses {
  platform: { enabled: boolean; platforms: PlatformChoice[]; default: string };
  local: { enabled: boolean; allowSignup: boolean; minLength: number };
  sms: { enabled: boolean };
}

const EMPTY_STATUS: Statuses = {
  platform: { enabled: false, platforms: [], default: "" },
  local: { enabled: false, allowSignup: false, minLength: 8 },
  sms: { enabled: false },
};

async function readJson(res: Response): Promise<Record<string, unknown>> {
  try {
    return (await res.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

async function post(
  path: string,
  body?: unknown,
): Promise<{ ok: boolean; status: number; data: Record<string, unknown> }> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  return { ok: res.ok, status: res.status, data: await readJson(res) };
}

function detailOf(data: Record<string, unknown>, fallback: string): string {
  const raw = data.detail;
  return typeof raw === "string" && raw ? raw : fallback;
}

/** 登录成功：写一次性 flash 再整页重载。 */
function finishLogin(data: Record<string, unknown>) {
  const flash = data.first_login
    ? "首次登录：已自动创建中台账号并完成绑定"
    : "登录成功";
  try {
    sessionStorage.setItem(FLASH_KEY, flash);
  } catch {
    /* 隐私模式禁用 storage，登录本身不受影响 */
  }
  window.location.reload();
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="flex items-baseline gap-1.5 text-label text-muted">
        {label}
        {hint ? <span className="text-muted/80">{hint}</span> : null}
      </span>
      {children}
    </label>
  );
}

const inputClass =
  "h-[30px] w-full rounded-md border border-line bg-paper px-2 text-body text-ink outline-none transition-colors duration-150 focus:border-signal disabled:bg-sunken disabled:text-muted";

export function LoginDialog({
  open,
  onClose,
  authOff,
}: {
  open: boolean;
  onClose: () => void;
  /** AUTH_MODE=off：没有登录这回事，别把人送去一页 JSON */
  authOff?: boolean;
}) {
  const [statuses, setStatuses] = useState<Statuses>(EMPTY_STATUS);
  const [loaded, setLoaded] = useState(false);
  const [mode, setMode] = useState<Mode>("platform");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 成功类提示（改密成功、验证码已发送），与 error 互斥显示 */
  const [flashNotice, setFlashNotice] = useState<string | null>(null);

  /* 平台账号 */
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [platform, setPlatform] = useState("");

  /* 本地账号 */
  const [localUser, setLocalUser] = useState("");
  const [localPass, setLocalPass] = useState("");
  const [newPass, setNewPass] = useState("");

  /* 短信 */
  const [mobile, setMobile] = useState("");
  const [code, setCode] = useState("");
  const [smsStage, setSmsStage] = useState<"mobile" | "captcha" | "code">("mobile");
  const [captchaSeq, setCaptchaSeq] = useState(0);
  const [ticket, setTicket] = useState("");
  const [countdown, setCountdown] = useState(0);
  const timerRef = useRef<number | null>(null);

  const available = useMemo(
    () => ({
      platform: statuses.platform.enabled,
      local: statuses.local.enabled,
      sms: statuses.sms.enabled,
    }),
    [statuses],
  );
  const anyPasswordMode = available.platform || available.local || available.sms;

  /* 打开时问一次各方式的开关。未登录也能问，接口不回任何账号信息。 */
  useEffect(() => {
    if (!open || loaded) return;
    void (async () => {
      const [p, l, s] = await Promise.allSettled([
        fetch("/api/auth/platform-login/status", { credentials: "same-origin" }),
        fetch("/api/auth/local-login/status", { credentials: "same-origin" }),
        fetch("/api/auth/sms-login/status", { credentials: "same-origin" }),
      ]);
      const next: Statuses = {
        platform: { enabled: false, platforms: [], default: "" },
        local: { enabled: false, allowSignup: false, minLength: 8 },
        sms: { enabled: false },
      };
      if (p.status === "fulfilled" && p.value.ok) {
        const data = await readJson(p.value);
        next.platform = {
          enabled: !!data.enabled,
          platforms: Array.isArray(data.platforms) ? (data.platforms as PlatformChoice[]) : [],
          default: String(data.default ?? ""),
        };
      }
      if (l.status === "fulfilled" && l.value.ok) {
        const data = await readJson(l.value);
        next.local = {
          enabled: !!data.enabled,
          allowSignup: !!data.allow_signup,
          minLength: Number(data.min_length ?? 8) || 8,
        };
      }
      if (s.status === "fulfilled" && s.value.ok) {
        const data = await readJson(s.value);
        next.sms = { enabled: !!data.enabled };
      }
      setStatuses(next);
      setPlatform(next.platform.default || next.platform.platforms[0]?.platform_id || "");
      setMode(
        next.platform.enabled
          ? "platform"
          : next.local.enabled
            ? "local"
            : next.sms.enabled
              ? "sms"
              : "platform",
      );
      setLoaded(true);
    })();
  }, [open, loaded]);

  /* 换 tab 清掉上一个表单的报错，否则"密码错"会挂在短信表单底下 */
  useEffect(() => {
    setError(null);
  }, [mode]);

  /* 倒计时清理 + 卸载 */
  useEffect(() => {
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current);
    };
  }, []);

  const startCountdown = useCallback((seconds: number) => {
    if (timerRef.current) window.clearInterval(timerRef.current);
    let left = Math.max(1, seconds);
    setCountdown(left);
    timerRef.current = window.setInterval(() => {
      left -= 1;
      if (left <= 0) {
        if (timerRef.current) window.clearInterval(timerRef.current);
        timerRef.current = null;
        setCountdown(0);
      } else {
        setCountdown(left);
      }
    }, 1000);
  }, []);

  /* ---------------- 提交 ---------------- */

  const submitPlatform = async () => {
    if (!username.trim() || !password) {
      setError("请输入账号和密码");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await post("/api/auth/platform-login", {
        username: username.trim(),
        password,
        platform,
      });
      if (!res.ok) throw new Error(detailOf(res.data, `登录失败（HTTP ${res.status}）`));
      finishLogin(res.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const submitLocal = async () => {
    if (!localUser.trim() || !localPass) {
      setError("请输入账号和密码");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await post("/api/auth/local-login", {
        username: localUser.trim(),
        password: localPass,
      });
      if (!res.ok) {
        // 服务端要求先改密：切表单，把已输入的账号与旧密码带过去
        if (res.status === 403 && res.data.code === "PASSWORD_CHANGE_REQUIRED") {
          setMode("change");
          return;
        }
        throw new Error(detailOf(res.data, `登录失败（HTTP ${res.status}）`));
      }
      finishLogin(res.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const submitChange = async () => {
    if (!localUser.trim() || !localPass || !newPass) {
      setError("请填完整");
      return;
    }
    if (newPass.length < statuses.local.minLength) {
      setError(`新密码至少 ${statuses.local.minLength} 位`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await post("/api/auth/local-login/change-password", {
        username: localUser.trim(),
        old_password: localPass,
        new_password: newPass,
      });
      if (!res.ok) throw new Error(detailOf(res.data, `修改失败（HTTP ${res.status}）`));
      // 改完直接把新密码填回去，少一次手打
      setLocalPass(newPass);
      setNewPass("");
      setMode("local");
      setError(null);
      setFlashNotice("密码已修改，请重新登录");
    } catch (err) {
      setError(err instanceof Error ? err.message : "修改失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const sendCode = async (captchaTicket: string) => {
    setBusy(true);
    setError(null);
    try {
      const res = await post("/api/auth/sms-login/send-code", {
        mobile: mobile.trim(),
        captchaVerification: captchaTicket,
      });
      if (!res.ok) throw new Error(detailOf(res.data, "发送失败"));
      setSmsStage("code");
      startCountdown(Number(res.data.resendIntervalSeconds ?? 60) || 60);
      setFlashNotice("验证码已发送");
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送失败");
      // 票据通常已被消费或失效，退回滑块重来
      setTicket("");
      setSmsStage("captcha");
      setCaptchaSeq((n) => n + 1);
    } finally {
      setBusy(false);
    }
  };

  const requestCode = () => {
    if (!/^1[3-9]\d{9}$/.test(mobile.trim())) {
      setError("请输入正确的 11 位手机号");
      return;
    }
    setError(null);
    setFlashNotice(null);
    // 已经发过码：票据是一次性的，重发必须重新过滑块
    if (smsStage === "code") {
      setTicket("");
      setCaptchaSeq((n) => n + 1);
      setSmsStage("captcha");
      return;
    }
    if (ticket) {
      void sendCode(ticket);
      return;
    }
    setCaptchaSeq((n) => n + 1);
    setSmsStage("captcha");
  };

  const submitSms = async () => {
    if (!/^1[3-9]\d{9}$/.test(mobile.trim())) {
      setError("请输入正确的 11 位手机号");
      return;
    }
    if (!/^\d{6}$/.test(code.trim())) {
      setError("请输入 6 位验证码");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await post("/api/auth/sms-login", {
        mobile: mobile.trim(),
        code: code.trim(),
      });
      if (!res.ok) throw new Error(detailOf(res.data, "登录失败"));
      finishLogin(res.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const tabs: { key: Mode; label: string; show: boolean }[] = [
    { key: "platform", label: "账号登录", show: available.platform },
    { key: "local", label: "本地账号", show: available.local },
    { key: "sms", label: "短信验证码", show: available.sms },
  ];
  const visibleTabs = tabs.filter((item) => item.show);

  return (
    <Dialog open={open} onClose={onClose} title="登录中台" className="max-w-[420px]">
      {authOff ? (
        <div className="flex flex-col gap-2">
          <p className="text-body leading-relaxed text-ink">
            当前是本地演示模式（<code className="rounded-[3px] bg-sunken px-1 text-label">AUTH_MODE=off</code>），
            不需要登录，直接发消息即可。
          </p>
          <p className="text-label leading-relaxed text-muted">
            如果需要多账号与角色权限，把 <code className="rounded-[3px] bg-sunken px-1">AUTH_MODE</code>
            改成 <code className="rounded-[3px] bg-sunken px-1">ticket</code> 并配置一种登录方式后重启服务。
          </p>
          <div className="flex justify-end pt-1">
            <Button variant="secondary" size="sm" onClick={onClose}>
              知道了
            </Button>
          </div>
        </div>
      ) : !loaded ? (
        <p className="py-6 text-center text-label text-muted">正在检查登录方式…</p>
      ) : !anyPasswordMode ? (
        <div className="flex flex-col gap-3">
          <p className="text-secondary leading-relaxed text-ink-2">
            当前部署只开放了统一身份登录（SSO）。点下面的按钮跳转认证，完成后会回到这里。
          </p>
          <Button
            variant="primary"
            size="md"
            onClick={() => window.location.assign("/api/auth/oidc/login")}
          >
            前往 SSO 登录
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {visibleTabs.length > 1 ? (
            <div className="flex items-center gap-1 border-b border-line pb-2">
              {visibleTabs.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  role="tab"
                  aria-selected={mode === item.key || (item.key === "local" && mode === "change")}
                  onClick={() => setMode(item.key)}
                  className={cn(
                    "h-[26px] rounded-md px-2.5 text-label transition-colors duration-150",
                    mode === item.key || (item.key === "local" && mode === "change")
                      ? "bg-sunken text-ink"
                      : "text-muted hover:text-ink",
                  )}
                >
                  {item.label}
                </button>
              ))}
            </div>
          ) : null}

          {mode === "platform" ? (
            <form
              className="flex flex-col gap-2.5"
              onSubmit={(event) => {
                event.preventDefault();
                void submitPlatform();
              }}
            >
              {statuses.platform.platforms.length > 1 ? (
                <Field label="登录平台">
                  <select
                    value={platform}
                    onChange={(event) => setPlatform(event.target.value)}
                    className={inputClass}
                  >
                    {statuses.platform.platforms.map((item) => (
                      <option key={item.platform_id} value={item.platform_id}>
                        {item.display_name || item.platform_id}
                      </option>
                    ))}
                  </select>
                </Field>
              ) : null}
              <Field label="账号">
                <input
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  autoComplete="username"
                  placeholder="用户名"
                  className={inputClass}
                />
              </Field>
              <Field label="密码">
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete="current-password"
                  placeholder="密码"
                  className={inputClass}
                />
              </Field>
              <Button type="submit" variant="primary" size="md" disabled={busy}>
                {busy ? "登录中…" : "登录"}
              </Button>
            </form>
          ) : null}

          {mode === "local" ? (
            <form
              className="flex flex-col gap-2.5"
              onSubmit={(event) => {
                event.preventDefault();
                void submitLocal();
              }}
            >
              <Field label="账号">
                <input
                  value={localUser}
                  onChange={(event) => setLocalUser(event.target.value)}
                  autoComplete="username"
                  placeholder="本地账号"
                  className={inputClass}
                />
              </Field>
              <Field label="密码">
                <input
                  type="password"
                  value={localPass}
                  onChange={(event) => setLocalPass(event.target.value)}
                  autoComplete="current-password"
                  placeholder="密码"
                  className={inputClass}
                />
              </Field>
              <Button type="submit" variant="primary" size="md" disabled={busy}>
                {busy ? "登录中…" : "登录"}
              </Button>
              {statuses.local.allowSignup ? (
                <p className="text-label text-muted">没有账号？这个部署开放了自助注册，请联系管理员开通。</p>
              ) : null}
            </form>
          ) : null}

          {mode === "change" ? (
            <form
              className="flex flex-col gap-2.5"
              onSubmit={(event) => {
                event.preventDefault();
                void submitChange();
              }}
            >
              <p className="text-label leading-relaxed text-ink-2">
                这个账号是首次登录，需要先设置新密码，然后重新登录一次。
              </p>
              <Field label="账号">
                <input
                  value={localUser}
                  onChange={(event) => setLocalUser(event.target.value)}
                  autoComplete="username"
                  className={inputClass}
                />
              </Field>
              <Field label="原密码">
                <input
                  type="password"
                  value={localPass}
                  onChange={(event) => setLocalPass(event.target.value)}
                  autoComplete="current-password"
                  className={inputClass}
                />
              </Field>
              <Field label="新密码" hint={`至少 ${statuses.local.minLength} 位`}>
                <input
                  type="password"
                  value={newPass}
                  onChange={(event) => setNewPass(event.target.value)}
                  autoComplete="new-password"
                  className={inputClass}
                />
              </Field>
              <Button type="submit" variant="primary" size="md" disabled={busy}>
                {busy ? "提交中…" : "修改密码"}
              </Button>
            </form>
          ) : null}

          {mode === "sms" ? (
            <div className="flex flex-col gap-2.5">
              <Field label="手机号">
                <input
                  value={mobile}
                  onChange={(event) => setMobile(event.target.value.replace(/\D/g, "").slice(0, 11))}
                  inputMode="numeric"
                  autoComplete="tel"
                  placeholder="请输入 11 位手机号"
                  disabled={smsStage === "code"}
                  className={inputClass}
                />
              </Field>

              {smsStage === "captcha" ? (
                <CaptchaSlider
                  key={captchaSeq}
                  mobile={mobile.trim()}
                  onVerified={(value) => {
                    setTicket(value);
                    void sendCode(value);
                  }}
                  onError={(message) => setError(message)}
                />
              ) : null}

              {smsStage === "code" ? (
                <form
                  className="flex flex-col gap-2.5"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void submitSms();
                  }}
                >
                  <Field label="验证码">
                    <input
                      value={code}
                      onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      placeholder="6 位短信验证码"
                      className={inputClass}
                    />
                  </Field>
                  <Button type="submit" variant="primary" size="md" disabled={busy}>
                    {busy ? "登录中…" : "登录"}
                  </Button>
                </form>
              ) : null}

              <Button
                variant="secondary"
                size="sm"
                disabled={busy || countdown > 0}
                onClick={requestCode}
              >
                {countdown > 0 ? `重新发送(${countdown}s)` : smsStage === "mobile" ? "获取验证码" : "重新获取"}
              </Button>
            </div>
          ) : null}

          {error ? (
            <p role="alert" className="text-label leading-relaxed text-danger">
              {error}
            </p>
          ) : flashNotice ? (
            <p role="status" className="text-label text-ok">
              {flashNotice}
            </p>
          ) : null}

          <div className="hairline-t flex flex-wrap items-center gap-x-3 gap-y-1 pt-2 text-label text-muted">
            <span>其它方式</span>
            <a href="/api/auth/oidc/login" className="text-signal hover:underline">
              SSO 登录
            </a>
            {available.sms && mode !== "sms" ? (
              <button type="button" onClick={() => setMode("sms")} className="hover:text-ink">
                短信验证码
              </button>
            ) : null}
            {available.local && mode !== "local" && mode !== "change" ? (
              <button type="button" onClick={() => setMode("local")} className="hover:text-ink">
                本地账号
              </button>
            ) : null}
            {available.platform && mode !== "platform" ? (
              <button type="button" onClick={() => setMode("platform")} className="hover:text-ink">
                平台账号
              </button>
            ) : null}
          </div>
        </div>
      )}
    </Dialog>
  );
}

export { FLASH_KEY as LOGIN_FLASH_KEY };
