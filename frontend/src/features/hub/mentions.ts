/**
 * 输入框里的「引用」模型。
 *
 * 三种引用，三种落点——这是它们唯一且关键的区别，别混着看：
 *
 *  - **技能**：随消息发 `content_blocks`（`skill://<id>`）。服务端
 *    `addons/resolver.py::extract_message_skill_ids` 把它读成「这一轮点名要用
 *    这个技能」，与会话级 `addon_selection` 叠加生效。这是真功能。
 *  - **文件**：只有路径本身有意义。`content_blocks` 里的 file 块服务端**只存不读**
 *    （`tools/tool_results.py` 写明 resource_link「只保存引用，不主动去读」），真正
 *    生效的是正文末尾那行 `@file:<path>`——实测模型会照着路径去打开文件并复述内容。
 *    块的作用是**回显**：历史气泡里能看出这条消息引用过哪些文件。
 *  - **连接器**：不是内容，是本轮的 `connector_ids`（旧页也是这么发的）。
 *
 * 为什么是「引用条」而不是旧页那种行内芯片：旧页输入框是 contenteditable，
 * 芯片是插在文本流里的 `<span contenteditable=false>`，代价是整套光标/输入法/
 * 退格逻辑都得自己写（旧页为此写了近千行）。新版是 textarea，行内芯片做不了。
 * 换回 contenteditable 会把中文输入法的既得利益全丢掉，不值得。这里改成芯片
 * 排在输入框下方一行、正文保持纯文本：语义等价，插入点从行内挪到了行下。
 */
import type { ResourceBlock } from "./types";

export type RefChip =
  | { kind: "skill"; id: string; name: string }
  | { kind: "file"; path: string; name: string; isDir: boolean }
  | { kind: "connector"; id: string; name: string };

/** 同一个目标只允许存在一个芯片：技能看 id，文件看路径。 */
export function chipKey(chip: RefChip): string {
  if (chip.kind === "skill") return `skill:${chip.id}`;
  if (chip.kind === "connector") return `connector:${chip.id}`;
  return `file:${chip.path}`;
}

export function addChip(list: RefChip[], chip: RefChip): RefChip[] {
  const key = chipKey(chip);
  if (list.some((item) => chipKey(item) === key)) return list;
  return [...list, chip];
}

export function removeChip(list: RefChip[], key: string): RefChip[] {
  return list.filter((item) => chipKey(item) !== key);
}

/**
 * 结构化内容块。只放技能与文件——连接器不是内容，混进来会让历史里
 * 出现一个"引用了一个连接器"的怪东西。
 */
export function chipBlocks(chips: RefChip[]): ResourceBlock[] {
  const blocks: ResourceBlock[] = [];
  for (const chip of chips) {
    if (chip.kind === "skill") {
      const uri = `skill://${chip.id}`;
      blocks.push({
        type: "resource_link",
        name: uri,
        uri,
        title: chip.name,
        _meta: {
          mentionType: "skill",
          type: "skill",
          displayText: chip.name,
          skillId: chip.id,
          trigger: "at",
        },
      });
    } else if (chip.kind === "file") {
      const uri = `file://${chip.path}`;
      blocks.push({
        type: "resource_link",
        name: uri,
        uri,
        title: chip.name,
        _meta: {
          mentionType: "file",
          type: "file",
          displayText: chip.name,
          path: chip.path,
          kind: chip.isDir ? "dir" : "file",
        },
      });
    }
  }
  return blocks;
}

/**
 * 附在消息正文末尾的文件标记。空串表示这条消息没引用文件。
 *
 * 拼在末尾而不是原文里：芯片本来就脱离了光标位置，硬塞回某个句子中间只会
 * 让正文读起来更怪。`@file:` 这个前缀与旧页、与服务端兜底扫描保持一致。
 */
export function fileMarkerLine(chips: RefChip[]): string {
  const files = chips
    .filter((chip): chip is Extract<RefChip, { kind: "file" }> => chip.kind === "file")
    .map((chip) => `@file:${chip.path}`);
  return files.join(" ");
}

export function connectorIds(chips: RefChip[]): string[] {
  return chips
    .filter((chip): chip is Extract<RefChip, { kind: "connector" }> => chip.kind === "connector")
    .map((chip) => chip.id);
}

/**
 * 渲染历史消息时去掉末尾那行 `@file:` 标记。
 *
 * 标记是给模型看的（它只读正文），气泡里已经有文件芯片了，再把
 * `@file:docs/a.md` 原样打一遍是重复。只削末尾这一行，正文里用户自己写的
 * `@file:` 不动——虽然几乎不会有人这么写，但削错了就是在改用户的话。
 */
export function stripFileMarkers(text: string): string {
  return text.replace(/(\n\s*)?@file:[^\n]*$/, "").trimEnd();
}

/** 芯片上的小标题：`2026-11-13.md` 这种长文件名要能截断显示。 */
export function chipLabel(chip: RefChip): string {
  return chip.name || ("id" in chip ? chip.id : "未命名");
}

/**
 * 光标前是否正处在一个 `@` 提及里。
 *
 * 只在行首或空白之后认 `@`——否则 `user@example.com` 打到一半就会弹菜单。
 * `\u200b` 是旧页留下的零宽占位，一并当成空白边界。
 */
export function mentionQuery(
  text: string,
  caret: number,
): { start: number; end: number; query: string } | null {
  const head = text.slice(0, Math.max(0, Math.min(caret, text.length)));
  const hit = /(^|[\s\u200b])@([^\s@]*)$/.exec(head);
  if (!hit) return null;
  const query = hit[2] ?? "";
  return { start: head.length - query.length - 1, end: head.length, query };
}
