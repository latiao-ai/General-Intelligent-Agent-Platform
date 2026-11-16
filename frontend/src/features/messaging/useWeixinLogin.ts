/**
 * 个人微信扫码登录。
 *
 * 职责边界要分清：**真正轮询 iLink 的是后端**（`weixin_login.py` 里的
 * `LoginManager._poll`）。前端只是每 1.8 秒问一次「扫到哪一步了」，拿到
 * 二维码图片和状态文字。所以关掉页面不会中断登录，重新进来还能接上。
 *
 * 三个终态必须停表：confirmed / expired / error。不停的话会一直打接口，
 * 而且二维码过期后每 1.8 秒闪一次同样的报错。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { weixinLoginCancel, weixinLoginLogout, weixinLoginStart, weixinLoginStatus } from "./api";
import type { WeixinLoginView } from "./types";

/** 已扫码后 iLink 还要等用户点确认，2 秒一问足够，也够省 */
const POLL_MS = 1800;

const STOP_STATUS = new Set(["confirmed", "expired", "error", "cancelled", "idle"]);

export function useWeixinLogin(active: boolean, onConfirmed: () => void | Promise<void>) {
  const toast = useToast();
  const [view, setView] = useState<WeixinLoginView | null>(null);
  const [polling, setPolling] = useState(false);
  const [busy, setBusy] = useState(false);

  const timer = useRef<number | null>(null);
  const confirmedRef = useRef(onConfirmed);
  const alive = useRef(true);

  useEffect(() => {
    confirmedRef.current = onConfirmed;
  }, [onConfirmed]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const stop = useCallback(() => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
    setPolling(false);
  }, []);

  const poll = useCallback(async () => {
    if (document.hidden) return;
    try {
      const next = await weixinLoginStatus();
      if (!alive.current) return;
      setView(next);
      if (next.status === "confirmed") {
        stop();
        toast("个人微信登录成功");
        await confirmedRef.current();
        return;
      }
      if (STOP_STATUS.has(next.status)) {
        stop();
        if (next.status === "expired") toast("二维码过期，请重新发起登录", "error");
      }
    } catch {
      // 轮询失败安静重试：网络抖一下不该把已经显示出来的二维码擦掉
    }
  }, [stop, toast]);

  const start = useCallback(async () => {
    setBusy(true);
    try {
      const next = await weixinLoginStart();
      if (!alive.current) return;
      setView(next);
      if (next.status === "error") {
        toast(next.message || "获取二维码失败", "error");
        return;
      }
      toast("二维码已生成，请用微信扫码");
      stop();
      setPolling(true);
      timer.current = window.setInterval(() => void poll(), POLL_MS);
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      if (alive.current) setBusy(false);
    }
  }, [poll, stop, toast]);

  const cancel = useCallback(async () => {
    try {
      await weixinLoginCancel();
    } catch {
      // 后端进程里本来就没有会话时也会报错，忽略即可，本地状态照样收掉
    }
    stop();
    setView({ status: "idle", message: "已取消", qr_svg: "", account_id: "", expires_in: 0 });
  }, [stop]);

  const logout = useCallback(async () => {
    setBusy(true);
    try {
      await weixinLoginLogout();
      stop();
      setView(null);
      toast("已退出登录");
      await confirmedRef.current();
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      if (alive.current) setBusy(false);
    }
  }, [stop, toast]);

  /**
   * 进来先问一次现有状态：上次扫码可能已经成功了，只是页面刷新过。
   * 切到别的渠道时把正在扫的那张码收掉，别在后台继续打接口；
   * 再切回来要重新播种，否则显示的是空的。
   */
  const seeded = useRef(false);
  useEffect(() => {
    if (!active) {
      stop();
      setView(null);
      seeded.current = false;
      return;
    }
    if (seeded.current) return;
    seeded.current = true;
    weixinLoginStatus()
      .then((next) => {
        if (alive.current) setView(next);
      })
      .catch(() => {});
  }, [active, stop]);

  useEffect(() => () => stop(), [stop]);

  return { view, polling, busy, start, cancel, logout };
}
