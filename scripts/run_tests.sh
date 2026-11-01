#!/usr/bin/env bash
# 本地跑测试用这个脚本：环境与 .github/workflows/ci.yml 完全一致。
#
# 为什么需要它：仓库根的 .env 是 ticket 模式，`src/webpage_agent/core/config.py` 在非 production 下会读它，
# 于是直接 `python -m unittest` 会拿 ticket 模式去跑按 AUTH_MODE=off 写的用例
# （一批 503、一批政策拒绝），看起来像代码坏了。CI 一直是绿的，因为 CI 显式钉了这些值。
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-/opt/anaconda3/envs/demo/bin/python}"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
export APP_ENV=test
export AUTH_MODE=off
export DB_AUTO_MIGRATE=true
export API_RATE_LIMIT_ENABLED=false
export TENANT_BUDGET_ENABLED=false
export MCP_SERVERS='[]'
# 测试库必须是一次性的：用例会 TRUNCATE 全部业务表。
export PG_TEST_DSN="${PG_TEST_DSN:-postgresql://${USER}@127.0.0.1:5432/agent_platform_test}"

case "$PG_TEST_DSN" in
  *test*) ;;
  *) echo "PG_TEST_DSN 的库名必须含 test（用例会 TRUNCATE）：$PG_TEST_DSN" >&2; exit 2 ;;
esac

echo "PG_TEST_DSN=$PG_TEST_DSN"
"$PYTHON" -m ruff check src scripts tests
"$PYTHON" -m mypy
"$PYTHON" -m unittest discover -s tests -t . "$@"
