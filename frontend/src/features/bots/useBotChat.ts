/**
 * 一个机器人的规范会话。
 *
 * 三条约定来自旧页，不能改：
 *  1. **不缓存 session_id**：每次打开都问服务端要（幂等），换浏览器不会丢对话。
 *  2. **SSE 只管"看得见的流"**：流结束必须重新拉一次 `/chat`，历史真相在库里。
 *  3. **乐观上屏**：自己的消息先画出来，请求失败再回滚并把原文放回输入框。
 *
 * 另外加了一条旧页没有的：切换机器人时，旧请求回来的结果直接丢弃。
 * 旧页靠 `if (S.activeBotId !== botId) return` 守，React 里同样需要一个 ref。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "@/lib/api";
import { getBotChat, sendBotMessage, turnEventsUrl } from "./api";
import type { BotChatPayload, ChatMessage, Turn } from "./types";

export type ChatPhase = "idle" | "loading" | "ready" | "error";

const ACTIVE_TURN_STATES = ["accepted", "running"];

function randomRequestId() {
  return "req_" + Math.random().toString(36).slice(2, 12);
}

export function useBotChat() {
  const [botId, setBotId] = useState("");
  const [detail, setDetail] = useState<BotChatPayload | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [epoch, setEpoch] = useState("");
  const [phase, setPhase] = useState<ChatPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /** SSE 增量。只在本轮正在跑时非空，结束后清掉。 */
  const [draft, setDraft] = useState("");

  const activeId = useRef("");
  const busyRef = useRef(false);
  const source = useRef<EventSource | null>(null);
  const draftRef = useRef("");

  const closeStream = useCallback(() => {
    if (source.current) {
      try {
        source.current.close();
      } catch {
        /* 已经关了 */
      }
      source.current = null;
    }
  }, []);

  useEffect(
    () => () => {
      closeStream();
      activeId.current = "";
    },
    [closeStream],
  );

  const apply = useCallback((data: BotChatPayload) => {
    setDetail(data);
    setMessages(data.messages ?? []);
    setTurns(data.turns ?? []);
    setEpoch((prev) => data.epoch ?? prev);
  }, []);

  /** 只看不说：拉一次会话，用于轮询和流结束后的校准。 */
  const reload = useCallback(
    async (id: string) => {
      const data = await getBotChat(id);
      if (activeId.current !== id) return null;
      apply(data);
      return data;
    },
    [apply],
  );

  const open = useCallback(
    async (id: string) => {
      if (!id) return;
      closeStream();
      activeId.current = id;
      setBotId(id);
      setPhase("loading");
      setError(null);
      setDraft("");
      draftRef.current = "";
      // 清掉上一个机器人的内容，否则新会话加载中会先闪一眼别人的对话
      setDetail(null);
      setMessages([]);
      setTurns([]);

      try {
        const data = await getBotChat(id);
        if (activeId.current !== id) return;
        apply(data);
        setPhase("ready");
      } catch (err) {
        if (activeId.current !== id) return;
        setPhase("error");
        setError(errorMessage(err));
      }
    },
    [apply, closeStream],
  );

  const reset = useCallback(() => {
    closeStream();
    activeId.current = "";
    setBotId("");
    setDetail(null);
    setMessages([]);
    setTurns([]);
    setEpoch("");
    setDraft("");
    draftRef.current = "";
    setPhase("idle");
    setError(null);
  }, [closeStream]);

  /**
   * 跟一轮 SSE 到结束。返回的 Promise 永远 resolve——界面不该因为
   * 流断了就一直转圈，30s / 180s 两道兜底之后按"已结束"处理。
   */
  const streamTurn = useCallback(
    (sessionId: string, turnId: string, id: string) =>
      new Promise<void>((resolve) => {
        if (!turnId) {
          resolve();
          return;
        }
        closeStream();
        let finished = false;
        let streamTimer = 0;
        let hardTimer = 0;

        const finish = async () => {
          if (finished) return;
          finished = true;
          window.clearTimeout(streamTimer);
          window.clearTimeout(hardTimer);
          closeStream();
          try {
            await reload(id);
          } catch {
            /* 校准失败就保留屏幕上的内容 */
          }
          setDraft("");
          draftRef.current = "";
          resolve();
        };

        const es = new EventSource(turnEventsUrl(sessionId, turnId));
        source.current = es;

        es.addEventListener("turn", (event) => {
          let payload: { type?: string; text?: string } = {};
          try {
            payload = JSON.parse((event as MessageEvent<string>).data);
          } catch {
            return;
          }
          if (payload.type === "answer_delta") {
            draftRef.current += String(payload.text ?? "");
            setDraft(draftRef.current);
          }
        });
        es.addEventListener("done", () => {
          void finish();
        });
        // 浏览器会自动重连，但界面不能无限等；30 秒收尾一次
        es.onerror = () => {
          window.clearTimeout(streamTimer);
          streamTimer = window.setTimeout(() => void finish(), 30_000);
        };
        hardTimer = window.setTimeout(() => void finish(), 180_000);
      }),
    [closeStream, reload],
  );

  const send = useCallback(
    async (text: string) => {
      const id = activeId.current;
      const body = text.trim();
      if (!id || !body || busyRef.current) return false;

      busyRef.current = true;
      setBusy(true);
      setDraft("");
      draftRef.current = "";

      // 乐观上屏
      setMessages((prev) => [...prev, { role: "user", content: body }]);

      try {
        const res = await sendBotMessage(id, body, randomRequestId());
        if (activeId.current !== id) return true;
        if (res.session_id && res.turn_id) {
          await streamTurn(res.session_id, res.turn_id, id);
        } else {
          await reload(id);
        }
        return true;
      } catch (err) {
        if (activeId.current === id) {
          // 回滚：把自己刚画的那条撤掉，原文交回调用方放回输入框
          setMessages((prev) => prev.slice(0, -1));
        }
        setError(errorMessage(err));
        return false;
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [reload, streamTurn],
  );

  /* 轮询：5 秒一次，只在没有本地请求在飞的时候。队友投递和 Worker 推进都会走这里。 */
  useEffect(() => {
    if (phase !== "ready" || !botId) return;
    const timer = window.setInterval(async () => {
      if (busyRef.current || activeId.current !== botId) return;
      try {
        const data = await getBotChat(botId);
        if (activeId.current !== botId || busyRef.current) return;
        const nextMessages = data.messages ?? [];
        const nextTurns = data.turns ?? [];
        // 只在真的有变化时 setState，否则每 5 秒白重渲染一次
        setMessages((prev) =>
          prev.length === nextMessages.length ? prev : nextMessages,
        );
        setTurns((prev) => {
          const a = prev.map((t) => t.status).join("|");
          const b = nextTurns.map((t) => t.status).join("|");
          return a === b ? prev : nextTurns;
        });
        setDetail(data);
        setEpoch((prev) => data.epoch ?? prev);
      } catch {
        /* 轮询失败静默，下一轮再说 */
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [phase, botId]);

  const streaming = turns.some((t) => ACTIVE_TURN_STATES.includes(String(t.status)));

  return {
    botId,
    detail,
    messages,
    turns,
    epoch,
    phase,
    error,
    busy,
    draft,
    streaming,
    open,
    reset,
    send,
    reload,
    clearError: () => setError(null),
  };
}
