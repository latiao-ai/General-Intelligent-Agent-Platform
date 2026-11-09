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
# 生产未设置 DEFAULT_RUNTIME_KIND 时，新建业务 run 走 Pi。
# 这套用例里的图路径（规划、修订、补偿、观察）仍在排空，进程内钉回 langgraph，
# 避免整库测试突然改去拉 Node。验证「默认是 pi」的用例用 override_settings，
# 或在子进程环境里显式写 DEFAULT_RUNTIME_KIND=pi（不要继承这个钉）。
os.environ.setdefault('DEFAULT_RUNTIME_KIND', 'langgraph')
# PG_TEST_DSN 故意不设默认值：`reset_db()` 会 TRUNCATE，指错库就是数据事故。
# 必须由调用方显式给出（库名含 test），否则用例按 SkipTest 跳过。
