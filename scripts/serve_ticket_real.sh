#!/usr/bin/env bash
# 真实账号登录联调：BeeFTV 本地密码校验 → 中台 ticket 会话。
#
# 和 serve_dev_login.sh 的区别：那条路接的是本地登录桩（任意非空账号密码都放行），
# 只用来验"登录界面能走通"。这条不接桩——密码由 BeeFTV 的 /api/auth/login 真校验，
# 错密码就是 401，账号也得是 BeeFTV 里真实存在的那一个。
#
# 用法：
#   bash scripts/serve_ticket_real.sh                 # 中台 8002 + BeeFTV 8004
#   HUB_PORT=8012 bash scripts/serve_ticket_real.sh   # 换端口
#   BEEF_ADDR=127.0.0.1:18204 bash scripts/serve_ticket_real.sh  # 指向别的 BeeFTV
#
# 前置：BeeFTV 库里要有能登录的账号（默认库 /.local/data 里是 user）。
# 停：Ctrl+C。脚本会连带把由它拉起的 BeeFTV 一起停掉；本来就在跑的 BeeFTV 不动。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-/opt/anaconda3/envs/demo/bin/python}"
[ -x "$PY" ] || PY=python3

BEEF_HOME="${BEEF_HOME:-/Users/latiao/Desktop/BeefTV}"
BEEF_DATA="${BEEF_DATA:-$BEEF_HOME/.local/data}"
BEEF_ADDR="${BEEF_ADDR:-127.0.0.1:8004}"
HUB_PORT="${HUB_PORT:-8002}"
PLATFORM_LOGIN_TENANT="${PLATFORM_LOGIN_TENANT:-default}"

# 种子写成与既有账号同名：Seed 遇到同名会跳过，因此不会往正式库里加演示账号。
# 不设这个变量时会回落到 auth.DemoSeedSpec，凭空多出 demo-admin / demo-user。
BEEF_SEED="${BEEF_SEED:-user:123456:user}"

started_beef=0

beeftv_up() {
  curl -s -m 1 --noproxy '*' -o /dev/null "http://${BEEF_ADDR}/api/health" 2>/dev/null
}

cleanup() {
  if [ "$started_beef" = "1" ] && [ -n "${beef_pid:-}" ]; then
    kill "$beef_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if beeftv_up; then
  echo "[ticket] 复用已在运行的 BeeFTV：${BEEF_ADDR}"
else
  if [ ! -x "$BEEF_HOME/.local/bin/beeftv-server" ]; then
    echo "[ticket] 找不到 beeftv-server：$BEEF_HOME/.local/bin/beeftv-server" >&2
    exit 1
  fi
  echo "[ticket] 启动 BeeFTV：${BEEF_ADDR}（数据目录 ${BEEF_DATA}）"
  # 必须从 BeeFTV 仓库根启动：它按当前目录找 plugin-packages，找不到会直接退出。
  (
    cd "$BEEF_HOME"
    exec env HOME="${HOME:-/Users/latiao}" \
      CANVAS_BACKEND_DATA_DIR="$BEEF_DATA" \
      DATABASE_URL="$BEEF_DATA/open_ai_canvas.db" \
      CANVAS_BACKEND_ADDR="$BEEF_ADDR" \
      CANVAS_AUTH_ENABLED=true \
      CANVAS_AUTH_COOKIE_SECURE=false \
      CANVAS_OFFICIAL_PLUGIN_DIR="$BEEF_HOME/plugin-packages" \
      BEEFTV_SEED_USERS="$BEEF_SEED" \
      NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
      "$BEEF_HOME/.local/bin/beeftv-server"
  ) > /tmp/beeftv-ticket.log 2>&1 &
  beef_pid=$!
  started_beef=1

  if ! curl -s -m 1 --noproxy '*' --retry 30 --retry-connrefused --retry-delay 1 \
       -o /dev/null "http://${BEEF_ADDR}/api/health"; then
    echo "[ticket] BeeFTV 没起来，看上面的日志" >&2
    exit 1
  fi
fi

# AUTH_DEV_COMPAT=true 只影响治理开关（工具策略 / 结果信封 / 异步查询保持关闭），
# 不放松身份：ticket 模式下中台只认自己的会话，请求头改不动身份。
AUTH_MODE=ticket \
AUTH_DEV_COMPAT=true \
AUTH_COOKIE_SECURE=false \
BEEFTV_API_BASE="http://${BEEF_ADDR}/api" \
PLATFORM_LOGIN_TENANT="$PLATFORM_LOGIN_TENANT" \
AUTH_ALLOWED_ORIGINS="http://127.0.0.1:${HUB_PORT},http://localhost:${HUB_PORT}" \
  "$PY" -m scripts.serve_dev --local --port "$HUB_PORT" "$@"
