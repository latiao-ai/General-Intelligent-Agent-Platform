"""本地前端联调代理：默认 8001，把请求分发给中台后端与 BeeFTV。

路由规则（与 docs/local-ports.md 的联调拓扑一致）：

    路径                         去向
    --------------------------  ------------------------------------
    / 与 api/hub/workbench/*    BeeFTV 后端（--platform-backend）
    canvas/*                    BeeFTV 前端（--platform-frontend，307 跳转）
    其余                       中台后端（--backend）

注意：中台**自带完整界面**，起 `python -m scripts.serve_api` 后直接访问
`http://127.0.0.1:8002/` 就能用。本脚本只在需要嵌进 BeeFTV 画布时才有用；
BeeFTV 没起来时根路径会回落到"请先登录 BeeFTV"提示。

    python -m scripts.serve_frontend                       # 127.0.0.1:8001
    python -m scripts.serve_frontend --lan                 # 对局域网开放
    python -m scripts.serve_frontend --port 8011 --backend http://127.0.0.1:9002
"""
import argparse
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

app = FastAPI()
BACKEND = 'http://127.0.0.1:8002'
PLATFORM_BACKEND = 'http://127.0.0.1:8004'
PLATFORM_FRONTEND = 'http://127.0.0.1:8003'
HOP_HEADERS = {'host', 'connection', 'keep-alive', 'proxy-authenticate',
               'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'}


@app.get('/')
async def index(request: Request):
    # Reuse the platform's verified identity exchange and canvas session mapping.
    return await proxy(request, '')


@app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
async def proxy(request: Request, path: str):
    if path.startswith('canvas/'):
        target = PLATFORM_FRONTEND + '/' + path
        if request.url.query:
            target += '?' + request.url.query
        return RedirectResponse(target, status_code=307)
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None), trust_env=False)
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_HEADERS]
    platform_route = not path or path.startswith('api/hub/workbench/')
    if platform_route:
        url = (PLATFORM_BACKEND + '/api/hub/workbench/' if not path
               else PLATFORM_BACKEND + '/' + path)
    else:
        url = BACKEND + '/' + path
    if request.url.query:
        url += '?' + request.url.query
    try:
        upstream = await client.send(client.build_request(
            request.method, url, headers=headers, content=request.stream()), stream=True)
    except Exception:
        await client.aclose()
        raise

    if not path and upstream.status_code == 401:
        await upstream.aclose()
        await client.aclose()
        return HTMLResponse('<h1>请先登录 BeeFTV</h1>'
                            '<p>中台本身自带界面，直接访问中台后端根路径即可。</p>'
                            f'<a href="{PLATFORM_FRONTEND}/">打开 BeeFTV 登录</a>'
                            '<p>登录后刷新本页即可。</p>', status_code=401)

    async def close():
        await upstream.aclose()
        await client.aclose()

    response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 background=BackgroundTask(close))
    response.raw_headers = [(k, v) for k, v in upstream.headers.raw
                            if k.decode().lower() not in HOP_HEADERS]
    if not path:
        response.headers['Cache-Control'] = 'no-store'
    return response


def main():
    global BACKEND, PLATFORM_BACKEND, PLATFORM_FRONTEND
    parser = argparse.ArgumentParser(description='本地前端联调代理（中台后端 + BeeFTV）')
    parser.add_argument('--port', type=int, default=8001)
    parser.add_argument('--host', default=None)
    parser.add_argument('--lan', action='store_true',
                        help='绑定 0.0.0.0，供局域网访问（联调用，别暴露到公网）')
    parser.add_argument('--backend', default=BACKEND, help='中台后端地址')
    parser.add_argument('--platform-backend', default=PLATFORM_BACKEND, help='BeeFTV 后端地址')
    parser.add_argument('--platform-frontend', default=PLATFORM_FRONTEND, help='BeeFTV 前端地址')
    args = parser.parse_args()
    if args.lan and args.host and args.host != '0.0.0.0':
        parser.error('--lan 与指定的 --host 冲突')
    BACKEND = args.backend.rstrip('/')
    PLATFORM_BACKEND = args.platform_backend.rstrip('/')
    PLATFORM_FRONTEND = args.platform_frontend.rstrip('/')
    host = '0.0.0.0' if args.lan else (args.host or '127.0.0.1')
    uvicorn.run(app, host=host, port=args.port)


if __name__ == '__main__':
    main()
