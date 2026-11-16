/**
 * 一个群聊房间。
 *
 * 房间由**服务端**调度：关掉页面 Worker 也会按轮次推进，所以这里的轮询
 * 不是"实时通信"，而是"每 5 秒对一次账"。手动「推进一拍」只是把同一段
 * 服务端逻辑提前触发一次（幂等），不是前端在编排。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "@/lib/api";
import { closeRoom as closeRoomApi, getRoom, sendRoomMessage, stepRoom } from "./api";
import type { RoomDetail } from "./types";

export type RoomPhase = "idle" | "loading" | "ready" | "error";

export function useRoom() {
  const [roomId, setRoomId] = useState("");
  const [detail, setDetail] = useState<RoomDetail | null>(null);
  const [phase, setPhase] = useState<RoomPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const activeId = useRef("");

  const load = useCallback(async (id: string, silent = false) => {
    if (!silent) setPhase("loading");
    try {
      const data = await getRoom(id);
      if (activeId.current !== id) return null;
      setDetail(data);
      setPhase("ready");
      setError(null);
      return data;
    } catch (err) {
      if (activeId.current !== id) return null;
      if (!silent) {
        setPhase("error");
        setError(errorMessage(err));
      }
      return null;
    }
  }, []);

  const open = useCallback(
    async (id: string) => {
      if (!id) return;
      activeId.current = id;
      setRoomId(id);
      setDetail(null);
      await load(id);
    },
    [load],
  );

  const reset = useCallback(() => {
    activeId.current = "";
    setRoomId("");
    setDetail(null);
    setPhase("idle");
    setError(null);
  }, []);

  const reload = useCallback(() => {
    if (!activeId.current) return Promise.resolve(null);
    return load(activeId.current, true);
  }, [load]);

  useEffect(() => {
    if (phase !== "ready" || !roomId) return;
    const timer = window.setInterval(() => {
      if (activeId.current === roomId) void load(roomId, true);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [phase, roomId, load]);

  /** 推进一拍。返回值交给调用方决定提示什么，不在 hook 里 toast。 */
  const step = useCallback(async () => {
    const id = activeId.current;
    if (!id) return null;
    setBusy(true);
    try {
      const res = await stepRoom(id);
      await load(id, true);
      return res;
    } catch (err) {
      setError(errorMessage(err));
      throw err;
    } finally {
      setBusy(false);
    }
  }, [load]);

  const close = useCallback(async () => {
    const id = activeId.current;
    if (!id) return;
    setBusy(true);
    try {
      await closeRoomApi(id);
      await load(id, true);
    } catch (err) {
      setError(errorMessage(err));
      throw err;
    } finally {
      setBusy(false);
    }
  }, [load]);

  const say = useCallback(async (text: string) => {
    const id = activeId.current;
    const body = text.trim();
    if (!id || !body) return false;
    setBusy(true);
    try {
      await sendRoomMessage(id, body);
      await load(id, true);
      // 服务端刚落的这条事件偶尔比事务提交晚一拍，1.2 秒后再对一次
      window.setTimeout(() => void load(id, true), 1200);
      return true;
    } catch (err) {
      setError(errorMessage(err));
      throw err;
    } finally {
      setBusy(false);
    }
  }, [load]);

  const status = detail?.room?.status ?? "";
  const open_ = status === "open";

  return {
    roomId,
    detail,
    phase,
    error,
    busy,
    isOpen: open_,
    status,
    open,
    reset,
    reload,
    step,
    close,
    say,
    clearError: () => setError(null),
  };
}
