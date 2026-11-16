/**
 * 灵感社区：接入子平台的优秀成果，形态是"只读展厅"。
 *
 * 三件事与旧页保持一致：
 *  1. 数据来自 `GET /api/inspiration/items`。后端**还没上线**时会 404，
 *     这时静默降级成空态——它不是错误，是"以后才有内容"。
 *  2. 「看示例布局」只在前端渲染 DEMO_ITEMS，不落库、不调接口。
 *  3. 点卡片是把作品当作参考**放进输入框**，不自动发送。用户还得自己补一句
 *     "照着这个风格来"，直接发出去只会得到一句莫名其妙的回答。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import { useToast } from "@/components/ui/Toast";
import { cn } from "@/lib/cn";
import { Icon } from "./icons";

type ItemType = "report" | "image" | "video" | "canvas";

interface InspireItem {
  id?: string;
  title?: string;
  desc?: string;
  type?: ItemType;
  platform?: string;
  author?: string;
  stat?: string;
  /** 封面渐变的起止色，由子平台给，缺省用青绿 */
  c1?: string;
  c2?: string;
  url?: string;
}

const TYPE_LABEL: Record<string, string> = { report: "报告", image: "图文", video: "视频", canvas: "画布" };
const TYPE_ICON: Record<string, string> = { report: "doc", image: "grid", video: "spark", canvas: "panel" };
const TYPE_TABS: { value: string; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "report", label: "报告" },
  { value: "image", label: "图文" },
  { value: "video", label: "视频" },
  { value: "canvas", label: "画布" },
];

const DEMO_ITEMS: InspireItem[] = [
  {
    type: "report",
    platform: "本站",
    title: "2026 消费品牌出海观察报告",
    desc: "从 37 个海外站点抽取的渠道结构与价格带，附可复用的调研模板。",
    author: "研究助手",
    stat: "1.2k 浏览",
    c1: "#0fb5a1",
    c2: "#45d9bb",
  },
  {
    type: "video",
    platform: "BeeFTV",
    title: "15 秒胶片感产品短片",
    desc: "手冲咖啡器具，竖版 9:16，可直接用于信息流投放。",
    author: "阿澈",
    stat: "3.4k 播放",
    c1: "#4b6bfb",
    c2: "#93a9ff",
  },
  {
    type: "image",
    platform: "BeeFTV",
    title: "「山野」系列海报三件套",
    desc: "低频取景加大面积留白，主色取自苔绿与雾白。",
    author: "林一",
    stat: "2.1k 浏览",
    c1: "#f0854b",
    c2: "#ffc46b",
  },
  {
    type: "canvas",
    platform: "LibTV",
    title: "品牌 TVC 分镜画布",
    desc: "12 个镜头节点，标注了运镜与时长，可直接复用改拍。",
    author: "制作组",
    stat: "860 浏览",
    c1: "#7a5af8",
    c2: "#b6a0ff",
  },
  {
    type: "report",
    platform: "LibTV",
    title: "短剧出海题材热度榜",
    desc: "按地区拆分的近 30 天题材热度与完播曲线。",
    author: "数据组",
    stat: "540 浏览",
    c1: "#e2568c",
    c2: "#ffa8c6",
  },
  {
    type: "image",
    platform: "本站",
    title: "一张图讲清 MCP 接入",
    desc: "把连接器、工具、权限三件事画在同一张图上。",
    author: "大吕",
    stat: "980 浏览",
    c1: "#2f9cf4",
    c2: "#84caff",
  },
];

const platformOf = (item: InspireItem) => item.platform || "未标注平台";

export function InspireView({
  onQuote,
  onGoConnectors,
}: {
  onQuote: (text: string) => void;
  onGoConnectors: () => void;
}) {
  const toast = useToast();
  const [items, setItems] = useState<InspireItem[]>([]);
  const [demo, setDemo] = useState(false);
  const [type, setType] = useState("all");
  const [src, setSrc] = useState("all");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/inspiration/items", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { items?: InspireItem[] };
      setItems(Array.isArray(data.items) ? data.items : []);
    } catch {
      // 接口还没上线：保持空态，不弹错误
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const data = demo ? DEMO_ITEMS : items;

  const platforms = useMemo(() => [...new Set(data.map(platformOf))], [data]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return data.filter((item) => {
      if (type !== "all" && item.type !== type) return false;
      if (src !== "all" && platformOf(item) !== src) return false;
      if (!q) return true;
      return [item.title, item.desc, item.author, platformOf(item)]
        .join(" ")
        .toLowerCase()
        .includes(q);
    });
  }, [data, type, src, query]);

  const quote = (item: InspireItem) => {
    onQuote(`参考这份作品：${item.title || ""}（${platformOf(item)}）`);
    toast(`已把「${item.title || "作品"}」作为参考放进输入框`);
  };

  return (
    <div className="mx-auto w-full max-w-[1000px] px-7 py-6">
      {/* 标题区 */}
      <div className="mb-4 flex flex-wrap items-start gap-3">
        <div className="min-w-0">
          <h2 className="text-title font-semibold text-ink">灵感社区</h2>
          <p className="mt-1 text-secondary text-muted">
            已接入子平台的优秀成果会同步到这里，可一键引用到当前任务
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索作品 / 作者 / 平台"
            aria-label="搜索作品"
            className="h-[30px] w-[200px] rounded-md border border-line bg-paper px-2.5 text-body text-ink placeholder:text-muted focus-visible:border-signal/50 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/15"
          />
          <Button
            variant="secondary"
            onClick={() => {
              if (demo) {
                setDemo(false);
                void load();
              } else {
                void load();
              }
            }}
          >
            刷新
          </Button>
        </div>
      </div>

      {data.length ? (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <div className="flex gap-0.5 rounded-md bg-sunken p-0.5" role="tablist" aria-label="按类型筛选">
            {TYPE_TABS.map((tab) => (
              <button
                key={tab.value}
                type="button"
                role="tab"
                aria-selected={type === tab.value}
                onClick={() => setType(tab.value)}
                className={cn(
                  "h-[26px] rounded-sm px-2.5 text-secondary transition-colors duration-150 ease-standard",
                  type === tab.value
                    ? "bg-paper text-ink shadow-[0_1px_2px_rgba(22,32,46,0.08)]"
                    : "text-muted hover:text-ink",
                )}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <div className="ml-1 flex flex-wrap gap-1">
            {["all", ...platforms].map((name) => (
              <button
                key={name}
                type="button"
                aria-pressed={src === name}
                onClick={() => setSrc(name)}
                className={cn(
                  "h-[24px] rounded-full border px-2.5 text-label transition-colors duration-150 ease-standard",
                  src === name
                    ? "border-signal/40 bg-signal-wash text-signal"
                    : "border-line bg-paper text-muted hover:border-line-strong hover:text-ink",
                )}
              >
                {name === "all" ? "全部平台" : name}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {demo ? (
        <div className="mb-3 flex items-center gap-1.5 text-label text-muted">
          <Icon name="spark" size={12} />
          以下是示例布局，接入子平台后由真实成果替换
        </div>
      ) : null}

      {loading && !demo ? (
        <p className="py-12 text-center text-secondary text-muted">加载中…</p>
      ) : visible.length ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {visible.map((item, index) => (
            <InspireCard key={item.id || `${item.title}-${index}`} item={item} onUse={() => quote(item)} />
          ))}
        </div>
      ) : data.length ? (
        <p className="py-12 text-center text-secondary text-muted">没有匹配的作品，换个关键词试试。</p>
      ) : (
        <EmptyInspire onGoConnectors={onGoConnectors} onDemo={() => setDemo(true)} />
      )}
    </div>
  );
}

function InspireCard({ item, onUse }: { item: InspireItem; onUse: () => void }) {
  const type = item.type && TYPE_LABEL[item.type] ? item.type : "image";
  return (
    <button
      type="button"
      onClick={onUse}
      className="group flex flex-col overflow-hidden rounded-lg border border-line bg-paper text-left transition-[border-color,transform] duration-150 ease-standard hover:-translate-y-px hover:border-line-strong focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/20"
    >
      <div
        className="relative flex h-[104px] items-center justify-center"
        style={{
          background: `linear-gradient(135deg, ${item.c1 || "#0fb5a1"}, ${item.c2 || "#45d9bb"})`,
        }}
      >
        <span className="absolute top-2 left-2 rounded-full bg-black/25 px-2 py-[2px] text-label text-white">
          {TYPE_LABEL[type]}
        </span>
        <Icon name={TYPE_ICON[type] || "grid"} size={26} className="text-white/85" />
        <span className="absolute right-2 bottom-2 rounded-full bg-black/25 px-2 py-[2px] text-label text-white">
          {platformOf(item)}
        </span>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-1 px-3 py-2.5">
        <div className="truncate text-body font-medium text-ink" title={item.title}>
          {item.title || "未命名作品"}
        </div>
        <div className="line-clamp-2 text-secondary leading-relaxed text-muted">{item.desc || ""}</div>
        <div className="mt-auto flex items-center gap-1.5 pt-1.5 text-label text-muted">
          <span className="truncate">{item.author || "匿名"}</span>
          {item.stat ? (
            <>
              <i className="h-[3px] w-[3px] shrink-0 rounded-full bg-line-strong" aria-hidden="true" />
              <span className="truncate">{item.stat}</span>
            </>
          ) : null}
          {/* 只在悬停时点亮：卡片整体是可点的，"引用"是它的结果而不是另一个动作 */}
          <span className="ml-auto shrink-0 text-signal opacity-0 transition-opacity duration-150 ease-standard group-hover:opacity-100">
            引用 →
          </span>
        </div>
      </div>
    </button>
  );
}

function EmptyInspire({ onGoConnectors, onDemo }: { onGoConnectors: () => void; onDemo: () => void }) {
  return (
    <div className="mx-auto max-w-[520px] rounded-xl border border-line bg-rail px-6 py-8 text-center">
      <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-full bg-paper text-muted">
        <Icon name="spark" size={20} />
      </div>
      <h3 className="mt-3 text-body font-medium text-ink">还没有可展示的作品</h3>
      <p className="mt-1.5 text-secondary leading-relaxed text-muted">
        在「连接器」里接入子平台后，子平台被评为优秀的成果会自动同步到这里：
        报告、图文、视频、画布都能展成卡片，点开就能引用到当前任务。
      </p>
      <div className="mt-4 flex items-center justify-center gap-2">
        <Button variant="primary" onClick={onGoConnectors}>
          去连接器
        </Button>
        <Button variant="secondary" onClick={onDemo}>
          看示例布局
        </Button>
      </div>
      <p className="mt-3 text-label text-muted">当前还没有接入任何子平台</p>
    </div>
  );
}
