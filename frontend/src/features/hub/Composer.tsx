/**
 * 输入器：吸底、自增高、Enter 发送 Shift+Enter 换行、`@` 引用、`＋` 添加。
 *
 * 三条行为照旧页：
 *  1. 输入法组字中（`isComposing`）按 Enter **不发送**——中文用户最容易踩的坑；
 *  2. 高度随内容长，超过 180px 自己滚；
 *  3. 发送失败时把原文放回框里，而不是让用户重打一遍。
 *
 * 引用分两层，界面上必须能一眼分开：
 *  - **会话级**（专家 + 技能）：写进 `sessions.addon_selection`，之后每一轮都生效；
 *  - **消息级**（技能 / 文件 / 连接器）：只作用于这一条消息，发完就清空。
 * 会话级芯片用靛青描边、消息级用灰底，两组都排在输入框上方。
 *
 * 芯片为什么不做成行内节点：旧页输入框是 contenteditable，芯片是插在文本流里的
 * `<span contenteditable=false>`，代价是整套光标/输入法/退格逻辑都得自己写（旧页
 * 为此写了近千行）。textarea 塞不进行内节点，换回 contenteditable 会把中文输入法
 * 的既得利益全丢掉。落点差异见 `mentions.ts` 文件头。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { Icon } from "./icons";
import { ComposerPicker, type PickerItem } from "./ComposerPicker";
import {
  addChip as withChip,
  chipKey,
  chipLabel,
  mentionQuery,
  removeChip,
  type RefChip,
} from "./mentions";
import {
  connectorApi,
  skillApi,
  workspaceApi,
  type ConnectorItem,
  type SkillItem,
  type WorkspaceEntry,
} from "./api";
import type { AddonsView } from "./useAddons";
import type { ModelRow } from "./types";

/** 引用只作用于这一条消息——这句必须在面板里说清楚，否则用户以为改了会话设置。 */
const PICKER_FOOTER = "只作用于这一条消息 · 文件只登记路径，要不要读它由模型自己决定";

/** 浏览器语音识别没有标准类型，只声明用到的那几个字段。 */
interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  maxAlternatives: number;
  continuous: boolean;
  onresult: ((event: SpeechEventLike) => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
}

interface SpeechEventLike {
  results: { length: number; [index: number]: { [index: number]: { transcript: string } } };
}

type SpeechCtor = new () => SpeechRecognitionLike;

function speechCtor(): SpeechCtor | null {
  const bag = window as unknown as {
    SpeechRecognition?: SpeechCtor;
    webkitSpeechRecognition?: SpeechCtor;
  };
  return bag.SpeechRecognition ?? bag.webkitSpeechRecognition ?? null;
}

function parentOf(path: string): string {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.length ? parts.join("/") : ".";
}

function chipIcon(chip: RefChip) {
  if (chip.kind === "skill") return "slash" as const;
  if (chip.kind === "connector") return "plug" as const;
  return chip.isDir ? ("folder" as const) : ("doc" as const);
}

/** 小文件别显示成 `1K`——18 字节写 1K 会让人以为选错了文件。 */
function fileSize(bytes?: number): string | undefined {
  if (!bytes) return undefined;
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}K`;
  return `${(bytes / 1024 / 1024).toFixed(1)}M`;
}

export function Composer({
  value,
  onChange,
  onSend,
  disabled,
  busy,
  models,
  currentModel,
  currentModelLabel,
  onSelectModel,
  spaceLabel,
  spaceTitle,
  chips,
  onChipsChange,
  addons,
  onOpenAddons,
  onRemoveAddonSkill,
}: {
  value: string;
  onChange: (next: string) => void;
  onSend: () => void;
  disabled?: boolean;
  busy?: boolean;
  models: ModelRow[];
  currentModel: string;
  /** 模型名（`deepseek-v4-1-flash` 是接口 id，不是给人看的） */
  currentModelLabel: string;
  onSelectModel: (id: string) => void;
  spaceLabel: string;
  spaceTitle: string;
  chips: RefChip[];
  onChipsChange: (next: RefChip[]) => void;
  /** 会话级选择：专家 + 技能（影响之后的每一轮） */
  addons: AddonsView;
  onOpenAddons: () => void;
  /** 从胶囊上直接撤掉一个会话级技能 */
  onRemoveAddonSkill: (id: string) => void;
}) {
  const area = useRef<HTMLTextAreaElement>(null);
  const toast = useToast();
  const [modelMenu, setModelMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const [listening, setListening] = useState(false);
  const [voiceUnsupported, setVoiceUnsupported] = useState(false);
  const recognizerRef = useRef<SpeechRecognitionLike | null>(null);

  /* 浏览器支持与否只判一次：不支持就不画麦克风，点了才说"不支持"更像敷衍 */
  useEffect(() => {
    setVoiceUnsupported(!speechCtor());
  }, []);

  /* 离开页面时别让麦克风继续录 */
  useEffect(() => {
    return () => {
      try {
        recognizerRef.current?.stop();
      } catch {
        /* 已经停了 */
      }
    };
  }, []);

  const startVoice = useCallback(() => {
    const Ctor = speechCtor();
    if (!Ctor) {
      setVoiceUnsupported(true);
      toast("当前浏览器不支持语音输入，可以改用 Chrome 或 Edge", "error");
      return;
    }
    if (listening) {
      try {
        recognizerRef.current?.stop();
      } catch {
        /* 忽略 */
      }
      return;
    }
    const rec = new Ctor();
    rec.lang = "zh-CN";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    rec.continuous = false;
    rec.onresult = (event) => {
      const text = event.results?.[0]?.[0]?.transcript ?? "";
      if (!text) return;
      // 追加到光标处不现实（识别结果是后到的），统一接在末尾并补一个空格
      const next = value && !/\s$/.test(value) ? `${value} ${text}` : `${value}${text}`;
      onChange(next);
      requestAnimationFrame(() => area.current?.focus());
    };
    rec.onerror = (event) => {
      setListening(false);
      // 用户自己按了取消 / 一分钟没说话：不是故障，别弹红条
      if (event?.error === "aborted" || event?.error === "no-speech") return;
      toast("语音识别失败，请重试", "error");
    };
    rec.onend = () => setListening(false);
    try {
      rec.start();
      recognizerRef.current = rec;
      setListening(true);
      toast("正在听…说完自动填入", "ok");
    } catch {
      setListening(false);
      toast("语音输入启动失败", "error");
    }
  }, [listening, onChange, toast, value]);

  /* ---------------- 选择面板 ---------------- */

  const [picker, setPicker] = useState<{ origin: "plus" | "mention" } | null>(null);
  /** `@query` 在草稿里的范围；选中后要把这段从正文里吃掉 */
  const [token, setToken] = useState<{ start: number; end: number } | null>(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [connectors, setConnectors] = useState<ConnectorItem[]>([]);
  const [files, setFiles] = useState<WorkspaceEntry[]>([]);
  const [dir, setDir] = useState(".");
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [filesLoading, setFilesLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const catalogLoaded = useRef(false);
  const pickerRef = useRef<HTMLDivElement>(null);
  const plusRef = useRef<HTMLButtonElement>(null);
  /** 选中引用后要把光标放回的位置（吃掉了 `@query`，得手动归位） */
  const caretRef = useRef<number | null>(null);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(180, el.scrollHeight)}px`;
  }, [value]);

  useEffect(() => {
    if (caretRef.current == null) return;
    const el = area.current;
    if (!el) return;
    const pos = caretRef.current;
    caretRef.current = null;
    el.focus();
    el.setSelectionRange(pos, pos);
  }, [value]);

  /* 模型菜单点外部关闭 */
  useEffect(() => {
    if (!modelMenu) return;
    const close = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setModelMenu(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [modelMenu]);

  /* 选择面板点外部关闭。`＋` 按钮自己负责开关，所以也要排除掉它。 */
  const pickerOpen = picker !== null;
  useEffect(() => {
    if (!pickerOpen) return;
    const close = (event: MouseEvent) => {
      const node = event.target as Node;
      if (pickerRef.current?.contains(node)) return;
      if (plusRef.current?.contains(node)) return;
      setPicker(null);
      setToken(null);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [pickerOpen]);

  /* 技能与连接器：打开过就不再拉（一次会话里装的东西不会变） */
  useEffect(() => {
    if (!pickerOpen || catalogLoaded.current) return;
    catalogLoaded.current = true;
    void (async () => {
      setCatalogLoading(true);
      setLoadError(null);
      const [skillRes, connectorRes] = await Promise.allSettled([
        skillApi.list(""),
        connectorApi.list(),
      ]);
      if (skillRes.status === "fulfilled") {
        setSkills(
          (skillRes.value.items ?? []).filter((item) => item.on_disk && item.enabled !== false),
        );
      }
      if (connectorRes.status === "fulfilled") {
        setConnectors((connectorRes.value.items ?? []).filter((item) => item.configured));
      }
      if (skillRes.status === "rejected" && connectorRes.status === "rejected") {
        catalogLoaded.current = false; // 允许下次打开重试
        setLoadError("技能与连接器都没读到：" + errorMessage(skillRes.reason));
      }
      setCatalogLoading(false);
    })();
  }, [pickerOpen]);

  /* 工作区文件：按目录拉 */
  useEffect(() => {
    if (!pickerOpen) return;
    let alive = true;
    setFilesLoading(true);
    workspaceApi
      .files(dir)
      .then((data) => {
        if (!alive) return;
        setFiles(data.entries ?? []);
        setFilesLoading(false);
      })
      .catch(() => {
        if (!alive) return;
        setFiles([]);
        setFilesLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [pickerOpen, dir]);

  // 换目录或重开面板后，高亮回到第一项，否则会停在一个不存在的位置上
  useEffect(() => {
    setActive(0);
  }, [dir, query, picker?.origin]);

  const closePicker = useCallback(() => {
    setPicker(null);
    setToken(null);
  }, []);

  const pickChip = useCallback(
    (chip: RefChip) => {
      onChipsChange(withChip(chips, chip));
      // 把 `@query` 从正文里吃掉：留着就会变成一句 "@每日" 的残句
      if (picker?.origin === "mention" && token) {
        onChange(value.slice(0, token.start) + value.slice(token.end));
        caretRef.current = token.start;
      } else {
        area.current?.focus();
      }
      setPicker(null);
      setToken(null);
    },
    [chips, onChipsChange, onChange, picker, token, value],
  );

  const items = useMemo<PickerItem[]>(() => {
    if (!picker) return [];
    const q = query.trim().toLowerCase();
    const hit = (text?: string) => !q || String(text ?? "").toLowerCase().includes(q);
    const out: PickerItem[] = [];

    /* 文件：先目录，后文件。当前目录不是根时给一条「返回上一级」。 */
    const fileGroup = dir === "." ? "工作区文件" : `工作区文件 · ${dir}`;
    /**
     * 换目录必须把过滤词清掉：带着「bots」进 `bots/`，里面的 `demo.txt`
     * 一条都不匹配，用户会以为目录是空的。
     */
    const gotoDir = (next: string) => {
      setDir(next);
      setQuery("");
    };
    if (dir !== ".") {
      out.push({
        key: "dir:..",
        label: "返回上一级",
        hint: parentOf(dir) === "." ? "工作区" : parentOf(dir),
        group: fileGroup,
        icon: "chevron",
        onPick: () => gotoDir(parentOf(dir)),
      });
    }
    for (const entry of files) {
      if (entry.kind === "dir") {
        if (!hit(entry.name)) continue;
        out.push({
          key: `dir:${entry.path}`,
          label: `${entry.name}/`,
          group: fileGroup,
          icon: "folder",
          onPick: () => gotoDir(entry.path),
        });
        continue;
      }
      if (!hit(entry.name)) continue;
      out.push({
        key: `file:${entry.path}`,
        label: entry.name,
        hint: fileSize(entry.size),
        group: fileGroup,
        icon: "doc",
        onPick: () => pickChip({ kind: "file", path: entry.path, name: entry.name, isDir: false }),
      });
    }

    /* 技能：真正有效的那种引用，服务端会把它加成「这一轮必须用」 */
    for (const skill of skills) {
      if (!hit(skill.name) && !hit(skill.id)) continue;
      const key = `skill:${skill.id}`;
      const already = chips.some((chip) => chipKey(chip) === key);
      out.push({
        key,
        label: skill.name || skill.id,
        note: already ? "已引用" : undefined,
        group: "技能",
        icon: "slash",
        disabled: already,
        onPick: () => pickChip({ kind: "skill", id: skill.id, name: skill.name || skill.id }),
      });
    }

    /* 连接器：只留配好的；要授权的点不动，标出来而不是让它静默失败 */
    for (const connector of connectors) {
      if (!hit(connector.name) && !hit(connector.id)) continue;
      const key = `connector:${connector.id}`;
      const already = chips.some((chip) => chipKey(chip) === key);
      out.push({
        key,
        label: connector.name || connector.id,
        note: already ? "已引用" : connector.need_authorize ? "待授权" : undefined,
        group: "连接器",
        icon: "plug",
        disabled: already || !!connector.need_authorize,
        onPick: () =>
          pickChip({ kind: "connector", id: connector.id, name: connector.name || connector.id }),
      });
    }
    return out;
  }, [picker, query, dir, files, skills, connectors, chips, pickChip]);

  const loading = catalogLoading || filesLoading;
  const canSend = value.trim().length > 0 && !disabled && !busy;

  /** 输入框与面板搜索框共用：↑↓ 选、Enter/Tab 确认、Esc 关。 */
  const onPickerKey = useCallback(
    (event: React.KeyboardEvent) => {
      if (!picker) return false;
      if (event.key === "Escape") {
        event.preventDefault();
        closePicker();
        area.current?.focus();
        return true;
      }
      if (event.nativeEvent.isComposing) return false; // 组字中的 Enter 是选词，不是确认
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!items.length) return true;
        const step = event.key === "ArrowDown" ? 1 : -1;
        setActive((prev) => (prev + step + items.length) % items.length);
        return true;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        const item = items[active];
        if (item && !item.disabled) item.onPick();
        return true;
      }
      return false;
    },
    [picker, items, active, closePicker],
  );

  const handleChange = useCallback(
    (next: string) => {
      onChange(next);
      const el = area.current;
      const caret = el?.selectionStart ?? next.length;
      const found = mentionQuery(next, caret);
      if (found) {
        setToken({ start: found.start, end: found.end });
        setQuery(found.query);
        // 从 `＋` 面板里直接打 `@` 也要切到提及模式，否则选完不会吃掉 `@query`
        setPicker((prev) => (prev?.origin === "mention" ? prev : { origin: "mention" }));
        return;
      }
      // 光标离开 `@` 段就收掉面板，否则它会杵在那里跟正文脱节
      if (picker?.origin === "mention") {
        setPicker(null);
        setToken(null);
      }
    },
    [onChange, picker],
  );

  const openPlus = useCallback(() => {
    if (picker?.origin === "plus") {
      closePicker();
      return;
    }
    setQuery("");
    setDir(".");
    setToken(null);
    setPicker({ origin: "plus" });
  }, [picker, closePicker]);

  return (
    <div className="border-t border-line bg-paper px-7 pt-2.5 pb-3.5">
      <div className="relative mx-auto w-full max-w-[720px]">
        {picker ? (
          <div ref={pickerRef}>
            <ComposerPicker
              items={items}
              active={active}
              loading={loading && items.length === 0}
              error={loadError}
              query={query}
              searchable={picker.origin === "plus"}
              footer={PICKER_FOOTER}
              onQuery={setQuery}
              onKeyDown={onPickerKey}
              onHover={setActive}
            />
          </div>
        ) : null}

        <div
          className={cn(
            "rounded-xl border bg-paper px-3 pt-2.5 pb-2 transition-[border-color,box-shadow] duration-150 ease-standard",
            "border-line-strong focus-within:border-signal focus-within:shadow-[0_0_0_3px_rgb(51_85_127_/_0.10)]",
          )}
        >
          {addons.expert || addons.skills.length ? (
            <div className="mb-1.5 flex flex-wrap items-center gap-1">
              <span className="text-label shrink-0 text-signal">会话</span>
              {addons.expert ? (
                <span
                  className="inline-flex h-[24px] max-w-[200px] items-center gap-1 rounded-md border border-signal/35 bg-signal/[0.06] px-1.5 text-label text-ink-2"
                  title={`当前专家：${addons.expert.name || addons.expert.id}（在「会话资源」里更换）`}
                >
                  <span aria-hidden="true" className="shrink-0 text-[11px] leading-none">
                    {addons.expert.icon || "🧩"}
                  </span>
                  <span className="min-w-0 truncate">{addons.expert.name || addons.expert.id}</span>
                </span>
              ) : null}
              {addons.skills.map((skill) => {
                const label = skill.name || skill.id;
                return (
                  <span
                    key={skill.id}
                    className="inline-flex h-[24px] max-w-[180px] items-center gap-1 rounded-md border border-signal/35 bg-signal/[0.06] pr-1 pl-1.5 text-label text-ink-2"
                  >
                    <Icon name="slash" size={10} className="shrink-0 text-muted" />
                    <span className="min-w-0 truncate" title={label}>
                      {label}
                    </span>
                    <button
                      type="button"
                      aria-label={`从当前会话移除技能 ${label}`}
                      onClick={() => onRemoveAddonSkill(skill.id)}
                      className="flex h-[16px] w-[16px] shrink-0 items-center justify-center rounded-[3px] text-muted transition-colors duration-150 hover:bg-line hover:text-ink"
                    >
                      <Icon name="close" size={10} />
                    </button>
                  </span>
                );
              })}
            </div>
          ) : null}

          {chips.length ? (
            <div className="mb-1.5 flex flex-wrap items-center gap-1">
              {chips.map((chip) => {
                const key = chipKey(chip);
                const label = chipLabel(chip);
                return (
                  <span
                    key={key}
                    className="inline-flex h-[24px] max-w-[200px] items-center gap-1 rounded-md bg-sunken pr-1 pl-1.5 text-label text-ink-2"
                  >
                    <Icon name={chipIcon(chip)} size={11} className="shrink-0 text-muted" />
                    <span className="min-w-0 truncate" title={label}>
                      {label}
                    </span>
                    <button
                      type="button"
                      aria-label={`移除引用 ${label}`}
                      onClick={() => onChipsChange(removeChip(chips, key))}
                      className="flex h-[16px] w-[16px] shrink-0 items-center justify-center rounded-[3px] text-muted transition-colors duration-150 hover:bg-line hover:text-ink"
                    >
                      <Icon name="close" size={10} />
                    </button>
                  </span>
                );
              })}
            </div>
          ) : null}

          <textarea
            ref={area}
            rows={1}
            value={value}
            disabled={disabled}
            onChange={(event) => handleChange(event.target.value)}
            onKeyDown={(event) => {
              if (onPickerKey(event)) return;
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                if (canSend) onSend();
              }
            }}
            placeholder="今天帮你做什么？输入主题，或粘贴一个链接…"
            aria-label="消息"
            spellCheck={false}
            className="w-full resize-none border-0 bg-transparent text-body leading-relaxed text-ink outline-none placeholder:text-muted disabled:cursor-not-allowed"
          />

          <div className="mt-1.5 flex items-center gap-2">
            <button
              ref={plusRef}
              type="button"
              onClick={openPlus}
              aria-haspopup="listbox"
              aria-expanded={picker?.origin === "plus"}
              title="添加技能、文件或连接器"
              className={cn(
                "flex h-[24px] w-[24px] shrink-0 items-center justify-center rounded-md transition-colors duration-150 ease-standard",
                picker?.origin === "plus"
                  ? "bg-sunken text-ink"
                  : "text-muted hover:bg-sunken hover:text-ink",
              )}
            >
              <Icon name="plus" size={14} />
            </button>

            <span
              className="min-w-0 flex-1 truncate text-label text-muted"
              title={spaceTitle || undefined}
            >
              {spaceLabel}
            </span>

            {/* 会话资源：专家 + 技能。放在 `＋` 右边，和"这一条消息的引用"分开 */}
            <button
              type="button"
              onClick={onOpenAddons}
              aria-haspopup="dialog"
              title="会话资源：专家与技能（对这个会话之后的每一轮生效）"
              className={cn(
                "flex h-[26px] max-w-[132px] shrink-0 items-center gap-1 rounded-md px-1.5 text-label transition-colors duration-150 ease-standard",
                addons.expert || addons.skills.length
                  ? "text-ink-2 hover:bg-sunken"
                  : "text-muted hover:bg-sunken hover:text-ink-2",
              )}
            >
              <Icon name="spark" size={12} className="shrink-0" />
              <span className="min-w-0 truncate">
                {addons.expert
                  ? addons.expert.name || addons.expert.id
                  : addons.skills.length
                    ? `技能 ${addons.skills.length}`
                    : "会话资源"}
              </span>
            </button>

            {/* 模型 */}
            <div className="relative shrink-0" ref={menuRef}>
              <button
                type="button"
                onClick={() => setModelMenu((v) => !v)}
                aria-haspopup="menu"
                aria-expanded={modelMenu}
                title="选择模型"
                className="flex h-[26px] items-center gap-1.5 rounded-md px-1.5 text-label text-muted transition-colors duration-150 ease-standard hover:bg-sunken hover:text-ink"
              >
                <span className="h-1.5 w-1.5 rounded-full bg-ok" aria-hidden="true" />
                <span className="max-w-[150px] truncate">{currentModelLabel || "默认模型"}</span>
                <Icon name="chevron" size={12} />
              </button>
              {modelMenu ? (
                <div
                  role="menu"
                  className="absolute right-0 bottom-[calc(100%+6px)] z-30 w-[240px] rounded-lg border border-line bg-paper p-1 shadow-[var(--shadow-overlay)]"
                >
                  <p className="px-2 py-1 text-label text-muted">选择模型</p>
                  {models.length === 0 ? (
                    <p className="px-2 py-1.5 text-label text-muted">没有可选的模型</p>
                  ) : (
                    models.map((model) => (
                      <button
                        key={model.id}
                        type="button"
                        role="menuitemradio"
                        aria-checked={model.id === currentModel}
                        onClick={() => {
                          onSelectModel(model.id);
                          setModelMenu(false);
                        }}
                        className={cn(
                          "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-secondary transition-colors duration-150 ease-standard hover:bg-sunken",
                          model.id === currentModel ? "text-ink" : "text-ink-2",
                        )}
                      >
                        <span className="min-w-0 flex-1 truncate">
                          {model.name || model.label || model.id}
                        </span>
                        {model.id === currentModel ? (
                          <Icon name="check" size={13} className="text-signal" />
                        ) : null}
                      </button>
                    ))
                  )}
                </div>
              ) : null}
            </div>

            {/* 语音：浏览器有 SpeechRecognition 才画。识别中按钮变色 + 可点停止 */}
            {voiceUnsupported ? null : (
              <button
                type="button"
                onClick={startVoice}
                aria-pressed={listening}
                aria-label={listening ? "停止语音输入" : "语音输入"}
                title={listening ? "正在听…点一下停止" : "语音输入（说完自动填入）"}
                className={cn(
                  "flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-md transition-colors duration-150 ease-standard",
                  listening
                    ? "bg-danger-wash text-danger"
                    : "text-muted hover:bg-sunken hover:text-ink",
                )}
              >
                <Icon name="mic" size={14} />
              </button>
            )}

            <button
              type="button"
              onClick={onSend}
              disabled={!canSend}
              aria-label="发送"
              className={cn(
                "flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded-md transition-colors duration-150 ease-standard",
                "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
                canSend ? "bg-signal text-paper hover:bg-signal-deep" : "bg-sunken text-muted",
                "disabled:cursor-not-allowed",
              )}
            >
              <Icon name="arrowUp" size={14} />
            </button>
          </div>
        </div>
        <p className="mt-1 px-1 text-label text-muted">Enter 发送 · Shift + Enter 换行 · @ 引用</p>
      </div>
    </div>
  );
}
