/**
 * 例行任务的两个读取口。
 *
 * 注意这是**两个不同的端点**，别合并：
 *  - `/api/bots/{id}/routines`：只属于这个机器人的，用在右侧详情栏；
 *  - `/api/schedules`：全量定时任务，前端再按 `bot_id` 过滤，用在例行任务视图。
 *
 * 合并成一个"反正都是例行任务"的请求会漏数据：视图要的是全部，
 * 侧栏要的是当前这一个；两个方向的过滤条件不一样。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "@/lib/api";
import { createRoutine, getBotRoutines, getSchedules } from "./api";
import type { Routine } from "./types";

/** 侧栏：某个机器人名下的例行任务。读不到就不显示，不打断聊天。 */
export function useBotRoutines(botId: string) {
  const [items, setItems] = useState<Routine[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!botId) {
      setItems([]);
      return;
    }
    let alive = true;
    setLoading(true);
    getBotRoutines(botId)
      .then((data) => {
        if (alive) setItems(data.items ?? []);
      })
      .catch(() => {
        // 读不到就当没有：这一块缺失不影响对话
        if (alive) setItems([]);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [botId]);

  return { items, loading };
}

/** 视图：全部归属机器人的例行任务。 */
export function useRoutineTasks() {
  const [tasks, setTasks] = useState<Routine[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    setPhase("loading");
    try {
      const data = await getSchedules();
      if (!alive.current) return;
      setTasks((data.tasks ?? []).filter((task) => Boolean(task.bot_id)));
      setPhase("ready");
      setError(null);
    } catch (err) {
      if (!alive.current) return;
      setError(errorMessage(err));
      setPhase("error");
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { tasks, phase, error, reload };
}

/** 给某个机器人加一条例行任务。 */
export function useCreateRoutine() {
  const [saving, setSaving] = useState(false);

  const submit = useCallback(
    async (botId: string, name: string, prompt: string, rrule: string) => {
      setSaving(true);
      try {
        await createRoutine({ bot_id: botId, name, prompt, rrule });
        return true;
      } finally {
        setSaving(false);
      }
    },
    [],
  );

  return { saving, submit };
}
