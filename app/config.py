import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / '.env')

@dataclass(frozen=True)
class Settings:
    llm_key: str = os.getenv('LLM_API_KEY', '')
    llm_model: str = os.getenv('LLM_MODEL', '')
    # 整理报告阶段的模型。空值时沿用 LLM_MODEL。
    # 该阶段要读完整正文并产出结构化报告，往往是耗时大头，可单独换更快的模型。
    llm_compose_model: str = os.getenv('LLM_COMPOSE_MODEL', '') or os.getenv('LLM_MODEL', '')
    # 解析阶段关闭模型推理。实测多轮场景 4.4s → 2.3s（约 2 倍），
    # 规划阶段的计划质量不受影响（5/5）。耗时几乎全在推理 token 上，与输入长度无关。
    llm_parse_disable_thinking: bool = (
        os.getenv('LLM_PARSE_DISABLE_THINKING', 'false').lower() == 'true')
    llm_base_url: str = os.getenv('LLM_BASE_URL', '')
    # 检索走 DeepSeek 官方 Search/Fetch API。这个 key 与 LLM_BASE_URL 指向的网关
    # 账号**不是一回事**：api.deepseek.com 只认平台自己的 key，不做兜底回退，
    # 否则配错了只会在第一次检索时 401。
    deepseek_api_key: str = os.getenv('DEEPSEEK_API_KEY', '').strip()
    # 接入的 MCP Server 标识；工具身份用它组成 mcp:<server_id>:<name>。
    # 目前只接一个 Server，字段先留出来，多 Server 时按配置注册即可。
    mcp_server_id: str = os.getenv('MCP_SERVER_ID', 'publishing')
    deepseek_base_url: str = os.getenv('DEEPSEEK_BASE_URL',
                                       'https://api.deepseek.com').rstrip('/')
    max_pages: int = min(5, max(1, int(os.getenv('MAX_PAGES', '5'))))
    # 单篇正文上限。必须和 max_total_chars 搭配看：总量 ÷ 单篇上限 = 能装下几个来源。
    # 单篇设得和总量一样大时，第一个来源就把预算吃光，报告退化成单一来源。
    max_source_chars: int = max(500, int(os.getenv('MAX_SOURCE_CHARS', '3000')))
    # 送进整理报告阶段的总字符预算。它直接决定那次模型调用有多慢——输入越长，
    # 产出结构化报告越久，超时与校验失败的概率也越高。
    max_total_chars: int = max(1000, int(os.getenv('MAX_TOTAL_CHARS', '10000')))
    # 单次模型调用的硬超时（秒）。整理报告一次尝试可能上百秒；没有上限时一旦卡住，
    # 会一直耗到整个任务 300s 的预算。超时即失败、不重试（重试只会把耗时翻倍）。
    llm_attempt_timeout: float = float(os.getenv('LLM_ATTEMPT_TIMEOUT', '180'))
    artifact_dir: Path = PROJECT_ROOT / os.getenv('ARTIFACT_DIR', 'artifacts')
    # 数据库：**只支持 PostgreSQL**（SQLite 实现已删除）。仓储接口见
    # app/persistence.py；留空会在 open_store() 里直接报错，不静默退回本地文件库。
    database_url: str = os.getenv('DATABASE_URL', '').strip()
    # PostgreSQL 连接池大小。一个进程最多同时占用这么多条连接：SSE 写入、
    # 后台作业、接口查询各自借各自的，不再被一把进程内的锁串行化。
    # 注意别超过服务端的 max_connections ÷ 副本数。
    db_pool_size: int = max(1, int(os.getenv('DB_POOL_SIZE', '4')))
    # 从池里借连接最多等多久（秒）。等不到就报错，而不是无限期挂住一个请求。
    db_pool_timeout: float = max(1.0, float(os.getenv('DB_POOL_TIMEOUT', '10')))
    # 用户画像文件（Markdown，人可读可改）。含电话等敏感信息，已在 .gitignore 排除。
    profile_path: Path = PROJECT_ROOT / os.getenv('PROFILE_PATH', 'profile/USER.md')
    deploy_enabled: bool = os.getenv('DEPLOY_ENABLED', 'false').lower() == 'true'
    # ---------- 执行预算（文档 §7：步数/时长/重规划次数必须可配） ----------
    # 计划最多几步。以前是 nodes.py 里的常量，改一次要动代码、也没法按部署环境调。
    max_steps: int = max(1, int(os.getenv('MAX_STEPS', '8')))
    # 单次任务总预算（秒）。注意要 > llm_attempt_timeout × 重试次数，
    # 否则外层会先砍断内层重试，重试等于白写。
    task_timeout: float = max(30.0, float(os.getenv('TASK_TIMEOUT', '300')))
    # LangGraph 的递归上限：每个步骤算一跳，留出余量。
    graph_recursion_limit: int = max(10, int(
        os.getenv('GRAPH_RECURSION_LIMIT', '30')))
    # run 租约时长（秒）。执行期间由看守协程续租；设得太短，别的执行者会
    # 误判"这个人已经死了"并接手同一份计划。
    run_lease_seconds: float = max(15.0, float(
        os.getenv('RUN_LEASE_SECONDS', '120')))
    # 创建任务幂等键的有效期（秒）。默认 24 小时：覆盖重试窗口，又不会让
    # 客户端某天重用旧键时拿到一个陈年任务。
    idempotency_ttl_seconds: float = max(60.0, float(
        os.getenv('IDEMPOTENCY_TTL_SECONDS', '86400')))

    # ---------- 身份：外部 token 换本地主体（app/auth.py） ----------
    # off      = 不介入，保持单用户行为（默认，本地开发与既有测试不受影响）
    # optional = 带了 token 就验证，没带就匿名（灰度期）
    # token    = 必须带合法 token，否则 401（正式启用）
    auth_mode: str = os.getenv('AUTH_MODE', 'off').strip().lower()
    # 只作标签，落 identities.provider。同一平台同一主体不会重复建号。
    auth_provider: str = os.getenv('AUTH_PROVIDER', 'oidc').strip() or 'oidc'
    # 平台 userinfo 端点。留空时 token 模式无法工作（会明确报 401 而不是静默放行）
    auth_userinfo_url: str = os.getenv('AUTH_USERINFO_URL', '').strip()
    # header = Authorization: Bearer；query = ?access_token=（部分平台只认这个）
    auth_token_in: str = os.getenv('AUTH_TOKEN_IN', 'header').strip().lower()
    # 平台返回字段的映射。支持 a.b 取值，适配把 uid 包在 data 里的平台。
    auth_subject_field: str = os.getenv('AUTH_SUBJECT_FIELD', 'sub').strip() or 'sub'
    auth_email_field: str = os.getenv('AUTH_EMAIL_FIELD', 'email').strip() or 'email'
    auth_name_field: str = os.getenv('AUTH_NAME_FIELD', 'name').strip() or 'name'
    # 凭证校验结果的进程内缓存（秒）。0 = 每次都问平台。
    # 注意这是**性能必需**而不是优化：一次任务有几十个请求，全部打平台会被限流。
    auth_cache_ttl: float = max(0.0, float(os.getenv('AUTH_CACHE_TTL', '60')))
    auth_timeout: float = max(1.0, float(os.getenv('AUTH_TIMEOUT', '5')))
    # 浏览器侧凭证：EventSource 与 <a download> 发不了自定义头，只能靠 cookie
    auth_cookie_name: str = os.getenv('AUTH_COOKIE_NAME', 'agent_token').strip()
    # 本地 http 调试时浏览器会拒收 secure cookie，此时设 false；线上必须 true
    auth_cookie_secure: bool = (
        os.getenv('AUTH_COOKIE_SECURE', 'true').strip().lower() == 'true')
    # cookie 存活时长（秒），默认 8 小时。**不要和 AUTH_CACHE_TTL 混用**：
    # 缓存是"多久问一次平台"，cookie 是"浏览器多久带一次凭证"，两者量级差两个数量级。
    auth_cookie_max_age: int = max(60, int(
        os.getenv('AUTH_COOKIE_MAX_AGE', '28800')))
    # ---------- 对象权限 ----------
    # owner_user_id='default' 的存量数据是否对所有登录用户可见。
    # 单用户升级期必须为 true；有第二个真实用户后必须改成 false。
    policy_legacy_visible: bool = (
        os.getenv('POLICY_LEGACY_VISIBLE', 'true').strip().lower() == 'true')

settings = Settings()

def check_runtime_keys():
    missing = []
    if not settings.deepseek_api_key:
        missing.append('DEEPSEEK_API_KEY')
    if not settings.llm_key or settings.llm_key == 'replace-me':
        missing.append('LLM_API_KEY')
    if not settings.llm_model or settings.llm_model.startswith('replace-'):
        missing.append('LLM_MODEL')
    if missing:
        raise RuntimeError('请在 .env 设置：' + ', '.join(missing))
