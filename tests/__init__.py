"""项目测试包，确保标准 unittest 发现及 tests.* 夹具导入指向本仓库。

**在这里钉住与 CI 等价的环境默认值**（`.github/workflows/ci.yml` 的 env 段）。
原因：`core/config.py` 在 `APP_ENV != production` 时会读仓库里的 `.env`，而本机 `.env`
是 ticket 模式（会打开 `tool_policy_enforced` / `result_envelope_v2`）。不钉住的话，
同一套用例在本机跑会因为"身份仓储未就绪 503"和"政策强制拒绝无绑定调用"整批变红，
看起来像代码坏了，其实只是环境不同。

`setdefault` 而不是直接赋值：显式导出的环境变量（例如专门验证 ticket 模式时
`export AUTH_MODE=ticket`）仍然优先。
"""
import os

# 必须早于任何 `app.*` 导入：`load_dotenv` 不会覆盖已存在的环境变量。
os.environ.setdefault('APP_ENV', 'test')
os.environ.setdefault('AUTH_MODE', 'off')
os.environ.setdefault('API_RATE_LIMIT_ENABLED', 'false')
os.environ.setdefault('TENANT_BUDGET_ENABLED', 'false')
os.environ.setdefault('MCP_SERVERS', '[]')
# 不钉引擎。生产默认是 Pi，用例跟同一默认。子进程若要覆盖，自己写
# DEFAULT_RUNTIME_KIND，不要指望本进程偷偷改回旧引擎。
# PG_TEST_DSN 故意不设默认值：`reset_db()` 会 TRUNCATE，指错库就是数据事故。
# 必须由调用方显式给出（库名含 test），否则用例按 SkipTest 跳过。
