/**
 * 一个会话的"读—发—流"。
 *
 * 三条顺序不能换（旧页踩过）：
 *  1. **先定会话 id 再发**。会话 id 为空时 URL 会变成 `/sessions/null/messages`，
 *     服务端真的会建一条叫 "null" 的会话，答复全落到那条里——界面上就是"发了没反应"。
 *  2. **SSE 只负责"看得见的流"**，真相在库里。流结束（`done`）后一律重新拉 `timeline`
 *     覆盖本地状态，否则切走再切回会出现两个一样的气泡。
 *  3. **切会话只解绑流，不取消后端任务**。任务在服务端继续跑，切回来按 seq 补齐事件。
 *     旧页早期在这里发过 `/cancel`，一换会话就把活儿停了。
 *
 * 另外补了旧页的一个洞：SSE 正常时，`started` 事件到达之前界面是空白的——Worker
 * 排队时用户以为没反应。这里 POST 成功后立刻上"正在思考…"，任何流事件到达就撤掉。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "@/lib/api";
import { hubApi, newRequestId, newSessionId, validSessionId } from "./api";
import { CHAT_TURN_DONE, formatElapsed, toolLabel, toolMeta } from "./format";
import { chipBlocks, connectorIds, fileMarkerLine, type RefChip } from "./mentions";
import type {
  AddonsPatch,
  ArtifactView,
  HubMessage,
  SessionRow,
  TaskTurn,
  Timeline,
  TurnEvent,
} from "./types";

export type Phase = "boot" | "ready" | "error";

/** 聊天 turn 的步骤（工具调用 / 确认 / 连接器警告）。 */
export interface ChatStep {
  id: number;
  icon: string;
  text: string;
  meta: string;
  tone: "normal" | "warn";
  done: boolean;
}

export interface ChatProcess {
  /** 毫秒时间戳，用来算"已处理 Ns" */
  startedAt: number;
  reasoning: string;
  steps: ChatStep[];
  answer: string;
  /** thinking 还在等/想，answering 已经在吐字，done 已收尾 */
  phase: "thinking" | "answering" | "done";
  status: string;
  collapsed: boolean;
  /** 收尾时刻：done 之后耗时不能再涨，否则"已处理 3s"会跟着页面重渲染一直加 */
  endedAt?: number;
}

function newProcess(): ChatProcess {
  return {
    startedAt: Date.now(),
    reasoning: "",
    steps: [],
    answer: "",
    phase: "thinking",
    status: "",
    collapsed: false,
  };
}

/**
 * @param options.onSessionCreated 会话 id 第一次生成时回调一次。侧栏用它把新任务
 *   归入"当前空间"——旧页也是在同一时机调 `window.onNewSession`。不要改成监听
 *   `sessionId` 变化：那样连"打开一条历史会话"也会被当成新建，历史任务会被
 *   悄悄挪进当前空间。
 */
export function useHubChat(
  options: { onSessionCreated?: (sessionId: string) => void } = {},
) {
  const { onSessionCreated } = options;
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState<HubMessage[]>([]);
  const [turns, setTurns] = useState<TaskTurn[]>([]);
  const [live, setLive] = useState<ChatProcess | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [taskArtifact, setTaskArtifact] = useState<{ taskId: string; artifact: ArtifactView } | null>(null);
  const [phase, setPhase] = useState<Phase>("boot");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /** 401：整页显示"去登录"，而不是让每个请求各弹一次错 */
  const [needLogin, setNeedLogin] = useState(false);

  const sidRef = useRef("");
  const epochRef = useRef(0);
  const turnEsRef = useRef<EventSource | null>(null);
  const busyRef = useRef(false);
  const liveRef = useRef<ChatProcess | null>(null);

  const setProcess = useCallback((fn: (prev: ChatProcess) => ChatProcess) => {
    setLive((prev) => {
      const next = fn(prev ?? newProcess());
      liveRef.current = next;
      return next;
    });
  }, []);

  const closeTurnStream = useCallback(() => {
    const es = turnEsRef.current;
    turnEsRef.current = null;
    if (es) {
      try {
        es.close();
      } catch {
        /* 已关 */
      }
    }
  }, []);

  useEffect(() => {
    return () => {
      closeTurnStream();
      epochRef.current += 1;
    };
  }, [closeTurnStream]);

  /* ---------------- 侧栏 ---------------- */

  const loadSessions = useCallback(async () => {
    try {
      const data = await hubApi.sessions();
      setSessions(data.sessions ?? []);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setNeedLogin(true);
    }
  }, []);

  /* ---------------- 打开会话 ---------------- */

  const applyTimeline = useCallback((data: Timeline) => {
    setMessages(data.messages ?? []);
    setTurns(data.turns ?? []);
    // 最近一份还能打开的产物：切回来仍然能看到视频 / 页面
    const withArtifact = [...(data.turns ?? [])].reverse().find((t) => t.artifact?.exists);
    setTaskArtifact(
      withArtifact?.artifact ? { taskId: withArtifact.task_id, artifact: withArtifact.artifact } : null,
    );
  }, []);

  /* ---------------- 轮询兜底（SSE 不可用时） ---------------- */

  const pollChatReply = useCallback(
    async (turnId: string, sid: string) => {
      const started = Date.now();
      const deadline = started + 120_000;
      let sawDraft = false;
      setPending("正在思考…");
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 280));
        if (sidRef.current !== sid) return; // 切走了就安静地等，别动界面
        if (!sawDraft) {
          const waited = Math.round((Date.now() - started) / 1000);
          setPending(waited >= 6 ? `正在思考… 已等待 ${waited}s` : "正在思考…");
        }
        try {
          const data = await hubApi.timeline(sid);
          if (sidRef.current !== sid) return;
          const turn = (data.chat_turns ?? []).find((t) => t.turn_id === turnId);
          if (turn?.draft) {
            sawDraft = true;
            setPending(null);
            setProcess((prev) => ({ ...prev, answer: turn.draft ?? "", phase: "answering" }));
          }
          if (turn && CHAT_TURN_DONE.includes(turn.status)) {
            setPending(null);
            setLive(null);
            liveRef.current = null;
            applyTimeline(data);
            void loadSessions();
            return;
          }
        } catch {
          /* 网络抖动，下一轮继续 */
        }
      }
      // 超时兜底：至少不丢已经落库的答复，并把"没等到"说清楚
      void loadSessions();
      if (sidRef.current === sid) {
        setPending(null);
        try {
          const data = await hubApi.timeline(sid);
          if (sidRef.current === sid) applyTimeline(data);
        } catch {
          /* 忽略 */
        }
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: "等了 2 分钟没有收到回复。任务可能仍在后台执行，稍后重新打开这个会话可以看到结果。",
          },
        ]);
      }
    },
    [applyTimeline, loadSessions, setProcess],
  );

  /* ---------------- 聊天 turn 的 SSE ---------------- */

  const streamTurn = useCallback(
    (turnId: string, sid: string, options: { resume?: boolean } = {}) => {
      closeTurnStream();
      const resumed = !!options.resume;
      const seed = newProcess();
      setLive(seed);
      liveRef.current = seed;
      setPending(null);

      const es = new EventSource(hubApi.turnEventsUrl(sid, turnId));
      turnEsRef.current = es;
      let fellBack = false;

      es.addEventListener("turn", (raw) => {
        let data: TurnEvent = {};
        try {
          data = JSON.parse((raw as MessageEvent<string>).data || "{}");
        } catch {
          return;
        }
        if (sidRef.current !== sid) return;
        setPending(null);
        switch (data.type) {
          case "started":
            setProcess((prev) => ({
              ...prev,
              startedAt: Number(data.at) > 0 ? Number(data.at) * 1000 : prev.startedAt,
            }));
            break;
          case "reasoning_delta":
            setProcess((prev) => ({
              ...prev,
              reasoning: (prev.reasoning + String(data.text ?? "")).slice(-6000),
              collapsed: false,
            }));
            break;
          case "tool_call": {
            const label = toolLabel(String(data.name ?? ""));
            const meta = toolMeta(data.args);
            setProcess((prev) => ({
              ...prev,
              steps: [
                ...prev.steps,
                {
                  id: prev.steps.length + 1,
                  icon: label === "网页搜索" ? "🌐" : "⚙",
                  text: meta ? `${label}  ${meta}` : label,
                  meta: "进行中",
                  tone: "normal",
                  done: false,
                },
              ],
            }));
            break;
          }
          case "tool_confirmation_required":
            setProcess((prev) => ({
              ...prev,
              steps: [
                ...prev.steps,
                {
                  id: prev.steps.length + 1,
                  icon: "⚠️",
                  text: `需要你确认：${String(data.name ?? "工具")}（会改动平台数据或产生费用，回复"确认"后执行）`,
                  meta: "待确认",
                  tone: "warn",
                  done: false,
                },
              ],
            }));
            break;
          case "connector_warning": {
            const detail = (data.detail ?? {}) as Record<string, string>;
            const parts = Object.entries(detail)
              .filter(([, reason]) => reason)
              .map(([id, reason]) => `${id}：${reason}`);
            if (!parts.length) break;
            setProcess((prev) => ({
              ...prev,
              steps: [
                ...prev.steps,
                {
                  id: prev.steps.length + 1,
                  icon: "🔌",
                  text: `连接器工具没有注入：${parts.join("；")}`,
                  meta: "连接器",
                  tone: "warn",
                  done: false,
                },
              ],
            }));
            break;
          }
          case "answer_delta":
            setProcess((prev) => ({
              ...prev,
              // 开始吐字 = 前面的步骤都过了，一起标成完成（警告行不动）
              steps: prev.steps.map((s) => (s.tone === "warn" ? s : { ...s, done: true, meta: "完成" })),
              answer: prev.answer + String(data.text ?? ""),
              phase: "answering",
            }));
            break;
          default:
            break;
        }
      });

      es.addEventListener("done", (raw) => {
        es.close();
        if (turnEsRef.current === es) turnEsRef.current = null;
        let data: { status?: string; answer?: string } = {};
        try {
          data = JSON.parse((raw as MessageEvent<string>).data || "{}");
        } catch {
          /* 忽略 */
        }
        const failed = data.status === "failed";
        setProcess((prev) => ({
          ...prev,
          phase: "done",
          status: data.status || "succeeded",
          steps: prev.steps.map((s) =>
            s.tone === "warn" ? s : { ...s, done: true, meta: failed ? "失败" : "完成" },
          ),
          answer: prev.answer || String(data.answer ?? ""),
          collapsed: true,
          endedAt: Date.now(),
        }));
        setPending(null);
        void loadSessions();
        if (sidRef.current !== sid) return;
        // 库里已经有答复时，把流式那张临时卡片收掉，否则会看到两份一样的话
        void (async () => {
          try {
            const fresh = await hubApi.timeline(sid);
            if (sidRef.current !== sid) return;
            const persisted = lastAssistant(fresh.messages ?? []);
            if (resumed || persisted || !liveRef.current?.answer) {
              applyTimeline(fresh);
              // 记住这一轮做了什么：卡片留着（折叠），只把流式的临时答复撤掉——
              // 库里那条已经画成气泡了，不撤就会看到两份一样的话。
              setProcess((prev) => ({ ...prev, answer: "", phase: "done", collapsed: true }));
            }
          } catch {
            /* 校准失败就保留屏幕上的内容 */
          }
        })();
      });

      es.onerror = () => {
        // 404 / 代理不支持 SSE：退化成轮询。可重连的断线交给浏览器自己带游标重连。
        if (es.readyState === EventSource.CLOSED && !fellBack) {
          fellBack = true;
          if (turnEsRef.current === es) turnEsRef.current = null;
          setLive(null);
          liveRef.current = null;
          void pollChatReply(turnId, sid);
        }
      };
    },
    [applyTimeline, closeTurnStream, loadSessions, pollChatReply, setProcess],
  );

  /* ---------------- 打开会话（依赖 streamTurn，所以放在它后面） ---------------- */

  const open = useCallback(
    async (id: string) => {
      if (!validSessionId(id)) return;
      const epoch = ++epochRef.current;
      closeTurnStream();
      setLive(null);
      liveRef.current = null;
      setPending(null);
      setError(null);
      sidRef.current = id;
      setSessionId(id);
      // 先清空，避免加载中先闪一眼上一会话的内容
      setMessages([]);
      setTurns([]);
      setTaskArtifact(null);

      try {
        const data = await hubApi.timeline(id);
        if (epoch !== epochRef.current) return;
        applyTimeline(data);
        setPhase("ready");
        // 这个会话里还有在跑的聊天 turn：把流接回去（后端不会因为浏览器断开而停）
        const liveTurn = (data.chat_turns ?? []).find(
          (t) => t.status === "accepted" || t.status === "running",
        );
        if (liveTurn?.turn_id && !data.live_task_id) {
          setPending("正在思考…");
          streamTurn(liveTurn.turn_id, id, { resume: true });
        }
      } catch (err) {
        if (epoch !== epochRef.current) return;
        if (err instanceof ApiError && err.status === 401) {
          setNeedLogin(true);
          setPhase("ready");
          return;
        }
        setPhase("error");
        setError(errorMessage(err));
      }
    },
    [applyTimeline, closeTurnStream, streamTurn],
  );

  /* ---------------- 发送 ---------------- */

  const send = useCallback(
    async (
      raw: string,
      options: { chips?: RefChip[]; addonSelection?: AddonsPatch | null } = {},
    ): Promise<{ ok: boolean; error?: string }> => {
      const text = raw.trim();
      if (!text || busyRef.current) return { ok: false };

      // 引用怎么落地见 `mentions.ts` 文件头：技能进 content_blocks（服务端据此
      // 生效），文件路径拼在正文末尾（服务端只存不读，模型得从正文里看到它），
      // 连接器走 connector_ids。
      const chips = options.chips ?? [];
      const blocks = chipBlocks(chips);
      const marker = fileMarkerLine(chips);
      const message = marker ? `${text}\n\n${marker}` : text;

      // 会话 id 先定下来再发（见文件头第 1 条）
      const isNew = !validSessionId(sidRef.current);
      const sid = isNew ? newSessionId() : sidRef.current;
      if (isNew) {
        sidRef.current = sid;
        setSessionId(sid);
        onSessionCreated?.(sid);
      }

      busyRef.current = true;
      setBusy(true);
      setError(null);
      setPending("正在思考…");
      setMessages((prev) => [...prev, { role: "user", content: message }]);

      try {
        const accepted = await hubApi.send(sid, {
          request_id: newRequestId(),
          message,
          connector_ids: connectorIds(chips),
          ...(blocks.length ? { content_blocks: blocks } : {}),
          // 新会话还没有会话行，PATCH /addons 会 404：会话级选择只能跟着第一条
          // 消息一起发，服务端与 turn 同事务落库（`docs/addons.md`）。
          ...(isNew && options.addonSelection
            ? { addon_selection: options.addonSelection as Record<string, unknown> }
            : {}),
        });
        if (accepted.kind === "ambiguous") {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: "你说的目标有多个任务，请指明要操作哪一个。" },
          ]);
        }
        void loadSessions();
        if (accepted.turn_id) {
          streamTurn(accepted.turn_id, sid);
        } else {
          const data = await hubApi.timeline(sid);
          if (sidRef.current === sid) applyTimeline(data);
        }
        return { ok: true };
      } catch (err) {
        setPending(null);
        // 回滚刚才乐观上屏的那条，原文交回输入框
        setMessages((prev) => prev.slice(0, -1));
        const message = errorMessage(err);
        setError(message);
        if (err instanceof ApiError && err.status === 401) setNeedLogin(true);
        setMessages((prev) => [...prev, { role: "assistant", content: "提交失败：" + message }]);
        return { ok: false, error: message };
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [applyTimeline, loadSessions, onSessionCreated, streamTurn],
  );

  /* ---------------- 新建会话 ---------------- */

  const newSession = useCallback(() => {
    epochRef.current += 1;
    closeTurnStream();
    sidRef.current = "";
    setSessionId("");
    setMessages([]);
    setTurns([]);
    setLive(null);
    liveRef.current = null;
    setPending(null);
    setTaskArtifact(null);
    setError(null);
  }, [closeTurnStream]);

  /* ---------------- 启动 ---------------- */

  useEffect(() => {
    let alive = true;
    void (async () => {
      await loadSessions();
      if (!alive) return;
      setPhase("ready");
      // 上一次停在哪条会话，这次就回到哪条
      let remembered = "";
      try {
        remembered = localStorage.getItem("wb.hub.session") || "";
      } catch {
        /* 隐私模式下没有 localStorage */
      }
      if (validSessionId(remembered)) void open(remembered);
    })();
    return () => {
      alive = false;
    };
    // 只在挂载时跑一次：open / loadSessions 的引用变化不该重开会话
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    try {
      if (validSessionId(sessionId)) localStorage.setItem("wb.hub.session", sessionId);
    } catch {
      /* 忽略 */
    }
  }, [sessionId]);

  /* 每秒刷一次"已处理 Ns"：只在有活的 turn 时跑，不做全局心跳 */
  const [, tick] = useState(0);
  useEffect(() => {
    if (!live || live.phase === "done") return;
    const timer = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [live]);

  const elapsed = live
    ? formatElapsed((live.endedAt ?? Date.now()) - (live.startedAt || Date.now()))
    : "";

  return {
    sessions,
    sessionId,
    messages,
    turns,
    live,
    pending,
    elapsed,
    taskArtifact,
    phase,
    error,
    busy,
    needLogin,
    open,
    newSession,
    send,
    loadSessions,
    clearError: () => setError(null),
  };
}

/** 时间线里最后一条 assistant 消息的正文（没有就空串）。 */
function lastAssistant(messages: HubMessage[]) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === "assistant") return String(messages[i]?.content ?? "").trim();
  }
  return "";
}
