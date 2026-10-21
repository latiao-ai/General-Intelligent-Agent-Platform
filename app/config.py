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
    # 数据库：留空用本地 SQLite；填 postgresql://... 则全部走 PostgreSQL。
    # 仓储接口见 app/persistence.py，两种实现互换，业务代码不感知。
    database_url: str = os.getenv('DATABASE_URL', '').strip()
    # PostgreSQL 连接池大小。一个进程最多同时占用这么多条连接：SSE 写入、
    # 后台作业、接口查询各自借各自的，不再被一把进程内的锁串行化。
    # 注意别超过服务端的 max_connections ÷ 副本数。
    db_pool_size: int = max(1, int(os.getenv('DB_POOL_SIZE', '4')))
    # 从池里借连接最多等多久（秒）。等不到就报错，而不是无限期挂住一个请求。
    db_pool_timeout: float = max(1.0, float(os.getenv('DB_POOL_TIMEOUT', '10')))
    # SQLite 持久化路径（DATABASE_URL 为空时使用）。改这里也要同步 .gitignore。
    db_path: Path = PROJECT_ROOT / os.getenv('DB_PATH', 'agent.db')
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
