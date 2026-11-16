import { useCallback, useEffect, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { useToast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { consoleApi } from "./api";
import { num, when } from "./format";
import {
  DrawerHead,
  Field,
  Grid,
  inputCls,
  Loading,
  Panel,
  RowMenu,
  selectCls,
  TableWrap,
  Tag,
  tdCls,
  tdNumCls,
  textareaCls,
  thCls,
  thNumCls,
  Toolbar,
  useAsk,
} from "./primitives";
import type { PlazaCategory, PlazaData, PlazaItem } from "./types";

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  pending: "待审核",
  published: "已上架",
  rejected: "未通过",
  offline: "已下架",
};

const STATUS_TONE: Record<string, "neutral" | "warn" | "ok" | "danger"> = {
  draft: "neutral",
  pending: "warn",
  published: "ok",
  rejected: "danger",
  offline: "neutral",
};

const CATEGORY_FALLBACK: PlazaCategory[] = [
  { id: "writing", name: "写作" },
  { id: "coding", name: "研发" },
  { id: "research", name: "研究" },
  { id: "data", name: "数据" },
  { id: "ops", name: "运维" },
  { id: "marketing", name: "市场" },
  { id: "product", name: "产品" },
  { id: "general", name: "通用" },
];

/**
 * Bot 广场。
 *
 * 管三件事：审核投稿、上架官方 Bot、调运营位。条目是**发布时的快照**——
 * 这里改的是广场上那份，不会反向写回投稿人的机器人。
 *
 * `install_rows` 是安装登记表的实际行数，`install_count` 是累计计数。
 * 两个数对不上说明有人绕过接口删了登记行，属于该查的异常，所以并排显示。
 */
export function PlazaTab() {
  const toast = useToast();
  const ask = useAsk();
  const [data, setData] = useState<PlazaData | null>(null);
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<PlazaItem | "new" | null>(null);

  const load = useCallback(async (nextStatus: string, nextQuery: string) => {
    setLoading(true);
    setError("");
    try {
      const next = await consoleApi.plaza(nextStatus, nextQuery);
      setData(next);
    } catch (err) {
      setError(errorMessage(err));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(status, query);
  }, [load, query, status]);

  const categoryName = (id: string) => {
    const list = data?.categories?.length ? data.categories : CATEGORY_FALLBACK;
    return list.find((c) => c.id === id)?.name ?? "通用";
  };

  const act = async (item: PlazaItem, action: string) => {
    if (action === "delete") {
      const ok = await ask.confirm({
        title: `删除「${item.name}」的广场条目？`,
        tone: "danger",
        confirmText: "删除",
        body: "已启用它的用户不受影响——他们拿到的是自己的机器人副本。",
      });
      if (!ok) return;
      try {
        await consoleApi.deletePlazaItem(item.item_id);
        toast("已删除");
        await load(status, query);
      } catch (err) {
        toast(errorMessage(err), "error");
      }
      return;
    }

    let payload: Record<string, unknown> | null = null;
    if (action === "publish") {
      payload = { status: "published", review_note: item.review_note || "审核通过" };
    } else if (action === "reject") {
      // 驳回理由不是形式主义：投稿人只能靠它知道该改哪
      const note = await ask.ask({
        title: `驳回「${item.name}」`,
        confirmText: "驳回",
        tone: "danger",
        body: "理由会显示给投稿人。",
        input: { label: "驳回理由", placeholder: "例如：人设写得太空，先补三条示例问法", required: true },
      });
      if (note === null) return;
      payload = { status: "rejected", review_note: note };
    } else if (action === "offline") {
      payload = { status: "offline", review_note: "管理员下架" };
    } else if (action === "feature") {
      payload = { featured: !item.featured };
    }
    if (!payload) return;
    try {
      await consoleApi.updatePlazaItem(item.item_id, payload);
      toast("已更新");
      await load(status, query);
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const items = data?.items ?? [];
  const counts = data?.counts ?? {};

  return (
    <>
      <Panel
        title="Bot 广场"
        hint="审核投稿、上架官方 Bot、调推荐位。条目是发布时的快照，改这里不会反向影响投稿人的机器人"
        actions={
          <Button variant="primary" onClick={() => setEditing("new")}>
            上架官方 Bot
          </Button>
        }
      >
        <Toolbar>
          <Button onClick={() => void load(status, query)}>刷新</Button>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            aria-label="按状态筛选"
            className={selectCls}
          >
            <option value="">全部状态</option>
            {(Object.keys(STATUS_LABEL) as string[]).map((s) => (
              <option key={s} value={s}>
                {STATUS_LABEL[s]}
              </option>
            ))}
          </select>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜名称 / handle / 标签"
            aria-label="搜索广场条目"
            className={cn(inputCls, "w-[220px]")}
            autoComplete="off"
          />
          <span className="text-label text-muted">
            {items.length} 条 · 待审核 {num(counts.pending)} · 已上架 {num(counts.published)}
          </span>
        </Toolbar>

        {error ? (
          <div className="flex flex-col items-start gap-3 py-6">
            <p className="text-body text-ink-2">{error}</p>
            <Button onClick={() => void load(status, query)}>重试</Button>
          </div>
        ) : loading ? (
          <Loading rows={5} label="正在加载广场" />
        ) : items.length === 0 ? (
          <div className="py-6 text-body leading-relaxed text-muted">
            <p>没有条目。</p>
            <p className="mt-1">用户投稿后会出现在这里，也可以点「上架官方 Bot」直接发布。</p>
          </div>
        ) : (
          <TableWrap>
            <table className="w-full min-w-[1020px] border-collapse text-body">
              <thead>
                <tr>
                  <th className={thCls}>Bot</th>
                  <th className={thCls}>分类</th>
                  <th className={thCls}>来源</th>
                  <th className={thCls}>状态</th>
                  <th className={thCls}>可见</th>
                  <th className={thNumCls}>启用</th>
                  <th className={thNumCls}>更新</th>
                  <th className={thCls}>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.item_id} className="hover:bg-rail/70">
                    <td className={tdCls}>
                      <div className="flex items-center gap-1.5">
                        <span aria-hidden="true">{item.icon || "🤖"}</span>
                        <span className="font-medium text-ink">{item.name}</span>
                      </div>
                      <div className="text-label text-muted">
                        @{item.handle || ""}
                        {item.title ? ` · ${item.title}` : ""}
                      </div>
                    </td>
                    <td className={cn(tdCls, "text-secondary text-muted")}>
                      {categoryName(item.category)}
                    </td>
                    <td className={tdCls}>
                      {item.source_bot_id ? (
                        <>
                          <Tag>投稿</Tag>
                          <div className="mt-0.5 text-label text-muted">
                            {item.publisher_tenant}
                          </div>
                        </>
                      ) : (
                        <Tag>官方</Tag>
                      )}
                    </td>
                    <td className={tdCls}>
                      <Badge tone={STATUS_TONE[item.status] || "neutral"}>
                        {STATUS_LABEL[item.status] || item.status}
                      </Badge>
                      {item.review_note ? (
                        <div className="mt-1 max-w-[190px] whitespace-normal text-label leading-relaxed text-muted">
                          {item.review_note}
                        </div>
                      ) : null}
                    </td>
                    <td className={tdCls}>
                      <Tag>{item.scope === "public" ? "全平台" : "本企业"}</Tag>
                      {item.featured ? <Tag>推荐</Tag> : null}
                      {item.position ? <Tag>#{item.position}</Tag> : null}
                    </td>
                    <td className={tdNumCls}>
                      {num(item.install_count)}
                      <div className="text-label text-muted">登记 {num(item.install_rows)}</div>
                    </td>
                    <td className={cn(tdNumCls, "text-secondary text-muted")}>
                      {when(item.updated_at)}
                    </td>
                    <td className={cn(tdCls, "whitespace-nowrap")}>
                      <div className="flex items-center gap-1">
                        <Button size="sm" onClick={() => setEditing(item)}>
                          编辑
                        </Button>
                        {item.status === "pending" || item.status === "published" ? (
                          item.status === "pending" ? (
                            <Button size="sm" variant="primary" onClick={() => void act(item, "publish")}>
                              通过
                            </Button>
                          ) : null
                        ) : (
                          <Button size="sm" variant="primary" onClick={() => void act(item, "publish")}>
                            上架
                          </Button>
                        )}
                        <RowMenu
                          label={`${item.name} 的更多操作`}
                          items={[
                            ...(item.status === "pending"
                              ? [{ label: "驳回投稿", onSelect: () => void act(item, "reject") }]
                              : []),
                            ...(item.status === "published"
                              ? [
                                  { label: "下架", onSelect: () => void act(item, "offline") },
                                  {
                                    label: item.featured ? "取消推荐" : "设为推荐",
                                    onSelect: () => void act(item, "feature"),
                                  },
                                ]
                              : []),
                            { label: "删除条目", danger: true, onSelect: () => void act(item, "delete") },
                          ]}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Panel>

      {editing ? (
        <PlazaEditor
          item={editing === "new" ? null : editing}
          categories={data?.categories?.length ? data.categories : CATEGORY_FALLBACK}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load(status, query);
          }}
        />
      ) : null}
    </>
  );
}

function PlazaEditor({
  item,
  categories,
  onClose,
  onSaved,
}: {
  item: PlazaItem | null;
  categories: PlazaCategory[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const isNew = !item;
  const [form, setForm] = useState({
    name: item?.name ?? "",
    handle: item?.handle ?? "",
    title: item?.title ?? "",
    description: item?.description ?? "",
    category: item?.category ?? "general",
    scope: item?.scope ?? "tenant",
    status: item?.status ?? "published",
    tags: (item?.tags || []).join(", "),
    featured: Boolean(item?.featured),
    position: String(item?.position || 0),
    readme: item?.readme ?? "",
    starters: (item?.starters || []).join("\n"),
    soul: item?.soul ?? "",
    model: item?.model ?? "",
    review_note: item?.review_note ?? "",
    source_bot_id: "",
  });
  const [busy, setBusy] = useState(false);

  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const save = async () => {
    if (!form.name.trim()) {
      toast("名称不能为空", "error");
      return;
    }
    const body: Record<string, unknown> = {
      name: form.name.trim(),
      handle: form.handle.trim(),
      title: form.title.trim(),
      description: form.description.trim(),
      soul: form.soul,
      model: form.model.trim(),
      category: form.category,
      tags: form.tags
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, 8),
      readme: form.readme,
      starters: form.starters
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, 6),
      scope: form.scope,
      status: form.status,
      featured: form.featured,
      position: Number(form.position || 0) || 0,
      review_note: form.review_note.trim(),
    };
    setBusy(true);
    try {
      if (item) {
        await consoleApi.updatePlazaItem(item.item_id, body);
        toast("已保存");
      } else {
        if (form.source_bot_id.trim()) body.source_bot_id = form.source_bot_id.trim();
        await consoleApi.createPlazaItem(body);
        toast("已上架");
      }
      onSaved();
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer
      open
      onClose={onClose}
      title={isNew ? "上架官方 Bot" : `编辑：${item?.name}`}
      width="wide"
      header={
        <DrawerHead
          title={isNew ? "上架官方 Bot" : `编辑：${item?.name}`}
          hint={isNew ? "保存后按所选状态立刻生效" : "改状态等于做一次审核动作"}
          onClose={onClose}
        />
      }
    >
      <Grid>
        <Field label="名称" htmlFor="pl-name" note="必填，最多 40 字">
          <input
            id="pl-name"
            maxLength={40}
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="handle" htmlFor="pl-handle" note="@ 用；留空按名称生成">
          <input
            id="pl-handle"
            maxLength={32}
            value={form.handle}
            onChange={(e) => set("handle", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="一句话职责" htmlFor="pl-title" span2>
          <input
            id="pl-title"
            maxLength={60}
            value={form.title}
            onChange={(e) => set("title", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="描述（卡片第二行）" htmlFor="pl-desc" span2>
          <textarea
            id="pl-desc"
            maxLength={400}
            rows={2}
            value={form.description}
            onChange={(e) => set("description", e.target.value)}
            className={textareaCls}
          />
        </Field>
        <Field label="分类" htmlFor="pl-cat">
          <select
            id="pl-cat"
            value={form.category}
            onChange={(e) => set("category", e.target.value)}
            className={cn(selectCls, "w-full")}
          >
            {categories.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="可见范围" htmlFor="pl-scope">
          <select
            id="pl-scope"
            value={form.scope}
            onChange={(e) => set("scope", e.target.value)}
            className={cn(selectCls, "w-full")}
          >
            <option value="tenant">本企业</option>
            <option value="public">全平台（只有超管能给）</option>
          </select>
        </Field>
        <Field label="状态" htmlFor="pl-status">
          <select
            id="pl-status"
            value={form.status}
            onChange={(e) => set("status", e.target.value)}
            className={cn(selectCls, "w-full")}
          >
            {(Object.keys(STATUS_LABEL) as string[]).map((s) => (
              <option key={s} value={s}>
                {STATUS_LABEL[s]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="标签（逗号分隔，最多 8 个）" htmlFor="pl-tags">
          <input
            id="pl-tags"
            value={form.tags}
            onChange={(e) => set("tags", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="推荐位" htmlFor="pl-featured">
          <select
            id="pl-featured"
            value={form.featured ? "1" : "0"}
            onChange={(e) => set("featured", e.target.value === "1")}
            className={cn(selectCls, "w-full")}
          >
            <option value="0">不推荐</option>
            <option value="1">推荐（排在前面）</option>
          </select>
        </Field>
        <Field label="排序位（小的在前）" htmlFor="pl-pos">
          <input
            id="pl-pos"
            value={form.position}
            onChange={(e) => set("position", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="详细介绍" htmlFor="pl-readme" span2>
          <textarea
            id="pl-readme"
            rows={4}
            maxLength={20000}
            value={form.readme}
            onChange={(e) => set("readme", e.target.value)}
            className={textareaCls}
          />
        </Field>
        <Field label="推荐开场白（每行一条）" htmlFor="pl-starters" span2 note="最多 6 条">
          <textarea
            id="pl-starters"
            rows={3}
            value={form.starters}
            onChange={(e) => set("starters", e.target.value)}
            className={textareaCls}
          />
        </Field>
        <Field label="人设（SOUL）" htmlFor="pl-soul" span2>
          <textarea
            id="pl-soul"
            rows={5}
            maxLength={12000}
            value={form.soul}
            onChange={(e) => set("soul", e.target.value)}
            className={textareaCls}
          />
        </Field>
        <Field label="模型" htmlFor="pl-model" note="留空跟随中台默认">
          <input
            id="pl-model"
            value={form.model}
            onChange={(e) => set("model", e.target.value)}
            className={inputCls}
          />
        </Field>
        <Field label="审核意见（给投稿人看）" htmlFor="pl-note">
          <input
            id="pl-note"
            maxLength={300}
            value={form.review_note}
            onChange={(e) => set("review_note", e.target.value)}
            className={inputCls}
          />
        </Field>
        {isNew ? (
          <Field
            label="从已有机器人复制（可选）"
            htmlFor="pl-source"
            span2
            note="填了它就复制那个机器人的人设/技能/连接器；空白字段才会被覆盖。"
          >
            <input
              id="pl-source"
              value={form.source_bot_id}
              placeholder="bot_xxxxxxxxxxxx"
              onChange={(e) => set("source_bot_id", e.target.value)}
              className={cn(inputCls, "font-mono text-label")}
            />
          </Field>
        ) : null}
      </Grid>

      <div className="mt-4 flex items-center gap-3">
        <Button variant="primary" onClick={() => void save()} disabled={busy}>
          保存
        </Button>
        <Button onClick={onClose}>取消</Button>
        {item ? (
          <span className="text-label text-muted">
            累计启用 {num(item.install_count)} 次（登记 {num(item.install_rows)} 行）
          </span>
        ) : null}
      </div>
    </Drawer>
  );
}
