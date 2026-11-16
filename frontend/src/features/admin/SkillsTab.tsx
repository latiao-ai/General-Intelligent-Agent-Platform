import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Drawer";
import { useToast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { consoleApi } from "./api";
import {
  depthOf,
  iconFileToDataURL,
  nameFromSource,
  overviewFromSkill,
  parseSkillFrontmatter,
  readDirEntry,
  zipIndex,
  zipReadText,
} from "./lib/skillParse";
import {
  Code,
  DrawerHead,
  Field,
  Grid,
  inputCls,
  Loading,
  Panel,
  Switch,
  TableWrap,
  Tag,
  tdCls,
  tdNumCls,
  textareaCls,
  thCls,
  Toolbar,
  useAsk,
} from "./primitives";
import type { PlatformSkill, PolicyGroup } from "./types";

/**
 * 技能。
 *
 * 旧页让运营手填「简介」「分类」「默认安装」三项，全是能从别处推出来的：
 * 简介 = 概述第一句、分类 = 跟着已有分类走、默认安装 = 交给技能开关。
 * 这里把它们收掉，表单只剩真正需要人判断的四项：名称、概述、图标、正文。
 */
export function SkillsTab() {
  const toast = useToast();
  const ask = useAsk();
  const [items, setItems] = useState<PlatformSkill[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [groups, setGroups] = useState<PolicyGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [policyError, setPolicyError] = useState("");
  const [editing, setEditing] = useState<PlatformSkill | "new" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await consoleApi.platformSkills();
      setItems(data.items || []);
      setCategories(data.categories || []);
    } catch (err) {
      setError(errorMessage(err));
      setItems([]);
    } finally {
      setLoading(false);
    }
    try {
      const policies = await consoleApi.policies();
      setGroups(policies.tenants || []);
      setPolicyError("");
    } catch (err) {
      setPolicyError(errorMessage(err));
      setGroups([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (s: PlatformSkill) => {
    try {
      await consoleApi.toggleSkill(s.id, !s.enabled);
      toast(s.enabled ? "已停用，用户页上不再显示" : "已发布");
      await load();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const remove = async (s: PlatformSkill) => {
    const ok = await ask.confirm({
      title: "删除这个技能？",
      tone: "danger",
      confirmText: "删除",
      body: (
        <>
          <div className="font-mono text-label text-ink">{s.id}</div>
          <p className="mt-2">
            平台目录里删除后，用户技能页不再出现这一项。已经安装到自己工作区的副本不会被回收。
          </p>
        </>
      ),
    });
    if (!ok) return;
    try {
      await consoleApi.deleteSkill(s.id);
      toast("已删除");
      await load();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  const togglePolicy = async (policyId: string, enabled: boolean) => {
    try {
      await consoleApi.togglePolicy(policyId, enabled);
      toast("已更新");
      await load();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  };

  return (
    <>
      <Panel
        title="平台技能"
        hint="发布一次，所有用户的技能页都会多出这一项，由用户自己点「+」安装"
        actions={
          <Button variant="primary" onClick={() => setEditing("new")}>
            新增技能
          </Button>
        }
      >
        <Toolbar>
          <Button onClick={() => void load()}>刷新</Button>
          {items.length ? (
            <span className="text-label text-muted">共 {items.length} 个</span>
          ) : null}
        </Toolbar>

        {error ? (
          <div className="flex flex-col items-start gap-3 py-6">
            <p className="text-body text-ink-2">{error}</p>
            <Button onClick={() => void load()}>重试</Button>
          </div>
        ) : loading ? (
          <Loading rows={5} label="正在加载技能" />
        ) : items.length === 0 ? (
          <div className="py-6 text-body leading-relaxed text-muted">
            <p>还没有平台技能。</p>
            <p className="mt-1">点「新增技能」，发布后所有用户的技能页都会出现。</p>
          </div>
        ) : (
          <TableWrap>
            <table className="w-full min-w-[860px] border-collapse text-body">
              <thead>
                <tr>
                  <th className={thCls}>技能</th>
                  <th className={thCls}>分类</th>
                  <th className={thCls}>版本</th>
                  <th className={thCls}>开关</th>
                  <th className={thCls}>默认安装</th>
                  <th className={thCls}>状态</th>
                  <th className={thCls} />
                </tr>
              </thead>
              <tbody>
                {items.map((s) => (
                  <tr key={s.id} className="hover:bg-rail/70">
                    <td className={tdCls}>
                      <div className="flex items-center gap-1.5">
                        <SkillIcon icon={s.icon} />
                        <Code>{s.id}</Code>
                      </div>
                      <div className="mt-0.5 text-body text-ink">{s.name}</div>
                      {s.description ? (
                        <div className="text-label leading-relaxed text-muted">{s.description}</div>
                      ) : null}
                    </td>
                    <td className={cn(tdCls, "text-secondary text-muted")}>{s.category}</td>
                    <td className={tdNumCls}>{s.version}</td>
                    <td className={tdCls}>
                      <Badge tone={s.enabled ? "ok" : "danger"}>
                        {s.enabled ? "已发布" : "已停用"}
                      </Badge>
                    </td>
                    <td className={tdCls}>
                      {s.installed_by_default ? (
                        <Badge tone="ok">开箱可用</Badge>
                      ) : (
                        <span className="text-label text-muted">用户自己装</span>
                      )}
                    </td>
                    <td className={tdCls}>
                      {s.shadowed ? (
                        <>
                          <Badge tone="danger">与内置技能同名</Badge>
                          <div className="mt-1 text-label leading-relaxed text-muted">
                            内置目录优先，这一条不会出现在用户页
                          </div>
                        </>
                      ) : (
                        <span className="text-label text-muted">用户可见</span>
                      )}
                    </td>
                    <td className={cn(tdCls, "whitespace-nowrap")}>
                      <div className="flex items-center gap-1">
                        <Button size="sm" onClick={() => setEditing(s)}>
                          编辑
                        </Button>
                        <Button size="sm" onClick={() => void toggle(s)}>
                          {s.enabled ? "停用" : "启用"}
                        </Button>
                        <Button size="sm" variant="danger" onClick={() => void remove(s)}>
                          删除
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Panel>

      <Panel
        title="技能与工具政策"
        hint="暂停某个工具不需要改代码"
        className="mt-3"
      >
        {policyError ? (
          <div className="flex flex-col items-start gap-3 py-4">
            <p className="text-body text-ink-2">{policyError}</p>
            <Button onClick={() => void load()}>重试</Button>
          </div>
        ) : groups.length === 0 ? (
          <p className="py-4 text-center text-body text-muted">还没有工具政策记录</p>
        ) : (
          <div className="flex flex-col gap-4">
            {groups.map((group) => (
              <div key={group.tenant_id}>
                <div className="mb-1.5 flex items-center gap-2">
                  <Tag>{group.tenant_id}</Tag>
                  <span className="text-label text-muted">{group.policies.length} 条</span>
                </div>
                <TableWrap>
                  <table className="w-full text-body">
                    <thead>
                      <tr>
                        <th className={thCls}>工具</th>
                        <th className={thCls}>作用域</th>
                        <th className={thCls}>效果</th>
                        <th className={thCls}>批准方式</th>
                        <th className={thCls}>状态</th>
                        <th className={thCls} />
                      </tr>
                    </thead>
                    <tbody>
                      {group.policies.map((p) => (
                        <tr key={p.policy_id}>
                          <td className={tdCls}>
                            <Code>{p.tool_key || "（未限定）"}</Code>
                          </td>
                          <td className={cn(tdCls, "text-secondary text-muted")}>
                            {[p.role, p.user_id].filter(Boolean).join(" / ") || "全租户"}
                          </td>
                          <td className={tdCls}>{p.effect}</td>
                          <td className={cn(tdCls, "text-secondary text-muted")}>
                            {p.approval_mode}
                          </td>
                          <td className={tdCls}>
                            <Badge tone={p.enabled ? "ok" : "danger"}>
                              {p.enabled ? "生效" : "暂停"}
                            </Badge>
                          </td>
                          <td className={tdCls}>
                            <Button size="sm" onClick={() => void togglePolicy(p.policy_id, !p.enabled)}>
                              {p.enabled ? "暂停" : "恢复"}
                            </Button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableWrap>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {editing ? (
        <SkillForm
          existing={editing === "new" ? null : editing}
          items={items}
          categories={categories}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
        />
      ) : null}
    </>
  );
}

/* ---------- 图标 ---------- */

export function isImageIcon(value: unknown) {
  return /^(data:image\/|https?:\/\/)/i.test(String(value || ""));
}

function SkillIcon({ icon }: { icon?: string }) {
  const value = String(icon || "✨");
  if (isImageIcon(value)) {
    return <img src={value} alt="" className="h-4 w-4 rounded-sm object-cover" />;
  }
  return (
    <span aria-hidden="true" className="text-secondary">
      {value}
    </span>
  );
}

/* ---------- 表单 ---------- */

function SkillForm({
  existing,
  items,
  categories,
  onClose,
  onSaved,
}: {
  existing: PlatformSkill | null;
  items: PlatformSkill[];
  categories: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const isNew = !existing;
  const [name, setName] = useState(existing?.name ?? "");
  const [version, setVersion] = useState(existing?.version ?? "1.0.0");
  const [overview, setOverview] = useState(existing?.overview ?? existing?.description ?? "");
  const [icon, setIcon] = useState(String(existing?.icon || "✨"));
  const [examples, setExamples] = useState<string[]>(
    Array.isArray(existing?.examples) && existing!.examples!.length
      ? existing!.examples!.slice(0, 8)
      : [""],
  );
  const [prompt, setPrompt] = useState(existing?.prompt ?? "");
  const [promptOpen, setPromptOpen] = useState(false);
  const [enabled, setEnabled] = useState(existing ? existing.enabled !== false : true);
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState("");
  const [imported, setImported] = useState<{ text: React.ReactNode; bad?: boolean } | null>(null);
  const [dragging, setDragging] = useState(false);

  const fileRef = useRef<HTMLInputElement>(null);
  const promptFileRef = useRef<HTMLInputElement>(null);
  const dirRef = useRef<HTMLInputElement>(null);

  const importFiles = async (files: FileList | File[] | null) => {
    const list = Array.from(files || []);
    if (!list.length) return;
    try {
      let text = "";
      let source = "";
      let count = list.length;
      if (list.length === 1 && /\.zip$/i.test(list[0]!.name)) {
        const buffer = await list[0]!.arrayBuffer();
        const entries = await zipIndex(buffer);
        const hits = entries
          .filter((e: { name: string }) => /(^|[\\/])SKILL\.md$/i.test(e.name))
          .sort((a: { name: string }, b: { name: string }) => depthOf(a.name) - depthOf(b.name));
        if (!hits.length) throw new Error("压缩包里没有 SKILL.md");
        text = await zipReadText(buffer, hits[0]!);
        source = `${list[0]!.name} › ${hits[0]!.name}`;
        count = entries.length;
      } else {
        const mds = list.filter((f) =>
          /(^|[\\/])SKILL\.md$/i.test(
            (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name,
          ),
        );
        mds.sort(
          (a, b) =>
            depthOf((a as File & { webkitRelativePath?: string }).webkitRelativePath || a.name) -
            depthOf((b as File & { webkitRelativePath?: string }).webkitRelativePath || b.name),
        );
        const pick = mds[0] || list.find((f) => /\.(md|markdown)$/i.test(f.name));
        if (!pick) throw new Error("没找到 SKILL.md，也没有 .md 文件");
        text = await pick.text();
        source = (pick as File & { webkitRelativePath?: string }).webkitRelativePath || pick.name;
      }

      const { meta, body } = parseSkillFrontmatter(text) as {
        meta: { name?: string; description?: string; version?: string };
        body: string;
      };
      const limit = 20000;
      const bodyText = body.trim();
      const applied: string[] = [];

      setPrompt(bodyText.slice(0, limit));
      setPromptOpen(false);

      const put = (value: string | undefined, label: string, max: number) => {
        if (!value) return "";
        applied.push(label);
        return String(value).slice(0, max);
      };
      const nextName = put(meta.name || nameFromSource(source), "名称", 40);
      const nextOverview = put(overviewFromSkill(meta, bodyText), "概述", 600);
      const nextVersion = put(meta.version, "版本", 20);
      if (nextName) setName(nextName);
      if (nextOverview) setOverview(nextOverview);
      if (nextVersion) setVersion(nextVersion);

      const pkgExamples = examplesFromSkill(bodyText);
      if (pkgExamples.length) {
        setExamples(pkgExamples);
        applied.push("示例问法");
      }

      setImported({
        text: (
          <>
            已读取 <b>{source}</b>
            {count > 1 ? `（包内 ${count} 个文件）` : ""} · 技能指令{" "}
            {Math.min(bodyText.length, limit)} 字
            {applied.length ? ` · 自动填入${applied.join("、")}，可以改` : ""}
            {meta.name || meta.description ? null : (
              <>
                <br />
                没读到 name / description：这个文件不像 SKILL.md，名称先用文件名顶上，
                概述和指令建议手动补。
              </>
            )}
            {bodyText.length > limit ? " · 正文超过 2 万字，后面截掉了" : ""}
          </>
        ),
      });
    } catch (err) {
      setImported({ bad: true, text: `读取失败：${(err as Error).message || String(err)}` });
    }
  };

  const uploadIcon = async (file: File) => {
    try {
      const dataUrl = (await iconFileToDataURL(file, 128)) as string;
      setIcon(dataUrl);
    } catch (err) {
      toast((err as Error).message || "读不到这个文件", "error");
    }
  };

  const nextId = (value: string) => {
    const base = String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^[-._]+|[-._]+$/g, "")
      .slice(0, 48);
    const taken = new Set(items.map((item) => item.id));
    const stem = base.length >= 2 ? base : `skill-${Date.now().toString(36)}`;
    let id = stem;
    let n = 2;
    while (taken.has(id)) {
      id = `${stem}-${n}`;
      n += 1;
    }
    return id;
  };

  const save = async () => {
    if (!name.trim()) {
      setState("名称不能为空");
      return;
    }
    const category =
      existing?.category && (!categories.length || categories.includes(existing.category))
        ? existing.category
        : categories[0] || "效率工具";
    setBusy(true);
    setState("保存中…");
    try {
      await consoleApi.saveSkill({
        id: existing ? existing.id : nextId(name),
        name: name.trim(),
        icon,
        category,
        version: version.trim() || "1.0.0",
        overview: overview.trim(),
        description: oneLineSummary(overview.trim(), name.trim()),
        examples: examples.map((e) => e.trim()).filter(Boolean).slice(0, 8),
        prompt: prompt.trim(),
        enabled,
        // 「默认安装」这一项收掉了：编辑时保留原值，别把历史上设过默认安装的技能悄悄改掉
        installed_by_default: existing ? Boolean(existing.installed_by_default) : false,
      });
      toast(isNew ? "已发布，用户技能页现在就能看到" : "已保存");
      onSaved();
    } catch (err) {
      setState(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const promptLength = prompt.trim().length;

  return (
    <Drawer
      open
      onClose={onClose}
      title={isNew ? "新增平台技能" : `编辑技能 · ${existing?.name || existing?.id}`}
      width="wide"
      header={
        <DrawerHead
          title={isNew ? "新增平台技能" : `编辑技能 · ${existing?.name || existing?.id}`}
          hint={existing ? `ID ${existing.id} · 分类 ${existing.category}` : "ID 与分类按名称和已有分类自动生成"}
          onClose={onClose}
        />
      }
    >
      <Grid>
        <Field label="名称" htmlFor="sf-name" note="不超过 40 字；导入技能包会自动填，也能改">
          <input
            id="sf-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="会议纪要整理"
            className={inputCls}
          />
        </Field>
        <Field label="版本" htmlFor="sf-version" note="包里读到版本号会自动填，也能手改">
          <input
            id="sf-version"
            value={version}
            onChange={(e) => setVersion(e.target.value)}
            className={inputCls}
          />
        </Field>
      </Grid>

      <div className="mt-3 flex items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-secondary font-medium text-ink-2">图标</span>
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            title="点击上传图标"
            className="flex h-14 w-14 items-center justify-center overflow-hidden rounded-xl border border-dashed border-line-strong bg-rail hover:border-signal hover:bg-signal-wash"
          >
            {isImageIcon(icon) ? (
              <img src={icon} alt="当前图标" className="h-full w-full object-cover" />
            ) : (
              <span className="text-headline">{icon}</span>
            )}
          </button>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-label text-muted">
            {isImageIcon(icon) ? "已上传图片，自动缩到 128×128" : "还没上传图片，用的是默认图标"}
          </span>
          <div className="flex gap-2">
            <Button size="sm" onClick={() => fileRef.current?.click()}>
              上传图片
            </Button>
            <Button size="sm" onClick={() => setIcon("✨")}>
              恢复默认
            </Button>
          </div>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/svg+xml,image/gif"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void uploadIcon(file);
            e.target.value = "";
          }}
        />
      </div>

      <Field
        label="概述"
        htmlFor="sf-overview"
        className="mt-3"
        note="技能详情里展开的那段介绍。导入技能包时自动填入 description，也能改；卡片上那句话也从这里截。"
      >
        <textarea
          id="sf-overview"
          rows={3}
          value={overview}
          onChange={(e) => setOverview(e.target.value)}
          placeholder="这个技能解决什么问题、给谁用、边界在哪"
          className={textareaCls}
        />
      </Field>

      <div className="mt-3 flex flex-col gap-1.5">
        <span className="text-secondary font-medium text-ink-2">试试这样用</span>
        <div className="flex flex-col gap-1.5">
          {examples.map((value, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <input
                value={value}
                maxLength={120}
                placeholder="例如：把这份会议录音整理成纪要"
                onChange={(e) =>
                  setExamples((list) => list.map((v, idx) => (idx === i ? e.target.value : v)))
                }
                className={inputCls}
              />
              <button
                type="button"
                title="删掉这条"
                aria-label="删掉这条"
                onClick={() =>
                  setExamples((list) => {
                    const next = list.filter((_, idx) => idx !== i);
                    return next.length ? next : [""];
                  })
                }
                className="h-8 w-8 shrink-0 rounded-md text-muted hover:bg-sunken hover:text-danger"
              >
                ×
              </button>
            </div>
          ))}
        </div>
        <div>
          <Button
            size="sm"
            disabled={examples.length >= 8}
            onClick={() => setExamples((list) => [...list, ""])}
          >
            {examples.length >= 8 ? "已经 8 条，够了" : "+ 添加一条"}
          </Button>
        </div>
        <p className="text-label leading-relaxed text-muted">
          用户在技能详情里点一下，这句话就直接填进输入框，省得自己想怎么开口。最多 8 条，空着就不显示这一栏。
        </p>
      </div>

      <div className="mt-3 flex flex-col gap-1.5">
        <span className="text-secondary font-medium text-ink-2">技能指令</span>
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            const items = Array.from(e.dataTransfer.items || []);
            const entries = items
              .map((item) => item.webkitGetAsEntry?.())
              .filter(Boolean) as unknown[];
            if (entries.length) {
              void Promise.all(entries.map((entry) => readDirEntry(entry))).then((groups) =>
                importFiles(groups.flat() as File[]),
              );
              return;
            }
            void importFiles(e.dataTransfer.files);
          }}
          className={cn(
            "flex flex-wrap items-center gap-3 rounded-lg border border-dashed px-3.5 py-3",
            dragging ? "border-signal bg-signal-wash" : "border-line-strong",
          )}
        >
          <span aria-hidden="true" className="text-title">
            📄
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-body text-ink">
              <b>把技能包拖到这里</b>，或者选一个文件夹 / .zip / SKILL.md
            </div>
            <div className="text-label leading-relaxed text-muted">
              读到 SKILL.md 就把开头的 name、description、version 填进上面的名称、概述、版本（能改），
              正文留作技能指令；整包只取这一份正文。
            </div>
          </div>
          <div className="flex gap-2">
            <Button size="sm" onClick={() => promptFileRef.current?.click()}>
              选文件
            </Button>
            <Button size="sm" onClick={() => dirRef.current?.click()}>
              选文件夹
            </Button>
          </div>
          <input
            ref={promptFileRef}
            type="file"
            accept=".md,.markdown,.zip"
            multiple
            hidden
            onChange={(e) => {
              void importFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <input
            ref={dirRef}
            type="file"
            // @ts-expect-error 非标准但各浏览器都支持；React 类型里没有这个属性
            webkitdirectory=""
            directory=""
            multiple
            hidden
            onChange={(e) => {
              void importFiles(e.target.files);
              e.target.value = "";
            }}
          />
        </div>

        {imported ? (
          <div
            className={cn(
              "rounded-md px-3 py-2 text-secondary leading-relaxed",
              imported.bad ? "bg-danger-wash text-danger" : "bg-ok-wash text-ok",
            )}
          >
            {imported.text}
          </div>
        ) : null}

        <p className="text-label leading-relaxed text-muted">
          用户安装后，模型会在需要时读到这段；没导入正文的技能只有名字和概述。
        </p>

        {promptLength ? (
          <div>
            <button
              type="button"
              onClick={() => setPromptOpen((v) => !v)}
              className="text-label text-signal underline underline-offset-2 hover:no-underline"
            >
              {promptOpen ? "收起正文" : "查看正文"} · {promptLength} 字
            </button>
            {promptOpen ? (
              <textarea
                rows={8}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="技能正文，从技能包里读到的就是它"
                className={cn(textareaCls, "mt-1.5 font-mono text-label")}
              />
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="mt-4 flex items-start gap-3 rounded-lg border border-line bg-rail/60 px-3.5 py-3">
        <Switch checked={enabled} onChange={setEnabled} />
        <div>
          <div className="text-body font-medium text-ink">技能开关</div>
          <p className="text-label leading-relaxed text-muted">
            {enabled
              ? "开启：保存后立刻出现在所有用户的技能页，由他们自己点「+」安装；关闭：只在控制台留着，用户看不到。"
              : "当前是关闭状态：保存后用户技能页看不到它，已装过的用户不受影响。"}
          </p>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-3">
        <Button variant="primary" onClick={() => void save()} disabled={busy}>
          {isNew ? "发布" : "保存"}
        </Button>
        <Button onClick={onClose}>取消</Button>
        {state ? <span className="text-secondary text-danger">{state}</span> : null}
      </div>
    </Drawer>
  );
}

/* 卡片上那句「简介」不单独填了，直接从概述里截第一句 */
function oneLineSummary(text: string, fallback: string) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  if (!flat) return fallback || "";
  const first = flat.split(/[。！？!?；;]/)[0]?.trim() || flat;
  return first.length > 60 ? `${first.slice(0, 58)}…` : first;
}

/** SKILL.md 里的「## 试试这样用」段落 → 示例数组。自家导出的包能原样再导进来。 */
function examplesFromSkill(body: string) {
  const lines = String(body || "").split("\n");
  const at = lines.findIndex((line) => /^##\s*试试这样用/.test(line.trim()));
  if (at < 0) return [];
  const out: string[] = [];
  for (const raw of lines.slice(at + 1)) {
    const line = raw.trim();
    if (/^##\s/.test(line)) break;
    const hit = line.match(/^[-*+]\s+(.*)$/) || line.match(/^\d+[.)]\s+(.*)$/);
    if (hit?.[1]?.trim()) out.push(hit[1].trim().slice(0, 120));
    if (out.length >= 8) break;
  }
  return out;
}
