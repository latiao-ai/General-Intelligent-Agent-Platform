/**
 * 极简 Markdown → React。
 *
 * 为什么不引 react-markdown：这个前端的依赖只有 react / react-dom（见 package.json），
 * 上一套 remark/rehype 插件链会把产物体积翻好几倍，而聊天场景只需要「块级 + 行内」两层。
 *
 * 之前气泡是 `whitespace-pre-wrap` + 纯文本，模型输出里的 `**加粗**`、`| 表格 |`、
 * `` `代码` `` 会原样露出来——这不是模型写得不好，是界面没把 Markdown 当语法看。
 *
 * 安全边界：全部产出 React 元素，绝不 dangerouslySetInnerHTML。链接只放行
 * http(s) / mailto，其余（含 `javascript:`）退化成纯文本，不给注入留口子。
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/* ---------------- 行内 ---------------- */

/**
 * 顺序即优先级：同一位置上，靠前的分支先赢。
 * 行内代码排第一，因为 `` `**x**` `` 里的星号不该被当成加粗。
 *
 * 存成源码字符串而不是 RegExp：`inline` 会为了处理嵌套粗体递归调用自己，
 * 共用一个带 `g` 的 RegExp 会互相踩 `lastIndex`，直接死循环。
 */
const INLINE_SRC = [
  "(`[^`\\n]+`)", // 1 行内代码
  "(\\*\\*[^\\n]+?\\*\\*)", // 2 粗体
  "(__[^\\n]+?__)", // 3 粗体
  "(~~[^~\\n]+?~~)", // 4 删除线
  "(\\*[^*\\n]+?\\*)", // 5 斜体
  "(_[^_\\n]+?_)", // 6 斜体
  "(!?\\[[^\\]\\n]*\\]\\([^)\\s]+\\))", // 7 链接 / 图片
].join("|");

function safeHref(raw: string): string | null {
  const url = raw.trim();
  if (/^https?:\/\//i.test(url) || /^mailto:/i.test(url)) return url;
  return null;
}

const CODE_CLS = "rounded bg-sunken px-1 py-px font-mono text-[12px] text-ink-2";

function inline(src: string, base: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = new RegExp(INLINE_SRC, "g");
  let last = 0;
  let seq = 0;

  for (let m = re.exec(src); m; m = re.exec(src)) {
    if (m.index > last) out.push(src.slice(last, m.index));
    const tok = m[0];
    const key = `${base}-${seq++}`;

    if (tok.startsWith("`")) {
      out.push(
        <code key={key} className={CODE_CLS}>
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith("**") || tok.startsWith("__")) {
      out.push(
        <strong key={key} className="font-semibold text-ink">
          {inline(tok.slice(2, -2), key)}
        </strong>,
      );
    } else if (tok.startsWith("~~")) {
      out.push(
        <del key={key} className="text-muted">
          {inline(tok.slice(2, -2), key)}
        </del>,
      );
    } else if (tok.startsWith("*") || tok.startsWith("_")) {
      out.push(
        <em key={key} className="italic">
          {inline(tok.slice(1, -1), key)}
        </em>,
      );
    } else {
      // [文字](地址) 或 ![替代文字](地址)
      const link = /^!?\[([^\]]*)\]\(([^)\s]+)\)$/.exec(tok);
      const href = link ? safeHref(link[2] ?? "") : null;
      const label = link?.[1] || link?.[2] || tok;
      out.push(
        href ? (
          <a
            key={key}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-signal underline underline-offset-2 transition-colors duration-150 ease-standard hover:text-signal-deep"
          >
            {label}
          </a>
        ) : (
          <span key={key}>{label}</span>
        ),
      );
    }
    last = m.index + tok.length;
  }

  if (last < src.length) out.push(src.slice(last));
  return out;
}

/* ---------------- 块级 ---------------- */

const UL = /^[-*+]\s+/;
const OL = /^\d{1,3}[.)]\s+/;
/** 列表标记的统一入口：一段列表里常混着无序子项，不能只认一种。 */
const LIST_MARK = /^([-*+]|\d{1,3}[.)])\s+/;
const DIVIDER = /^(?:-{3,}|\*{3,}|_{3,})$/;
const TABLE_DIVIDER = /^\|?[\s:|-]+\|?$/;

function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((cell) => cell.trim());
}

function isTableRow(line: string): boolean {
  return line.includes("|") && !line.startsWith(">") && !line.startsWith("```");
}

function isTableDivider(line: string | undefined): boolean {
  if (!line) return false;
  const s = line.trim();
  return s.includes("|") && s.includes("-") && TABLE_DIVIDER.test(s);
}

function alignOf(cell: string): "left" | "center" | "right" | "" {
  const left = cell.startsWith(":");
  const right = cell.endsWith(":");
  if (left && right) return "center";
  if (right) return "right";
  if (left) return "left";
  return "";
}

const ALIGN: Record<string, string> = {
  left: "text-left",
  center: "text-center",
  right: "text-right",
  "": "text-left",
};

function isBlockStart(line: string): boolean {
  return (
    line.startsWith("```") ||
    line.startsWith("#") ||
    line.startsWith(">") ||
    DIVIDER.test(line) ||
    UL.test(line) ||
    OL.test(line) ||
    isTableRow(line)
  );
}

/**
 * 中文换行后不该补空格，英文单词之间该补。
 * 判据只看衔接的两个字符是不是 ASCII 词字符——够用，不做语言检测。
 */
function joinLines(lines: string[]): string {
  return lines.reduce((acc, cur) => {
    if (!acc) return cur;
    const a = acc.slice(-1);
    const b = cur.slice(0, 1);
    const needSpace = /[A-Za-z0-9,.;:!?)\]}"']/.test(a) && /[A-Za-z0-9("'[]/.test(b);
    return acc + (needSpace ? " " : "") + cur;
  }, "");
}

function renderBlocks(lines: string[], base: string): ReactNode[] {
  const out: ReactNode[] = [];
  let i = 0;
  let seq = 0;
  const key = () => `${base}-${seq++}`;

  while (i < lines.length) {
    const line = (lines[i] ?? "").trim();

    if (!line) {
      i += 1;
      continue;
    }

    /* ---- 代码块：围栏内一切原样 ---- */
    if (line.startsWith("```")) {
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !(lines[i] ?? "").trim().startsWith("```")) {
        buf.push(lines[i] ?? "");
        i += 1;
      }
      i += 1;
      out.push(
        <pre
          key={key()}
          className="my-1.5 overflow-x-auto rounded-md border border-line bg-rail p-2 font-mono text-[12px] leading-relaxed text-ink-2"
        >
          <code>{buf.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    /* ---- 分隔线 ---- */
    if (DIVIDER.test(line)) {
      out.push(<hr key={key()} className="my-2.5 border-0 border-t border-line" />);
      i += 1;
      continue;
    }

    /* ---- 标题：气泡里最大只到 title 级，不让 h1 撑破版面 ---- */
    const head = /^(#{1,6})\s+(.+)$/.exec(line);
    if (head) {
      const level = (head[1] ?? "#").length;
      const cls =
        level <= 2 ? "mt-3 mb-1 text-title font-semibold" : "mt-2.5 mb-1 text-body font-semibold";
      const content = inline(head[2] ?? "", key());
      out.push(
        level === 1 ? (
          <h3 key={key()} className={cls}>
            {content}
          </h3>
        ) : level === 2 ? (
          <h4 key={key()} className={cls}>
            {content}
          </h4>
        ) : (
          <h5 key={key()} className={cls}>
            {content}
          </h5>
        ),
      );
      i += 1;
      continue;
    }

    /* ---- 表格：表头 + 分隔行 + 数据行 ---- */
    if (isTableRow(line) && isTableDivider(lines[i + 1])) {
      const header = splitRow(line);
      const aligns = splitRow((lines[i + 1] ?? "").trim()).map(alignOf);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length) {
        const next = (lines[i] ?? "").trim();
        if (!next || !isTableRow(next)) break;
        rows.push(splitRow(next));
        i += 1;
      }
      const tk = key();
      out.push(
        <div key={tk} className="my-2 overflow-x-auto rounded-md border border-line">
          <table className="w-full border-collapse text-secondary">
            <thead>
              <tr className="bg-rail">
                {header.map((cell, j) => (
                  <th
                    key={j}
                    className={cn(
                      "border-b border-line px-2.5 py-1.5 font-semibold text-ink",
                      ALIGN[aligns[j] ?? ""],
                    )}
                  >
                    {inline(cell, `${tk}-h${j}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr key={ri} className="border-b border-line last:border-b-0">
                  {header.map((_, j) => (
                    <td
                      key={j}
                      className={cn("px-2.5 py-1.5 align-top text-ink-2", ALIGN[aligns[j] ?? ""])}
                    >
                      {inline(row[j] ?? "", `${tk}-r${ri}c${j}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    /* ---- 引用 ---- */
    if (line.startsWith(">")) {
      const buf: string[] = [];
      while (i < lines.length && (lines[i] ?? "").trim().startsWith(">")) {
        buf.push((lines[i] ?? "").trim().replace(/^>\s?/, ""));
        i += 1;
      }
      out.push(
        <blockquote
          key={key()}
          className="my-1.5 border-l-2 border-line-strong pl-2.5 text-muted"
        >
          {renderBlocks(buf, key())}
        </blockquote>,
      );
      continue;
    }

    /* ---- 列表 ----
     * 无序和有序走同一段逻辑：出现顺序不定的混排（`1.` 底下挂 `-`）很常见。
     * 层级只看缩进差——2~4 空格算一层，再多算两层，不按标记类型推。
     * 深度用 padding 表达，不再展开成嵌套 ul：聊天气泡里两层就是极限了。
     */
    if (UL.test(line) || OL.test(line)) {
      const ordered = OL.test(line);
      const items: { text: string; depth: number }[] = [];
      let baseIndent = -1;

      while (i < lines.length) {
        const raw = lines[i] ?? "";
        const t = raw.trim();
        if (!t) break;
        const indent = raw.length - raw.trimStart().length;
        if (LIST_MARK.test(t)) {
          if (baseIndent < 0) baseIndent = indent;
          const delta = indent - baseIndent;
          items.push({
            text: t.replace(LIST_MARK, ""),
            depth: delta <= 0 ? 0 : delta <= 4 ? 1 : 2,
          });
        } else if (items.length) {
          // 硬折行的长句并回上一条，不新起一项
          items[items.length - 1]!.text += ` ${t}`;
        } else {
          break;
        }
        i += 1;
      }

      // 子项不放平在同一层 li 里：有序列表里混挂无序子项时，父级编号会被子项
      // 顶掉（1、2、3、4、7）。折成真正的嵌套 ul，序号和语义一起保住。
      // 深度上限两层，聊天气泡再深也读不动。
      const tree: { text: string; mid: { text: string; leaf: string[] }[] }[] = [];
      for (const item of items) {
        if (item.depth === 0 || !tree.length) {
          tree.push({ text: item.text, mid: [] });
          continue;
        }
        const top = tree[tree.length - 1]!;
        if (item.depth === 1 || !top.mid.length) {
          top.mid.push({ text: item.text, leaf: [] });
          continue;
        }
        top.mid[top.mid.length - 1]!.leaf.push(item.text);
      }

      const lk = key();
      const lis = tree.map((node, j) => (
        <li key={j} className="text-ink">
          {inline(node.text, `${lk}-${j}`)}
          {node.mid.length ? (
            <ul className="mt-0.5 list-[circle] space-y-0.5 pl-4 marker:text-muted">
              {node.mid.map((sub, k) => (
                <li key={k}>
                  {inline(sub.text, `${lk}-${j}-${k}`)}
                  {sub.leaf.length ? (
                    <ul className="mt-0.5 list-[square] space-y-0.5 pl-4 marker:text-muted">
                      {sub.leaf.map((leaf, n) => (
                        <li key={n}>{inline(leaf, `${lk}-${j}-${k}-${n}`)}</li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </li>
      ));
      out.push(
        ordered ? (
          <ol key={lk} className="my-1 list-decimal space-y-0.5 pl-5 marker:text-muted">
            {lis}
          </ol>
        ) : (
          <ul key={lk} className="my-1 list-disc space-y-0.5 pl-5 marker:text-muted">
            {lis}
          </ul>
        ),
      );
      continue;
    }

    /* ---- 段落：连续的普通行合成一段 ---- */
    const para: string[] = [line];
    i += 1;
    while (i < lines.length) {
      const next = (lines[i] ?? "").trim();
      if (!next || isBlockStart(next)) break;
      para.push(next);
      i += 1;
    }
    out.push(
      <p key={key()} className="my-1 first:mt-0 last:mb-0">
        {inline(joinLines(para), key())}
      </p>,
    );
  }

  return out;
}

/* ---------------- 导出 ---------------- */

/**
 * 渲染一段模型输出。
 *
 * `whitespace-normal` 是必须的：外层气泡带 `whitespace-pre-wrap`（给用户原样输入用），
 * 到这里换行已经被解析成块了，再保留原始空白会把缩进放大。
 */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const src = String(text ?? "");
  if (!src.trim()) return null;
  return (
    <div className={cn("whitespace-normal", className)}>
      {renderBlocks(src.replace(/\r\n?/g, "\n").split("\n"), "md")}
    </div>
  );
}
