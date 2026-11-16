/**
 * 短信登录用的滑块拼图。
 *
 * 后端代理返回两张 base64 图：背景（JPEG）与拼图块（PNG，47×155）。
 * 拼图块横向跟着滑块走，松手时把**底图坐标**下的 x 交给后端校验——坐标加密
 * （AES-ECB）在后端做，浏览器原生 Web Crypto 不支持 ECB，别想着在前端补。
 *
 * 三个容易错的地方：
 *  1. x 必须是 310 宽底图上的坐标。canvas 被 CSS 缩放时按 `310 / clientWidth`
 *     换算，否则窄屏上永远对不齐（旧页也是这么处理的）；
 *  2. 票据是一次性的：校验通过拿到 `captchaVerification` 后要立刻用它发码，
 *     再点「获取验证码」必须重新过一次滑块；
 *  3. 失败只把滑块归零、不自动重新拉挑战——自动刷新会闪一下，用户会以为
 *     自己点错了。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";

const IMG_W = 310;
const IMG_H = 155;
const PIECE_W = 47;

interface Challenge {
  token: string;
  secretKey: string;
  bg: string;
  piece: string;
}

async function readJson(res: Response): Promise<Record<string, unknown>> {
  try {
    return (await res.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function detailOf(data: Record<string, unknown>, fallback: string): string {
  const raw = data.detail;
  return typeof raw === "string" && raw ? raw : fallback;
}

export function CaptchaSlider({
  mobile,
  onVerified,
  onError,
}: {
  /** 手机号要一起交给后端：`SmsCheckRequest.mobile` 是必填，空串直接 422 */
  mobile: string;
  /** 校验通过，交出一次性票据（父组件拿它去发短信） */
  onVerified: (ticket: string) => void;
  onError: (message: string) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLDivElement>(null);
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [left, setLeft] = useState(0);
  const [hint, setHint] = useState("加载拼图中…");
  const [failed, setFailed] = useState(false);
  const [checking, setChecking] = useState(false);
  const [done, setDone] = useState(false);
  const dragRef = useRef<{ startX: number; origin: number } | null>(null);
  const imagesRef = useRef<{ bg: HTMLImageElement; piece: HTMLImageElement } | null>(null);

  /* ---------------- 拉挑战 + 预加载图片 ---------------- */
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch("/api/auth/sms-login/captcha/get", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const data = await readJson(res);
        if (!res.ok) throw new Error(detailOf(data, "获取滑块失败"));
        if (!alive) return;
        const bgType = String(data.originalImageType || "image/jpeg");
        const pieceType = String(data.jigsawImageType || "image/png");
        const next: Challenge = {
          token: String(data.token ?? ""),
          secretKey: String(data.secretKey ?? ""),
          bg: `data:${bgType};base64,${String(data.originalImageBase64 ?? "")}`,
          piece: `data:${pieceType};base64,${String(data.jigsawImageBase64 ?? "")}`,
        };
        setChallenge(next);
        const bg = new Image();
        const piece = new Image();
        await Promise.all([
          new Promise<void>((resolve, reject) => {
            bg.onload = () => resolve();
            bg.onerror = () => reject(new Error("背景图加载失败"));
            bg.src = next.bg;
          }),
          new Promise<void>((resolve, reject) => {
            piece.onload = () => resolve();
            piece.onerror = () => reject(new Error("拼图块加载失败"));
            piece.src = next.piece;
          }),
        ]);
        if (!alive) return;
        imagesRef.current = { bg, piece };
        setHint("拖动滑块完成拼图");
        // 图片到位后立刻画一次，否则画布空白
        draw(0);
      } catch (err) {
        if (!alive) return;
        setFailed(true);
        const message = err instanceof Error ? err.message : "获取滑块失败";
        setHint("加载失败，点「获取验证码」重试");
        onError(message);
      }
    })();
    return () => {
      alive = false;
    };
    // 只在挂载时拉一次：换挑战由父组件换 key 重挂载
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* ---------------- 绘制 ---------------- */
  const draw = useCallback((offset: number) => {
    const canvas = canvasRef.current;
    const images = imagesRef.current;
    if (!canvas || !images) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, IMG_W, IMG_H);
    ctx.drawImage(images.bg, 0, 0, IMG_W, IMG_H);
    const scale = canvas.clientWidth ? IMG_W / canvas.clientWidth : 1;
    ctx.drawImage(images.piece, offset * scale, 0, PIECE_W, IMG_H);
  }, []);

  // offset 变了就重绘（拖拽时每次 move 都会走这里）
  useEffect(() => {
    draw(left);
  }, [left, draw, challenge]);

  /* ---------------- 拖拽 ---------------- */
  const maxLeft = () => {
    const track = trackRef.current;
    const thumb = thumbRef.current;
    if (!track || !thumb) return 0;
    return Math.max(0, track.clientWidth - thumb.clientWidth);
  };

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (done || checking || failed) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = { startX: event.clientX, origin: left };
    event.preventDefault();
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const next = Math.max(0, Math.min(maxLeft(), drag.origin + (event.clientX - drag.startX)));
    setLeft(next);
  };

  const onPointerUp = () => {
    if (!dragRef.current) return;
    dragRef.current = null;
    void check();
  };

  /**
   * 键盘也能过：滑块是 `role="slider"`，方向键微调、Enter 提交。
   * 只做鼠标拖拽会把只用键盘的人直接挡在短信登录外面。
   */
  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (done || checking || failed) return;
    const step = event.shiftKey ? 10 : 2;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const delta = event.key === "ArrowLeft" ? -step : step;
      setLeft((prev) => Math.max(0, Math.min(maxLeft(), prev + delta)));
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      void check();
    }
  };

  const check = async () => {
    if (!challenge || checking || done) return;
    const canvas = canvasRef.current;
    const scale = canvas && canvas.clientWidth ? IMG_W / canvas.clientWidth : 1;
    const x = Math.round(left * scale);
    setChecking(true);
    setHint("校验中…");
    try {
      const res = await fetch("/api/auth/sms-login/captcha/check", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          captchaType: "blockPuzzle",
          token: challenge.token,
          secretKey: challenge.secretKey,
          x,
          mobile,
        }),
      });
      const data = await readJson(res);
      if (!res.ok) throw new Error(detailOf(data, "滑块校验失败"));
      const ticket = String(data.captchaVerification ?? "");
      if (!ticket) throw new Error("滑块校验失败");
      setDone(true);
      setHint("拼图完成 ✓");
      onVerified(ticket);
    } catch (err) {
      const message = err instanceof Error ? err.message : "滑块校验失败";
      setHint("拖动滑块重试");
      setLeft(0);
      onError(message);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div className="relative overflow-hidden rounded-md border border-line bg-sunken">
        <canvas
          ref={canvasRef}
          width={IMG_W}
          height={IMG_H}
          className="block h-auto w-full"
          aria-hidden="true"
        />
      </div>
      <div
        ref={trackRef}
        className="relative h-[30px] w-full rounded-md border border-line bg-sunken"
      >
        <div
          ref={thumbRef}
          role="slider"
          tabIndex={0}
          aria-label="拖动完成拼图验证"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round((left / (maxLeft() || 1)) * 100)}
          aria-disabled={done || checking}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onKeyDown={onKeyDown}
          className={cn(
            "absolute top-0 left-0 flex h-[28px] w-[44px] items-center justify-center rounded-md border border-line-strong bg-paper text-secondary text-ink select-none",
            "transition-colors duration-150 ease-standard",
            done
              ? "border-ok/40 text-ok"
              : "cursor-grab active:cursor-grabbing hover:border-signal",
            "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-signal/25",
          )}
          style={{ left }}
        >
          {done ? "✓" : "→"}
        </div>
        <span className="absolute inset-0 flex items-center justify-center text-label text-muted">
          {done ? "" : "拖动滑块完成拼图"}
        </span>
      </div>
      <p
        className={cn(
          "text-label",
          failed ? "text-danger" : done ? "text-ok" : "text-muted",
        )}
        role="status"
      >
        {hint}
      </p>
    </div>
  );
}
