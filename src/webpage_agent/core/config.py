import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

def _discover_project_root() -> Path:
    """向上找 pyproject.toml 作为仓库根标记。

    不按 `__file__` 的固定层数算：文件在 src/webpage_agent/core/ 下，层数一变就全错
    （`.env` 读不到、artifacts 目录跑偏）。装成 wheel 后没有 pyproject.toml，退回上三级。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / 'pyproject.toml').is_file():
            return parent
    return here.parents[3]


PROJECT_ROOT = _discover_project_root()
# Production takes secrets only from the deployment environment.
if os.getenv('APP_ENV', 'development').strip().lower() != 'production':
    load_dotenv(PROJECT_ROOT / '.env')


def _env_flag(name: str, default: str = 'false') -> bool:
    return os.getenv(name, default).strip().lower() == 'true'


# 画布任务的默认系统提示。Pi 生产入口必须带上，否则模型不知道何时停手，
# 同一检索会换关键词反复调用。PI_SYSTEM_PROMPT 未设置时用这段；设为空字符串则关闭。
DEFAULT_PI_SYSTEM_PROMPT = (
    '你是画布任务执行助手。执行规则：'
    '1) 每个检索意图只调用一次搜索工具，禁止对同一问题换关键词重复搜索；'
    '2) 已有信息足以回答或完成当前要求时立即给出最终答复，不要为了再确认而继续调用工具；'
    '3) 工具失败时最多重试一次，仍失败就说明原因并停止；'
    '4) 缺少对象、字段或用户必须做的选择时，调用 request_input 停下来问人，'
    '不要猜测、不要空转重试；'
    '5) 写操作只使用宿主给出的对象标识，写完后等待回读核验，不要自行重复写入。'
)


def pi_system_prompt_from_env() -> str:
    """未设置环境变量时用画布默认规则；显式设为空字符串表示不注入。"""
    raw = os.environ.get('PI_SYSTEM_PROMPT')
    if raw is None:
        return DEFAULT_PI_SYSTEM_PROMPT
    return raw


def governance_enabled(auth_mode: str, dev_compat: bool, explicit: bool) -> bool:
    """票模式且没有开发兼容时，政策、结果信封、异步查询一起打开。

    本地单用户（AUTH_MODE=off）仍看各自的环境变量，默认关闭。
    AUTH_DEV_COMPAT=true 时也看环境变量，只给本机签票联调用。
    """
    if str(auth_mode or '').strip().lower() == 'ticket' and not dev_compat:
        return True
    return bool(explicit)


if os.getenv('APP_ENV', 'development').strip().lower() not in ('development', 'test', 'production'):
    raise ValueError('APP_ENV must be development, test or production')

_AUTH_MODE = os.getenv('AUTH_MODE', 'off').strip().lower()
_AUTH_DEV_COMPAT = _env_flag('AUTH_DEV_COMPAT')


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv('APP_ENV', 'development').strip().lower()
    db_auto_migrate: bool = _env_flag('DB_AUTO_MIGRATE',
        'false' if os.getenv('APP_ENV', '').strip().lower() == 'production' else 'true')
    api_rate_limit_enabled: bool = _env_flag('API_RATE_LIMIT_ENABLED',
        'true' if os.getenv('APP_ENV', '').strip().lower() == 'production' else 'false')
    api_rate_limit_per_minute: int = max(1, int(os.getenv('API_RATE_LIMIT_PER_MINUTE', '120')))
    auth_rate_limit_per_minute: int = max(1, int(os.getenv('AUTH_RATE_LIMIT_PER_MINUTE', '10')))
    user_rate_limit_per_minute: int = max(1, int(os.getenv('USER_RATE_LIMIT_PER_MINUTE', '60')))
    tenant_budget_enabled: bool = _env_flag('TENANT_BUDGET_ENABLED',
        'true' if os.getenv('APP_ENV', '').strip().lower() == 'production' else 'false')
    tenant_daily_provider_requests: int = max(1, int(os.getenv('TENANT_DAILY_PROVIDER_REQUESTS', '1000')))
    # Micro currency units; prices are operator supplied upper bounds, not live quotes.
    tenant_daily_cost_micros: int = int(os.getenv('TENANT_DAILY_COST_MICROS', '10000000'))
    llm_input_price_micros_per_million: int = int(os.getenv('LLM_INPUT_PRICE_MICROS_PER_MILLION', '0'))
    llm_output_price_micros_per_million: int = int(os.getenv('LLM_OUTPUT_PRICE_MICROS_PER_MILLION', '0'))
    search_request_cost_micros: int = int(os.getenv('SEARCH_REQUEST_COST_MICROS', '0'))
    maintenance_interval_seconds: float = max(10.0, float(os.getenv('MAINTENANCE_INTERVAL_SECONDS', '3600')))
    prompt_timezone: str = os.getenv('PROMPT_TIMEZONE', 'Asia/Shanghai').strip() or 'Asia/Shanghai'
    # Pi 系统提示。默认是画布停手/少重复检索规则；PI_SYSTEM_PROMPT 可覆盖。
    pi_system_prompt: str = pi_system_prompt_from_env()
    # 写工具扩展开关。默认关闭。仅 PI_WRITE_TOOLS_ENABLED=true 时，
    # PI_WRITE_TOOL_KEYS 才会并入写白名单，并打审计日志。
    # 只配了键、没开这道门：键被忽略并告警，不会静默扩权。
    pi_write_tools_enabled: bool = _env_flag('PI_WRITE_TOOLS_ENABLED', 'false')
    llm_key: str = os.getenv('LLM_API_KEY', '')
    llm_model: str = os.getenv('LLM_MODEL', '')
    # 解析阶段关闭模型推理。实测多轮场景 4.4s → 2.3s（约 2 倍），
    # 规划阶段的计划质量不受影响（5/5）。耗时几乎全在推理 token 上，与输入长度无关。
    llm_parse_disable_thinking: bool = (
        os.getenv('LLM_PARSE_DISABLE_THINKING', 'false').lower() == 'true')
    llm_base_url: str = os.getenv('LLM_BASE_URL', '')
    # 检索走 DeepSeek 官方 Search/Fetch API。这个 key 与 LLM_BASE_URL 指向的网关
    # 账号**不是一回事**：api.deepseek.com 只认平台自己的 key，不做兜底回退，
    # 否则配错了只会在第一次检索时 401。
    deepseek_api_key: str = os.getenv('DEEPSEEK_API_KEY', '').strip()
    # 单项 MCP_* 配置时的默认服务端 id；配了 MCP_SERVERS 后以数组里的 id 为准。
    mcp_server_id: str = os.getenv('MCP_SERVER_ID', 'publishing')
    # ---------- 标准 MCP Tasks 原语（experimental，默认关闭） ----------
    # 工具自己声明 execution.taskSupport 时，按协议任务通道调用（callTool 带 task，
    # 用 tasks/get 轮询）。**默认关闭**：该 API 已标弃用，SEP-1686 也已移出 MCP
    # 规范，mcp 2.0 会移除；只有明确要对接声明了 taskSupport 的服务端时才打开。
    mcp_tasks_enabled: bool = _env_flag('MCP_TASKS_ENABLED')
    # 声明 optional 的工具是否也改走任务通道。默认 false：保持原有同步行为。
    # 注意 required 也受上面的 MCP_TASKS_ENABLED 总开关约束——总开关关着时，
    # 声明 required 的工具仍然走普通调用（宁可发普通请求，也不悄悄启用弃用通道）。
    mcp_tasks_optional_async: bool = _env_flag('MCP_TASKS_OPTIONAL_ASYNC')
    # 请求服务端保留任务的时长（毫秒）。到期后 tasks/get 失效，故须长于预期作业。
    mcp_tasks_ttl_ms: int = max(1000, int(os.getenv('MCP_TASKS_TTL_MS', '3600000')))
    deepseek_base_url: str = os.getenv('DEEPSEEK_BASE_URL',
                                       'https://api.deepseek.com').rstrip('/')
    max_pages: int = min(3, max(1, int(os.getenv('MAX_PAGES', '3'))))
    # 单篇正文上限。必须和 max_total_chars 搭配看：总量 ÷ 单篇上限 = 能装下几个来源。
    # 单篇设得和总量一样大时，第一个来源就把预算吃光。
    max_source_chars: int = max(500, int(os.getenv('MAX_SOURCE_CHARS', '3000')))
    # 送进模型的总字符预算。输入越长，调用越慢，超时与校验失败的概率也越高。
    max_total_chars: int = max(1000, int(os.getenv('MAX_TOTAL_CHARS', '6000')))
    # 单次模型调用的硬超时（秒）。没有上限时一旦卡住，会一直耗到整个任务
    # 300s 的预算。超时即失败、不重试（重试只会把耗时翻倍）。
    llm_attempt_timeout: float = float(os.getenv('LLM_ATTEMPT_TIMEOUT', '180'))
    # 完整请求预算：包含 system、消息、工具 schema 与序列化开销。
    # 按实际模型窗口配置；输入使用 UTF-8 字节上界保守估算，不依赖联网分词器。
    llm_context_window: int = max(1, int(os.getenv('LLM_CONTEXT_WINDOW', '32768')))
    llm_input_budget: int = max(1, int(os.getenv('LLM_INPUT_BUDGET', '24576')))
    llm_output_tokens: int = max(1, int(os.getenv('LLM_OUTPUT_TOKENS', '4096')))
    llm_context_margin: int = max(0, int(os.getenv('LLM_CONTEXT_MARGIN', '2048')))
    llm_retry_max_delay: float = max(0.0, min(30.0, float(
        os.getenv('LLM_RETRY_MAX_DELAY', '2'))))
    # All host platforms share this scene budget; full request limits still apply.
    platform_context_max_chars: int = max(800, min(64000, int(
        os.getenv('PLATFORM_CONTEXT_MAX_CHARS', '12000'))))
    artifact_dir: Path = PROJECT_ROOT / os.getenv('ARTIFACT_DIR', 'artifacts')
    # 数据库：**只支持 PostgreSQL**（SQLite 实现已删除）。仓储接口见
    # webpage_agent/store/persistence.py；留空会在 open_store() 里直接报错，不静默退回本地文件库。
    database_url: str = os.getenv('DATABASE_URL', '').strip()
    # PostgreSQL 连接池大小。一个进程最多同时占用这么多条连接：SSE 写入、
    # 后台作业、接口查询各自借各自的，不再被一把进程内的锁串行化。
    # 一次运行只在写账本、写事件时借连接，不是全程占着；但池 4 配并发 16 时，
    # 一次突发写入会排到等待超时。默认 8 是给并发 16 留的写入余量，
    # 不是按「一条运行一条连接」换算出来的。
    # 注意别超过服务端的 max_connections ÷ 副本数。
    db_pool_size: int = max(1, int(os.getenv('DB_POOL_SIZE', '8')))
    # 从池里借连接最多等多久（秒）。等不到就报错，而不是无限期挂住一个请求。
    db_pool_timeout: float = max(1.0, float(os.getenv('DB_POOL_TIMEOUT', '10')))
    # 用户画像文件（Markdown，人可读可改）。含电话等敏感信息，已在 .gitignore 排除。
    profile_path: Path = PROJECT_ROOT / os.getenv('PROFILE_PATH', 'profile/USER.md')
    deploy_enabled: bool = os.getenv('DEPLOY_ENABLED', 'false').lower() == 'true'
    # ---------- 执行预算（步数/时长/重规划次数必须可配） ----------
    # 计划最多几步。以前是 nodes.py 里的常量，改一次要动代码、也没法按部署环境调。
    max_steps: int = max(1, int(os.getenv('MAX_STEPS', '8')))
    # 单次任务总预算（秒）。注意要 > llm_attempt_timeout × 重试次数，
    # 否则外层会先砍断内层重试，重试等于白写。
    task_timeout: float = max(30.0, float(os.getenv('TASK_TIMEOUT', '300')))
    # 规划节点循环的步数上限（测试驱动与历史计划路径）。每个步骤算一跳，留出余量。
    # 一次工具调用就要 2 跳（run_step + observe_result），再叠加 persist_plan /
    # decide_next / verify_completion / finalize，30 跳连 8 步的计划都不够跑。
    # 按 max_steps 折算，一个步骤给 8 跳（含修订引起的重跑）。
    graph_recursion_limit: int = max(40, int(
        os.getenv('GRAPH_RECURSION_LIMIT', str(max(40, max_steps * 8)))))
    # run 租约时长（秒）。执行期间由看守协程续租；设得太短，别的执行者会
    # 误判"这个人已经死了"并接手同一份计划。
    run_lease_seconds: float = max(15.0, float(
        os.getenv('RUN_LEASE_SECONDS', '120')))
    # stdio 子进程在同一进程内复用，空闲这么多秒后再关掉。
    # 0 表示每次调用结束就关，退回旧行为。对方只把任务放在进程内存里时，
    # 进程重启后仍然接不上，那要靠对方自己的可查询任务号。
    stdio_session_idle_seconds: float = float(
        os.getenv('STDIO_SESSION_IDLE_SECONDS', '600'))
    # 创建任务幂等键的有效期（秒）。默认 24 小时：覆盖重试窗口，又不会让
    # 客户端某天重用旧键时拿到一个陈年任务。
    idempotency_ttl_seconds: float = max(60.0, float(
        os.getenv('IDEMPOTENCY_TTL_SECONDS', '86400')))
    # ---------- 运行中补充输入（《智能体中台说明书》） ----------
    # 功能开关：关掉时接口返回 404，前端维持"新消息=新任务"的旧行为。
    # 默认开：本机单用户场景，补充要求比"打断重开"更符合直觉。
    run_inputs_enabled: bool = (
        os.getenv('RUN_INPUTS_ENABLED', 'true').strip().lower() == 'true')
    # 一条补充的字符上限与待处理条数上限。上限在**接受事务内**检查：
    # 事后限流没法阻止无限积压，只会让用户以为发出去了。
    run_input_max_chars: int = max(200, int(os.getenv('RUN_INPUT_MAX_CHARS', '4000')))
    run_input_max_pending: int = max(1, int(os.getenv('RUN_INPUT_MAX_PENDING', '20')))
    # 一次运行最多因补充要求修订几次计划。超过就停下来等用户显式继续——
    # 无限修订等于让一次任务永远不结束，而且每一次都要一次模型调用。
    run_max_revisions: int = max(1, int(os.getenv('RUN_MAX_REVISIONS', '3')))

    # ---------- 会话聊天（第十一阶段：正式会话入口） ----------
    # 显式能力开关，逐项开放。默认只开「聊天 + 查询」；创建业务任务、修改、
    # 暂停、取消等控制能力需显式打开并经正式链路验证后启用。
    session_chat_enabled: bool = (
        os.getenv('SESSION_CHAT_ENABLED', 'true').strip().lower() == 'true')
    session_query_enabled: bool = (
        os.getenv('SESSION_QUERY_ENABLED', 'true').strip().lower() == 'true')
    # 业务任务创建/控制：写能力未补验前保持关闭。
    session_task_create_enabled: bool = (
        os.getenv('SESSION_TASK_CREATE_ENABLED', 'false').strip().lower() == 'true')
    session_task_control_enabled: bool = (
        os.getenv('SESSION_TASK_CONTROL_ENABLED', 'false').strip().lower() == 'true')
    # 聊天 turn 与业务 run 分别预留的并发额度（避免长业务阻塞聊天）。
    session_chat_concurrency: int = max(1, int(
        os.getenv('SESSION_CHAT_CONCURRENCY', '2')))
    # 聊天 turn 的租约时长（秒）。聊天执行会起 Node 子进程 + 模型调用，租约需
    # 覆盖一次完整答复；过期 running 会被新执行者重新领取（递增执行代次）。
    session_turn_lease_seconds: float = max(15.0, float(
        os.getenv('SESSION_TURN_LEASE_SECONDS', '120')))
    # ---------- 结果观察与自主决策（《智能体中台说明书》） ----------
    # 功能开关：关掉时观察节点只做程序事实采集，不调用模型（等价于"按计划跑完"）。
    observe_enabled: bool = (
        os.getenv('OBSERVE_ENABLED', 'true').strip().lower() == 'true')
    # 一次运行最多做几次自主决策（模型判断下一步）。与用户补充修订**共享**运行预算，
    # 但分开计数：观察烧的是"想", 修订烧的是"改主意"，两者不能互相顶账。
    run_max_decisions: int = max(1, int(os.getenv('RUN_MAX_DECISIONS', '8')))
    # 有限修复次数：只对"确认未执行"的本地错误开放，避免把写失败也变成自动重试。
    run_max_repairs: int = max(0, int(os.getenv('RUN_MAX_REPAIRS', '2')))
    # 运行级 provider 请求上限：初始规划、观察决策、用户修订、结构化修复、最终回答
    # 全部计入（`structured_call` 的内部重试也算一次真实请求）。
    # 单进程并发从 8 提到 16 后，上游 429 大约翻倍，而每一次重试都吃这份预算。
    # 上限不跟着从 24 提到 32，正常重试会先把次数闸打满。
    # 次数闸保留：搜索与抓取不走模型、没有 token 可记，只靠 token 上限盖不住它们。
    run_max_provider_requests: int = max(4, int(
        os.getenv('RUN_MAX_PROVIDER_REQUESTS', '32')))
    # 单次运行的 token 上限（输入加输出）。默认一百万。
    # 真实用量要等流式响应的最后一个分片才知道，所以只能在下一次请求前按已记账的量拦，
    # 不能像日额度那样按最坏情况预留。超限走已有的 BudgetExceeded，停在 waiting_input。
    run_max_tokens: int = max(1, int(os.getenv('RUN_MAX_TOKENS', '1000000')))
    # 工具调用次数上限（含查询）。外部写操作另有账本与授权，不靠这个数管。
    run_max_tool_calls: int = max(4, int(
        os.getenv('RUN_MAX_TOOL_CALLS', '40')))
    # 连续"没有新进展"的容忍次数：相同指纹重复出现就停，而不是继续烧预算。
    run_max_no_progress: int = max(1, int(
        os.getenv('RUN_MAX_NO_PROGRESS', '2')))
    # 新建 run 的引擎。只影响**新建**任务；恢复读库里已存的 runtime_kind。
    # 未设置环境变量时默认 pi。历史值 langgraph 不再对应可执行引擎。
    default_runtime_kind: str = os.getenv('DEFAULT_RUNTIME_KIND', 'pi').strip().lower() or 'pi'

    # ---------- 身份（webpage_agent/api/auth.py / webpage_agent/governance/ticket_auth.py） ----------
    # off      = 不介入，保持单用户行为（默认，本地开发与既有测试不受影响）
    # optional = 有中台会话就按会话，没有就匿名（灰度期）
    # ticket   = **平台签票 → 中台换会话**。所有业务接口必须带有效会话，否则 401
    auth_mode: str = _AUTH_MODE
    # 票据有效期上限（秒）。票据是"一次性短期凭证"，不该活很久。
    auth_ticket_max_lifetime: int = max(5, int(
        os.getenv('AUTH_TICKET_MAX_LIFETIME', '60')))
    # 中台登录会话有效期（秒）。到期后由平台重新申请票据续期。
    auth_session_ttl: int = max(60, int(os.getenv('AUTH_SESSION_TTL', '7200')))
    auth_session_cookie: str = os.getenv('AUTH_SESSION_COOKIE',
                                         'agent_session').strip() or 'agent_session'
    # SPA 与中台不同源时用它（例如平台前端经同源代理访问，可不设）
    auth_cookie_samesite: str = (
        os.getenv('AUTH_COOKIE_SAMESITE', 'lax').strip().lower() or 'lax')
    # SSE 期间多久复查一次会话是否被撤销/过期（秒）
    auth_sse_recheck: float = max(5.0, float(
        os.getenv('AUTH_SSE_RECHECK', '20')))
    # 本地 http 调试时浏览器会拒收 secure cookie，此时设 false；线上必须 true
    auth_cookie_secure: bool = (
        os.getenv('AUTH_COOKIE_SECURE', 'true').strip().lower() == 'true')
    # ---------- 对象权限 ----------
    # owner_user_id='default' 的存量数据是否对所有登录用户可见。
    # 单用户升级期必须为 true；有第二个真实用户后必须改成 false。
    # 默认关闭：ticket 模式下若还让 default 数据对所有人可见，等于把存量历史
    # 公开。本地单用户（AUTH_MODE=off）想保留旧行为可显式设 true。
    policy_legacy_visible: bool = (
        os.getenv('POLICY_LEGACY_VISIBLE', 'false').strip().lower() == 'true')

    # ---------- 工具运行时（《智能体中台说明书》） ----------
    # 本地单用户三个都关着。AUTH_MODE=ticket 且未开 AUTH_DEV_COMPAT 时，
    # 三项在这里一起打开：不能只开票模式、却让政策或结果信封停在演示路径。
    # 显式写成 true 时，即使还是 off 模式也会打开对应的一环。
    #
    # RESULT_ENVELOPE_V2：结果信封 v2（schema_version=2）。开启后结果以
    # manifest + 分块的形式保存与受控读取，旧字段由兼容投影生成。
    result_envelope_v2: bool = governance_enabled(
        _AUTH_MODE, _AUTH_DEV_COMPAT, _env_flag('RESULT_ENVELOPE_V2'))
    # TOOL_POLICY_ENFORCED：工具政策强制执行。开启后模型只看得到被授权的
    # 工具，调用前重新校验主体/凭据/批准/额度，缺绑定直接拒绝。
    tool_policy_enforced: bool = governance_enabled(
        _AUTH_MODE, _AUTH_DEV_COMPAT, _env_flag('TOOL_POLICY_ENFORCED'))
    # ASYNC_TOOL_RUNTIME_ENABLED：异步长任务受理与跟踪。**不能只开这一半**：
    # 没有外部任务合同与跟踪作业时，受理的动作会永远停在等待态。
    async_tool_runtime_enabled: bool = governance_enabled(
        _AUTH_MODE, _AUTH_DEV_COMPAT, _env_flag('ASYNC_TOOL_RUNTIME_ENABLED'))

    # 结果存储：单条结果的体积上限与单次读取的分页上限。超出时结果标记为
    # incomplete 并给出原因——**不能无限扩容，也不能悄悄截断**。
    tool_result_max_bytes: int = max(4096, int(
        os.getenv('TOOL_RESULT_MAX_BYTES', str(4 * 1024 * 1024))))
    tool_result_page_limit: int = max(256, int(
        os.getenv('TOOL_RESULT_PAGE_LIMIT', '8192')))
    # 凭据解析：按绑定取密钥时允许的缓存秒数。0 = 每次都查库。
    credential_cache_ttl: float = max(0.0, float(
        os.getenv('CREDENTIAL_CACHE_TTL', '30')))
    # 外部任务：轮询退避（秒）、单次查询超时、等待总期限。
    external_poll_base_seconds: float = max(1.0, float(
        os.getenv('EXTERNAL_POLL_BASE_SECONDS', '5')))
    external_poll_max_seconds: float = max(5.0, float(
        os.getenv('EXTERNAL_POLL_MAX_SECONDS', '120')))
    external_poll_timeout: float = max(5.0, float(
        os.getenv('EXTERNAL_POLL_TIMEOUT', '30')))
    external_wait_deadline: float = max(60.0, float(
        os.getenv('EXTERNAL_WAIT_DEADLINE', '3600')))
    # 轮询作业的并发上限：查询远端会占住连接，不能无限并发。
    external_poll_concurrency: int = max(1, int(
        os.getenv('EXTERNAL_POLL_CONCURRENCY', '4')))
    # 批准：待确认批准的有效期（秒）与单次批准可覆盖的动作数。
    approval_ttl_seconds: int = max(60, int(
        os.getenv('APPROVAL_TTL_SECONDS', '900')))
    approval_max_actions: int = max(1, int(
        os.getenv('APPROVAL_MAX_ACTIONS', '20')))
    # 为 true 时，票模式不再强制打开三环，启动检查也跳过。只用于本机签票联调。
    auth_dev_compat: bool = _AUTH_DEV_COMPAT
    auth_allowed_origins: tuple = tuple(
        item.strip() for item in os.getenv('AUTH_ALLOWED_ORIGINS', '').split(',')
        if item.strip())
    async_contracts_file: str = os.getenv('ASYNC_CONTRACTS_FILE', '').strip()
    # 显式选中的异步合同（`contract_id:version` 逗号分隔）。第八阶段第 2 点：
    # 不自动启用仓库里的示例合同，只有显式列出的合同才参与提交匹配与轮询。
    async_contracts_enabled: str = os.getenv('ASYNC_CONTRACTS_ENABLED', '').strip()
    result_gc_grace_seconds: int = max(0, int(
        os.getenv('RESULT_GC_GRACE_SECONDS', '86400')))

    # ---------- 运维（评审 P2） ----------
    # 单进程同时执行的运行数上限。超过时新任务留在 queued，不再无限起协程。
    # 只约束本进程。多副本之间不做集群级公平调度，重租户仍可以占满其中一个进程。
    max_concurrent_runs: int = max(1, int(
        os.getenv('MAX_CONCURRENT_RUNS', '16')))
    # 事件与产物的保留天数。0 = 不自动清理（运维手动处理）。
    retention_events_days: int = max(0, int(
        os.getenv('RETENTION_EVENTS_DAYS', '30')))
    retention_artifacts_days: int = max(0, int(
        os.getenv('RETENTION_ARTIFACTS_DAYS', '30')))
    # 对账：扫描孤儿运行的间隔（秒）。
    reconcile_interval_seconds: float = max(30.0, float(
        os.getenv('RECONCILE_INTERVAL_SECONDS', '300')))
    # 指标输出格式：json（默认）或 prometheus。
    metrics_format: str = (os.getenv('METRICS_FORMAT', 'json')
                           .strip().lower() or 'json')

settings = Settings()

# 占位符前缀：把 .env.example 原样抄成 .env 时，这些值看起来"已经配上了"，
# 实际一定在第一次模型/检索调用时失败。判定集中在这里，启动检查和运行时
# 兜底才不会各算一套，出现"启动放行、运行炸"的缝。
_PLACEHOLDER_PREFIXES = ('replace-', 'your-', 'changeme')


def is_placeholder(value: str) -> bool:
    """值为空、或仍是示例里的占位符，都算"没配"。"""
    text = (value or '').strip().lower()
    return not text or any(text.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


def missing_runtime_keys() -> list:
    """返回未配置或仍是占位符的必填项名字；空列表表示齐全。

    只做判断不抛异常：启动校验要把名字拼成一条可读的拒绝理由，
    运行期兜底要直接抛错，两边共用这份判定。
    """
    missing = []
    # 检索 key 也要查占位符：deploy/production.env.example 里就是 replace-me，
    # 只判空的话，把示例文件原样部署上去仍会"启动成功、第一次检索 401"。
    if is_placeholder(settings.deepseek_api_key):
        missing.append('DEEPSEEK_API_KEY')
    if is_placeholder(settings.llm_key):
        missing.append('LLM_API_KEY')
    if is_placeholder(settings.llm_model):
        missing.append('LLM_MODEL')
    return missing


def check_runtime_keys():
    # 受控模型脚本不访问供应商。测试和崩溃恢复子进程是在导入之后才写
    # PI_MOCK_SCRIPT，所以看当前环境变量，不看导入时冻进 settings 的空值。
    if os.getenv('PI_MOCK_SCRIPT', '').strip():
        return
    missing = missing_runtime_keys()
    if missing:
        raise RuntimeError('请在 .env 设置：' + ', '.join(missing))
