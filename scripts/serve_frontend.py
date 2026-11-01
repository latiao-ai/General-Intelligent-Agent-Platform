"""Local frontend entry: 8001, forwarding API and SSE to backend 8002."""
import argparse
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

app = FastAPI()
BACKEND = 'http://127.0.0.1:8002'
HOP_HEADERS = {'host', 'connection', 'keep-alive', 'proxy-authenticate',
               'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'}


@app.get('/')
async def index(request: Request):
    # Reuse the platform's verified identity exchange and canvas session mapping.
    return await proxy(request, '')


@app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
async def proxy(request: Request, path: str):
    if path.startswith('canvas/'):
        target = 'http://127.0.0.1:8003/' + path
        if request.url.query:
            target += '?' + request.url.query
        return RedirectResponse(target, status_code=307)
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None), trust_env=False)
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_HEADERS]
    platform_route = not path or path.startswith('api/hub/workbench/')
    url = ('http://127.0.0.1:8004/api/hub/workbench/' if not path else
           'http://127.0.0.1:8004/' + path) if platform_route else BACKEND + '/' + path
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
        return HTMLResponse('<h1>请先登录 BeeFTV</h1><p>中台与 BeeFTV 使用同一账号查看画布对话。</p>'
                            '<a href="http://127.0.0.1:8003/">打开 BeeFTV 登录</a>'
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8001)
    args = parser.parse_args()
    uvicorn.run(app, host='127.0.0.1', port=args.port)
