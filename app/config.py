import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / '.env')

@dataclass(frozen=True)
class Settings:
    tavily_key: str = os.getenv('TAVILY_API_KEY', '')
    llm_key: str = os.getenv('LLM_API_KEY', '')
    llm_model: str = os.getenv('LLM_MODEL', '')
    # 整理报告阶段的模型。空值时沿用 LLM_MODEL。
    # 该阶段要读完整正文并产出结构化报告，往往是耗时大头，可单独换更快的模型。
    llm_compose_model: str = os.getenv('LLM_COMPOSE_MODEL', '') or os.getenv('LLM_MODEL', '')
    # 解析阶段关闭模型推理。实测多轮场景 4.4s → 2.3s（约 2 倍），
    # auto_deploy 判断准确率不变（5/5）。耗时几乎全在推理 token 上，与输入长度无关。
    llm_parse_disable_thinking: bool = (
        os.getenv('LLM_PARSE_DISABLE_THINKING', 'false').lower() == 'true')
    llm_base_url: str = os.getenv('LLM_BASE_URL', '')
    max_pages: int = min(5, max(1, int(os.getenv('MAX_PAGES', '5'))))
    max_queries: int = min(2, max(1, int(os.getenv('MAX_QUERIES', '2'))))
    max_source_chars: int = max(500, int(os.getenv('MAX_SOURCE_CHARS', '10000')))
    max_total_chars: int = max(1000, int(os.getenv('MAX_TOTAL_CHARS', '35000')))
    artifact_dir: Path = PROJECT_ROOT / os.getenv('ARTIFACT_DIR', 'artifacts')
    # SQLite 持久化路径：会话、任务与事件日志。改这里也要同步 .gitignore。
    db_path: Path = PROJECT_ROOT / os.getenv('DB_PATH', 'agent.db')
    deploy_enabled: bool = os.getenv('DEPLOY_ENABLED', 'false').lower() == 'true'

settings = Settings()

def check_runtime_keys():
    missing = []
    if not settings.tavily_key or settings.tavily_key == 'replace-me':
        missing.append('TAVILY_API_KEY')
    if not settings.llm_key or settings.llm_key == 'replace-me':
        missing.append('LLM_API_KEY')
    if not settings.llm_model or settings.llm_model.startswith('replace-'):
        missing.append('LLM_MODEL')
    if missing:
        raise RuntimeError('请在 .env 设置：' + ', '.join(missing))
