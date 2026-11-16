import { useEffect, useRef, useState } from "react";
import { compact, num } from "./format";

/**
 * 柱状图。
 *
 * 关键一点：viewBox 的宽度必须等于容器的真实像素宽度。旧页先按 1040 画、
 * 再用 width:100% 缩到实际宽度，配合 preserveAspectRatio="none" 会把整张图
 * 等比压扁——手机上和抽屉里，柱子和日期挤成一条线、字也糊在一起。
 * 这里用 ResizeObserver 拿真实宽度重画，字号才是"所见即所写"。
 */
export function BarChart<T extends { day: string }>({
  rows,
  field,
  color = "var(--color-signal)",
  height = 130,
  unit = "",
  empty = "这段时间没有数据",
  padding = false,
}: {
  rows: readonly T[];
  field: string;
  color?: string;
  height?: number;
  unit?: string;
  empty?: string;
  padding?: boolean;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const update = () => setWidth(Math.floor(el.clientWidth));
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (!rows.length) {
    return <p className="py-6 text-center text-body text-muted">{empty}</p>;
  }

  const W = Math.max(280, width || 640);
  const H = height;
  const padL = 38;
  const padB = 20;
  const padT = 8;
  const plotW = W - padL - 8;
  const plotH = H - padT - padB;

  const at = (row: T, key: string) => Number((row as Record<string, unknown>)[key] || 0);
  const values = rows.map((r) => at(r, field));
  const max = Math.max(1, ...values);
  const step = plotW / rows.length;
  const barW = Math.max(2, Math.min(28, step - 3));
  // 每个标签按 36px 预算排，排不下就隔几天标一次。固定"最多 14 个"
  // 在宽屏上太稀、在抽屉里全挤在一起。
  const labelEvery = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(plotW / 36))));

  return (
    <div ref={boxRef} className="w-full">
      <svg
        width={W}
        height={H}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={rows.map((r) => `${r.day}:${num(at(r, field))}`).join("，")}
        className={padding ? "overflow-visible" : undefined}
      >
        {[0, 0.5, 1].map((f) => {
          const y = padT + plotH * (1 - f);
          return (
            <g key={f}>
              <line
                x1={padL}
                x2={W - 8}
                y1={y}
                y2={y}
                stroke="var(--color-line)"
                strokeWidth="1"
                strokeDasharray={f === 0 ? undefined : "2 3"}
              />
              <text
                x={padL - 6}
                y={y + 3}
                textAnchor="end"
                fontSize="10"
                fill="var(--color-muted)"
              >
                {compact(Math.round(max * f))}
              </text>
            </g>
          );
        })}

        {rows.map((row, i) => {
          const value = at(row, field);
          const h = value === 0 ? 0 : Math.max(2, (value / max) * plotH);
          const x = padL + i * step + (step - barW) / 2;
          const y = padT + plotH - h;
          return (
            <g key={row.day + i}>
              <rect x={x} y={y} width={barW} height={h} rx="2" fill={color} opacity="0.92">
                <title>{`${row.day}：${num(value)}${unit}`}</title>
              </rect>
              {i % labelEvery === 0 ? (
                <text
                  x={x + barW / 2}
                  y={H - 5}
                  textAnchor="middle"
                  fontSize="10"
                  fill="var(--color-muted)"
                >
                  {row.day.slice(5)}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/**
 * 每日消耗的迷你柱。旧页用 <i> + 高度百分比，这里用同样的做法但做成组件：
 * 表里 100 行时这玩意儿会出现 100 次，必须是纯 CSS，不能一个图表一个 svg。
 */
export function Spark({
  values,
  titles,
}: {
  values: number[];
  titles?: string[];
}) {
  const peak = Math.max(1, ...values);
  return (
    <div className="flex h-[22px] items-end gap-[2px]" aria-hidden="true">
      {values.map((v, i) => (
        <i
          key={i}
          title={titles?.[i]}
          className={
            v >= peak * 0.66
              ? "block w-[4px] rounded-[1px] bg-signal"
              : "block w-[4px] rounded-[1px] bg-signal/35"
          }
          style={{ height: `${Math.max(6, Math.round((v / peak) * 100))}%` }}
        />
      ))}
    </div>
  );
}
