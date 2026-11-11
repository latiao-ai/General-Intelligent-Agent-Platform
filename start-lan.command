#!/bin/zsh
# 双击运行：以「局域网联调模式」启动中台服务，手机/平板连同一个 WiFi 就能打开。
# 关掉这个终端窗口 = 服务停止；要指定端口就 `PORT=9000 ./start-lan.command`。
cd "$(dirname "$0")" || exit 1

PY=".venv-pi-test/bin/python"
[ -x "$PY" ] || PY="python3"

# 端口被占（常见：已经有一个只绑 127.0.0.1 的实例在跑）时自动往后找，
# 否则 0.0.0.0:8002 绑不上，手机会一直连不上还看不出原因。
CHOSEN=""
for p in "${PORT:-8002}" 8003 8004 8005 8006; do
  if ! nc -z 127.0.0.1 "$p" >/dev/null 2>&1; then
    CHOSEN="$p"
    break
  fi
done

if [ -z "$CHOSEN" ]; then
  echo "8002-8006 都被占用了，先腾一个端口出来再跑。"
  echo "看看是谁占着：lsof -nP -iTCP:8002 -sTCP:LISTEN"
  exit 1
fi

if [ "$CHOSEN" != "${PORT:-8002}" ]; then
  echo "端口 ${PORT:-8002} 已被占用（可能是只绑本机的旧实例），改用 ${CHOSEN}。"
fi

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)

echo ""
echo "中台服务启动中…"
echo "  本机：   http://127.0.0.1:${CHOSEN}/"
if [ -n "$IP" ]; then
  echo "  手机：   http://${IP}:${CHOSEN}/        （连同一个 WiFi）"
  echo "  控制台： http://${IP}:${CHOSEN}/admin"
else
  echo "  没识别到局域网 IP，查一下：ipconfig getifaddr en0"
fi
echo ""
echo "按 Ctrl-C 或关掉窗口停止服务。"
echo ""

# 聊天/查询 turn 的模型调用是 **Worker** 从数据库里领走的，API 只负责接纳（202）。
# 只起 API 的话，消息会一直停在 status=accepted：「发出去了，但永远没有回复」。
# 这里顺手把 Worker 一起拉起，并在退出时收掉它。
# 不去探测"是不是已经有一个 Worker"：多开 Worker 本身是安全的（租约 +
# advisory lock 保证同一条 turn 只有一个执行者），而"复用别的 Worker"会让
# 这个窗口关掉后服务看起来还活着、实际没人干活。
WORKER_PID=""
cleanup() {
  [ -n "$WORKER_PID" ] && kill "$WORKER_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

"$PY" -m scripts.run_worker &
WORKER_PID=$!
echo "Worker 已启动（pid ${WORKER_PID}）。"
echo ""

"$PY" -m scripts.serve_api --lan --port "$CHOSEN"
