"""独立 SkillHub 服务：目录 / 详情 / 包下载。

本地：
    python -m scripts.serve_skillhub --port 8010
    # 健康检查 http://127.0.0.1:8010/v1/health

部署到服务器/域名：
    python -m scripts.serve_skillhub --host 0.0.0.0 --port 8010
    # 前面挂 nginx/HTTPS，再把主应用的 SKILLHUB_URL 指向 https://域名/v1/skills

鉴权（可选）：
    设置 SKILLHUB_TOKEN，所有 /v1/*（健康检查除外）要求
    Authorization: Bearer <token>；主应用的 SKILLHUB_TOKEN 要配同一个值。
"""
import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_ROOT / 'src'))

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from webpage_agent.api.skillhub_registry_api import _load, router

load_dotenv(_ROOT / '.env')

app = FastAPI(title='SkillHub', version='1.0')
app.include_router(router)

_TOKEN = os.getenv('SKILLHUB_TOKEN', '').strip()


@app.middleware('http')
async def require_token(request: Request, call_next):
    if (_TOKEN and request.url.path.startswith('/v1/')
            and request.url.path != '/v1/health'):
        if request.headers.get('authorization', '') != f'Bearer {_TOKEN}':
            return JSONResponse({'detail': 'unauthorized'}, status_code=401)
    return await call_next(request)


@app.get('/v1/health')
async def health():
    return {'status': 'ok', 'service': 'skillhub', 'items': len(_load())}


def main():
    parser = argparse.ArgumentParser(description='启动独立 SkillHub 服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8010)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
