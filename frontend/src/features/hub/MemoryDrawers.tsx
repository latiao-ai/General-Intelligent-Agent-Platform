/**
 * 「我的画像」与「记忆与进化」两个抽屉。
 *
 * 三件事必须讲清楚，否则用户会以为界面上改的就是数据库里的东西：
 *  1. 这里改的是**给模型看的版本**，原文仍在库里（旧页文案如此，保留）。
 *  2. 画像与记忆里同名的条目，**画像优先**。
 *  3. 两者都是「每行 键：值」的纯文本。存之前不做格式校验——服务端会解析，
 *     解析不了的行走"未识别"分支比前端静默丢弃更好排查。
 */
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { useToast } from "@/components/ui/Toast";
import { memoryApi } from "./api";

const areaCls =
  "w-full resize-y rounded-md border border-line bg-paper px-2.5 py-2 font-mono text-secondary leading-relaxed " +
  "text-ink placeholder:text-muted focus-visible:border-signal/50 focus-visible:outline-none " +
  "focus-visible:ring-[3px] focus-visible:ring-signal/15";

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <div className="mt-1 text-label font-medium text-ink-2">{children}</div>;
}

/* ---------------- 我的画像 ---------------- */

export function ProfileDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const [text, setText] = useState("");
  const [meta, setMeta] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await memoryApi.profile();
      setText(data.text || "");
      const count = Object.keys(data.facts || {}).length;
      // `v` 是服务端每次写入自增的版本号，出问题时能对上日志
      setMeta(count ? `${count} 条 · v${data.version ?? "?"}` : "尚未建立");
    } catch (err) {
      setMeta(err instanceof Error ? `读取失败：${err.message}` : "读取失败");
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setStatus("");
    void load();
  }, [open, load]);

  const save = async () => {
    setBusy(true);
    setStatus("保存中…");
    try {
      const res = await memoryApi.saveProfile(text);
      // 服务端会重写文件头部（补注释与计数），所以要重新拉，不能拿本地文本当结果
      await load();
      setStatus(`已保存 · ${res.count ?? 0} 条 · v${res.version ?? "?"}`);
      toast("画像已保存");
    } catch (err) {
      setStatus(err instanceof Error ? `保存失败：${err.message}` : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    if (!window.confirm("确定清空画像？会在 profile/USER.md.bak 留一份备份。")) return;
    setStatus("清空中…");
    try {
      await memoryApi.clearProfile();
      await load();
      setStatus("已清空");
    } catch (err) {
      setStatus(err instanceof Error ? `清空失败：${err.message}` : "清空失败");
    }
  };

  return (
    <Drawer open={open} onClose={onClose} title="我的画像">
      <div className="flex flex-col gap-3">
        <div className="-mt-1 text-label text-muted">{meta}</div>

        <p className="text-secondary leading-relaxed text-muted">
          每行「键：值」一条。助手在规划任务时会自动补充，你也可以直接改、删、清空。
          保存后立即生效：闲聊和派任务都会读这份画像。与「记忆与进化」里同名的条目以这里为准（画像优先）。
        </p>

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
          rows={16}
          placeholder="例如：&#10;称呼：老李&#10;偏好：结论先行，不要客套话"
          className={areaCls}
        />

        <div className="flex items-center gap-2">
          <Button variant="ghost" className="text-danger hover:bg-danger-wash" onClick={() => void clear()}>
            清空画像
          </Button>
          <span className="text-label text-muted">{status}</span>
          <Button variant="primary" className="ml-auto" disabled={busy} onClick={() => void save()}>
            保存
          </Button>
        </div>
      </div>
    </Drawer>
  );
}

/* ---------------- 记忆与进化 ---------------- */

export function MemoryDrawer({
  open,
  onClose,
  sessionId,
}: {
  open: boolean;
  onClose: () => void;
  sessionId: string;
}) {
  const [summary, setSummary] = useState("");
  const [facts, setFacts] = useState("");
  const [meta, setMeta] = useState("");
  const [sessionStatus, setSessionStatus] = useState("");

  const [userFacts, setUserFacts] = useState("");
  const [userStatus, setUserStatus] = useState("");
  const [workspaceFacts, setWorkspaceFacts] = useState("");
  const [workspaceStatus, setWorkspaceStatus] = useState("");

  const loadSession = useCallback(async () => {
    if (!sessionId) {
      setMeta("当前没有会话");
      setSummary("");
      setFacts("");
      return;
    }
    try {
      const data = await memoryApi.session(sessionId);
      setSummary(data.summary || "");
      setFacts(data.facts_text || "");
      setMeta(data.upto ? `已压缩前 ${data.upto} 条消息` : "尚未压缩");
    } catch (err) {
      setMeta(err instanceof Error ? `读取失败：${err.message}` : "读取失败");
    }
  }, [sessionId]);

  const loadLongTerm = useCallback(async () => {
    try {
      const data = await memoryApi.longTerm();
      setUserFacts(data.user?.facts_text || "");
      setWorkspaceFacts(data.workspace?.facts_text || "");
    } catch {
      /* 读不到就留空，不影响上层保存 */
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setSessionStatus("");
    setUserStatus("");
    setWorkspaceStatus("");
    void loadSession();
    void loadLongTerm();
  }, [open, loadSession, loadLongTerm]);

  const saveSession = async () => {
    if (!sessionId) return setSessionStatus("当前没有会话");
    setSessionStatus("保存中…");
    try {
      const res = await memoryApi.saveSession(sessionId, { summary, facts });
      await loadSession();
      setSessionStatus(`已保存 · ${Object.keys(res.facts || {}).length} 条事实`);
    } catch (err) {
      setSessionStatus(err instanceof Error ? `保存失败：${err.message}` : "保存失败");
    }
  };

  const clearSession = async () => {
    if (!sessionId) return setSessionStatus("当前没有会话");
    if (!window.confirm("清空会话记忆？之后会从原文重新累积。")) return;
    try {
      await memoryApi.clearSession(sessionId);
      await loadSession();
      setSessionStatus("已清空");
    } catch (err) {
      setSessionStatus(err instanceof Error ? `清空失败：${err.message}` : "清空失败");
    }
  };

  const saveLongTerm = async (scope: "user" | "workspace") => {
    const setStatus = scope === "user" ? setUserStatus : setWorkspaceStatus;
    setStatus("保存中…");
    try {
      const res = await memoryApi.saveLongTerm(scope, scope === "user" ? userFacts : workspaceFacts);
      await loadLongTerm();
      setStatus(`已保存 · ${res.count ?? 0} 条`);
    } catch (err) {
      setStatus(err instanceof Error ? `保存失败：${err.message}` : "保存失败");
    }
  };

  const clearLongTerm = async (scope: "user" | "workspace") => {
    const setStatus = scope === "user" ? setUserStatus : setWorkspaceStatus;
    if (!window.confirm(scope === "user" ? "清空用户长期记忆？" : "清空当前工作区记忆？")) return;
    try {
      await memoryApi.clearLongTerm(scope);
      await loadLongTerm();
      setStatus("已清空");
    } catch (err) {
      setStatus(err instanceof Error ? `清空失败：${err.message}` : "清空失败");
    }
  };

  return (
    <Drawer open={open} onClose={onClose} title="记忆与进化" width="wide">
      <div className="flex flex-col gap-3">
        <div className="-mt-1 text-label text-muted">{meta}</div>

        <p className="text-secondary leading-relaxed text-muted">
          分三层：会话记忆（本轮压缩）、用户长期记忆（跨会话）、工作区记忆（当前目录）。
          原文仍在数据库里，改这里只是改「给模型看的版本」。
        </p>

        <SectionLabel>会话记忆 · 摘要</SectionLabel>
        <textarea
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          spellCheck={false}
          rows={5}
          className={areaCls}
        />

        <SectionLabel>会话记忆 · 关键事实（每行「键：值」）</SectionLabel>
        <textarea
          value={facts}
          onChange={(e) => setFacts(e.target.value)}
          spellCheck={false}
          rows={4}
          placeholder={sessionId ? "" : "当前没有会话，这一层不可编辑"}
          className={areaCls}
        />

        <div className="flex items-center gap-2">
          <Button variant="ghost" className="text-danger hover:bg-danger-wash" onClick={() => void clearSession()}>
            清空会话记忆
          </Button>
          <span className="text-label text-muted">{sessionStatus}</span>
          <Button variant="primary" className="ml-auto" onClick={() => void saveSession()}>
            保存会话记忆
          </Button>
        </div>

        <div className="mt-2 h-px bg-line" />

        <SectionLabel>用户长期记忆（跨会话，每行「键：值」）</SectionLabel>
        <textarea
          value={userFacts}
          onChange={(e) => setUserFacts(e.target.value)}
          spellCheck={false}
          rows={4}
          className={areaCls}
        />
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            className="text-danger hover:bg-danger-wash"
            onClick={() => void clearLongTerm("user")}
          >
            清空用户记忆
          </Button>
          <span className="text-label text-muted">{userStatus}</span>
          <Button variant="primary" className="ml-auto" onClick={() => void saveLongTerm("user")}>
            保存用户记忆
          </Button>
        </div>

        <div className="mt-2 h-px bg-line" />

        <SectionLabel>工作区记忆（当前目录，每行「键：值」）</SectionLabel>
        <textarea
          value={workspaceFacts}
          onChange={(e) => setWorkspaceFacts(e.target.value)}
          spellCheck={false}
          rows={4}
          className={areaCls}
        />
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            className="text-danger hover:bg-danger-wash"
            onClick={() => void clearLongTerm("workspace")}
          >
            清空工作区记忆
          </Button>
          <span className="text-label text-muted">{workspaceStatus}</span>
          <Button variant="primary" className="ml-auto" onClick={() => void saveLongTerm("workspace")}>
            保存工作区记忆
          </Button>
        </div>
      </div>
    </Drawer>
  );
}
