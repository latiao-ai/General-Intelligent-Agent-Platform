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

# 预检：连不上库就直接失败。
#
# 没有这一步时，连不上 PostgreSQL 的机器上会有一批用例自行跳过，脚本最后仍然
# 退出 0 —— 少了 249 个用例却看起来"通过"。这种"静默通过"比失败坏得多：它让
# 人以为改动已经验证过了。
#
# 用真实驱动连一次，而不是 pg_isready 之类的外部命令：判断的就是用例将要用的
# 那条连接串和那个驱动，库不存在、认证失败、端口没开都能一次问清。
echo "PG_TEST_DSN=$PG_TEST_DSN"
DB_ERROR=$("$PYTHON" "$PWD/scripts/check_test_db.py" "$PG_TEST_DSN" 2>&1) && DB_OK=1 || DB_OK=0
if [ "$DB_OK" != "1" ]; then
  cat >&2 <<EOF
[run_tests] 连不上测试数据库，已中止（不跑测试，避免"少跑一批用例却显示通过"）。

  目标：$PG_TEST_DSN
  原因：$DB_ERROR

测试需要 PostgreSQL（用例会真实建表、TRUNCATE 业务表）。请先准备一个实例，
再用一次性测试库名（库名必须含 test）指给脚本，例如：

  # 方式一：本机已有 PostgreSQL
  createdb -h 127.0.0.1 -p 5432 agent_platform_test
  PG_TEST_DSN='postgresql://\$USER@127.0.0.1:5432/agent_platform_test' scripts/run_tests.sh

  # 方式二：临时起一个（端口与密码按需改）
  docker run --rm -d --name giap-test-pg -p 5432:5432 \\
    -e POSTGRES_PASSWORD=test-only-password -e POSTGRES_DB=agent_platform_test postgres:18

  # 方式三：已经跑在别处
  PG_TEST_DSN='postgresql://user:pass@host:5432/agent_test' scripts/run_tests.sh
EOF
  exit 2
fi
"$PYTHON" -m ruff check src scripts tests
"$PYTHON" -m mypy
"$PYTHON" -m unittest discover -s tests -t . "$@"
