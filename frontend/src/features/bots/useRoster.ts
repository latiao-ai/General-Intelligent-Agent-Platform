/**
 * 花名册：机器人 + 分区 + 群聊。
 *
 * 这三份数据来自同一个 `/api/bots`，所以放一个 hook 里，不要拆成三个
 * 各自发一次请求——旧页就干过这事，切一次页签打三遍接口。
 *
 * `refresh()` 在每次增删改之后都要调：花名册是"当前事实"，本地做乐观更新
 * 会在分区、置顶排序这些地方越算越偏。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "@/lib/api";
import { getRoster } from "./api";
import type { BotSection, BotSummary, RoomSummary } from "./types";

interface RosterState {
  bots: BotSummary[];
  sections: BotSection[];
  rooms: RoomSummary[];
  /** 服务端总开关 BOT_MODE_ENABLED；关掉时只能看不能动 */
  enabled: boolean;
  loading: boolean;
  error: string | null;
}

const INITIAL: RosterState = {
  bots: [],
  sections: [],
  rooms: [],
  enabled: true,
  loading: true,
  error: null,
};

export function useRoster() {
  const [state, setState] = useState<RosterState>(INITIAL);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const data = await getRoster();
      if (!alive.current) return;
      setState({
        bots: data.items ?? [],
        sections: data.sections ?? [],
        rooms: data.rooms ?? [],
        enabled: data.enabled !== false,
        loading: false,
        error: null,
      });
    } catch (err) {
      if (!alive.current) return;
      setState((prev) => ({ ...prev, loading: false, error: errorMessage(err) }));
      throw err;
    }
  }, []);

  useEffect(() => {
    refresh().catch(() => {
      /* 首次加载失败已在 state.error 里，页面会画错误态 */
    });
  }, [refresh]);

  return { ...state, refresh };
}
