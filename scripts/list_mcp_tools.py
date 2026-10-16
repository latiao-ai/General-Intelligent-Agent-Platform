import asyncio
import json
# 直接跑脚本时（python scripts/xxx.py）sys.path[0] 是 scripts/，项目根不在其中，
# 导入 app.* 会失败。这里显式补上，让"直接跑"和"python -m scripts.xxx"都能用。
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from app.config import settings  # 加载 .env
from app.mcp_adapter import list_server_tools


if __name__ == '__main__':
    print(json.dumps(asyncio.run(list_server_tools()), ensure_ascii=False, indent=2))
