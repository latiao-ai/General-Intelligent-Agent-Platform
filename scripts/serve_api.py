"""启动 FastAPI + SSE 服务。

    python -m scripts.serve_api                    # 默认 127.0.0.1:8000
    python -m scripts.serve_api --port 9000        # 换端口
    python -m scripts.serve_api --reload           # 开发模式，改代码自动重启

启动后浏览器打开 http://127.0.0.1:8000/ 即可使用聊天界面。
"""
import argparse

import uvicorn
# 直接跑脚本时（python scripts/xxx.py）sys.path[0] 是 scripts/，项目根不在其中，
# 导入 app.* 会失败。这里显式补上，让"直接跑"和"python -m scripts.xxx"都能用。
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))




def main():
    parser = argparse.ArgumentParser(description='启动网页研究报告 API 服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--reload', action='store_true', help='开发模式：改代码自动重启')
    args = parser.parse_args()
    uvicorn.run('app.api:app', host=args.host, port=args.port, reload=args.reload)


if __name__ == '__main__':
    main()
