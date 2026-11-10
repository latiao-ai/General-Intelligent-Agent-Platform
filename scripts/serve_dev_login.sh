#!/usr/bin/env bash
# 本地开发：以「真登录」模式起中台（ticket 模式 + 本地账号登录桩）。
#
#   bash scripts/serve_dev_login.sh              # 中台 8002，登录桩 8099
#   HUB_PORT=8010 STUB_PORT=8099 bash scripts/serve_dev_login.sh
#
# 和 `python -m scripts.serve_api` 的区别：AUTH_MODE 从 off 换成 ticket，
# 于是页面左下角账号卡片会出现「登录中台 / 退出登录 / 切换账号」，
# 退出登录会真的撤销服务端会话（AUTH_MODE=off 时没有会话可撤销，所以退不了）。
#
# 前提：先跑一次 `python -m scripts.dev_login_bind`，把登录身份绑到本机单用户，
#       否则登录后是个空账号，看不到已有会话。
#
# 账号密码随便填（任意非空），本地登录桩不做校验。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-.venv-pi-test/bin/python}"
if [ ! -x "$PY" ]; then
  PY=python3
fi

HUB_PORT="${HUB_PORT:-8002}"
STUB_PORT="${STUB_PORT:-8099}"

"$PY" scripts/dev_login_stub.py --port "$STUB_PORT" &
STUB=$!
cleanup() { kill "$STUB" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# BEEFTV_API_BASE 必须带路径段（中台会拼 /auth/login），只给 host 会被判定为未启用
AUTH_MODE=ticket \
AUTH_DEV_COMPAT=true \
AUTH_COOKIE_SECURE=false \
BEEFTV_API_BASE="http://127.0.0.1:${STUB_PORT}/api" \
PLATFORM_LOGIN_TENANT=default \
AUTH_ALLOWED_ORIGINS="http://127.0.0.1:${HUB_PORT},http://localhost:${HUB_PORT}" \
  "$PY" -m scripts.serve_api --port "$HUB_PORT" "$@"
