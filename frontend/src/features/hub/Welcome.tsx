/**
 * 空态：一句主张 + 6 个场景 + 4 条示例。
 *
 * 文案照抄旧页——它是这个产品唯一讲清"能干什么"的地方，改词属于产品决策。
 * 点任意一条都会把提示词填进输入框（不直接发送），用户可以先改两个字。
 */
import { BrandMark } from "./icons";

const SCENES: { label: string; prompt: string }[] = [
  {
    label: "深度研究",
    prompt:
      "帮我深度研究一个行业问题，用至少 3 个可靠来源互相印证，最后输出一份带引用的 HTML 报告。",
  },
  { label: "HTML 报告", prompt: "围绕以下主题生成一份可以直接发布的 HTML 报告：" },
  { label: "资料成页", prompt: "把这些链接里的正文提取出来，整理成一个可以分享的资料页面：" },
  { label: "视频成片", prompt: "把这个主题做成一段带中文旁白和字幕的短视频：" },
  {
    label: "事实核查",
    prompt: "核对这个说法是否属实，列出你查到的来源，并标出互相矛盾的地方：",
  },
  { label: "部署上线", prompt: "把刚才生成的报告部署到服务器，并给我访问链接。" },
];

const SUGGESTS: { title: string; sub: string; prompt: string }[] = [
  {
    title: "看懂一个行业问题",
    sub: "用 3 个可靠来源互相印证，标出矛盾处",
    prompt: "用 3 个可靠来源看懂一个行业问题：2026 年 AI Agent 在企业里到底落地了哪些场景？",
  },
  {
    title: "做一张能力速查表",
    sub: "HTML 表格呈现，能直接发给同事",
    prompt: "做一张「网页研究智能体」的能力速查表，用 HTML 表格呈现，能直接发给同事。",
  },
  {
    title: "讲清执行过程",
    sub: "每一步动作与耗时是怎么被记录的",
    prompt: "一句话讲清楚：这个智能体的执行过程是怎么被观察和记录的？",
  },
  {
    title: "最近 30 天有变化吗",
    sub: "这个领域值得关注的新动向，整理成要点",
    prompt: "最近 30 天，网页研究/信息检索领域发生了哪些值得关注的变化？整理成要点。",
  },
];

export function Welcome({ onPick }: { onPick: (prompt: string) => void }) {
  return (
    <section className="pt-[clamp(28px,7vh,64px)] pb-5" aria-label="开始">
      <p className="flex items-center gap-2 text-secondary text-muted">
        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-signal">
          <BrandMark size={14} />
        </span>
        <span className="text-ink-2">网页研究智能体</span>
        <span className="h-3 w-px bg-line-strong" aria-hidden="true" />
        <span>集成栈 · Pi Runtime</span>
      </p>

      <h1 className="mt-3 text-headline font-semibold leading-tight text-ink">
        给一个主题，
        <br className="hidden sm:block" />
        拿回一份带来源的报告
      </h1>
      <p className="mt-2 max-w-[620px] text-body leading-relaxed text-muted">
        它会自己搜索、读完整页、把结论整理成可以直接发布的 HTML。每一步动作与耗时都在页面上实时可见，署名来源写进报告里。
      </p>

      <div className="mt-5">
        <p className="mb-1.5 text-label text-muted">场景</p>
        <div className="flex flex-wrap gap-1.5">
          {SCENES.map((scene) => (
            <button
              key={scene.label}
              type="button"
              onClick={() => onPick(scene.prompt)}
              className="h-[28px] rounded-md border border-line-strong bg-paper px-2.5 text-secondary text-ink-2 transition-colors duration-150 ease-standard hover:border-signal hover:text-signal"
            >
              {scene.label}
            </button>
          ))}
        </div>
      </div>

      {/* 建议条不用卡片：旧页最后定型成"发丝线分隔的列表"——四条方框在空态里
          把留白吃掉了，读起来也更像广告位。hover 只动箭头和标题色。 */}
      <div className="mt-5 grid border-t border-line sm:grid-cols-2 sm:gap-x-7">
        {SUGGESTS.map((item, index) => (
          <button
            key={item.title}
            type="button"
            onClick={() => onPick(item.prompt)}
            className="group flex items-center gap-3 border-b border-line py-3 text-left transition-colors duration-150 ease-standard hover:border-line-strong"
          >
            <span className="shrink-0 pt-px font-mono text-label font-medium tracking-wide text-signal/85">
              {String(index + 1).padStart(2, "0")}
            </span>
            <span className="min-w-0 flex-1">
              <b className="block truncate text-secondary font-medium text-ink transition-colors duration-150 ease-standard group-hover:text-signal-deep">
                {item.title}
              </b>
              <small className="block truncate text-label text-muted">{item.sub}</small>
            </span>
            <span className="shrink-0 text-line-strong transition-[color,transform] duration-150 ease-standard group-hover:translate-x-0.5 group-hover:text-signal">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M9.5 6l6 6-6 6"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
