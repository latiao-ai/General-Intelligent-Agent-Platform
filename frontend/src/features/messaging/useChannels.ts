/**
 * 消息平台的数据层：实例列表 + 渠道清单 + 当前渠道详情 + 表单 + 轮询。
 *
 * 三条从老页面照搬、不能改的语义：
 *  1. 列表和详情**分开拉**。列表决定左侧那三个点的颜色，详情决定正文，
 *     一个失败不该让另一个也空掉。
 *  2. 轮询只在「没有未保存修改」时跑。老页用 `state.dirty` 挡住 6 秒一次的
 *     刷新，否则用户输入到一半会被服务端返回值覆盖。
 *  3. 保存后必须重拉列表。渠道状态是后端**派生**的（开关 + 凭据 + 运行态），
 *     前端算不出来，只能问。
 *
 * 与老页不同的两处：
 *  - 切换渠道时先清空详情再拉新的，避免旧渠道的卡片在新渠道的标题下闪一下。
 *  - **多了一层实例**：同一个「飞书」在每个机器人下是各自一套配置和凭据。
 *    所有请求都带 `instance`，并且响应回来要再比对一次当前实例——用户连点
 *    两个 tab 时，先发的请求可能后回来，不比对就会把 A 的配置画在 B 的标题下。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import {
  approvePairing,
  bindSession as bindSessionApi,
  createInstance as createInstanceApi,
  deleteInstance as deleteInstanceApi,
  getBots,
  getChannel,
  getChannels,
  getInstances,
  getSessions,
  patchChannel,
  reloadChannel,
  revokePairing,
  testChannel,
} from "./api";
import type {
  BotOption,
  ChannelForm,
  ChannelView,
  CreateInstanceResult,
  MessagingInstance,
  PairingRow,
  SessionRow,
} from "./types";

export type LoadPhase = "loading" | "ready" | "error";
export type Busy =
  | ""
  | "enable"
  | "save"
  | "reload"
  | "test"
  | "pair"
  | "instance"
  | "session";

/** 后端把白名单存成数组；界面上是一格文本，逗号、中文逗号、换行都算分隔。 */
export function splitUsers(text: string): string[] {
  return text
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function formFromChannel(channel: ChannelView): ChannelForm {
  const options: Record<string, string> = {};
  for (const field of channel.options ?? []) {
    options[field.key] = field.value ?? "";
  }
  return {
    bot_id: channel.bot_id ?? "",
    dm_policy: channel.dm_policy ?? "",
    group_policy: channel.group_policy ?? "",
    allowed_users: (channel.allowed_users ?? []).join("\n"),
    options,
  };
}

export function useChannels() {
  const toast = useToast();

  const [instances, setInstances] = useState<MessagingInstance[]>([]);
  const [channels, setChannels] = useState<ChannelView[]>([]);
  const [gatewayAlive, setGatewayAlive] = useState(false);
  const [messagingEnabled, setMessagingEnabled] = useState(true);
  const [listPhase, setListPhase] = useState<LoadPhase>("loading");
  const [listError, setListError] = useState("");

  /** 深链 `?instance=<bot_id>`：从「机器人」页跳过来直接落在它的接入配置上。 */
  const [instanceId, setInstanceId] = useState(
    () => new URLSearchParams(window.location.search).get("instance") ?? "",
  );

  const [selected, setSelected] = useState(() => {
    // 深链 `?channel=weixin`：从别处跳过来时直接落在那个渠道上
    return new URLSearchParams(window.location.search).get("channel") ?? "";
  });
  const [detail, setDetail] = useState<ChannelView | null>(null);
  const [detailPhase, setDetailPhase] = useState<LoadPhase>("loading");
  const [detailError, setDetailError] = useState("");

  const [form, setForm] = useState<ChannelForm | null>(null);
  const [dirty, setDirty] = useState(false);
  const [bots, setBots] = useState<BotOption[]>([]);
  const [busy, setBusy] = useState<Busy>("");
  const [enablePending, setEnablePending] = useState<boolean | null>(null);

  /* 会话身份：这个渠道下最近聊过的会话，以及每段由谁回话 */
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [sessionBots, setSessionBots] = useState<BotOption[]>([]);
  const [channelBotId, setChannelBotId] = useState("");
  const [sessionsPhase, setSessionsPhase] = useState<LoadPhase>("loading");
  const [sessionsError, setSessionsError] = useState("");

  /* 轮询回调里要读最新值，但不该因为值变了就重建定时器 */
  const instanceRef = useRef(instanceId);
  const selectedRef = useRef(selected);
  const dirtyRef = useRef(dirty);
  const busyRef = useRef(busy);
  const alive = useRef(true);
  const seq = useRef(0);
  /* 会话列表单独一个序号：它和详情是两个请求，共用一个计数器会互相作废 */
  const sessionSeq = useRef(0);

  useEffect(() => {
    instanceRef.current = instanceId;
  }, [instanceId]);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);
  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);
  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /* ---------------- 实例 ---------------- */

  const loadInstances = useCallback(async () => {
    try {
      const data = await getInstances();
      if (!alive.current) return;
      const list = data.instances ?? [];
      setInstances(list);
      setGatewayAlive(Boolean(data.gateway_alive));
      setMessagingEnabled(data.enabled !== false);
      // URL 里的实例没了（被删掉、换了部署）→ 退回默认，不白屏
      const current = instanceRef.current;
      if (current && !list.some((item) => item.instance_id === current)) {
        instanceRef.current = "";
        setInstanceId("");
      }
    } catch {
      // 实例列表读不到不覆盖已有内容：渠道列表还能照常显示
    }
  }, []);

  /* ---------------- 列表 ---------------- */

  const loadList = useCallback(async (instance: string, opts?: { silent?: boolean }) => {
    try {
      const data = await getChannels(instance);
      // 实例已经切走了：这份响应属于上一个 tab，丢掉
      if (!alive.current || instance !== instanceRef.current) return;
      const list = data.channels ?? [];
      setChannels(list);
      setGatewayAlive(Boolean(data.gateway_alive));
      setMessagingEnabled(data.enabled !== false);
      setListPhase("ready");
      setListError("");
      // 选中的渠道不在清单里（老链接、切到没有个人微信的实例）→ 退到第一个
      setSelected((prev) => {
        if (!list.length) return prev;
        return list.some((item) => item.id === prev) ? prev : list[0]!.id;
      });
    } catch (err) {
      if (!alive.current || instance !== instanceRef.current) return;
      // 静默刷新失败时不要把已有列表换成错误页：网关了但数据还在
      if (!opts?.silent) {
        setListPhase("error");
        setListError(errorMessage(err));
      }
    }
  }, []);

  /* ---------------- 详情 ---------------- */

  const loadDetail = useCallback(
    async (id: string, instance: string, opts?: { silent?: boolean }) => {
      if (!id) return;
      const mine = ++seq.current;
      if (!opts?.silent) {
        setDetailPhase("loading");
        setDetailError("");
      }
      try {
        const data = await getChannel(id, instance);
        // 连点两个渠道/实例时，先发的请求可能后回来，用序号丢弃过期响应
        if (!alive.current || mine !== seq.current) return;
        if (instance !== instanceRef.current) return;
        setDetail(data);
        setDetailPhase("ready");
        setDetailError("");
      } catch (err) {
        if (!alive.current || mine !== seq.current) return;
        if (!opts?.silent) {
          setDetailPhase("error");
          setDetailError(errorMessage(err));
        }
      }
    },
    [],
  );

  useEffect(() => {
    void loadInstances();
  }, [loadInstances]);

  /* ---------------- 会话身份 ---------------- */

  const loadSessions = useCallback(
    async (id: string, instance: string, opts?: { silent?: boolean }) => {
      if (!id) {
        setSessions([]);
        return;
      }
      const mine = ++sessionSeq.current;
      if (!opts?.silent) setSessionsPhase("loading");
      try {
        const data = await getSessions(id, instance);
        if (!alive.current || mine !== sessionSeq.current) return;
        if (instance !== instanceRef.current) return;
        setSessions(data.sessions ?? []);
        setSessionBots(data.bots ?? []);
        setChannelBotId(data.channel_bot_id ?? "");
        setSessionsPhase("ready");
        setSessionsError("");
      } catch (err) {
        if (!alive.current || mine !== sessionSeq.current) return;
        if (!opts?.silent) {
          setSessionsPhase("error");
          setSessionsError(errorMessage(err));
        }
      }
    },
    [],
  );

  useEffect(() => {
    if (!selected) {
      setSessions([]);
      return;
    }
    void loadSessions(selected, instanceId);
  }, [selected, instanceId, loadSessions]);

  useEffect(() => {
    void loadList(instanceId);
  }, [instanceId, loadList]);

  useEffect(() => {
    if (!selected) return;
    void loadDetail(selected, instanceId);
  }, [selected, instanceId, loadDetail]);

  /**
   * 表单只在**换渠道/换实例**时重置。
   * 不能跟着 `detail` 对象重置：每 6 秒轮询都会换一个新对象，
   * 那样用户打一半的字会被服务端的旧值覆盖，光标也会跳回开头。
   */
  const detailId = detail ? `${detail.instance_id ?? ""}/${detail.id}` : "";
  useEffect(() => {
    if (!detail) {
      setForm(null);
      return;
    }
    setForm(formFromChannel(detail));
    setDirty(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailId]);

  /* ---------------- 轮询 ---------------- */

  useEffect(() => {
    const tick = () => {
      if (document.hidden) return;
      if (dirtyRef.current || busyRef.current) return;
      void loadInstances();
      void loadList(instanceRef.current, { silent: true });
      void loadDetail(selectedRef.current, instanceRef.current, { silent: true });
      void loadSessions(selectedRef.current, instanceRef.current, { silent: true });
    };
    const timer = window.setInterval(tick, 6000);
    // 切回标签页时立刻补一次：离开 10 分钟回来看到的是 10 分钟前的状态
    const onVisible = () => {
      if (!document.hidden && !dirtyRef.current && !busyRef.current) tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [loadInstances, loadList, loadDetail, loadSessions]);

  /* 有未保存修改时关页面/刷新要拦一下。老页没有这一步，改完直接关就没了。 */
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  /* ---------------- 选中 ---------------- */

  const select = useCallback((id: string) => {
    if (!id || id === selectedRef.current) return;
    selectedRef.current = id;
    setSelected(id);
    setDetail(null);
    setDetailPhase("loading");
    setDetailError("");
    const url = new URL(window.location.href);
    url.searchParams.set("channel", id);
    window.history.replaceState(null, "", url);
  }, []);

  /**
   * 换实例。
   *
   * 三个必须一起做的事：
   *  1. 渠道选中清空——个人微信只在默认实例里，带着 `weixin` 切到机器人实例会
   *     先发一个 404 再落到第一个渠道，屏幕上闪一条错误。
   *  2. 详情和表单清空，理由同上：旧实例的凭据卡会短暂挂在新实例的标题下。
   *  3. 地址栏跟着变，刷新后还停在同一个机器人上。
   */
  const selectInstance = useCallback((next: string) => {
    if (next === instanceRef.current) return;
    instanceRef.current = next;
    setInstanceId(next);
    selectedRef.current = "";
    setSelected("");
    setDetail(null);
    setDetailPhase("loading");
    setDetailError("");
    setForm(null);
    setDirty(false);
    const url = new URL(window.location.href);
    if (next) url.searchParams.set("instance", next);
    else url.searchParams.delete("instance");
    url.searchParams.delete("channel");
    window.history.replaceState(null, "", url);
  }, []);

  /* ---------------- 机器人下拉 ---------------- */

  useEffect(() => {
    getBots()
      .then((items) => {
        if (alive.current) setBots(items.filter((bot) => bot.status !== "archived"));
      })
      .catch(() => {
        // 花名册读不到就退化成「不绑定」一个选项，不能连累整页
        if (alive.current) setBots([]);
      });
  }, []);

  /* ---------------- 实例增删 ---------------- */

  const createInstance = useCallback(
    async (botId: string): Promise<CreateInstanceResult | null> => {
      setBusy("instance");
      try {
        const result = await createInstanceApi(botId);
        await loadInstances();
        // 建完直接落在这个实例上：用户下一步就是去填它要的那几条凭据
        selectInstance(result.instance.instance_id);
        return result;
      } catch (err) {
        toast(errorMessage(err), "error");
        return null;
      } finally {
        setBusy("");
      }
    },
    [loadInstances, selectInstance, toast],
  );

  const removeInstance = useCallback(
    async (id: string) => {
      if (!id) return false;
      setBusy("instance");
      try {
        await deleteInstanceApi(id);
        await loadInstances();
        if (instanceRef.current === id) selectInstance("");
        toast("实例已移除；环境变量和历史会话都还在");
        return true;
      } catch (err) {
        toast(errorMessage(err), "error");
        return false;
      } finally {
        setBusy("");
      }
    },
    [loadInstances, selectInstance, toast],
  );

  /* ---------------- 改表单 ---------------- */

  const edit = useCallback((patch: Partial<ChannelForm>) => {
    setForm((prev) => (prev ? { ...prev, ...patch } : prev));
    setDirty(true);
  }, []);

  const editOption = useCallback((key: string, value: string) => {
    setForm((prev) => (prev ? { ...prev, options: { ...prev.options, [key]: value } } : prev));
    setDirty(true);
  }, []);

  /* ---------------- 动作 ---------------- */

  const save = useCallback(async () => {
    const channel = detail;
    const current = form;
    if (!channel || !current) return false;
    const instance = channel.instance_id ?? "";
    setBusy("save");
    try {
      const updated = await patchChannel(
        channel.id,
        {
          bot_id: current.bot_id,
          dm_policy: current.dm_policy,
          group_policy: current.group_policy,
          allowed_users: splitUsers(current.allowed_users),
          options: current.options,
        },
        instance,
      );
      setDetail(updated);
      setForm(formFromChannel(updated));
      setDirty(false);
      await loadList(instance, { silent: true });
      await loadInstances();
      // 渠道默认身份可能变了：跟随渠道的那些会话，"有效身份"跟着变
      await loadSessions(updated.id, instance, { silent: true });
      toast("配置已保存；Worker 下一个节拍会按需重建连接");
      return true;
    } catch (err) {
      toast(errorMessage(err), "error");
      return false;
    } finally {
      setBusy("");
    }
  }, [detail, form, loadInstances, loadList, loadSessions, toast]);

  const setEnabled = useCallback(
    async (next: boolean) => {
      const id = selectedRef.current;
      if (!id) return;
      // 先让开关跟着手指走，失败再回弹——不然点了没反应会像坏了
      setEnablePending(next);
      setBusy("enable");
      try {
        const updated = await patchChannel(id, { enabled: next }, instanceRef.current);
        setDetail(updated);
        await loadList(instanceRef.current, { silent: true });
        await loadInstances();
        toast(next ? "已启用" : "已停用");
      } catch (err) {
        toast(errorMessage(err), "error");
      } finally {
        setEnablePending(null);
        setBusy("");
      }
    },
    [loadInstances, loadList, toast],
  );

  const reload = useCallback(async () => {
    const id = selectedRef.current;
    if (!id) return;
    setBusy("reload");
    try {
      await reloadChannel(id, instanceRef.current);
      toast("已请求 Worker 重建连接");
      // Worker 的一个节拍大约 1 秒，等它落库再重拉才有意义
      window.setTimeout(
        () => void loadDetail(id, instanceRef.current, { silent: true }),
        1200,
      );
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      setBusy("");
    }
  }, [loadDetail, toast]);

  const sendTest = useCallback(async () => {
    const channel = detail;
    const current = form;
    if (!channel || !current) return false;
    // 目标就是 home_chat_id；报错里用它自己的标签，用户才知道去哪填
    const option = (channel.options ?? []).find((item) => item.key === "home_chat_id");
    const label = option?.label ?? "默认会话 ID";
    const target = (current.options.home_chat_id ?? "").trim();
    if (!target) {
      toast(`先在「${label}」里填一个目标，再发送测试`, "error");
      return false;
    }
    setBusy("test");
    try {
      await testChannel(channel.id, target, channel.instance_id ?? "");
      toast("测试消息已进入出站队列，几秒内会发出");
      return true;
    } catch (err) {
      toast(errorMessage(err), "error");
      return false;
    } finally {
      setBusy("");
    }
  }, [detail, form, toast]);

  const approve = useCallback(
    async (code: string) => {
      const trimmed = code.trim();
      if (!trimmed) {
        toast("请输入配对码", "error");
        return false;
      }
      setBusy("pair");
      try {
        await approvePairing(trimmed);
        toast("已批准，已通知对方");
        await loadDetail(selectedRef.current, instanceRef.current, { silent: true });
        return true;
      } catch (err) {
        toast(errorMessage(err), "error");
        return false;
      } finally {
        setBusy("");
      }
    },
    [loadDetail, toast],
  );

  const revoke = useCallback(
    async (row: PairingRow) => {
      setBusy("pair");
      try {
        await revokePairing(row.platform, row.user_id);
        toast("已撤销");
        await loadDetail(selectedRef.current, instanceRef.current, { silent: true });
        return true;
      } catch (err) {
        toast(errorMessage(err), "error");
        return false;
      } finally {
        setBusy("");
      }
    },
    [loadDetail, toast],
  );

  const refresh = useCallback(async () => {
    await loadInstances();
    await loadList(instanceRef.current);
    await loadDetail(selectedRef.current, instanceRef.current, { silent: true });
    await loadSessions(selectedRef.current, instanceRef.current, { silent: true });
    toast("已刷新");
  }, [loadDetail, loadInstances, loadList, loadSessions, toast]);

  /** 静默补一次全部数据。扫码成功后、错误态重试时用，不弹「已刷新」。 */
  const sync = useCallback(async () => {
    await loadInstances();
    await loadList(instanceRef.current, { silent: true });
    await loadDetail(selectedRef.current, instanceRef.current, { silent: true });
    await loadSessions(selectedRef.current, instanceRef.current, { silent: true });
  }, [loadDetail, loadInstances, loadList, loadSessions]);

  /**
   * 改一段会话的回话身份。
   *
   * 只写覆盖值：真正换会话是这条路下一条消息进来时 bridge 的事（它发现人格和
   * 生效身份不一致会滚一代）。所以文案说"下一条消息"而不是"已切换"——
   * 现在就说切了，用户马上发一句却发现历史还在，反而像没生效。
   */
  const bindChat = useCallback(
    async (chatId: string, botId: string) => {
      const channelId = selectedRef.current;
      if (!channelId || !chatId) return false;
      setBusy("session");
      try {
        await bindSessionApi({
          channel_id: channelId,
          instance: instanceRef.current,
          chat_id: chatId,
          bot_id: botId,
        });
        await loadSessions(channelId, instanceRef.current, { silent: true });
        toast(botId ? "已指定；对方下一条消息起由它回答" : "已恢复跟随渠道默认");
        return true;
      } catch (err) {
        toast(errorMessage(err), "error");
        return false;
      } finally {
        setBusy("");
      }
    },
    [loadSessions, toast],
  );

  const active = useMemo(
    () => channels.find((item) => item.id === selected),
    [channels, selected],
  );

  const activeInstance = useMemo(
    () => instances.find((item) => item.instance_id === instanceId),
    [instances, instanceId],
  );

  return {
    instances,
    instanceId,
    activeInstance,
    channels,
    active,
    gatewayAlive,
    messagingEnabled,
    listPhase,
    listError,
    selected,
    detail,
    detailPhase,
    detailError,
    form,
    dirty,
    bots,
    busy,
    enablePending,
    sessions,
    sessionBots,
    channelBotId,
    sessionsPhase,
    sessionsError,
    select,
    selectInstance,
    createInstance,
    removeInstance,
    edit,
    editOption,
    save,
    setEnabled,
    reload,
    sendTest,
    approve,
    revoke,
    bindChat,
    refresh,
    sync,
    reloadDetail: loadDetail,
  };
}
