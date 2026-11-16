/**
 * 发布到 Bot 广场。
 *
 * 广场上展示的是**发布那一刻的快照**：之后改这个机器人，广场上的版本不会
 * 跟着变；想更新就重新发布（版本 +1，重新进审核）。这句话必须写在弹窗里，
 * 否则用户改完人设会以为广场自动同步了。
 *
 * 撤回是"下架"，不是"删除"：已启用它的用户不受影响。文案里说清楚，
 * 免得有人以为撤回会把人家的副本也收走。
 */
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Field, Select, TextArea, TextInput } from "./primitives";
import type { BotSummary, CategoryOption, PlazaItem } from "./types";

export function PublishDialog({
  open,
  bot,
  item,
  categories,
  saving,
  onClose,
  onSubmit,
  onWithdraw,
}: {
  open: boolean;
  bot: BotSummary | null;
  item: PlazaItem | undefined;
  categories: CategoryOption[];
  saving: boolean;
  onClose: () => void;
  onSubmit: (body: {
    category: string;
    tags: string[];
    readme: string;
    starters: string[];
  }) => Promise<boolean>;
  onWithdraw: () => Promise<void>;
}) {
  const [category, setCategory] = useState("general");
  const [readme, setReadme] = useState("");
  const [tags, setTags] = useState("");
  const [starters, setStarters] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setCategory(item?.category ?? "general");
    setReadme(item?.readme ?? "");
    setTags((item?.tags ?? []).join(", "));
    setStarters((item?.starters ?? []).join("\n"));
    setError(null);
  }, [open, item]);

  const updating = Boolean(item?.item_id);
  const canWithdraw = Boolean(item?.item_id) && ["pending", "published"].includes(String(item?.status));

  const submit = async () => {
    setError(null);
    const ok = await onSubmit({
      category,
      tags: tags
        .split(/[,，\s]+/)
        .map((part) => part.trim())
        .filter(Boolean)
        .slice(0, 8),
      readme,
      starters: starters
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .slice(0, 6),
    });
    if (!ok) setError("提交失败，请查看页面提示");
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={updating ? "更新广场版本" : "发布到 Bot 广场"}
      className="max-w-[620px]"
    >
      <div className="flex flex-col gap-3.5">
        <p className="rounded-md bg-rail px-2.5 py-1.5 text-label leading-relaxed text-muted">
          广场上展示的是发布这一刻的快照：之后你再改这个机器人，广场上的版本不会跟着变；
          想更新就重新发布一次（版本 +1，重新进审核）。
        </p>

        <Field label="分类" htmlFor="pz-category" tip="分类决定它出现在广场的哪个筛选里。">
          <Select
            id="pz-category"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
          >
            {categories.map((option) => (
              <option key={option.id} value={option.id}>
                {option.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="详细介绍（可选）"
          htmlFor="pz-readme"
          tip="写清「什么时候该找它」「它会怎么干」「有什么边界」，比堆形容词有用。"
        >
          <TextArea
            id="pz-readme"
            value={readme}
            maxLength={20000}
            className="min-h-[96px]"
            placeholder="例：先给你三个候选标题，再按你选的那个写全文。不要泛泛而谈，不知道就说不确定。"
            onChange={(event) => setReadme(event.target.value)}
          />
        </Field>

        <Field label="标签（逗号或空格分隔，最多 8 个）" htmlFor="pz-tags">
          <TextInput
            id="pz-tags"
            value={tags}
            maxLength={160}
            placeholder="例：代码审查, 并发, 上线把关"
            onChange={(event) => setTags(event.target.value)}
          />
        </Field>

        <Field
          label="推荐开场白（每行一条，最多 6 条）"
          htmlFor="pz-starters"
          tip="广场详情页会把它们画成按钮，用户点一下就带着这句话开始对话。"
        >
          <TextArea
            id="pz-starters"
            value={starters}
            maxLength={1200}
            className="min-h-[72px]"
            placeholder={"审一下这个仓库的并发风险\n看看这次改动会不会拖慢首屏"}
            onChange={(event) => setStarters(event.target.value)}
          />
        </Field>

        {error ? (
          <p role="alert" className="text-label text-danger">
            {error}
          </p>
        ) : null}

        <p className="text-label text-muted">当前机器人：{bot?.name ?? "—"}</p>
      </div>

      <div className="sticky -bottom-4 -mx-5 -mb-4 mt-4 flex items-center gap-2 border-t border-line bg-paper px-5 py-3">
        {canWithdraw ? (
          <Button
            variant="danger"
            disabled={saving}
            onClick={() => {
              if (
                window.confirm("撤回后这条会从广场下架，已启用它的用户不受影响。继续吗？")
              ) {
                void onWithdraw();
              }
            }}
          >
            撤回投稿
          </Button>
        ) : null}
        <span className="flex-1" />
        <Button onClick={onClose} disabled={saving}>
          取消
        </Button>
        <Button variant="primary" onClick={() => void submit()} disabled={saving}>
          {saving ? "提交中…" : updating ? "重新提交审核" : "提交审核"}
        </Button>
      </div>
    </Dialog>
  );
}
