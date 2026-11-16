/**
 * 输入器：签名组件，永远吸底。
 *
 * 三条行为必须与旧页一致，否则用户肌肉记忆会出错：
 *  1. Enter 发送，Shift+Enter 换行；
 *  2. 输入法组字中（`isComposing`）按 Enter 不发送——中文用户最容易踩的坑；
 *  3. 高度随内容长，最多 180px 后自己滚。
 */
import { useEffect, useRef } from "react";
import { cn } from "@/lib/cn";
import { Icon } from "./primitives";

export function Composer({
  value,
  onChange,
  onSend,
  placeholder,
  hint,
  disabled,
  muted,
}: {
  value: string;
  onChange: (next: string) => void;
  onSend: () => void;
  placeholder: string;
  hint: string;
  disabled?: boolean;
  /** 房间已结束时用它：能看能选，但发不出去 */
  muted?: boolean;
}) {
  const area = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(180, el.scrollHeight)}px`;
  }, [value]);

  const canSend = value.trim().length > 0 && !disabled && !muted;

  return (
    <div className="border-t border-line bg-paper px-5 py-3">
      <div
        className={cn(
          "mx-auto max-w-[760px] rounded-xl border bg-paper px-3 pt-2.5 pb-2 transition-[border-color,box-shadow] duration-150 ease-standard",
          muted
            ? "border-line opacity-70"
            : "border-line-strong focus-within:border-signal focus-within:shadow-[0_0_0_3px_rgb(51_85_127_/_0.10)]",
        )}
      >
        <textarea
          ref={area}
          rows={1}
          value={value}
          disabled={disabled || muted}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              if (canSend) onSend();
            }
          }}
          placeholder={placeholder}
          aria-label={placeholder}
          className="w-full resize-none border-0 bg-transparent text-body leading-relaxed text-ink outline-none placeholder:text-muted disabled:cursor-not-allowed"
        />
        <div className="mt-1.5 flex items-end gap-2">
          <p className="min-w-0 flex-1 text-label leading-relaxed text-muted">{hint}</p>
          <button
            type="button"
            onClick={onSend}
            disabled={!canSend}
            aria-label="发送"
            className={cn(
              "flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-md transition-colors duration-150 ease-standard",
              "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
              canSend
                ? "bg-signal text-paper hover:bg-signal-deep"
                : "bg-sunken text-muted",
              "disabled:cursor-not-allowed",
            )}
          >
            <Icon name="arrowUp" size={13} />
          </button>
        </div>
      </div>
    </div>
  );
}
